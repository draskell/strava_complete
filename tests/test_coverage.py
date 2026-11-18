from __future__ import annotations

import math

import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiPolygon, box
from shapely.geometry.base import BaseGeometry

from strava_complete.config import TrailError
from strava_complete.coverage import analyze, measure
from strava_complete.geo import eligible, metric_crs, profile, track_parts, utc_date
from strava_complete.storage import Store

from .conftest import track_frame, trail_frame, wgs


@pytest.mark.parametrize(("distance", "expected"), [(0, 0), (400, 40), (1000, 100)])
def test_known_lengths(
    boundary: BaseGeometry,
    trail: gpd.GeoDataFrame,
    distance: int,
    expected: float,
) -> None:
    tracks = track_frame(
        *([LineString([(500050, 5000500), (500050 + distance, 5000500)])] if distance else [])
    )
    result = measure(boundary, trail, tracks, 15, "EPSG:32610")
    assert result.metadata["total_m"] == pytest.approx(1000, abs=1e-6)
    assert result.metadata["percent"] == pytest.approx(expected, abs=1e-6)
    assert result.metadata["covered_m"] + result.metadata["uncovered_m"] == pytest.approx(1000)


def test_repeated_reversed_and_duplicate_trails(
    boundary: BaseGeometry,
    trail: gpd.GeoDataFrame,
) -> None:
    geometry = LineString([(500050, 5000500), (500450, 5000500)])
    trails = trail_frame(
        LineString([(500050, 5000500), (501050, 5000500)]),
        LineString([(501050, 5000500), (500050, 5000500)]),
    )
    result = measure(boundary, trails, track_frame(geometry, geometry.reverse()), 15, "EPSG:32610")
    assert result.metadata["percent"] == pytest.approx(40, abs=1e-6)
    assert result.metadata["total_m"] == pytest.approx(1000)


def test_different_grades_preserved(boundary: BaseGeometry) -> None:
    line = LineString([(500050, 5000500), (501050, 5000500)])
    trails = trail_frame(line, line)
    trails.at[1, "tags"] = {"highway": "path", "bridge": "yes", "layer": "1"}
    result = measure(boundary, trails, track_frame(), 15)
    assert result.metadata["total_m"] == pytest.approx(2000)


def test_empty_denominator(boundary: BaseGeometry) -> None:
    result = measure(boundary, trail_frame(), track_frame(), 15)
    assert result.metadata["percent"] is None


@pytest.mark.parametrize("tolerance", [0, -1, math.nan, math.inf])
def test_invalid_tolerance(
    boundary: BaseGeometry, trail: gpd.GeoDataFrame, tolerance: float
) -> None:
    with pytest.raises(TrailError, match="positive"):
        measure(boundary, trail, track_frame(), tolerance)


def test_parallel_false_positive_is_explicit(
    boundary: BaseGeometry, trail: gpd.GeoDataFrame
) -> None:
    result = measure(
        boundary,
        trail,
        track_frame(LineString([(500050, 5000510), (501050, 5000510)])),
        15,
    )
    assert result.metadata["percent"] == pytest.approx(100, abs=1e-6)
    assert "parallel" in result.metadata["warnings"][0]


def test_boundary_adjacent_tracks_and_date_sport_filters(seeded: Store) -> None:
    boundary = wgs(box(500000, 5000000, 501100, 5001000))
    seeded.add_area("edge", boundary, "synthetic")
    seeded.add_snapshot(
        "edge", trail_frame(LineString([(500050, 5000000), (501050, 5000000)])), "synthetic"
    )
    for activity, sport, date in [
        (1, "Hike", "2024-01-01T00:00:00+00:00"),
        (2, "Ride", "2024-02-01T00:00:00+00:00"),
    ]:
        with seeded.transaction():
            seeded.upsert_activity({"id": activity, "sport_type": sport, "start_date": date})
        seeded.save_tracks(activity, [wgs(LineString([(500050, 4999990), (500450, 4999990)]))])
    result = analyze(seeded, "edge", ["Hike"], start="2024-01-01", end="2024-02-01")
    assert result.metadata["percent"] == pytest.approx(40, abs=1e-6)
    assert result.metadata["candidate_track_parts"] == 1
    assert not result.metadata["history_complete"]
    assert analyze(seeded, "edge", ["Hike"], end="2024-01-01").metadata["percent"] == 0


def test_disjoint_and_hole_clipping() -> None:
    first = box(500000, 5000000, 500300, 5001000).difference(box(500100, 5000400, 500200, 5000600))
    boundary = wgs(MultiPolygon([first, box(500600, 5000000, 501000, 5001000)]))
    trails = trail_frame(LineString([(500000, 5000500), (501000, 5000500)]))
    result = measure(boundary, trails, track_frame(), 15, "EPSG:32610")
    assert result.metadata["total_m"] == pytest.approx(600, abs=1e-6)


@pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:3857", "EPSG:32633"])
def test_invalid_projection(boundary: BaseGeometry, crs: str) -> None:
    with pytest.raises(TrailError):
        metric_crs(boundary, crs)


def test_oversize_and_polar_areas_rejected() -> None:
    for geometry in [box(-125, 40, -110, 45), box(0, 85, 1, 86)]:
        with pytest.raises(TrailError):
            metric_crs(geometry)


def test_access_and_profiles() -> None:
    assert eligible({"access": "private", "foot": "yes"}, "foot", {})
    assert not eligible({"access": "yes", "foot": "no"}, "foot", {})
    assert not eligible({"access": "private"}, "bicycle", {})
    assert eligible({"bicycle": "designated"}, "bicycle", {})
    assert not eligible({"surface": "asphalt"}, "foot", {"surface": "dirt"})
    assert not eligible({"footway": "sidewalk"}, "foot", {})
    assert profile(["Hike", "TrailRun"]) == "foot"
    with pytest.raises(TrailError):
        profile(["Swim"])
    assert profile(["Swim"], "foot") == "foot"
    with pytest.raises(TrailError):
        profile(["Hike", "Ride"])


def test_time_gaps_and_invalid_streams() -> None:
    parts, note = track_parts(
        [[45, -123], [45.001, -123], [45.1, -123], [45.101, -123]], [0, 10, 500, 510]
    )
    assert len(parts) == 2
    assert note and "1 recording gaps" in note
    assert track_parts([], []) == ([], None)
    with pytest.raises(TrailError):
        track_parts([[45, -123]], [])
    with pytest.raises(TrailError):
        track_parts([[math.nan, -123]], [0])
    assert utc_date("2024-01-01") == "2024-01-01T00:00:00+00:00"
    assert utc_date("2024-01-01T01:00:00+01:00") == utc_date("2024-01-01")


def test_invalid_date_range(seeded: Store) -> None:
    with pytest.raises(TrailError, match="precede"):
        analyze(seeded, "demo", ["Hike"], start="2024-02-01", end="2024-01-01")


def test_empty_snapshot_has_undefined_coverage(seeded: Store) -> None:
    seeded.add_snapshot("demo", trail_frame(), "synthetic empty snapshot")
    assert analyze(seeded, "demo", ["Hike"]).metadata["percent"] is None


def test_many_track_batches_count_only_unique_length(
    boundary: BaseGeometry,
    trail: gpd.GeoDataFrame,
) -> None:
    part = LineString([(500050, 5000500), (500450, 5000500)])
    result = measure(boundary, trail, track_frame(*([part] * 260)), 15)
    assert result.metadata["covered_m"] == pytest.approx(400, abs=1e-6)
    assert result.metadata["percent"] == pytest.approx(40, abs=1e-6)
