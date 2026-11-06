"""最小宿主拒绝收据的风险检查；ASGI/fake模型，数据库及模型外发均为0。"""
import asyncio
import json

import httpx
import pytest

from services.business.app import create_app
from services.business.config import Settings
from services.guide import assistant as module
from services.guide.knowledge import fingerprint
from services.guide.model import ModelUnavailable
from test_assistant import Harness, chat_body, tool_candidate


def app_for(monkeypatch, harness):
    monkeypatch.setattr(module, "Assistant", lambda settings: harness)
    return create_app(Settings(database_url="offline-not-connected", workspace_id="test", mode="test"))


async def post(app, body=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/assistant/chat", json=(body or chat_body()).model_dump(mode="json"))


def receipt(harness):
    files = list((harness.report_dir / "rejections").glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_formal_rejection_keeps_generic503_and_minimal_host_receipt(tmp_path, monkeypatch):
    async def scenario():
        candidate = {"content": "请支付123元。", "tool_calls": []}
        h = Harness(tmp_path, candidate)
        app = app_for(monkeypatch, h)
        response = await post(app)
        assert response.status_code == 503
        assert response.json() == {"error": "assistant_result_unavailable"}
        saved = receipt(h)
        reports = list(h.report_dir.glob("*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        assert saved == {"kind": "assistant_reply_rejection", "schema_version": 1,
            "stage": "reply_validation", "reason": "amount_unverified",
            "input_hash": report["input_hash"], "candidate_hash": fingerprint(candidate),
            "inference_report": reports[0].name}
        assert report["status"] == "received" and report["candidate"] == candidate
        session = next(iter(h.sessions.values()))
        assert session.history == [] and session.pending is None
        # 下一次求助不能把拒绝分类或内部收据带进模型上下文。
        candidate["content"] = "请查看付款资料。"
        assert (await post(app, chat_body(next(iter(h.sessions))))).status_code == 200
        next_input = json.dumps(h.calls[1], ensure_ascii=False)
        assert all(text not in next_input for text in (saved["input_hash"], saved["candidate_hash"],
            saved["inference_report"], "amount_unverified", "assistant_reply_rejection"))
    asyncio.run(scenario())


def test_navigation_claim_has_separate_rejection_stage(tmp_path, monkeypatch):
    async def scenario():
        candidate = tool_candidate()
        candidate["content"] = "已经打开付款资料。"
        h = Harness(tmp_path, candidate)
        response = await post(app_for(monkeypatch, h))
        assert response.status_code == 503
        saved = receipt(h)
        assert (saved["stage"], saved["reason"]) == ("navigation_validation", "navigation_unconfirmed")
        assert all(session.pending is None for session in h.sessions.values())
    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["observation_value", "observation_io", "transport", "stale"])
def test_other_failures_never_become_reply_rejections(tmp_path, monkeypatch, source):
    async def scenario():
        h = Harness(tmp_path, {"content": "请支付123元。", "tool_calls": []})
        if source.startswith("observation"):
            async def unavailable(*args, **kwargs):
                raise (ValueError("private-host-detail") if source.endswith("value") else OSError("private-host-path"))
            h.state = unavailable
        elif source == "transport":
            async def unavailable(*args, **kwargs):
                raise ModelUnavailable("model_unavailable")
            h.infer = unavailable
        else:
            h.after = lambda: setattr(h, "facts", {"payment_profile_state": "changed"})
        response = await post(app_for(monkeypatch, h))
        assert response.status_code == (409 if source == "stale" else 503)
        assert not list(h.report_dir.glob("rejections/*.json"))
        assert "private-host" not in response.text
    asyncio.run(scenario())


def test_arbitrary_guard_error_is_never_copied_into_receipt(tmp_path, monkeypatch):
    async def scenario():
        h = Harness(tmp_path, {"content": "离线候选", "tool_calls": []})
        def private_failure(*args, **kwargs):
            raise ValueError("private-host-path-and-secret")
        monkeypatch.setattr(module, "validate_reply", private_failure)
        response = await post(app_for(monkeypatch, h))
        assert response.status_code == 503
        saved = receipt(h)
        assert saved["reason"] == "guard_unclassified"
        assert "private-host" not in json.dumps(saved)
    asyncio.run(scenario())


def test_receipt_uses_actual_inference_hash_after_host_transform(tmp_path, monkeypatch):
    async def scenario():
        h = Harness(tmp_path, {"content": "请支付123元。", "tool_calls": []})
        infer = h.infer
        actual = {}
        async def transformed(payload, kind, version):
            changed = json.loads(json.dumps(payload))
            context = json.loads(changed["messages"][-1]["content"])
            context.pop("current_state")
            changed["messages"][-1]["content"] = json.dumps(context, ensure_ascii=False)
            actual["hash"] = fingerprint(changed)
            assert actual["hash"] != fingerprint(payload)
            return await infer(changed, kind, version)
        h.infer = transformed
        response = await post(app_for(monkeypatch, h))
        assert response.status_code == 503
        assert receipt(h)["input_hash"] == actual["hash"]
    asyncio.run(scenario())
