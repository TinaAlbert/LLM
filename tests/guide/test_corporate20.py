"""20 企业语义：独立 55433 合成库、真实 HTTP/SQL 与离线模型替身。

不加载 tests/business/conftest.py。任何重置前同时核验实例目录、端口、
数据库和工作空间标记，原 55432 dev/test 不属于这些用例的写入范围。
"""

from decimal import Decimal
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
import pytest
from starlette.requests import Request

from services.business.app import create_app
from services.business.config import Settings
from services.business.db import connect
from services.business.manage import reset_test_database
from services.business import worker
from services.guide.assistant import current_state, display_state, identity, validate_reply
from services.guide.model import ModelResult, TokenUsage
from services.guide.releases import Releases


ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / ".local/d3-20/sandbox"
TABLES = (
    "users", "organizations", "departments", "memberships", "approval_assignments",
    "payment_sources", "payment_profiles", "trips", "quotes", "booking_locks", "payments",
    "payment_jobs", "provider_payments", "ticket_tasks", "tickets", "flights",
    "budget_projects", "trip_budget_selections", "approval_requests", "business_events",
    "idempotency", "coupons", "workspaces",
)


@pytest.fixture
def sandbox_settings():
    """仅接受本轮脚本产生的隔离实例，绝不回退项目默认连接。"""
    config_path = SANDBOX / "business.json"
    assert config_path.is_file(), "先运行 scripts/round20_sandbox.py 准备独立 55433 实例"
    from scripts import round20_sandbox as runner
    runner.bind_helper()
    runner.check_runner()
    configured = runner.checked_config()
    runner.check_business(configured)  # 管理连接独立核验实际data_directory，不提升业务角色权限。
    url = configured["test_database_url"]
    params = conninfo_to_dict(url)
    assert params.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert params.get("port") == "55433"
    assert params.get("dbname") == "llm_business_test"
    with connect(url) as conn:
        actual = conn.execute("SELECT current_database() AS name, current_setting('port') AS port").fetchone()
        assert actual["name"] == "llm_business_test"
        assert actual["port"] == "55433"
        assert conn.execute("SELECT project,purpose FROM project_meta").fetchall() == [
            {"project": "llm-business-d1", "purpose": "test"}]
        assert conn.execute("SELECT id FROM workspaces").fetchall() == [{"id": "test"}]
    reset_test_database(url, confirmation="llm_business_test")
    return Settings(database_url=url, workspace_id="test", demo_login=True, mode="test")


@pytest.fixture
def client20(sandbox_settings):
    with TestClient(create_app(sandbox_settings)) as client:
        yield client


@pytest.fixture
def sql20(sandbox_settings):
    def query(statement, parameters=()):
        with connect(sandbox_settings.database_url) as conn:
            return conn.execute(statement, parameters).fetchall()
    return query


def snapshot(sql):
    expressions = [f"'{name}',(SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {name} t)"
                   for name in TABLES]
    return sql("SELECT jsonb_build_object(" + ",".join(expressions) + ") AS state")[0]["state"]


def login(client, account):
    result = client.post("/demo/sessions", json={"account": account})
    assert result.status_code == 201
    return {"Authorization": "Bearer " + result.json()["access_token"]}


def write(client, headers, path, body=None, method="post"):
    response = getattr(client, method)(path, headers={**headers, "Idempotency-Key": str(uuid4())},
                                       json={} if body is None else body)
    assert response.status_code in {200, 201, 202}, response.text
    return response.json()


def ready(client, *, stage="approved"):
    headers = login(client, "employee_chen")
    trip = write(client, headers, "/trips", {
        "flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 1})
    write(client, headers, "/payment-profile", {
        "source_ref": "demo_corporate_chen", "expected_revision": 0}, method="put")
    quote = write(client, headers, f"/trips/{trip['id']}/quotes", {"expected_revision": 1})
    booking = write(client, headers, f"/quotes/{quote['id']}/lock")
    ctx = {"headers": headers, "trip": trip, "quote": quote, "lock": booking}
    if stage == "locked":
        return ctx
    write(client, headers, f"/trips/{trip['id']}/budget", {
        "project_id": "A-eng", "expected_revision": 1, "expected_selection_revision": 0}, method="put")
    application = write(client, headers, f"/locks/{booking['id']}/approvals", {"expected_revision": 1})
    ctx["application"] = application
    if stage == "pending":
        return ctx
    ctx["approver"] = login(client, "approver_lin")
    ctx["approved"] = write(client, ctx["approver"], f"/approvals/{application['id']}/decision", {
        "expected_revision": 1, "decision": "approved"})
    return ctx


def request(headers):
    return Request({"type": "http", "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()]})


def capture(settings, headers, trip_id=None):
    source = request(headers)
    deadline = time.monotonic() + 5
    who = identity(settings, source, deadline)
    return current_state(settings, source, who, trip_id, deadline)


def assert_anonymous(value):
    serialized = json.dumps(value, ensure_ascii=False)
    for text in ("employee_chen", "approver_lin", "org-A", "source_ref", "profile_id", "certificate",
                 "budget_total_minor", "total_minor", "reserved_minor", "used_minor", "project_id"):
        assert text not in serialized
    import re
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", serialized)


def test_employee_pending_facts_and_self_approval_denial(client20, sandbox_settings, sql20):
    ctx = ready(client20, stage="pending")
    before = snapshot(sql20)
    state, _ = capture(sandbox_settings, ctx["headers"], ctx["trip"]["id"])
    assert state["data"]["trip"]["revision"] == 1
    assert state["data"]["approval"]["effective_status"] == "pending"
    assert state["data"]["approval"]["valid"] is False
    assert state["data"]["approval"]["applicability"] == "not_approved"
    assert state["data"]["budget_selection"] == {"status": "selected", "revision": 1}
    assert state["approval_handoff"] == "assigned_department_approver"
    assert "approver_name" in state["unknowns"]
    assert state["data"]["payment"] is None
    assert state["data"]["ticket"] is None
    assert_anonymous(state)
    # Chen 同时有 employee/approver 角色，仍不能自批。
    denied = client20.post(f"/approvals/{ctx['application']['id']}/decision",
        headers={**ctx["headers"], "Idempotency-Key": "employee-self-approval"},
        json={"expected_revision": 1, "decision": "approved"})
    assert denied.status_code == 404 and denied.json() == {"error": "not_visible"}
    assert snapshot(sql20) == before


def test_changed_trip_invalidates_approved_snapshot(client20, sandbox_settings, sql20):
    ctx = ready(client20)
    before_state, before_hash = capture(sandbox_settings, ctx["headers"], ctx["trip"]["id"])
    assert before_state["data"]["approval"]["valid"] is True
    write(client20, ctx["headers"], f"/trips/{ctx['trip']['id']}", {
        "expected_revision": 1, "passengers": 2}, method="patch")
    before = snapshot(sql20)
    after, after_hash = capture(sandbox_settings, ctx["headers"], ctx["trip"]["id"])
    assert after["data"]["trip"]["revision"] == 2
    assert after["data"]["approval"]["trip_revision"] == 1
    assert after["data"]["approval"]["effective_status"] == "invalidated"
    assert after["data"]["approval"]["valid"] is False
    assert after["data"]["approval"]["applicability"] == "stale"
    assert after_hash != before_hash
    assert sql20("SELECT status,resource_status FROM approval_requests") == [
        {"status": "invalidated", "resource_status": "released"}]
    denied = client20.post(f"/locks/{ctx['lock']['id']}/payments",
        headers={**ctx["headers"], "Idempotency-Key": "stale-approved-payment"}, json={"amount_minor": 100000})
    assert denied.status_code == 409
    assert snapshot(sql20) == before


def test_paid_pending_ticket_keeps_one_payment_and_used_budget(client20, sandbox_settings, sql20):
    ctx = ready(client20)
    payment = write(client20, ctx["headers"], f"/locks/{ctx['lock']['id']}/payments", {"amount_minor": 100000})
    worker.process_payment(sandbox_settings.database_url, "test", payment["id"], outcome="succeeded")
    before = snapshot(sql20)
    state, _ = capture(sandbox_settings, ctx["headers"], ctx["trip"]["id"])
    assert state["data"]["payment"]["status"] == "succeeded"
    assert state["data"]["payment"]["amount_minor"] == 100000
    assert state["data"]["ticket_task"]["status"] == "pending"
    assert state["data"]["ticket"] is None
    assert state["data"]["approval"]["applicability"] == "used_for_payment"
    assert sql20("SELECT status,amount_minor FROM payments") == [{"status": "succeeded", "amount_minor": 100000}]
    assert sql20("SELECT status FROM ticket_tasks") == [{"status": "pending"}]
    assert sql20("SELECT count(*) AS n FROM tickets") == [{"n": 0}]
    assert sql20("SELECT reserved_minor,used_minor FROM budget_projects WHERE id='A-eng'") == [
        {"reserved_minor": 0, "used_minor": 100000}]
    denied = client20.post(f"/locks/{ctx['lock']['id']}/payments",
        headers={**ctx["headers"], "Idempotency-Key": "duplicate-corporate-payment"}, json={"amount_minor": 100000})
    assert denied.status_code == 409
    assert snapshot(sql20) == before


def test_assigned_summary_filters_same_department_unassigned_and_other_organizations(
    client20, sandbox_settings, sql20,
):
    ctx = ready(client20, stage="pending")
    lin = login(client20, "approver_lin")
    alternatives = {account: login(client20, account) for account in
                    ("approver_qian", "approver_wu", "approver_zhao")}
    before = snapshot(sql20)
    state, _ = capture(sandbox_settings, lin)
    assert len(state["assigned_approvals"]) == 1
    row = state["assigned_approvals"][0]
    assert row == {"ref": "assigned_1", "revision": 1, "trip_revision": 1,
                   "current_trip_revision": 1, "flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01",
                   "passengers": 1, "amount_minor": 100000, "currency": "DEMO",
                   "effective_status": "pending", "valid": False}
    assert state["assigned_list_truncated"] is False
    assert "applicant_payment_and_ticket_state" in state["unknowns"]
    assert "data" not in state
    assert_anonymous(state)
    presented = display_state(state, "assistant-5")
    assert presented["assigned_approvals"][0]["amount_display"] == "1000.00 模拟元"
    assert "amount_minor" not in presented["assigned_approvals"][0]
    assert validate_reply("本次指派申请金额为1000.00 模拟元。", state, prompt_version="assistant-5")
    for headers in (lin, *alternatives.values()):
        with pytest.raises(HTTPException) as error:
            capture(sandbox_settings, headers, ctx["trip"]["id"])
        assert (error.value.status_code, error.value.detail) == (404, "not_visible")
    for headers in alternatives.values():
        other, _ = capture(sandbox_settings, headers)
        assert other["assigned_approvals"] == []
        assert other["assigned_list_truncated"] is False
    assert snapshot(sql20) == before


@pytest.mark.parametrize("change", ["assignment", "project", "payment_source"])
def test_live_configuration_change_invalidates_applicability_and_private_hash(
    change, client20, sandbox_settings, sql20,
):
    ctx = ready(client20)
    original, old_hash = capture(sandbox_settings, ctx["headers"], ctx["trip"]["id"])
    assert original["data"]["approval"]["applicability"] == "current"
    statements = {
        "assignment": "UPDATE approval_assignments SET active=false WHERE applicant_id='employee_chen' RETURNING applicant_id",
        "project": "UPDATE budget_projects SET active=false WHERE id='A-eng' RETURNING id",
        "payment_source": "UPDATE payment_sources SET eligible=false WHERE ref='demo_corporate_chen' RETURNING ref",
    }
    assert sql20(statements[change])
    before = snapshot(sql20)
    fresh, new_hash = capture(sandbox_settings, ctx["headers"], ctx["trip"]["id"])
    # 批准记录本身没被读取动作改写；当前适用性必须反映原业务校验。
    assert fresh["data"]["approval"]["valid"] is True
    assert fresh["data"]["approval"]["applicability"] == "unavailable"
    assert new_hash != old_hash
    if change == "assignment":
        assert fresh["approval_handoff"] == "unavailable"
    if change == "project":
        assert fresh["data"]["budget_selection"]["status"] == "unavailable"
    assert_anonymous(fresh)
    assert snapshot(sql20) == before


def install_offline_adapter(client, tmp_path):
    """走真实 Assistant/HTTP；只替换付费推理运输，不替换观察、身份或业务。"""
    records = SimpleNamespace(calls=[], after=None)
    assistant = client.app.state.assistant
    assistant.releases = Releases("test", tmp_path / "releases.json")
    assistant.report_dir = tmp_path / "runs"
    # 与构造时传入离线 adapter_factory 相同；不制造任何真实外发批准。
    assistant.external_model = False
    def factory(validator):
        async def generate(value, deadline):
            validator(value)
            records.calls.append(value)
            if records.after:
                records.after()
            return ModelResult(
                {"content": "审批和业务状态以本轮允许的观察为准，请本人在网页继续处理。", "tool_calls": []},
                TokenUsage(100, 20, 120, 0, 100), "offline-round20", Decimal("0"), "offline-round20")
        return SimpleNamespace(generate_experiment=generate)
    assistant.adapter_factory = factory
    return records


def chat_payload(trip_id, session_id=None):
    result = {"message": "当前申请卡在哪里？已有批准是否仍适用？", "page": "trips", "trip_id": trip_id,
              "observation": {"page": "trips", "title": "我的行程", "items": ["公开功能说明"], "product_version": "v1"}}
    if session_id:
        result["session_id"] = session_id
    return result


@pytest.mark.parametrize("change", ["session", "membership", "assignment"])
def test_late_offline_candidate_rechecks_actual_authorization(
    change, client20, sandbox_settings, sql20, tmp_path,
):
    ctx = ready(client20, stage="pending")
    records = install_offline_adapter(client20, tmp_path)
    if change == "session":
        records.after = lambda: sql20("UPDATE sessions SET revoked=true WHERE user_id='employee_chen' RETURNING user_id")
    elif change == "membership":
        records.after = lambda: sql20("UPDATE memberships SET active=false,revision=revision+1 WHERE user_id='employee_chen' RETURNING user_id")
    else:
        records.after = lambda: sql20("UPDATE approval_assignments SET active=false WHERE applicant_id='employee_chen' RETURNING applicant_id")
    response = client20.post("/assistant/chat", headers=ctx["headers"], json=chat_payload(ctx["trip"]["id"]))
    assert response.status_code in {401, 404, 409}
    assert len(records.calls) == 1
    assert "reply" not in response.json()
    assert all(not session.history for session in client20.app.state.assistant.sessions.values())


def test_same_conversation_cannot_move_to_another_identity(client20, sandbox_settings, sql20, tmp_path):
    ctx = ready(client20, stage="pending")
    records = install_offline_adapter(client20, tmp_path)
    first = client20.post("/assistant/chat", headers=ctx["headers"], json=chat_payload(ctx["trip"]["id"]))
    assert first.status_code == 200, first.text
    other = login(client20, "approver_qian")
    before = snapshot(sql20)
    response = client20.post("/assistant/chat", headers=other,
                            json={**chat_payload(None, first.json()["session_id"]), "page": "approvals",
                                  "observation": {"page": "approvals", "title": "被指派的申请摘要",
                                                  "items": ["公开功能说明"], "product_version": "v1"}})
    assert response.status_code == 409 and response.json() == {"error": "session_changed"}
    assert len(records.calls) == 1
    assert snapshot(sql20) == before


def test_ordinary_employee_cannot_approve_or_read_another_trip(client20, sandbox_settings, sql20):
    ctx = ready(client20, stage='pending')
    ding = login(client20, 'employee_ding')
    before = snapshot(sql20)
    denied = client20.post(f"/approvals/{ctx['application']['id']}/decision",
        headers={**ding, 'Idempotency-Key': 'ordinary-employee-decision'},
        json={'expected_revision': 1, 'decision': 'approved'})
    assert denied.status_code == 404 and denied.json() == {'error': 'not_visible'}
    for identifier in (ctx['trip']['id'], str(uuid4())):
        with pytest.raises(HTTPException) as error:
            capture(sandbox_settings, ding, identifier)
        assert (error.value.status_code, error.value.detail) == (404, 'not_visible')
    assert snapshot(sql20) == before


@pytest.mark.parametrize('change', ['source_owner', 'budget_total', 'flight_fee'])
def test_private_configuration_change_without_public_change_invalidates_hash(
    change, client20, sandbox_settings, sql20,
):
    ctx = ready(client20, stage='pending')
    original, old_hash = capture(sandbox_settings, ctx['headers'], ctx['trip']['id'])
    statements = {
        'source_owner': "UPDATE payment_sources SET owner_id='employee_ding' WHERE ref='demo_corporate_chen' RETURNING ref",
        'budget_total': "UPDATE budget_projects SET total_minor=total_minor+1 WHERE id='A-eng' RETURNING id",
        'flight_fee': "UPDATE flights SET fee_minor=fee_minor+1 WHERE travel_date='2026-10-01' RETURNING id",
    }
    assert sql20(statements[change])
    before = snapshot(sql20)
    fresh, new_hash = capture(sandbox_settings, ctx['headers'], ctx['trip']['id'])
    for value in (original, fresh):
        value.pop('observed_at')
        value.pop('valid_until')
    assert fresh == original
    assert new_hash != old_hash
    assert snapshot(sql20) == before


def test_same_identity_revised_trip_clears_previous_fact_history(client20, sandbox_settings, sql20, tmp_path):
    ctx = ready(client20, stage='pending')
    records = install_offline_adapter(client20, tmp_path)
    first = client20.post('/assistant/chat', headers=ctx['headers'], json=chat_payload(ctx['trip']['id']))
    assert first.status_code == 200
    sid = first.json()['session_id']
    assert client20.app.state.assistant.sessions[sid].history
    write(client20, ctx['headers'], f"/trips/{ctx['trip']['id']}",
          {'expected_revision': 1, 'travel_date': '2026-10-02'}, method='patch')
    before = snapshot(sql20)
    second = client20.post('/assistant/chat', headers=ctx['headers'], json=chat_payload(ctx['trip']['id'], sid))
    assert second.status_code == 200
    sent = json.loads(records.calls[-1]['messages'][-1]['content'])
    assert sent['history'] == []
    assert sent['current_state']['data']['trip']['revision'] == 2
    assert snapshot(sql20) == before
