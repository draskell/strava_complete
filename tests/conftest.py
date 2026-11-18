from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import geopandas as gpd
import pytest
import respx
from shapely.geometry import LineString, box
from shapely.geometry.base import BaseGeometry

from strava_complete.config import Settings
from strava_complete.geo import project
from strava_complete.storage import Store


@pytest.fixture(autouse=True)
def no_live_http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        tmp_path / "private",
        tmp_path / "cache",
        client_id="synthetic-client",
        client_secret="synthetic-secret",
    )


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    value = Store(settings.db_path)
    yield value
    value.close()


def wgs(geometry: BaseGeometry) -> BaseGeometry:
    return project(geometry, "EPSG:32610", "EPSG:4326")


@pytest.fixture
def boundary() -> BaseGeometry:
    return wgs(box(500000, 5000000, 501100, 5001000))


def trail_frame(*geometries: BaseGeometry) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        [
            {
                "source_id": str(i),
                "geometry": wgs(geometry),
                "tags": {"name": f"Synthetic trail {i}", "highway": "path"},
            }
            for i, geometry in enumerate(geometries)
        ],
        columns=["source_id", "geometry", "tags"],
        crs=4326,
    )


def track_frame(*geometries: BaseGeometry) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"geometry": [wgs(geometry) for geometry in geometries]},
        geometry="geometry",
        crs=4326,
    )


@pytest.fixture
def trail() -> gpd.GeoDataFrame:
    return trail_frame(LineString([(500050, 5000500), (501050, 5000500)]))


@pytest.fixture
def seeded(store: Store, boundary: BaseGeometry, trail: gpd.GeoDataFrame) -> Store:
    store.add_area("demo", boundary, "synthetic")
    store.add_snapshot("demo", trail, "synthetic")
    return store
