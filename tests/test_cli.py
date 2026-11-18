from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString
from typer.testing import CliRunner

from strava_complete.cli import app
from strava_complete.config import Settings
from strava_complete.coverage import analyze
from strava_complete.report import export
from strava_complete.storage import Store

from .conftest import wgs

runner = CliRunner()


def test_offline_cli_workflow(seeded: Store, settings: Settings) -> None:
    with seeded.transaction():
        seeded.upsert_activity(
            {"id": 1, "sport_type": "Hike", "start_date": "2024-01-01T00:00:00+00:00"}
        )
    seeded.save_tracks(1, [wgs(LineString([(500050, 5000500), (500450, 5000500)]))])
    arguments = ["--data-dir", str(settings.data_dir)]
    response = runner.invoke(
        app, [*arguments, "coverage", "analyze", "demo", "--sport", "Hike", "--no-basemap"]
    )
    assert response.exit_code == 0, response.output
    summary = json.loads(response.output)
    assert abs(summary["percent"] - 40) < 1e-6
    assert len(summary["outputs"]) == 5
    for path in summary["outputs"]:
        assert Path(path).exists()
        assert Path(path).stat().st_mode & 0o777 == 0o600
    report = (settings.data_dir / "reports" / "coverage.html").read_text()
    assert "Uncovered" in report and "Covered" in report
    assert "40.00%" in report
    assert "tile.openstreetmap.org" not in report
    assert json.loads((settings.data_dir / "reports" / "coverage-covered.geojson").read_text())[
        "features"
    ]
    assert (
        "total_m,covered_m,uncovered_m"
        in (settings.data_dir / "reports" / "coverage.csv").read_text()
    )
    assert runner.invoke(app, [*arguments, "data", "activities"]).exit_code == 0
    assert runner.invoke(app, [*arguments, "data", "status"]).exit_code == 0


def test_empty_map_and_html_escaping(
    seeded: Store, settings: Settings, trail: gpd.GeoDataFrame
) -> None:
    trail.at[0, "tags"] = {"name": "</script><img src=x onerror=alert(1)>", "highway": "path"}
    seeded.add_snapshot("demo", trail, "synthetic")
    result = analyze(seeded, "demo", ["Hike"])
    paths = export(result, settings.data_dir / "reports")
    report = paths[0].read_text()
    assert "</script><img" not in report
    assert "Synthetic" not in report
    assert "0.00%" in report
    assert "tile.openstreetmap.org" in report
    result = analyze(seeded, "demo", ["Hike"], filters={"surface": "missing"})
    paths = export(result, settings.data_dir / "empty")
    assert "undefined" in paths[0].read_text()


def test_cli_errors_and_area_creation(tmp_path: Path) -> None:
    args = ["--data-dir", str(tmp_path / "data")]
    response = runner.invoke(app, [*args, "area", "add", "demo", "--bbox=-123,45,-122.9,45.1"])
    assert response.exit_code == 0, response.output
    assert runner.invoke(app, [*args, "area", "show", "demo"]).exit_code == 0
    assert (
        runner.invoke(app, [*args, "area", "add", "demo", "--bbox=-123,45,-122.9,45.1"]).exit_code
        == 1
    )
    response = runner.invoke(app, [*args, "strava", "sync"])
    assert response.exit_code == 1 and "policy-reviewed" in response.output
    response = runner.invoke(app, [*args, "coverage", "map", "missing", "--sport", "Hike"])
    assert response.exit_code == 1 and "Unknown area" in response.output


def test_purge_keeps_unrelated_files(settings: Settings, seeded: Store) -> None:
    reports = settings.data_dir / "reports"
    reports.mkdir()
    (reports / "coverage.html").write_text("private")
    (settings.data_dir / "keep.txt").write_text("unrelated")
    settings.token_path.write_text("{}")
    seeded.close()
    response = runner.invoke(app, ["--data-dir", str(settings.data_dir), "data", "purge", "--yes"])
    assert response.exit_code == 0, response.output
    assert not settings.db_path.exists() and not settings.token_path.exists()
    assert not (reports / "coverage.html").exists()
    assert (settings.data_dir / "keep.txt").exists()


def test_explicit_missing_config_fails(tmp_path: Path) -> None:
    response = runner.invoke(app, ["--config", str(tmp_path / "absent.toml"), "data", "status"])
    assert response.exit_code == 1 and "does not exist" in response.output
