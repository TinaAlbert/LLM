"""开发者控制的持久任务与虚拟时间；不注册 HTTP 接口、不调用真实供应商。"""

from datetime import timedelta
from uuid import UUID, uuid4

from . import db, personal
from .manage import check_marker, database_purpose, check_workspace


def _object_id(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("对象 ID 必须是有效 UUID") from exc


def _context(conn, workspace):
    purpose = database_purpose(conn)
    check_marker(conn, purpose)
    check_workspace(purpose, workspace)


def _payment_context(conn, workspace, payment_id):
    initial = conn.execute(
        """SELECT t.owner_id, t.id AS trip_id FROM payments p
           JOIN trips t ON t.workspace_id=p.workspace_id AND t.id=p.trip_id
           WHERE p.workspace_id=%s AND p.id=%s""",
        (workspace, payment_id),
    ).fetchone()
    if initial is None:
        raise ValueError("付款对象不存在")
    actor = initial["owner_id"]
    db.lock_actor(conn, workspace, actor)
    trip = conn.execute(
        "SELECT * FROM trips WHERE workspace_id=%s AND id=%s FOR UPDATE",
        (workspace, initial["trip_id"]),
    ).fetchone()
    payment = conn.execute(
        "SELECT * FROM payments WHERE workspace_id=%s AND id=%s FOR UPDATE",
        (workspace, payment_id),
    ).fetchone()
    lock = conn.execute(
        "SELECT * FROM booking_locks WHERE workspace_id=%s AND id=%s FOR UPDATE",
        (workspace, payment["lock_id"]),
    ).fetchone()
    if lock is None or lock["trip_id"] != trip["id"]:
        raise ValueError("付款与锁价快照不一致")
    return actor, trip, payment, lock


def process_payment(database_url, workspace, payment_id, outcome="succeeded"):
    """消费原付款任务；稳定供应商 operation_id 保证结果重试不产生新扣款。"""
    if not isinstance(outcome, str) or outcome not in {"succeeded", "failed", "lost_response", "unknown"}:
        raise ValueError("不支持的模拟付款结果")
    payment_id = _object_id(payment_id)
    with db.connect(database_url) as conn:
        _context(conn, workspace)
        db.business_time(conn, workspace)
        actor, trip, payment, lock = _payment_context(conn, workspace, payment_id)
        if payment["status"] in {"succeeded", "failed"}:
            return personal.view(conn, workspace, "payment", payment)
        if lock["status"] != "protected" or trip["status"] != "payment_pending":
            raise ValueError("付款未持有受保护的库存快照")
        job = conn.execute(
            "SELECT * FROM payment_jobs WHERE workspace_id=%s AND payment_id=%s FOR UPDATE",
            (workspace, payment_id),
        ).fetchone()
        if job is None or job["status"] == "done":
            raise ValueError("付款任务状态不一致")

        # unknown 表示供应商尚无确定结果；lost_response 则已扣款但响应未送达。
        # 这里不读可变的 payment_profiles，更不接受用户传入新的金额/付款来源。
        if outcome != "unknown":
            provider_outcome = "failed" if outcome == "failed" else "succeeded"
            captured = payment["amount_minor"] if provider_outcome == "succeeded" else 0
            inserted = conn.execute(
                """INSERT INTO provider_payments
                       (workspace_id,operation_id,outcome,amount_minor,captured_minor)
                   VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING operation_id""",
                (workspace, payment_id, provider_outcome, payment["amount_minor"], captured),
            ).fetchone()
            if inserted is not None:
                personal.event(
                    conn, workspace, actor, trip["id"], "payment", payment_id,
                    "payment.provider_recorded", lock["trip_revision"], executor="worker",
                )

        if outcome in {"unknown", "lost_response"}:
            if payment["status"] != "unknown":
                payment = conn.execute(
                    """UPDATE payments SET status='unknown'
                       WHERE workspace_id=%s AND id=%s RETURNING *""",
                    (workspace, payment_id),
                ).fetchone()
                personal.event(
                    conn, workspace, actor, trip["id"], "payment", payment_id,
                    "payment.unknown", lock["trip_revision"], executor="worker",
                )
            conn.execute(
                """UPDATE payment_jobs SET status='awaiting_reconciliation'
                   WHERE workspace_id=%s AND payment_id=%s""",
                (workspace, payment_id),
            )
            return personal.view(conn, workspace, "payment", payment)

        # 再次处理时只收敛已持久的供应商结果；新注入的 outcome 不覆盖旧账本。
        personal.settle(conn, workspace, actor, payment, executor="worker")
        payment = conn.execute(
            "SELECT * FROM payments WHERE workspace_id=%s AND id=%s",
            (workspace, payment_id),
        ).fetchone()
        return personal.view(conn, workspace, "payment", payment)


def process_ticket(database_url, workspace, task_id, outcome="succeeded"):
    """一次成功付款的唯一出票任务；失败是明确终态，不自动退款或重付。"""
    if not isinstance(outcome, str) or outcome not in {"succeeded", "failed"}:
        raise ValueError("不支持的模拟出票结果")
    task_id = _object_id(task_id)
    with db.connect(database_url) as conn:
        _context(conn, workspace)
        db.business_time(conn, workspace)
        initial = conn.execute(
            "SELECT payment_id FROM ticket_tasks WHERE workspace_id=%s AND id=%s",
            (workspace, task_id),
        ).fetchone()
        if initial is None:
            raise ValueError("出票任务不存在")
        actor, trip, payment, lock = _payment_context(conn, workspace, initial["payment_id"])
        task = conn.execute(
            "SELECT * FROM ticket_tasks WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace, task_id),
        ).fetchone()
        if task["status"] in {"succeeded", "failed"}:
            return personal.view(conn, workspace, "ticket_task", task)
        if (payment["status"] != "succeeded" or lock["status"] != "consumed"
                or trip["status"] != "paid" or task["trip_id"] != trip["id"]
                or trip["revision"] != lock["trip_revision"]):
            raise ValueError("出票所需的成功付款与冻结行程快照不一致")
        if outcome == "succeeded":
            ticket_id = uuid4()
            conn.execute(
                """INSERT INTO tickets
                       (workspace_id,id,task_id,payment_id,trip_id,trip_revision,quantity,certificate)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (workspace, ticket_id, task_id, payment["id"], trip["id"],
                 lock["trip_revision"], lock["quantity"], "DEMO-" + ticket_id.hex.upper()),
            )
            personal.event(
                conn, workspace, actor, trip["id"], "ticket", ticket_id,
                "ticket.issued", lock["trip_revision"], executor="worker",
            )
        task = conn.execute(
            """UPDATE ticket_tasks SET status=%s
               WHERE workspace_id=%s AND id=%s RETURNING *""",
            (outcome, workspace, task_id),
        ).fetchone()
        personal.event(
            conn, workspace, actor, trip["id"], "ticket_task", task_id,
            "ticket_task." + outcome, lock["trip_revision"], executor="worker",
        )
        return personal.view(conn, workspace, "ticket_task", task)


def advance_time(database_url, workspace, seconds):
    """显式前移固定业务时钟；与业务事务的 FOR SHARE 锁互斥。"""
    if type(seconds) is not int or seconds < 0:
        raise ValueError("虚拟时间增量必须为非负整数秒")
    with db.connect(database_url) as conn:
        _context(conn, workspace)
        row = conn.execute(
            "SELECT business_time FROM workspaces WHERE id=%s FOR UPDATE", (workspace,),
        ).fetchone()
        if row is None:
            raise ValueError("工作空间不存在")
        try:
            changed = row["business_time"] + timedelta(seconds=seconds)
        except OverflowError as exc:
            raise ValueError("虚拟时间超出支持范围") from exc
        conn.execute("UPDATE workspaces SET business_time=%s WHERE id=%s", (changed, workspace))
        return {"workspace_id": workspace, "business_time": changed, "advanced_seconds": seconds}


def expire(database_url, workspace):
    """清理恰好到期的预报价及未付款锁；受保护/已消费库存始终保留。"""
    with db.connect(database_url) as conn:
        _context(conn, workspace)
        now = db.business_time(conn, workspace)
        owners = conn.execute(
            """SELECT DISTINCT t.owner_id FROM trips t
               WHERE t.workspace_id=%s AND (
                   EXISTS(SELECT 1 FROM quotes q WHERE q.workspace_id=t.workspace_id
                          AND q.trip_id=t.id AND q.status='active' AND q.expires_at<=%s)
                   OR EXISTS(SELECT 1 FROM booking_locks b WHERE b.workspace_id=t.workspace_id
                             AND b.trip_id=t.id AND b.status='held' AND b.expires_at<=%s))
               ORDER BY t.owner_id""",
            (workspace, now, now),
        ).fetchall()
        # 先取得全部主体锁，再碰 trip/lock/flight；不持有库存锁等待新主体锁。
        for row in owners:
            db.lock_actor(conn, workspace, row["owner_id"])
        actor_ids = [row["owner_id"] for row in owners]
        trips = conn.execute(
            """SELECT * FROM trips WHERE workspace_id=%s AND owner_id=ANY(%s)
               ORDER BY owner_id,id FOR UPDATE""",
            (workspace, actor_ids),
        ).fetchall()
        trip_owners = {row["id"]: row["owner_id"] for row in trips}
        trip_ids = list(trip_owners)
        quotes = conn.execute(
            """SELECT * FROM quotes WHERE workspace_id=%s AND trip_id=ANY(%s)
               AND status='active' AND expires_at<=%s ORDER BY id FOR UPDATE""",
            (workspace, trip_ids, now),
        ).fetchall()
        locks = conn.execute(
            """SELECT * FROM booking_locks WHERE workspace_id=%s AND trip_id=ANY(%s)
               AND status='held' AND expires_at<=%s ORDER BY id FOR UPDATE""",
            (workspace, trip_ids, now),
        ).fetchall()
        for flight_id, travel_date in sorted({(row["flight_id"], row["travel_date"]) for row in locks}):
            conn.execute(
                """SELECT id FROM flights WHERE workspace_id=%s AND id=%s
                   AND travel_date=%s FOR UPDATE""",
                (workspace, flight_id, travel_date),
            )
        for quote in quotes:
            conn.execute(
                "UPDATE quotes SET status='expired' WHERE workspace_id=%s AND id=%s",
                (workspace, quote["id"]),
            )
            personal.event(
                conn, workspace, trip_owners[quote["trip_id"]], quote["trip_id"],
                "quote", quote["id"], "quote.expired", quote["trip_revision"], executor="worker",
            )
        for lock in locks:
            personal.release_lock(
                conn, workspace, trip_owners[lock["trip_id"]], lock, "expired", executor="worker",
            )
        return {"workspace_id": workspace, "business_time": now,
                "quotes_expired": len(quotes), "locks_expired": len(locks)}
