from __future__ import annotations

import functools
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

import typer
from filelock import FileLock, Timeout
from shapely.errors import GEOSException
from shapely.geometry import mapping

from .config import Settings, TrailError, private_directory
from .coverage import analyze
from .geo import HIGHWAYS
from .osm import download_trails, resolve_area
from .report import export
from .storage import Store
from .strava import OAUTH, StravaClient, sync

app = typer.Typer(no_args_is_help=True, help="Local, approximate trail coverage by recorded sport.")
area_app = typer.Typer(no_args_is_help=True)
strava_app = typer.Typer(no_args_is_help=True)
coverage_app = typer.Typer(no_args_is_help=True)
data_app = typer.Typer(no_args_is_help=True)
app.add_typer(area_app, name="area")
app.add_typer(strava_app, name="strava")
app.add_typer(coverage_app, name="coverage")
app.add_typer(data_app, name="data")
P = ParamSpec("P")
R = TypeVar("R")


def handled(function: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except (TrailError, OSError, sqlite3.Error, Timeout, GEOSException) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc

    return wrapper


def output(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2))


@contextmanager
def dataset_lock(settings: Settings) -> Iterator[None]:
    private_directory(settings.data_dir)
    with FileLock(str(settings.data_dir / "dataset.lock"), timeout=1):
        yield


@contextmanager
def database(ctx: typer.Context) -> Iterator[Store]:
    with dataset_lock(ctx.obj):
        store = Store(ctx.obj.db_path)
        try:
            yield store
        finally:
            store.close()


@contextmanager
def strava_client(ctx: typer.Context) -> Iterator[StravaClient]:
    client = StravaClient(ctx.obj)
    try:
        yield client
    finally:
        client.close()


@app.callback()
@handled
def main(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None, typer.Option(help="Private database/token/report directory.")
    ] = None,
    config: Annotated[Path | None, typer.Option(help="Path to local TOML configuration.")] = None,
) -> None:
    ctx.obj = Settings.load(data_dir, config)


@area_app.command("add")
@handled
def area_add(
    ctx: typer.Context,
    name: str,
    bbox: Annotated[str | None, typer.Option(help="west,south,east,north (WGS84).")] = None,
    geojson: Path | None = None,
    place: str | None = None,
    allow_public_nominatim: bool = False,
) -> None:
    geometry, source = resolve_area(ctx.obj, bbox, geojson, place, allow_public_nominatim)
    with database(ctx) as store:
        store.add_area(name, geometry, source)
    output(
        {
            "area": name,
            "source": source,
            "bounds": geometry.bounds,
            "geometry": mapping(geometry),
            "next": "Inspect this boundary before running area refresh.",
        }
    )


@area_app.command("show")
@handled
def area_show(ctx: typer.Context, name: str) -> None:
    with database(ctx) as store:
        geometry = store.area(name)
    output({"name": name, "bounds": geometry.bounds, "geometry": mapping(geometry)})


@area_app.command("refresh")
@handled
def area_refresh(
    ctx: typer.Context,
    name: str,
    highway: Annotated[
        list[str] | None, typer.Option(help="Repeat to replace default highway types.")
    ] = None,
) -> None:
    with database(ctx) as store:
        snapshot = download_trails(store, name, ctx.obj, highway or list(HIGHWAYS))
    output({"snapshot": snapshot, "area": name})


@strava_app.command("auth")
@handled
def strava_auth(
    ctx: typer.Context,
    include_private: bool = False,
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8765,
) -> None:
    typer.echo(
        "Local use only. GPS data stays local; disconnect and data purge remove managed data."
    )
    with database(ctx) as store, strava_client(ctx) as client:
        client.authorize(store, include_private, port)
    typer.echo("Authorization saved privately. No activity history has been downloaded.")


@strava_app.command("sync")
@handled
def strava_sync(
    ctx: typer.Context,
    sport: Annotated[
        list[str] | None, typer.Option(help="Exact sport type; repeat for multiple.")
    ] = None,
    reconcile: Annotated[
        bool, typer.Option(help="Full rescan for edits/deletions and fresh GPS.")
    ] = False,
    policy_reviewed: Annotated[
        bool, typer.Option(help="Confirm API analytics/storage policy applicability was resolved.")
    ] = False,
    restart: Annotated[
        bool, typer.Option(help="Abandon an interrupted scan and start a fresh discovery pass.")
    ] = False,
) -> None:
    with database(ctx) as store, strava_client(ctx) as client:
        output(sync(store, client, sport, reconcile, policy_reviewed, restart))


def purge_files(settings: Settings) -> None:
    paths = [settings.token_path, settings.db_path]
    paths.extend(Path(str(settings.db_path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    reports = settings.data_dir / "reports"
    if reports.is_symlink():
        raise TrailError(
            "Reports directory is a symlink; delete its personal contents manually first."
        )
    if reports.is_dir() and not reports.is_symlink():
        paths.extend(
            path
            for path in reports.iterdir()
            if path.is_file()
            and path.suffix
            in {
                ".html",
                ".json",
                ".geojson",
                ".csv",
            }
        )
    for path in paths:
        path.unlink(missing_ok=True)


@strava_app.command("disconnect")
@handled
def strava_disconnect(ctx: typer.Context, yes: bool = False) -> None:
    if not yes:
        typer.confirm(
            "Revoke Strava access and delete local database, tokens, and managed reports?",
            abort=True,
        )
    with dataset_lock(ctx.obj), strava_client(ctx) as client:
        client.request("POST", f"{OAUTH}/deauthorize", data={"access_token": client.token()})
        purge_files(ctx.obj)
    typer.echo(
        "Access revoked; managed database, tokens, and reports deleted. "
        "Delete any separately copied exports yourself."
    )


@data_app.command("status")
@handled
def data_status(ctx: typer.Context) -> None:
    with database(ctx) as store:
        output({"data_dir": str(ctx.obj.data_dir), **store.status()})


@data_app.command("activities")
@handled
def data_activities(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1)] = 100,
    sport: str | None = None,
) -> None:
    with database(ctx) as store:
        output(
            [
                dict(row)
                for row in store.db.execute(
                    """SELECT id,sport,start_date,name,stream_status,stream_note FROM activities
               WHERE (? IS NULL OR sport=?) ORDER BY start_date DESC LIMIT ?""",
                    (sport, sport, limit),
                )
            ]
        )


@data_app.command("purge")
@handled
def data_purge(ctx: typer.Context, yes: bool = False) -> None:
    if not yes:
        typer.confirm(
            f"Delete database, tokens, and managed reports in {ctx.obj.data_dir}?", abort=True
        )
    with dataset_lock(ctx.obj):
        purge_files(ctx.obj)
    typer.echo(
        "Managed personal data and tokens deleted. This does not revoke remote access; "
        "revoke it in Strava settings. Delete separately copied exports yourself."
    )


@coverage_app.command("analyze")
@coverage_app.command("map")
@handled
def coverage_report(
    ctx: typer.Context,
    area: str,
    sport: Annotated[list[str], typer.Option(help="Exact Strava sport type; repeat to combine.")],
    start: Annotated[str | None, typer.Option(help="Inclusive UTC ISO date/time.")] = None,
    end: Annotated[str | None, typer.Option(help="Exclusive UTC ISO date/time.")] = None,
    tolerance: float = 15,
    profile: str | None = None,
    tag: Annotated[
        list[str] | None, typer.Option(help="Required OSM key=value; repeat for AND.")
    ] = None,
    crs: str | None = None,
    output_dir: Path | None = None,
    name: str = "coverage",
    basemap: bool = True,
) -> None:
    filters: dict[str, str] = {}
    for item in tag or []:
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise TrailError("--tag must be a non-empty key=value pair.")
        filters[key] = value
    with database(ctx) as store:
        result = analyze(store, area, sport, start, end, tolerance, profile, filters, crs)
    paths = export(result, output_dir or ctx.obj.data_dir / "reports", name, basemap)
    output({**result.metadata, "outputs": [str(path) for path in paths]})
