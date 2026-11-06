"""企业 V1：可信成员与指派、预算占用、审批快照；复用个人付款/出票引擎。"""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Request
from pydantic import Field

from .app import Input, Revision, authenticate, fail, operation_key, owned_trip, save_result
from .db import business_time, connect, lock_actor, read_snapshot


class BudgetInput(Input):
    project_id: Annotated[str, Field(min_length=1, max_length=80)]
    expected_revision: Revision
    expected_selection_revision: Annotated[int, Field(strict=True, ge=0)]


class ApprovalInput(Input):
    expected_revision: Revision


class DecisionInput(Input):
    expected_revision: Revision
    decision: Literal["approved", "rejected"]


def membership(conn, ws, actor, *, required=False, employee=False, approver=False):
    row = conn.execute("""SELECT m.*,o.active AS organization_active,d.active AS department_active
        FROM memberships m JOIN organizations o ON (o.workspace_id,o.id)=(m.workspace_id,m.organization_id)
        JOIN departments d ON (d.workspace_id,d.organization_id,d.id)=
            (m.workspace_id,m.organization_id,m.department_id)
        WHERE m.workspace_id=%s AND m.user_id=%s FOR SHARE OF m,o,d""", (ws, actor)).fetchone()
    if row is None:
        if required:
            fail(404, "not_visible")
        return None
    if (not row["active"] or not row["organization_active"] or not row["department_active"]
            or (employee and not row["employee"]) or (approver and not row["approver"])):
        fail(404, "not_visible")
    return row


def employee_context(conn, ws, actor):
    return membership(conn, ws, actor, employee=True)


def check_trip_access(conn, ws, actor, trip):
    member = employee_context(conn, ws, actor)
    if trip["organization_id"] is not None and (not member or
            (member["organization_id"], member["department_id"]) !=
            (trip["organization_id"], trip["department_id"])):
        fail(404, "not_visible")


def check_source(conn, ws, actor, source):
    member = employee_context(conn, ws, actor)
    if source["kind"] == "corporate" and (not member or
            source["organization_id"] != member["organization_id"]):
        fail(404, "not_visible")
    return member


def project_for(conn, ws, member, project_id, *, lock=False):
    query = """SELECT * FROM budget_projects WHERE workspace_id=%s AND id=%s
        AND organization_id=%s AND department_id=%s AND active"""
    if lock:
        query += " FOR UPDATE"
    row = conn.execute(query, (ws, project_id, member["organization_id"], member["department_id"])).fetchone()
    if not row:
        fail(404, "not_visible")
    return row


def assigned_approver(conn, ws, member, *, expected=None):
    assignment = conn.execute("""SELECT * FROM approval_assignments
        WHERE workspace_id=%s AND applicant_id=%s AND active FOR SHARE""",
        (ws, member["user_id"])).fetchone()
    if not assignment or assignment["approver_id"] == member["user_id"]:
        fail(404, "not_visible")
    if expected is not None and assignment["approver_id"] != expected:
        fail(404, "not_visible")
    target = membership(conn, ws, assignment["approver_id"], required=True, approver=True)
    user = conn.execute("SELECT active FROM users WHERE workspace_id=%s AND id=%s FOR SHARE",
                        (ws, target["user_id"])).fetchone()
    if not user or not user["active"] or (target["organization_id"], target["department_id"]) != (
            member["organization_id"], member["department_id"]):
        fail(404, "not_visible")
    return target["user_id"]


def approval_row(conn, ws, identifier):
    try:
        identifier = UUID(str(identifier))
    except (ValueError, TypeError):
        fail(404, "not_visible")
    row = conn.execute("SELECT * FROM approval_requests WHERE workspace_id=%s AND id=%s",
                       (ws, identifier)).fetchone()
    if not row:
        fail(404, "not_visible")
    return row


def authorize_approval(conn, ws, actor, row, *, decision=False):
    if actor == row["applicant_id"] and not decision:
        owned_trip(conn, ws, actor, row["trip_id"])
        return
    if actor != row["approver_id"] or actor == row["applicant_id"]:
        fail(404, "not_visible")
    member = membership(conn, ws, row["applicant_id"], required=True, employee=True)
    applicant = conn.execute("SELECT active FROM users WHERE workspace_id=%s AND id=%s FOR SHARE",
                             (ws, row["applicant_id"])).fetchone()
    # 归属/当前成员属性发生变化后，历史指派不能继续授予读取或审批权。
    trip = conn.execute("SELECT * FROM trips WHERE workspace_id=%s AND id=%s",
                        (ws, row["trip_id"])).fetchone()
    if (not applicant or not applicant["active"] or trip is None or trip["owner_id"] != row["applicant_id"] or
            (member["organization_id"], member["department_id"]) !=
            (row["organization_id"], row["department_id"]) or
            (trip["organization_id"], trip["department_id"]) !=
            (row["organization_id"], row["department_id"])):
        fail(404, "not_visible")
    assigned_approver(conn, ws, member, expected=actor)


def approval_view(conn, ws, row):
    names = ("id", "trip_id", "trip_revision", "lock_id", "project_id", "selection_revision",
             "policy_revision", "budget_total_minor", "amount_minor", "currency", "revision",
             "status", "resource_status")
    result = {name: str(row[name]) if isinstance(row[name], UUID) else row[name] for name in names}
    detail = conn.execute("""SELECT u.display_name,b.flight_id,b.travel_date,b.quantity,b.expires_at,
            b.status AS lock_status,t.revision AS current_revision
        FROM booking_locks b JOIN trips t ON (t.workspace_id,t.id)=(b.workspace_id,b.trip_id)
        JOIN users u ON (u.workspace_id,u.id)=(t.workspace_id,t.owner_id)
        WHERE b.workspace_id=%s AND b.id=%s""", (ws, row["lock_id"])).fetchone()
    result.update(applicant_name=detail["display_name"], flight_id=detail["flight_id"],
                  travel_date=detail["travel_date"].isoformat(), passengers=detail["quantity"])
    now = conn.execute("SELECT business_time FROM workspaces WHERE id=%s", (ws,)).fetchone()["business_time"]
    expired = row["resource_status"] == "held" and now >= detail["expires_at"]
    result["effective_status"] = "expired" if expired else row["status"]
    result["valid"] = (row["status"] == "approved" and row["resource_status"] in {"held", "protected", "consumed"}
                       and not expired and detail["current_revision"] == row["trip_revision"])
    return result


def end_reservation(conn, ws, actor, booking, reason, executor):
    """仅释放尚未受理付款的占用；由共有 release_lock 在库存更新之后调用。"""
    from .personal import event
    row = conn.execute("""UPDATE approval_requests SET status=%s,resource_status='released',revision=revision+1
        WHERE workspace_id=%s AND lock_id=%s AND resource_status='held' RETURNING *""",
        (reason, ws, booking["id"])).fetchone()
    if row:
        conn.execute("""UPDATE budget_projects SET reserved_minor=reserved_minor-%s
            WHERE workspace_id=%s AND id=%s""", (row["amount_minor"], ws, row["project_id"]))
        event(conn, ws, actor, row["trip_id"], "approval", row["id"], "approval." + reason,
              row["trip_revision"], executor)


def validate_approval_snapshot(conn, ws, trip, booking, row, now):
    from .personal import check_snapshot
    member = membership(conn, ws, trip["owner_id"], required=True, employee=True)
    check_trip_access(conn, ws, trip["owner_id"], trip)
    check_snapshot(conn, ws, trip["owner_id"], trip, booking, now)
    selected = conn.execute("SELECT * FROM trip_budget_selections WHERE workspace_id=%s AND trip_id=%s",
                            (ws, trip["id"])).fetchone()
    if (booking["status"] != "held" or booking["payment_kind"] != "corporate" or
            row["resource_status"] != "held" or row["trip_revision"] != trip["revision"] or
            row["lock_id"] != booking["id"] or row["amount_minor"] != booking["amount_minor"] or
            row["applicant_id"] != trip["owner_id"] or not selected or
            (row["project_id"], row["selection_revision"]) != (selected["project_id"], selected["revision"]) or
            (row["organization_id"], row["department_id"]) !=
            (member["organization_id"], member["department_id"])):
        fail(409, "state_changed")
    assigned_approver(conn, ws, member, expected=row["approver_id"])
    project_for(conn, ws, member, row["project_id"], lock=True)


def protect_approval(conn, ws, actor, trip, booking, now):
    from .personal import event
    if booking["payment_kind"] != "corporate":
        return None
    row = conn.execute("""SELECT * FROM approval_requests WHERE workspace_id=%s AND lock_id=%s
        AND resource_status='held' FOR UPDATE""", (ws, booking["id"])).fetchone()
    if not row or row["status"] != "approved":
        fail(409, "requires_approval")
    validate_approval_snapshot(conn, ws, trip, booking, row, now)
    conn.execute("UPDATE approval_requests SET resource_status='protected' WHERE workspace_id=%s AND id=%s",
                 (ws, row["id"]))
    event(conn, ws, actor, trip["id"], "approval", row["id"], "approval.protected", trip["revision"])
    return row["id"]


def settle_budget(conn, ws, actor, payment, booking, succeeded, executor):
    from .personal import event
    if booking["payment_kind"] != "corporate":
        if payment["approval_id"] is not None:
            raise ValueError("个人付款不应携带企业批准")
        return
    row = conn.execute("SELECT * FROM approval_requests WHERE workspace_id=%s AND id=%s FOR UPDATE",
                       (ws, payment["approval_id"])).fetchone()
    if (not row or row["resource_status"] != "protected" or row["status"] != "approved" or
            row["lock_id"] != booking["id"] or row["trip_revision"] != booking["trip_revision"] or
            row["amount_minor"] != payment["amount_minor"]):
        raise ValueError("企业付款与预算保护快照不一致")
    conn.execute("""UPDATE budget_projects SET reserved_minor=reserved_minor-%s,used_minor=used_minor+%s
        WHERE workspace_id=%s AND id=%s""",
        (row["amount_minor"], row["amount_minor"] if succeeded else 0, ws, row["project_id"]))
    conn.execute("""UPDATE approval_requests SET resource_status=%s,status=%s,revision=revision+%s
        WHERE workspace_id=%s AND id=%s""",
        ("consumed" if succeeded else "released", "approved" if succeeded else "failed",
         0 if succeeded else 1, ws, row["id"]))
    event(conn, ws, actor, row["trip_id"], "approval", row["id"],
          "approval.consumed" if succeeded else "approval.failed", row["trip_revision"], executor)


def register_routes(app, settings):
    from . import personal
    ws = settings.workspace_id

    def writer(conn, request):
        actor = authenticate(conn, request, ws)["id"]
        now = business_time(conn, ws)
        lock_actor(conn, ws, actor)
        return actor, now, operation_key(request)

    @app.get("/budget-projects")
    def budgets(request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            actor = authenticate(conn, request, ws)["id"]
            member = membership(conn, ws, actor, required=True, employee=True)
            rows = conn.execute("""SELECT id,currency,total_minor,reserved_minor,used_minor,
                total_minor-reserved_minor-used_minor AS available_minor,policy_revision
                FROM budget_projects WHERE workspace_id=%s AND organization_id=%s AND department_id=%s
                AND active ORDER BY id""", (ws, member["organization_id"], member["department_id"])).fetchall()
        return {"projects": rows}

    @app.put("/trips/{trip_id}/budget")
    def select_budget(trip_id: str, body: BudgetInput, request: Request):
        with connect(settings.database_url) as conn:
            actor, now, key = writer(conn, request)
            trip = owned_trip(conn, ws, actor, trip_id, lock=True)
            member = membership(conn, ws, actor, required=True, employee=True)
            project_for(conn, ws, member, body.project_id)
            params, action = {"trip_id": str(trip["id"]), **body.model_dump()}, "budget.selected"
            result = personal.previous(conn, ws, actor, action, key, params)
            if result is None:
                if trip["status"] != "draft":
                    fail(409, "payment_protected")
                selection = conn.execute("SELECT * FROM trip_budget_selections WHERE workspace_id=%s AND trip_id=%s FOR UPDATE",
                                         (ws, trip["id"])).fetchone()
                if (trip["revision"] != body.expected_revision or body.expected_selection_revision !=
                        (selection["revision"] if selection else 0)):
                    fail(409, "state_changed")
                quotation = conn.execute("""SELECT * FROM quotes WHERE workspace_id=%s AND trip_id=%s
                    AND status='active' AND payment_kind='corporate' AND expires_at>%s
                    ORDER BY expires_at DESC,id LIMIT 1""", (ws, trip["id"], now)).fetchone()
                if not quotation:
                    fail(409, "requires_corporate_quote")
                personal.check_snapshot(conn, ws, actor, trip, quotation, now)
                if selection and selection["project_id"] != body.project_id:
                    locks = conn.execute("SELECT * FROM booking_locks WHERE workspace_id=%s AND trip_id=%s AND status='held' FOR UPDATE",
                                         (ws, trip["id"])).fetchall()
                    for booking in locks:
                        personal.release_lock(conn, ws, actor, booking, "invalidated", "user")
                changed = not selection or selection["project_id"] != body.project_id
                revision = (selection["revision"] if selection else 0) + int(changed)
                conn.execute("""INSERT INTO trip_budget_selections(workspace_id,trip_id,project_id,revision)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(workspace_id,trip_id)
                    DO UPDATE SET project_id=excluded.project_id,revision=excluded.revision""",
                    (ws, trip["id"], body.project_id, revision))
                result = {"trip_id": str(trip["id"]), "project_id": body.project_id,
                          "trip_revision": trip["revision"], "selection_revision": revision}
                if changed:
                    personal.event(conn, ws, actor, trip["id"], "budget_selection", trip["id"], action, trip["revision"])
                save_result(conn, ws, actor, action, key, params, result)
        return result

    @app.post("/locks/{lock_id}/approvals", status_code=201)
    def submit(lock_id: str, body: ApprovalInput, request: Request):
        with connect(settings.database_url) as conn:
            actor, now, key = writer(conn, request)
            booking = personal.owned(conn, ws, actor, "lock", lock_id, lock=True)
            trip = owned_trip(conn, ws, actor, booking["trip_id"])
            params, action = {"lock_id": str(booking["id"]), **body.model_dump()}, "approval.submitted"
            result = personal.previous(conn, ws, actor, action, key, params)
            if result is None:
                member = membership(conn, ws, actor, required=True, employee=True)
                if trip["status"] != "draft":
                    fail(409, "payment_protected")
                if trip["revision"] != body.expected_revision or booking["status"] != "held":
                    fail(409, "state_changed")
                if booking["payment_kind"] != "corporate":
                    fail(409, "requires_corporate_quote")
                personal.check_snapshot(conn, ws, actor, trip, booking, now)
                active = conn.execute("""SELECT 1 FROM approval_requests WHERE workspace_id=%s AND trip_id=%s
                    AND resource_status IN ('held','protected','consumed')""", (ws, trip["id"])).fetchone()
                if active:
                    fail(409, "active_approval_exists")
                selected = conn.execute("SELECT * FROM trip_budget_selections WHERE workspace_id=%s AND trip_id=%s",
                                        (ws, trip["id"])).fetchone()
                if not selected:
                    fail(409, "requires_budget")
                approver = assigned_approver(conn, ws, member)
                project = project_for(conn, ws, member, selected["project_id"], lock=True)
                reserved = conn.execute("""UPDATE budget_projects SET reserved_minor=reserved_minor+%s
                    WHERE workspace_id=%s AND id=%s AND total_minor-used_minor-reserved_minor>=%s RETURNING id""",
                    (booking["amount_minor"], ws, project["id"], booking["amount_minor"])).fetchone()
                if not reserved:
                    fail(409, "budget_insufficient")
                row = conn.execute("""INSERT INTO approval_requests(workspace_id,id,trip_id,applicant_id,approver_id,
                    organization_id,department_id,trip_revision,lock_id,project_id,selection_revision,policy_revision,
                    budget_total_minor,amount_minor,currency,revision,status,resource_status)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'DEMO',1,'pending','held') RETURNING *""",
                    (ws, uuid4(), trip["id"], actor, approver, member["organization_id"], member["department_id"],
                     trip["revision"], booking["id"], project["id"], selected["revision"], project["policy_revision"],
                     project["total_minor"], booking["amount_minor"])).fetchone()
                personal.event(conn, ws, actor, trip["id"], "approval", row["id"], action, trip["revision"])
                result = approval_view(conn, ws, row)
                save_result(conn, ws, actor, action, key, params, result)
        return result

    @app.get("/approvals")
    def list_approvals(request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            actor = authenticate(conn, request, ws)["id"]
            membership(conn, ws, actor, required=True, approver=True)
            # 列表先做相同授权联接，避免向未指派者泄露对象数量/ID。
            rows = conn.execute("""SELECT a.* FROM approval_requests a
                JOIN approval_assignments x ON (x.workspace_id,x.applicant_id,x.approver_id)=
                    (a.workspace_id,a.applicant_id,a.approver_id)
                JOIN memberships m ON (m.workspace_id,m.user_id)=(a.workspace_id,a.applicant_id)
                JOIN memberships r ON (r.workspace_id,r.user_id)=(a.workspace_id,a.approver_id)
                JOIN users u ON (u.workspace_id,u.id)=(a.workspace_id,a.applicant_id)
                JOIN trips t ON (t.workspace_id,t.id)=(a.workspace_id,a.trip_id)
                WHERE a.workspace_id=%s AND a.approver_id=%s AND a.applicant_id<>%s AND a.status='pending'
                AND x.active AND m.active AND m.employee AND u.active
                AND (m.organization_id,m.department_id)=(a.organization_id,a.department_id)
                AND (r.organization_id,r.department_id)=(a.organization_id,a.department_id)
                AND (t.organization_id,t.department_id,t.owner_id)=(a.organization_id,a.department_id,a.applicant_id)
                ORDER BY a.id""", (ws, actor, actor)).fetchall()
            result = {"approvals": [approval_view(conn, ws, row) for row in rows]}
        return result

    @app.get("/approvals/{approval_id}")
    def read_approval(approval_id: str, request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            actor = authenticate(conn, request, ws)["id"]
            row = approval_row(conn, ws, approval_id)
            authorize_approval(conn, ws, actor, row)
            result = approval_view(conn, ws, row)
        return result

    @app.post("/approvals/{approval_id}/decision")
    def decide(approval_id: str, body: DecisionInput, request: Request):
        with connect(settings.database_url) as conn:
            actor = authenticate(conn, request, ws)["id"]
            now = business_time(conn, ws)
            key, action = operation_key(request), "approval.decided"
            # 审批人不取得申请人的对象访问权；只用其主体锁防止修改/付款竞争。
            row = approval_row(conn, ws, approval_id)
            authorize_approval(conn, ws, actor, row, decision=True)
            scope = json.dumps([ws, actor, action, key], separators=(",", ":"))
            conn.execute("SELECT pg_advisory_xact_lock(%s)",
                         (int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], signed=True),))
            lock_actor(conn, ws, row["applicant_id"])
            trip = conn.execute("SELECT * FROM trips WHERE workspace_id=%s AND id=%s FOR UPDATE",
                                (ws, row["trip_id"])).fetchone()
            booking = conn.execute("SELECT * FROM booking_locks WHERE workspace_id=%s AND id=%s FOR UPDATE",
                                   (ws, row["lock_id"])).fetchone()
            row = conn.execute("SELECT * FROM approval_requests WHERE workspace_id=%s AND id=%s FOR UPDATE",
                               (ws, row["id"])).fetchone()
            authorize_approval(conn, ws, actor, row, decision=True)
            params = {"approval_id": str(row["id"]), **body.model_dump()}
            old = conn.execute("""SELECT parameters,response FROM idempotency WHERE workspace_id=%s
                AND actor_id=%s AND action=%s AND key=%s""", (ws, actor, action, key)).fetchone()
            if old:
                authorize_approval(conn, ws, actor, approval_row(conn, ws, old["response"]["id"]), decision=True)
                if old["parameters"] != params:
                    fail(409, "idempotency_conflict")
                result = old["response"]
            else:
                if row["revision"] != body.expected_revision or row["status"] != "pending":
                    fail(409, "state_changed")
                if trip["status"] != "draft":
                    fail(409, "payment_protected")
                validate_approval_snapshot(conn, ws, trip, booking, row, now)
                if body.decision == "rejected":
                    # 共用库存释放逻辑，再将该次申请的终结原因记为拒绝。
                    personal.release_lock(conn, ws, actor, booking, "cancelled", "user", approval_reason="rejected")
                else:
                    conn.execute("UPDATE approval_requests SET status='approved',revision=revision+1 WHERE workspace_id=%s AND id=%s",
                                 (ws, row["id"]))
                    personal.event(conn, ws, actor, trip["id"], "approval", row["id"], "approval.approved", trip["revision"])
                result = approval_view(conn, ws, approval_row(conn, ws, row["id"]))
                save_result(conn, ws, actor, action, key, params, result)
        return result
