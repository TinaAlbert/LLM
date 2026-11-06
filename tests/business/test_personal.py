"""D1-02 个人路径：真实 PostgreSQL 独立终态、边界与故障反例。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, get_ident
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.business.db import connect
from services.business.manage import reset_test_database, seed
from services.business import personal, worker
from services.business import app as app_module

pytestmark = pytest.mark.integration


TABLES = (
    "trips", "payment_profiles", "quotes", "booking_locks", "payments",
    "payment_jobs", "provider_payments", "ticket_tasks", "tickets", "flights",
    "business_events", "idempotency",
)


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"error": code}


def write(client, headers, path, body=None, *, key=None, method="post"):
    response = getattr(client, method)(
        path, headers={**headers, "Idempotency-Key": key or str(uuid4())},
        json={} if body is None else body,
    )
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def bind(client, headers, *, source="demo_personal_alice", revision=0, key="bind"):
    return write(client, headers, "/payment-profile", {
        "source_ref": source, "expected_revision": revision,
    }, key=key, method="put")


def quote(client, headers, trip, *, revision=None, key="quote"):
    return write(client, headers, f"/trips/{trip['id']}/quotes", {
        "expected_revision": trip["revision"] if revision is None else revision,
    }, key=key)


def lock(client, headers, quotation, *, key="lock"):
    return write(client, headers, f"/quotes/{quotation['id']}/lock", key=key)


def pay(client, headers, booking, *, key="pay", amount=110000):
    return write(client, headers, f"/locks/{booking['id']}/payments", {
        "amount_minor": amount,
    }, key=key)


def ready(client, alice, create):
    trip = create()
    profile = bind(client, alice)
    quotation = quote(client, alice, trip)
    booking = lock(client, alice, quotation)
    return trip, profile, quotation, booking


def snapshot(sql):
    # 固定表白名单，读取实际持久行；不借业务成功判断函数自证。
    fragments = [
        f"'{name}', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]'::jsonb) FROM {name} t)"
        for name in TABLES
    ]
    return sql("SELECT jsonb_build_object(" + ",".join(fragments) + ") AS state")[0]["state"]


def inventory(sql, *, reserved, sold, travel_date="2026-10-01"):
    rows = sql(
        "SELECT capacity,reserved,sold,capacity-reserved-sold AS available FROM flights "
        "WHERE workspace_id='test' AND id='DEMO-PEK-SHA' AND travel_date=%s",
        (travel_date,),
    )
    assert rows == [{"capacity": 2, "reserved": reserved, "sold": sold,
                     "available": 2 - reserved - sold}]
    assert reserved >= 0 and sold >= 0 and reserved + sold <= 2


def process_payment(settings, payment, outcome="succeeded"):
    return worker.process_payment(settings.database_url, settings.workspace_id,
                                  payment["id"], outcome=outcome)


def process_ticket(settings, task_id, outcome="succeeded"):
    return worker.process_ticket(settings.database_url, settings.workspace_id,
                                 task_id, outcome=outcome)


def advance(settings, seconds):
    return worker.advance_time(settings.database_url, settings.workspace_id, seconds)


def expire(settings):
    return worker.expire(settings.database_url, settings.workspace_id)


@contextmanager
def fail_business_events(settings):
    # 让真实数据库在事件写入时失败，验证状态和幂等记录与事件共同回滚。
    with connect(settings.database_url) as connection:
        connection.execute("""CREATE FUNCTION test_reject_event() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test event failure'; END $$""")
        connection.execute("""CREATE TRIGGER test_reject_event BEFORE INSERT ON business_events
            FOR EACH ROW EXECUTE FUNCTION test_reject_event()""")
    try:
        yield
    finally:
        with connect(settings.database_url) as connection:
            connection.execute("DROP TRIGGER IF EXISTS test_reject_event ON business_events")
            connection.execute("DROP FUNCTION IF EXISTS test_reject_event()")


def test_c01_personal_ticket_has_independent_database_terminal_state(
    client, alice, create, sql, settings,
):
    trip, profile, quotation, booking = ready(client, alice, create)
    assert profile["owner_id"] == "personal_alice"
    assert profile["revision"] == 1
    assert quotation["amount_minor"] == booking["amount_minor"] == 110000
    assert quotation["currency"] == booking["currency"] == "DEMO"
    assert quotation["trip_revision"] == booking["trip_revision"] == 1
    assert quotation["profile_revision"] == booking["profile_revision"] == 1
    assert quotation["expires_at"] == booking["expires_at"]
    assert datetime.fromisoformat(quotation["expires_at"]) == datetime(2026, 9, 27, tzinfo=UTC) + timedelta(seconds=300)
    inventory(sql, reserved=1, sold=0)

    payment = pay(client, alice, booking)
    assert payment["status"] == "processing"
    assert sql("SELECT count(*) AS n FROM payment_jobs") == [{"n": 1}]
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 0}]
    assert sql("SELECT status FROM booking_locks") == [{"status": "protected"}]
    process_payment(settings, payment)
    paid = client.get(f"/payments/{payment['id']}", headers=alice).json()
    assert paid["status"] == "succeeded"
    task_id = paid["ticket_task_id"]
    assert task_id
    assert client.get(f"/ticket-tasks/{task_id}", headers=alice).json()["status"] == "pending"
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 0}]
    # 付款成功仍不是出票终态；任务单独推进。
    process_ticket(settings, task_id)
    task = client.get(f"/ticket-tasks/{task_id}", headers=alice).json()
    assert task["status"] == "succeeded"
    ticket = client.get(f"/tickets/{task['ticket_id']}", headers=alice).json()
    assert ticket["status"] == "valid"
    assert ticket["trip_id"] == trip["id"]
    assert ticket["trip_revision"] == 1

    assert sql("SELECT revision,status FROM trips WHERE id=%s", (trip["id"],)) == [
        {"revision": 1, "status": "paid"}
    ]
    assert sql("SELECT p.status,p.amount_minor,l.trip_revision FROM payments p "
               "JOIN booking_locks l ON (l.workspace_id,l.id)=(p.workspace_id,p.lock_id)") == [
        {"status": "succeeded", "amount_minor": 110000, "trip_revision": 1}
    ]
    assert sql("SELECT trip_revision,quantity,length(certificate)>0 AS certified FROM tickets") == [
        {"trip_revision": 1, "quantity": 1, "certified": True}
    ]
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 1}]
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [
        {"n": 1, "captured": 110000}
    ]
    assert sql("SELECT status FROM booking_locks") == [{"status": "consumed"}]
    inventory(sql, reserved=0, sold=1)
    before = snapshot(sql)
    process_payment(settings, payment)
    process_ticket(settings, task_id)
    assert snapshot(sql) == before


def test_unbound_profile_and_unavailable_flight_cannot_quote(client, alice, create, sql):
    trip = create()
    assert client.get(f"/trips/{trip['id']}", headers=alice).json()["price"] is None
    before = snapshot(sql)
    response = client.post(f"/trips/{trip['id']}/quotes", headers={**alice, "Idempotency-Key": "unbound"},
                           json={"expected_revision": 1})
    assert_error(response, 409, "requires_payment_profile")
    assert snapshot(sql) == before
    bind(client, alice)
    unavailable = create(key="invented-flight", parameters={
        "flight_id": "INVENTED-FLIGHT", "travel_date": "2026-10-01", "passengers": 1,
    })
    before = snapshot(sql)
    response = client.post(f"/trips/{unavailable['id']}/quotes", headers={**alice, "Idempotency-Key": "not-saleable"},
                           json={"expected_revision": 1})
    assert_error(response, 409, "flight_unavailable")
    assert snapshot(sql) == before


@pytest.mark.parametrize("source", ["demo_personal_bob", "unknown-source"])
def test_profile_source_must_belong_to_actor(client, alice, sql, source):
    before = snapshot(sql)
    response = client.put("/payment-profile", headers={**alice, "Idempotency-Key": "forged-source"},
                          json={"source_ref": source, "expected_revision": 0})
    assert_error(response, 404, "not_visible")
    assert snapshot(sql) == before


def test_quote_does_not_reserve_inventory_and_locks_freeze_amount(client, alice, create, sql):
    trip = create()
    bind(client, alice)
    quotation = quote(client, alice, trip)
    second = quote(client, alice, trip, key="quote-again")
    assert second["id"] != quotation["id"]
    inventory(sql, reserved=0, sold=0)
    booking = lock(client, alice, quotation)
    sql("UPDATE flights SET base_minor=123456 WHERE workspace_id='test' RETURNING id")
    assert client.get(f"/locks/{booking['id']}", headers=alice).json()["amount_minor"] == 110000
    pay(client, alice, booking)
    assert sql("SELECT amount_minor FROM payments") == [{"amount_minor": 110000}]


def test_profile_quote_lock_and_payment_idempotency_preserve_original_responses(
    client, alice, create, sql, settings,
):
    trip, profile, quotation, booking = ready(client, alice, create)
    assert bind(client, alice) == profile
    assert quote(client, alice, trip) == quotation
    assert lock(client, alice, quotation) == booking
    payment = pay(client, alice, booking)
    before = snapshot(sql)
    assert pay(client, alice, booking) == payment
    assert snapshot(sql) == before
    for path, body, key, method in [
        ("/payment-profile", {"source_ref": "demo_personal_alice_alt", "expected_revision": 0}, "bind", "put"),
        (f"/trips/{trip['id']}/quotes", {"expected_revision": 2}, "quote", "post"),
        (f"/locks/{booking['id']}/payments", {"amount_minor": 110001}, "pay", "post"),
    ]:
        response = getattr(client, method)(path, headers={**alice, "Idempotency-Key": key}, json=body)
        assert_error(response, 409, "idempotency_conflict")
    process_payment(settings, payment)
    before = snapshot(sql)
    assert pay(client, alice, booking) == payment  # 原响应仍为 processing。
    assert lock(client, alice, quotation) == booking  # 原响应仍为 held。
    assert client.get(f"/payments/{payment['id']}", headers=alice).json()["status"] == "succeeded"
    assert snapshot(sql) == before


def test_new_keys_cannot_duplicate_active_lock_or_payment(client, alice, create, sql, settings):
    trip, profile, quotation, booking = ready(client, alice, create)
    before = snapshot(sql)
    response = client.post(f"/quotes/{quotation['id']}/lock", headers={**alice, "Idempotency-Key": "lock-new"}, json={})
    assert_error(response, 409, "active_lock_exists")
    assert snapshot(sql) == before
    payment = pay(client, alice, booking)
    for succeeded in (False, True):
        if succeeded:
            process_payment(settings, payment)
        before = snapshot(sql)
        response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": f"pay-new-{succeeded}"},
                               json={"amount_minor": 110000})
        assert_error(response, 409, "payment_exists")
        assert snapshot(sql) == before


def test_wrong_amount_does_not_accept_payment(client, alice, create, sql):
    _, _, _, booking = ready(client, alice, create)
    before = snapshot(sql)
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "wrong-amount"},
                           json={"amount_minor": 1100})
    assert_error(response, 409, "amount_mismatch")
    assert snapshot(sql) == before
    inventory(sql, reserved=1, sold=0)


def test_299_acceptance_protects_inventory_at_301(client, alice, create, sql, settings):
    _, _, _, booking = ready(client, alice, create)
    advance(settings, 299)
    payment = pay(client, alice, booking)
    advance(settings, 2)
    expire(settings)
    inventory(sql, reserved=1, sold=0)
    assert sql("SELECT status FROM booking_locks") == [{"status": "protected"}]
    process_payment(settings, payment)
    assert sql("SELECT status,amount_minor FROM payments") == [{"status": "succeeded", "amount_minor": 110000}]
    inventory(sql, reserved=0, sold=1)


def test_exact_300_expiry_is_read_only_then_released_once(client, alice, create, sql, settings):
    _, _, quotation, booking = ready(client, alice, create)
    advance(settings, 300)
    before = snapshot(sql)
    for path in (f"/quotes/{quotation['id']}", f"/locks/{booking['id']}"):
        response = client.get(path, headers=alice)
        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert response.json()["effective_status"] == "expired"
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "at-expiry"},
                           json={"amount_minor": 110000})
    assert_error(response, 409, "quote_expired")
    assert snapshot(sql) == before
    expire(settings)
    inventory(sql, reserved=0, sold=0)
    before = snapshot(sql)
    expire(settings)
    assert snapshot(sql) == before


def test_exact_300_quote_cannot_first_lock_inventory(client, alice, create, sql, settings):
    trip = create()
    bind(client, alice)
    quotation = quote(client, alice, trip)
    advance(settings, 300)
    before = snapshot(sql)
    response = client.post(f"/quotes/{quotation['id']}/lock",
                           headers={**alice, "Idempotency-Key": "late-lock"}, json={})
    assert_error(response, 409, "quote_expired")
    assert snapshot(sql) == before
    inventory(sql, reserved=0, sold=0)


@pytest.mark.parametrize("change", ["trip", "profile"])
def test_draft_or_profile_revision_invalidates_unpaid_dependencies_and_releases(
    change, client, alice, create, sql,
):
    trip, profile, quotation, booking = ready(client, alice, create)
    if change == "trip":
        revised = write(client, alice, f"/trips/{trip['id']}", {
            "expected_revision": 1, "travel_date": "2026-10-02",
        }, method="patch")
        assert revised["revision"] == 2
    else:
        revised = bind(client, alice, source="demo_personal_alice_alt", revision=1, key="rebind")
        assert revised["revision"] == 2
    inventory(sql, reserved=0, sold=0)
    before = snapshot(sql)
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "stale-lock"},
                           json={"amount_minor": 110000})
    assert_error(response, 409, "state_changed")
    response = client.post(f"/quotes/{quotation['id']}/lock", headers={**alice, "Idempotency-Key": "stale-quote"}, json={})
    assert_error(response, 409, "state_changed")
    assert snapshot(sql) == before


@pytest.mark.parametrize("outcome", [None, "unknown", "lost_response", "succeeded"])
def test_accepted_payment_protects_trip_and_lock_from_mutation(
    outcome, client, alice, create, sql, settings,
):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    if outcome:
        process_payment(settings, payment, outcome)
    before = snapshot(sql)
    for method, path, body in [
        ("patch", f"/trips/{trip['id']}", {"expected_revision": 1, "passengers": 2}),
        ("post", f"/locks/{booking['id']}/cancel", {}),
    ]:
        response = getattr(client, method)(path, headers={**alice, "Idempotency-Key": str(uuid4())}, json=body)
        assert_error(response, 409, "payment_protected")
    if outcome != "succeeded":
        response = client.put("/payment-profile", headers={**alice, "Idempotency-Key": "protected-profile"},
                              json={"source_ref": "demo_personal_alice_alt", "expected_revision": 1})
        assert_error(response, 409, "payment_protected")
    assert snapshot(sql) == before


def test_lost_payment_response_reconciles_without_second_capture(client, alice, create, sql, settings):
    _, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    process_payment(settings, payment, "lost_response")
    assert sql("SELECT status FROM payments") == [{"status": "unknown"}]
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 110000}]
    advance(settings, 301)
    expire(settings)
    inventory(sql, reserved=1, sold=0)
    before = snapshot(sql)
    for _ in range(3):
        assert client.get(f"/payments/{payment['id']}", headers=alice).json()["status"] == "unknown"
    assert snapshot(sql) == before
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "new-pay"},
                           json={"amount_minor": 110000})
    assert_error(response, 409, "payment_exists")
    reconciled = write(client, alice, f"/payments/{payment['id']}/reconcile", key="reconcile")
    assert reconciled["status"] == "succeeded"
    assert write(client, alice, f"/payments/{payment['id']}/reconcile", key="reconcile") == reconciled
    write(client, alice, f"/payments/{payment['id']}/reconcile", key="reconcile-new")
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 110000}]
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 1}]
    inventory(sql, reserved=0, sold=1)


def test_unknown_without_provider_result_remains_protected(client, alice, create, sql, settings):
    _, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    process_payment(settings, payment, "unknown")
    advance(settings, 301)
    expire(settings)
    for key in ("reconcile-1", "reconcile-2"):
        assert write(client, alice, f"/payments/{payment['id']}/reconcile", key=key)["status"] == "unknown"
    assert sql("SELECT count(*) AS n FROM provider_payments") == [{"n": 0}]
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 0}]
    inventory(sql, reserved=1, sold=0)
    # 明确推进同一供应商 operation 后才收敛；仍使用已受理付款与冻结金额。
    process_payment(settings, payment)
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 110000}]
    assert sql("SELECT status FROM payments") == [{"status": "succeeded"}]
    inventory(sql, reserved=0, sold=1)


def test_payment_failure_releases_and_requires_new_quote_lock(client, alice, create, sql, settings):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    process_payment(settings, payment, "failed")
    assert sql("SELECT status FROM payments") == [{"status": "failed"}]
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 0}]
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 0}]
    inventory(sql, reserved=0, sold=0)
    before = snapshot(sql)
    process_payment(settings, payment, "succeeded")
    assert snapshot(sql) == before
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "failed-lock-pay"},
                           json={"amount_minor": 110000})
    assert_error(response, 409, "state_changed")
    new_quote = quote(client, alice, trip, key="retry-quote")
    new_lock = lock(client, alice, new_quote, key="retry-lock")
    retry = pay(client, alice, new_lock, key="retry-pay")
    process_payment(settings, retry)
    assert sql("SELECT status,count(*) AS n FROM payments GROUP BY status ORDER BY status") == [
        {"status": "failed", "n": 1}, {"status": "succeeded", "n": 1}
    ]
    assert sql("SELECT sum(captured_minor) AS captured FROM provider_payments") == [{"captured": 110000}]
    inventory(sql, reserved=0, sold=1)


def test_ticket_failure_keeps_paid_state_without_refund_or_repayment(client, alice, create, sql, settings):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    process_payment(settings, payment)
    task_id = client.get(f"/payments/{payment['id']}", headers=alice).json()["ticket_task_id"]
    process_ticket(settings, task_id, "failed")
    task = client.get(f"/ticket-tasks/{task_id}", headers=alice).json()
    assert task["status"] == "failed" and task["ticket_id"] is None
    before = snapshot(sql)
    process_ticket(settings, task_id, "succeeded")
    assert snapshot(sql) == before
    assert sql("SELECT status FROM trips") == [{"status": "paid"}]
    assert sql("SELECT status FROM payments") == [{"status": "succeeded"}]
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 0}]
    assert sql("SELECT sum(captured_minor) AS captured FROM provider_payments") == [{"captured": 110000}]
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "paid-no-ticket"},
                           json={"amount_minor": 110000})
    assert_error(response, 409, "payment_exists")
    inventory(sql, reserved=0, sold=1)


def test_cancel_releases_inventory_exactly_once(client, alice, create, sql):
    _, _, _, booking = ready(client, alice, create)
    first = write(client, alice, f"/locks/{booking['id']}/cancel", key="cancel")
    inventory(sql, reserved=0, sold=0)
    assert write(client, alice, f"/locks/{booking['id']}/cancel", key="cancel") == first
    write(client, alice, f"/locks/{booking['id']}/cancel", key="cancel-new")
    inventory(sql, reserved=0, sold=0)
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "cancelled-pay"},
                           json={"amount_minor": 110000})
    assert_error(response, 409, "state_changed")


def test_all_personal_objects_recheck_owner_and_hide_existence(client, alice, bob, create, sql, settings):
    trip, _, quotation, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    process_payment(settings, payment)
    task_id = client.get(f"/payments/{payment['id']}", headers=alice).json()["ticket_task_id"]
    process_ticket(settings, task_id)
    ticket_id = client.get(f"/ticket-tasks/{task_id}", headers=alice).json()["ticket_id"]
    before = snapshot(sql)
    for resource, object_id in [("quotes", quotation["id"]), ("locks", booking["id"]),
                                ("payments", payment["id"]), ("ticket-tasks", task_id), ("tickets", ticket_id)]:
        for identifier in (object_id, str(uuid4()), "invalid-uuid"):
            response = client.get(f"/{resource}/{identifier}?role=admin&user_id=personal_alice",
                                  headers={**bob, "X-Role": "admin", "X-User-ID": "personal_alice"})
            assert_error(response, 404, "not_visible")
    for path, body in [
        (f"/trips/{trip['id']}/quotes", {"expected_revision": 1}),
        (f"/quotes/{quotation['id']}/lock", {}),
        (f"/locks/{booking['id']}/cancel", {}),
        (f"/locks/{booking['id']}/payments", {"amount_minor": 110000}),
        (f"/payments/{payment['id']}/reconcile", {}),
    ]:
        response = client.post(path, headers={**bob, "Idempotency-Key": "forbidden"}, json=body)
        assert_error(response, 404, "not_visible")
    assert snapshot(sql) == before


@pytest.mark.parametrize("extra", [{"role": "admin"}, {"owner_id": "personal_bob"},
                                   {"outcome": "succeeded"}, {"fault": "none"}, {"clock": 0}])
def test_payment_input_cannot_override_identity_or_internal_control(extra, client, alice, create, sql):
    _, _, _, booking = ready(client, alice, create)
    before = snapshot(sql)
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "inject"},
                           json={"amount_minor": 110000, **extra})
    assert_error(response, 422, "invalid_request")
    assert snapshot(sql) == before


@pytest.mark.parametrize("amount", [True, "110000", 110000.0, None, -1])
def test_payment_amount_requires_positive_integer_minor_units(amount, client, alice, create, sql):
    _, _, _, booking = ready(client, alice, create)
    before = snapshot(sql)
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "invalid-amount"},
                           json={"amount_minor": amount})
    assert_error(response, 422, "invalid_request")
    assert snapshot(sql) == before


@pytest.mark.parametrize("stage", ["profile", "quote", "lock", "payment", "cancel"])
def test_business_event_failure_rolls_back_api_state_inventory_and_idempotency(
    stage, client, alice, create, sql, settings,
):
    trip = create()
    if stage == "profile":
        method, path, body = "put", "/payment-profile", {"source_ref": "demo_personal_alice", "expected_revision": 0}
    else:
        bind(client, alice)
        if stage == "quote":
            method, path, body = "post", f"/trips/{trip['id']}/quotes", {"expected_revision": 1}
        else:
            quotation = quote(client, alice, trip)
            if stage == "lock":
                method, path, body = "post", f"/quotes/{quotation['id']}/lock", {}
            else:
                booking = lock(client, alice, quotation)
                if stage == "payment":
                    method, path, body = "post", f"/locks/{booking['id']}/payments", {"amount_minor": 110000}
                else:
                    method, path, body = "post", f"/locks/{booking['id']}/cancel", {}
    before = snapshot(sql)
    with fail_business_events(settings):
        response = getattr(client, method)(path, headers={**alice, "Idempotency-Key": "retry-rollback"}, json=body)
        assert_error(response, 503, "storage_unavailable")
    assert snapshot(sql) == before
    write(client, alice, path, body, key="retry-rollback", method=method)


def test_fixed_initial_state_can_be_reproduced_after_personal_flow(client, alice, create, sql, settings):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    process_payment(settings, payment)
    task_id = client.get(f"/payments/{payment['id']}", headers=alice).json()["ticket_task_id"]
    process_ticket(settings, task_id)
    advance(settings, 500)
    reset_test_database(settings.database_url, confirmation="llm_business_test")
    seed(settings.database_url, settings.workspace_id)
    reset = snapshot(sql)
    for table in TABLES:
        if table != "flights":
            assert reset[table] == []
    inventory(sql, reserved=0, sold=0)
    assert sql("SELECT business_time FROM workspaces WHERE id='test'") == [
        {"business_time": datetime(2026, 9, 27, tzinfo=UTC)}
    ]
    reset_test_database(settings.database_url, confirmation="llm_business_test")
    seed(settings.database_url, settings.workspace_id)
    assert snapshot(sql) == reset


def test_server_side_eligibility_is_rechecked_before_quote_and_payment(client, alice, create, sql):
    trip = create()
    bind(client, alice)
    sql("UPDATE payment_sources SET eligible=false WHERE ref='demo_personal_alice' RETURNING ref")
    before = snapshot(sql)
    response = client.post(f"/trips/{trip['id']}/quotes", headers={**alice, "Idempotency-Key": "ineligible-quote"},
                           json={"expected_revision": 1})
    assert_error(response, 409, "payment_profile_ineligible")
    assert snapshot(sql) == before
    sql("UPDATE payment_sources SET eligible=true WHERE ref='demo_personal_alice' RETURNING ref")
    quotation = quote(client, alice, trip)
    booking = lock(client, alice, quotation)
    sql("UPDATE payment_sources SET eligible=false WHERE ref='demo_personal_alice' RETURNING ref")
    before = snapshot(sql)
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": "ineligible-pay"},
                           json={"amount_minor": 110000})
    assert_error(response, 409, "payment_profile_ineligible")
    assert snapshot(sql) == before


def test_ineligible_personal_source_cannot_be_bound(client, alice, sql):
    sql("UPDATE payment_sources SET eligible=false WHERE ref='demo_personal_alice' RETURNING ref")
    before = snapshot(sql)
    response = client.put("/payment-profile", headers={**alice, "Idempotency-Key": "bind-ineligible"},
                          json={"source_ref": "demo_personal_alice", "expected_revision": 0})
    assert_error(response, 409, "payment_profile_ineligible")
    assert snapshot(sql) == before


def test_stale_profile_and_trip_revisions_write_nothing(client, alice, create, sql):
    trip = create()
    bind(client, alice)
    write(client, alice, f"/trips/{trip['id']}", {"expected_revision": 1, "passengers": 2}, method="patch")
    before = snapshot(sql)
    response = client.put("/payment-profile", headers={**alice, "Idempotency-Key": "stale-profile"},
                          json={"source_ref": "demo_personal_alice_alt", "expected_revision": 0})
    assert_error(response, 409, "state_changed")
    response = client.post(f"/trips/{trip['id']}/quotes", headers={**alice, "Idempotency-Key": "stale-trip"},
                           json={"expected_revision": 1})
    assert_error(response, 409, "state_changed")
    assert snapshot(sql) == before


def test_replay_reauthorizes_associated_trip(client, alice, create, sql):
    trip, _, quotation, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    sql("UPDATE trips SET owner_id='personal_bob' WHERE id=%s RETURNING id", (trip["id"],))
    before = snapshot(sql)
    for path, body, key in [
        (f"/trips/{trip['id']}/quotes", {"expected_revision": 1}, "quote"),
        (f"/quotes/{quotation['id']}/lock", {}, "lock"),
        (f"/locks/{booking['id']}/payments", {"amount_minor": 110000}, "pay"),
    ]:
        response = client.post(path, headers={**alice, "Idempotency-Key": key}, json=body)
        assert_error(response, 404, "not_visible")
    assert snapshot(sql) == before


def test_profile_replay_reauthorizes_current_profile_owner(client, alice, sql):
    profile = bind(client, alice)
    sql("UPDATE payment_profiles SET owner_id='personal_bob' WHERE id=%s RETURNING id", (profile["id"],))
    before = snapshot(sql)
    response = client.put("/payment-profile", headers={**alice, "Idempotency-Key": "bind"},
                          json={"source_ref": "demo_personal_alice", "expected_revision": 0})
    assert_error(response, 404, "not_visible")
    assert snapshot(sql) == before


def test_definite_payment_failure_prevents_relocking_prior_quotes(client, alice, create, sql, settings):
    trip, _, quotation, booking = ready(client, alice, create)
    other_prior_quote = quote(client, alice, trip, key="prior-unused-quote")
    payment = pay(client, alice, booking)
    process_payment(settings, payment, "failed")
    before = snapshot(sql)
    for old_quote in (quotation, other_prior_quote):
        response = client.post(f"/quotes/{old_quote['id']}/lock",
                               headers={**alice, "Idempotency-Key": str(uuid4())}, json={})
        assert_error(response, 409, "state_changed")
    assert snapshot(sql) == before
    inventory(sql, reserved=0, sold=0)


def test_ticket_event_failure_rolls_back_ticket_and_task(client, alice, create, sql, settings):
    _, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    process_payment(settings, payment)
    task_id = client.get(f"/payments/{payment['id']}", headers=alice).json()["ticket_task_id"]
    before = snapshot(sql)
    with fail_business_events(settings):
        with pytest.raises(psycopg.Error):
            process_ticket(settings, task_id)
    assert snapshot(sql) == before
    process_ticket(settings, task_id)
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 1}]


def test_payment_event_failure_rolls_back_capture_and_business_state(client, alice, create, sql, settings):
    _, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    before = snapshot(sql)
    with fail_business_events(settings):
        with pytest.raises(psycopg.Error):
            process_payment(settings, payment)
    assert snapshot(sql) == before
    process_payment(settings, payment)
    assert sql("SELECT status FROM payments") == [{"status": "succeeded"}]
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 1}]
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 110000}]
    inventory(sql, reserved=0, sold=1)


def test_reconcile_event_failure_preserves_existing_capture_without_partial_settlement(
    client, alice, create, sql, settings,
):
    _, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    process_payment(settings, payment, "lost_response")
    before = snapshot(sql)
    with fail_business_events(settings):
        response = client.post(f"/payments/{payment['id']}/reconcile",
                               headers={**alice, "Idempotency-Key": "reconcile-retry"}, json={})
        assert_error(response, 503, "storage_unavailable")
    assert snapshot(sql) == before
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 110000}]
    assert sql("SELECT status FROM payments") == [{"status": "unknown"}]
    inventory(sql, reserved=1, sold=0)
    result = write(client, alice, f"/payments/{payment['id']}/reconcile", key="reconcile-retry")
    assert result["status"] == "succeeded"
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 1}]
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 110000}]
    inventory(sql, reserved=0, sold=1)


def synchronize_connections(monkeypatch, module):
    """确保两个真实 PG 事务均开始，随后再竞争相同业务/库存锁。"""
    original = module.connect
    barrier = Barrier(2)
    seen_threads, backend_ids = set(), set()

    @contextmanager
    def synchronized(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            thread_id = get_ident()
            if thread_id not in seen_threads:
                seen_threads.add(thread_id)
                backend_ids.add(connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                barrier.wait(timeout=15)
            yield connection

    monkeypatch.setattr(module, "connect", synchronized)
    return backend_ids


def concurrent(functions):
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(function) for function in functions]
        return [future.result(timeout=30) for future in futures]


def test_concurrent_independent_owners_cannot_oversell(app, client, alice, bob, create, sql, monkeypatch):
    trip_a = create(key="alice-pair", parameters={"flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 2})
    trip_b = create(key="bob-pair", headers=bob, parameters={"flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 2})
    bind(client, alice)
    bind(client, bob, source="demo_personal_bob")
    quote_a = quote(client, alice, trip_a)
    quote_b = quote(client, bob, trip_b)
    assert quote_a["amount_minor"] == quote_b["amount_minor"] == 220000
    backends = synchronize_connections(monkeypatch, personal)

    def request(headers, quotation):
        with TestClient(app) as separate:
            return separate.post(f"/quotes/{quotation['id']}/lock",
                                 headers={**headers, "Idempotency-Key": "contended-lock"}, json={})

    results = concurrent([lambda: request(alice, quote_a), lambda: request(bob, quote_b)])
    assert sorted(result.status_code for result in results) == [201, 409]
    assert_error(next(result for result in results if result.status_code == 409), 409, "inventory_unavailable")
    assert len(backends) == 2
    assert sql("SELECT count(*) AS n FROM booking_locks") == [{"n": 1}]
    inventory(sql, reserved=2, sold=0)


def test_concurrent_payment_new_keys_accept_once(app, client, alice, create, sql, monkeypatch):
    _, _, _, booking = ready(client, alice, create)
    backends = synchronize_connections(monkeypatch, personal)

    def request(key):
        with TestClient(app) as separate:
            return separate.post(f"/locks/{booking['id']}/payments", headers={**alice, "Idempotency-Key": key},
                                 json={"amount_minor": 110000})

    results = concurrent([lambda: request("payment-a"), lambda: request("payment-b")])
    assert sorted(result.status_code for result in results) == [202, 409]
    assert_error(next(result for result in results if result.status_code == 409), 409, "payment_exists")
    assert len(backends) == 2
    assert sql("SELECT count(*) AS n FROM payments") == [{"n": 1}]
    assert sql("SELECT count(*) AS n FROM payment_jobs") == [{"n": 1}]
    inventory(sql, reserved=1, sold=0)


def test_concurrent_workers_capture_and_issue_once(client, alice, create, sql, settings, monkeypatch):
    _, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    with monkeypatch.context() as scoped:
        backends = synchronize_connections(scoped, worker.db)
        concurrent([lambda: process_payment(settings, payment), lambda: process_payment(settings, payment)])
        assert len(backends) == 2
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 110000}]
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 1}]
    task_id = client.get(f"/payments/{payment['id']}", headers=alice).json()["ticket_task_id"]
    with monkeypatch.context() as scoped:
        backends = synchronize_connections(scoped, worker.db)
        concurrent([lambda: process_ticket(settings, task_id), lambda: process_ticket(settings, task_id)])
        assert len(backends) == 2
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 1}]
    inventory(sql, reserved=0, sold=1)


def test_trip_get_uses_one_snapshot_during_concurrent_revision_and_quote(
    app, client, alice, create, monkeypatch,
):
    trip = create()
    bind(client, alice)
    quote(client, alice, trip)
    entered, release = Event(), Event()
    original = app_module.owned_trip

    def pause_after_trip(*args, **kwargs):
        row = original(*args, **kwargs)
        if not kwargs.get("lock") and not entered.is_set():
            entered.set()
            assert release.wait(timeout=15)
        return row

    monkeypatch.setattr(app_module, "owned_trip", pause_after_trip)

    def read():
        with TestClient(app) as separate:
            return separate.get(f"/trips/{trip['id']}", headers=alice)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read)
        assert entered.wait(timeout=15)
        try:
            revised = write(client, alice, f"/trips/{trip['id']}",
                            {"expected_revision": 1, "passengers": 2}, method="patch")
            newer = quote(client, alice, revised, key="quote-revision-2")
            assert newer["amount_minor"] == 220000
        finally:
            release.set()
        response = future.result(timeout=15)
    assert response.status_code == 200
    old = response.json()
    assert old["revision"] == 1 and old["passengers"] == 1
    assert old["price"] == "1100.00" and old["price_status"] == "quoted"
    latest = client.get(f"/trips/{trip['id']}", headers=alice).json()
    assert latest["revision"] == 2 and latest["price"] == "2200.00"
