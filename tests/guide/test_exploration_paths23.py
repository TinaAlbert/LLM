"""P23 路径/报告独占/完整证据门；全部使用本地合成字节，模型及原生进程0。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.guide import manage, windows
from services.guide.exploration_runtime import ExplorationRuntime, require_native_engineering_evidence
from scripts import round22_explore, round23_explore


def report_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, "ROOT", tmp_path)
    monkeypatch.setattr(manage, "ROOT", tmp_path)
    source = tmp_path / "services/guide/shell.c"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic source; never compiled")
    binary = tmp_path / "explore-shell.exe"
    binary.write_bytes(b"synthetic bytes; never launched")
    monkeypatch.setattr(windows, "exploration_executable", lambda: binary)
    checks = [{"resource": name, "result": "denied\t5"} for name in windows.EXPLORATION_FILE_CHECKS]
    checks.append({"resource": "child-process", "result": "spawn\t367"})
    checks.extend({"resource": name, "result": "socket\t10013", "host_control": True,
                   "child_received": False} for name in (
                       "live-tcp4", "live-tcp6", "live-udp4", "live-udp6", "live-lan-udp4"))
    checks.extend({"resource": name, "result": "socket\t10013", "host_control": True}
                  for name in ("postgres", "business-http", "arbitrary-network", "arbitrary-tcp6",
                               "arbitrary-udp4", "arbitrary-udp6"))
    report = {"schema_version": 1, "stage": "complete", "passed": True, "profile": windows.PROFILE,
              "appcontainer_started": True, "child_reaped": True, "host_spawn_exit": 11,
              "network_capabilities": [], "loopback_exempt": False,
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "launcher_sha256": hashlib.sha256(Path(windows.__file__).read_bytes()).hexdigest(),
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks, "limitations": []}
    return report


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")


def test_selected_report_does_not_fallback_to_old_report(tmp_path, monkeypatch):
    report = report_fixture(tmp_path, monkeypatch)
    original = tmp_path / windows.EXPLORATION_REPORT
    selected = tmp_path / ".local/d3-23/native-isolation.json"
    write_report(original, report)
    write_report(selected, {**report, "passed": False})
    windows.require_exploration_evidence()
    with pytest.raises(windows.IsolationUnavailable):
        windows.require_exploration_evidence(report_path=selected)
    write_report(original, {**report, "passed": False})
    write_report(selected, report)
    windows.require_exploration_evidence(report_path=".local/d3-23/native-isolation.json")
    assert require_native_engineering_evidence(report_path=selected)["full_probe_passed"] is True
    with pytest.raises(windows.IsolationUnavailable):
        windows.require_exploration_evidence()


@pytest.mark.parametrize("change", [{"stage": "probing"}, {"stage": "failed"},
                                    {"appcontainer_started": False}, {"child_reaped": False}])
def test_incomplete_selected_lifecycle_refused(tmp_path, monkeypatch, change):
    report = report_fixture(tmp_path, monkeypatch)
    selected = tmp_path / ".local/d3-23/native-isolation.json"
    write_report(selected, {**report, **change})
    with pytest.raises(windows.IsolationUnavailable):
        windows.require_exploration_evidence(report_path=selected)


def test_existing_probe_report_refused_before_native_or_network(tmp_path, monkeypatch):
    report_fixture(tmp_path, monkeypatch)
    selected = tmp_path / ".local/d3-23/native-isolation.json"
    original = tmp_path / windows.EXPLORATION_REPORT
    write_report(selected, {"passed": False, "preserve": "current failure"})
    write_report(original, {"passed": False, "preserve": "old failure"})
    before = {path: path.read_bytes() for path in (selected, original)}
    monkeypatch.setattr(windows, "exploration_executable", lambda: pytest.fail("existing report launched probe"))
    monkeypatch.setattr(manage.socket, "socket", lambda *_: pytest.fail("existing report used network"))
    with pytest.raises(RuntimeError, match="exploration_report_exists_choose_new_path"):
        manage.probe_exploration(report_path=selected)
    with pytest.raises(RuntimeError, match="exploration_report_exists_choose_new_path"):
        manage.probe_exploration()
    assert before == {path: path.read_bytes() for path in (selected, original)}


def test_started_probe_failure_stays_in_new_report(tmp_path, monkeypatch):
    report_fixture(tmp_path, monkeypatch)
    selected = tmp_path / ".local/d3-23/native-isolation.json"
    original = tmp_path / windows.EXPLORATION_REPORT
    write_report(original, {"preserve": "old failure"})
    before = original.read_bytes()
    def unavailable():
        raise windows.IsolationUnavailable("synthetic_no_native_probe")
    monkeypatch.setattr(windows, "exploration_executable", unavailable)
    with pytest.raises(RuntimeError, match="隔离探测未完整通过"):
        manage.probe_exploration(report_path=selected)
    saved = json.loads(selected.read_text(encoding="utf-8"))
    assert saved["stage"] == "failed" and saved["passed"] is False
    assert saved["failure_code"] == "synthetic_no_native_probe"
    assert original.read_bytes() == before
    with pytest.raises(RuntimeError, match="exploration_report_exists_choose_new_path"):
        manage.probe_exploration(report_path=selected)


def test_selected_report_rechecked_before_each_native_candidate(tmp_path, monkeypatch):
    selected = tmp_path / ".local/d3-23/native-isolation.json"
    calls = []
    def evidence(*, report_path):
        calls.append(report_path)
        if len(calls) > 1:
            raise windows.IsolationUnavailable()
    monkeypatch.setattr(windows, "require_exploration_evidence", evidence)
    monkeypatch.setattr(windows, "Child", lambda **_: pytest.fail("changed report created child"))
    with windows.begin_exploration("0123456789abcdef0123456789abcdef", 180, report_path=selected) as session:
        with pytest.raises(windows.IsolationUnavailable):
            session.authorize({}, "observe", None, 1)
    assert calls == [selected, selected]


def test_runtime_passes_selected_report_into_default_native_entry(tmp_path, monkeypatch):
    selected = tmp_path / ".local/d3-23/native-isolation.json"
    calls = []
    def begin(reference, seconds, *, report_path):
        calls.append(report_path)
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(windows, "begin_exploration", begin)
    item = ExplorationRuntime(SimpleNamespace(dispose=lambda: None), report_path=selected)
    item.close()
    assert calls == [selected]


def test_round23_server_and_runner_use_same_report_without_old_output(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(round22_explore, "configure_server", lambda **kwargs: calls.append(kwargs) or ("app", "settings"))
    marker = tmp_path / "consult"
    assert round23_explore.configure_server(marker_directory=marker, scope="P23-consult") == ("app", "settings")
    assert calls[0] == {"marker_directory": marker, "scope": "P23-consult", "binding_path": "/__round23/binding",
                        "report_path": round23_explore.REPORT, "exclusive": True}
    monkeypatch.setattr(round22_explore, "run", lambda **kwargs: calls.append(kwargs))
    round23_explore.run(server_marker_directory=marker, server_scope="P23-consult")
    assert calls[1]["report_path"] == calls[0]["report_path"]
    assert calls[1]["output_directory"] == round23_explore.OUT
    assert calls[1]["server_marker_directory"] == marker
    assert calls[1]["scope"] == "P23-consult"
    assert "d3-22" not in str(calls)


def test_exclusive_server_marker_cannot_overwrite(tmp_path):
    round22_explore.save("server.json", {"first": True}, directory=tmp_path, exclusive=True)
    with pytest.raises(FileExistsError):
        round22_explore.save("server.json", {"first": False}, directory=tmp_path, exclusive=True)
    assert json.loads((tmp_path / "server.json").read_text(encoding="utf-8")) == {"first": True}


def test_real_server_configuration_records_normalized_selected_report(tmp_path, monkeypatch):
    source = tmp_path / "services/business/app.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fake app; no server started")
    monkeypatch.setattr(round22_explore, "ROOT", tmp_path)
    monkeypatch.setattr(round22_explore, "configuration", lambda: ({}, "settings"))
    monkeypatch.setattr(round22_explore.sandbox, "check_cluster", lambda: {"instance_id": "synthetic-test"})
    routes = {}
    class App:
        def get(self, route):
            def attach(function):
                routes[route] = function
                return function
            return attach
    app = App()
    monkeypatch.setattr(round22_explore, "create_app", lambda settings: app)
    marker = tmp_path / ".local/d3-23/consult"
    selected = tmp_path / ".local/d3-23/native-isolation.json"
    assert round22_explore.configure_server(marker_directory=marker, scope="P23-consult",
        binding_path="/__round23/binding", report_path=selected, exclusive=True) == (app, "settings")
    saved = json.loads((marker / "server.json").read_text(encoding="utf-8"))
    assert saved["exploration_report"] == ".local/d3-23/native-isolation.json"
    assert routes["/__round23/binding"]() == saved
    with pytest.raises(FileExistsError):
        round22_explore.configure_server(marker_directory=marker, scope="P23-consult",
            binding_path="/__round23/binding", report_path=selected, exclusive=True)
