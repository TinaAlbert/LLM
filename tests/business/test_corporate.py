"""D1-03 企业 V1：真实 PG 的独立终态、权限反例、原子性与资源竞争。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, get_ident
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.business import app as app_module
from services.business import corporate
from services.business.db import connect
from test_personal import (
    advance, assert_error, bind, expire, fail_business_events, inventory, lock,
    pay, process_payment, process_ticket, quote, write,
)

pytestmark = pytest.mark.integration

TABLES = (
    "trips", "payment_profiles", "quotes", "booking_locks", "payments",
    "payment_jobs", "provider_payments", "ticket_tasks", "tickets", "flights",
    "budget_projects", "trip_budget_selections", "approval_requests",
    "business_events", "idempotency",
)


def snapshot(sql):
    # 固定表清单；拒绝前后检查实际 PG 持久行，不用业务成功判定函数。
    fragments = [
        f"'{name}', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]'::jsonb) FROM {name} t)"
        for name in TABLES
    ]
    return sql("SELECT jsonb_build_object(" + ",".join(fragments) + ") AS state")[0]["state"]


def budget(sql, *, reserved=0, used=0, total=200000, policy_revision=1, project="A-eng"):
    assert sql(
        "SELECT total_minor,reserved_minor,used_minor,policy_revision,"
        "total_minor-reserved_minor-used_minor AS available_minor "
        "FROM budget_projects WHERE workspace_id='test' AND id=%s", (project,),
    ) == [{"total_minor": total, "reserved_minor": reserved, "used_minor": used,
           "policy_revision": policy_revision, "available_minor": total - reserved - used}]
    assert 0 <= reserved + used <= total


def request(client, headers, path, body=None, *, key=None, method="post"):
    return getattr(client, method)(path, headers={**headers, "Idempotency-Key": key or str(uuid4())},
                                   json={} if body is None else body)


def select(client, headers, trip, project="A-eng", revision=0, *, key="select"):
    return write(client, headers, f"/trips/{trip['id']}/budget", {
        "project_id": project, "expected_revision": trip["revision"],
        "expected_selection_revision": revision,
    }, key=key, method="put")


def submit(client, headers, booking, *, key="submit"):
    return write(client, headers, f"/locks/{booking['id']}/approvals", {
        "expected_revision": booking["trip_revision"],
    }, key=key)


def decide(client, headers, application, decision="approved", *, key="decision"):
    return write(client, headers, f"/approvals/{application['id']}/decision", {
        "expected_revision": application["revision"], "decision": decision,
    }, key=key)


def ready(client, login, *, account="employee_chen", stage="approved"):
    headers = login(account)
    trip = write(client, headers, "/trips", {
        "flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 1,
    }, key="create")
    profile = bind(client, headers, source="demo_corporate_" + account.removeprefix("employee_"))
    quotation = quote(client, headers, trip)
    booking = lock(client, headers, quotation)
    context = {"headers": headers, "trip": trip, "profile": profile,
               "quote": quotation, "lock": booking}
    if stage == "locked":
        return context
    context["selection"] = select(client, headers, trip)
    if stage == "selected":
        return context
    context["application"] = submit(client, headers, booking)
    if stage == "pending":
        return context
    context["approver"] = login("approver_lin")
    context["approved"] = decide(client, context["approver"], context["application"])
    return context


def test_c02_ticket_payment_approval_and_budget_have_independent_terminal_state(client, login, sql, settings):
    ctx = ready(client, login, stage="locked")
    headers, trip, booking = ctx["headers"], ctx["trip"], ctx["lock"]
    assert trip["organization_id"] == "org-A"
    assert ctx["quote"]["amount_minor"] == booking["amount_minor"] == 100000
    assert booking["currency"] == "DEMO"
    assert booking["trip_revision"] == 1
    inventory(sql, reserved=1, sold=0)
    budget(sql)
    selection = select(client, headers, trip)
    assert selection["project_id"] == "A-eng"
    budget(sql)
    assert client.get(f"/locks/{booking['id']}", headers=headers).json()["valid"] is True
    application = submit(client, headers, booking)
    assert application["lock_id"] == booking["id"]
    assert application["trip_revision"] == application["selection_revision"] == application["policy_revision"] == 1
    assert application["budget_total_minor"] == 200000
    assert application["amount_minor"] == 100000 and application["status"] == "pending"
    budget(sql, reserved=100000)
    approved = decide(client, login("approver_lin"), application)
    assert approved["revision"] == 2 and approved["status"] == "approved"
    payment = pay(client, headers, booking, amount=100000)
    assert sql("SELECT status,resource_status FROM approval_requests") == [
        {"status": "approved", "resource_status": "protected"}]
    budget(sql, reserved=100000)
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 0}]
    process_payment(settings, payment)
    budget(sql, used=100000)
    paid = client.get(f"/payments/{payment['id']}", headers=headers).json()
    assert paid["status"] == "succeeded"
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 0}]
    process_ticket(settings, paid["ticket_task_id"])
    assert sql("""SELECT t.trip_revision,t.quantity,length(t.certificate)>0 AS certified,
        p.amount_minor,p.status,a.status AS approval_status,a.resource_status,
        a.trip_revision=p_lock.trip_revision AS same_revision,a.lock_id=p.lock_id AS same_lock
        FROM tickets t JOIN payments p ON (p.workspace_id,p.id)=(t.workspace_id,t.payment_id)
        JOIN booking_locks p_lock ON (p_lock.workspace_id,p_lock.id)=(p.workspace_id,p.lock_id)
        JOIN approval_requests a ON (a.workspace_id,a.id)=(p.workspace_id,p.approval_id)""") == [
        {"trip_revision": 1, "quantity": 1, "certified": True, "amount_minor": 100000,
         "status": "succeeded", "approval_status": "approved", "resource_status": "consumed",
         "same_revision": True, "same_lock": True}]
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [
        {"n": 1, "captured": 100000}]
    for table in ("payments", "payment_jobs", "ticket_tasks", "tickets"):
        assert sql(f"SELECT count(*) AS n FROM {table}") == [{"n": 1}]
    inventory(sql, reserved=0, sold=1)
    approver = login("approver_lin")
    task = client.get(f"/ticket-tasks/{paid['ticket_task_id']}", headers=headers).json()
    for resource, identifier in (("payments", payment["id"]), ("ticket-tasks", paid["ticket_task_id"]),
                                 ("tickets", task["ticket_id"])):
        assert_error(client.get(f"/{resource}/{identifier}", headers=approver), 404, "not_visible")
    before = snapshot(sql)
    process_payment(settings, payment)
    process_ticket(settings, paid["ticket_task_id"])
    assert snapshot(sql) == before


def test_permissions_hide_unassigned_unknown_and_linked_private_objects_without_changes(client, login, sql):
    ctx = ready(client, login, stage="pending")
    approver = login("approver_lin")
    response = client.get(f"/approvals/{ctx['application']['id']}", headers=approver)
    assert response.status_code == 200
    summary = response.json()
    assert summary["amount_minor"] == 100000 and summary["trip_id"] == ctx["trip"]["id"]
    assert set(summary) == {
        "id", "trip_id", "trip_revision", "lock_id", "project_id", "selection_revision",
        "policy_revision", "budget_total_minor", "amount_minor", "currency", "revision",
        "status", "resource_status", "applicant_name", "flight_id", "travel_date", "passengers",
        "effective_status", "valid",
    }
    forbidden = {"profile_id", "profile_revision", "source_ref", "payment_id", "payment_source", "provider_operation_id"}
    assert forbidden.isdisjoint(summary)
    assert "demo_corporate" not in response.text
    todo = client.get("/approvals", headers=approver)
    assert todo.status_code == 200 and ctx["application"]["id"] in todo.text
    denied = [login(account) for account in (
        "employee_ding", "employee_fang", "approver_wu", "approver_zhao", "approver_qian", "personal_bob",
    )]
    before = snapshot(sql)
    for headers in denied:
        for identifier in (ctx["application"]["id"], str(uuid4()), "invalid-uuid"):
            spoofed = {**headers, "X-Role": "approver", "X-User-ID": "approver_lin", "X-Organization-ID": "org-A"}
            assert_error(client.get(f"/approvals/{identifier}?role=approver&owner_id=approver_lin", headers=spoofed), 404, "not_visible")
            assert_error(request(client, spoofed, f"/approvals/{identifier}/decision",
                                 {"expected_revision": 1, "decision": "approved"}), 404, "not_visible")
    # 兼任员工/审批角色仍不能自批；申请人可以读自己的摘要。
    assert_error(request(client, ctx["headers"], f"/approvals/{ctx['application']['id']}/decision",
                         {"expected_revision": 1, "decision": "approved"}), 404, "not_visible")
    for resource, identifier in (("trips", ctx["trip"]["id"]), ("quotes", ctx["quote"]["id"]),
                                 ("locks", ctx["lock"]["id"])):
        assert_error(client.get(f"/{resource}/{identifier}", headers=approver), 404, "not_visible")
    assert snapshot(sql) == before


def test_budget_visibility_and_source_ownership_use_server_membership(client, login, sql):
    chen, fang = login("employee_chen"), login("employee_fang")
    own = client.get("/budget-projects", headers=chen)
    assert own.status_code == 200 and '"A-eng"' in own.text
    assert '"A-sales"' not in own.text and '"B-eng"' not in own.text
    other = client.get("/budget-projects", headers=fang)
    assert other.status_code == 200 and '"B-eng"' in other.text and '"A-eng"' not in other.text
    before = snapshot(sql)
    for source in ("demo_corporate_fang", "demo_corporate_ding", "unknown"):
        assert_error(request(client, chen, "/payment-profile", {"source_ref": source, "expected_revision": 0}, method="put"),
                     404, "not_visible")
    assert snapshot(sql) == before


def test_v1_quote_and_selection_approval_prerequisites_write_nothing(client, login, sql):
    headers = login("employee_chen")
    trip = write(client, headers, "/trips", {"flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 1})
    bind(client, headers, source="demo_corporate_chen")
    before = snapshot(sql)
    response = request(client, headers, f"/trips/{trip['id']}/budget",
                       {"project_id": "A-eng", "expected_revision": 1, "expected_selection_revision": 0}, method="put")
    assert response.status_code == 409
    assert snapshot(sql) == before
    quotation = quote(client, headers, trip)
    booking = lock(client, headers, quotation)
    before = snapshot(sql)
    assert request(client, headers, f"/locks/{booking['id']}/approvals", {"expected_revision": 1}).status_code == 409
    assert request(client, headers, f"/locks/{booking['id']}/payments", {"amount_minor": 100000}).status_code == 409
    assert snapshot(sql) == before
    select(client, headers, trip)
    submit(client, headers, booking)
    before = snapshot(sql)
    assert request(client, headers, f"/locks/{booking['id']}/payments", {"amount_minor": 100000}).status_code == 409
    assert snapshot(sql) == before


@pytest.mark.parametrize("assignee", ["employee_chen", "approver_wu", "approver_zhao"])
def test_explicit_assignment_does_not_override_nonself_department_or_organization(assignee, client, login, sql):
    ctx = ready(client, login, stage="selected")
    sql("UPDATE approval_assignments SET approver_id=%s WHERE applicant_id='employee_chen' RETURNING applicant_id", (assignee,))
    before = snapshot(sql)
    response = request(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/approvals", {"expected_revision": 1})
    assert_error(response, 404, "not_visible")
    assert snapshot(sql) == before
    budget(sql)
    inventory(sql, reserved=1, sold=0)


def test_insufficient_budget_leaves_no_application_or_partial_reservation(client, login, sql):
    ctx = ready(client, login, stage="selected")
    sql("UPDATE budget_projects SET total_minor=99999,policy_revision=2 WHERE id='A-eng' RETURNING id")
    before = snapshot(sql)
    response = request(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/approvals", {"expected_revision": 1})
    assert response.status_code == 409
    assert snapshot(sql) == before
    budget(sql, total=99999, policy_revision=2)
    inventory(sql, reserved=1, sold=0)


@pytest.mark.parametrize("organization,department", [("org-A", "sales"), ("org-B", "eng")])
def test_designated_approver_must_still_share_current_department_and_organization(
    organization, department, client, login, sql,
):
    ctx = ready(client, login, stage="pending")
    approver = login("approver_lin")
    sql("UPDATE memberships SET organization_id=%s,department_id=%s,revision=revision+1 "
        "WHERE user_id='approver_lin' RETURNING user_id", (organization, department))
    before = snapshot(sql)
    assert_error(client.get(f"/approvals/{ctx['application']['id']}", headers=approver), 404, "not_visible")
    assert_error(request(client, approver, f"/approvals/{ctx['application']['id']}/decision",
                         {"expected_revision": 1, "decision": "approved"}), 404, "not_visible")
    assert client.get("/approvals", headers=approver).json() == {"approvals": []}
    assert snapshot(sql) == before
    budget(sql, reserved=100000)


def test_v1_approval_retains_frozen_policy_snapshot_without_v2_revision_gate(client, login, sql, settings):
    ctx = ready(client, login)
    sql("UPDATE budget_projects SET total_minor=250000,policy_revision=2 WHERE id='A-eng' RETURNING id")
    payment = pay(client, ctx["headers"], ctx["lock"], amount=100000)
    # 已受理后不可依据后来资料资格、当前航价或原报价期限重新解释付款。
    sql("UPDATE payment_sources SET eligible=false WHERE ref='demo_corporate_chen' RETURNING ref")
    sql("UPDATE flights SET base_minor=300000 WHERE workspace_id='test' RETURNING id")
    advance(settings, 301)
    expire(settings)
    process_payment(settings, payment)
    assert sql("SELECT policy_revision,budget_total_minor,amount_minor FROM approval_requests") == [
        {"policy_revision": 1, "budget_total_minor": 200000, "amount_minor": 100000}]
    budget(sql, used=100000, total=250000, policy_revision=2)
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 100000}]


def test_idempotency_returns_original_snapshots_and_new_keys_cannot_duplicate(client, login, sql, settings):
    ctx = ready(client, login)
    headers, application = ctx["headers"], ctx["application"]
    before = snapshot(sql)
    assert select(client, headers, ctx["trip"]) == ctx["selection"]
    assert submit(client, headers, ctx["lock"]) == application  # 原响应仍为 pending。
    assert decide(client, ctx["approver"], application) == ctx["approved"]
    assert snapshot(sql) == before
    assert request(client, headers, f"/locks/{ctx['lock']['id']}/approvals", {"expected_revision": 1}, key="new-apply").status_code == 409
    for actor, path, body, key, method in (
        (headers, f"/trips/{ctx['trip']['id']}/budget", {"project_id": "A-eng-alt", "expected_revision": 1, "expected_selection_revision": 0}, "select", "put"),
        (headers, f"/locks/{ctx['lock']['id']}/approvals", {"expected_revision": 2}, "submit", "post"),
        (ctx["approver"], f"/approvals/{application['id']}/decision", {"expected_revision": 1, "decision": "rejected"}, "decision", "post"),
    ):
        assert_error(request(client, actor, path, body, key=key, method=method), 409, "idempotency_conflict")
    assert snapshot(sql) == before
    payment = pay(client, headers, ctx["lock"], amount=100000)
    process_payment(settings, payment)
    before = snapshot(sql)
    assert pay(client, headers, ctx["lock"], amount=100000) == payment
    assert_error(request(client, headers, f"/locks/{ctx['lock']['id']}/payments", {"amount_minor": 100000}), 409, "payment_exists")
    assert snapshot(sql) == before
    budget(sql, used=100000)


def test_replay_rechecks_current_assignment_before_disclosing_prior_decision(client, login, sql):
    ctx = ready(client, login)
    sql("UPDATE approval_assignments SET approver_id='approver_qian' WHERE applicant_id='employee_chen' RETURNING applicant_id")
    before = snapshot(sql)
    assert_error(request(client, ctx["approver"], f"/approvals/{ctx['application']['id']}/decision",
                         {"expected_revision": 1, "decision": "approved"}, key="decision"), 404, "not_visible")
    assert_error(client.get(f"/approvals/{ctx['application']['id']}", headers=ctx["approver"]), 404, "not_visible")
    assert snapshot(sql) == before


def test_revoked_employee_cannot_read_or_replay_corporate_objects(client, login, sql):
    ctx = ready(client, login, stage="pending")
    sql("UPDATE memberships SET active=false,revision=revision+1 WHERE user_id='employee_chen' RETURNING user_id")
    before = snapshot(sql)
    for path in (f"/trips/{ctx['trip']['id']}", f"/approvals/{ctx['application']['id']}", f"/locks/{ctx['lock']['id']}"):
        assert_error(client.get(path, headers=ctx["headers"]), 404, "not_visible")
    assert_error(request(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/approvals", {"expected_revision": 1}, key="submit"), 404, "not_visible")
    assert snapshot(sql) == before


@pytest.mark.parametrize("change", ["date", "profile", "project"])
def test_c04_change_invalidates_approved_snapshot_and_releases_once(change, client, login, sql, settings):
    ctx = ready(client, login)
    if change == "date":
        revised = write(client, ctx["headers"], f"/trips/{ctx['trip']['id']}",
                        {"expected_revision": 1, "travel_date": "2026-10-02"}, method="patch")
        assert revised["revision"] == 2
    elif change == "profile":
        bind(client, ctx["headers"], source="demo_corporate_chen_alt", revision=1, key="rebind")
    else:
        select(client, ctx["headers"], ctx["trip"], "A-eng-alt", 1, key="change-project")
        # V1项目变化不重新计算已有预报价，但已经失效的锁不能付款。
        assert client.get(f"/quotes/{ctx['quote']['id']}", headers=ctx["headers"]).json()["valid"] is True
    assert sql("SELECT status,resource_status FROM approval_requests") == [
        {"status": "invalidated", "resource_status": "released"}]
    budget(sql)
    inventory(sql, reserved=0, sold=0)
    before = snapshot(sql)
    assert_error(request(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/payments", {"amount_minor": 100000}), 409, "state_changed")
    assert request(client, ctx["approver"], f"/approvals/{ctx['application']['id']}/decision", {"expected_revision": 2, "decision": "approved"}).status_code == 409
    expire(settings)
    assert snapshot(sql) == before


@pytest.mark.parametrize("ending", ["rejected", "cancelled"])
def test_rejection_or_cancellation_terminates_application_and_releases_once(ending, client, login, sql):
    ctx = ready(client, login, stage="pending")
    if ending == "rejected":
        approver = login("approver_lin")
        first = decide(client, approver, ctx["application"], "rejected")
        assert decide(client, approver, ctx["application"], "rejected") == first
    else:
        first = write(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/cancel", key="cancel")
        assert write(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/cancel", key="cancel") == first
        write(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/cancel", key="cancel-again")
    assert sql("SELECT resource_status FROM approval_requests") == [{"resource_status": "released"}]
    assert sql("SELECT count(*) AS n FROM approval_requests WHERE status IN ('pending','approved')") == [{"n": 0}]
    budget(sql)
    inventory(sql, reserved=0, sold=0)


@pytest.mark.parametrize("stage", ["pending", "approved"])
def test_exact_300_expiry_cannot_approve_or_pay_and_cleanup_releases_once(stage, client, login, sql, settings):
    ctx = ready(client, login, stage=stage)
    approver = ctx.get("approver") or login("approver_lin")
    advance(settings, 300)
    before = snapshot(sql)
    lock_view = client.get(f"/locks/{ctx['lock']['id']}", headers=ctx["headers"]).json()
    assert lock_view["valid"] is False and lock_view["effective_status"] == "expired"
    response = request(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/payments", {"amount_minor": 100000})
    assert_error(response, 409, "quote_expired")
    assert request(client, approver, f"/approvals/{ctx['application']['id']}/decision",
                   {"expected_revision": 1 if stage == "pending" else 2, "decision": "approved"}).status_code == 409
    assert snapshot(sql) == before
    expire(settings)
    budget(sql)
    inventory(sql, reserved=0, sold=0)
    assert sql("SELECT status,resource_status FROM approval_requests") == [{"status": "expired", "resource_status": "released"}]
    before = snapshot(sql)
    expire(settings)
    assert snapshot(sql) == before


@pytest.mark.parametrize("outcome", [None, "unknown", "succeeded"])
def test_299_payment_protects_budget_and_rejects_mutation_across_expiry(outcome, client, login, sql, settings):
    ctx = ready(client, login)
    advance(settings, 299)
    payment = pay(client, ctx["headers"], ctx["lock"], amount=100000)
    if outcome:
        process_payment(settings, payment, outcome)
    advance(settings, 2)
    expire(settings)
    budget(sql, reserved=0 if outcome == "succeeded" else 100000, used=100000 if outcome == "succeeded" else 0)
    inventory(sql, reserved=0 if outcome == "succeeded" else 1, sold=1 if outcome == "succeeded" else 0)
    before = snapshot(sql)
    for path, body, method in (
        (f"/trips/{ctx['trip']['id']}", {"expected_revision": 1, "travel_date": "2026-10-02"}, "patch"),
        (f"/trips/{ctx['trip']['id']}/budget", {"project_id": "A-eng-alt", "expected_revision": 1, "expected_selection_revision": 1}, "put"),
        (f"/locks/{ctx['lock']['id']}/cancel", {}, "post"),
    ):
        assert_error(request(client, ctx["headers"], path, body, method=method), 409, "payment_protected")
    if outcome != "succeeded":
        assert_error(request(client, ctx["headers"], "/payment-profile",
                             {"source_ref": "demo_corporate_chen_alt", "expected_revision": 1}, method="put"), 409, "payment_protected")
    assert snapshot(sql) == before
    process_payment(settings, payment)
    budget(sql, used=100000)


@pytest.mark.parametrize("outcome", ["unknown", "lost_response"])
def test_unknown_result_keeps_budget_until_original_provider_operation_reconciles(outcome, client, login, sql, settings):
    ctx = ready(client, login)
    payment = pay(client, ctx["headers"], ctx["lock"], amount=100000)
    process_payment(settings, payment, outcome)
    advance(settings, 301)
    expire(settings)
    budget(sql, reserved=100000)
    inventory(sql, reserved=1, sold=0)
    if outcome == "unknown":
        unknown = write(client, ctx["headers"], f"/payments/{payment['id']}/reconcile", key="unknown-reconcile")
        assert unknown["status"] == "unknown"
        assert sql("SELECT count(*) AS n FROM provider_payments") == [{"n": 0}]
        budget(sql, reserved=100000)
        process_payment(settings, payment, "lost_response")
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 100000}]
    result = write(client, ctx["headers"], f"/payments/{payment['id']}/reconcile", key="settle")
    assert result["status"] == "succeeded"
    budget(sql, used=100000)
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 1}]
    before = snapshot(sql)
    assert write(client, ctx["headers"], f"/payments/{payment['id']}/reconcile", key="settle") == result
    assert_error(request(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/payments", {"amount_minor": 100000}), 409, "payment_exists")
    assert snapshot(sql) == before


def test_definite_payment_failure_releases_budget_and_invalidates_old_approval(client, login, sql, settings):
    ctx = ready(client, login)
    payment = pay(client, ctx["headers"], ctx["lock"], amount=100000)
    process_payment(settings, payment, "failed")
    assert sql("SELECT status,resource_status FROM approval_requests") == [{"status": "failed", "resource_status": "released"}]
    budget(sql)
    inventory(sql, reserved=0, sold=0)
    assert sql("SELECT count(*) AS n FROM ticket_tasks") == [{"n": 0}]
    before = snapshot(sql)
    process_payment(settings, payment, "failed")
    assert request(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/payments", {"amount_minor": 100000}).status_code == 409
    assert snapshot(sql) == before


def test_ticket_failure_retains_paid_and_budget_used_without_second_payment(client, login, sql, settings):
    ctx = ready(client, login)
    payment = pay(client, ctx["headers"], ctx["lock"], amount=100000)
    process_payment(settings, payment)
    paid = client.get(f"/payments/{payment['id']}", headers=ctx["headers"]).json()
    process_ticket(settings, paid["ticket_task_id"], "failed")
    assert sql("SELECT status FROM trips") == [{"status": "paid"}]
    assert sql("SELECT status FROM ticket_tasks") == [{"status": "failed"}]
    assert sql("SELECT count(*) AS n FROM tickets") == [{"n": 0}]
    budget(sql, used=100000)
    inventory(sql, reserved=0, sold=1)
    before = snapshot(sql)
    process_ticket(settings, paid["ticket_task_id"])
    assert_error(request(client, ctx["headers"], f"/locks/{ctx['lock']['id']}/payments", {"amount_minor": 100000}), 409, "payment_exists")
    assert snapshot(sql) == before


def test_corporate_strict_input_rejects_identity_control_and_stale_revisions(client, login, sql):
    ctx = ready(client, login, stage="pending")
    approver = login("approver_lin")
    before = snapshot(sql)
    for actor, path, body, method in (
        (ctx["headers"], f"/trips/{ctx['trip']['id']}/budget", {"project_id": "A-eng", "expected_revision": 1, "expected_selection_revision": 1}, "put"),
        (ctx["headers"], f"/locks/{ctx['lock']['id']}/approvals", {"expected_revision": 1}, "post"),
        (approver, f"/approvals/{ctx['application']['id']}/decision", {"expected_revision": 1, "decision": "approved"}, "post"),
    ):
        for extra in ({"role": "approver"}, {"owner_id": "employee_chen"}, {"organization_id": "org-A"}, {"outcome": "succeeded"}):
            assert_error(request(client, actor, path, {**body, **extra}, method=method), 422, "invalid_request")
        assert_error(request(client, actor, path, {**body, "expected_revision": True}, method=method), 422, "invalid_request")
    assert_error(request(client, approver, f"/approvals/{ctx['application']['id']}/decision",
                         {"expected_revision": 2, "decision": "approved"}), 409, "state_changed")
    assert snapshot(sql) == before


@pytest.mark.parametrize("stage", ["select", "submit", "decision", "payment", "cancel"])
def test_event_failure_rolls_back_corporate_state_budget_and_idempotency(stage, client, login, sql, settings):
    ctx = ready(client, login, stage={"select": "locked", "submit": "selected", "decision": "pending"}.get(stage, "approved"))
    headers, method = ctx["headers"], "post"
    if stage == "select":
        method, path = "put", f"/trips/{ctx['trip']['id']}/budget"
        body = {"project_id": "A-eng", "expected_revision": 1, "expected_selection_revision": 0}
    elif stage == "submit":
        path, body = f"/locks/{ctx['lock']['id']}/approvals", {"expected_revision": 1}
    elif stage == "decision":
        headers, path, body = login("approver_lin"), f"/approvals/{ctx['application']['id']}/decision", {"expected_revision": 1, "decision": "rejected"}
    elif stage == "payment":
        path, body = f"/locks/{ctx['lock']['id']}/payments", {"amount_minor": 100000}
    else:
        path, body = f"/locks/{ctx['lock']['id']}/cancel", {}
    before = snapshot(sql)
    with fail_business_events(settings):
        assert_error(request(client, headers, path, body, key="retry-fault", method=method), 503, "storage_unavailable")
    assert snapshot(sql) == before
    write(client, headers, path, body, key="retry-fault", method=method)


def test_worker_event_fault_rolls_back_capture_and_budget_settlement(client, login, sql, settings):
    ctx = ready(client, login)
    payment = pay(client, ctx["headers"], ctx["lock"], amount=100000)
    before = snapshot(sql)
    # 在预算已由占用转已用后才拒绝事件，而非只在供应商首条事件处失败。
    with fail_budget_consumption(settings):
        with pytest.raises(psycopg.Error):
            process_payment(settings, payment)
    assert snapshot(sql) == before
    budget(sql, reserved=100000)
    process_payment(settings, payment)
    budget(sql, used=100000)
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 100000}]


@contextmanager
def fail_budget_consumption(settings):
    with connect(settings.database_url) as connection:
        connection.execute("""CREATE FUNCTION test_reject_budget_consumption() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test budget event failure'; END $$""")
        connection.execute("""CREATE TRIGGER test_reject_budget_consumption BEFORE INSERT ON business_events
            FOR EACH ROW WHEN (NEW.action='approval.consumed')
            EXECUTE FUNCTION test_reject_budget_consumption()""")
    try:
        yield
    finally:
        with connect(settings.database_url) as connection:
            connection.execute("DROP TRIGGER IF EXISTS test_reject_budget_consumption ON business_events")
            connection.execute("DROP FUNCTION IF EXISTS test_reject_budget_consumption()")


def synchronize_connections(monkeypatch, *modules):
    barrier, seen_threads, backend_ids = Barrier(2), set(), set()
    for module in modules:
        original = module.connect

        def factory(original_connect):
            @contextmanager
            def synchronized(*args, **kwargs):
                with original_connect(*args, **kwargs) as connection:
                    identifier = get_ident()
                    if identifier not in seen_threads:
                        seen_threads.add(identifier)
                        backend_ids.add(connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                        barrier.wait(timeout=15)
                    yield connection
            return synchronized

        monkeypatch.setattr(module, "connect", factory(original))
    return backend_ids


def concurrent(functions):
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(function) for function in functions]
        return [future.result(timeout=30) for future in futures]


def test_concurrent_employees_reserve_only_one_100000_from_150000_budget(app, client, login, sql, monkeypatch):
    chen = ready(client, login, stage="selected")
    ding = ready(client, login, account="employee_ding", stage="selected")
    sql("UPDATE budget_projects SET total_minor=150000,policy_revision=2 WHERE id='A-eng' RETURNING id")
    backends = synchronize_connections(monkeypatch, corporate)

    def apply(ctx):
        with TestClient(app) as separate:
            return request(separate, ctx["headers"], f"/locks/{ctx['lock']['id']}/approvals", {"expected_revision": 1})

    responses = concurrent([lambda: apply(chen), lambda: apply(ding)])
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(backends) == 2
    assert sql("SELECT count(*) AS n,sum(amount_minor) AS amount FROM approval_requests") == [{"n": 1, "amount": 100000}]
    budget(sql, reserved=100000, total=150000, policy_revision=2)
    inventory(sql, reserved=2, sold=0)


def test_concurrent_approval_and_trip_revision_cannot_leave_a_live_stale_approval(app, client, login, sql, monkeypatch):
    ctx = ready(client, login, stage="pending")
    approver = login("approver_lin")
    backends = synchronize_connections(monkeypatch, corporate, app_module)

    def approve():
        with TestClient(app) as separate:
            return request(separate, approver, f"/approvals/{ctx['application']['id']}/decision",
                           {"expected_revision": 1, "decision": "approved"})

    def modify():
        with TestClient(app) as separate:
            return request(separate, ctx["headers"], f"/trips/{ctx['trip']['id']}",
                           {"expected_revision": 1, "travel_date": "2026-10-02"}, method="patch")

    approval, changed = concurrent([approve, modify])
    assert changed.status_code == 200
    assert approval.status_code in (200, 409)
    assert len(backends) == 2
    assert sql("SELECT revision FROM trips") == [{"revision": 2}]
    assert sql("SELECT status,resource_status FROM approval_requests") == [{"status": "invalidated", "resource_status": "released"}]
    budget(sql)
    inventory(sql, reserved=0, sold=0)


def test_role_removal_blocks_original_profile_replay_without_leaking_source(client, login, sql):
    ctx = ready(client, login, stage="pending")
    sql("UPDATE memberships SET employee=false,revision=revision+1 WHERE user_id='employee_chen' RETURNING user_id")
    before = snapshot(sql)
    assert_error(client.get("/payment-profile", headers=ctx["headers"]), 404, "not_visible")
    assert_error(request(client, ctx["headers"], "/payment-profile",
                         {"source_ref": "demo_corporate_chen", "expected_revision": 0},
                         key="bind", method="put"), 404, "not_visible")
    assert_error(request(client, ctx["headers"], "/trips", {
        "flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 1,
    }, key="create"), 404, "not_visible")
    assert snapshot(sql) == before


def test_even_explicit_self_assignment_cannot_approve_existing_application(client, login, sql):
    ctx = ready(client, login, stage="pending")
    sql("UPDATE approval_assignments SET approver_id='employee_chen' WHERE applicant_id='employee_chen' RETURNING applicant_id")
    sql("UPDATE approval_requests SET approver_id='employee_chen' RETURNING id")
    before = snapshot(sql)
    assert_error(request(client, ctx["headers"], f"/approvals/{ctx['application']['id']}/decision",
                         {"expected_revision": 1, "decision": "approved"}), 404, "not_visible")
    assert client.get("/approvals", headers=ctx["headers"]).json() == {"approvals": []}
    assert snapshot(sql) == before
    budget(sql, reserved=100000)


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_submissions_hold_only_one_reservation(same_key, app, client, login, sql, monkeypatch):
    ctx = ready(client, login, stage="selected")
    backends = synchronize_connections(monkeypatch, corporate)

    def apply(key):
        with TestClient(app) as separate:
            return request(separate, ctx["headers"], f"/locks/{ctx['lock']['id']}/approvals",
                           {"expected_revision": 1}, key=key)

    responses = concurrent([lambda: apply("submission-a"), lambda: apply("submission-a" if same_key else "submission-b")])
    assert sorted(response.status_code for response in responses) == ([201, 201] if same_key else [201, 409])
    if same_key:
        assert responses[0].json() == responses[1].json()
    assert len(backends) == 2
    assert sql("SELECT count(*) AS n,sum(amount_minor) AS amount FROM approval_requests") == [{"n": 1, "amount": 100000}]
    assert sql("SELECT count(*) AS n FROM business_events WHERE action='approval.submitted'") == [{"n": 1}]
    budget(sql, reserved=100000)


def test_reapprove_new_date_issues_only_revision_two_ticket(client, login, sql, settings):
    ctx = ready(client, login)
    changed = write(client, ctx["headers"], f"/trips/{ctx['trip']['id']}",
                    {"expected_revision": 1, "travel_date": "2026-10-02"}, method="patch")
    quotation = quote(client, ctx["headers"], changed, key="revised-quote")
    booking = lock(client, ctx["headers"], quotation, key="revised-lock")
    before = snapshot(sql)
    assert_error(request(client, ctx["headers"], f"/locks/{booking['id']}/payments",
                         {"amount_minor": 100000}), 409, "requires_approval")
    assert snapshot(sql) == before
    application = submit(client, ctx["headers"], booking, key="revised-application")
    decide(client, ctx["approver"], application, key="revised-decision")
    payment = pay(client, ctx["headers"], booking, amount=100000)
    process_payment(settings, payment)
    paid = client.get(f"/payments/{payment['id']}", headers=ctx["headers"]).json()
    process_ticket(settings, paid["ticket_task_id"])
    assert sql("SELECT trip_revision,quantity FROM tickets") == [{"trip_revision": 2, "quantity": 1}]
    assert sql("SELECT status,resource_status,trip_revision FROM approval_requests ORDER BY trip_revision") == [
        {"status": "invalidated", "resource_status": "released", "trip_revision": 1},
        {"status": "approved", "resource_status": "consumed", "trip_revision": 2},
    ]
    assert sql("SELECT count(*) AS n,sum(captured_minor) AS captured FROM provider_payments") == [{"n": 1, "captured": 100000}]
    budget(sql, used=100000)
    inventory(sql, reserved=0, sold=0)
    inventory(sql, reserved=0, sold=1, travel_date="2026-10-02")
