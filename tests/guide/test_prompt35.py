"""P35新版生成约束与既有正式链；全为合成离线模型替身，无付费请求。"""
import asyncio
from copy import deepcopy
import hashlib
import json

import pytest

from services.guide.assistant import (SYSTEM_V9, SYSTEM_V10, display_state, prompt_system,
    trim_shared_history, document_only_payload)
from services.guide.action_contract import ACTION_CONTRACT_VERSION, action_digest, render_action_contract
from services.guide.experiment import MAX_INPUT_BYTES, input_size_bytes
from test_assistant import knowledge, observation
from test_shared_prompt21 import SharedHarness, body, facts, oversized_history_payload
from test_reply_receipts30 import app_for, post, receipt


def value(**fields):
    return {"v": 1, "action": {"kind": "lock", "target": "trip_2", "entry": "booking.lock", "params": {}},
            "reason": "按本轮可见资料执行本步。", "conditions": "仅依据当前可见资料。",
            "check": "核对trip_2锁价结果与有效性。", **fields}


def harness(tmp_path, candidate):
    h = SharedHarness(tmp_path, {"content": json.dumps(candidate, ensure_ascii=False), "tool_calls": []})
    h.facts = facts()
    release = h.releases.save_candidate(knowledge(), [observation("profile"), observation("trips")],
                                       prompt_version="assistant-10")
    h.releases.publish(release["version"])
    return h


def test_ten_is_separate_version_and_nine_is_preserved():
    assert hashlib.sha256(SYSTEM_V9.encode()).hexdigest() == "726b82d1db38ff963610f227836df8ac8d52dc42b68a30cd91659d6799dbe090"
    assert prompt_system("assistant-9") == SYSTEM_V9
    assert prompt_system("assistant-10") == SYSTEM_V10
    assert SYSTEM_V10.startswith(SYSTEM_V9) and SYSTEM_V10 != SYSTEM_V9
    added = SYSTEM_V10[len(SYSTEM_V9):]
    for rule in ("reason仅解释", "conditions仅写当前必要事实", "逐对象分句且明示ref", "check仅核对本步返回或预期结果",
                 "后续计划", "另一对象动作", "条件成立后的动作", "再次或重复执行", "保留未知"):
        assert rule in added
    for answer in ("DEV27", "trip_1", "trip_2", "1400", "1480", "先大后小", "最优", "LARGE", "SMALL"):
        assert answer not in added


def test_ten_keeps_same_facts_document_and_b0_difference():
    state = facts(); saved = deepcopy(state)
    assert display_state(state, "assistant-10") == display_state(state, "assistant-9")
    assert state == saved
    payload = {"messages": [{"role": "system", "content": SYSTEM_V10},
                             {"role": "user", "content": json.dumps({"current_state": display_state(state, "assistant-10"),
                               "question": "核对当前情况。", "history": []}, ensure_ascii=False)}],
               "tools": [], "max_tokens": 512, "json_output": False}
    baseline = document_only_payload(payload)
    assert baseline["messages"][0] == payload["messages"][0]
    assert "current_state" not in json.loads(baseline["messages"][-1]["content"])


def test_ten_trims_only_old_optional_history():
    payload = oversized_history_payload(); payload["messages"][0]["content"] = SYSTEM_V10
    before = deepcopy(payload)
    assert input_size_bytes(payload) > MAX_INPUT_BYTES
    result = trim_shared_history(payload)
    old = json.loads(before["messages"][-1]["content"])
    new = json.loads(result["messages"][-1]["content"])
    assert payload == before and input_size_bytes(result) <= MAX_INPUT_BYTES
    assert len(new["history"]) < len(old["history"])
    assert {k: v for k, v in new.items() if k != "history"} == {k: v for k, v in old.items() if k != "history"}
    assert result["messages"][0]["content"] == SYSTEM_V10


def test_ten_formal_chain_preserves_complete_raw_candidate_and_new_version(tmp_path, monkeypatch):
    async def run():
        candidate = value(); h = harness(tmp_path, candidate)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 200
        formal = response.json()
        assert formal["action_contract"] == candidate
        assert formal["action_contract_version"] == ACTION_CONTRACT_VERSION
        assert formal["action_sha256"] == action_digest(candidate)
        assert formal["reply"] == render_action_contract(candidate)
        report = json.loads(next(h.report_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert json.loads(report["candidate"]["content"]) == candidate
        assert h.calls[0]["messages"][0]["content"] == SYSTEM_V10
    asyncio.run(run())


@pytest.mark.parametrize("field", ["reason", "conditions", "check"])
def test_ten_keeps_original_amount_guard_in_every_field(tmp_path, monkeypatch, field):
    async def run():
        h = harness(tmp_path, value(**{field: "trip_2的报价为999模拟元。"}))
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503 and response.json() == {"error": "assistant_result_unavailable"}
        assert receipt(h)["reason"] == "amount_unverified"
        assert all(not s.history and s.pending is None for s in h.sessions.values())
    asyncio.run(run())


@pytest.mark.parametrize("field", ["reason", "conditions", "check"])
def test_ten_keeps_cross_object_fact_rejection_in_every_field(tmp_path, monkeypatch, field):
    async def run():
        h = harness(tmp_path, value(**{field: "trip_1与trip_2报价当前有效。"}))
        h.facts["delegated_trips"][1]["data"]["quote"].update(valid=False, effective_status="expired")
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503 and receipt(h)["reason"] == "action_fact_ambiguous"
    asyncio.run(run())


def test_ten_does_not_accept_extra_schema_fields(tmp_path, monkeypatch):
    async def run():
        candidate = value(); candidate["later_plan"] = "不应出现的字段"
        h = harness(tmp_path, candidate)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503
        assert receipt(h)["reason"] == "action_fields_invalid"
    asyncio.run(run())
