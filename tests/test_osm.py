from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import httpx
import pandas as pd
import pytest
import respx
from shapely.geometry import LineString, box, mapping
from shapely.geometry.base import BaseGeometry

from strava_complete.config import Settings, TrailError
from strava_complete.osm import download_trails, normalize_trails, place_boundary, resolve_area
from strava_complete.storage import Store

from .conftest import wgs


def test_bbox_and_geojson(settings: Settings, tmp_path: Path) -> None:
    boundary, _ = resolve_area(settings, "-123,45,-122.9,45.1", None, None, False)
    assert boundary.bounds == (-123, 45, -122.9, 45.1)
    shape = box(-123, 45, -122.9, 45.1).difference(box(-122.98, 45.02, -122.95, 45.05))
    path = tmp_path / "area.geojson"
    path.write_text(json.dumps({"type": "Feature", "geometry": mapping(shape), "properties": {}}))
    boundary, _ = resolve_area(settings, None, path, None, False)
    assert len(boundary.interiors) == 1
    with pytest.raises(TrailError, match="exactly one"):
        resolve_area(settings, "-123,45,-122.9,45.1", path, None, False)


@pytest.mark.parametrize(
    "bbox",
    [
        "1,2,3",
        "no",
        "3,2,1,4",
        "-190,40,-180,41",
        "nan,40,1,41",
        "0,0,50,50",
    ],
)
def test_invalid_bbox(settings: Settings, bbox: str) -> None:
    with pytest.raises(TrailError):
        resolve_area(settings, bbox, None, None, False)


def test_geocoder_opt_in_ambiguity_and_cache(
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    response = [{"geojson": mapping(box(-123, 45, -122.9, 45.1)), "display_name": "Synthetic Park"}]
    with pytest.raises(TrailError, match="allow-public"):
        place_boundary("Synthetic Park", settings, False)
    route = no_live_http.get(settings.nominatim_url.rstrip("/") + "/search").respond(
        200, json=response
    )
    first = place_boundary("Synthetic Park", settings, True)
    assert first.equals(place_boundary("Synthetic Park", settings, True))
    assert route.call_count == 1
    assert "trail-coverage" in route.calls[0].request.headers["User-Agent"]
    route.respond(200, json=response * 2)
    with pytest.raises(TrailError, match="2 polygon candidates"):
        place_boundary("Ambiguous", settings, True)
    route.respond(503)
    with pytest.raises(TrailError, match="lookup failed"):
        place_boundary("Unavailable", settings, True)


def test_trail_normalization_clips_and_deduplicates(boundary: BaseGeometry) -> None:
    features = gpd.GeoDataFrame(
        {
            "highway": ["path"] * 3,
            "name": ["trail"] * 3,
            "geometry": [wgs(LineString([(499000, 5000500), (502000, 5000500)]))] * 3,
        },
        index=pd.MultiIndex.from_tuples([("way", 1), ("way", 1), ("relation", 2)]),
        crs=4326,
    )
    result = normalize_trails(features, boundary)
    assert len(result) == 1
    assert result.iloc[0].geometry.difference(boundary).is_empty
    assert result.to_crs(32610).length.sum() == pytest.approx(1100, abs=1e-4)


def test_geocoder_malformed_json(settings: Settings, no_live_http: respx.MockRouter) -> None:
    no_live_http.get(settings.nominatim_url.rstrip("/") + "/search").mock(
        return_value=httpx.Response(200, text="not JSON")
    )
    with pytest.raises(TrailError, match="lookup failed"):
        place_boundary("Broken", settings, True)


def test_download_snapshots_and_failure_preserves_previous(
    seeded: Store,
    settings: Settings,
    boundary: BaseGeometry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import osmnx as ox

    features = gpd.GeoDataFrame(
        {
            "highway": ["path"],
            "geometry": [
                wgs(
                    LineString(
                        [
                            (500050, 5000500),
                            (501050, 5000500),
                        ]
                    )
                )
            ],
        },
        index=pd.MultiIndex.from_tuples([("way", 100)]),
        crs=4326,
    )
    monkeypatch.setattr(ox.features, "features_from_polygon", lambda *args: features)
    snapshot = download_trails(seeded, "demo", settings, ["path"])
    assert seeded.trails("demo")[1]["id"] == snapshot

    def failed(*args: object) -> None:
        raise ox._errors.ResponseStatusCodeError("synthetic server failure")

    monkeypatch.setattr(ox.features, "features_from_polygon", failed)
    with pytest.raises(TrailError, match="download failed"):
        download_trails(seeded, "demo", settings, ["path"])
    assert seeded.trails("demo")[1]["id"] == snapshot
