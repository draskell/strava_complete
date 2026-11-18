from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely import from_wkb
from shapely.geometry.base import BaseGeometry

from .config import TrailError, private_directory


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        private_directory(path.parent)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise TrailError(f"Unsupported database schema {version}; upgrade the application.")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS areas (
                name TEXT PRIMARY KEY, geometry BLOB NOT NULL,
                crs TEXT NOT NULL DEFAULT 'EPSG:4326',
                source TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY, area TEXT NOT NULL REFERENCES areas(name),
                fetched_at TEXT NOT NULL, source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trails (
                snapshot INTEGER NOT NULL REFERENCES snapshots(id), source_id TEXT NOT NULL,
                geometry BLOB NOT NULL, tags TEXT NOT NULL,
                PRIMARY KEY (snapshot, source_id)
            );
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY, sport TEXT NOT NULL, start_date TEXT NOT NULL,
                name TEXT NOT NULL, metadata TEXT NOT NULL, stream_status TEXT NOT NULL,
                stream_note TEXT, imported_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS activities_sport_date ON activities(sport, start_date);
            CREATE TABLE IF NOT EXISTS tracks (
                activity INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
                part INTEGER NOT NULL, geometry BLOB NOT NULL,
                crs TEXT NOT NULL DEFAULT 'EPSG:4326',
                minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
                PRIMARY KEY(activity, part)
            );
            CREATE INDEX IF NOT EXISTS tracks_bounds ON tracks(minx, maxx, miny, maxy);
            CREATE TABLE IF NOT EXISTS track_sources (
                activity INTEGER PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
                info TEXT NOT NULL, fetched_at TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.db:
            yield

    def state(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def bind_athlete(self, athlete: int) -> None:
        existing = self.state("athlete")
        if existing is not None and existing != athlete:
            raise TrailError(
                "Different athlete: use a separate --data-dir or purge this one first."
            )
        with self.transaction():
            self.set_state("athlete", athlete)

    def add_area(self, name: str, geometry: BaseGeometry, source: str) -> None:
        if not name.strip():
            raise TrailError("Area name must not be empty.")
        if self.db.execute("SELECT 1 FROM areas WHERE name=?", (name,)).fetchone():
            raise TrailError(f"Area {name!r} already exists; choose a new name.")
        with self.transaction():
            self.db.execute(
                "INSERT INTO areas(name,geometry,source,created_at) VALUES (?,?,?,?)",
                (name, geometry.wkb, source, now()),
            )

    def area(self, name: str) -> BaseGeometry:
        row = self.db.execute("SELECT geometry FROM areas WHERE name=?", (name,)).fetchone()
        if row is None:
            raise TrailError(f"Unknown area {name!r}; use area add first.")
        return from_wkb(row[0])

    def add_snapshot(self, area: str, trails: gpd.GeoDataFrame, source: str) -> int:
        with self.transaction():
            cursor = self.db.execute(
                "INSERT INTO snapshots(area,fetched_at,source) VALUES (?,?,?)",
                (area, now(), source),
            )
            snapshot = cursor.lastrowid
            assert snapshot is not None
            for row in trails.itertuples():
                self.db.execute(
                    "INSERT INTO trails VALUES (?,?,?,?)",
                    (snapshot, str(row.source_id), row.geometry.wkb, json.dumps(row.tags)),
                )
        return snapshot

    def trails(self, area: str) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
        snapshot = self.db.execute(
            "SELECT * FROM snapshots WHERE area=? ORDER BY id DESC LIMIT 1", (area,)
        ).fetchone()
        if snapshot is None:
            raise TrailError("No trail snapshot for this area; use area refresh first.")
        rows = self.db.execute(
            "SELECT source_id,geometry,tags FROM trails WHERE snapshot=?", (snapshot["id"],)
        ).fetchall()
        return gpd.GeoDataFrame(
            [
                {"source_id": r[0], "geometry": from_wkb(r[1]), "tags": json.loads(r[2])}
                for r in rows
            ],
            columns=["source_id", "geometry", "tags"],
            geometry="geometry",
            crs=4326,
        ), dict(snapshot)

    def upsert_activity(self, activity: dict[str, Any], force: bool = False) -> None:
        activity_id = int(activity["id"])
        existing = self.db.execute(
            "SELECT metadata FROM activities WHERE id=?", (activity_id,)
        ).fetchone()
        metadata = json.dumps(activity, sort_keys=True)
        changed = force or existing is None or existing[0] != metadata
        if changed:
            self.db.execute("DELETE FROM tracks WHERE activity=?", (activity_id,))
            self.db.execute("DELETE FROM track_sources WHERE activity=?", (activity_id,))
        self.db.execute(
            """INSERT INTO activities VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET sport=excluded.sport, start_date=excluded.start_date,
               name=excluded.name, metadata=excluded.metadata,
               stream_status=CASE WHEN ? THEN 'pending' ELSE activities.stream_status END,
               stream_note=CASE WHEN ? THEN NULL ELSE activities.stream_note END,
               imported_at=excluded.imported_at""",
            (
                activity_id,
                activity["sport_type"],
                activity["start_date"],
                activity.get("name", ""),
                metadata,
                "pending",
                None,
                now(),
                changed,
                changed,
            ),
        )

    def save_tracks(
        self,
        activity: int,
        parts: list[BaseGeometry],
        note: str | None = None,
        source_info: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM tracks WHERE activity=?", (activity,))
            for index, geometry in enumerate(parts):
                self.db.execute(
                    """INSERT INTO tracks(activity,part,geometry,minx,miny,maxx,maxy)
                       VALUES (?,?,?,?,?,?,?)""",
                    (activity, index, geometry.wkb, *geometry.bounds),
                )
            self.db.execute(
                "UPDATE activities SET stream_status=?, stream_note=?, imported_at=? WHERE id=?",
                ("ready" if parts else "skipped", note, now(), activity),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO track_sources VALUES (?,?,?)",
                (activity, json.dumps(source_info or {"source": "local"}), now()),
            )

    def candidates(
        self,
        bounds: tuple[float, float, float, float],
        sports: list[str],
        start: str | None,
        end: str | None,
    ) -> gpd.GeoDataFrame:
        marks = ",".join("?" for _ in sports)
        minx, miny, maxx, maxy = bounds
        rows = self.db.execute(
            f"""SELECT t.geometry,t.activity FROM tracks t JOIN activities a ON a.id=t.activity
                WHERE a.sport IN ({marks}) AND (? IS NULL OR a.start_date>=?)
                AND (? IS NULL OR a.start_date<?)
                AND t.minx<=? AND t.maxx>=? AND t.miny<=? AND t.maxy>=?""",
            (*sports, start, start, end, end, maxx, minx, maxy, miny),
        ).fetchall()
        return gpd.GeoDataFrame(
            [{"geometry": from_wkb(r[0]), "activity": r[1]} for r in rows],
            columns=["geometry", "activity"],
            crs=4326,
        )

    def status(self) -> dict[str, Any]:
        return {
            "athlete": self.state("athlete"),
            "areas": [
                dict(row)
                for row in self.db.execute("SELECT name,source,created_at FROM areas ORDER BY name")
            ],
            "activities": dict(
                self.db.execute(
                    "SELECT stream_status,COUNT(*) FROM activities GROUP BY stream_status"
                ).fetchall()
            ),
            "sync": self.state("sync"),
        }
