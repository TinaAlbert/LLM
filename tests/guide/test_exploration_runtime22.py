"""P22离线broker契约；不冒充真实隔离、业务终态或模型效果。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.guide.exploration import ExplorationError
from services.guide.exploration_runtime import (ExplorationRuntime, candidate_for_review,
                                               digest, offline_next, review_candidate,
                                               require_native_engineering_evidence)
from test_exploration21 import fixture


class IPC:
    def __init__(self, ref, remaining):
        self.calls = []
        self.closed = False
        self.hook = None

    def authorize(self, observation, operation, target, seq):
        self.calls.append((observation, operation, target, seq))
        if self.hook:
            self.hook()

    def abort(self):
        self.closed = True

    def close(self):
        self.closed = True


def runtime():
    bounded, state, ids = fixture()
    item = ExplorationRuntime(bounded, session_factory=IPC, clock=lambda: state.now)
    return item, bounded, state, ids


def test_default_factory_fails_closed_before_any_http(monkeypatch):
    from services.guide import windows
    bounded, state, _ = fixture()

    def unavailable(*args):
        raise windows.IsolationUnavailable()

    monkeypatch.setattr(windows, "begin_exploration", unavailable)
    with pytest.raises(windows.IsolationUnavailable):
        ExplorationRuntime(bounded)
    assert not state.requests


def test_ipc_receives_only_filtered_input_then_http_acts():
    item, bounded, state, ids = runtime()
    item.act({"operation": "observe"})
    bounded._trace[0]["state"]["sql"] = "private-sql"
    encoded = json.dumps(item.observation())
    assert all(value not in encoded for value in (*ids, state.coupon["id"], "private-sql", "private-synthetic-token"))
    observation, name, target, seq = item._session.calls[0]
    assert observation["latest_observation"] is None
    assert (name, target, seq) == ("observe", None, 1)
    result = item.deliver()
    assert result["executor_isolation"]["accepted"] is False
    assert result["model_calls"] == 0
    assert all(row["bytes"] <= 12288 for row in result["ipc_inputs"])


@pytest.mark.parametrize("candidate", [
    {"operation": "reset"}, {"operation": "booking.pay", "target": "trip_1", "amount_minor": 1},
    {"operation": "observe", "url": "http://127.0.0.1:55432"},
])
def test_malicious_candidate_rejected_before_ipc_or_http(candidate):
    item, _, state, _ = runtime()
    with pytest.raises(ValidationError):
        item.act(candidate)
    assert not item._session.calls and not state.requests


@pytest.mark.parametrize("change", ["cancel", "identity", "deadline", "permit_failure"])
def test_late_or_rejected_permit_never_performs_http(change):
    item, _, state, _ = runtime()

    def hook():
        if change == "cancel":
            item.abort()
        elif change == "identity":
            state.recipient = replace(state.recipient, generation="changed")
        elif change == "deadline":
            state.now = 180
        else:
            raise ExplorationError("ipc_rejected")

    item._session.hook = hook
    with pytest.raises(ExplorationError):
        item.act({"operation": "observe"})
    assert not state.requests


def test_delivery_rejects_real_state_changed_after_last_observation():
    item, _, state, _ = runtime()
    item.act({"operation": "observe"})
    state.coupon["status"] = "held"
    with pytest.raises(ExplorationError, match="candidate_state_changed"):
        candidate_for_review(item)


def test_review_binds_independent_verdict_and_rechecks_recipient():
    item, _, state, _ = runtime()
    item.act({"operation": "observe"})
    candidate, source = candidate_for_review(item)
    verdict = {"passed": True, "method": "independent_read_only_sql",
               "source_evidence_id": source["evidence_id"],
               "observed_final_state_hash": digest(source["trace"][-1]["state"]),
               "terminal_sql_sha256": "a" * 64}
    assert review_candidate(item, candidate, source, verdict)["publishable"] is False
    wrong = {**verdict, "source_evidence_id": "wrong-branch"}
    with pytest.raises(ExplorationError, match="independent_verification_required"):
        review_candidate(item, candidate, source, wrong)
    state.recipient = replace(state.recipient, disclosure_revision=2)
    with pytest.raises(ExplorationError, match="session_changed"):
        review_candidate(item, candidate, source, verdict)


def test_tampered_candidate_or_source_cannot_be_reviewed():
    item, _, _, _ = runtime()
    item.act({"operation": "observe"})
    candidate, source = candidate_for_review(item)
    candidate["simulated_identity"]["role"] = "administrator"
    with pytest.raises(ExplorationError, match="candidate_source_changed"):
        review_candidate(item, candidate, source, {})


def test_source_id_cannot_be_replaced_with_matching_forged_verdict():
    item, _, _, _ = runtime()
    item.act({"operation": "observe"})
    candidate, source = candidate_for_review(item)
    source["evidence_id"] = "b" * 64
    verdict = {"passed": True, "method": "independent_read_only_sql",
               "source_evidence_id": source["evidence_id"],
               "observed_final_state_hash": digest(source["trace"][-1]["state"]),
               "terminal_sql_sha256": "a" * 64}
    with pytest.raises(ExplorationError, match="candidate_source_changed"):
        review_candidate(item, candidate, source, verdict)


def test_offline_planner_uses_observed_discount_not_alias_or_standard_amount():
    item, _, _, _ = runtime()
    assert offline_next(item.observation()) == {"operation": "observe"}
    item.act({"operation": "observe"})
    observed = item.observation()
    for index, row in enumerate(observed["latest_observation"]["state"]["delegated_trips"]):
        row["data"]["quote"] = {"trip_revision": 1, "amount_minor": 299 - index,
            "base_amount_minor": 400, "coupon_discount_minor": 101 + index, "currency": "DEMO",
            "effective_status": "active", "valid": True, "expires_at": "2026-10-17T00:05:00+00:00",
            "price_basis": "estimate", "coupon_allocation": "estimate"}
    assert offline_next(observed) == {"operation": "booking.lock", "target": "trip_2"}
    reverse = deepcopy(observed)
    reverse["latest_observation"]["state"]["delegated_trips"][0]["data"]["quote"]["coupon_discount_minor"] = 103
    assert offline_next(reverse) == {"operation": "booking.lock", "target": "trip_1"}


def test_close_is_idempotent_and_prevents_more_actions():
    item, bounded, _, _ = runtime()
    item.close()
    item.close()
    assert item._session.closed and bounded._headers == {}
    with pytest.raises(ExplorationError, match="exploration_disposed"):
        item.act({"operation": "observe"})


def test_stub_cannot_self_declare_actual_native_process():
    bounded, _, _ = fixture()

    class PretendNative(IPC):
        native_process_used = True

    item = ExplorationRuntime(bounded, session_factory=PretendNative)
    item.act({"operation": "observe"})
    isolation = item.deliver()["executor_isolation"]
    assert isolation["native_process_used"] is False
    assert isolation["executor"] == "engineering_stub_trusted_http_broker"


@pytest.mark.parametrize("operation", ["close", "abort"])
def test_native_cleanup_failure_still_disposes_bound_credentials(operation):
    item, bounded, _, _ = runtime()

    def fail():
        raise ExplorationError("native_cleanup_failed")

    setattr(item._session, operation, fail)
    with pytest.raises(ExplorationError, match="native_cleanup_failed"):
        getattr(item, operation)()
    assert bounded._headers == bounded._ids == {} and bounded._trace == []


def native_report(tmp_path, monkeypatch):
    from services.guide import windows
    monkeypatch.setattr(windows, "ROOT", tmp_path)
    monkeypatch.setattr(windows, "EXPLORATION_REPORT", "report.json")
    monkeypatch.setattr(windows, "EXPLORATION_FILE_CHECKS", ("source.py", "answer.py"))
    (tmp_path / "services/guide").mkdir(parents=True)
    (tmp_path / "services/guide/shell.c").write_bytes(b"synthetic probe source")
    binary = tmp_path / "shell.exe"
    binary.write_bytes(b"synthetic bytes; this test never starts a process")
    monkeypatch.setattr(windows, "exploration_executable", lambda: binary)
    checks = [{"resource": name, "result": "denied\t5"} for name in windows.EXPLORATION_FILE_CHECKS]
    checks.append({"resource": "child-process", "result": "spawn\t367"})
    checks.extend({"resource": name, "result": "timeout", "host_control": True, "child_received": False}
                  for name in ("live-tcp4", "live-tcp6", "live-udp4", "live-udp6", "live-lan-udp4"))
    checks.extend({"resource": name, "result": "timeout", "host_control": True}
                  for name in ("postgres", "business-http"))
    checks.extend({"resource": name, "result": "not_proven", "host_control": False}
                  for name in ("arbitrary-network", "arbitrary-tcp6", "arbitrary-udp4", "arbitrary-udp6"))
    report = {"schema_version": 1, "stage": "complete", "passed": False,
              "appcontainer_started": True, "child_reaped": True, "host_spawn_exit": 11,
              "profile": windows.PROFILE, "network_capabilities": [], "loopback_exempt": False,
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "launcher_sha256": hashlib.sha256(Path(windows.__file__).read_bytes()).hexdigest(),
              "source_sha256": hashlib.sha256((tmp_path / "services/guide/shell.c").read_bytes()).hexdigest(),
              "checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks,
              "limitations": ["arbitrary-udp4_host_control_unavailable"]}
    return report


@pytest.mark.parametrize("mutation", ["none", "missing", "duplicate", "offline8000", "udp_received", "stale", "binary"])
def test_native_engineering_gate_checks_local_prerequisites_preserves_full_failure(tmp_path, monkeypatch, mutation):
    report = native_report(tmp_path, monkeypatch)
    if mutation == "missing":
        report["checks"].pop()
    elif mutation == "duplicate":
        report["checks"][-1] = report["checks"][0]
    elif mutation == "offline8000":
        next(row for row in report["checks"] if row["resource"] == "business-http")["host_control"] = False
    elif mutation == "udp_received":
        next(row for row in report["checks"] if row["resource"] == "live-udp6")["child_received"] = True
    elif mutation == "stale":
        report["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    elif mutation == "binary":
        report["binary_sha256"] = "wrong"
    (tmp_path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if mutation == "none":
        result = require_native_engineering_evidence()
        assert result["full_probe_passed"] is False
        assert result["local_prerequisites"] == "passed"
        assert result["limitations"] == report["limitations"]
    else:
        with pytest.raises(ExplorationError, match="native_engineering_evidence_unavailable"):
            require_native_engineering_evidence()
