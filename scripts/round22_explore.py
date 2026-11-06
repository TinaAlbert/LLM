"""P22 独立工程生命周期；只在核验后的55433合成实例运行，模型0次。

server由工程显式启动；run默认要求本轮真实隔离证据。engineering-run是明确
普通宿主替身运输检查，不能当作隔离成功或LLM自主发现。不会启动PG。
"""
from __future__ import annotations

import asyncio
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
from scripts.round21_explore import (configuration, ordinary_session, write, sql_evidence,
                                     canonical_initial, settle, evaluate, require)
from services.business.app import create_app
from services.business.manage import reset_test_database
from services.guide.exploration import BoundedExploration, ExplorationError, Recipient, TARGET, VERSIONS
from services.guide.exploration_runtime import (ExplorationRuntime, candidate_for_review,
                                               digest, offline_next, review_candidate,
                                               require_native_engineering_evidence)

OUT = ROOT / ".local/d3-22/exploration"
SERVER_MARKER = OUT / "server.json"
STOP = OUT / "server.stop"


def save(name, value, *, directory=OUT, exclusive=False):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def configure_server(*, marker_directory=OUT, scope="P22-synthetic-ordinary-role",
                     binding_path="/__round22/binding", report_path=None, exclusive=False):
    config, settings = configuration()
    marker = {"scope": scope, "instance": sandbox.check_cluster()["instance_id"],
              "runtime": uuid4().hex, "versions": VERSIONS,
              "app_source_sha256": hashlib.sha256((ROOT / "services/business/app.py").read_bytes()).hexdigest()}
    if report_path is not None:
        marker["exploration_report"] = Path(report_path).relative_to(ROOT).as_posix()
    app = create_app(settings)

    @app.get(binding_path)
    def binding():
        return marker

    save("server.json", marker, directory=marker_directory, exclusive=exclusive)
    return app, settings


def server(*, output_directory=OUT, scope="P22-synthetic-ordinary-role",
           binding_path="/__round22/binding", report_path=None, exclusive=False):
    import uvicorn
    stop = output_directory / "server.stop"
    if stop.exists():
        raise RuntimeError("round22_stop_marker_exists_check_old_process_first")
    app, settings = configure_server(marker_directory=output_directory, scope=scope,
        binding_path=binding_path, report_path=report_path, exclusive=exclusive)
    guard = NoModelTraffic()
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000,
                                           access_log=False, proxy_headers=False))

    async def serve():
        task = asyncio.create_task(instance.serve())
        while not task.done():
            if stop.exists():
                instance.should_exit = True
            await asyncio.sleep(.2)
        await task

    try:
        with guard.installed():
            asyncio.run(serve())
    finally:
        save("server-transport-guard.json", {"model_send_attempts": guard.send_attempts,
             "budget_reserve_attempts": guard.reserve_attempts, "model": "none"}, directory=output_directory)


class EngineeringIPC:
    """显式普通宿主替身，不产生或保存任何真实隔离证据。"""
    def __init__(self, reference, remaining_seconds):
        self.closed = False

    def authorize(self, observation, operation, target, seq):
        if self.closed:
            raise ExplorationError("exploration_disposed")

    def abort(self):
        self.closed = True

    def close(self):
        self.closed = True


def run(*, engineering=False, native_engineering=False, output_directory=OUT, report_path=None,
        scope="P22-synthetic-ordinary-role", binding_path="/__round22/binding",
        server_marker_directory=None):
    require(not (engineering and native_engineering), "invalid_executor_configuration")
    config, settings = configuration()
    marker_directory = output_directory if server_marker_directory is None else server_marker_directory
    run_directory = output_directory / ("run-" + uuid4().hex)
    summary = {"complete": False, "model_calls": 0, "planner": "offline_observation_driven_substitute",
               "engineering_stub": engineering, "executor_isolation_accepted": False,
               "native_engineering": native_engineering,
               "http": "real_tcp_127.0.0.1_8000", "sql": "independent_55433_only",
               "run_directory": str(run_directory.relative_to(ROOT)), "active_release_changed": False}

    def record(name, value):
        save(name, value, directory=run_directory)

    record("summary.json", summary)
    save("latest-summary.json", summary, directory=output_directory)
    guard = NoModelTraffic()
    runtime = None
    session = None
    try:
        with sandbox.runner_lock(), guard.installed(), httpx.Client(
                base_url=TARGET, trust_env=False, follow_redirects=False, timeout=15) as client:
            expected = json.loads((marker_directory / "server.json").read_text(encoding="utf-8"))
            require(expected.get("scope") == scope, "server_scope_mismatch")
            require(expected.get("instance") == sandbox.check_cluster()["instance_id"], "server_cluster_marker_mismatch")
            if report_path is not None:
                selected_report = Path(report_path).relative_to(ROOT).as_posix()
                require(expected.get("exploration_report") == selected_report, "server_probe_path_mismatch")
            binding = client.get(binding_path)
            require(binding.status_code == 200 and binding.json() == expected, "not_round22_synthetic_server")
            with ordinary_session(client, settings):
                pass
            # 真实模式在任何reset/合成写入前先检查本轮隔离证据。
            if native_engineering:
                summary["native_engineering_evidence"] = require_native_engineering_evidence(report_path=report_path)
                record("native-engineering-prerequisites.json", summary["native_engineering_evidence"])
            elif not engineering:
                from services.guide.windows import require_exploration_evidence
                require_exploration_evidence(report_path=report_path)
            reset_test_database(config["test_database_url"], confirmation="llm_business_test", fixture="two-trips-one-coupon")
            initial = sql_evidence(settings)
            canonical_initial(initial)
            record("initial-sql.json", initial)
            with ordinary_session(client, settings) as headers:
                write(client, headers, "/payment-profile", {"source_ref": "demo_personal_alice", "expected_revision": 0}, "PUT")
                trips = tuple(write(client, headers, "/trips", {"flight_id": "DEMO-" + size,
                              "travel_date": "2026-10-01", "passengers": 1})["id"] for size in ("LARGE", "SMALL"))
                recipient = Recipient("local-round22-reviewer", uuid4().hex, 1)
                session = BoundedExploration(client, headers, trips, recipient, lambda: recipient)
                runtime = ExplorationRuntime(session, session_factory=EngineeringIPC if engineering else None,
                                             native_engineering=native_engineering, report_path=report_path)
                decisions = []
                while True:
                    allowed = runtime.observation()
                    action = offline_next(allowed)
                    if action is None:
                        break
                    # 仅该预先过滤的输入可见于planner；SQL、UUID、凭证与答案独立保存。
                    decisions.append({"input": allowed, "output": action})
                    record("planner-input-output.json", decisions)
                    result = runtime.act(action)
                    if action["operation"] == "booking.pay" and result["result"] == "accepted":
                        # 工程模拟外部事件；不是运行工具，ID及控制结果不送入planner。
                        settle(settings, action["target"], trips)
                candidate, source = candidate_for_review(runtime)
                final = sql_evidence(settings)
                # 此判据只在运行终结后调用；固定正确金额/顺序不进入运行输入。
                verdict = evaluate("first-then-second", final, source["trace"], trips)
                verdict.update(source_evidence_id=source["evidence_id"],
                               observed_final_state_hash=digest(source["trace"][-1]["state"]),
                               terminal_sql_sha256=hashlib.sha256(json.dumps(final, ensure_ascii=True,
                                   sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest())
                review = review_candidate(runtime, candidate, source, verdict)
                record("observation.json", source)
                record("candidate.json", candidate)
                record("candidate-review.json", review)
                record("final-sql.json", final)
                summary.update(complete=True, verdict=verdict, candidate_version=candidate["version"],
                               evidence_id=source["evidence_id"], used_at_candidate=source["used"],
                               used={"operations": session._count, "http_requests": session._requests},
                               native_process_used=source["executor_isolation"]["native_process_used"],
                               executor_isolation_accepted=source["executor_isolation"]["accepted"])
            require(guard.send_attempts == guard.reserve_attempts == 0, "unexpected_model_traffic")
    except BaseException as error:
        # 只保存固定异常类型；OS/HTTP异常原文可能包含路径或凭证。
        summary["failure_type"] = type(error).__name__
        raise
    finally:
        try:
            if runtime is not None:
                runtime.close()
            elif session is not None:
                session.dispose()
        except BaseException as error:
            summary.update(complete=False, cleanup_failure_type=type(error).__name__)
            raise
        finally:
            record("controller-transport-guard.json", {"model_send_attempts": guard.send_attempts,
                 "budget_reserve_attempts": guard.reserve_attempts, "model": "none"})
            record("summary.json", summary)
            save("latest-summary.json", summary, directory=output_directory)
    print("P22合成观察驱动替身HTTP/独立SQL与待审候选完成；模型0次；隔离状态见summary。")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["server"]:
            server()
        elif sys.argv[1:] == ["run"]:
            run()
        elif sys.argv[1:] == ["engineering-run"]:
            run(engineering=True)
        elif sys.argv[1:] == ["native-engineering-run"]:
            run(native_engineering=True)
        else:
            raise RuntimeError("only_server_run_engineering-run_or_native-engineering-run")
    except (RuntimeError, ExplorationError) as error:
        raise SystemExit(str(error)) from None
    except (psycopg.Error, httpx.HTTPError, ValueError, KeyError, OSError):
        raise SystemExit("P22工程入口失败；检查专用server/55433/本轮隔离证据，不输出凭证或完整响应。") from None
