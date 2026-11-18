from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_cache_path, user_config_path, user_data_path


class TrailError(Exception):
    """An actionable error suitable for display without a traceback."""


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def private_write(path: Path, content: str) -> None:
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise TrailError(f"Cannot read JSON file {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise TrailError(f"Expected a JSON object in {path}.")
    return result


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cache_dir: Path
    client_id: str = ""
    client_secret: str = ""
    user_agent: str = "trail-coverage/0.1 (local personal trail analysis)"
    nominatim_url: str = "https://nominatim.openstreetmap.org/"
    overpass_url: str = "https://overpass-api.de/api"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "coverage.sqlite3"

    @classmethod
    def load(cls, data_dir: Path | None = None, config: Path | None = None) -> Settings:
        config_path = config or user_config_path("trail-coverage") / "config.toml"
        values: dict[str, Any] = {}
        if config is not None and not config_path.is_file():
            raise TrailError(f"Configuration file does not exist: {config_path}")
        if config_path.exists():
            try:
                values = tomllib.loads(config_path.read_text())
            except (OSError, ValueError) as exc:
                raise TrailError(f"Cannot read configuration {config_path}: {exc}") from exc
        allowed = {
            "data_dir",
            "cache_dir",
            "client_id",
            "client_secret",
            "user_agent",
            "nominatim_url",
            "overpass_url",
        }
        if set(values) - allowed or any(not isinstance(value, str) for value in values.values()):
            raise TrailError("Configuration must contain only documented string-valued settings.")
        selected_data = data_dir or Path(
            os.getenv("TRAIL_COVERAGE_DATA_DIR")
            or values.get("data_dir")
            or user_data_path("trail-coverage")
        )
        return cls(
            data_dir=selected_data.expanduser().resolve(),
            cache_dir=Path(
                values.get("cache_dir")
                or (selected_data / "cache" if data_dir else user_cache_path("trail-coverage"))
            )
            .expanduser()
            .resolve(),
            client_id=os.getenv("STRAVA_CLIENT_ID") or values.get("client_id", ""),
            client_secret=os.getenv("STRAVA_CLIENT_SECRET") or values.get("client_secret", ""),
            user_agent=values.get("user_agent", cls.user_agent),
            nominatim_url=values.get("nominatim_url", cls.nominatim_url),
            overpass_url=values.get("overpass_url", cls.overpass_url),
        )
