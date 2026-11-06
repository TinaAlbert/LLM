"""P21 本地探索工程入口；固定55433合成库、8000 HTTP，模型0次。

root先串行prepare原round20 sandbox，再在独立进程运行本文件server；运行run后
按原服务关闭顺序停止。不会启动PG、复制原记录或自动启动模型。
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psycopg

from scripts import round20_sandbox as sandbox
from scripts.d3_evaluate import NoModelTraffic
from services.business.app import create_app
from services.business.config import Settings
from services.business.db import connect, read_snapshot
from services.business.manage import reset_test_database
from services.business.worker import process_payment, process_ticket
from services.guide.exploration import BoundedExploration, ExplorationError, Recipient, REFS, TARGET, VERSIONS

OUT = ROOT / ".local/d3-21/exploration"
SERVER_MARKER = OUT / "server.json"
BRANCHES = ("first-then-second", "second-then-first", "cancel-release-reorder", "consumed-cannot-release")


def require(value, code):
    if not value:
        raise RuntimeError(code)


def save(name, value, *, directory=OUT):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def configuration():
    sandbox.bind_helper()
    sandbox.check_runner()
    config = sandbox.checked_config()
    sandbox.check_business(config)
    return config, Settings(config["test_database_url"], "test", demo_login=True, mode="test")


def configure_server():
    config, settings = configuration()
    marker = {"scope": "P21-synthetic-ordinary-role", "instance": sandbox.check_cluster()["instance_id"],
              "runtime": uuid4().hex, "versions": VERSIONS,
              "app_source_sha256": hashlib.sha256((ROOT / "services/business/app.py").read_bytes()).hexdigest()}
    app = create_app(settings)

    @app.get("/__round21/binding")
    def binding():
        # 只在专用工程server注册；没有初始化/重置/时间/worker的HTTP入口。
        return marker

    save("server.json", marker)
    return app, settings


def server():
    import uvicorn
    app, settings = configure_server()
    guard = NoModelTraffic()
    try:
        with guard.installed():
            uvicorn.run(app, host="127.0.0.1", port=8000, access_log=False, proxy_headers=False)
    finally:
        save("transport-guard.json", {"model_send_attempts": guard.send_attempts,
             "budget_reserve_attempts": guard.reserve_attempts, "model": "none"})


def write(client, headers, path, body=None, method="POST"):
    result = client.request(method, path, headers={**headers, "Idempotency-Key": uuid4().hex},
                            json=body if body is not None else {})
    require(result.status_code in (200, 201, 202), "synthetic_preparation_http_failed")
    return result.json()


@contextmanager
def ordinary_session(client, settings):
    response = client.post("/demo/sessions", json={"account": "personal_alice"})
    require(response.status_code == 201, "synthetic_login_failed")
    token = response.json()["access_token"]
    headers = {"Authorization": "Bearer " + token}
    try:
        # 原API或任何其他DB上的会话不会通过；业务写前核实真实服务的连接目标。
        with connect(settings.database_url) as conn:
            read_snapshot(conn)
            conn.execute("SET TRANSACTION READ ONLY")
            require(conn.execute("SELECT current_setting('port') AS port").fetchone()["port"] == "55433",
                    "api_cluster_mismatch")
            row = conn.execute("SELECT user_id FROM sessions WHERE workspace_id='test' AND token_hash=%s AND NOT revoked",
                               (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            require(row == {"user_id": "personal_alice"}, "api_session_not_in_synthetic_cluster")
        yield headers
    finally:
        require(client.delete("/session", headers=headers).status_code == 204, "synthetic_logout_failed")


def sql_evidence(settings):
    """独立只读连接；只写工程证据文件，绝不交给探索通道或运行观察。"""
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        conn.execute("SET TRANSACTION READ ONLY")
        require(conn.execute("SELECT current_setting('port') AS port").fetchone()["port"] == "55433",
                "independent_sql_cluster_mismatch")
        workspace = conn.execute("SELECT fixture_key,product_version,business_time FROM workspaces WHERE id='test'").fetchone()
        require(workspace["fixture_key"] == "two-trips-one-coupon" and workspace["product_version"] == "v1",
                "synthetic_fixture_version_changed")
        queries = {
            "trips": "SELECT id,owner_id,flight_id,revision,status FROM trips ORDER BY flight_id",
            "coupons": "SELECT id,owner_id,status,lock_id,percent,cap_minor FROM coupons ORDER BY id",
            "locks": "SELECT id,trip_id,trip_revision,status,amount_minor,coupon_id,coupon_discount_minor FROM booking_locks ORDER BY amount_minor,id",
            "payments": "SELECT id,trip_id,lock_id,status,amount_minor FROM payments ORDER BY amount_minor,id",
            "payment_jobs": "SELECT payment_id,status FROM payment_jobs ORDER BY payment_id",
            "provider_payments": "SELECT operation_id,outcome,captured_minor FROM provider_payments ORDER BY operation_id",
            "ticket_tasks": "SELECT id,payment_id,trip_id,status FROM ticket_tasks ORDER BY id",
            "tickets": "SELECT id,trip_id,trip_revision,payment_id,task_id FROM tickets ORDER BY id",
            "flights": "SELECT id,capacity,reserved,sold FROM flights ORDER BY id",
            "coupon_events": "SELECT action,count(*) AS count FROM business_events WHERE object_type='coupon' GROUP BY action ORDER BY action",
            "migrations": "SELECT version,checksum FROM schema_migrations ORDER BY version",
        }
        result = {key: conn.execute(query).fetchall() for key, query in queries.items()}
    return {"workspace": workspace, **result}


def canonical_initial(evidence):
    require(all(not evidence[key] for key in ("trips", "locks", "payments", "payment_jobs",
                                             "provider_payments", "ticket_tasks", "tickets", "coupon_events")),
            "synthetic_initial_not_empty")
    return {"workspace": evidence["workspace"], "flights": evidence["flights"],
            "coupons": [{key: row[key] for key in ("owner_id", "status", "percent", "cap_minor")}
                        for row in evidence["coupons"]], "migrations": evidence["migrations"]}


def settle(settings, ref, trip_ids):
    # worker是工程外部事件控制面，未列入Operation，不将任务ID或控制接口传入探索通道。
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute("SELECT id FROM payments WHERE trip_id=%s AND status='processing'", (trip_ids[REFS.index(ref)],)).fetchall()
    require(len(rows) == 1, "expected_one_pending_synthetic_payment")
    paid = process_payment(settings.database_url, "test", rows[0]["id"])
    require(paid["status"] == "succeeded", "synthetic_payment_not_settled")
    issued = process_ticket(settings.database_url, "test", paid["ticket_task_id"])
    require(issued["status"] == "succeeded", "synthetic_ticket_not_issued")


def evaluate(branch, evidence, trace, trip_ids):
    """独立判据位于工程控制器；正确顺序/总额不进入BoundedExploration。"""
    consumed_only = branch == "consumed-cannot-release"
    expected_total = 48000 if consumed_only else 148000 if branch == "second-then-first" else 140000
    require(len(evidence["tickets"]) == (1 if consumed_only else 2), "independent_ticket_count_failed")
    require(sum(row["captured_minor"] for row in evidence["provider_payments"]) == expected_total,
            "independent_capture_total_failed")
    require(all(row["outcome"] == "succeeded" for row in evidence["provider_payments"]), "independent_provider_failed")
    require(all(row["status"] == "succeeded" for row in evidence["payments"] + evidence["ticket_tasks"]),
            "independent_payment_or_ticket_failed")
    require(all(row["status"] == "done" for row in evidence["payment_jobs"]), "independent_job_failed")
    require(len(evidence["payments"]) == len(evidence["provider_payments"]) == len(evidence["ticket_tasks"]) == len(evidence["tickets"]),
            "independent_duplicate_failed")
    coupon = evidence["coupons"]
    require(len(coupon) == 1 and coupon[0]["status"] == "consumed", "independent_coupon_not_consumed")
    owner_locks = [row for row in evidence["locks"] if row["coupon_id"] == coupon[0]["id"] and row["status"] == "consumed"]
    expected_holder = trip_ids[1 if branch in {"second-then-first", "consumed-cannot-release"} else 0]
    require(len(owner_locks) == 1 and owner_locks[0]["id"] == coupon[0]["lock_id"]
            and str(owner_locks[0]["trip_id"]) == expected_holder, "independent_coupon_attribution_failed")
    require(next(row["count"] for row in evidence["coupon_events"] if row["action"] == "coupon.consumed") == 1,
            "independent_coupon_consumed_twice")
    require(any(row["state"]["shared_coupon"]["status"] == "protected" for row in trace), "protected_observation_missing")
    require(any(row["state"]["shared_coupon"]["status"] == "consumed" for row in trace), "consumed_observation_missing")
    if branch == "cancel-release-reorder":
        require(any(row["operation"]["operation"] == "booking.cancel_lock" and row["result"] == "accepted"
                    and row["state"]["shared_coupon"]["status"] == "available" for row in trace), "release_observation_missing")
    if consumed_only:
        require(any(row["operation"]["operation"] == "booking.cancel_lock" and row["result"] == "precondition_not_met"
                    and row["state"]["shared_coupon"]["status"] == "consumed" for row in trace), "irreversible_observation_missing")
        require([row["amount_minor"] for row in evidence["locks"] if row["status"] == "held"] == [100000], "subsequent_no_coupon_lock_failed")
    else:
        require(all(row["reserved"] == 0 and row["sold"] == 1 for row in evidence["flights"]), "independent_inventory_failed")
    return {"passed": True, "captured_minor": expected_total, "tickets": len(evidence["tickets"]),
            "coupon_consumed": 1, "method": "independent_read_only_sql"}


def run():
    config, settings = configuration()
    guard = NoModelTraffic()
    run_directory = OUT / ("run-" + uuid4().hex)
    def record(name, value):
        save(name, value, directory=run_directory)
    summary = {"branches": [], "model_calls": 0, "complete": False,
               "executor_isolation_accepted": False, "http": "real_tcp_127.0.0.1_8000",
               "sql": "independent_55433_only", "run_directory": str(run_directory.relative_to(ROOT))}
    # 新运行开始即撤销旧摘要的“本次已完成”含义；各次原件保留在独立目录。
    save("latest-summary.json", summary)
    record("summary.json", summary)
    with sandbox.runner_lock(), guard.installed(), httpx.Client(base_url=TARGET, trust_env=False, timeout=15) as client:
        expected_marker = json.loads(SERVER_MARKER.read_text(encoding="utf-8"))
        require(expected_marker.get("instance") == sandbox.check_cluster()["instance_id"], "server_cluster_marker_mismatch")
        binding = client.get("/__round21/binding")
        require(binding.status_code == 200 and binding.json() == expected_marker, "not_round21_synthetic_server")
        # 重置前也先核对实际HTTP API会话确实进入55433；原55432永不参与。
        with ordinary_session(client, settings):
            pass
        initial = None
        summaries = []
        for branch in BRANCHES:
            reset_test_database(config["test_database_url"], confirmation="llm_business_test", fixture="two-trips-one-coupon")
            start = sql_evidence(settings)
            canonical = canonical_initial(start)
            if initial is None:
                initial = canonical
            require(initial == canonical, "branch_initial_state_mismatch")
            record(branch + "-initial-sql.json", start)
            with ordinary_session(client, settings) as headers:
                write(client, headers, "/payment-profile", {"source_ref": "demo_personal_alice", "expected_revision": 0}, "PUT")
                trips = tuple(write(client, headers, "/trips", {"flight_id": "DEMO-" + size,
                              "travel_date": "2026-10-01", "passengers": 1})["id"] for size in ("LARGE", "SMALL"))
                recipient = Recipient("local-reviewer", uuid4().hex, 1)
                session = BoundedExploration(client, headers, trips, recipient, lambda: recipient)
                try:
                    def act(name, ref=None):
                        return session.act({"operation": name, **({"target": ref} if ref else {})})
                    act("observe")
                    for ref in REFS:
                        act("booking.quote", ref)
                    if branch == "cancel-release-reorder":
                        act("booking.lock", "trip_2")
                        act("booking.cancel_lock", "trip_2")
                    order = ("trip_2", "trip_1") if branch in {"second-then-first", "consumed-cannot-release"} else REFS
                    if branch == "consumed-cannot-release":
                        act("booking.lock", "trip_2")
                        act("booking.pay", "trip_2")
                        settle(settings, "trip_2", trips)
                        act("observe")
                        act("booking.cancel_lock", "trip_2")
                        act("booking.quote", "trip_1")
                        act("booking.lock", "trip_1")
                    else:
                        for ref in order:
                            act("booking.lock", ref)
                        for ref in order:
                            act("booking.pay", ref)
                            settle(settings, ref, trips)
                            act("observe")
                    envelope = session.deliver()
                    final = sql_evidence(settings)
                    verdict = evaluate(branch, final, envelope["trace"], trips)
                    record(branch + "-observation.json", envelope)
                    record(branch + "-final-sql.json", final)
                    branch_summary = {"branch": branch, "verdict": verdict, "evidence_id": envelope["evidence_id"],
                               "simulated_identity": envelope["simulated_identity"], "versions": VERSIONS,
                               "assumptions": envelope["assumptions"], "business_clock": start["workspace"]["business_time"],
                               "seed": "deterministic_fixture_opaque_ids_random", "initial_equivalent": True,
                               "used": envelope["used"], "executor_isolation_accepted": False}
                    summaries.append(branch_summary)
                    summary.update(branches=summaries, complete=len(summaries) == len(BRANCHES))
                    record("summary.json", summary)
                    save("latest-summary.json", summary)
                finally:
                    session.dispose()
        require(guard.send_attempts == guard.reserve_attempts == 0, "unexpected_model_traffic")
        record("controller-transport-guard.json", {"model_send_attempts": guard.send_attempts,
             "budget_reserve_attempts": guard.reserve_attempts, "model": "none"})
    print("P21四个合成分支HTTP/独立SQL通过；模型0次；普通宿主执行器隔离未验收。")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["server"]:
            server()
        elif sys.argv[1:] == ["run"]:
            run()
        else:
            raise RuntimeError("仅支持 server 或 run；先由原隔离入口 prepare。")
    except (RuntimeError, ExplorationError) as error:
        raise SystemExit(str(error)) from None
    except (psycopg.Error, httpx.HTTPError, ValueError, KeyError, OSError):
        raise SystemExit("P21工程入口失败；检查隔离实例/专用server和本地证据，不输出凭证或完整响应。") from None
