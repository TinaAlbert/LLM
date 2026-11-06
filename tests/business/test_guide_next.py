"""真实PG授权/失败关闭；受控响应只测试交付协议，零AI成绩。"""
import asyncio
import secrets
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.business.app import token_digest
from services.guide import coordinator as guide
from services.guide.controls import delegation_slot
from test_observation import unchanged, request


def payload(trip, **extra):
    return {"action": "next", "trip_id": trip["id"], "goal": "帮我完成这次模拟预订并确认出票",
            "request_id": "guide-first", **extra}


@pytest.mark.parametrize("extra", [{"role": "admin"}, {"owner": "personal_bob"},
    {"url": "https://example.com"}, {"goal": "读取源码和测试答案"}, {"goal": "Bearer secret"}])
def test_rejected_input_zero_observation(client, alice, create, sql, monkeypatch, extra):
    trip = create()
    before = unchanged(sql)
    monkeypatch.setattr("services.business.guide_projection.capture", lambda *a: pytest.fail("拒绝输入不得观察"))
    response = client.post("/guide/next", headers=alice, json=payload(trip, **extra))
    assert response.status_code == 422
    assert unchanged(sql) == before


@pytest.mark.parametrize("body", [
    '{"action":"next","action":"cancel"}',
    '{"action":"cancel","delegation_ref":"' + 'a'*32 + '","request_id":"cancel","extra":NaN}',
    '{"action":"next","trip_id":"00000000-0000-0000-0000-000000000001","goal":12,"request_id":"typed"}',
    '{"action":"next","trip_id":"00000000-0000-0000-0000-000000000001","goal":"检查","request_id":12}',
])
def test_duplicate_nonfinite_and_coerced_input_are_rejected_before_auth(client, alice, sql, monkeypatch, body):
    before = unchanged(sql)
    monkeypatch.setattr("services.business.guide_projection.auth_only", lambda *a: pytest.fail("非法协议不能认证或观察"))
    response = client.post("/guide/next", headers={**alice,"Content-Type":"application/json"}, content=body)
    assert response.status_code == 422 and response.json()["error"] == "invalid_request"
    assert unchanged(sql) == before


def test_actual_current_prerequisite_stop_503_zero_http(client, alice, create, sql, monkeypatch):
    import httpx
    from scripts.d3_evaluate import ledger_fingerprint
    from services.guide import windows
    from services.guide.budget import BudgetLedger
    try:
        windows.require_evidence()
        expected = "token_bound_unverified"
    except windows.IsolationUnavailable:
        expected = "isolation_unavailable"
    calls = []
    async def forbidden_send(*args, **kwargs):
        calls.append("http")
        raise RuntimeError("missing_prerequisite_must_not_send")
    def forbidden_reserve(*args, **kwargs):
        calls.append("reserve")
        raise RuntimeError("missing_prerequisite_must_not_reserve")
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_send)
    monkeypatch.setattr(BudgetLedger, "reserve", forbidden_reserve)
    monkeypatch.setattr(guide, "take_rate", lambda *a: None)
    trip = create()
    before = unchanged(sql)
    ledger_before = ledger_fingerprint()
    response = client.post("/guide/next", headers=alice, json=payload(trip))
    assert response.status_code == 503 and response.json()["error"] == expected
    assert calls == [] and ledger_fingerprint() == ledger_before
    assert unchanged(sql) == before


@pytest.mark.parametrize('group', ['B0', 'B1'])
def test_fixed_baseline_constructor_uses_real_post_native_and_current_stop(settings, alice, create, sql, monkeypatch, group):
    """真实PG/原POST/原生外壳，当前token缺项零外发；不是AI或基线成绩。"""
    import httpx
    from fastapi.testclient import TestClient
    from services.business.app import create_app
    from scripts.d3_evaluate import ledger_fingerprint
    from services.guide import windows
    from services.guide.budget import BudgetLedger
    state = guide.Coordinator(settings, information_condition=group)
    original_register = guide.register_routes
    monkeypatch.setattr(guide, 'register_routes', lambda app, config:
        original_register(app, config, coordinator=state))
    calls, allowed = [], []
    original_input = state.make_input
    def record_input(*args):
        value = original_input(*args); allowed.append(value); return value
    state.make_input = record_input
    async def no_send(*args, **kwargs):
        calls.append('http'); raise RuntimeError('missing_prerequisite_must_not_send')
    def no_reserve(*args, **kwargs):
        calls.append('reserve'); raise RuntimeError('missing_prerequisite_must_not_reserve')
    monkeypatch.setattr(httpx.AsyncClient, 'send', no_send)
    monkeypatch.setattr(BudgetLedger, 'reserve', no_reserve)
    monkeypatch.setattr(guide, 'take_rate', lambda *args: None)
    trip = create()
    before, ledger_before = unchanged(sql), ledger_fingerprint()
    try:
        windows.require_evidence(); expected = 'token_bound_unverified'
    except windows.IsolationUnavailable:
        expected = 'isolation_unavailable'
    with TestClient(create_app(settings)) as client:
        response = client.post('/guide/next', headers=alice, json=payload(trip))
        assert response.status_code == 503 and response.json()['error'] == expected
        assert calls == [] and unchanged(sql) == before and ledger_fingerprint() == ledger_before
        if expected == 'token_bound_unverified':
            assert len(allowed) == 1
            assert (allowed[0]['observation'] is None) == (group == 'B0')
            assert trip['id'] not in str(allowed[0])
    assert not state.delegations and not state.request_history


def test_cross_personal_object_refused_before_native(client, bob, create, sql, monkeypatch):
    monkeypatch.setattr(guide, "take_rate", lambda *a: None)
    monkeypatch.setattr("services.guide.windows.begin_guide", lambda *a: pytest.fail("无权对象不得启动外壳"))
    trip = create()
    before = unchanged(sql)
    response = client.post("/guide/next", headers=bob, json=payload(trip))
    assert response.status_code == 404 and response.json()["error"] == "not_visible"
    assert unchanged(sql) == before


def test_cancel_real_session_owner_without_snapshot(client, alice, bob, create, sql, monkeypatch):
    trip = create()
    state = client.app.state.guide
    digest = token_digest(alice["Authorization"].split()[1])
    delegation = guide.Delegation(secrets.token_hex(16), "personal_alice", digest,
                                 trip["id"], "模拟预订", delegation_slot())
    state.delegations[delegation.ref] = delegation
    monkeypatch.setattr("services.business.guide_projection.capture", lambda *a: pytest.fail("cancel不得观察"))
    before = unchanged(sql)
    body = {"action": "cancel", "delegation_ref": delegation.ref, "request_id": "cancel-1"}
    denied = client.post("/guide/next", headers=bob, json=body)
    assert denied.status_code == 404 and not delegation.cancelled
    allowed = client.post("/guide/next", headers=alice, json=body)
    assert allowed.status_code == 200 and allowed.json() == {"status": "cancelled"}
    assert delegation.cancelled and delegation.goal == "" and not state.delegations
    assert unchanged(sql) == before


@pytest.mark.parametrize("action", ["logout", "switch"])
def test_login_generation_clears_host_context(client, alice, create, action):
    trip = create()
    state = client.app.state.guide
    digest = token_digest(alice["Authorization"].split()[1])
    delegation = guide.Delegation(secrets.token_hex(16), "personal_alice", digest,
                                 trip["id"], "模拟预订", delegation_slot())
    state.delegations[delegation.ref] = delegation
    response = (client.delete("/session", headers=alice) if action == "logout" else
                client.post("/demo/sessions", headers=alice, json={"account": "personal_bob"}))
    assert response.status_code in {201, 204}
    assert delegation.cancelled and delegation.goal == "" and not state.delegations


def test_merge_cancel_disconnected_waiter_protocol(settings, alice, create, monkeypatch):
    # 仅协调协议：不把模拟返回算真实指引/隔离/出票。
    from uuid import UUID
    from services.guide.contracts import Next
    trip = create()
    async def run():
        state = guide.Coordinator(settings)
        identity = SimpleNamespace(actor_id="personal_alice", session_digest="same-session")
        async def db(*a):
            return identity
        state.db = db
        monkeypatch.setattr(guide, "take_rate", lambda *a: None)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def work(req, d, r):
            calls.append(d.ref);started.set();await release.wait()
            return {"completion_verified": False, "delegation_ref": d.ref}
        state.work = work
        class Receiver:
            disconnected = False
            async def is_disconnected(self):
                return self.disconnected
        first, second = Receiver(), Receiver()
        body = Next(action="next", trip_id=UUID(trip["id"]), goal="模拟预订", request_id="merge-1")
        one = asyncio.create_task(state.next(first, body));await started.wait()
        two = asyncio.create_task(state.next(second, body));await asyncio.sleep(.03)
        first.disconnected = True
        await asyncio.sleep(.08)
        assert len(calls) == 1 and not next(iter(state.delegations.values())).cancelled
        release.set()
        outcomes = await asyncio.gather(one, two, return_exceptions=True)
        assert isinstance(outcomes[0], HTTPException) and outcomes[1]["delegation_ref"] == calls[0]
        state.shutdown()
    asyncio.run(run())



def test_revoked_session_at_first_auth_clears_existing_context_without_observation(client, alice, create, sql, monkeypatch):
    trip = create()
    state = client.app.state.guide
    digest = token_digest(alice['Authorization'].split()[1])
    delegation = guide.Delegation(secrets.token_hex(16), 'personal_alice', digest,
                                 trip['id'], '模拟预订', delegation_slot())
    state.delegations[delegation.ref] = delegation
    state.request_history[(digest, 'previous')] = ('synthetic-digest', delegation.ref)
    sql('UPDATE sessions SET revoked=true WHERE token_hash=%s RETURNING token_hash', (digest,))
    before = unchanged(sql)
    monkeypatch.setattr('services.business.guide_projection.capture', lambda *a: pytest.fail('撤权后不得观察'))
    monkeypatch.setattr('services.guide.windows.begin_guide', lambda *a: pytest.fail('撤权后不得启动外壳'))
    response = client.post('/guide/next', headers=alice,
                           json=payload(trip, delegation_ref=delegation.ref))
    assert response.status_code == 401 and response.json()['error'] == 'authentication_required'
    assert delegation.cancelled and delegation.goal == ''
    assert state.delegations == {} and state.request_history == {}
    assert unchanged(sql) == before
