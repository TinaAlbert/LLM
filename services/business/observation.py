"""可信宿主的最小观察投影；此模块和DB权限绝不交给受限外壳。"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import Request
from pydantic import Field

from .app import Input, authenticate, fail, owned_trip, token_digest, token_from
from .corporate import approval_row, approval_view, authorize_approval
from .db import connect, read_snapshot
from .personal import view
from .reads import latest

VERSIONS = {
    "product_id": "travel-lab", "product_version": "v1", "contract_version": "d2-1",
    "knowledge_version": "read-status-1", "rule_version": "booking-v1",
    "policy_version": "subject-assignment-v1", "feature_config_version": "read-only-1",
}
# 这是独立筛选的产品资料，不读取开发文档或测试预期。
DOCUMENTS = {
    "trip": {"id": "booking-status", "revision": 1, "source_kind": "documented",
             "source": "product-published:travel-lab/v1/booking-status",
             "text": "在我的行程中打开本人行程。报价是暂估，锁价后确认冻结金额。付款与出票分别查询；付款未知时查询原付款，不另建付款。",
             "ui_anchor": "我的行程 / 行程详情 / 付款与票据"},
    "approval": {"id": "assigned-approval", "revision": 1, "source_kind": "documented",
                 "source": "product-published:travel-lab/v1/assigned-approval",
                 "text": "审批人仅查看被指派申请的必要摘要。申请对应指定行程修订；状态或权限变化后重新读取，由本人在网页决定。",
                 "ui_anchor": "被指派的申请摘要"},
}
FIELDS = {
    "trip": ("id", "revision", "flight_id", "travel_date", "passengers", "status"),
    "quote": ("id", "trip_revision", "amount_minor", "currency", "effective_status", "valid", "expires_at", "price_basis"),
    "lock": ("id", "trip_revision", "amount_minor", "currency", "effective_status", "valid", "expires_at", "price_basis"),
    "payment": ("id", "status", "amount_minor", "currency"),
    "ticket_task": ("id", "status"),
    "ticket": ("id", "status", "trip_revision"),
    "approval": ("id", "revision", "effective_status", "valid"),
    "assigned": ("id", "revision", "trip_revision", "applicant_name", "flight_id", "travel_date",
                 "passengers", "amount_minor", "currency", "effective_status", "valid"),
}


class Selection(Input):
    kind: Literal["trip", "approval"]
    object_id: UUID
    expected_evidence: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def pick(kind, row):
    if row is None:
        return None
    result = {}
    for name in FIELDS[kind]:
        if name not in row: fail(503, "observation_unavailable")
        value = row[name]  # 缺失字段失败，不静默用None填造。
        result[name] = str(value) if isinstance(value, UUID) else (
            value.isoformat() if hasattr(value, "isoformat") else value)
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def capture(settings, request, selection):
    """每次独立PG快照重新授权；返回过滤信封，不返回原HTTP/SQL行。"""
    ws = settings.workspace_id
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        actor = authenticate(conn, request, ws)
        now = conn.execute("SELECT business_time FROM workspaces WHERE id=%s", (ws,)).fetchone()["business_time"]
        versions = {"membership": actor.get("membership_revision", 0)}
        unknowns = []
        if selection.kind == "trip":
            trip = owned_trip(conn, ws, actor["id"], selection.object_id)
            data = {"trip": pick("trip", trip)}
            versions["trip"] = trip["revision"]
            for kind, action in (("quote", "quote.created"), ("lock", "lock.created"),
                                 ("payment", "payment.accepted"), ("ticket_task", "ticket_task.created"),
                                 ("ticket", "ticket.issued"), ("approval", "approval.submitted")):
                row = latest(conn, ws, trip["id"], kind, action)
                if row is not None and kind == "approval":
                    authorize_approval(conn, ws, actor["id"], row)
                    result = approval_view(conn, ws, row)
                else:
                    result = view(conn, ws, kind, row) if row is not None else None
                data[kind] = pick(kind, result)
            # 资料只参与作废，不披露来源标识/资格推断。
            profile = conn.execute("SELECT revision FROM payment_profiles WHERE workspace_id=%s AND owner_id=%s",
                                   (ws, actor["id"])).fetchone()
            versions["payment_profile"] = profile["revision"] if profile else 0
            current = data["lock"] if data["lock"] and data["lock"]["valid"] else data["quote"]
            if not current or not current["valid"]:
                unknowns.append("current_quote_amount")
        else:
            row = approval_row(conn, ws, selection.object_id)
            authorize_approval(conn, ws, actor["id"], row)
            data = {"approval": pick("assigned", approval_view(conn, ws, row))}
            versions["approval"] = row["revision"]
            versions["trip"] = row["trip_revision"]
        stable = {**VERSIONS, "state_versions": versions, "business_time": now.isoformat(),
                  "source_kind": "observed", "environment": "local-demo",
                  "simulated_identity": actor["id"], "disclosure_scope": "current-session",
                  "assumptions": ["synthetic_demo_account", "simulated_business_services"],
                  "unknowns": unknowns, "data": data, "documented": DOCUMENTS[selection.kind]}
        evidence = fingerprint({"projection": stable, "authorization": actor,
                                "workspace": ws, "session": token_digest(token_from(request))})
        if selection.expected_evidence is not None and selection.expected_evidence != evidence:
            fail(409, "state_changed")
    observed = datetime.now(timezone.utc)
    return {**stable, "evidence_id": evidence, "observed_at": observed.isoformat(),
            "valid_until": (observed + timedelta(seconds=30)).isoformat()}


def verify_delivery(settings, request, selection, envelope):
    # 任何来自外壳/未来模型的来源或字段变动都不能自证可信。
    if any(envelope.get(key) != value for key, value in VERSIONS.items()):
        fail(409, "unsupported_version")
    if envelope.get("source_kind") != "observed" or envelope.get("environment") != "local-demo":
        fail(409, "state_changed")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(envelope["valid_until"]):
        fail(409, "observation_expired")
    fresh = capture(settings, request, selection)
    for key in fresh.keys() - {"observed_at", "valid_until"}:
        if envelope.get(key) != fresh[key]:
            fail(409, "state_changed")
    if set(envelope) != set(fresh):
        fail(409, "state_changed")
    return fresh


def register_routes(app, settings):
    @app.get("/guide/observe")
    def observe(request: Request):
        from pydantic import ValidationError
        from services.guide.windows import IsolationUnavailable, exchange
        if (set(request.query_params) - {"kind", "object_id", "expected_evidence"}
                or len(request.query_params.multi_items()) != len(request.query_params)):
            fail(422, "invalid_request")
        try:
            selection = Selection.model_validate(dict(request.query_params))
        except ValidationError:
            fail(422, "invalid_request")
        # 先授权，再启动。没有隔离环境时不把观察发给普通进程替代。
        envelope = capture(settings, request, selection)
        try:
            returned = exchange(selection.kind, str(selection.object_id),
                                lambda: verify_delivery(settings, request, selection, envelope))
        except IsolationUnavailable:
            fail(503, "isolation_unavailable")
        return verify_delivery(settings, request, selection, returned)
