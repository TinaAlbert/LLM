"""开发者 CLI：迁移、固定初态及严格限定的测试重置；没有 HTTP 控制面。"""

import argparse
import hashlib
from pathlib import Path
from uuid import uuid4

import psycopg

from .config import load_settings, WORKSPACE_FIXTURES
from .db import connect

PROJECT = "llm-business-d1"
DATABASES = {"llm_business_dev": "demo", "llm_business_test": "test"}
MIGRATIONS = Path(__file__).with_name("migrations")


def database_purpose(conn):
    name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    if name not in DATABASES:
        raise ValueError("拒绝未知数据库；只允许项目专属 dev/test 库")
    return DATABASES[name]


def check_marker(conn, purpose):
    if not conn.execute("SELECT to_regclass('public.project_meta') AS name").fetchone()["name"]:
        raise ValueError("缺少项目数据库标记")
    rows = conn.execute("SELECT project, purpose FROM project_meta").fetchall()
    if rows != [{"project": PROJECT, "purpose": purpose}]:
        raise ValueError("项目数据库标记不匹配")


def migrate(database_url: str) -> list[str]:
    applied = []
    with connect(database_url) as conn:
        purpose = database_purpose(conn)
        conn.execute("SELECT pg_advisory_xact_lock(74012001)")
        existing = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        names = {row["tablename"] for row in existing}
        if names:
            if "schema_migrations" not in names:
                raise ValueError("数据库已有未知表，拒绝初始化")
            check_marker(conn, purpose)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
        """)
        history = {
            row["version"]: row["checksum"]
            for row in conn.execute("SELECT version, checksum FROM schema_migrations")
        }
        files = sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
        if set(history) - {path.name for path in files}:
            raise ValueError("存在当前代码不认识的迁移版本")
        for path in files:
            content = path.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            if path.name in history:
                if history[path.name] != checksum:
                    raise ValueError("已执行的迁移内容发生变化，必须新增迁移")
                continue
            conn.execute(content.decode("utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
            applied.append(path.name)
        if not names:
            conn.execute(
                "INSERT INTO project_meta(project, purpose) VALUES (%s, %s)",
                (PROJECT, purpose),
            )
    return applied


def check_workspace(purpose, workspace_id):
    if workspace_id not in WORKSPACE_FIXTURES or ((workspace_id == 'test') != (purpose == 'test')):
        raise ValueError("工作空间与数据库用途不匹配")


def _seed(conn, workspace_id, fixture=None):
    configured = WORKSPACE_FIXTURES[workspace_id]
    existing = conn.execute('SELECT fixture_key FROM workspaces WHERE id=%s FOR UPDATE', (workspace_id,)).fetchone()
    fixture = fixture or configured or (existing['fixture_key'] if existing else 'single-trip')
    if fixture not in {'single-trip', 'two-trips-one-coupon'} or (configured and fixture != configured):
        raise ValueError('工作空间夹具不匹配')
    if existing and existing['fixture_key'] != fixture:
        raise ValueError('seed不能更改已有工作空间夹具')
    conn.execute(
        """INSERT INTO workspaces(id, business_time, product_version,fixture_key)
           VALUES (%s, '2026-09-27T00:00:00Z', 'v1',%s) ON CONFLICT DO NOTHING""",
        (workspace_id,fixture),
    )
    for account, name in (
        ("personal_alice", "Alice"), ("personal_bob", "Bob"),
        ("employee_chen", "Chen"), ("employee_ding", "Ding"),
        ("approver_lin", "Lin"), ("employee_fang", "Fang"),
        ("approver_wu", "Wu"), ("approver_zhao", "Zhao"), ("approver_qian", "Qian"),
    ):
        conn.execute(
            """INSERT INTO users(workspace_id, id, display_name)
               VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
            (workspace_id, account, name),
        )
    for organization_id in ("org-A", "org-B"):
        conn.execute("""INSERT INTO organizations(workspace_id,id)
                        VALUES(%s,%s) ON CONFLICT DO NOTHING""",
                     (workspace_id, organization_id))
    for organization_id, department_id in (("org-A", "eng"), ("org-A", "sales"), ("org-B", "eng")):
        conn.execute("""INSERT INTO departments(workspace_id,organization_id,id)
                        VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                     (workspace_id, organization_id, department_id))
    for account, organization_id, department_id, employee, approver in (
        ("employee_chen", "org-A", "eng", True, True),
        ("employee_ding", "org-A", "eng", True, False),
        ("approver_lin", "org-A", "eng", False, True),
        ("employee_fang", "org-B", "eng", True, False),
        ("approver_wu", "org-A", "sales", False, True),
        ("approver_zhao", "org-B", "eng", False, True),
        ("approver_qian", "org-A", "eng", False, True),
    ):
        conn.execute("""INSERT INTO memberships
                        (workspace_id,user_id,organization_id,department_id,employee,approver)
                        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                     (workspace_id, account, organization_id, department_id, employee, approver))
    for applicant_id, approver_id in (
        ("employee_chen", "approver_lin"), ("employee_ding", "approver_lin"),
        ("employee_fang", "approver_zhao"),
    ):
        conn.execute("""INSERT INTO approval_assignments(workspace_id,applicant_id,approver_id)
                        VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                     (workspace_id, applicant_id, approver_id))
    for project_id, organization_id, department_id in (
        ("A-eng", "org-A", "eng"), ("A-eng-alt", "org-A", "eng"),
        ("A-sales", "org-A", "sales"), ("B-eng", "org-B", "eng"),
    ):
        conn.execute("""INSERT INTO budget_projects
                        (workspace_id,id,organization_id,department_id,total_minor,policy_revision)
                        VALUES(%s,%s,%s,%s,200000,1) ON CONFLICT DO NOTHING""",
                     (workspace_id, project_id, organization_id, department_id))
    for ref, owner in (("demo_personal_alice", "personal_alice"),
                       ("demo_personal_alice_alt", "personal_alice"),
                       ("demo_personal_bob", "personal_bob")):
        conn.execute("""INSERT INTO payment_sources(workspace_id,ref,owner_id,kind,eligible)
                        VALUES(%s,%s,%s,'personal',true) ON CONFLICT DO NOTHING""",
                     (workspace_id, ref, owner))
    for ref, owner, organization_id in (
        ("demo_corporate_chen", "employee_chen", "org-A"),
        ("demo_corporate_chen_alt", "employee_chen", "org-A"),
        ("demo_corporate_ding", "employee_ding", "org-A"),
        ("demo_corporate_fang", "employee_fang", "org-B"),
    ):
        conn.execute("""INSERT INTO payment_sources
                        (workspace_id,ref,owner_id,kind,eligible,organization_id)
                        VALUES(%s,%s,%s,'corporate',true,%s) ON CONFLICT DO NOTHING""",
                     (workspace_id, ref, owner, organization_id))
    if fixture == 'two-trips-one-coupon':
        for flight_id, amount in (('DEMO-LARGE', 100000), ('DEMO-SMALL', 60000)):
            conn.execute("""INSERT INTO flights(workspace_id,id,travel_date,base_minor,fee_minor,capacity)
                            VALUES(%s,%s,'2026-10-01',%s,0,2) ON CONFLICT DO NOTHING""",
                         (workspace_id, flight_id, amount))
        conn.execute("""INSERT INTO coupons(workspace_id,id,owner_id,percent,cap_minor)
                        VALUES(%s,%s,'personal_alice',20,20000) ON CONFLICT DO NOTHING""", (workspace_id, uuid4()))
        return
    for day in ("2026-10-01", "2026-10-02"):
        conn.execute("""INSERT INTO flights(workspace_id,id,travel_date,base_minor,fee_minor,capacity)
                        VALUES(%s,'DEMO-PEK-SHA',%s,100000,10000,2) ON CONFLICT DO NOTHING""",
                     (workspace_id, day))


def seed(database_url: str, workspace_id: str, *, fixture=None):
    with connect(database_url) as conn:
        purpose = database_purpose(conn)
        check_marker(conn, purpose)
        check_workspace(purpose, workspace_id)
        conn.execute("SELECT pg_advisory_xact_lock(74012001)")
        _seed(conn, workspace_id, fixture)


def reset_test_database(database_url: str, confirmation: str = "", *, fixture='single-trip'):
    if confirmation != "llm_business_test":
        raise ValueError("须明确确认 llm_business_test")
    with connect(database_url) as conn:
        purpose = database_purpose(conn)
        if purpose != "test":
            raise ValueError("拒绝重置非测试库")
        conn.execute("SELECT pg_advisory_xact_lock(74012001)")
        check_marker(conn, "test")
        conn.execute("""LOCK TABLE workspaces, users, sessions, trips, business_events, idempotency,
                        payment_sources,payment_profiles,flights,quotes,booking_locks,payments,
                        payment_jobs,provider_payments,ticket_tasks,tickets,
                        organizations,departments,memberships,approval_assignments,budget_projects,
                        trip_budget_selections,approval_requests,coupons IN ACCESS EXCLUSIVE MODE""")
        workspaces = conn.execute("SELECT id FROM workspaces").fetchall()
        if workspaces != [{"id": "test"}]:
            raise ValueError("拒绝重置未知或尚未初始化的工作空间")
        conn.execute("""TRUNCATE business_events,idempotency,trips,sessions,users,workspaces,
                        payment_sources,payment_profiles,flights,quotes,booking_locks,payments,
                        payment_jobs,provider_payments,ticket_tasks,tickets,
                        organizations,departments,memberships,approval_assignments,budget_projects,
                        trip_budget_selections,approval_requests,coupons RESTART IDENTITY""")
        _seed(conn, "test", fixture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["migrate", "seed", "reset-test"])
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--fixture", choices=['single-trip', 'two-trips-one-coupon'])
    args = parser.parse_args()
    settings = load_settings(test=args.test)
    try:
        if args.action == "migrate":
            versions = migrate(settings.database_url)
            print("已应用迁移：", ", ".join(versions) or "无新增")
        elif args.action == "seed":
            seed(settings.database_url, settings.workspace_id, fixture=args.fixture)
            print("固定初态已补齐；既有数据保留")
        else:
            if not args.test:
                raise ValueError("重置还需要 --test")
            reset_test_database(settings.database_url, args.confirm, fixture=args.fixture or 'single-trip')
            print("仅 llm_business_test/test 已重置")
    except (psycopg.Error, ValueError) as exc:
        # 连接错误可能含凭证/主机信息；不原样打印。
        if isinstance(exc, psycopg.Error):
            parser.exit(1, "PostgreSQL 操作失败；检查本地连接配置与实例状态。\n")
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
