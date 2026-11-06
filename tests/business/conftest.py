"""所有业务验收均连接专属 PostgreSQL；不可用时失败，不回退或跳过。"""

import pytest
import psycopg
from fastapi.testclient import TestClient

from services.business.app import create_app
from services.business.config import load_settings
from services.business.db import connect
from services.business.manage import migrate, reset_test_database, seed


@pytest.fixture(scope="session")
def settings():
    configured = load_settings(test=True)
    assert configured.mode == "test"
    assert configured.workspace_id == "test"
    try:
        migrate(configured.database_url)
        seed(configured.database_url, configured.workspace_id)
    except psycopg.Error as error:
        # pytest 的完整连接栈可能含 DSN；失败仍失败，只输出脱敏错误类别。
        pytest.fail(f"真实 PostgreSQL 初始化失败：{type(error).__name__}/{error.sqlstate}", pytrace=False)
    return configured


@pytest.fixture(autouse=True)
def fixed_initial_state(settings, request):
    try:
        fixture = 'two-trips-one-coupon' if request.node.get_closest_marker('coupon') else 'single-trip'
        reset_test_database(settings.database_url, confirmation="llm_business_test", fixture=fixture)
    except psycopg.Error as error:
        pytest.fail(f"真实 PostgreSQL 重置失败：{type(error).__name__}/{error.sqlstate}", pytrace=False)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as current:
        yield current


@pytest.fixture
def sql(settings):
    """每次独立连接，避免 API 会话或未提交事务为重读制造假通过。"""
    def query(statement, parameters=()):
        try:
            with connect(settings.database_url) as connection:
                return connection.execute(statement, parameters).fetchall()
        except psycopg.Error as error:
            pytest.fail(f"独立 SQL 检查失败：{type(error).__name__}/{error.sqlstate}", pytrace=False)

    return query


@pytest.fixture
def login(client):
    def as_account(account="personal_alice"):
        response = client.post("/demo/sessions", json={"account": account})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["id"] == account
        return {"Authorization": f"Bearer {body['access_token']}"}

    return as_account


@pytest.fixture
def alice(login):
    return login("personal_alice")


@pytest.fixture
def bob(login):
    return login("personal_bob")


@pytest.fixture
def draft():
    return {"flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 1}


@pytest.fixture
def create(client, alice, draft):
    def make(key="create-1", *, headers=None, parameters=None):
        response = client.post(
            "/trips",
            headers={**(alice if headers is None else headers), "Idempotency-Key": key},
            json=draft if parameters is None else parameters,
        )
        assert response.status_code == 201, response.text
        return response.json()

    return make
