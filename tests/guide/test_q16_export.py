"""Q16精确双scope外发许可：纯离线，不读取本机许可/账本、不连接模型或PG。"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from services.guide import assistant as module
from services.guide.assistant import Assistant, authorize_shared_export, corporate_export_scope
from services.guide.model import ModelResult, ModelUnavailable, TokenUsage
from services.guide.releases import Releases
from test_shared_prompt21 import payload


def samples():
    initial = payload()
    following = deepcopy(initial)
    context = json.loads(following["messages"][-1]["content"])
    context["question"] = "第二个委托行程已锁价，请按当前观察继续安排。"
    state = context["current_state"]
    state["shared_coupon"].update(status="held", attribution="delegated_trip", trip_ref="trip_2")
    trip = state["delegated_trips"][1]["data"]
    trip["lock"] = {**trip["quote"], "ref": "lock", "price_basis": "frozen",
                    "effective_status": "held", "coupon_allocation": "held"}
    following["messages"][-1]["content"] = json.dumps(context, ensure_ascii=False)
    return initial, following


def approval(values):
    return {"package": "P22-Q16-two", "approved": True, "source_kind": "explicit_user_reply",
            "scope_hashes": [corporate_export_scope(value) for value in values]}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("index", [0, 1])
def test_q16_each_exact_scope_accepts_only_explicit_path(tmp_path, index):
    values = samples()
    assert corporate_export_scope(values[0]) != corporate_export_scope(values[1])
    path = tmp_path / "q16.json"
    write(path, approval(values))
    assert authorize_shared_export(values[index], path) is None


@pytest.mark.parametrize("changed", ["third", "question", "state", "system", "limit", "history", "tool"])
def test_q16_third_or_changed_field_rejected(tmp_path, changed):
    values = samples()
    path = tmp_path / "q16.json"
    write(path, approval(values))
    value = deepcopy(values[0])
    context = json.loads(value["messages"][-1]["content"])
    if changed in {"third", "question"}:
        context["question"] = "另一项未批准的咨询。"
    elif changed == "state":
        context["current_state"]["shared_coupon"]["status"] = "consumed"
    elif changed == "system":
        value["messages"][0]["content"] += "修改提示内容。"
    elif changed == "limit":
        value["max_tokens"] = 513
    elif changed == "history":
        context["history"] = [{"source_kind": "user_reported", "text": "新增未审历史。"}]
    elif changed == "tool":
        value["tools"] = [{"type": "function", "function": {"name": "unapproved_tool"}}]
    value["messages"][-1]["content"] = json.dumps(context, ensure_ascii=False)
    with pytest.raises(ModelUnavailable, match="shared_export_not_approved"):
        authorize_shared_export(value, path)


@pytest.mark.parametrize("schema", ["package", "false", "numeric_true", "source", "extra", "missing",
                                    "one", "three", "duplicate", "malformed", "integer", "nested"])
def test_q16_rejects_wrong_approval_schema(tmp_path, schema):
    values = samples()
    document = approval(values)
    if schema == "package":
        document["package"] = "P22-other"
    elif schema == "false":
        document["approved"] = False
    elif schema == "numeric_true":
        document["approved"] = 1
    elif schema == "source":
        document["source_kind"] = "model_claim"
    elif schema == "extra":
        document["schema_version"] = 1
    elif schema == "missing":
        document.pop("source_kind")
    elif schema == "one":
        document["scope_hashes"].pop()
    elif schema == "three":
        document["scope_hashes"].append("a" * 64)
    elif schema == "duplicate":
        document["scope_hashes"][1] = document["scope_hashes"][0]
    elif schema == "malformed":
        document["scope_hashes"][1] = "not-a-reviewed-hash"
    elif schema == "integer":
        document["scope_hashes"][1] = 9
    elif schema == "nested":
        document["scope_hashes"][1] = ["a" * 64]
    path = tmp_path / "q16.json"
    write(path, document)
    with pytest.raises(ModelUnavailable, match="shared_export_not_approved"):
        authorize_shared_export(values[0], path)


def test_default_path_cannot_automatically_adopt_q16_two_scope_file(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    values = samples()
    default = tmp_path / ".local/d3-21/shared-export-approval.json"
    write(default, approval(values))
    for value in values:
        with pytest.raises(ModelUnavailable, match="shared_export_not_approved"):
            authorize_shared_export(value)
    # 原单样本门仍保留，不能通过Q16附带扩大默认入口。
    write(default, {"package": "P21-shared-one", "approved": True,
                    "source_kind": "explicit_user_reply", "scope_hash": corporate_export_scope(values[0])})
    assert authorize_shared_export(values[0]) is None
    with pytest.raises(ModelUnavailable, match="shared_export_not_approved"):
        authorize_shared_export(values[1])


@pytest.mark.parametrize("explicit", [True, False])
def test_assistant_constructor_routes_gate_before_offline_adapter(tmp_path, monkeypatch, explicit):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    values = samples()
    path = tmp_path / "q16.json"
    write(path, approval(values))
    # 默认入口即使发现一个两scope文件也必须拒绝。
    write(tmp_path / ".local/d3-21/shared-export-approval.json", approval(values))
    calls = []

    def factory(validator):
        async def generate(value, deadline):
            validator(value)
            calls.append(value)
            return ModelResult({"content": "离线运输检查。", "tool_calls": []},
                               TokenUsage(1, 1, 2, 0, 1), "offline-q16", Decimal("0"), "offline-q16")
        return SimpleNamespace(generate_experiment=generate)

    arguments = {"shared_export_approval_path": path} if explicit else {}
    item = Assistant(SimpleNamespace(workspace_id="test"),
                     releases=Releases("test", tmp_path / "releases.json"),
                     adapter_factory=factory, report_dir=tmp_path / "runs", **arguments)
    assert item.shared_export_approval_path == (path if explicit else None)
    item.external_model = True  # 只打开外发门；适配器仍为上述明确离线替身。

    async def run():
        for value in values:
            if explicit:
                result, report = await item.infer(value, "chat", "builtin-v1")
                assert result.candidate["content"] == "离线运输检查。"
                assert report.parent == tmp_path / "runs"
            else:
                with pytest.raises(ModelUnavailable, match="shared_export_not_approved"):
                    await item.infer(value, "chat", "builtin-v1")

    asyncio.run(run())
    assert len(calls) == (2 if explicit else 0)
    assert (tmp_path / "runs").exists() is explicit
