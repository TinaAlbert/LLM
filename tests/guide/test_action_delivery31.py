"""P31正式ASGI交付、逐字段事实及新旧版保留；零模型/PG。"""
import asyncio
from copy import deepcopy
import hashlib
import json

import pytest
from fastapi import HTTPException

from services.guide.assistant import SYSTEM_V6, SYSTEM_V7, SYSTEM_V8
from services.guide.action_contract import action_digest, render_action_contract
from test_assistant import Request, knowledge, observation
from test_shared_prompt21 import SharedHarness, body, facts
from test_reply_receipts30 import app_for, post, receipt


def contract(kind="lock", target="trip_1", **fields):
    entries = {"lock": "booking.lock", "pay": "booking.pay", "query": "booking.reconcile",
               "requote": "booking.quote", "cancel": "booking.cancel_lock", "navigate": "navigate"}
    return {"v": 1, "action": {"kind": kind, "target": target,
        "entry": entries.get(kind, "task"), "params": {}},
        "reason": "按当前公开资料继续。", "conditions": "报价、库存、期限和资格仍满足。",
        "check": "本步返回的实际结果。", **fields}


def harness(tmp_path, value, calls=None, *, raw=None):
    h = SharedHarness(tmp_path, {"content": raw if raw is not None else json.dumps(value, ensure_ascii=False), "tool_calls": calls or []})
    h.facts = facts()
    release = h.releases.save_candidate(knowledge(), [observation("profile"), observation("trips")],
                                       prompt_version="assistant-9")
    h.releases.publish(release["version"])
    return h


def test_formal_delivery_preserves_raw_candidate_and_binds_rendered_action(tmp_path, monkeypatch):
    async def run():
        value = contract()
        h = harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 200
        formal = response.json()
        assert formal["action_contract"] == value
        assert formal["action_contract_version"] == "single-action-8"
        assert formal["action_sha256"] == action_digest(value)
        assert formal["reply"] == render_action_contract(value)
        assert "trip_1" in formal["reply"] and "当前动作：" in formal["reply"]
        report = json.loads(next(h.report_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert json.loads(report["candidate"]["content"]) == value
        assert report["candidate"]["content"] != formal["reply"]
    asyncio.run(run())


@pytest.mark.parametrize("field", ["reason", "conditions", "check"])
def test_all_explanatory_fields_keep_amount_guard(tmp_path, monkeypatch, field):
    async def run():
        h = harness(tmp_path, contract(**{field: "trip_1的报价为999模拟元。"}))
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503 and response.json() == {"error": "assistant_result_unavailable"}
        assert receipt(h)["reason"] == "amount_unverified"
        assert all(s.pending is None and not s.history for s in h.sessions.values())
    asyncio.run(run())


def test_action_target_binds_unqualified_price_claim(tmp_path, monkeypatch):
    async def run():
        # selected_ref=trip_1；trip_2动作的说明不能借trip_1有效报价自证。
        value = contract(target="trip_2", reason="报价当前有效。")
        h = harness(tmp_path, value)
        h.facts["delegated_trips"][1]["data"]["quote"].update(valid=False, effective_status="expired")
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503
        assert receipt(h)["reason"] == "price_validity_conflict"
    asyncio.run(run())


def test_navigation_requires_matching_tool_and_actual_ack(tmp_path):
    async def run():
        value = contract("navigate", "task", conditions="等待浏览器回执。", check="目标页面是否呈现。")
        value["action"]["params"] = {"page": "profile"}
        calls = [{"id": "synthetic-call", "name": "navigate", "arguments": {"page": "profile"}}]
        h = harness(tmp_path, value, calls)
        formal = await h.chat(body(), Request())
        assert formal["navigation_status"] == "proposed"
        assert formal["tool"]["arguments"] == {"page": "profile"}
        report = json.loads(next(h.report_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert "browser_ack" not in report
        from services.guide.assistant import Ack
        done = await h.ack(Ack(session_id=formal["session_id"], nonce=formal["tool"]["nonce"],
                              success=True, observation=observation("profile")), Request())
        assert done["navigation_status"] == "succeeded"
        with pytest.raises(HTTPException):
            await h.ack(Ack(session_id=formal["session_id"], nonce=formal["tool"]["nonce"],
                           success=True, observation=observation("profile")), Request())
    asyncio.run(run())


def test_historical_systems_preserved_exactly():
    expected = ["ed1fc7459c59107e83cd99ab80c72e244b9c31aa6d2f444580a5100f08eec02e",
                "e1d664d31c5b69413ee4b6be91da3fb2bf9f0f8c9a0cc5660df16a3f027bdd89"]
    assert hashlib.sha256(SYSTEM_V6.encode()).hexdigest() == expected[0]
    assert hashlib.sha256(SYSTEM_V8.encode()).hexdigest() == expected[1]
    assert SYSTEM_V7.startswith(SYSTEM_V6)


def test_parallel_object_price_assertion_does_not_skip_invalid_second_quote(tmp_path, monkeypatch):
    async def run():
        h = harness(tmp_path, contract(reason="trip_1与trip_2报价当前有效。"))
        h.facts["delegated_trips"][1]["data"]["quote"].update(valid=False, effective_status="expired")
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503 and receipt(h)["reason"] == "action_fact_ambiguous"
    asyncio.run(run())
