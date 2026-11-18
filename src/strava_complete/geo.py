from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import geopandas as gpd
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely import transform
from shapely.geometry import GeometryCollection, LineString, MultiLineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .config import TrailError

SPORTS = {
    "Hike": "foot",
    "Walk": "foot",
    "Run": "foot",
    "TrailRun": "foot",
    "Ride": "bicycle",
    "MountainBikeRide": "bicycle",
    "GravelRide": "bicycle",
    "EBikeRide": "bicycle",
    "EMountainBikeRide": "bicycle",
}
HIGHWAYS = ("path", "footway", "bridleway", "track")
DENIED = {"no", "private", "customers", "delivery", "agricultural", "forestry"}
ALLOWED = {"yes", "designated", "official", "permissive", "destination"}


def line_parts(geometry: BaseGeometry) -> list[BaseGeometry]:
    if isinstance(geometry, LineString):
        return [geometry] if geometry.length > 0 else []
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        return [part for item in geometry.geoms for part in line_parts(item)]
    return []


def lines(geometry: BaseGeometry) -> BaseGeometry:
    parts = line_parts(geometry)
    return unary_union(parts) if parts else GeometryCollection()


def project(geometry: BaseGeometry, source: CRS | str, target: CRS | str) -> BaseGeometry:
    return transform(
        geometry,
        Transformer.from_crs(source, target, always_xy=True).transform,
        interleaved=False,
    )


def metric_crs(boundary: BaseGeometry, explicit: str | None = None) -> CRS:
    west, south, east, north = boundary.bounds
    if east - west > 6 or north - south > 6 or south < -80 or north > 84:
        raise TrailError(
            "Area exceeds compact UTM coverage; split it into smaller non-polar areas."
        )
    estimated = gpd.GeoSeries([boundary], crs=4326).estimate_utm_crs()
    try:
        result = CRS(explicit or estimated)
    except CRSError as exc:
        raise TrailError("Invalid projection; use a local EPSG code in meters.") from exc
    if (
        not result.is_projected
        or result.to_epsg() == 3857
        or any(axis.unit_name != "metre" for axis in result.axis_info)
    ):
        raise TrailError("Use a local projected CRS in meters, not degrees or Web Mercator.")
    extent = result.area_of_use
    if extent and not (
        extent.west <= (west + east) / 2 <= extent.east
        and extent.south <= (south + north) / 2 <= extent.north
    ):
        raise TrailError("Projection is outside its area of use.")
    return result


def validate_boundary(geometry: BaseGeometry) -> BaseGeometry:
    if geometry.geom_type not in {"Polygon", "MultiPolygon"} or geometry.is_empty:
        raise TrailError("Area must be a non-empty Polygon or MultiPolygon.")
    if not geometry.is_valid:
        raise TrailError("Area polygon is invalid; repair the boundary file before importing it.")
    west, south, east, north = geometry.bounds
    if not all(math.isfinite(v) for v in geometry.bounds) or not (
        -180 <= west < east <= 180 and -90 <= south < north <= 90
    ):
        raise TrailError("Invalid WGS84 bounds or antimeridian-crossing area.")
    metric_crs(geometry)
    return geometry


def profile(sports: list[str], override: str | None = None) -> str:
    if not sports:
        raise TrailError("Select at least one exact Strava sport type.")
    if override is not None:
        if override not in {"foot", "bicycle"}:
            raise TrailError("Trail profile must be foot or bicycle.")
        return override
    if any(sport not in SPORTS for sport in sports):
        raise TrailError("Unsupported sport: select an explicit --profile foot or bicycle.")
    modes = {SPORTS[sport] for sport in sports}
    if len(modes) != 1:
        raise TrailError(
            "Mixed foot/bicycle sports need an explicit --profile for the denominator."
        )
    return modes.pop()


def eligible(tags: dict[str, Any], mode: str, filters: dict[str, str]) -> bool:
    if any(str(tags.get(key, "")) != value for key, value in filters.items()):
        return False
    if str(tags.get("area", "")).lower() == "yes":
        return False
    specific = str(tags.get(mode, "")).lower()
    general = str(tags.get("access", "")).lower()
    if specific in DENIED or tags.get("footway") == "sidewalk":
        return False
    if mode == "bicycle" and specific in {"dismount", "use_sidepath"}:
        return False
    return specific in ALLOWED or general not in DENIED


def utc_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrailError(f"Invalid ISO date/time {value!r}.") from exc
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()


def track_parts(
    coordinates: Iterable[Any],
    times: Iterable[Any],
    max_gap_seconds: float = 120,
) -> tuple[list[BaseGeometry], str | None]:
    coords, seconds = list(coordinates), list(times)
    if len(coords) != len(seconds):
        raise TrailError("GPS and time streams have different lengths.")
    parts: list[BaseGeometry] = []
    current: list[tuple[float, float]] = []
    previous: float | None = None
    gaps = 0
    for pair, timestamp in zip(coords, seconds, strict=True):
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in pair)
            or not -90 <= pair[0] <= 90
            or not -180 <= pair[1] <= 180
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
        ):
            raise TrailError("Malformed GPS/time stream.")
        if previous is not None and (
            timestamp <= previous or timestamp - previous > max_gap_seconds
        ):
            if len(set(current)) >= 2:
                parts.append(LineString(current))
            current = []
            gaps += 1
        current.append((pair[1], pair[0]))
        previous = timestamp
    if len(set(current)) >= 2:
        parts.append(LineString(current))
    return parts, f"Split {gaps} recording gaps (>120s or non-increasing time)." if gaps else None
