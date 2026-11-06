"""D3个人投影/真实PG取消；这些测试不算模型或AppContainer运行证据。"""

import json
import socket
import threading
import time
from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from psycopg.conninfo import make_conninfo
from starlette.requests import Request

from services.business import guide_projection as projection
from services.guide import knowledge
from services.guide.contracts import parse_candidate, validate_evidence
from test_observation import unchanged
from test_personal import advance, bind, lock, pay, process_payment, process_ticket, quote, ready, write

pytestmark = pytest.mark.integration


def request(headers):
    return Request({"type": "http", "headers": [(key.lower().encode(), value.encode())
                                                for key, value in headers.items()]})


def capture(settings, headers, trip_id, *, seconds=30):
    return projection.capture(settings, request(headers), trip_id, time.monotonic() + seconds)


def assert_failure(call, status, code):
    with pytest.raises(HTTPException) as raised:
        call()
    assert (raised.value.status_code, raised.value.detail) == (status, code)


def confirm_candidate(public):
    bundle = knowledge.load_knowledge()
    candidate = parse_candidate({
        'status': 'next_step', 'step_id': 'payment.confirm', 'anchor_id': 'payment.confirm',
        'explanation': '核对当前锁价后，在原入口确认付款',
        'preconditions': ['当前锁价有效'], 'expected_change': '查看原付款进度',
        'references': ['knowledge:payment.confirm', 'observation:current'],
        'unknowns': [], 'amount_ref': 'lock.amount_minor',
    })
    return validate_evidence(candidate, public, bundle.knowledge, bundle.anchors)


def test_definite_failed_payment_recovery_uses_real_new_lock_and_rejects_active_payment(
    client, settings, alice, create, sql,
):
    trip, _, _, old_lock = ready(client, alice, create)
    first = pay(client, alice, old_lock)
    process_payment(settings, first, 'failed')
    assert sql('SELECT status FROM payments') == [{'status': 'failed'}]
    assert sql('SELECT status FROM booking_locks') == [{'status': 'failed'}]
    assert sql('SELECT reserved,sold FROM flights WHERE travel_date=\'2026-10-01\'') == [
        {'reserved': 0, 'sold': 0}]
    before = unchanged(sql)
    with pytest.raises(ValueError, match='付款前提未成立'):
        confirm_candidate(capture(settings, alice, trip['id']).public)
    assert unchanged(sql) == before

    new_quote = quote(client, alice, trip, key='guide-recovery-quote')
    new_lock = lock(client, alice, new_quote, key='guide-recovery-lock')
    recovered = capture(settings, alice, trip['id'])
    assert recovered.ref_map['lock'] == new_lock['id'] != old_lock['id']
    assert recovered.public['data']['trip']['status'] == 'draft'
    assert recovered.public['data']['payment']['status'] == 'failed'
    assert recovered.public['data']['lock']['valid'] is True
    assert recovered.public['data']['lock']['effective_status'] == 'held'
    before = unchanged(sql)
    assert confirm_candidate(recovered.public).amount_ref == 'lock.amount_minor'
    assert unchanged(sql) == before

    retry = pay(client, alice, new_lock, key='guide-recovery-pay', amount=new_lock['amount_minor'])
    for state in ('processing', 'unknown', 'succeeded'):
        if state != 'processing':
            process_payment(settings, retry, state)
        current = capture(settings, alice, trip['id']).public
        assert current['data']['payment']['status'] == state
        before = unchanged(sql)
        with pytest.raises(ValueError, match='付款前提未成立'):
            confirm_candidate(current)
        assert unchanged(sql) == before
    assert sql('SELECT status,count(*) AS n FROM payments GROUP BY status ORDER BY status') == [
        {'status': 'failed', 'n': 1}, {'status': 'succeeded', 'n': 1}]
    assert sql('SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments') == [
        {'n': 2, 'captured': new_lock['amount_minor']}]
    assert sql('SELECT count(*) AS n FROM ticket_tasks') == [{'n': 1}]
    assert sql('SELECT count(*) AS n FROM tickets') == [{'n': 0}]


@pytest.mark.parametrize('change,status', [('cancel', 'cancelled'), ('revise', 'invalidated'),
                                          ('expire', 'expired')])
def test_failed_payment_recovery_cannot_confirm_released_or_stale_new_lock(
    client, settings, alice, create, sql, change, status,
):
    trip, _, _, old_lock = ready(client, alice, create)
    process_payment(settings, pay(client, alice, old_lock), 'failed')
    new_quote = quote(client, alice, trip, key='guide-recovery-quote')
    new_lock = lock(client, alice, new_quote, key='guide-recovery-lock')
    assert confirm_candidate(capture(settings, alice, trip['id']).public).amount_ref == 'lock.amount_minor'
    if change == 'cancel':
        write(client, alice, f"/locks/{new_lock['id']}/cancel", key='guide-recovery-cancel')
    elif change == 'revise':
        write(client, alice, f"/trips/{trip['id']}",
              {'expected_revision': trip['revision'], 'passengers': 2},
              key='guide-recovery-revise', method='patch')
    else:
        advance(settings, 300)
    current = capture(settings, alice, trip['id']).public
    assert current['data']['payment']['status'] == 'failed'
    assert current['data']['lock']['effective_status'] == status
    assert current['data']['lock']['valid'] is False
    if change == 'revise':
        assert current['data']['lock']['trip_revision'] != current['data']['trip']['revision']
    before = unchanged(sql)
    with pytest.raises(ValueError, match='付款前提未成立'):
        confirm_candidate(current)
    assert unchanged(sql) == before


def test_single_trip_anonymous_leaves_and_unknown_price(settings, alice, create, sql):
    trip = create()
    before = unchanged(sql)
    evidence = capture(settings, alice, trip["id"])
    public = evidence.public
    assert public["data"] == {
        "trip": {"ref": "trip", "revision": 1, "flight_id": "DEMO-PEK-SHA",
                 "travel_date": "2026-10-01", "passengers": 1, "status": "draft"},
        "quote": None, "lock": None, "payment": None, "ticket_task": None, "ticket": None,
    }
    assert public["payment_profile_state"] == "unbound"
    assert public["state_versions"]["payment_profile"] == 0
    assert sql("SELECT count(*) AS n FROM payment_profiles")[0]["n"] == 0
    assert public["flight_state"] == "available"  # 仅启用/适用，未预留库存。
    assert sql("SELECT reserved,sold FROM flights WHERE travel_date='2026-10-01'") == [{"reserved": 0, "sold": 0}]
    assert public["unknowns"] == ["current_quote_amount"]
    assert public["simulated_identity"] == "synthetic_personal"
    assert evidence.actor_id == "personal_alice"
    assert evidence.ref_map == {"trip": trip["id"]}
    serialized = json.dumps(public)
    assert trip["id"] not in serialized
    # disclosure_scope的公开标签current-session不含令牌；按JSON键检查私有字段，
    # 不能把合法来源标签中的"session"字串误报为真实会话泄漏。
    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(child) for child in value))
        return set()
    assert not (keys(public) & {"source_ref", "owner_id", "profile_id", "session", "session_digest",
                                "authorization", "actor_id", "evidence_id", "fingerprint",
                                "config_fingerprint", "ref_map"})
    assert evidence.actor_id not in serialized
    assert evidence.session_digest not in serialized
    assert evidence.valid_until - evidence.observed_at == timedelta(seconds=30)
    assert evidence.expired() is False
    assert unchanged(sql) == before


def test_current_profile_semantics_are_not_revision_inference(client, settings, alice, create, sql):
    trip = create()
    bound = bind(client, alice)
    assert sql("SELECT revision FROM payment_profiles") == [{"revision": bound["revision"]}]
    usable = capture(settings, alice, trip["id"])
    assert usable.public["payment_profile_state"] == "usable"
    revision = usable.public["state_versions"]["payment_profile"]
    sql("UPDATE payment_sources SET eligible=false WHERE ref='demo_personal_alice' RETURNING ref")
    unusable = capture(settings, alice, trip["id"])
    assert unusable.public["state_versions"]["payment_profile"] == revision
    assert unusable.public["payment_profile_state"] == "unusable"
    assert usable.fingerprint != unusable.fingerprint
    assert usable.config_fingerprint != unusable.config_fingerprint


def test_original_eligible_and_active_changes_invalidate_frozen_evidence(client, settings, alice, create, sql):
    trip, _, _, _ = ready(client, alice, create)
    before = capture(settings, alice, trip["id"])
    assert before.public["data"]["lock"]["valid"] is True
    sql("UPDATE flights SET active=false WHERE travel_date='2026-10-01' RETURNING id")
    after = capture(settings, alice, trip["id"])
    assert after.public["data"]["lock"] == before.public["data"]["lock"]
    assert after.public["flight_state"] == "unavailable"
    assert after.public["data"]["trip"]["revision"] == before.public["data"]["trip"]["revision"]
    assert after.fingerprint != before.fingerprint
    assert after.config_fingerprint != before.config_fingerprint
    sql("UPDATE flights SET active=true,fee_minor=fee_minor+1 WHERE travel_date='2026-10-01' RETURNING id")
    changed_same_public_state = capture(settings, alice, trip["id"])
    assert changed_same_public_state.public["data"] == before.public["data"]
    assert changed_same_public_state.public["flight_state"] == before.public["flight_state"]
    assert changed_same_public_state.fingerprint != before.fingerprint


def test_missing_segment_is_unavailable_not_zero_quote(settings, alice, create, sql, draft):
    trip = create(parameters={**draft, "flight_id": "UNLISTED-SYNTHETIC"})
    before = unchanged(sql)
    evidence = capture(settings, alice, trip["id"])
    assert evidence.public["flight_state"] == "unavailable"
    assert evidence.public["data"]["quote"] is None
    assert evidence.public["unknowns"] == ["current_quote_amount"]
    assert unchanged(sql) == before


def test_authorization_and_personal_scope_reject_without_business_effect(settings, alice, bob, login, create, sql):
    trip = create()
    before = unchanged(sql)
    assert_failure(lambda: capture(settings, bob, trip["id"]), 404, "not_visible")
    assert_failure(lambda: capture(settings, alice, "00000000-0000-0000-0000-000000000000"), 404, "not_visible")
    lin = login("approver_lin")
    assert_failure(lambda: capture(settings, lin, trip["id"]), 404, "not_visible")
    chen = login("employee_chen")
    own = create(key="employee-own", headers=chen)
    after_legal_creation = unchanged(sql)
    assert_failure(lambda: capture(settings, chen, own["id"]), 409, "guide_scope_unsupported")
    assert unchanged(sql) == after_legal_creation
    # 登录会话不属于23类业务状态；惟一差异是明确的合法创建。
    assert before["trips"] != after_legal_creation["trips"]


def test_auth_only_cancel_does_not_observe_or_read_knowledge(settings, alice, sql, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("cancel不应读取业务对象或知识")
    monkeypatch.setattr(projection, "owned_trip", forbidden)
    monkeypatch.setattr(projection, "load_knowledge", forbidden)
    before = unchanged(sql)
    identity = projection.auth_only(settings, request(alice), time.monotonic() + 30)
    assert tuple(identity) == ("personal_alice", projection.token_digest(projection.token_from(request(alice))))
    assert unchanged(sql) == before


def test_session_switch_and_revocation_cannot_reuse_evidence(settings, alice, bob, create, sql):
    trip = create()
    a = capture(settings, alice, trip["id"])
    assert_failure(lambda: capture(settings, bob, trip["id"]), 404, "not_visible")
    sql("UPDATE sessions SET revoked=true WHERE token_hash=%s RETURNING token_hash", (a.session_digest,))
    before = unchanged(sql)
    assert_failure(lambda: capture(settings, alice, trip["id"]), 401, "authentication_required")
    assert_failure(lambda: projection.auth_only(settings, request(alice), time.monotonic() + 30),
                   401, "authentication_required")
    assert unchanged(sql) == before


def test_same_version_knowledge_and_anchor_content_changes_invalidate(settings, alice, create, monkeypatch):
    trip = create()
    original = knowledge.load_knowledge()
    before = capture(settings, alice, trip["id"])
    changed = deepcopy(original.knowledge)
    changed["nodes"][0]["purpose"] += " 请确认目标行程。"
    updated = knowledge.validate_knowledge(changed, original.anchors)
    monkeypatch.setattr(projection, "load_knowledge", lambda: updated)
    after = capture(settings, alice, trip["id"])
    assert after.public["knowledge_version"] == before.public["knowledge_version"]
    assert after.public["data"] == before.public["data"]
    assert after.knowledge_fingerprint != before.knowledge_fingerprint
    assert after.fingerprint != before.fingerprint
    anchors = deepcopy(original.anchors)
    anchors["anchors"]["trip.open"]["path"] += " → 核对选中行程"
    assert knowledge.validate_knowledge(original.knowledge, anchors).fingerprint != original.fingerprint


@pytest.mark.parametrize("path", [
    ("credential",), ("data", "trip", "owner_id"), ("data", "quote", "source_ref"),
    ("state_versions", "authorization"), ("data", "ticket", "certificate"),
])
def test_extra_model_leaf_fields_are_rejected(client, settings, alice, create, path):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    task = process_payment(settings, payment)
    process_ticket(settings, task["ticket_task_id"])
    value = deepcopy(capture(settings, alice, trip["id"]).public)
    target = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = "hidden"
    with pytest.raises(ValidationError):
        projection.validate_public(value)


def test_unknown_states_remain_unknown_and_required_leaves_are_not_filled(settings, alice, create):
    public = deepcopy(capture(settings, alice, create()["id"]).public)
    public["payment_profile_state"], public["flight_state"] = "unknown", "unknown"
    public["unknowns"] += ["payment_profile_eligibility", "flight_availability"]
    validated = projection.validate_public(public)
    assert validated["payment_profile_state"] == "unknown"
    assert validated["flight_state"] == "unknown"
    del public["data"]["trip"]["passengers"]
    with pytest.raises(ValidationError):
        projection.validate_public(public)


def test_evidence_ttl_uses_real_time_and_monotonic_time(settings, alice, create, monkeypatch):
    evidence = capture(settings, alice, create()["id"])
    assert evidence.expired() is False
    monkeypatch.setattr(projection.time, "monotonic", lambda: evidence.expires_monotonic)
    assert evidence.expired() is True
    changed = deepcopy(evidence.public)
    changed["valid_until"] = (evidence.valid_until + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValidationError):
        projection.validate_public(changed)


@pytest.mark.parametrize("seconds", [3, .65])
def test_slow_pg_query_is_cancelled_and_connection_released(settings, sql, seconds):
    started = time.monotonic()
    pid = None
    with pytest.raises(projection.ObservationTimeout):
        with projection.bounded_snapshot(settings.database_url, started + seconds) as conn:
            pid = conn.info.backend_pid
            conn.execute("SELECT pg_sleep(10)")
    elapsed = time.monotonic() - started
    assert elapsed < seconds + .3
    assert pid is not None
    assert sql("SELECT pid FROM pg_stat_activity WHERE pid=%s", (pid,)) == []
    assert sql("SELECT count(*) AS n FROM pg_stat_activity WHERE application_name='llm-d3-observation'") == [{"n": 0}]


def test_stalled_connect_is_closed_within_remaining_deadline(settings):
    # 本机假PG只接受TCP并不回应启动握手，验证真实建连socket被关闭；无模型请求。
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    closed = threading.Event()
    failure = []

    def accept():
        try:
            with listener.accept()[0] as peer:
                peer.settimeout(2)
                while peer.recv(4096):
                    pass
                closed.set()
        except Exception as error:
            failure.append(type(error).__name__)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    configured = make_conninfo(settings.database_url, host="127.0.0.1", hostaddr="127.0.0.1",
                              port=listener.getsockname()[1])
    started = time.monotonic()
    try:
        with pytest.raises(projection.ObservationTimeout):
            with projection.bounded_snapshot(configured, started + .65):
                raise AssertionError("建连不应成功")
        assert time.monotonic() - started < .95
        thread.join(2)
        assert not thread.is_alive()
        assert closed.is_set() and not failure
    finally:
        listener.close()


@pytest.mark.parametrize("mutation", ["extra", "private_text", "unknown_anchor", "duplicate_node"])
def test_published_knowledge_is_strict_and_contains_no_task_answers(mutation):
    bundle = knowledge.load_knowledge()
    documents, anchors = deepcopy(bundle.knowledge), deepcopy(bundle.anchors)
    serialized = json.dumps(documents, ensure_ascii=False)
    assert not any(text in serialized for text in ("110000", "140000", "148000", "personal_alice",
                                                   "scenario_id", "SELECT", "source_ref"))
    if mutation == "extra":
        documents["nodes"][0]["hidden"] = "x"
    elif mutation == "private_text":
        documents["nodes"][0]["purpose"] = "读取源码与测试答案"
    elif mutation == "unknown_anchor":
        anchors["anchors"]["trip.open"]["id"] = "hidden.run"
    else:
        documents["nodes"][1] = documents["nodes"][0]
    with pytest.raises(ValueError):
        knowledge.validate_knowledge(documents, anchors)


def test_knowledge_duplicate_json_key_is_rejected(tmp_path, monkeypatch):
    bundle = knowledge.load_knowledge()
    (tmp_path / "personal-anchors-v1.json").write_text(json.dumps(bundle.anchors), encoding="utf-8")
    duplicate = json.dumps(bundle.knowledge).replace('"revision": 1', '"revision": 1, "revision": 2', 1)
    (tmp_path / "personal-v1.json").write_text(duplicate, encoding="utf-8")
    monkeypatch.setattr(knowledge, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="重复字段"):
        knowledge.load_knowledge()
