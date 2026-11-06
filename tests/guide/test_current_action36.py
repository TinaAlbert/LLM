"""P36有限当前动作修饰与本步返回核对；只用精筛合成夹具，零模型/PG。"""
import asyncio
from copy import deepcopy
import hashlib
import json

import pytest

from scripts import round29_task as task
from scripts.check_round31 import restore
from services.guide.action_contract import (
    ACTION_CONTRACT_VERSION, action_digest, render_action_contract,
    validate_action_contract,
)
from services.guide.assistant import Chat, display_state, validate_reply
from test_action_contract31 import candidate, tool, validate
from test_current_action35 import current_harness, saved
from test_reply_receipts30 import app_for, post, receipt
from test_round31_entry import confirmation
from test_shared_prompt21 import body


Q26_CONTENT_SHA256 = "7f8d383a663ffb674ad0931971643d01524b9c428b396292b053a88e75474e68"


def test_q26_complete_original_formal_replay_keeps_facts_and_confirmation_binding(tmp_path, monkeypatch):
    async def run():
        fixture = saved("q26-current-result.json")
        raw = fixture["candidate"]["content"]
        assert hashlib.sha256(raw.encode()).hexdigest() == fixture["candidate_content_sha256"] == Q26_CONTENT_SHA256
        value = json.loads(raw)
        state = restore(fixture["current_state"])
        assert display_state(state, "assistant-10") == fixture["current_state"]
        assert validate_action_contract(raw, state, fixture["allowed_pages"], []) == value
        rendered = render_action_contract(value)
        assert validate_reply(rendered, state, fixture["current_observation"], prompt_version="assistant-10") == rendered
        h = current_harness(tmp_path, value, raw=raw)
        h.facts = state
        request = body().model_dump()
        request.update(page=fixture["current_observation"]["page"], observation={
            key: val for key, val in fixture["current_observation"].items() if key != "source_kind"})
        response = await post(app_for(monkeypatch, h), Chat(**request))
        assert response.status_code == 200
        formal = response.json()
        assert formal["action_contract_version"] == ACTION_CONTRACT_VERSION == "single-action-8"
        assert formal["action_contract"] == value
        assert formal["action_sha256"] == action_digest(value)
        assert formal["reply"] == rendered
        assert all(value[field] in rendered for field in ("reason", "conditions", "check"))
        report = json.loads(next(h.report_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert report["candidate"] == fixture["candidate"]
        assert report["prompt_version"] == "assistant-10"
        observed = json.loads(h.calls[0]["messages"][-1]["content"])
        assert observed["current_state"] == fixture["current_state"]
        assert observed["current_observation"] == fixture["current_observation"]
        confirmed = confirmation(formal)
        assert (confirmed["action"], confirmed["object"]) == ("lock", "trip_1")
        assert confirmed["quote"] == rendered.splitlines()[0]
        assert task.validate_confirmation(confirmed, formal, task_ref="p31-test", turn=1) == confirmed
        for field in ("reason", "conditions", "check"):
            changed = deepcopy(formal)
            changed["action_contract"][field] += "changed"
            with pytest.raises(RuntimeError):
                task.validate_confirmation(confirmed, changed, task_ref="p31-test", turn=1)
        for field in ("reply", "action_contract_version", "action_sha256"):
            changed = deepcopy(formal)
            changed[field] += "changed"
            with pytest.raises(RuntimeError):
                task.validate_confirmation(confirmed, changed, task_ref="p31-test", turn=1)
    asyncio.run(run())


@pytest.mark.parametrize("field", ["reason", "conditions", "check"])
def test_selected_qualified_current_action_still_requires_explicit_same_target(field):
    value = candidate(target="trip_2")
    value[field] = "需先对所选trip_2执行合格锁价。"
    assert validate(value) == value


def test_returned_lock_result_list_keeps_an_explicit_neutral_target():
    value = candidate(target="trip_2")
    value["check"] = "核对trip_2锁价返回的归属券ref、frozen金额与锁有效性状态。"
    assert validate(value) == value
    assert value["check"] in render_action_contract(value)


@pytest.mark.parametrize("field,text", [
    ("reason", "需先对所选trip_2执行合格锁价。"),
    ("reason", "需先对所选行程执行合格锁价。"),
    ("reason", "需先对所选trip_1执行合格锁价；成功后对trip_1付款。"),
    ("reason", "需先对所选trip_1执行合格锁价；再次为trip_1锁价。"),
    ("check", "核对trip_2锁价返回的归属券ref、frozen金额与锁有效性状态。"),
    ("check", "核对锁价返回的归属券ref、frozen金额与锁有效性状态。"),
    ("check", "核对trip_1锁价返回的归属券ref，随后查询trip_1付款状态。"),
    ("check", "核对trip_1锁价返回的归属券ref、frozen金额与锁有效性状态；再对trip_2重新核价。"),
])
def test_new_wording_does_not_exempt_other_missing_later_or_repeated_actions(field, text):
    value = candidate()
    value[field] = text
    with pytest.raises(ValueError, match="action_explanation_route"):
        validate(value)


def test_current_modifier_cannot_authorize_an_extra_tool():
    value = candidate()
    value["reason"] = "需先对所选trip_1执行合格锁价。"
    with pytest.raises(ValueError, match="action_tool_mismatch"):
        validate(value, calls=[tool()])


@pytest.mark.parametrize("claim,error", [
    ("trip_1的报价为999模拟元。", "amount_unverified"),
    ("trip_1锁价当前有效。", "price_validity_conflict"),
])
def test_current_modifiers_and_returned_result_words_keep_independent_fact_guards(tmp_path, monkeypatch, claim, error):
    async def run():
        value = candidate()
        value["reason"] = "需先对所选trip_1执行合格锁价；" + claim
        value["check"] = "核对trip_1锁价返回的归属券ref、frozen金额与锁有效性状态。"
        assert validate(value) == value
        h = current_harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503
        assert receipt(h)["reason"] == error
        assert all(session.pending is None and not session.history for session in h.sessions.values())
    asyncio.run(run())
