from __future__ import annotations

import csv
import html
import io
import json
import re
from pathlib import Path
from typing import Any

import folium
from branca.element import Figure
from shapely.geometry import mapping

from .config import TrailError, private_write
from .coverage import Coverage


def geojson(result: Coverage, status: str) -> dict[str, Any]:
    data = result.covered if status == "covered" else result.uncovered
    return {
        "type": "FeatureCollection",
        "attribution": "© OpenStreetMap contributors; ODbL. Activity data: Strava.",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(row.geometry),
                "properties": {
                    "osm_id": row.source_id,
                    "name": str(row.name),
                    "status": status,
                    "length_m": row.length_m,
                    "access_tags": row.tags,
                },
            }
            for row in data.itertuples()
        ],
    }


def map_html(result: Coverage, basemap: bool = True) -> str:
    west, south, east, north = result.boundary.bounds
    view = folium.Map(
        location=[(south + north) / 2, (west + east) / 2],
        tiles="OpenStreetMap" if basemap else None,
    )
    root = Figure()
    view.add_to(root)
    folium.GeoJson(
        mapping(result.boundary),
        name="Area boundary",
        style_function=lambda feature: {"color": "#444444", "fillOpacity": 0.02, "weight": 2},
    ).add_to(view)
    for status, color in (("covered", "#16834a"), ("uncovered", "#c43a28")):
        data = geojson(result, status)
        if not data["features"]:
            folium.FeatureGroup(name=status.capitalize()).add_to(view)
            continue
        for feature in data["features"]:
            props = feature["properties"]
            props["name"] = html.escape(props["name"])
            props["access_tags"] = html.escape(json.dumps(props["access_tags"], sort_keys=True))
        # Folium embeds GeoJSON inside a script; escape '<' even inside JSON string values.
        serialized = json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e")
        folium.GeoJson(
            serialized,
            name=status.capitalize(),
            style_function=lambda feature, line_color=color: {
                "color": line_color,
                "weight": 5,
                "opacity": 0.9 if line_color == "#16834a" else 0.8,
                "dashArray": None if line_color == "#16834a" else "7 4",
            },
            tooltip=folium.GeoJsonTooltip(fields=["name", "length_m"]),
            popup=folium.GeoJsonPopup(fields=["name", "status", "length_m", "access_tags"]),
        ).add_to(view)
    metadata = result.metadata
    percentage = metadata["percent"]
    value = f"{percentage:.2f}%" if percentage is not None else "undefined (no eligible trails)"
    title = html.escape(str(metadata.get("area", "Trail coverage")))
    sports = html.escape(", ".join(metadata.get("sports", [])))
    dates = html.escape(
        f"{metadata.get('start') or 'all time'} to {metadata.get('end') or 'latest import'}"
    )
    skipped = metadata.get("activity_counts", {}).get("skipped", 0)
    summary = (
        '<div style="position:fixed;bottom:25px;left:25px;z-index:9999;background:white;'
        'padding:14px;max-width:380px;border:1px solid #555;font:14px sans-serif">'
        f"<b>{title}: {value}</b><br>"
        f"{sports}<br>{dates} (end exclusive)<br>"
        f"Covered {metadata['covered_m'] / 1000:.2f} / "
        f"{metadata['total_m'] / 1000:.2f} km<br>"
        '<span style="color:#16834a">Solid green: covered</span>; '
        '<span style="color:#c43a28">dashed red: uncovered</span><br>'
        f"GPS tolerance: {metadata['tolerance_m']:g} m. Proximity estimate only.<br>"
        + ("History import incomplete.<br>" if not metadata.get("history_complete") else "")
        + (f"{skipped} activities skipped (no usable GPS).<br>" if skipped else "")
        + (
            "Private-history access absent/unverified.<br>"
            if metadata.get("history_scope") != "activity:read_all"
            else ""
        )
        + "Private report. Activity data: Strava.<br>"
        "Garmin-recorded activity data: Garmin.<br>"
        '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors / ODbL</a>'
        "</div>"
    )
    root.html.add_child(folium.Element(summary))
    folium.LayerControl().add_to(view)
    view.fit_bounds([[south, west], [north, east]])
    return root.render()


def export(
    result: Coverage, directory: Path, name: str = "coverage", basemap: bool = True
) -> list[Path]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise TrailError("Report name must contain only ASCII letters, numbers, '-' or '_'.")
    outputs = {
        f"{name}.html": map_html(result, basemap),
        f"{name}.json": json.dumps(result.metadata, indent=2),
        f"{name}-covered.geojson": json.dumps(geojson(result, "covered")),
        f"{name}-uncovered.geojson": json.dumps(geojson(result, "uncovered")),
    }
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["osm_id", "name", "total_m", "covered_m", "uncovered_m", "percent"])
    covered = dict(zip(result.covered.source_id, result.covered.length_m, strict=True))
    for row in result.trails.itertuples():
        length = covered.get(row.source_id, 0)
        # Avoid spreadsheet formula execution when opening externally supplied OSM trail names.
        label = str(row.name)
        if label.lstrip().startswith(("=", "+", "-", "@")):
            label = "'" + label
        writer.writerow(
            [
                row.source_id,
                label,
                row.length_m,
                length,
                row.length_m - length,
                length / row.length_m * 100,
            ]
        )
    outputs[f"{name}.csv"] = buffer.getvalue()
    for filename, content in outputs.items():
        private_write(directory / filename, content)
    return [directory / filename for filename in outputs]
