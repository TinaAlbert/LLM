"""一个请求一个同步 PostgreSQL 事务；连接退出前提交，异常回滚。"""

import psycopg
import hashlib
from psycopg.rows import dict_row


def lock_actor(conn, workspace, actor):
    """资料、草稿与工作处理采用同一主体锁，库存仍按航段跨主体竞争。"""
    key = hashlib.sha256(f"personal:{workspace}:{actor}".encode()).digest()[:8]
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (int.from_bytes(key, signed=True),))


def business_time(conn, workspace):
    row = conn.execute("SELECT business_time FROM workspaces WHERE id=%s FOR SHARE", (workspace,)).fetchone()
    if row is None:
        raise ValueError("工作空间不存在")
    return row["business_time"]


def read_snapshot(conn):
    """聚合 GET 的授权、对象及关联状态来自同一提交快照；须在首次查询前调用。"""
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")


def connect(database_url: str):
    return psycopg.connect(
        database_url,
        row_factory=dict_row,
        # 本机Windows PG建连曾超过5秒；只延长有界建连等待，不重试业务事务。
        connect_timeout=10,
        options="-c statement_timeout=10000 -c lock_timeout=5000 -c timezone=UTC",
    )
