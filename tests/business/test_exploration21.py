"""P21真实PG/ASGI HTTP：有界探索状态、资源、当前授权；root仅在55433运行。"""
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from services.guide.exploration import BoundedExploration, ExplorationError, Recipient, TARGET
from test_coupons import coupon_state, terminal
from test_personal import process_payment, process_ticket, write

pytestmark = [pytest.mark.integration, pytest.mark.coupon]


def branch(app):
    client = TestClient(app, base_url=TARGET)
    response = client.post("/demo/sessions", json={"account": "personal_alice"})
    assert response.status_code == 201
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    write(client, headers, "/payment-profile", {"source_ref": "demo_personal_alice", "expected_revision": 0}, method="put")
    ids = tuple(write(client, headers, "/trips", {"flight_id": "DEMO-" + size,
                "travel_date": "2026-10-01", "passengers": 1})["id"] for size in ("LARGE", "SMALL"))
    recipient = Recipient("independent-current-user", uuid4().hex, 1)
    session = BoundedExploration(client, headers, ids, recipient, lambda: recipient)
    return client, headers, ids, session


def act(session, operation, ref=None):
    return session.act({"operation": operation, **({"target": ref} if ref else {})})


@pytest.mark.parametrize("order,expected_total,holder", [
    (("trip_1", "trip_2"), 140000, "trip_1"),
    (("trip_2", "trip_1"), 148000, "trip_2"),
])
def test_both_orders_use_real_anonymous_observations_and_independent_sql(order, expected_total, holder, app, sql, settings):
    client, headers, ids, session = branch(app)
    try:
        initial = act(session, "observe")
        assert initial["state"]["shared_coupon"]["status"] == "available"
        for ref in ("trip_1", "trip_2"):
            act(session, "booking.quote", ref)
        first = act(session, "booking.lock", order[0])
        assert first["state"]["shared_coupon"]["trip_ref"] == holder
        # 独立SQL证明第一次实际占用券，并非观察接口自行声明。
        owner = sql("SELECT t.id FROM coupons c JOIN booking_locks l ON l.id=c.lock_id JOIN trips t ON t.id=l.trip_id")
        assert len(owner) == 1 and str(owner[0]["id"]) == ids[("trip_1", "trip_2").index(holder)]
        second = act(session, "booking.lock", order[1])
        other = next(row for row in second["state"]["delegated_trips"] if row["ref"] == order[1])
        assert other["data"]["quote"]["coupon_allocation"] == "estimate"
        assert other["data"]["lock"]["coupon_allocation"] == "not_applied"
        for ref in order:
            pending = act(session, "booking.pay", ref)
            if ref == order[0]:
                assert pending["state"]["shared_coupon"]["status"] == "protected"
            payment = sql("SELECT id FROM payments WHERE trip_id=%s", (ids[("trip_1", "trip_2").index(ref)],))[0]
            paid = process_payment(settings, payment)
            process_ticket(settings, paid["ticket_task_id"])
            act(session, "observe")
        result = session.deliver()
        assert result["source_kind"] == "sandbox_observed" and not result["executor_isolation"]["accepted"]
        assert "current_user_quote" in result["unknowns"]
        terminal(sql, expected_total)
    finally:
        session.dispose()
        assert client.delete("/session", headers=headers).status_code == 204
        client.close()


def test_cancel_releases_once_and_consumed_resource_cannot_be_reordered(app, sql, settings):
    client, headers, ids, session = branch(app)
    try:
        for ref in ("trip_1", "trip_2"):
            act(session, "booking.quote", ref)
        act(session, "booking.lock", "trip_2")
        cancelled = act(session, "booking.cancel_lock", "trip_2")
        assert cancelled["state"]["shared_coupon"]["status"] == "available"
        coupon_state(sql, "available")
        act(session, "booking.cancel_lock", "trip_2")
        assert sql("SELECT count(*) AS n FROM business_events WHERE action='coupon.available'") == [{"n": 1}]
        act(session, "booking.lock", "trip_1")
        act(session, "booking.pay", "trip_1")
        protected = act(session, "booking.cancel_lock", "trip_1")
        assert protected["result"] == "precondition_not_met"
        coupon_state(sql, "protected")
        payment = sql("SELECT id FROM payments")[0]
        paid = process_payment(settings, payment)
        process_ticket(settings, paid["ticket_task_id"])
        consumed = act(session, "booking.cancel_lock", "trip_1")
        assert consumed["result"] == "precondition_not_met"
        coupon_state(sql, "consumed")
        second = act(session, "booking.lock", "trip_2")
        lock = second["state"]["delegated_trips"][1]["data"]["lock"]
        assert lock["amount_minor"] == 60000 and lock["coupon_allocation"] == "not_applied"
        assert sql("SELECT captured_minor FROM provider_payments") == [{"captured_minor": 80000}]
        session.deliver()
    finally:
        session.dispose()
        assert client.delete("/session", headers=headers).status_code == 204
        client.close()


@pytest.mark.parametrize("revocation", ["object", "session"])
def test_delivery_rechecks_current_simulated_access_after_observation(revocation, app, sql):
    client, headers, ids, session = branch(app)
    try:
        act(session, "observe")
        if revocation == "object":
            sql("UPDATE trips SET owner_id='personal_bob' WHERE id=%s RETURNING id", (ids[1],))
        else:
            assert client.delete("/session", headers=headers).status_code == 204
        with pytest.raises(ExplorationError, match="not_visible|authentication_required"):
            session.deliver()
        # 上次合法观察保留为工程轨迹；撤销后的deliver没有返回新接收者。
        assert len(session._trace) == 1
    finally:
        session.dispose()
        if revocation == "object":
            assert client.delete("/session", headers=headers).status_code == 204
        client.close()
