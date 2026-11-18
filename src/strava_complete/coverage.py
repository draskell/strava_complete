from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

import geopandas as gpd
from shapely.geometry import GeometryCollection
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .config import TrailError
from .geo import eligible, lines, metric_crs, profile, project, utc_date
from .storage import Store, now


@dataclass
class Coverage:
    boundary: BaseGeometry
    trails: gpd.GeoDataFrame
    covered: gpd.GeoDataFrame
    uncovered: gpd.GeoDataFrame
    metadata: dict[str, Any]


def frame(records: list[dict[str, Any]], crs: Any) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        records,
        columns=["source_id", "name", "tags", "geometry", "length_m"],
        geometry="geometry",
        crs=crs,
    )


def measure(
    boundary: BaseGeometry,
    trails: gpd.GeoDataFrame,
    tracks: gpd.GeoDataFrame,
    tolerance: float,
    crs: str | None = None,
) -> Coverage:
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise TrailError("GPS tolerance must be a positive finite number of meters.")
    projection = metric_crs(boundary, crs)
    trails = trails.to_crs(projection)
    tracks = tracks.to_crs(projection)
    whole: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    seen: dict[tuple[str, ...], BaseGeometry] = {}
    projected_boundary = project(boundary, "EPSG:4326", projection)
    for row in trails.sort_values("source_id").itertuples():
        tags = row.tags
        grade = tuple(
            str(tags.get(key, default))
            for key, default in (("layer", "0"), ("bridge", "no"), ("tunnel", "no"))
        )
        geometry = lines(row.geometry.intersection(projected_boundary))
        geometry = lines(geometry.difference(seen.get(grade, GeometryCollection())))
        if geometry.is_empty:
            continue
        seen[grade] = unary_union([seen.get(grade, GeometryCollection()), geometry])
        indices = tracks.sindex.query(geometry.buffer(tolerance), predicate="intersects")
        mask: BaseGeometry = GeometryCollection()
        for offset in range(0, len(indices), 128):
            batch = tracks.iloc[indices[offset : offset + 128]]
            # Flat caps avoid claiming a tolerance-radius extension beyond a track's endpoints.
            local = batch.geometry.intersection(geometry.buffer(2 * tolerance))
            mask = unary_union([mask, *local.buffer(tolerance, cap_style="flat").tolist()])
        matched = lines(geometry.intersection(mask))
        remaining = lines(geometry.difference(mask))
        properties = {
            "source_id": row.source_id,
            "name": tags.get("name", f"OSM way {row.source_id}"),
            "tags": tags,
        }
        whole.append({**properties, "geometry": geometry, "length_m": geometry.length})
        if not matched.is_empty:
            covered.append({**properties, "geometry": matched, "length_m": matched.length})
        if not remaining.is_empty:
            uncovered.append({**properties, "geometry": remaining, "length_m": remaining.length})
    total = sum(row["length_m"] for row in whole)
    completed = sum(row["length_m"] for row in covered)
    left = sum(row["length_m"] for row in uncovered)
    if not math.isclose(completed + left, total, rel_tol=1e-8, abs_tol=1e-6):
        raise TrailError("Geometry accounting failed: covered + uncovered does not equal total.")
    return Coverage(
        boundary,
        frame(whole, projection).to_crs(4326),
        frame(covered, projection).to_crs(4326),
        frame(uncovered, projection).to_crs(4326),
        {
            "total_m": total,
            "covered_m": completed,
            "uncovered_m": left,
            "percent": completed / total * 100 if total else None,
            "tolerance_m": tolerance,
            "crs": projection.to_string(),
            "method": "unique trail length within flat-capped GPS track buffers",
            "generated_at": now(),
            "warnings": [
                "Proximity estimate, not map-matching: parallel trails/crossings may overcount.",
                "OSM completeness and access tags are not guaranteed; verify local access.",
                "Coverage reveals private activity information; do not publish personal reports.",
            ],
        },
    )


def analyze(
    store: Store,
    area: str,
    sports: list[str],
    start: str | None = None,
    end: str | None = None,
    tolerance: float = 15,
    mode: str | None = None,
    filters: dict[str, str] | None = None,
    crs: str | None = None,
) -> Coverage:
    start, end = utc_date(start), utc_date(end)
    if start and end and start >= end:
        raise TrailError("--start must precede the exclusive --end.")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise TrailError("GPS tolerance must be positive and finite.")
    chosen_profile = profile(sports, mode)
    boundary = store.area(area)
    trails, snapshot = store.trails(area)
    selected = (
        trails["tags"].map(lambda tags: eligible(tags, chosen_profile, filters or {})).astype(bool)
    )
    trails = trails.loc[selected]
    projection = metric_crs(boundary, crs)
    expanded = project(
        project(boundary, "EPSG:4326", projection).buffer(tolerance),
        projection,
        "EPSG:4326",
    )
    tracks = store.candidates(expanded.bounds, sports, start, end)
    result = measure(boundary, trails, tracks, tolerance, projection.to_string())
    marks = ",".join("?" for _ in sports)
    counts = dict(
        store.db.execute(
            f"""SELECT stream_status,COUNT(*) FROM activities WHERE sport IN ({marks})
            AND (? IS NULL OR start_date>=?) AND (? IS NULL OR start_date<?)
            GROUP BY stream_status""",
            (*sports, start, start, end, end),
        ).fetchall()
    )
    sync = store.state("sync", {})
    resolutions = Counter(
        json.loads(row[0]).get("latlng", {}).get("resolution", "local/unreported")
        for row in store.db.execute(
            f"""SELECT s.info FROM track_sources s JOIN activities a ON a.id=s.activity
                WHERE a.sport IN ({marks}) AND a.stream_status='ready'
                AND (? IS NULL OR a.start_date>=?) AND (? IS NULL OR a.start_date<?)""",
            (*sports, start, start, end, end),
        )
    )
    result.metadata.update(
        {
            "area": area,
            "sports": sports,
            "profile": chosen_profile,
            "start": start,
            "end": end,
            "tag_filters": filters or {},
            "snapshot": snapshot,
            "activity_counts": counts,
            "sync": sync,
            "gps_resolutions": dict(resolutions),
            "history_scope": sync.get("scope", "unknown"),
            "candidate_track_parts": len(tracks),
            "history_complete": sync.get("phase") == "complete" and counts.get("pending", 0) == 0,
        }
    )
    if not result.metadata["history_complete"]:
        result.metadata["warnings"].append("Activity history import is absent or incomplete.")
    if counts.get("skipped"):
        result.metadata["warnings"].append(
            "Some activities have no usable GPS; inspect data activities for skip reasons."
        )
    if sync.get("scope") != "activity:read_all":
        result.metadata["warnings"].append(
            "Private-history access is absent/unverified; results cover API-visible/imported data."
        )
    if result.metadata["percent"] is None:
        result.metadata["warnings"].append("No eligible trail length; percentage is undefined.")
    return result
