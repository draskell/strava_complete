from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from strava_complete.config import Settings, TrailError, private_write
from strava_complete.storage import Store
from strava_complete.strava import API, OAUTH, StravaClient, callback_result, stream_info, sync

STREAM = {
    "latlng": {"data": [[45, -123], [45.001, -123]], "resolution": "high", "original_size": 2},
    "time": {"data": [0, 10], "resolution": "high", "original_size": 2},
}


def tokens(settings: Settings, expired: bool = False) -> None:
    private_write(
        settings.token_path,
        json.dumps(
            {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_at": time.time() + (-1 if expired else 3600),
            }
        ),
    )


def activity(number: int, sport: str = "Hike") -> dict[str, Any]:
    return {
        "id": number,
        "sport_type": sport,
        "start_date": "2024-01-01T00:00:00Z",
        "name": f"Synthetic {number}",
    }


def setup_sync(router: respx.MockRouter, settings: Settings, values: list[dict[str, Any]]) -> None:
    tokens(settings)
    router.get(f"{API}/athlete").respond(200, json={"id": 123})
    router.get(f"{API}/athlete/activities").mock(
        side_effect=[
            httpx.Response(200, json=values),
            httpx.Response(200, json=[]),
        ]
    )


def test_callback_validation() -> None:
    assert (
        callback_result("code=abc&state=state&scope=activity:read", "state", "activity:read")
        == "abc"
    )
    for query in [
        "code=abc&state=wrong&scope=activity:read",
        "error=access_denied&state=state",
        "code=abc&state=state&scope=read",
        "state=state&scope=activity:read",
    ]:
        with pytest.raises(TrailError):
            callback_result(query, "state", "activity:read")


def test_refresh_rotates_and_persists(settings: Settings, no_live_http: respx.MockRouter) -> None:
    tokens(settings, expired=True)
    route = no_live_http.post(f"{OAUTH}/token").respond(
        200,
        json={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": time.time() + 3600,
        },
    )
    with httpx.Client() as http:
        client = StravaClient(settings, http)
        assert client.token() == "new-access"
    assert json.loads(settings.token_path.read_text())["refresh_token"] == "new-refresh"
    fields = parse_qs(route.calls[0].request.content.decode())
    assert fields["grant_type"] == ["refresh_token"]
    assert settings.token_path.stat().st_mode & 0o777 == 0o600


def test_policy_gate_no_network(store: Store, settings: Settings) -> None:
    with pytest.raises(TrailError, match="policy-reviewed"):
        sync(store, StravaClient(settings))


def test_paginated_sync_idempotence_and_missing_gps(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1), activity(2)])
    no_live_http.get(f"{API}/activities/1/streams").respond(200, json=STREAM)
    no_live_http.get(f"{API}/activities/2/streams").respond(200, json={})
    client = StravaClient(settings)
    result = sync(store, client, policy_reviewed=True)
    assert result["activities"] == {"ready": 1, "skipped": 1}
    assert result["sync"]["phase"] == "complete"
    no_live_http.get(f"{API}/athlete/activities").mock(
        side_effect=[
            httpx.Response(200, json=[activity(1), activity(2)]),
            httpx.Response(200, json=[]),
        ]
    )
    sync(store, client, policy_reviewed=True)
    assert store.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert no_live_http.get(f"{API}/activities/1/streams").calls.call_count == 1
    client.close()


def test_interrupted_stream_sync_resumes(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1), activity(2)])
    no_live_http.get(f"{API}/activities/1/streams").respond(200, json=STREAM)
    second = no_live_http.get(f"{API}/activities/2/streams").respond(429)
    client = StravaClient(settings)
    with pytest.raises(TrailError, match="429"):
        sync(store, client, policy_reviewed=True)
    assert store.state("sync")["phase"] == "streams"
    assert store.status()["activities"] == {"pending": 1, "ready": 1}
    second.respond(200, json=STREAM)
    sync(store, client, policy_reviewed=True)
    assert store.status()["activities"] == {"ready": 2}
    assert no_live_http.get(f"{API}/activities/1/streams").calls.call_count == 1
    client.close()


def test_interrupted_discovery_resumes(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [])
    route = no_live_http.get(f"{API}/athlete/activities").mock(
        side_effect=[
            httpx.Response(200, json=[activity(1)]),
            httpx.Response(429),
        ]
    )
    client = StravaClient(settings)
    with pytest.raises(TrailError, match="429"):
        sync(store, client, policy_reviewed=True)
    assert store.state("sync")["page"] == 2
    route.respond(200, json=[])
    no_live_http.get(f"{API}/activities/1/streams").respond(200, json=STREAM)
    sync(store, client, policy_reviewed=True)
    assert route.calls[-1].request.url.params["page"] == "2"
    client.close()


def test_reconciliation_deletes_missing_and_refreshes_existing(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1), activity(2)])
    no_live_http.get(f"{API}/activities/1/streams").respond(200, json=STREAM)
    no_live_http.get(f"{API}/activities/2/streams").respond(200, json=STREAM)
    client = StravaClient(settings)
    sync(store, client, policy_reviewed=True)
    no_live_http.get(f"{API}/athlete/activities").mock(
        side_effect=[
            httpx.Response(200, json=[activity(1)]),
            httpx.Response(200, json=[]),
        ]
    )
    sync(store, client, reconcile=True, policy_reviewed=True)
    assert store.db.execute("SELECT id FROM activities").fetchall()[0][0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert no_live_http.get(f"{API}/activities/1/streams").calls.call_count == 2
    client.close()


def test_identity_mismatch(store: Store) -> None:
    store.bind_athlete(1)
    with pytest.raises(TrailError, match="Different athlete"):
        store.bind_athlete(2)
    assert store.state("athlete") == 1


def test_limits_stop_before_next_request(
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    route = no_live_http.get(f"{API}/example").respond(
        200,
        json={},
        headers={
            "X-ReadRateLimit-Limit": "100,1000",
            "X-ReadRateLimit-Usage": "100,200",
        },
    )
    client = StravaClient(settings)
    client.request("GET", f"{API}/example")
    with pytest.raises(TrailError, match="Resume after"):
        client.request("GET", f"{API}/example")
    assert route.call_count == 1
    client.close()


def test_transient_retry_and_auth_failure(
    settings: Settings,
    no_live_http: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("strava_complete.strava.time.sleep", lambda _: None)
    route = no_live_http.get(f"{API}/example").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = StravaClient(settings)
    assert client.request("GET", f"{API}/example") == {"ok": True}
    assert route.call_count == 2
    no_live_http.get(f"{API}/example").respond(403)
    with pytest.raises(TrailError, match="authorization"):
        client.request("GET", f"{API}/example")
    client.close()


def test_authorization_header(settings: Settings, no_live_http: respx.MockRouter) -> None:
    tokens(settings)
    route = no_live_http.get(f"{API}/athlete").respond(200, json={"id": 123})
    client = StravaClient(settings)
    client.get("athlete")
    assert route.calls[0].request.headers["Authorization"] == "Bearer synthetic-access"
    client.close()


def test_stream_resolution_provenance(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1)])
    no_live_http.get(f"{API}/activities/1/streams").respond(200, json=STREAM)
    client = StravaClient(settings)
    sync(store, client, policy_reviewed=True)
    info = json.loads(store.db.execute("SELECT info FROM track_sources").fetchone()[0])
    assert info["latlng"]["resolution"] == "high"
    assert info["latlng"]["sample_count"] == 2
    with pytest.raises(TrailError, match="high resolution"):
        stream_info({**STREAM, "latlng": {**STREAM["latlng"], "resolution": "low"}})
    client.close()


def test_restart_recovers_deleted_activity_mid_scan(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1)])
    no_live_http.get(f"{API}/activities/1/streams").respond(404)
    client = StravaClient(settings)
    with pytest.raises(TrailError, match="restart"):
        sync(store, client, policy_reviewed=True)
    no_live_http.get(f"{API}/athlete/activities").respond(200, json=[])
    sync(store, client, reconcile=True, restart=True, policy_reviewed=True)
    assert store.status()["activities"] == {}
    client.close()


def test_failed_reconciliation_does_not_remove_existing_history(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    with store.transaction():
        store.upsert_activity(activity(99))
    setup_sync(no_live_http, settings, [])
    no_live_http.get(f"{API}/athlete/activities").mock(
        side_effect=[
            httpx.Response(200, json=[activity(1)]),
            httpx.Response(429),
        ]
    )
    client = StravaClient(settings)
    with pytest.raises(TrailError):
        sync(store, client, reconcile=True, policy_reviewed=True)
    ids = {row[0] for row in store.db.execute("SELECT id FROM activities")}
    assert ids == {1, 99}
    client.close()


def test_malformed_stream_stays_pending(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1)])
    no_live_http.get(f"{API}/activities/1/streams").respond(
        200, json={**STREAM, "time": {**STREAM["time"], "data": [0]}}
    )
    client = StravaClient(settings)
    with pytest.raises(TrailError, match="different lengths"):
        sync(store, client, policy_reviewed=True)
    assert store.status()["activities"] == {"pending": 1}
    assert store.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
    client.close()


def test_changed_metadata_reimports_gps(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1)])
    route = no_live_http.get(f"{API}/activities/1/streams").respond(200, json=STREAM)
    client = StravaClient(settings)
    sync(store, client, policy_reviewed=True)
    no_live_http.get(f"{API}/athlete/activities").mock(
        side_effect=[
            httpx.Response(200, json=[{**activity(1), "sport_type": "TrailRun"}]),
            httpx.Response(200, json=[]),
        ]
    )
    sync(store, client, policy_reviewed=True)
    assert route.call_count == 2
    assert store.db.execute("SELECT sport FROM activities").fetchone()[0] == "TrailRun"
    client.close()


def test_scope_change_requires_full_visibility_refresh(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1)])
    no_live_http.get(f"{API}/activities/1/streams").respond(200, json=STREAM)
    client = StravaClient(settings)
    sync(store, client, policy_reviewed=True)
    value = json.loads(settings.token_path.read_text())
    value["scope"] = "activity:read_all"
    private_write(settings.token_path, json.dumps(value))
    with pytest.raises(TrailError, match="scope changed"):
        sync(store, client, policy_reviewed=True)
    no_live_http.get(f"{API}/athlete/activities").mock(
        side_effect=[
            httpx.Response(200, json=[activity(1)]),
            httpx.Response(200, json=[]),
        ]
    )
    sync(store, client, reconcile=True, restart=True, policy_reviewed=True)
    assert store.state("sync")["scope"] == "activity:read_all"
    client.close()


def test_reconcile_retains_other_sports_gps(
    store: Store,
    settings: Settings,
    no_live_http: respx.MockRouter,
) -> None:
    setup_sync(no_live_http, settings, [activity(1, "Hike"), activity(2, "Ride")])
    no_live_http.get(f"{API}/activities/1/streams").respond(200, json=STREAM)
    second = no_live_http.get(f"{API}/activities/2/streams").respond(200, json=STREAM)
    client = StravaClient(settings)
    sync(store, client, policy_reviewed=True)
    no_live_http.get(f"{API}/athlete/activities").mock(
        side_effect=[
            httpx.Response(200, json=[activity(1, "Hike"), activity(2, "Ride")]),
            httpx.Response(200, json=[]),
        ]
    )
    sync(store, client, sports=["Hike"], reconcile=True, policy_reviewed=True)
    assert store.status()["activities"] == {"ready": 2}
    assert second.call_count == 1
    client.close()
