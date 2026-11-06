"""P21 的本地有界探索通道：普通模拟角色、固定业务 HTTP、匿名委托。

此模块不创建/重置数据库、不推进时钟或 worker，不持有 SQL 能力，不调用模型。
当前执行器是可信普通宿主，尚未完成文件/网络/派生进程隔离验收；不能作为在线
助手的业务代操作入口。工程控制器须先核实 API 与合成 55433 实例的绑定。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import time
from typing import Callable, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, model_validator

from .page_observation import PAGE_CONTROLS

TARGET = "http://127.0.0.1:8000"
REFS = ("trip_1", "trip_2")
OPERATIONS = ("observe", "booking.quote", "booking.lock", "booking.cancel_lock", "booking.pay")
VERSIONS = {"product_id": "travel-lab", "product_version": "v1",
            "contract_version": "sandbox-shared-1", "rule_version": "booking-v1",
            "policy_version": "subject-assignment-v1", "feature_config_version": "read-only-1"}
FIELDS = {
    "trip": ("revision", "flight_id", "travel_date", "passengers", "status"),
    "quote": ("trip_revision", "amount_minor", "base_amount_minor", "coupon_discount_minor",
              "currency", "effective_status", "valid", "expires_at", "price_basis"),
    "lock": ("trip_revision", "amount_minor", "base_amount_minor", "coupon_discount_minor",
             "currency", "effective_status", "valid", "expires_at", "price_basis"),
    "payment": ("status", "amount_minor", "currency"),
    "ticket_task": ("status",), "ticket": ("status", "trip_revision"),
}
STATUSES = {"trip": {"draft", "payment_pending", "paid"},
            "quote": {"active", "invalidated", "expired"},
            "lock": {"held", "protected", "consumed", "cancelled", "expired", "invalidated", "failed"},
            "payment": {"processing", "unknown", "succeeded", "failed"},
            "ticket_task": {"pending", "succeeded", "failed"}, "ticket": {"valid"}}
ALLOCATIONS = {"estimate", "not_applied", "held", "protected", "consumed", "released", "unknown"}


class ExplorationError(RuntimeError):
    """只传递固定错误码；HTTP 原响应、凭证、对象编号均不进入错误文本。"""


@dataclass(frozen=True)
class Recipient:
    """由可信宿主绑定的当前接收会话；模型没有此构造或修改能力。"""
    subject: str
    generation: str
    disclosure_revision: int
    allow_synthetic_procedure: bool = True
    allow_simulated_amounts: bool = True


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["observe", "booking.quote", "booking.lock", "booking.cancel_lock", "booking.pay"]
    target: Literal["trip_1", "trip_2"] | None = None

    @model_validator(mode="after")
    def target_required(self):
        if (self.operation == "observe") != (self.target is None):
            raise ValueError("invalid_exploration_operation")
        return self


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _pick(kind, raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or any(field not in raw for field in FIELDS[kind]):
        raise ExplorationError("observation_unavailable")
    result = {field: raw[field] for field in FIELDS[kind]}
    for field, value in result.items():
        valid = True
        if field in {"revision", "trip_revision", "passengers"}:
            valid = type(value) is int and 1 <= value <= (9 if field == "passengers" else 2**31 - 1)
        elif field.endswith("_minor"):
            valid = type(value) is int and 0 <= value <= 2**63 - 1
        elif field in {"status", "effective_status"}:
            valid = isinstance(value, str) and value in STATUSES[kind]
        elif field == "currency": valid = value == "DEMO"
        elif field == "valid": valid = type(value) is bool
        elif field == "price_basis": valid = value == ("estimate" if kind == "quote" else "frozen")
        elif field == "flight_id": valid = value in {"DEMO-LARGE", "DEMO-SMALL"}
        elif field == "travel_date":
            try: valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
            except ValueError: valid = False
        elif field == "expires_at":
            try: valid = isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
            except ValueError: valid = False
        if not valid:
            raise ExplorationError("observation_unavailable")
    return result


def _allocation(raw, *, estimate=False):
    if estimate:
        return "estimate" if raw and raw.get("coupon_discount_minor", 0) > 0 else "not_applied"
    if not raw or raw.get("coupon_id") is None:
        return "not_applied"
    return {"held": "held", "protected": "protected", "consumed": "consumed",
            "cancelled": "released", "expired": "released", "invalidated": "released"}.get(
                raw.get("status"), "unknown")


class BoundedExploration:
    """一次合成分支；仅允许预先委托的两对象及注册语义动作。

    client、headers、trip_ids 和接收会话均由工程宿主提供。HTTP 路径只由本适配器
    生成，不接收模型 URL、path、UUID、身份、金额或管理参数。限制为28个语义
    操作、120个HTTP请求、180秒；没有重试或回写原业务环境的能力。
    """
    def __init__(self, client, headers: dict, trip_ids: tuple[str, str], recipient: Recipient,
                 current_recipient: Callable[[], Recipient], *, clock=time.monotonic):
        if str(client.base_url).rstrip("/") != TARGET:
            raise ExplorationError("destination_not_allowed")
        if (len(trip_ids) != 2 or len(set(trip_ids)) != 2
                or any(str(UUID(value)) != value for value in trip_ids)):
            raise ExplorationError("invalid_delegation")
        if set(headers) != {"Authorization"} or not headers["Authorization"].startswith("Bearer "):
            raise ExplorationError("invalid_simulated_session")
        self._client, self._headers = client, dict(headers)
        self._ids = dict(zip(REFS, trip_ids))
        self._recipient, self._current_recipient = recipient, current_recipient
        self._clock, self._deadline = clock, clock() + 180
        self._count = self._requests = 0
        self._trace = []
        self._closed = False
        self._run = uuid4().hex

    def _check(self):
        if self._closed:
            raise ExplorationError("exploration_disposed")
        if self._current_recipient() != self._recipient:
            raise ExplorationError("session_changed")
        if not self._recipient.allow_synthetic_procedure:
            raise ExplorationError("not_disclosable")
        if self._clock() >= self._deadline:
            raise ExplorationError("exploration_budget_exceeded")

    def _http(self, method, path, body=None):
        self._check()
        if self._requests >= 120:
            raise ExplorationError("exploration_budget_exceeded")
        self._requests += 1
        headers = dict(self._headers)
        if method != "GET":
            headers["Idempotency-Key"] = f"explore-{self._run}-{self._requests}"
        kwargs = {"headers": headers, "timeout": min(10, self._deadline - self._clock())}
        if body is not None:
            kwargs["json"] = body
        try:
            response = self._client.request(method, path, **kwargs)
        except Exception:
            # 连接异常可能带 token/URL；不把原始异常送入运行观察。
            raise ExplorationError("exploration_transport_unavailable") from None
        self._check()  # 取消/切换身份/撤权期间晚到的HTTP结果不可继续使用。
        if response.status_code not in (200, 201, 202):
            allowed = {401: "authentication_required", 404: "not_visible", 409: "precondition_not_met"}
            return None, allowed.get(response.status_code, "exploration_operation_failed")
        try:
            return response.json(), None
        except (ValueError, TypeError):
            raise ExplorationError("observation_unavailable") from None

    def _get(self, path):
        result, error = self._http("GET", path)
        if error:
            raise ExplorationError(error)
        return result

    def _ordinary_actor(self):
        if self._get("/session").get("id") != "personal_alice":
            raise ExplorationError("simulated_identity_not_allowed")

    def _capture(self):
        self._ordinary_actor()
        bookings = {ref: self._get(f"/trips/{identifier}/booking") for ref, identifier in self._ids.items()}
        coupon_rows = self._get("/coupons").get("coupons")
        if not isinstance(coupon_rows, list) or len(coupon_rows) != 1:
            raise ExplorationError("synthetic_fixture_changed")
        coupon = coupon_rows[0]
        if coupon.get("status") not in {"available", "held", "protected", "consumed"}:
            raise ExplorationError("observation_unavailable")
        holder = next((ref for ref, booking in bookings.items() if booking.get("lock")
                       and booking["lock"].get("coupon_id") == coupon.get("id")
                       and booking["lock"].get("status") in {"held", "protected", "consumed"}), None)
        shared = {"ref": "shared_coupon", **{key: coupon[key] for key in
                  ("status", "percent", "cap_minor", "currency")},
                  "attribution": "none" if coupon["status"] == "available" else
                                 "delegated_trip" if holder else "unknown",
                  "trip_ref": holder}
        trips = []
        for ref, booking in bookings.items():
            data = {kind: _pick(kind, booking.get(kind)) for kind in FIELDS}
            if data["trip"] is None:
                raise ExplorationError("observation_unavailable")
            for kind in ("quote", "lock"):
                if data[kind]:
                    data[kind]["coupon_allocation"] = _allocation(booking[kind], estimate=kind == "quote")
            current = data["lock"] if data["lock"] and data["lock"]["valid"] else data["quote"]
            trips.append({"ref": ref, "data": data,
                          "unknowns": [] if current and current["valid"] else ["current_quote_amount"]})
        return {"delegated_trips": trips, "shared_coupon": shared}, bookings

    def act(self, value: dict):
        operation = Operation.model_validate(value)
        self._check()
        if self._count >= 28:
            raise ExplorationError("exploration_budget_exceeded")
        self._count += 1
        state, bookings = self._capture()
        result = "observed"
        if operation.operation != "observe":
            booking = bookings[operation.target]
            trip_id = self._ids[operation.target]
            if operation.operation == "booking.quote":
                path, body = f"/trips/{trip_id}/quotes", {"expected_revision": booking["trip"]["revision"]}
            elif operation.operation == "booking.lock":
                if not booking.get("quote"):
                    raise ExplorationError("insufficient_observation")
                path, body = f"/quotes/{booking['quote']['id']}/lock", {}
            else:
                if not booking.get("lock"):
                    raise ExplorationError("insufficient_observation")
                path = f"/locks/{booking['lock']['id']}/" + (
                    "cancel" if operation.operation == "booking.cancel_lock" else "payments")
                body = {} if operation.operation == "booking.cancel_lock" else {
                    "amount_minor": booking["lock"]["amount_minor"]}
            _, error = self._http("POST", path, body)
            result = error or "accepted"
            state, _ = self._capture()
        record = {"sequence": self._count, "operation": operation.model_dump(exclude_none=True),
                  "result": result, "observed_at": datetime.now(timezone.utc).isoformat(),
                  "state": state}
        self._trace.append(record)
        return self._filter_record(record, self._recipient.allow_simulated_amounts)

    @staticmethod
    def _filter_record(record, amounts):
        # 第二次白名单重建：即使内部轨迹被污染，也不转发 SQL/UUID/完整HTTP响应。
        state = record["state"]
        trips = []
        for row in state["delegated_trips"]:
            if (row["ref"] not in REFS or not isinstance(row["unknowns"], list)
                    or any(value != "current_quote_amount" for value in row["unknowns"])):
                raise ExplorationError("not_disclosable")
            data = {kind: _pick(kind, row["data"].get(kind)) for kind in FIELDS}
            for kind in ("quote", "lock"):
                if data[kind]:
                    allocation = row["data"][kind]["coupon_allocation"]
                    if allocation not in ALLOCATIONS:
                        raise ExplorationError("not_disclosable")
                    data[kind]["coupon_allocation"] = allocation
            if not amounts:
                for kind in ("quote", "lock", "payment"):
                    if data[kind]:
                        for name in ("amount_minor", "base_amount_minor", "coupon_discount_minor"):
                            if name in data[kind]: data[kind][name] = None
            trips.append({"ref": row["ref"], "data": data, "unknowns": list(row["unknowns"])})
        coupon = {key: state["shared_coupon"][key] for key in
                  ("ref", "status", "percent", "cap_minor", "currency", "attribution", "trip_ref")}
        if (coupon["ref"] != "shared_coupon" or coupon["status"] not in {"available", "held", "protected", "consumed"}
                or coupon["currency"] != "DEMO" or type(coupon["percent"]) is not int or not 1 <= coupon["percent"] <= 100
                or type(coupon["cap_minor"]) is not int or not 0 <= coupon["cap_minor"] <= 2**63 - 1
                or coupon["attribution"] not in {"none", "delegated_trip", "unknown"}
                or coupon["trip_ref"] not in {*REFS, None}
                or (coupon["attribution"] == "delegated_trip") != (coupon["trip_ref"] in REFS)
                or type(record["sequence"]) is not int or not 1 <= record["sequence"] <= 28
                or record["result"] not in {"observed", "accepted", "authentication_required", "not_visible",
                                           "precondition_not_met", "exploration_operation_failed"}):
            raise ExplorationError("not_disclosable")
        try:
            if datetime.fromisoformat(record["observed_at"]).tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise ExplorationError("not_disclosable") from None
        if not amounts:
            coupon["cap_minor"] = None
        return {"sequence": record["sequence"], "operation": Operation.model_validate(record["operation"]).model_dump(exclude_none=True),
                "result": record["result"], "observed_at": record["observed_at"],
                "state": {"delegated_trips": trips, "shared_coupon": coupon}}

    def deliver(self):
        self._check()
        self._capture()  # 交付前重新核对模拟会话及两个委托对象仍可读取。
        trace = [self._filter_record(row, self._recipient.allow_simulated_amounts) for row in self._trace]
        result = {**VERSIONS, "source_kind": "sandbox_observed", "environment": "synthetic-55433",
                  "simulated_identity": {"account": "personal_alice", "role": "personal"},
                  "disclosure_scope": "synthetic_procedure_with_assumptions",
                  "assumptions": ["synthetic_fixture", "simulated_personal_payment_profile",
                                  "simulated_providers", "no_current_user_records_copied"],
                  "unknowns": ["current_user_eligibility", "current_user_quote", "current_user_inventory"],
                  "executor_isolation": {"accepted": False, "executor": "ordinary_trusted_host_http",
                                         "unverified": ["file", "network", "child_process"]},
                  "page_evidence": "registered_semantics_only_no_browser_dom_observation",
                  "operation_semantics": [name for name in OPERATIONS if name in PAGE_CONTROLS["booking"]],
                  "limits": {"operations": 28, "http_requests": 120, "seconds": 180},
                  "used": {"operations": self._count, "http_requests": self._requests}, "trace": trace}
        self._check()
        return {**result, "evidence_id": _digest(result)}

    def dispose(self):
        self._closed = True
        self._headers.clear()
        self._ids.clear()
        self._trace.clear()
