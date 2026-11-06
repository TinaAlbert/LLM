"""C03的一次性个人券；资源转移接入已有预订事务，没有独立付款引擎。"""

from uuid import UUID

from fastapi import Request

from .app import authenticate, fail
from .db import connect, read_snapshot


def eligible(conn, ws, actor, trip, payment_kind):
    # 请求字段不能赋予券资格；当前组织成员也不能套用个人优惠。
    return (payment_kind == "personal" and trip["organization_id"] is None
            and not conn.execute("SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s",
                                 (ws, actor)).fetchone()
            and conn.execute("SELECT fixture_key FROM workspaces WHERE id=%s", (ws,)).fetchone()["fixture_key"]
            == "two-trips-one-coupon")


def candidate(conn, ws, actor, trip, payment_kind, *, reserve=False):
    if not eligible(conn, ws, actor, trip, payment_kind):
        return None
    query = "SELECT * FROM coupons WHERE workspace_id=%s AND owner_id=%s"
    if reserve:
        query += " FOR UPDATE"
    row = conn.execute(query, (ws, actor)).fetchone()
    return row if row and row["status"] == "available" else None


def discount(base, coupon):
    return min(base * coupon["percent"] // 100, coupon["cap_minor"]) if coupon else 0


def hold(conn, ws, actor, booking):
    if booking["coupon_id"] is None:
        return
    row = conn.execute("""UPDATE coupons SET status='held',lock_id=%s
        WHERE workspace_id=%s AND id=%s AND owner_id=%s AND status='available' RETURNING id""",
        (booking["id"], ws, booking["coupon_id"], actor)).fetchone()
    if not row:
        raise ValueError("券归属与新锁不一致")
    _event(conn, ws, actor, booking, "held", "user")


def transition(conn, ws, actor, booking, expected, target, executor="user"):
    if booking["coupon_id"] is None:
        return
    row = conn.execute("""UPDATE coupons SET status=%s,lock_id=%s
        WHERE workspace_id=%s AND id=%s AND owner_id=%s AND lock_id=%s AND status=%s RETURNING id""",
        (target, None if target == "available" else booking["id"], ws,
         booking["coupon_id"], actor, booking["id"], expected)).fetchone()
    if not row:
        raise ValueError("券与原锁的资源状态不一致")
    _event(conn, ws, actor, booking, target, executor)


def _event(conn, ws, actor, booking, target, executor):
    from .personal import event
    event(conn, ws, actor, booking["trip_id"], "coupon", booking["coupon_id"],
          "coupon." + target, booking["trip_revision"], executor)


def view(row):
    return {"id": str(row["id"]), "percent": row["percent"], "cap_minor": row["cap_minor"],
            "currency": row["currency"], "status": row["status"]}


def register_routes(app, settings):
    ws = settings.workspace_id

    def own_rows(conn, request):
        actor = authenticate(conn, request, ws)["id"]
        if conn.execute("SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s", (ws, actor)).fetchone():
            return []
        return conn.execute("SELECT * FROM coupons WHERE workspace_id=%s AND owner_id=%s ORDER BY id",
                            (ws, actor)).fetchall()

    @app.get("/coupons")
    def list_coupons(request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            return {"coupons": [view(row) for row in own_rows(conn, request)]}

    @app.get("/coupons/{coupon_id}")
    def get_coupon(coupon_id: str, request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            rows = own_rows(conn, request)
            try:
                identifier = UUID(coupon_id)
            except ValueError:
                fail(404, "not_visible")
            row = next((row for row in rows if row["id"] == identifier), None)
            if row is None:
                fail(404, "not_visible")
            return view(row)
