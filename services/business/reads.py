"""薄网页所需的主体白名单读取；复用授权与视图，不提供工程控制。"""

from fastapi import Request

from .app import authenticate, owned_trip, trip_view
from .corporate import employee_context, approval_view, authorize_approval
from .db import connect, read_snapshot
from .personal import TABLES, enrich_trip, view


def latest(conn, ws, trip_id, kind, action):
    table = {**TABLES, "approval": "approval_requests"}[kind]
    return conn.execute(f"""SELECT o.* FROM {table} o
        JOIN business_events e ON (e.workspace_id,e.object_id)=(o.workspace_id,o.id)
        WHERE o.workspace_id=%s AND o.trip_id=%s AND e.object_type=%s AND e.action=%s
        ORDER BY e.seq DESC LIMIT 1""", (ws, trip_id, kind, action)).fetchone()


def register_routes(app, settings):
    ws = settings.workspace_id

    @app.get("/booking-options")
    def options(request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            actor = authenticate(conn, request, ws)["id"]
            member = employee_context(conn, ws, actor)
            flights = conn.execute("SELECT id,travel_date FROM flights WHERE workspace_id=%s AND active ORDER BY id,travel_date", (ws,)).fetchall()
            sources = conn.execute("""SELECT ref,kind FROM payment_sources
                WHERE workspace_id=%s AND owner_id=%s AND eligible
                AND (kind='personal' OR organization_id=%s) ORDER BY ref""",
                (ws, actor, member["organization_id"] if member else None)).fetchall()
            return {"flights": [{"id": f["id"], "travel_date": f["travel_date"].isoformat()} for f in flights],
                    "sources": sources}

    @app.get("/trips")
    def trips(request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            actor = authenticate(conn, request, ws)["id"]
            member = employee_context(conn, ws, actor)
            rows = conn.execute("""SELECT * FROM trips WHERE workspace_id=%s AND owner_id=%s
                AND (organization_id IS NULL OR (organization_id=%s AND department_id=%s)) ORDER BY id""",
                (ws, actor, member["organization_id"] if member else None,
                 member["department_id"] if member else None)).fetchall()
            return {"trips": [enrich_trip(conn, ws, row, trip_view(row)) for row in rows]}

    @app.get("/trips/{trip_id}/booking")
    def booking(trip_id: str, request: Request):
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            actor = authenticate(conn, request, ws)["id"]
            trip = owned_trip(conn, ws, actor, trip_id)
            result = {"trip": enrich_trip(conn, ws, trip, trip_view(trip))}
            for kind, action in (("quote", "quote.created"), ("lock", "lock.created"),
                                 ("payment", "payment.accepted"), ("ticket_task", "ticket_task.created"),
                                 ("ticket", "ticket.issued"), ("approval", "approval.submitted")):
                row = latest(conn, ws, trip["id"], kind, action)
                if row is not None and kind == "approval":
                    authorize_approval(conn, ws, actor, row)
                    result[kind] = approval_view(conn, ws, row)
                else:
                    result[kind] = view(conn, ws, kind, row) if row is not None else None
        return result
