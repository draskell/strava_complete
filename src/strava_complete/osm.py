from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import osmnx as ox
import requests
from filelock import FileLock
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .config import Settings, TrailError, private_directory, private_write
from .geo import HIGHWAYS, lines, validate_boundary
from .storage import Store

NOMINATIM_POLICY = "https://operations.osmfoundation.org/policies/nominatim/"


def place_boundary(query: str, settings: Settings, public_opt_in: bool) -> BaseGeometry:
    if "nominatim.openstreetmap.org" in settings.nominatim_url and not public_opt_in:
        raise TrailError(
            "Public Nominatim requires --allow-public-nominatim after reviewing "
            f"{NOMINATIM_POLICY}. Max 1 request/sec, cached single-user lookups only; "
            "no autocomplete or bulk geocoding. Or configure another nominatim_url."
        )
    cache = private_directory(settings.cache_dir / "geocoding")
    digest = hashlib.sha256(f"{settings.nominatim_url}:{query}".encode()).hexdigest()
    path = cache / f"{digest}.json"
    with FileLock(str(cache / "request.lock"), timeout=60):
        if path.exists():
            try:
                results = json.loads(path.read_text())
            except ValueError as exc:
                raise TrailError(f"Invalid geocoding cache; remove {path} and retry.") from exc
        else:
            stamp = cache / "last-request"
            previous = float(stamp.read_text()) if stamp.exists() else 0
            time.sleep(max(0, 1.1 - (time.time() - previous)))
            private_write(stamp, str(time.time()))
            try:
                response = httpx.get(
                    settings.nominatim_url.rstrip("/") + "/search",
                    params={"q": query, "format": "jsonv2", "polygon_geojson": 1, "limit": 5},
                    headers={"User-Agent": settings.user_agent},
                    timeout=60,
                )
                response.raise_for_status()
                results = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise TrailError(
                    "Place lookup failed; check the provider or use a boundary file."
                ) from exc
            private_write(path, json.dumps(results))
    if not isinstance(results, list):
        raise TrailError("Geocoding provider returned an invalid response.")
    polygons = []
    for item in results:
        if not isinstance(item, dict):
            raise TrailError("Geocoding provider returned a malformed result.")
        geometry = item.get("geojson")
        if isinstance(geometry, dict) and geometry.get("type") in {"Polygon", "MultiPolygon"}:
            polygons.append(item)
    if len(polygons) != 1:
        names = "; ".join(str(item.get("display_name", "")) for item in polygons)
        raise TrailError(
            f"Place lookup found {len(polygons)} polygon candidates: {names}. "
            "Use a more specific name or an explicit GeoJSON/bbox; no boundary was selected."
        )
    return validate_boundary(shape(polygons[0]["geojson"]))


def resolve_area(
    settings: Settings,
    bbox: str | None,
    geojson: Path | None,
    place: str | None,
    public_opt_in: bool,
) -> tuple[BaseGeometry, str]:
    if sum(value is not None for value in (bbox, geojson, place)) != 1:
        raise TrailError("Provide exactly one of --bbox, --geojson, or --place.")
    if bbox is not None:
        try:
            bounds = [float(value) for value in bbox.split(",")]
        except ValueError as exc:
            raise TrailError("Bbox must be west,south,east,north in decimal degrees.") from exc
        if (
            len(bounds) != 4
            or not all(math.isfinite(value) for value in bounds)
            or bounds[0] >= bounds[2]
            or bounds[1] >= bounds[3]
        ):
            raise TrailError("Bbox must be ordered west,south,east,north.")
        return validate_boundary(box(*bounds)), f"bbox:{bbox}"
    if geojson is not None:
        try:
            data = json.loads(geojson.read_text())
            if data.get("crs") is not None:
                raise TrailError("Use RFC 7946 GeoJSON in WGS84, without a legacy crs member.")
            if data["type"] == "FeatureCollection":
                geometries = [shape(feature["geometry"]) for feature in data["features"]]
            elif data["type"] == "Feature":
                geometries = [shape(data["geometry"])]
            else:
                geometries = [shape(data)]
            for geometry in geometries:
                validate_boundary(geometry)
            geometry = validate_boundary(unary_union(geometries))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TrailError(f"Cannot read polygon GeoJSON: {exc}") from exc
        return geometry, "geojson"
    assert place is not None
    return place_boundary(place, settings, public_opt_in), f"place:{place}"


def normalize_trails(features: gpd.GeoDataFrame, boundary: BaseGeometry) -> gpd.GeoDataFrame:
    if features.crs is None:
        raise TrailError("Downloaded trails have no CRS.")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in features.to_crs(4326).iterrows():
        element, osm_id = index
        if element != "way" or str(osm_id) in seen:
            continue
        if row.geometry.geom_type not in {"LineString", "MultiLineString"}:
            continue
        clipped = lines(row.geometry.intersection(boundary))
        if clipped.is_empty:
            continue
        tags = {
            key: value
            for key, value in row.items()
            if key != "geometry"
            and isinstance(value, (str, int, float, bool))
            and not (isinstance(value, float) and not math.isfinite(value))
        }
        records.append({"source_id": str(osm_id), "geometry": clipped, "tags": tags})
        seen.add(str(osm_id))
    return gpd.GeoDataFrame(
        records,
        columns=["source_id", "geometry", "tags"],
        geometry="geometry",
        crs=4326,
    )


def download_trails(store: Store, name: str, settings: Settings, highways: list[str]) -> int:
    boundary = store.area(name)
    if not highways or any(not item.strip() for item in highways):
        raise TrailError("Select at least one non-empty OSM highway value.")
    ox.settings.use_cache = True
    ox.settings.cache_folder = private_directory(settings.cache_dir / "overpass")
    ox.settings.http_user_agent = settings.user_agent
    ox.settings.overpass_url = settings.overpass_url
    ox.settings.requests_timeout = 180
    try:
        features = ox.features.features_from_polygon(boundary, {"highway": highways})
    except ox._errors.InsufficientResponseError as exc:
        raise TrailError(
            "OSM returned no matching features; check the area and highway filters."
        ) from exc
    except (requests.RequestException, ox._errors.ResponseStatusCodeError, ValueError) as exc:
        raise TrailError("Trail download failed; retry later or select a smaller area.") from exc
    trails = normalize_trails(features, boundary)
    if trails.empty:
        raise TrailError("No linear trails found; the previous snapshot, if any, is unchanged.")
    return store.add_snapshot(
        name,
        trails,
        json.dumps({"provider": "OpenStreetMap/Overpass", "highways": highways or list(HIGHWAYS)}),
    )
