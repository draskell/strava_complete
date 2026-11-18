from __future__ import annotations

import json
import math
import secrets
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Settings, TrailError, private_write, read_json
from .geo import SPORTS, track_parts, utc_date
from .storage import Store, now

API = "https://www.strava.com/api/v3"
OAUTH = "https://www.strava.com/oauth"
POLICY = "https://www.strava.com/legal/api_policy"


def callback_result(query: str, expected_state: str, required_scope: str) -> str:
    params = parse_qs(query)
    if params.get("state") != [expected_state]:
        raise TrailError("OAuth state mismatch; authorization was not accepted.")
    if "error" in params:
        raise TrailError("Strava authorization was denied.")
    scopes = params.get("scope", [""])[0].split(",")
    if required_scope not in scopes:
        raise TrailError(f"Authorization did not grant {required_scope}; authorize again.")
    code = params.get("code", [""])[0]
    if not code:
        raise TrailError("OAuth callback did not include an authorization code.")
    return code


class StravaClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.settings = settings
        self.http = http or httpx.Client(timeout=60, headers={"User-Agent": settings.user_agent})
        self.next_request = 0.0

    def close(self) -> None:
        self.http.close()

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        if time.time() < self.next_request:
            reset = datetime.fromtimestamp(self.next_request, UTC).isoformat()
            raise TrailError(f"Strava rate limit reached. Resume after {reset}.")
        for attempt in range(3):
            try:
                response = self.http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise TrailError(
                    "Cannot reach Strava; sync progress is saved. Retry later."
                ) from exc
            self._limits(response)
            if response.status_code == 429:
                raise TrailError(
                    "Strava rate limit (429); progress saved. Retry after quota reset."
                )
            if response.status_code in {502, 503, 504} and attempt < 2:
                time.sleep(2**attempt)
                continue
            if response.status_code in {401, 403}:
                raise TrailError(
                    "Strava authorization failed; check permissions and authorize again."
                )
            if response.status_code == 404:
                raise TrailError(
                    "Strava resource unavailable (404); check access or run "
                    "strava sync --reconcile --restart --policy-reviewed."
                )
            if not response.is_success:
                raise TrailError(f"Strava returned HTTP {response.status_code}; progress saved.")
            try:
                return response.json()
            except ValueError as exc:
                raise TrailError("Strava returned invalid JSON.") from exc
        raise AssertionError("Unreachable retry state")

    def _limits(self, response: httpx.Response) -> None:
        current = time.time()
        for prefix in ("x-ratelimit", "x-readratelimit"):
            try:
                limits = [int(x) for x in response.headers[f"{prefix}-limit"].split(",")]
                usage = [int(x) for x in response.headers[f"{prefix}-usage"].split(",")]
            except (KeyError, ValueError):
                continue
            if len(limits) != 2 or len(usage) != 2:
                continue
            if usage[0] >= limits[0]:
                self.next_request = max(self.next_request, (int(current) // 900 + 1) * 900 + 1)
            if usage[1] >= limits[1]:
                tomorrow = datetime.now(UTC).date() + timedelta(days=1)
                self.next_request = max(
                    self.next_request,
                    datetime.combine(tomorrow, datetime.min.time(), UTC).timestamp(),
                )

    def exchange(self, fields: dict[str, str]) -> dict[str, Any]:
        if not self.settings.client_id or not self.settings.client_secret:
            raise TrailError("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET before authorizing.")
        data = self.request(
            "POST",
            f"{OAUTH}/token",
            data={
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                **fields,
            },
        )
        if (
            not isinstance(data, dict)
            or not all(
                isinstance(data.get(key), str) and data[key]
                for key in ("access_token", "refresh_token")
            )
            or not isinstance(data.get("expires_at"), (int, float))
            or not math.isfinite(data["expires_at"])
        ):
            raise TrailError("Strava returned an invalid token response.")
        return data

    def token(self) -> str:
        if not self.settings.token_path.exists():
            raise TrailError("No authorization found; run strava auth first.")
        token = read_json(self.settings.token_path)
        if (
            not isinstance(token.get("expires_at"), (int, float))
            or not math.isfinite(token["expires_at"])
            or not all(
                isinstance(token.get(key), str) and token[key]
                for key in ("refresh_token", "access_token")
            )
        ):
            raise TrailError("Invalid local token file; authorize again.")
        if float(token["expires_at"]) <= time.time() + 60:
            fresh = self.exchange(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                }
            )
            token.update(fresh)
            private_write(self.settings.token_path, json.dumps(token))
        return str(token["access_token"])

    def get(self, endpoint: str, **params: Any) -> Any:
        return self.request(
            "GET",
            f"{API}/{endpoint}",
            params=params,
            headers={"Authorization": f"Bearer {self.token()}"},
        )

    def authorize(self, store: Store, include_private: bool, port: int = 8765) -> None:
        if not self.settings.client_id or not self.settings.client_secret:
            raise TrailError("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET first.")
        state = secrets.token_urlsafe(32)
        scope = "activity:read_all" if include_private else "activity:read"
        result: dict[str, str] = {}

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if urlparse(self.path).path != "/callback":
                    self.send_error(404)
                    return
                result["query"] = urlparse(self.path).query
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Callback received. Check the terminal for authorization status.")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        try:
            server = HTTPServer(("127.0.0.1", port), Callback)
        except OSError as exc:
            raise TrailError("Cannot bind loopback callback; choose another --port.") from exc
        with server:
            server.timeout = 1
            redirect = f"http://127.0.0.1:{server.server_port}/callback"
            url = f"{OAUTH}/authorize?" + urlencode(
                {
                    "client_id": self.settings.client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "approval_prompt": "force",
                    "scope": scope,
                    "state": state,
                }
            )
            print(f"Authorize in your browser (expires in 3 minutes):\n{url}", flush=True)
            webbrowser.open(url)
            deadline = time.monotonic() + 180
            while not result and time.monotonic() < deadline:
                server.handle_request()
        if not result:
            raise TrailError("OAuth timed out; run strava auth again.")
        code = callback_result(result["query"], state, scope)
        token = self.exchange({"grant_type": "authorization_code", "code": code})
        athlete = token.get("athlete")
        if not isinstance(athlete, dict) or not isinstance(athlete.get("id"), int):
            raise TrailError("Token response omitted athlete identity.")
        store.bind_athlete(athlete["id"])
        token["scope"] = scope
        private_write(self.settings.token_path, json.dumps(token))


def activity_record(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), int)
        or not isinstance(value.get("sport_type"), str)
        or not isinstance(value.get("start_date"), str)
    ):
        raise TrailError("Malformed activity metadata from Strava.")
    # Store only fields needed by this application, not social/profile metrics.
    result = {
        key: value[key]
        for key in (
            "id",
            "sport_type",
            "start_date",
            "name",
            "manual",
            "trainer",
            "map",
            "device_name",
        )
        if key in value
    }
    result["start_date"] = utc_date(value["start_date"])
    return result


def stream_info(streams: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"source": "Strava activity streams"}
    for kind in ("latlng", "time"):
        stream = streams.get(kind)
        if not isinstance(stream, dict) or not isinstance(stream.get("data"), list):
            raise TrailError(f"Malformed {kind} stream.")
        if stream.get("resolution") != "high":
            raise TrailError(
                f"{kind} stream is not labeled high resolution; refusing degraded coverage input."
            )
        result[kind] = {
            "resolution": stream["resolution"],
            "original_size": stream.get("original_size"),
            "series_type": stream.get("series_type"),
            "sample_count": len(stream["data"]),
        }
    return result


def sync(
    store: Store,
    client: StravaClient,
    sports: list[str] | None = None,
    reconcile: bool = False,
    policy_reviewed: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    if not policy_reviewed:
        raise TrailError(
            f"Live persistent sync is gated pending review of {POLICY} "
            "(analytics/storage restrictions; personal-use exemption is not established). "
            "Use --policy-reviewed only after resolving applicability for your use."
        )
    athlete = client.get("athlete")
    if not isinstance(athlete, dict) or not isinstance(athlete.get("id"), int):
        raise TrailError("Strava did not return an athlete identity.")
    store.bind_athlete(athlete["id"])
    granted_scope = read_json(client.settings.token_path).get("scope", "unknown")
    selected = sports or list(SPORTS)
    state = store.state("sync", {})
    if state and state.get("scope") != granted_scope and not (reconcile and restart):
        raise TrailError(
            "Authorization scope changed; run sync with --reconcile --restart "
            "to refresh historical visibility before reporting complete history."
        )
    if state.get("phase") in {"discover", "streams"} and not restart:
        if reconcile != state["reconcile"]:
            raise TrailError(
                "Resume with the same --reconcile setting or use --restart to begin a new scan."
            )
        if sorted(selected) != sorted(state["sports"]):
            raise TrailError("Resume with the same --sport selections or use --restart.")
    else:
        state = {
            "phase": "discover",
            "page": 1,
            "before": int(time.time()),
            "after": 0 if reconcile else max(0, int(state.get("through", 0)) - 7 * 86400),
            "through": state.get("through", 0),
            "reconcile": reconcile,
            "seen": [],
            "started_at": now(),
            "sports": selected,
            "scope": granted_scope,
        }
    with store.transaction():
        store.set_state("sync", state)
    while state["phase"] == "discover":
        values = client.get(
            "athlete/activities",
            page=state["page"],
            per_page=200,
            before=state["before"],
            after=state["after"],
        )
        if not isinstance(values, list):
            raise TrailError("Strava activity list was not an array.")
        with store.transaction():
            for value in values:
                record = activity_record(value)
                store.upsert_activity(record, force=reconcile and record["sport_type"] in selected)
                if reconcile:
                    state["seen"].append(record["id"])
            state["page"] += 1
            if not values:
                if reconcile:
                    store.db.execute("CREATE TEMP TABLE IF NOT EXISTS seen(id INTEGER PRIMARY KEY)")
                    store.db.execute("DELETE FROM seen")
                    store.db.executemany(
                        "INSERT OR IGNORE INTO seen VALUES (?)", ((x,) for x in state["seen"])
                    )
                    store.db.execute("DELETE FROM activities WHERE id NOT IN (SELECT id FROM seen)")
                state["phase"] = "streams"
                state["through"] = state["before"]
                state.pop("seen")
            store.set_state("sync", state)
    marks = ",".join("?" for _ in selected)
    pending = store.db.execute(
        f"SELECT id,metadata FROM activities WHERE stream_status='pending' AND sport IN ({marks})",
        selected,
    ).fetchall()
    for row in pending:
        metadata = json.loads(row["metadata"])
        if metadata.get("manual") or metadata.get("trainer"):
            store.save_tracks(row["id"], [], "Manual or indoor activity; no trail coverage.")
            continue
        streams = client.get(
            f"activities/{row['id']}/streams", keys="latlng,time", key_by_type="true"
        )
        if not isinstance(streams, dict):
            raise TrailError(f"Invalid GPS stream for activity {row['id']}.")
        if "latlng" not in streams:
            store.save_tracks(row["id"], [], "No GPS stream available under granted permissions.")
        elif "time" not in streams:
            raise TrailError(f"Missing time stream for activity {row['id']}; cannot validate gaps.")
        else:
            provenance = stream_info(streams)
            try:
                parts, note = track_parts(streams["latlng"]["data"], streams["time"]["data"])
            except (KeyError, TypeError) as exc:
                raise TrailError(f"Malformed streams for activity {row['id']}.") from exc
            store.save_tracks(
                row["id"],
                parts,
                note or (None if parts else "Insufficient GPS points."),
                provenance,
            )
    state["phase"] = "complete"
    state["sports"] = selected
    state["completed_at"] = now()
    with store.transaction():
        store.set_state("sync", state)
    return store.status()
