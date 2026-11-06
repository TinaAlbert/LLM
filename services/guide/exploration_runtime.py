"""P22 可信探索 broker：匿名观察与有限动作经过受限 IPC 后才触及合成 HTTP。

默认入口只接受本轮实际 AppContainer 证据；没有普通进程回退。显式注入的
工程替身仅供离线/合成运输检查，不得计为隔离或模型自主探索成功。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from .exploration import BoundedExploration, ExplorationError, Operation, OPERATIONS, VERSIONS

INPUT_BYTES = 12288
ASSUMPTIONS = ["synthetic_fixture", "simulated_personal_payment_profile",
               "simulated_providers", "no_current_user_records_copied"]
UNKNOWNS = ["current_user_eligibility", "current_user_quote", "current_user_inventory"]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _source_basis(value):
    # 交付/审查本身会继续消耗只读HTTP配额；证据绑定语义来源，资源计数单独记账。
    return {key: item for key, item in value.items() if key not in {"evidence_id", "used"}}


def require_native_engineering_evidence(*, report_path=None):
    """仅本轮显式工程闭环的局部前置，不替代产品完整隔离证据门。

    文件/派生/本机IPv4/6 TCP/UDP与在线8000/PG必须逐项通过，且绑定当前
    原生产物和启动器；公网对照缺口原样返回，accepted仍为false。
    """
    from . import windows
    try:
        raw = windows.exploration_report_path(report_path).read_bytes()
        def distinct(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=distinct)
        if (type(report) is not dict or report.get("schema_version") != 1
                or report.get("stage") != "complete" or type(report.get("passed")) is not bool
                or report.get("appcontainer_started") is not True or report.get("child_reaped") is not True
                or report.get("profile") != windows.PROFILE or report.get("network_capabilities") != []
                or report.get("loopback_exempt") is not False or report.get("host_spawn_exit") != 11
                or report.get("binary_sha256") != hashlib.sha256(windows.exploration_executable().read_bytes()).hexdigest()
                or report.get("launcher_sha256") != hashlib.sha256(Path(windows.__file__).read_bytes()).hexdigest()
                or report.get("source_sha256") != hashlib.sha256((windows.ROOT / "services/guide/shell.c").read_bytes()).hexdigest()):
            raise ValueError()
        age = datetime.now(timezone.utc) - datetime.fromisoformat(report["checked_at"])
        if age.total_seconds() < 0 or age > windows.PROBE_MAX_AGE:
            raise ValueError()
        local = set(windows.EXPLORATION_FILE_CHECKS) | {
            "child-process", "live-tcp4", "live-tcp6", "live-udp4", "live-udp6",
            "live-lan-udp4", "postgres", "business-http"}
        expected = local | {"arbitrary-network", "arbitrary-tcp6", "arbitrary-udp4", "arbitrary-udp6"}
        checks = report["checks"]
        if (type(checks) is not list or len(checks) != len(expected)
                or {row["resource"] for row in checks} != expected):
            raise ValueError()
        for row in checks:
            resource, result = row["resource"], row["result"]
            if resource not in local:
                continue
            if resource in windows.EXPLORATION_FILE_CHECKS:
                valid = result == "denied\t5"
            elif resource == "child-process":
                valid = result in {"spawn\t5", "spawn\t367", "spawn\t1816"}
            elif resource.startswith("live-"):
                valid = (result in {"timeout", "socket\t10013", "udp-no-response"}
                         and row.get("host_control") is True and row.get("child_received") is False)
            else:
                valid = result in {"timeout", "socket\t10013"} and row.get("host_control") is True
            if not valid:
                raise ValueError()
        limitations = report["limitations"]
        if type(limitations) is not list or any(type(item) is not str for item in limitations):
            raise ValueError()
        return {"report_sha256": hashlib.sha256(raw).hexdigest(), "checked_at": report["checked_at"],
                "local_prerequisites": "passed", "full_probe_passed": report["passed"],
                "limitations": list(limitations), "product_gate_bypassed": False,
                "scope": "explicit_engineering_native_local_http_only"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ExplorationError("native_engineering_evidence_unavailable") from None


def _record(value):
    """仅重建允许字段；污染的内部附加字段不会进入执行器或候选。"""
    try:
        filtered = BoundedExploration._filter_record(value, True)
        if {row["ref"] for row in filtered["state"]["delegated_trips"]} != {"trip_1", "trip_2"}:
            raise ValueError()
        if len(filtered["state"]["delegated_trips"]) != 2:
            raise ValueError()
        return filtered
    except (KeyError, TypeError, ValueError):
        raise ExplorationError("observation_unavailable") from None


class ExplorationRuntime:
    """一分支、28次操作、180秒；任意一步授权/取消失败均先于下一次业务写入。

    session_factory 是工程测试注入点，始终产生 engineering_stub 标签。
    真实产品默认入口不会捕捉 IsolationUnavailable 后切换到该注入点。
    """
    def __init__(self, exploration, *, session_factory=None, native_engineering=False,
                 report_path=None, clock=time.monotonic):
        self._exploration = exploration
        self._clock, self._deadline = clock, clock() + 180
        self._closed = False
        self._latest = None
        self._sequence = 0
        self._inputs = []
        if type(native_engineering) is not bool or (native_engineering and session_factory is not None):
            raise ExplorationError("invalid_executor_configuration")
        self.isolated = session_factory is None and not native_engineering
        self._native_completed = False
        from . import windows
        self._native_type = windows.ExplorationSession
        if native_engineering:
            require_native_engineering_evidence(report_path=report_path)
            session_factory = windows.ExplorationSession
        if session_factory is None:
            from .windows import begin_exploration
            if report_path is not None:
                self._session = begin_exploration(uuid4().hex, self._deadline - clock(), report_path=report_path)
                return
            session_factory = begin_exploration
        self._session = session_factory(uuid4().hex, self._deadline - clock())

    def _check(self):
        if self._closed:
            raise ExplorationError("exploration_disposed")
        if self._clock() >= self._deadline or self._sequence >= 28:
            raise ExplorationError("exploration_budget_exceeded")
        self._exploration._check()

    def observation(self):
        self._check()
        value = {**VERSIONS, "source_kind": "sandbox_observed", "environment": "synthetic-55433",
                 "simulated_identity": {"account": "personal_alice", "role": "personal"},
                 "goal": "obtain_simulated_tickets_for_both_delegated_trips_compare_observed_costs",
                 "assumptions": list(ASSUMPTIONS), "unknowns": list(UNKNOWNS),
                 "allowed_operations": list(OPERATIONS), "delegated_refs": ["trip_1", "trip_2"],
                 "limits": {"operations": 28, "http_requests": 120, "seconds": 180},
                 "latest_observation": _record(self._latest) if self._latest else None}
        # ASCII 是 native 管道的传输形式，不能用 UTF-8 小尺寸掩盖转义膨胀。
        if len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")) > INPUT_BYTES:
            raise ExplorationError("observation_budget_exceeded")
        return value

    def act(self, candidate):
        operation = Operation.model_validate(candidate)
        observation = self.observation()
        self._session.authorize(deepcopy(observation), operation.operation, operation.target, self._sequence + 1)
        # 不采信替身自称native_process_used；只有准确原生会话实例成功完成许可才记录。
        if type(self._session) is self._native_type:
            self._native_completed = True
        self._check()  # IPC期间的取消、换身份或撤权使许可失效，不产生HTTP写。
        result = self._exploration.act(operation.model_dump(exclude_none=True))
        self._check()
        self._latest = _record(result)
        self._sequence += 1
        self._inputs.append({"sequence": self._sequence, "input_hash": digest(observation),
                             "bytes": len(json.dumps(observation, ensure_ascii=True,
                                               separators=(",", ":")).encode("ascii")),
                             "operation": operation.model_dump(exclude_none=True)})
        return deepcopy(self._latest)

    def deliver(self):
        if self._closed or self._clock() >= self._deadline:
            raise ExplorationError("exploration_disposed")
        envelope = self._exploration.deliver()  # 重新核对接收会话和真实模拟对象。
        envelope["trace"] = [_record(row) for row in envelope["trace"]]
        if not self._latest:
            raise ExplorationError("insufficient_observation")
        # 原deliver只核对可读性。P22候选还绑定最终观察，状态改变必须先observe，
        # 不能因旧轨迹hash未变就把已失效的券归属/付款终态称为当前来源。
        current_state, _ = self._exploration._capture()
        current = _record({**self._latest, "state": current_state})
        self._exploration._check()
        if current["state"] != self._latest["state"]:
            raise ExplorationError("candidate_state_changed")
        envelope["executor_isolation"] = {
            "accepted": self.isolated and self._native_completed,
            "executor": ("appcontainer_explicit_ipc_trusted_http_broker" if self.isolated else
                         "engineering_appcontainer_trusted_http_broker" if self._native_completed else
                         "engineering_stub_trusted_http_broker"),
            "native_process_used": self._native_completed,
            "unverified": ([] if self.isolated and self._native_completed else
                           ["full_isolation_evidence"] if self._native_completed else
                           ["file", "network", "child_process"]),
        }
        envelope["planner"] = "offline_observation_driven_substitute"
        envelope["model_calls"] = 0
        envelope["ipc_inputs"] = deepcopy(self._inputs)
        envelope.pop("evidence_id", None)
        envelope["used"] = {"operations": self._exploration._count,
                            "http_requests": self._exploration._requests}
        envelope["evidence_id"] = digest(_source_basis(envelope))
        return envelope

    def abort(self):
        self._closed = True
        try:
            self._session.abort()
        finally:
            self._exploration.dispose()

    def close(self):
        self._closed = True
        try:
            self._session.close()
        finally:
            self._exploration.dispose()


def offline_next(observation):
    """只读允许观察的确定性运输替身；没有标准顺序/金额/SQL/worker输入。

    依当前估算优惠比较候选锁价，再逐项提交；不是LLM或最优性证据。
    未观察到支付/票据结果时只刷新，不能自行推进工程事件。
    """
    latest = observation["latest_observation"]
    if latest is None:
        return {"operation": "observe"}
    rows = _record(latest)["state"]["delegated_trips"]
    if all(row["data"]["ticket"] and row["data"]["ticket"]["status"] == "valid" for row in rows):
        return None
    for row in rows:
        if not row["data"]["quote"] and not row["data"]["lock"]:
            return {"operation": "booking.quote", "target": row["ref"]}
    unlocked = [row for row in rows if not row["data"]["lock"]]
    if unlocked:
        if any(not row["data"]["quote"] or not row["data"]["quote"]["valid"] for row in unlocked):
            return {"operation": "booking.quote", "target": next(row["ref"] for row in unlocked
                        if not row["data"]["quote"] or not row["data"]["quote"]["valid"])}
        row = max(unlocked, key=lambda row: row["data"]["quote"]["coupon_discount_minor"])
        return {"operation": "booking.lock", "target": row["ref"]}
    for row in rows:
        if not row["data"]["payment"]:
            return {"operation": "booking.pay", "target": row["ref"]}
    return {"operation": "observe"}


def candidate_for_review(runtime):
    """保留可追溯模拟轨迹的待审候选；没有发布、回滚或active切换能力。"""
    source = runtime.deliver()
    findings = []
    previous = None
    for row in source["trace"]:
        coupon = row["state"]["shared_coupon"]
        if previous and (coupon["status"], coupon["trip_ref"]) != (previous["status"], previous["trip_ref"]):
            findings.append({"before_sequence": row["sequence"] - 1, "source_sequence": row["sequence"],
                             "operation": row["operation"], "result": row["result"],
                             "coupon_before": {key: previous[key] for key in ("status", "trip_ref")},
                             "coupon_after": {key: coupon[key] for key in ("status", "trip_ref")}})
        previous = coupon
    candidate = {**VERSIONS, "source_kind": "sandbox_observed", "source_evidence_id": source["evidence_id"],
                 "simulated_identity": source["simulated_identity"], "assumptions": source["assumptions"],
                 "unknowns": source["unknowns"], "disclosure_scope": source["disclosure_scope"],
                 "findings": findings, "executor_isolation": source["executor_isolation"],
                 "status": "requires_independent_and_disclosure_review", "publishable": False,
                 "reason": "synthetic_evidence_is_not_current_user_fact_or_public_release"}
    candidate["version"] = "sandbox-candidate-" + digest(candidate)[:20]
    return candidate, source


def review_candidate(runtime, candidate, source, independent_verdict):
    """当前接收者过滤/来源重核与独立SQL判据分离；不会自行把候选发布。"""
    current_candidate, current_source = candidate_for_review(runtime)
    if (current_candidate != candidate or current_source["evidence_id"] != source.get("evidence_id")
            or _source_basis(current_source) != _source_basis(source)):
        raise ExplorationError("candidate_source_changed")
    if (type(independent_verdict) is not dict or independent_verdict.get("passed") is not True
            or independent_verdict.get("method") != "independent_read_only_sql"
            or independent_verdict.get("source_evidence_id") != source["evidence_id"]
            or independent_verdict.get("observed_final_state_hash") != digest(source["trace"][-1]["state"])
            or not re.fullmatch(r"[0-9a-f]{64}", str(independent_verdict.get("terminal_sql_sha256", "")))):
        raise ExplorationError("independent_verification_required")
    return {"candidate_version": candidate["version"], "source_evidence_id": source["evidence_id"],
            "source_integrity": "matched", "recipient_disclosure": "rechecked",
            "independent_truth": "passed", "scope": "synthetic_record_only",
            "terminal_sql_sha256": independent_verdict["terminal_sql_sha256"],
            "executor_isolation": source["executor_isolation"], "model_calls": 0,
            "publishable": False, "active_release_changed": False}
