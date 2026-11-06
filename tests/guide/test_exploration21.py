"""P21离线边界反例；HTTP替身只测契约，不计业务/隔离/模型效果。"""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from services.guide.exploration import (BoundedExploration, ExplorationError,
                                        Operation, Recipient, TARGET)


def fixture():
    ids = (str(uuid4()), str(uuid4()))
    coupon_id = str(uuid4())
    bookings = {}
    for index, trip_id in enumerate(ids):
        bookings[trip_id] = {"trip": {"id": trip_id, "revision": 1, "flight_id": "DEMO-" + ("LARGE" if index == 0 else "SMALL"),
                            "travel_date": "2026-10-01", "passengers": 1, "status": "draft"},
                            "quote": None, "lock": None, "payment": None, "ticket_task": None, "ticket": None}
    recipient = Recipient("receiver", "generation-1", 1)
    state = SimpleNamespace(recipient=recipient, actor="personal_alice", bookings=bookings, requests=[],
                            now=0.0, after_request=None, coupon={"id": coupon_id, "status": "available", "percent": 20,
                                                               "cap_minor": 31234, "currency": "DEMO"})
    def handler(request):
        state.requests.append(request)
        path = request.url.path
        if path == "/session": value = {"id": state.actor}
        elif path == "/coupons": value = {"coupons": [state.coupon]}
        elif path.startswith("/trips/") and path.endswith("/booking"):
            value = state.bookings[path.split("/")[2]]
        else:
            value = {}
        if state.after_request:
            state.after_request(request)
        return httpx.Response(200, json=deepcopy(value))
    client = httpx.Client(base_url=TARGET, transport=httpx.MockTransport(handler), trust_env=False)
    session = BoundedExploration(client, {"Authorization": "Bearer private-synthetic-token"}, ids,
                                 recipient, lambda: state.recipient, clock=lambda: state.now)
    return session, state, ids


@pytest.mark.parametrize("field", ["url", "path", "object_id", "identity", "role", "amount_minor", "reset", "clock"])
def test_operation_cannot_supply_authority_or_arbitrary_inputs(field):
    with pytest.raises(ValidationError):
        Operation.model_validate({"operation": "booking.lock", "target": "trip_1", field: "forbidden"})


@pytest.mark.parametrize("payload", [
    {"operation": "booking.lock", "target": str(uuid4())}, {"operation": "booking.lock"},
    {"operation": "observe", "target": "trip_1"}, {"operation": "reset"},
    {"operation": "worker.advance"}, {"operation": "profile.bind", "target": "trip_1"},
])
def test_only_fixed_semantic_actions_and_delegated_aliases(payload):
    with pytest.raises(ValidationError): Operation.model_validate(payload)


def test_destination_fixed_and_not_a_model_input():
    client = SimpleNamespace(base_url="http://127.0.0.1:55432")
    with pytest.raises(ExplorationError, match="destination_not_allowed"):
        BoundedExploration(client, {"Authorization": "Bearer test"}, (str(uuid4()), str(uuid4())),
                           Recipient("x", "1", 1), lambda: None)


def test_simulated_role_is_derived_from_http_session():
    session, state, _ = fixture()
    state.actor = "approver_lin"
    with pytest.raises(ExplorationError, match="simulated_identity_not_allowed"):
        session.act({"operation": "observe"})
    assert len(state.requests) == 1


@pytest.mark.parametrize("change", [
    {"subject": "other"}, {"generation": "generation-2"}, {"disclosure_revision": 2},
    {"allow_synthetic_procedure": False},
])
def test_recipient_change_prevents_old_or_late_delivery(change):
    session, state, _ = fixture()
    session.act({"operation": "observe"})
    state.recipient = replace(state.recipient, **change)
    before = len(state.requests)
    with pytest.raises(ExplorationError, match="session_changed"):
        session.deliver()
    assert len(state.requests) == before


def test_switch_during_http_discards_result_without_retry():
    session, state, _ = fixture()
    state.after_request = lambda request: setattr(state, "recipient", replace(state.recipient, generation="new"))
    with pytest.raises(ExplorationError, match="session_changed"):
        session.act({"operation": "observe"})
    assert len(state.requests) == 1 and session._trace == []


@pytest.mark.parametrize("kind", ["deadline", "operations", "requests"])
def test_fixed_limits_stop_before_another_http_request(kind):
    session, state, _ = fixture()
    if kind == "deadline": state.now = 180
    elif kind == "operations": session._count = 28
    else: session._requests = 120
    with pytest.raises(ExplorationError, match="exploration_budget_exceeded"):
        session.act({"operation": "observe"})
    assert state.requests == []


def test_dispose_removes_credentials_objects_and_old_trace():
    session, state, _ = fixture()
    session.act({"operation": "observe"})
    session.dispose()
    assert session._headers == session._ids == {} and session._trace == []
    with pytest.raises(ExplorationError, match="exploration_disposed"):
        session.deliver()


def test_simulated_amount_scope_masks_coupon_cap_as_well_as_booking_amounts():
    session, state, ids = fixture()
    session._recipient = state.recipient = replace(state.recipient, allow_simulated_amounts=False)
    state.bookings[ids[0]]["quote"] = {"trip_revision": 1, "amount_minor": 731, "base_amount_minor": 1000,
        "coupon_discount_minor": 269, "currency": "DEMO", "effective_status": "active", "valid": True,
        "expires_at": "2026-09-27T00:05:00+00:00", "price_basis": "estimate"}
    session.act({"operation": "observe"})
    result = session.deliver()
    assert result["trace"][0]["state"]["shared_coupon"]["cap_minor"] is None
    quote = result["trace"][0]["state"]["delegated_trips"][0]["data"]["quote"]
    assert all(quote[field] is None for field in ("amount_minor", "base_amount_minor", "coupon_discount_minor"))
    assert quote["coupon_allocation"] == "estimate"
    assert "current_user_quote" in result["unknowns"]


def test_http_and_private_trace_extras_are_not_forwarded_or_claimed_isolated():
    session, state, ids = fixture()
    state.bookings[ids[0]]["trip"]["hidden_response"] = "private-value"
    session.act({"operation": "observe"})
    session._trace[0]["state"]["sql"] = "private-sql"
    result = session.deliver()
    encoded = json.dumps(result)
    assert all(value not in encoded for value in (*ids, state.coupon["id"], "private-value", "private-sql", "private-synthetic-token"))
    assert result["source_kind"] == "sandbox_observed"
    assert result["executor_isolation"]["accepted"] is False
    assert result["executor_isolation"]["unverified"] == ["file", "network", "child_process"]


def test_held_coupon_without_delegated_holder_keeps_attribution_unknown():
    session, state, _ = fixture()
    state.coupon["status"] = "held"
    result = session.act({"operation": "observe"})
    coupon = result["state"]["shared_coupon"]
    assert coupon["status"] == "held" and coupon["attribution"] == "unknown" and coupon["trip_ref"] is None
    # 不查第三对象，不从券占用状态断言另有委托之外的行程。
    assert all(request.url.path == "/session" or request.url.path == "/coupons"
               or request.url.path.endswith("/booking") for request in state.requests)


@pytest.mark.parametrize("field", ["flight_id", "unknowns", "result", "coupon_allocation", "coupon_percent"])
def test_delivery_rejects_tainted_leaf_values(field):
    session, _, _ = fixture()
    session.act({"operation": "observe"})
    row = session._trace[0]
    trip = row["state"]["delegated_trips"][0]
    if field == "flight_id": trip["data"]["trip"]["flight_id"] = "SELECT secret FROM db"
    elif field == "unknowns": trip["unknowns"] = ["Bearer private"]
    elif field == "result": row["result"] = "https://private.invalid"
    elif field == "coupon_percent": row["state"]["shared_coupon"]["percent"] = "private"
    else:
        trip["data"]["quote"] = {"trip_revision": 1, "amount_minor": 731, "base_amount_minor": 1000,
            "coupon_discount_minor": 269, "currency": "DEMO", "effective_status": "active", "valid": True,
            "expires_at": "2026-09-27T00:05:00+00:00", "price_basis": "estimate", "coupon_allocation": "Bearer secret"}
    with pytest.raises(ExplorationError): session.deliver()
