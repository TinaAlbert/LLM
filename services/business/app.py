"""D1-01：真实会话、草稿与 PostgreSQL 原子业务事件。"""

import hashlib
import json
import logging
import re
import secrets
from datetime import date
from typing import Annotated, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from .config import Settings, load_settings
from .db import business_time, connect, lock_actor, read_snapshot

FlightId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
PassengerCount = Annotated[int, Field(strict=True, ge=1, le=9)]
Revision = Annotated[int, Field(strict=True, ge=1)]


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("日期须为 YYYY-MM-DD")
    return value


TravelDate = Annotated[date, BeforeValidator(iso_date)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    account: Literal["personal_alice", "personal_bob", "employee_chen", "employee_ding",
                     "employee_fang", "approver_lin", "approver_wu", "approver_zhao", "approver_qian"]


class TripCreate(Input):
    flight_id: FlightId
    travel_date: TravelDate
    passengers: PassengerCount


class TripUpdate(Input):
    expected_revision: Revision
    flight_id: FlightId | None = None
    travel_date: TravelDate | None = None
    passengers: PassengerCount | None = None

    @model_validator(mode="after")
    def require_changes(self):
        changes = self.model_fields_set - {"expected_revision"}
        if not changes or any(getattr(self, name) is None for name in changes):
            raise ValueError("至少修改一个非 null 的草稿字段")
        return self


def fail(status, code):
    raise HTTPException(status_code=status, detail=code)


def token_from(request: Request, *, optional=False):
    value = request.headers.get("authorization")
    if value is None and optional:
        return None
    if value is None:
        fail(401, "authentication_required")
    parts = value.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 128:
        fail(401, "authentication_required")
    return parts[1]


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(conn, request, workspace, *, exclusive=False):
    token = token_from(request)
    query = """SELECT u.id, u.display_name FROM sessions s
           JOIN users u ON (u.workspace_id, u.id) = (s.workspace_id, s.user_id)
           WHERE s.token_hash = %s AND s.workspace_id = %s
             AND NOT s.revoked AND s.expires_at > clock_timestamp() AND u.active
           """
    query += " FOR UPDATE OF s FOR SHARE OF u" if exclusive else " FOR SHARE OF s, u"
    user = conn.execute(
        query,
        (token_digest(token), workspace),
    ).fetchone()
    if not user:
        fail(401, "authentication_required")
    from .corporate import membership
    member = membership(conn, workspace, user["id"])
    if member:
        user.update(organization_id=member["organization_id"], department_id=member["department_id"],
                    membership_revision=member["revision"],
                    roles=[role for role in ("employee", "approver") if member[role]])
    return user


def operation_key(request):
    value = request.headers.get("idempotency-key", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        fail(422, "invalid_request")
    return value


def owned_trip(conn, workspace, actor, trip_id, *, lock=False):
    try:
        identifier = UUID(str(trip_id))
    except (ValueError, TypeError):
        fail(404, "not_visible")
    query = """SELECT * FROM trips
               WHERE workspace_id = %s AND id = %s AND owner_id = %s"""
    if lock:
        business_time(conn, workspace)
        lock_actor(conn, workspace, actor)
        query += " FOR UPDATE"
    trip = conn.execute(query, (workspace, identifier, actor)).fetchone()
    if not trip:
        fail(404, "not_visible")
    from .corporate import check_trip_access
    check_trip_access(conn, workspace, actor, trip)
    return trip


def trip_view(trip):
    # 显式白名单；业务内部字段不能随 SELECT * 自动暴露。
    return {
        "id": str(trip["id"]),
        "owner_id": trip["owner_id"],
        "organization_id": trip["organization_id"],
        "flight_id": trip["flight_id"],
        "travel_date": trip["travel_date"].isoformat(),
        "passengers": trip["passengers"],
        "revision": trip["revision"],
        "status": trip["status"],
        "price": None,
        "price_status": "requires_payment_profile",
    }


def replay(conn, workspace, actor, action, key, parameters):
    scope = json.dumps([workspace, actor, action, key], separators=(",", ":"))
    lock_id = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], signed=True)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
    old = conn.execute(
        """SELECT parameters, response FROM idempotency
           WHERE workspace_id=%s AND actor_id=%s AND action=%s AND key=%s""",
        (workspace, actor, action, key),
    ).fetchone()
    if old:
        # 即使已存在幂等结果，当前对象访问权也必须仍有效。
        owned_trip(conn, workspace, actor, old["response"]["id"])
        if old["parameters"] != parameters:
            fail(409, "idempotency_conflict")
        return old["response"]
    return None


def save_result(conn, workspace, actor, action, key, parameters, result):
    conn.execute(
        """INSERT INTO idempotency(workspace_id, actor_id, action, key, parameters, response)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (workspace, actor, action, key, Jsonb(parameters), Jsonb(result)),
    )


def record_event(conn, workspace, actor, trip_id, action, before, after):
    conn.execute(
        """INSERT INTO business_events
           (workspace_id, actor_id, trip_id, action, before_revision,
            after_revision, business_time, result)
           SELECT %s, %s, %s, %s, %s, %s, business_time, 'succeeded'
           FROM workspaces WHERE id = %s""",
        (workspace, actor, trip_id, action, before, after, workspace),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="模拟差旅：D1-03", version="0.3.0", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.exception_handler(HTTPException)
    async def public_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.exception_handler(psycopg.Error)
    async def storage_error(request, exc):
        # 仅记录异常类别/SQLSTATE供工程诊断；不记录DSN、SQL、参数或异常原文。
        logging.getLogger(__name__).warning("PostgreSQL failure: %s/%s", type(exc).__name__, exc.sqlstate)
        return JSONResponse({"error": "storage_unavailable"}, status_code=503)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    if settings.demo_login:
        @app.post("/demo/sessions", status_code=201)
        def login(body: Login, request: Request):
            local_peers = {"127.0.0.1", "::1"}
            if settings.mode == "test":
                local_peers.add("testclient")
            if not request.client or request.client.host not in local_peers:
                fail(404, "not_visible")
            with connect(settings.database_url) as conn:
                old_token = token_from(request, optional=True)
                if old_token:
                    authenticate(conn, request, settings.workspace_id, exclusive=True)
                    conn.execute("UPDATE sessions SET revoked=true WHERE token_hash=%s",
                                 (token_digest(old_token),))
                user = conn.execute(
                    """SELECT id, display_name FROM users
                       WHERE workspace_id=%s AND id=%s AND active FOR SHARE""",
                    (settings.workspace_id, body.account),
                ).fetchone()
                if not user:
                    fail(401, "authentication_required")
                token = secrets.token_urlsafe(32)
                row = conn.execute(
                    """INSERT INTO sessions(token_hash, workspace_id, user_id, expires_at)
                       VALUES (%s, %s, %s, clock_timestamp() + interval '1 hour')
                       RETURNING expires_at""",
                    (token_digest(token), settings.workspace_id, user["id"]),
                ).fetchone()
                result = {"access_token": token, "token_type": "bearer",
                          "expires_at": row["expires_at"].isoformat(), "user": user}
            if old_token and hasattr(app.state, "guide"):
                app.state.guide.invalidate_session(token_digest(old_token))
            if old_token and hasattr(app.state, "assistant"):
                app.state.assistant.invalidate_session(token_digest(old_token))
            return result

    @app.get("/session")
    def session(request: Request):
        with connect(settings.database_url) as conn:
            result = authenticate(conn, request, settings.workspace_id)
        return result

    @app.delete("/session", status_code=204)
    def logout(request: Request):
        with connect(settings.database_url) as conn:
            authenticate(conn, request, settings.workspace_id, exclusive=True)
            conn.execute("UPDATE sessions SET revoked=true WHERE token_hash=%s",
                         (token_digest(token_from(request)),))
        if hasattr(app.state, "guide"):
            app.state.guide.invalidate_session(token_digest(token_from(request)))
        if hasattr(app.state, "assistant"):
            app.state.assistant.invalidate_session(token_digest(token_from(request)))
        return Response(status_code=204)

    @app.post("/trips", status_code=201)
    def create_trip(body: TripCreate, request: Request):
        from .personal import enrich_trip
        from .corporate import employee_context
        workspace = settings.workspace_id
        parameters = body.model_dump(mode="json")
        with connect(settings.database_url) as conn:
            actor = authenticate(conn, request, workspace)["id"]
            member = employee_context(conn, workspace, actor)
            key = operation_key(request)
            result = replay(conn, workspace, actor, "trip.created", key, parameters)
            if result is None:
                trip = conn.execute(
                    """INSERT INTO trips(workspace_id,id,owner_id,organization_id,department_id,flight_id,travel_date,
                       passengers,revision,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,'draft')
                       RETURNING *""",
                    (workspace, uuid4(), actor, member["organization_id"] if member else None,
                     member["department_id"] if member else None, body.flight_id, body.travel_date, body.passengers),
                ).fetchone()
                result = enrich_trip(conn, workspace, trip, trip_view(trip))
                record_event(conn, workspace, actor, trip["id"], "trip.created", 0, 1)
                save_result(conn, workspace, actor, "trip.created", key, parameters, result)
        return result

    @app.get("/trips/{trip_id}")
    def get_trip(trip_id: str, request: Request):
        from .personal import enrich_trip
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            actor = authenticate(conn, request, settings.workspace_id)["id"]
            trip = owned_trip(conn, settings.workspace_id, actor, trip_id)
            result = enrich_trip(conn, settings.workspace_id, trip, trip_view(trip))
        return result

    @app.patch("/trips/{trip_id}")
    def update_trip(trip_id: str, body: TripUpdate, request: Request):
        workspace = settings.workspace_id
        with connect(settings.database_url) as conn:
            actor = authenticate(conn, request, workspace)["id"]
            trip = owned_trip(conn, workspace, actor, trip_id, lock=True)
            key = operation_key(request)
            parameters = {"trip_id": str(trip["id"]), **body.model_dump(mode="json", exclude_unset=True)}
            result = replay(conn, workspace, actor, "trip.updated", key, parameters)
            if result is None:
                if trip["revision"] != body.expected_revision:
                    fail(409, "state_changed")
                from .personal import before_trip_change, enrich_trip
                before_trip_change(conn, workspace, actor, trip)
                changes = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
                updated = {**trip, **changes}
                row = conn.execute(
                    """UPDATE trips SET flight_id=%s,travel_date=%s,passengers=%s,
                       revision=revision+1 WHERE workspace_id=%s AND id=%s RETURNING *""",
                    (updated["flight_id"], updated["travel_date"], updated["passengers"],
                     workspace, trip["id"]),
                ).fetchone()
                result = enrich_trip(conn, workspace, row, trip_view(row))
                record_event(conn, workspace, actor, trip["id"], "trip.updated",
                             trip["revision"], row["revision"])
                save_result(conn, workspace, actor, "trip.updated", key, parameters, result)
        return result

    from .personal import register_routes
    register_routes(app, settings)
    from .corporate import register_routes as register_corporate
    register_corporate(app, settings)
    from .coupons import register_routes as register_coupons
    register_coupons(app, settings)
    from .reads import register_routes as register_reads
    register_reads(app, settings)
    from .observation import register_routes as register_observation
    register_observation(app, settings)
    from services.guide.coordinator import register_routes as register_guide
    register_guide(app, settings)
    from services.guide.assistant import register_routes as register_assistant
    register_assistant(app, settings)
    return app
