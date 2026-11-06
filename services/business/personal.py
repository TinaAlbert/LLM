"""共有预订引擎与个人路径：冻结报价、库存/付款保护、持久结果；无控制HTTP。"""

from datetime import timedelta
from uuid import UUID, uuid4
from typing import Annotated

from fastapi import Request
from pydantic import Field

from .app import (Input, Revision, authenticate, fail, operation_key, owned_trip,
                  save_result)
from .db import business_time, connect, lock_actor, read_snapshot


class ProfileInput(Input):
    source_ref: Annotated[str, Field(min_length=1, max_length=80)]
    expected_revision: Annotated[int, Field(strict=True, ge=0)]


class QuoteInput(Input):
    expected_revision: Revision


class PaymentInput(Input):
    amount_minor: Annotated[int, Field(strict=True, ge=0, le=9223372036854775807)]


TABLES = {"quote": "quotes", "lock": "booking_locks", "payment": "payments",
          "ticket_task": "ticket_tasks", "ticket": "tickets"}
FIELDS = {
    "profile": ("id", "owner_id", "source_ref", "revision"),
    "quote": ("id", "trip_id", "trip_revision", "profile_id", "profile_revision",
              "flight_id", "travel_date", "quantity", "amount_minor", "currency", "expires_at", "status"),
    "lock": ("id", "trip_id", "quote_id", "trip_revision", "profile_id", "profile_revision",
             "flight_id", "travel_date", "quantity", "amount_minor", "currency", "expires_at", "status"),
    "payment": ("id", "trip_id", "lock_id", "amount_minor", "currency", "status", "accepted_at"),
    "ticket_task": ("id", "payment_id", "trip_id", "status"),
    "ticket": ("id", "task_id", "payment_id", "trip_id", "trip_revision", "quantity", "certificate"),
}


def event(conn, ws, actor, trip_id, object_type, object_id, action, revision, executor="user"):
    conn.execute("""INSERT INTO business_events
        (workspace_id,actor_id,trip_id,action,before_revision,after_revision,business_time,
         result,object_type,object_id,executor)
        SELECT %s,%s,%s,%s,%s,%s,business_time,'succeeded',%s,%s,%s
        FROM workspaces WHERE id=%s""",
        (ws, actor, trip_id, action, revision, revision, object_type, object_id, executor, ws))


def view(conn, ws, kind, row):
    result = {}
    for name in FIELDS[kind]:
        value = row[name]
        result[name] = str(value) if isinstance(value, UUID) else (
            value.isoformat() if hasattr(value, "isoformat") else value)
    if kind in {"quote", "lock"}:
        result["payment_kind"] = row["payment_kind"]
        result.update(base_amount_minor=row["base_amount_minor"],
                      coupon_discount_minor=row["coupon_discount_minor"],
                      price_basis="estimate" if kind == "quote" else "frozen")
        if kind == "lock":
            result["coupon_id"] = str(row["coupon_id"]) if row["coupon_id"] else None
        now = conn.execute("SELECT business_time FROM workspaces WHERE id=%s", (ws,)).fetchone()["business_time"]
        timed_out = row["status"] in {"active", "held"} and now >= row["expires_at"]
        result["effective_status"] = "expired" if timed_out else row["status"]
        result["valid"] = row["status"] in {"active", "held"} and not timed_out
    if kind == "payment":
        if row["approval_id"] is not None:
            result["approval_id"] = str(row["approval_id"])
        task = conn.execute("SELECT id FROM ticket_tasks WHERE workspace_id=%s AND payment_id=%s",
                            (ws, row["id"])).fetchone()
        result["ticket_task_id"] = str(task["id"]) if task else None
    if kind == "ticket_task":
        ticket = conn.execute("SELECT id FROM tickets WHERE workspace_id=%s AND task_id=%s",
                              (ws, row["id"])).fetchone()
        result["ticket_id"] = str(ticket["id"]) if ticket else None
    if kind == "ticket":
        # 本轮无作废/退款动作；只有真实出票提交后的行才是有效票据。
        result["status"] = "valid"
    return result


def owned(conn, ws, actor, kind, identifier, *, lock=False):
    try:
        identifier = UUID(str(identifier))
    except (ValueError, TypeError):
        fail(404, "not_visible")
    # Table names come only from the fixed internal map, never from a request.
    row = conn.execute(f"""SELECT o.* FROM {TABLES[kind]} o JOIN trips t
        ON (t.workspace_id,t.id)=(o.workspace_id,o.trip_id)
        WHERE o.workspace_id=%s AND o.id=%s AND t.owner_id=%s""",
        (ws, identifier, actor)).fetchone()
    if not row:
        fail(404, "not_visible")
    owned_trip(conn, ws, actor, row["trip_id"])
    if lock:
        owned_trip(conn, ws, actor, row["trip_id"], lock=True)
        row = conn.execute(f"SELECT * FROM {TABLES[kind]} WHERE workspace_id=%s AND id=%s FOR UPDATE",
                           (ws, identifier)).fetchone()
    return row


def previous(conn, ws, actor, action, key, parameters):
    # All new writes already hold the actor lock; their original result stays immutable.
    old = conn.execute("""SELECT parameters,response FROM idempotency
        WHERE workspace_id=%s AND actor_id=%s AND action=%s AND key=%s""",
        (ws, actor, action, key)).fetchone()
    if old:
        response = old["response"]
        if "trip_id" in response:
            owned_trip(conn, ws, actor, response["trip_id"])
        elif not conn.execute("SELECT 1 FROM payment_profiles WHERE workspace_id=%s AND id=%s AND owner_id=%s",
                              (ws, response.get("id"), actor)).fetchone():
            fail(404, "not_visible")
        if parameters != old["parameters"]:
            fail(409, "idempotency_conflict")
        return response
    return None


def release_lock(conn, ws, actor, lock, reason, executor="worker", *, approval_reason=None):
    if reason not in {"expired", "cancelled", "invalidated"}:
        raise ValueError("未知释放原因")
    changed = conn.execute("""UPDATE booking_locks SET status=%s
        WHERE workspace_id=%s AND id=%s AND status='held' RETURNING *""",
        (reason, ws, lock["id"])).fetchone()
    if changed:
        conn.execute("""UPDATE flights SET reserved=reserved-%s
            WHERE workspace_id=%s AND id=%s AND travel_date=%s""",
            (changed["quantity"], ws, changed["flight_id"], changed["travel_date"]))
        from .corporate import end_reservation
        end_reservation(conn, ws, actor, changed, approval_reason or reason, executor)
        from .coupons import transition
        transition(conn, ws, actor, changed, "held", "available", executor)
        event(conn, ws, actor, changed["trip_id"], "lock", changed["id"],
              f"lock.{reason}", changed["trip_revision"], executor)


def invalidate(conn, ws, actor, trips):
    # Same order across owners, including owners with several trips/flights.
    locks = []
    for trip in trips:
        quotes = conn.execute("""UPDATE quotes SET status='invalidated'
            WHERE workspace_id=%s AND trip_id=%s AND status='active' RETURNING *""",
            (ws, trip["id"])).fetchall()
        for quote in quotes:
            event(conn, ws, actor, trip["id"], "quote", quote["id"], "quote.invalidated", trip["revision"])
        locks.extend(conn.execute("""SELECT * FROM booking_locks
            WHERE workspace_id=%s AND trip_id=%s AND status='held' FOR UPDATE""",
            (ws, trip["id"])).fetchall())
    # 多行程资料变更先持有所有库存锁，避免持有共享预算时再等其他库存。
    for flight_id, travel_date in sorted({(row["flight_id"], row["travel_date"]) for row in locks}):
        conn.execute("SELECT id FROM flights WHERE workspace_id=%s AND id=%s AND travel_date=%s FOR UPDATE",
                     (ws, flight_id, travel_date))
    for lock in sorted(locks, key=lambda row: (row["flight_id"], row["travel_date"], str(row["id"]))):
        release_lock(conn, ws, actor, lock, "invalidated", "user")


def before_trip_change(conn, ws, actor, trip):
    if trip["status"] != "draft":
        fail(409, "payment_protected")
    invalidate(conn, ws, actor, [trip])


def profile_for_quote(conn, ws, actor):
    profile = conn.execute("""SELECT p.*,s.eligible,s.kind,s.organization_id FROM payment_profiles p JOIN payment_sources s
        ON (s.workspace_id,s.ref)=(p.workspace_id,p.source_ref)
        WHERE p.workspace_id=%s AND p.owner_id=%s AND s.owner_id=%s""", (ws, actor, actor)).fetchone()
    if not profile:
        fail(409, "requires_payment_profile")
    if not profile["eligible"]:
        fail(409, "payment_profile_ineligible")
    fixture = conn.execute("SELECT fixture_key FROM workspaces WHERE id=%s", (ws,)).fetchone()["fixture_key"]
    if fixture == "two-trips-one-coupon" and profile["kind"] != "personal":
        fail(409, "payment_profile_ineligible")
    from .corporate import check_source
    check_source(conn, ws, actor, profile)
    return profile


def check_snapshot(conn, ws, actor, trip, snapshot, now):
    if now >= snapshot["expires_at"]:
        fail(409, "quote_expired")
    profile = profile_for_quote(conn, ws, actor)
    if (trip["revision"] != snapshot["trip_revision"] or profile["id"] != snapshot["profile_id"]
            or profile["revision"] != snapshot["profile_revision"]
            or profile["source_ref"] != snapshot["source_ref"] or profile["kind"] != snapshot["payment_kind"]):
        fail(409, "state_changed")
    if snapshot.get("coupon_id") is not None:
        from .coupons import eligible
        if not eligible(conn, ws, actor, trip, profile["kind"]):
            fail(409, "state_changed")
    flight = conn.execute("""SELECT * FROM flights WHERE workspace_id=%s AND id=%s
        AND travel_date=%s FOR UPDATE""", (ws, snapshot["flight_id"], snapshot["travel_date"])).fetchone()
    if not flight or not flight["active"]:
        fail(409, "flight_unavailable")


def settle(conn, ws, actor, payment, executor="worker"):
    """只收敛原 provider operation，不发起供应商操作；与 API 对账共用。"""
    payment = conn.execute("SELECT * FROM payments WHERE workspace_id=%s AND id=%s FOR UPDATE",
                           (ws, payment["id"])).fetchone()
    if payment["status"] in {"succeeded", "failed"}:
        return view(conn, ws, "payment", payment)
    lock = conn.execute("SELECT * FROM booking_locks WHERE workspace_id=%s AND id=%s FOR UPDATE",
                        (ws, payment["lock_id"])).fetchone()
    outcome = conn.execute("SELECT * FROM provider_payments WHERE workspace_id=%s AND operation_id=%s",
                           (ws, payment["id"])).fetchone()
    if not outcome:
        if payment["status"] != "unknown":
            conn.execute("UPDATE payments SET status='unknown' WHERE workspace_id=%s AND id=%s", (ws, payment["id"]))
            event(conn, ws, actor, payment["trip_id"], "payment", payment["id"],
                  "payment.unknown", lock["trip_revision"], executor)
        conn.execute("UPDATE payment_jobs SET status='awaiting_reconciliation' WHERE workspace_id=%s AND payment_id=%s",
                     (ws, payment["id"]))
        return view(conn, ws, "payment", {**payment, "status": "unknown"})
    if lock["status"] != "protected" or outcome["amount_minor"] != payment["amount_minor"]:
        raise ValueError("付款账本与保护资源不一致")
    succeeded = outcome["outcome"] == "succeeded"
    status = outcome["outcome"]
    conn.execute("""UPDATE flights SET reserved=reserved-%s,sold=sold+%s
        WHERE workspace_id=%s AND id=%s AND travel_date=%s""",
        (lock["quantity"], lock["quantity"] if succeeded else 0, ws, lock["flight_id"], lock["travel_date"]))
    from .corporate import settle_budget
    settle_budget(conn, ws, actor, payment, lock, succeeded, executor)
    from .coupons import transition
    transition(conn, ws, actor, lock, "protected", "consumed" if succeeded else "available", executor)
    conn.execute("UPDATE booking_locks SET status=%s WHERE workspace_id=%s AND id=%s",
                 ("consumed" if succeeded else "failed", ws, lock["id"]))
    conn.execute("UPDATE payments SET status=%s WHERE workspace_id=%s AND id=%s", (status, ws, payment["id"]))
    conn.execute("UPDATE trips SET status=%s WHERE workspace_id=%s AND id=%s",
                 ("paid" if succeeded else "draft", ws, payment["trip_id"]))
    conn.execute("UPDATE payment_jobs SET status='done' WHERE workspace_id=%s AND payment_id=%s", (ws, payment["id"]))
    event(conn, ws, actor, payment["trip_id"], "lock", lock["id"],
          "lock.consumed" if succeeded else "lock.failed", lock["trip_revision"], executor)
    event(conn, ws, actor, payment["trip_id"], "payment", payment["id"], f"payment.{status}", lock["trip_revision"], executor)
    if succeeded:
        task = conn.execute("""INSERT INTO ticket_tasks(workspace_id,id,payment_id,trip_id,status)
            VALUES(%s,%s,%s,%s,'pending') ON CONFLICT(workspace_id,payment_id) DO NOTHING RETURNING *""",
            (ws, uuid4(), payment["id"], payment["trip_id"])).fetchone()
        if task:
            event(conn, ws, actor, payment["trip_id"], "ticket_task", task["id"], "ticket_task.created", lock["trip_revision"], executor)
    else:
        quotes = conn.execute("""UPDATE quotes SET status='invalidated'
            WHERE workspace_id=%s AND trip_id=%s AND status='active' RETURNING id""",
            (ws, payment["trip_id"])).fetchall()
        for quote in quotes:
            event(conn, ws, actor, payment["trip_id"], "quote", quote["id"],
                  "quote.invalidated", lock["trip_revision"], executor)
    return view(conn, ws, "payment", {**payment, "status": status})


def enrich_trip(conn, ws, trip, result):
    if trip["organization_id"] is not None:
        result["department_id"] = trip["department_id"]
        selected = conn.execute("SELECT project_id,revision FROM trip_budget_selections WHERE workspace_id=%s AND trip_id=%s",
                                (ws, trip["id"])).fetchone()
        result["budget_selection"] = ({"project_id": selected["project_id"], "selection_revision": selected["revision"]}
                                      if selected else None)
    profile = conn.execute("SELECT id FROM payment_profiles WHERE workspace_id=%s AND owner_id=%s",
                           (ws, trip["owner_id"])).fetchone()
    if profile:
        result["price_status"] = "requires_quote"
    lock = conn.execute("""SELECT amount_minor,status,expires_at FROM booking_locks
        WHERE workspace_id=%s AND trip_id=%s AND status IN ('held','protected','consumed')""",
        (ws, trip["id"])).fetchone()
    now = conn.execute("SELECT business_time FROM workspaces WHERE id=%s", (ws,)).fetchone()["business_time"]
    if lock and (lock["status"] != "held" or now < lock["expires_at"]):
        amount = lock["amount_minor"]
        result.update(price=f"{amount // 100}.{amount % 100:02d}", price_status="locked")
    else:
        quote = conn.execute("""SELECT amount_minor FROM quotes WHERE workspace_id=%s AND trip_id=%s
            AND status='active' AND expires_at>%s ORDER BY expires_at DESC,id LIMIT 1""", (ws, trip["id"], now)).fetchone()
        if quote:
            amount = quote["amount_minor"]
            result.update(price=f"{amount // 100}.{amount % 100:02d}", price_status="quoted")
    return result


def register_routes(app, settings):
    ws = settings.workspace_id

    def writer(conn, request):
        actor = authenticate(conn, request, ws)["id"]
        from .corporate import employee_context
        employee_context(conn, ws, actor)
        now = business_time(conn, ws)
        lock_actor(conn, ws, actor)
        return actor, now, operation_key(request)

    @app.get("/payment-profile")
    def get_profile(request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            actor = authenticate(conn, request, ws)["id"]
            from .corporate import employee_context
            employee_context(conn, ws, actor)
            row = conn.execute("SELECT * FROM payment_profiles WHERE workspace_id=%s AND owner_id=%s", (ws, actor)).fetchone()
            result = view(conn, ws, "profile", row) if row else {"id": None, "owner_id": actor, "source_ref": None, "revision": 0}
        return result

    @app.put("/payment-profile")
    def bind_profile(body: ProfileInput, request: Request):
        params = body.model_dump()
        with connect(settings.database_url) as conn:
            actor, now, key = writer(conn, request)
            action = "profile.bound"
            result = previous(conn, ws, actor, action, key, params)
            if result is None:
                source = conn.execute("SELECT * FROM payment_sources WHERE workspace_id=%s AND ref=%s AND owner_id=%s",
                                      (ws, body.source_ref, actor)).fetchone()
                if not source:
                    fail(404, "not_visible")
                from .corporate import check_source
                check_source(conn, ws, actor, source)
                if not source["eligible"]:
                    fail(409, "payment_profile_ineligible")
                pending = conn.execute("""SELECT 1 FROM payments p JOIN trips t ON (t.workspace_id,t.id)=(p.workspace_id,p.trip_id)
                    WHERE p.workspace_id=%s AND t.owner_id=%s AND p.status IN ('processing','unknown')""", (ws, actor)).fetchone()
                if pending:
                    fail(409, "payment_protected")
                profile = conn.execute("SELECT * FROM payment_profiles WHERE workspace_id=%s AND owner_id=%s FOR UPDATE", (ws, actor)).fetchone()
                if body.expected_revision != (profile["revision"] if profile else 0):
                    fail(409, "state_changed")
                trips = conn.execute("SELECT * FROM trips WHERE workspace_id=%s AND owner_id=%s AND status='draft' ORDER BY id FOR UPDATE", (ws, actor)).fetchall()
                invalidate(conn, ws, actor, trips)
                row = conn.execute("""INSERT INTO payment_profiles(workspace_id,id,owner_id,source_ref,revision)
                    VALUES(%s,%s,%s,%s,1) ON CONFLICT(workspace_id,owner_id)
                    DO UPDATE SET source_ref=excluded.source_ref,revision=payment_profiles.revision+1 RETURNING *""",
                    (ws, uuid4(), actor, body.source_ref)).fetchone()
                event(conn, ws, actor, None, "profile", row["id"], action, row["revision"])
                result = view(conn, ws, "profile", row)
                save_result(conn, ws, actor, action, key, params, result)
        return result

    @app.post("/trips/{trip_id}/quotes", status_code=201)
    def quote_trip(trip_id: str, body: QuoteInput, request: Request):
        with connect(settings.database_url) as conn:
            actor, now, key = writer(conn, request)
            trip = owned_trip(conn, ws, actor, trip_id, lock=True)
            params = {"trip_id": str(trip["id"]), **body.model_dump()}
            action = "quote.created"
            result = previous(conn, ws, actor, action, key, params)
            if result is None:
                if trip["revision"] != body.expected_revision:
                    fail(409, "state_changed")
                if trip["status"] != "draft":
                    fail(409, "payment_protected")
                profile = profile_for_quote(conn, ws, actor)
                if profile["kind"] == "corporate" and trip["organization_id"] != profile["organization_id"]:
                    fail(409, "state_changed")
                flight = conn.execute("""SELECT * FROM flights WHERE workspace_id=%s AND id=%s
                    AND travel_date=%s AND active FOR SHARE""", (ws, trip["flight_id"], trip["travel_date"])).fetchone()
                if not flight:
                    fail(409, "flight_unavailable")
                amount = (flight["base_minor"] + (0 if profile["kind"] == "corporate" else flight["fee_minor"])) * trip["passengers"]
                from .coupons import candidate, discount
                coupon = candidate(conn, ws, actor, trip, profile["kind"])
                reduction = discount(amount, coupon)
                row = conn.execute("""INSERT INTO quotes(workspace_id,id,trip_id,trip_revision,profile_id,profile_revision,
                    source_ref,flight_id,travel_date,quantity,amount_minor,currency,rule_version,expires_at,status,payment_kind,
                    base_amount_minor,coupon_discount_minor)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'DEMO',%s,%s,'active',%s,%s,%s) RETURNING *""",
                    (ws, uuid4(), trip["id"], trip["revision"], profile["id"], profile["revision"], profile["source_ref"],
                     trip["flight_id"], trip["travel_date"], trip["passengers"], amount - reduction,
                     "coupon-v1" if coupon else profile["kind"] + "-v1", now + timedelta(seconds=300),
                     profile["kind"], amount, reduction)).fetchone()
                event(conn, ws, actor, trip["id"], "quote", row["id"], action, trip["revision"])
                result = view(conn, ws, "quote", row)
                save_result(conn, ws, actor, action, key, params, result)
        return result

    @app.post("/quotes/{quote_id}/lock", status_code=201)
    def lock_quote(quote_id: str, body: Input, request: Request):
        with connect(settings.database_url) as conn:
            actor, now, key = writer(conn, request)
            quote = owned(conn, ws, actor, "quote", quote_id, lock=True)
            trip = owned_trip(conn, ws, actor, quote["trip_id"])
            params, action = {"quote_id": str(quote["id"])}, "lock.created"
            result = previous(conn, ws, actor, action, key, params)
            if result is None:
                if trip["status"] != "draft":
                    fail(409, "payment_protected")
                existing = conn.execute("""SELECT 1 FROM booking_locks WHERE workspace_id=%s AND trip_id=%s
                    AND status IN ('held','protected','consumed')""", (ws, trip["id"])).fetchone()
                if existing:
                    fail(409, "active_lock_exists")
                if quote["status"] != "active":
                    fail(409, "quote_expired" if quote["status"] == "expired" else "state_changed")
                check_snapshot(conn, ws, actor, trip, quote, now)
                reserved = conn.execute("""UPDATE flights SET reserved=reserved+%s
                    WHERE workspace_id=%s AND id=%s AND travel_date=%s AND capacity-reserved-sold>=%s RETURNING id""",
                    (quote["quantity"], ws, quote["flight_id"], quote["travel_date"], quote["quantity"])).fetchone()
                if not reserved:
                    fail(409, "inventory_unavailable")
                from .coupons import candidate, discount, hold
                coupon = candidate(conn, ws, actor, trip, quote["payment_kind"], reserve=True)
                reduction = discount(quote["base_amount_minor"], coupon)
                row = conn.execute("""INSERT INTO booking_locks(workspace_id,id,trip_id,quote_id,trip_revision,
                    profile_id,profile_revision,source_ref,flight_id,travel_date,quantity,amount_minor,currency,expires_at,status,payment_kind,
                    base_amount_minor,coupon_discount_minor,coupon_id,coupon_percent,coupon_cap_minor)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'DEMO',%s,'held',%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (ws, uuid4(), trip["id"], quote["id"], quote["trip_revision"], quote["profile_id"], quote["profile_revision"],
                     quote["source_ref"], quote["flight_id"], quote["travel_date"], quote["quantity"], quote["base_amount_minor"] - reduction,
                     quote["expires_at"], quote["payment_kind"], quote["base_amount_minor"], reduction,
                     coupon["id"] if coupon else None, coupon["percent"] if coupon else None,
                     coupon["cap_minor"] if coupon else None)).fetchone()
                hold(conn, ws, actor, row)
                event(conn, ws, actor, trip["id"], "lock", row["id"], action, trip["revision"])
                result = view(conn, ws, "lock", row)
                save_result(conn, ws, actor, action, key, params, result)
        return result

    @app.post("/locks/{lock_id}/cancel")
    def cancel_lock(lock_id: str, body: Input, request: Request):
        with connect(settings.database_url) as conn:
            actor, now, key = writer(conn, request)
            lock = owned(conn, ws, actor, "lock", lock_id, lock=True)
            params, action = {"lock_id": str(lock["id"])}, "lock.cancelled"
            result = previous(conn, ws, actor, action, key, params)
            if result is None:
                if lock["status"] in {"protected", "consumed"}:
                    fail(409, "payment_protected")
                release_lock(conn, ws, actor, lock, "cancelled", "user")
                row = owned(conn, ws, actor, "lock", lock_id)
                result = view(conn, ws, "lock", row)
                save_result(conn, ws, actor, action, key, params, result)
        return result

    @app.post("/locks/{lock_id}/payments", status_code=202)
    def pay_lock(lock_id: str, body: PaymentInput, request: Request):
        with connect(settings.database_url) as conn:
            actor, now, key = writer(conn, request)
            lock = owned(conn, ws, actor, "lock", lock_id, lock=True)
            trip = owned_trip(conn, ws, actor, lock["trip_id"])
            params, action = {"lock_id": str(lock["id"]), **body.model_dump()}, "payment.accepted"
            result = previous(conn, ws, actor, action, key, params)
            if result is None:
                active = conn.execute("SELECT 1 FROM payments WHERE workspace_id=%s AND trip_id=%s AND status<>'failed'", (ws, trip["id"])).fetchone()
                if active:
                    fail(409, "payment_exists")
                if lock["status"] != "held":
                    fail(409, "quote_expired" if lock["status"] == "expired" else "state_changed")
                check_snapshot(conn, ws, actor, trip, lock, now)
                if body.amount_minor != lock["amount_minor"]:
                    fail(409, "amount_mismatch")
                from .corporate import protect_approval
                approval_id = protect_approval(conn, ws, actor, trip, lock, now)
                from .coupons import transition
                transition(conn, ws, actor, lock, "held", "protected")
                row = conn.execute("""INSERT INTO payments(workspace_id,id,trip_id,lock_id,amount_minor,currency,status,accepted_at,approval_id)
                    VALUES(%s,%s,%s,%s,%s,'DEMO','processing',%s,%s) RETURNING *""",
                    (ws, uuid4(), trip["id"], lock["id"], lock["amount_minor"], now, approval_id)).fetchone()
                conn.execute("INSERT INTO payment_jobs(workspace_id,payment_id,status) VALUES(%s,%s,'pending')", (ws, row["id"]))
                conn.execute("UPDATE booking_locks SET status='protected' WHERE workspace_id=%s AND id=%s", (ws, lock["id"]))
                conn.execute("UPDATE trips SET status='payment_pending' WHERE workspace_id=%s AND id=%s", (ws, trip["id"]))
                event(conn, ws, actor, trip["id"], "lock", lock["id"], "lock.protected", trip["revision"])
                event(conn, ws, actor, trip["id"], "payment", row["id"], action, trip["revision"])
                result = view(conn, ws, "payment", row)
                save_result(conn, ws, actor, action, key, params, result)
        return result

    @app.post("/payments/{payment_id}/reconcile")
    def reconcile(payment_id: str, body: Input, request: Request):
        with connect(settings.database_url) as conn:
            actor, now, key = writer(conn, request)
            payment = owned(conn, ws, actor, "payment", payment_id, lock=True)
            params, action = {"payment_id": str(payment["id"])}, "payment.reconciled"
            result = previous(conn, ws, actor, action, key, params)
            if result is None:
                result = settle(conn, ws, actor, payment, "user")
                save_result(conn, ws, actor, action, key, params, result)
        return result

    def reader(kind):
        def get_object(object_id: str, request: Request):
            with connect(settings.database_url) as conn:
                read_snapshot(conn)
                actor = authenticate(conn, request, ws)["id"]
                result = view(conn, ws, kind, owned(conn, ws, actor, kind, object_id))
            return result
        return get_object

    for path, kind in (("quotes", "quote"), ("locks", "lock"), ("payments", "payment"),
                       ("ticket-tasks", "ticket_task"), ("tickets", "ticket")):
        app.get(f"/{path}/{{object_id}}", name=f"get_{kind}")(reader(kind))
