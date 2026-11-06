"""D1-01 独立预期：实际对象、事件、权限、修订和事务结果。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
import hashlib
from threading import Barrier
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

import services.business.app as business_app


pytestmark = pytest.mark.integration


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"error": code}


def counts(sql):
    return sql(
        "SELECT (SELECT count(*) FROM trips) AS trips, "
        "(SELECT count(*) FROM business_events) AS events, "
        "(SELECT count(*) FROM idempotency) AS operations"
    )[0]


def test_create_read_and_independent_postgres_state(client, alice, create, sql, draft):
    trip = create()
    UUID(trip["id"])
    assert trip == {
        "id": trip["id"], "owner_id": "personal_alice", "organization_id": None,
        **draft, "revision": 1, "status": "draft", "price": None,
        "price_status": "requires_payment_profile",
    }
    read = client.get(f"/trips/{trip['id']}", headers=alice)
    assert read.status_code == 200
    assert read.json() == trip
    persisted = sql(
        "SELECT workspace_id,owner_id,organization_id,flight_id,travel_date,"
        "passengers,revision,status,price_minor FROM trips WHERE id=%s", (trip["id"],)
    )
    assert persisted == [{
        "workspace_id": "test", "owner_id": "personal_alice", "organization_id": None,
        "flight_id": "DEMO-PEK-SHA", "travel_date": date(2026, 10, 1),
        "passengers": 1, "revision": 1, "status": "draft", "price_minor": None,
    }]
    event = sql(
        "SELECT workspace_id,actor_id,action,before_revision,after_revision,"
        "result,business_time FROM business_events WHERE trip_id=%s",
        (trip["id"],),
    )
    assert len(event) == 1
    assert event[0] == {
        "workspace_id": "test", "actor_id": "personal_alice", "action": "trip.created",
        "before_revision": 0, "after_revision": 1, "result": "succeeded",
        "business_time": datetime(2026, 9, 27, tzinfo=UTC),
    }
    assert counts(sql) == {"trips": 1, "events": 1, "operations": 1}


def test_same_role_other_owner_and_unknown_objects_are_identical(client, alice, bob, create, sql):
    trip = create()
    before = counts(sql)
    forbidden = client.get(f"/trips/{trip['id']}", headers=bob)
    missing = client.get(f"/trips/{uuid4()}", headers=bob)
    malformed = client.get("/trips/not-a-uuid", headers=bob)
    for response in (forbidden, missing, malformed):
        assert_error(response, 404, "not_visible")
    forged = client.get(
        f"/trips/{trip['id']}?role=admin&user_id=personal_alice",
        headers={**bob, "X-Role": "admin", "X-User-ID": "personal_alice"},
    )
    assert_error(forged, 404, "not_visible")
    write = client.patch(
        f"/trips/{trip['id']}", headers={**bob, "Idempotency-Key": "unauthorized"},
        json={"expected_revision": 1, "passengers": 2},
    )
    assert_error(write, 404, "not_visible")
    assert counts(sql) == before
    assert client.get(f"/trips/{trip['id']}", headers=alice).json() == trip


def test_update_revision_stale_write_and_replay(client, alice, create, sql):
    trip = create()
    path = f"/trips/{trip['id']}"
    parameters = {"expected_revision": 1, "travel_date": "2026-10-02"}
    first = client.patch(path, headers={**alice, "Idempotency-Key": "change-1"}, json=parameters)
    assert first.status_code == 200, first.text
    assert first.json() == {**trip, "travel_date": "2026-10-02", "revision": 2}
    second = client.patch(
        path, headers={**alice, "Idempotency-Key": "change-2"},
        json={"expected_revision": 2, "passengers": 2},
    )
    assert second.status_code == 200
    assert second.json()["revision"] == 3
    before = counts(sql)
    stale = client.patch(path, headers={**alice, "Idempotency-Key": "stale"}, json=parameters)
    assert_error(stale, 409, "state_changed")
    replay = client.patch(path, headers={**alice, "Idempotency-Key": "change-1"}, json=parameters)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    changed_parameters = client.patch(
        path, headers={**alice, "Idempotency-Key": "change-1"},
        json={"expected_revision": 1, "travel_date": "2026-10-03"},
    )
    assert_error(changed_parameters, 409, "idempotency_conflict")
    assert counts(sql) == before == {"trips": 1, "events": 3, "operations": 3}
    assert sql("SELECT revision,travel_date,passengers FROM trips WHERE id=%s", (trip["id"],)) == [
        {"revision": 3, "travel_date": date(2026, 10, 2), "passengers": 2}
    ]
    assert sql(
        "SELECT action,before_revision,after_revision FROM business_events "
        "WHERE trip_id=%s ORDER BY seq", (trip["id"],)
    ) == [
        {"action": "trip.created", "before_revision": 0, "after_revision": 1},
        {"action": "trip.updated", "before_revision": 1, "after_revision": 2},
        {"action": "trip.updated", "before_revision": 2, "after_revision": 3},
    ]


def test_unchanged_accepted_patch_is_new_revision(client, alice, create, sql):
    trip = create()
    response = client.patch(
        f"/trips/{trip['id']}", headers={**alice, "Idempotency-Key": "same-value"},
        json={"expected_revision": 1, "passengers": 1},
    )
    assert response.status_code == 200
    assert response.json() == {**trip, "revision": 2}
    assert counts(sql) == {"trips": 1, "events": 2, "operations": 2}


def test_create_idempotency_and_actor_scope(client, alice, bob, create, draft, sql):
    trip = create()
    replay = client.post(
        "/trips", headers={**alice, "Idempotency-Key": "create-1"},
        json={"passengers": 1, "travel_date": "2026-10-01", "flight_id": "DEMO-PEK-SHA"},
    )
    assert replay.status_code == 201
    assert replay.json() == trip
    conflicting = client.post(
        "/trips", headers={**alice, "Idempotency-Key": "create-1"}, json={**draft, "passengers": 2},
    )
    assert_error(conflicting, 409, "idempotency_conflict")
    assert counts(sql) == {"trips": 1, "events": 1, "operations": 1}
    bob_trip = create(headers=bob)
    assert bob_trip["id"] != trip["id"]
    assert bob_trip["owner_id"] == "personal_bob"
    assert counts(sql) == {"trips": 2, "events": 2, "operations": 2}


def test_authentication_is_rechecked_before_idempotent_replay(client, alice, create, draft, sql):
    create()
    sql("UPDATE users SET active=false WHERE workspace_id='test' AND id='personal_alice' RETURNING id")
    response = client.post("/trips", headers={**alice, "Idempotency-Key": "create-1"}, json=draft)
    assert_error(response, 401, "authentication_required")
    assert counts(sql) == {"trips": 1, "events": 1, "operations": 1}


def test_object_access_is_rechecked_before_idempotent_replay(client, alice, create, draft, sql):
    trip = create()
    # 受保护的测试控制面模拟归属变化，业务 API 不允许用户改 owner。
    sql("UPDATE trips SET owner_id='personal_bob' WHERE id=%s RETURNING id", (trip["id"],))
    response = client.post("/trips", headers={**alice, "Idempotency-Key": "create-1"}, json=draft)
    assert_error(response, 404, "not_visible")
    assert counts(sql) == {"trips": 1, "events": 1, "operations": 1}


@pytest.mark.parametrize("operation", ["create", "update"])
def test_event_database_failure_rolls_back_every_write(operation, client, alice, create, draft, sql, monkeypatch):
    existing = create() if operation == "update" else None
    before = counts(sql)
    original = business_app.record_event

    def fail_event(connection, workspace, actor, trip_id, action, before, after):
        connection.execute("SELECT 1 / 0")

    monkeypatch.setattr(business_app, "record_event", fail_event)
    headers = {**alice, "Idempotency-Key": "retry-after-rollback"}
    path = f"/trips/{existing['id']}" if existing else "/trips"
    method = client.patch if existing else client.post
    parameters = {"expected_revision": 1, "passengers": 3} if existing else draft
    failed = method(path, headers=headers, json=parameters)
    assert_error(failed, 503, "storage_unavailable")
    assert counts(sql) == before
    if existing:
        assert client.get(path, headers=alice).json() == existing
    monkeypatch.setattr(business_app, "record_event", original)
    retried = method(path, headers=headers, json=parameters)
    assert retried.status_code == (200 if existing else 201), retried.text
    assert retried.json()["revision"] == (2 if existing else 1)
    assert counts(sql) == {
        "trips": before["trips"] + (0 if existing else 1),
        "events": before["events"] + 1, "operations": before["operations"] + 1,
    }


def test_concurrent_same_key_creates_one_object(app, alice, draft, sql, monkeypatch):
    start = Barrier(2)
    backends = set()
    original = business_app.replay

    def synchronized_replay(connection, workspace, actor, action, key, parameters):
        # authenticate 已执行 SQL；两条独立 PostgreSQL 事务均开始后才竞争锁。
        backends.add(connection.info.backend_pid)
        start.wait(timeout=10)
        return original(connection, workspace, actor, action, key, parameters)

    monkeypatch.setattr(business_app, "replay", synchronized_replay)

    def request():
        with TestClient(app) as separate_client:
            return separate_client.post(
                "/trips", headers={**alice, "Idempotency-Key": "concurrent-create"}, json=draft,
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(request) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert [response.status_code for response in results] == [201, 201]
    assert results[0].json() == results[1].json()
    assert len(backends) == 2
    assert counts(sql) == {"trips": 1, "events": 1, "operations": 1}


def test_concurrent_revision_updates_have_one_winner(app, alice, create, sql, monkeypatch):
    trip = create()
    start = Barrier(2)
    backends = set()
    original = business_app.owned_trip

    def synchronized_owned_trip(connection, workspace, actor, trip_id, *, lock=False):
        if lock:
            backends.add(connection.info.backend_pid)
            start.wait(timeout=10)
        return original(connection, workspace, actor, trip_id, lock=lock)

    monkeypatch.setattr(business_app, "owned_trip", synchronized_owned_trip)

    def request(index):
        with TestClient(app) as separate_client:
            return separate_client.patch(
                f"/trips/{trip['id']}",
                headers={**alice, "Idempotency-Key": f"concurrent-update-{index}"},
                json={"expected_revision": 1, "passengers": index + 2},
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(request, index) for index in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in results) == [200, 409]
    winner = next(response for response in results if response.status_code == 200)
    loser = next(response for response in results if response.status_code == 409)
    assert_error(loser, 409, "state_changed")
    assert winner.json()["revision"] == 2
    assert len(backends) == 2
    assert sql("SELECT revision,passengers FROM trips WHERE id=%s", (trip["id"],)) == [
        {"revision": 2, "passengers": winner.json()["passengers"]}
    ]
    assert counts(sql) == {"trips": 1, "events": 2, "operations": 2}


def test_session_token_hash_expiry_and_logout(client, alice, sql):
    token = alice["Authorization"].split(" ", 1)[1]
    stored = sql("SELECT token_hash,user_id,expires_at > now() AS live,revoked FROM sessions")
    assert stored == [{
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "user_id": "personal_alice", "live": True, "revoked": False,
    }]
    current = client.get("/session", headers=alice)
    assert current.status_code == 200
    assert current.json()["id"] == "personal_alice"
    assert set(current.json()) == {"id", "display_name"}
    assert client.delete("/session", headers=alice).status_code == 204
    assert_error(client.get("/session", headers=alice), 401, "authentication_required")
    assert sql("SELECT revoked FROM sessions") == [{"revoked": True}]


def test_expired_session_is_not_authenticated(client, alice, sql):
    sql("UPDATE sessions SET expires_at=now()-interval '1 second' RETURNING token_hash")
    assert_error(client.get("/session", headers=alice), 401, "authentication_required")


def test_switch_account_revokes_previous_token(client, alice):
    switched = client.post("/demo/sessions", headers=alice, json={"account": "personal_bob"})
    assert switched.status_code == 201
    assert switched.json()["user"]["id"] == "personal_bob"
    assert_error(client.get("/session", headers=alice), 401, "authentication_required")
    bob = {"Authorization": f"Bearer {switched.json()['access_token']}"}
    assert client.get("/session", headers=bob).json()["id"] == "personal_bob"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invented"}, {"Authorization": "Basic admin"}])
def test_missing_or_forged_session_cannot_read_or_create(client, draft, headers, sql):
    assert_error(client.get("/session", headers=headers), 401, "authentication_required")
    response = client.post("/trips", headers={**headers, "Idempotency-Key": "forged"}, json=draft)
    assert_error(response, 401, "authentication_required")
    assert counts(sql) == {"trips": 0, "events": 0, "operations": 0}


@pytest.mark.parametrize("extra", [{"role": "admin"}, {"user_id": "personal_bob"}, {"workspace": "demo"}])
def test_login_rejects_client_identity_overrides(client, extra, sql):
    response = client.post("/demo/sessions", json={"account": "personal_alice", **extra})
    assert_error(response, 422, "invalid_request")
    assert sql("SELECT count(*) AS count FROM sessions") == [{"count": 0}]


@pytest.mark.parametrize("overrides", [
    {"owner_id": "personal_bob"}, {"role": "admin"}, {"user_id": "personal_bob"},
    {"workspace_id": "demo"}, {"revision": 88}, {"price": 0}, {"status": "paid"},
    {"passengers": True}, {"passengers": "2"}, {"passengers": 0}, {"passengers": 10},
    {"passengers": None}, {"travel_date": None}, {"travel_date": "not-a-date"},
    {"travel_date": 0}, {"travel_date": "0"}, {"travel_date": "2026-10-01T00:00:00"},
    {"flight_id": None}, {"flight_id": ""}, {"flight_id": "X" * 81},
])
def test_create_rejects_invalid_and_server_owned_fields(client, alice, draft, sql, overrides):
    response = client.post(
        "/trips", headers={**alice, "Idempotency-Key": "invalid"}, json={**draft, **overrides},
    )
    assert_error(response, 422, "invalid_request")
    assert counts(sql) == {"trips": 0, "events": 0, "operations": 0}


@pytest.mark.parametrize("parameters", [
    {"expected_revision": 1}, {"passengers": 2},
    {"expected_revision": True, "passengers": 2}, {"expected_revision": "1", "passengers": 2},
    {"expected_revision": 1, "passengers": None}, {"expected_revision": 1, "passengers": True},
    {"expected_revision": 1, "owner_id": "personal_bob", "passengers": 2},
    {"expected_revision": 1, "travel_date": None}, {"expected_revision": 1, "flight_id": None},
])
def test_patch_rejects_invalid_input_without_writing(client, alice, create, sql, parameters):
    trip = create()
    response = client.patch(
        f"/trips/{trip['id']}", headers={**alice, "Idempotency-Key": "invalid"}, json=parameters,
    )
    assert_error(response, 422, "invalid_request")
    assert client.get(f"/trips/{trip['id']}", headers=alice).json() == trip
    assert counts(sql) == {"trips": 1, "events": 1, "operations": 1}


@pytest.mark.parametrize("key", [None, "", "has space", "slash/key", "X" * 129])
def test_create_requires_valid_operation_key(client, alice, draft, sql, key):
    headers = dict(alice)
    if key is not None:
        headers["Idempotency-Key"] = key
    response = client.post("/trips", headers=headers, json=draft)
    assert_error(response, 422, "invalid_request")
    assert counts(sql) == {"trips": 0, "events": 0, "operations": 0}
