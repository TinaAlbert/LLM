"""专用测试库的迁移、固定初态与拒绝未知重置目标。"""

from dataclasses import replace

from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
import pytest

from services.business.app import create_app
from services.business.db import connect
from services.business.manage import migrate, reset_test_database, seed


pytestmark = pytest.mark.integration


def snapshot(sql):
    return {
        "users": sql("SELECT workspace_id,id,display_name,active FROM users ORDER BY workspace_id,id"),
        "workspace": sql("SELECT id,business_time,product_version FROM workspaces ORDER BY id"),
        "counts": sql(
            "SELECT (SELECT count(*) FROM trips) AS trips, "
            "(SELECT count(*) FROM sessions) AS sessions, "
            "(SELECT count(*) FROM business_events) AS events, "
            "(SELECT count(*) FROM idempotency) AS operations"
        ),
    }


def test_migration_and_seed_repeat_preserve_persisted_data(settings, create, sql):
    trip = create()
    before = snapshot(sql)
    migrations = sql("SELECT * FROM schema_migrations ORDER BY version")
    assert migrations
    assert migrate(settings.database_url) == []
    seed(settings.database_url, settings.workspace_id)
    assert snapshot(sql) == before
    assert sql("SELECT * FROM schema_migrations ORDER BY version") == migrations
    assert sql("SELECT revision FROM trips WHERE id=%s", (trip["id"],)) == [{"revision": 1}]


def test_fixed_initial_state_reset_is_reproducible(settings, create, sql):
    initial = snapshot(sql)
    create()
    reset_test_database(settings.database_url, confirmation="llm_business_test")
    seed(settings.database_url, settings.workspace_id)
    # 重置后保留两个个人账户和七个企业固定账户、一个工作空间及空业务/会话表。
    first = snapshot(sql)
    reset_test_database(settings.database_url, confirmation="llm_business_test")
    seed(settings.database_url, settings.workspace_id)
    assert snapshot(sql) == first
    assert first["users"] == initial["users"]
    assert first["workspace"] == initial["workspace"]
    assert [row["id"] for row in first["users"]] == [
        "approver_lin", "approver_qian", "approver_wu", "approver_zhao",
        "employee_chen", "employee_ding", "employee_fang", "personal_alice", "personal_bob",
    ]
    assert first["counts"] == [{"trips": 0, "sessions": 0, "events": 0, "operations": 0}]


def test_reset_rejects_wrong_confirmation_without_writes(settings, create, sql):
    create()
    before = snapshot(sql)
    with pytest.raises(ValueError):
        reset_test_database(settings.database_url, confirmation="some-other-database")
    assert snapshot(sql) == before


def test_reset_rejects_wrong_project_purpose(settings, create, sql):
    create()
    # 只改变已经核实为本项目测试库的标记，绝不连接或重置另一项目。
    original = sql("SELECT project,purpose FROM project_meta")[0]
    sql("UPDATE project_meta SET purpose='demo' RETURNING singleton")
    try:
        before = snapshot(sql)
        with pytest.raises(ValueError):
            reset_test_database(settings.database_url, confirmation="llm_business_test")
        assert snapshot(sql) == before
    finally:
        sql(
            "UPDATE project_meta SET project=%s,purpose=%s RETURNING singleton",
            (original["project"], original["purpose"]),
        )


def test_reset_rejects_missing_project_marker(settings, create, sql):
    create()
    original = sql("DELETE FROM project_meta RETURNING project,purpose")[0]
    try:
        before = snapshot(sql)
        with pytest.raises(ValueError):
            reset_test_database(settings.database_url, confirmation="llm_business_test")
        assert snapshot(sql) == before
    finally:
        sql(
            "INSERT INTO project_meta(project,purpose) VALUES (%s,%s) RETURNING singleton",
            (original["project"], original["purpose"]),
        )


def test_reset_rejects_unknown_database_without_writing(settings):
    # 本项目独占的本地实例，其 postgres 默认库不在允许重置名单中。
    # 当前 reset 的第一项数据库查询是名称核验；本例不创建未知库或测试对象。
    unknown_url = make_conninfo(settings.database_url, dbname="postgres")
    with connect(unknown_url) as connection:
        before = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
    with pytest.raises(ValueError):
        reset_test_database(unknown_url, confirmation="llm_business_test")
    with connect(unknown_url) as connection:
        assert connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall() == before


def test_reset_rejects_unrecognized_workspace(settings, create, sql):
    create()
    sql(
        "INSERT INTO workspaces(id,business_time,product_version) "
        "VALUES ('unknown-workspace','2026-09-27T00:00:00Z','v1') RETURNING id"
    )
    try:
        before = snapshot(sql)
        with pytest.raises(ValueError):
            reset_test_database(settings.database_url, confirmation="llm_business_test")
        assert snapshot(sql) == before
    finally:
        sql("DELETE FROM workspaces WHERE id='unknown-workspace' RETURNING id")


def test_demo_login_requires_explicit_local_switch(settings):
    disabled = replace(settings, mode="local", demo_login=False)
    with TestClient(create_app(disabled)) as client:
        assert client.post("/demo/sessions", json={"account": "personal_alice"}).status_code == 404


def test_token_from_another_workspace_is_rejected(settings, alice):
    other_workspace = replace(settings, workspace_id="demo")
    with TestClient(create_app(other_workspace)) as client:
        response = client.get("/session", headers=alice)
    assert response.status_code == 401
    assert response.json() == {"error": "authentication_required"}
