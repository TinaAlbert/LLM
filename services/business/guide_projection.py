"""D3 可信宿主的单个人对象投影；私有身份/配置/UUID 不进入模型。"""

import math
import selectors
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import psycopg
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.guide.knowledge import fingerprint, load_knowledge, published_text
from .app import authenticate, fail, owned_trip, token_digest, token_from
from .db import read_snapshot
from .observation import pick
from .personal import view
from .reads import latest

VERSIONS = {
    "product_id": "travel-lab", "product_version": "v1", "contract_version": "d3-personal-1",
    "knowledge_version": "personal-guide-1", "rule_version": "booking-v1",
    "policy_version": "subject-assignment-v1", "feature_config_version": "personal-1",
}
# 送模叶字段在这些严格模型集中定义。源行先复用D2 pick，再去掉实际ID。
# 私有配置选择与public模型分开；额外字段从不随SELECT *外发。
Minor = int
MAX_MINOR = 9223372036854775807


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Trip(Strict):
    ref: Literal["trip"]
    revision: int = Field(ge=1)
    flight_id: str = Field(min_length=1, max_length=80)
    travel_date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    passengers: int = Field(ge=1, le=9)
    status: Literal["draft", "payment_pending", "paid"]

    @field_validator("flight_id")
    @classmethod
    def bounded_label(cls, value):
        return published_text(value)

    @field_validator("travel_date")
    @classmethod
    def valid_date(cls, value):
        datetime.strptime(value, "%Y-%m-%d")
        return value


class Price(Strict):
    ref: Literal["quote", "lock"]
    trip_revision: int = Field(ge=1)
    amount_minor: int = Field(ge=0, le=MAX_MINOR)
    currency: Literal["DEMO"]
    effective_status: Literal["active", "held", "protected", "consumed", "cancelled",
                              "expired", "invalidated", "failed"]
    valid: bool
    expires_at: str
    price_basis: Literal["estimate", "frozen"]

    @field_validator("expires_at")
    @classmethod
    def aware_time(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("报价期限缺时区")
        return value

    @model_validator(mode="after")
    def kind_consistent(self):
        if self.price_basis != ("estimate" if self.ref == "quote" else "frozen"):
            raise ValueError("报价来源类型错误")
        if self.ref == "quote" and self.effective_status not in {"active", "expired", "invalidated"}:
            raise ValueError("报价状态错误")
        if self.valid != (self.effective_status in {"active", "held"}):
            raise ValueError("有效性字段矛盾")
        return self


class Payment(Strict):
    ref: Literal["payment"]
    status: Literal["processing", "unknown", "succeeded", "failed"]
    amount_minor: int = Field(ge=0, le=MAX_MINOR)
    currency: Literal["DEMO"]


class TicketTask(Strict):
    ref: Literal["ticket_task"]
    status: Literal["pending", "succeeded", "failed"]


class Ticket(Strict):
    ref: Literal["ticket"]
    status: Literal["valid"]
    trip_revision: int = Field(ge=1)


class Data(Strict):
    trip: Trip
    quote: Price | None
    lock: Price | None
    payment: Payment | None
    ticket_task: TicketTask | None
    ticket: Ticket | None

    @model_validator(mode="after")
    def refs_match(self):
        for kind in ("quote", "lock"):
            value = getattr(self, kind)
            if value is not None and value.ref != kind:
                raise ValueError("匿名引用错位")
        return self


class StateVersions(Strict):
    trip: int = Field(ge=1)
    payment_profile: int = Field(ge=0)


class PublicProjection(Strict):
    product_id: Literal["travel-lab"]
    product_version: Literal["v1"]
    contract_version: Literal["d3-personal-1"]
    knowledge_version: Literal["personal-guide-1"]
    rule_version: Literal["booking-v1"]
    policy_version: Literal["subject-assignment-v1"]
    feature_config_version: Literal["personal-1"]
    source_kind: Literal["observed"]
    environment: Literal["local-demo"]
    simulated_identity: Literal["synthetic_personal"]
    disclosure_scope: Literal["current-session"]
    assumptions: list[Literal["synthetic_demo_account", "simulated_business_services"]]
    unknowns: list[Literal["current_quote_amount", "payment_profile_eligibility", "flight_availability"]]
    business_time: str
    state_versions: StateVersions
    payment_profile_state: Literal["unbound", "usable", "unusable", "unknown"]
    flight_state: Literal["available", "unavailable", "unknown"]
    data: Data
    observed_at: str
    valid_until: str

    @field_validator("business_time", "observed_at", "valid_until")
    @classmethod
    def aware_time(cls, value):
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("观察时效缺时区")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.assumptions != ["synthetic_demo_account", "simulated_business_services"]:
            raise ValueError("合成环境假设缺失")
        if len(self.unknowns) != len(set(self.unknowns)):
            raise ValueError("未知字段重复")
        if self.state_versions.trip != self.data.trip.revision:
            raise ValueError("行程修订矛盾")
        observed = datetime.fromisoformat(self.observed_at)
        if datetime.fromisoformat(self.valid_until) - observed != timedelta(seconds=30):
            raise ValueError("观察时效不是30秒")
        return self


def validate_public(public):
    """只放行本次获准叶字段；错误字段不删除后继续外发，直接拒绝。"""
    return PublicProjection.model_validate(public).model_dump(mode="json")


@dataclass(frozen=True)
class HostIdentity:
    actor_id: str
    session_digest: str = field(repr=False)

    def __iter__(self):
        yield self.actor_id
        yield self.session_digest


@dataclass(frozen=True)
class HostEvidence:
    public: dict
    fingerprint: str = field(repr=False)
    actor_id: str = field(repr=False)
    session_digest: str = field(repr=False)
    knowledge_fingerprint: str = field(repr=False)
    observed_at: datetime
    valid_until: datetime
    ref_map: dict = field(repr=False)
    config_fingerprint: str = field(repr=False)
    expires_monotonic: float = field(repr=False)

    def expired(self):
        return time.monotonic() >= self.expires_monotonic or datetime.now(timezone.utc) >= self.valid_until


class ObservationTimeout(Exception):
    """不带DSN/SQL/配置的可信宿主观察超时。"""


class _BoundedConnection(psycopg.Connection):
    """锁定Psycopg3.3的同步wait支持真实客户端期限；取消后立刻关闭本连接。"""

    stop_at: float
    release_at: float

    def _cancel_and_close(self):
        if self.closed:
            return
        try:
            # PG17/libpq17的非阻塞取消；额外取消连接也有绝对期限。
            self.cancel_safe(timeout=max(.001, min(.1, self.release_at - time.monotonic())))
        except psycopg.Error:
            pass
        finally:
            self.close()

    def wait(self, gen, interval=.05, timeout=None):
        remaining = self.stop_at - time.monotonic()
        if remaining <= 0:
            self._cancel_and_close()
            raise ObservationTimeout("observation_timeout")
        try:
            return super().wait(gen, interval=min(.05, remaining),
                                timeout=min(remaining, timeout) if timeout is not None else remaining)
        except psycopg.errors._WaitTimeout:
            self._cancel_and_close()
            raise ObservationTimeout("observation_timeout") from None


def _open_connection(database_url, stop_at, release_at):
    """固定本机PG单端点非阻塞建连，沿用原数据库/身份，不改变业务db.connect。"""
    params = conninfo_to_dict(database_url)
    host = params.get("host", "")
    if host not in {"127.0.0.1", "::1", "localhost"} or params.get("hostaddr") not in {None, "", "127.0.0.1", "::1"}:
        raise ValueError("D3观察仅允许当前本机单端点PG")
    # 固定数字地址免于DNS等待；localhost对应现有IPv4本地实例。
    params["hostaddr"] = "::1" if host == "::1" else "127.0.0.1"
    params["application_name"] = "llm-d3-observation"
    params["connect_timeout"] = "3"
    milliseconds = max(1, min(2500, math.floor((stop_at - time.monotonic()) * 1000)))
    params["options"] = f"-c statement_timeout={milliseconds} -c lock_timeout={milliseconds} -c timezone=UTC"
    conninfo = make_conninfo("", **params)
    pgconn = pq.PGconn.connect_start(conninfo.encode())
    transferred = False
    try:
        with selectors.DefaultSelector() as selector:
            while True:
                remaining = stop_at - time.monotonic()
                if remaining <= 0:
                    raise ObservationTimeout("observation_timeout")
                status = pgconn.connect_poll()
                if status == pq.PollingStatus.OK:
                    pgconn.nonblocking = 1
                    conn = _BoundedConnection(pgconn, row_factory=dict_row)
                    conn.stop_at, conn.release_at = stop_at, release_at
                    transferred = True
                    return conn
                if status == pq.PollingStatus.FAILED or pgconn.status == pq.ConnStatus.BAD:
                    # 不把低层libpq错误原文（可能含连接信息）带到观察调用层。
                    raise psycopg.OperationalError("D3 observation connection unavailable")
                if status not in {pq.PollingStatus.READING, pq.PollingStatus.WRITING}:
                    raise psycopg.OperationalError("D3 observation connection unavailable")
                events = selectors.EVENT_READ if status == pq.PollingStatus.READING else selectors.EVENT_WRITE
                selector.register(pgconn.socket, events)
                try:
                    if not selector.select(remaining):
                        raise ObservationTimeout("observation_timeout")
                finally:
                    selector.unregister(pgconn.socket)
    finally:
        if not transferred:
            pgconn.finish()


@contextmanager
def bounded_snapshot(database_url, deadline):
    """连接及全部快照查询总计最多3秒，取消/释放预留在同一上限内。"""
    release_at = min(float(deadline), time.monotonic() + 3)
    stop_at = release_at - .15  # 留同一3秒上限内的取消/释放时间。
    if stop_at <= time.monotonic():
        raise ObservationTimeout("observation_timeout")
    conn = _open_connection(database_url, stop_at, release_at)
    try:
        read_snapshot(conn)
        yield conn
        if time.monotonic() >= stop_at:
            raise ObservationTimeout("observation_timeout")
        # 观察不写任何业务状态，回滚也释放 authenticate 取得的短时共享锁。
        conn.rollback()
    except psycopg.errors.QueryCanceled:
        conn._cancel_and_close()
        raise ObservationTimeout("observation_timeout") from None
    finally:
        # 异常路径不用无限等待ROLLBACK；关闭后PG释放事务/锁。
        conn.close()


def auth_only(settings, request, deadline):
    """cancel只验证当前会话；不读取对象、配置、观察或知识。"""
    with bounded_snapshot(settings.database_url, deadline) as conn:
        actor = authenticate(conn, request, settings.workspace_id)
        return HostIdentity(actor["id"], token_digest(token_from(request)))


def _required(row, names):
    if row is None:
        return None
    if any(name not in row for name in names):
        fail(503, "observation_unavailable")
    return {name: _scalar(row[name]) for name in names}


def _scalar(value):
    if isinstance(value, UUID):
        return str(value)
    return value.isoformat() if hasattr(value, "isoformat") else value


def capture(settings, request, trip_id, deadline):
    """本人单行程实查；仅public可构造GuideInput，其余绑定只留宿主。"""
    bundle = load_knowledge()
    ws = settings.workspace_id
    with bounded_snapshot(settings.database_url, deadline) as conn:
        observed = datetime.now(timezone.utc)
        observed_monotonic = time.monotonic()
        actor = authenticate(conn, request, ws)
        trip = owned_trip(conn, ws, actor["id"], trip_id)
        # owned_trip先走同一不可见策略，合法本人超范围才说明未覆盖。
        workspace = conn.execute("SELECT business_time,fixture_key FROM workspaces WHERE id=%s", (ws,)).fetchone()
        if (actor.get("organization_id") is not None or trip["organization_id"] is not None
                or workspace["fixture_key"] != "single-trip"):
            fail(409, "guide_scope_unsupported")
        data, ref_map = {}, {}
        def project(kind, row):
            if row is None:
                return None
            chosen = pick(kind, row)
            ref_map[kind] = chosen.pop("id")
            return {"ref": kind, **chosen}
        data["trip"] = project("trip", trip)
        for kind, action in (("quote", "quote.created"), ("lock", "lock.created"),
                             ("payment", "payment.accepted"), ("ticket_task", "ticket_task.created"),
                             ("ticket", "ticket.issued")):
            row = latest(conn, ws, trip["id"], kind, action)
            data[kind] = project(kind, view(conn, ws, kind, row) if row is not None else None)

        profile = conn.execute(
            "SELECT id,owner_id,source_ref,revision FROM payment_profiles WHERE workspace_id=%s AND owner_id=%s",
            (ws, actor["id"])).fetchone()
        profile_config = _required(profile, ("id", "owner_id", "source_ref", "revision"))
        source = None
        if profile is not None:
            source = conn.execute(
                "SELECT ref,owner_id,kind,eligible,organization_id FROM payment_sources WHERE workspace_id=%s AND ref=%s",
                (ws, profile["source_ref"])).fetchone()
        source_config = _required(source, ("ref", "owner_id", "kind", "eligible", "organization_id"))
        if profile is None:
            profile_state = "unbound"
        elif source is None:
            profile_state = "unusable"
        elif source["eligible"] is None:
            profile_state = "unknown"
        else:
            profile_state = "usable" if (
                source["eligible"] is True and source["owner_id"] == actor["id"] and
                source["kind"] == "personal" and source["organization_id"] is None) else "unusable"

        flight = conn.execute(
            """SELECT id,travel_date,active,base_minor,fee_minor,capacity,reserved,sold FROM flights
               WHERE workspace_id=%s AND id=%s AND travel_date=%s""",
            (ws, trip["flight_id"], trip["travel_date"])).fetchone()
        flight_config = _required(flight, ("id", "travel_date", "active", "base_minor", "fee_minor",
                                            "capacity", "reserved", "sold"))
        flight_state = ("unavailable" if flight is None else "unknown" if flight["active"] is None
                        else "available" if flight["active"] is True else "unavailable")
        unknowns = []
        current = data["lock"] if data["lock"] and data["lock"]["valid"] else data["quote"]
        if current is None or not current["valid"]:
            unknowns.append("current_quote_amount")
        if profile_state == "unknown":
            unknowns.append("payment_profile_eligibility")
        if flight_state == "unknown":
            unknowns.append("flight_availability")
        config_fingerprint = fingerprint({"profile": profile_config, "source": source_config,
                                          "flight": flight_config, "fixture": workspace["fixture_key"]})
        stable = {
            **VERSIONS, "source_kind": "observed", "environment": "local-demo",
            "simulated_identity": "synthetic_personal", "disclosure_scope": "current-session",
            "assumptions": ["synthetic_demo_account", "simulated_business_services"],
            "unknowns": unknowns, "business_time": workspace["business_time"].isoformat(),
            "state_versions": {"trip": trip["revision"], "payment_profile": profile["revision"] if profile else 0},
            "data": data, "payment_profile_state": profile_state, "flight_state": flight_state,
        }
        session_digest = token_digest(token_from(request))
        private_fingerprint = fingerprint({
            "projection": stable, "authorization": actor, "workspace": ws, "session": session_digest,
            "references": ref_map, "configuration": config_fingerprint, "knowledge": bundle.fingerprint,
        })
    valid_until = observed + timedelta(seconds=30)
    public = validate_public({**stable, "observed_at": observed.isoformat(), "valid_until": valid_until.isoformat()})
    return HostEvidence(public, private_fingerprint, actor["id"], session_digest, bundle.fingerprint,
                        observed, valid_until, ref_map, config_fingerprint, observed_monotonic + 30)
