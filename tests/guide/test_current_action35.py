"""P35当前唯一动作语气和完整历史回放；零模型、零PG。"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import round29_task as task
from scripts.check_round31 import restore
from services.guide.action_contract import (
    ACTION_CONTRACT_VERSION, _explanation, action_digest,
    render_action_contract, validate_action_contract,
)
from services.guide.assistant import Chat, display_state, validate_reply
from test_action_contract31 import candidate, validate, tool
from test_action_delivery31 import harness
from test_assistant import knowledge, observation
from test_reply_receipts30 import app_for, post, receipt
from test_round31_entry import confirmation
from test_shared_prompt21 import body


FIXTURES = Path(__file__).parent / "fixtures"
Q25_CONTENT_SHA256 = "ff09a538e9e341f20839444eb5fbec6c688b134af5d46a9df5fff0a0031c0bb3"


def saved(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def current_harness(tmp_path, value, *, raw=None):
    h = harness(tmp_path, value, raw=raw)
    release = h.releases.save_candidate(
        knowledge(), [observation("profile"), observation("trips")],
        prompt_version="assistant-10")
    h.releases.publish(release["version"])
    return h


@pytest.mark.parametrize("field", ["reason", "conditions", "check"])
@pytest.mark.parametrize("target", ["trip_1", "trip_2"])
@pytest.mark.parametrize("text", [
    "需先锁价以确定券归属与{ref}最终应付额。",
    "需要先为{ref}锁价。",
    "必须对{ref}锁定报价。",
    "本步应当为{ref}尝试锁价。",
    "应该先给{ref}锁定价格。",
    "当前{ref}应先锁价。",
    "{ref}报价尚未锁定；需先为{ref}锁价。",
])
def test_current_modal_restatement_keeps_explicit_same_target(field, target, text):
    value = candidate(target=target)
    value[field] = text.format(ref=target)
    assert validate(value) == value
    assert value[field] in render_action_contract(value)


@pytest.mark.parametrize("kind,text", [
    ("pay", "需要先为trip_2支付。"),
    ("cancel", "必须为trip_2取消锁价。"),
    ("requote", "应当先对trip_2重新核价。"),
    ("query", "需先查询trip_2付款状态。"),
])
def test_modal_restatement_is_not_a_lock_or_first_ref_policy(kind, text):
    value = candidate(kind, "trip_2"); value["reason"] = text
    assert validate(value) == value


@pytest.mark.parametrize("field", ["reason", "conditions", "check"])
@pytest.mark.parametrize("text", [
    "需先锁价。", "需要为当前行程锁价。", "必须先给trip_2锁价。",
    "需先为trip_1支付。", "应该为trip_1重新核价。",
    "成功后需先给trip_1锁价。", "若本步成功；必须为trip_1锁价。",
    "如果失败，应先给trip_1锁价。", "若trip_1锁价成功，则需先为trip_2锁价。",
    "需先为trip_1锁价，然后给trip_1付款。",
    "需要为trip_1锁价；随后对trip_2重新核价。",
    "需先为trip_1锁价。再对trip_2重新核价。",
    "必须为trip_1锁价、再次为trip_1锁价。",
    "需先给trip_1锁价，失败后请重试。",
    "需先对trip_1锁价并打开付款资料。",
    "需先给trip_1锁价，之后查询付款状态。",
    "需先给trip_1锁价，若成功，必须先给trip_1付款。",
    "需先给trip_1锁价，重复执行。",
    "需先给trip_1锁价，重新核价trip_2。",
])
def test_modal_words_do_not_exempt_later_other_or_repeated_operations(field, text):
    value = candidate(); value[field] = text
    with pytest.raises(ValueError, match="action_explanation_route"):
        validate(value)


def test_modal_restatement_does_not_authorize_an_extra_tool():
    value = candidate(); value["reason"] = "需先为trip_1锁价。"
    with pytest.raises(ValueError, match="action_tool_mismatch"):
        validate(value, calls=[tool()])


@pytest.mark.parametrize("name", [
    "q22-current-action.json", "q23-current-lock.json",
    "q23-current-query.json", "q24-step-result.json",
])
def test_complete_legal_history_replays_under_current_prompt_and_complete_confirmation(tmp_path, monkeypatch, name):
    async def run():
        fixture = saved(name)
        raw = fixture["candidate"]["content"]; value = json.loads(raw)
        assert hashlib.sha256(raw.encode()).hexdigest() == fixture["candidate_content_sha256"]
        state = restore(fixture["current_state"])
        assert display_state(state, "assistant-10") == fixture["current_state"]
        assert validate_action_contract(raw, state, fixture["allowed_pages"], []) == value
        rendered = render_action_contract(value)
        assert validate_reply(rendered, state, fixture["current_observation"], prompt_version="assistant-10") == rendered
        h = current_harness(tmp_path, value, raw=raw); h.facts = state
        request = body().model_dump()
        request.update(page=fixture["current_observation"]["page"], observation={
            k:v for k,v in fixture["current_observation"].items() if k != "source_kind"})
        response = await post(app_for(monkeypatch, h), Chat(**request))
        assert response.status_code == 200
        formal = response.json()
        assert formal["reply"] == rendered
        assert formal["action_contract"] == value
        assert formal["action_contract_version"] == ACTION_CONTRACT_VERSION == "single-action-8"
        assert formal["action_sha256"] == action_digest(value)
        assert all(value[field] in rendered for field in ("reason", "conditions", "check"))
        report = json.loads(next(h.report_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert report["candidate"] == fixture["candidate"]
        assert report["prompt_version"] == "assistant-10"
        context = json.loads(h.calls[0]["messages"][-1]["content"])
        assert context["current_state"] == fixture["current_state"]
        assert context["current_observation"] == fixture["current_observation"]
        confirmed = confirmation(formal)
        assert (confirmed["action"], confirmed["object"]) == (value["action"]["kind"], value["action"]["target"])
        assert task.validate_confirmation(confirmed, formal, task_ref="p31-test", turn=1) == confirmed
        for field in ("reason", "conditions", "check"):
            changed = deepcopy(formal); changed["action_contract"][field] += "changed"
            with pytest.raises(RuntimeError):
                task.validate_confirmation(confirmed, changed, task_ref="p31-test", turn=1)
        for field in ("reply", "action_contract_version", "action_sha256"):
            changed = deepcopy(formal); changed[field] += "changed"
            with pytest.raises(RuntimeError):
                task.validate_confirmation(confirmed, changed, task_ref="p31-test", turn=1)
    asyncio.run(run())


def test_q25_original_contract_rejects_real_later_action_after_current_modal_repair():
    fixture = saved("q25-single-step.json")
    raw = fixture["candidate"]["content"]
    assert hashlib.sha256(raw.encode()).hexdigest() == Q25_CONTENT_SHA256
    value = json.loads(raw); action = value["action"]
    # 此独立正例仅用于证明当前动作复述；从不替换原候选或作为正式成功。
    assert _explanation("需先锁价以确定券归属与trip_1最终应付额。", action, "reason") is None
    with pytest.raises(ValueError, match="action_explanation_route"):
        validate_action_contract(raw, restore(fixture["current_state"]), fixture["allowed_pages"], [])
    with pytest.raises(ValueError, match="action_explanation_route"):
        _explanation("再对trip_2重新核价。", action, "reason")
    for field in ("conditions", "check"):
        assert _explanation(value[field], action, field) is None
    # 完整确定性呈现独立交给原事实守卫，和契约拒绝不是同一条栈。
    with pytest.raises(ValueError, match="action_fact_ambiguous"):
        validate_reply(render_action_contract(value), restore(fixture["current_state"]),
                       fixture["current_observation"], prompt_version="assistant-10")


def test_q25_full_original_formal_replay_still_fails_without_confirmation_or_rewriting(tmp_path, monkeypatch):
    async def run():
        fixture = saved("q25-single-step.json"); raw = fixture["candidate"]["content"]
        assert hashlib.sha256(raw.encode()).hexdigest() == Q25_CONTENT_SHA256
        h = current_harness(tmp_path, json.loads(raw), raw=raw)
        h.facts = restore(fixture["current_state"])
        request = body().model_dump()
        request.update(page=fixture["current_observation"]["page"], observation={
            k:v for k,v in fixture["current_observation"].items() if k != "source_kind"})
        response = await post(app_for(monkeypatch, h), Chat(**request))
        assert response.status_code == 503
        assert response.json() == {"error":"assistant_result_unavailable"}
        assert receipt(h)["reason"] == "action_explanation_route"
        report = json.loads(next(h.report_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert report["candidate"] == fixture["candidate"]
        assert report["prompt_version"] == "assistant-10"
        assert all(session.pending is None and not session.history for session in h.sessions.values())
    asyncio.run(run())


@pytest.mark.parametrize("field", ["reason", "conditions", "check"])
@pytest.mark.parametrize("claim,reason", [
    ("trip_1的报价为999模拟元。", "amount_unverified"),
    ("trip_1锁价当前有效。", "price_validity_conflict"),
    ("trip_2的报价为1000模拟元。", "shared_amount_attribution_unverified"),
    ("两个行程已经完成。", "shared_completion_unverified"),
    ("预估合计1550模拟元，报价、库存和有效期仍满足。", "shared_total_conditions_missing"),
])
def test_current_modal_restatement_never_proves_amount_validity_attribution_qualification_or_completion(tmp_path, monkeypatch, field, claim, reason):
    async def run():
        value = candidate(); value[field] = "需先为trip_1锁价；" + claim
        assert validate(value) == value
        h = current_harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503 and receipt(h)["reason"] == reason
    asyncio.run(run())
