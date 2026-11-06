"""当前动作说明与未来执行的业务反例；全部零模型、零数据库。"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import round29_task as task
from scripts.check_round31 import restore
from services.guide.action_contract import (
    ACTION_CONTRACT_VERSION, action_digest, render_action_contract, validate_action_contract,
)
from services.guide.assistant import Chat, display_state
from test_action_contract31 import candidate, validate
from test_action_delivery31 import harness
from test_reply_receipts30 import app_for, post, receipt
from test_shared_prompt21 import body
from test_round31_entry import confirmation


@pytest.mark.parametrize('field', ['reason', 'conditions', 'check'])
@pytest.mark.parametrize('text', [
    '先给trip_1锁价以确定本步实际金额。',
    '请为trip_1锁定价格。',
    '当前对trip_1尝试锁价。',
    '本步先为trip_1执行锁价，预期生成冻结金额与券归属。',
    '仅在trip_1报价仍有效时才可锁价。',
    'trip_1锁价后预计生成锁价快照。',
    '本步成功后预期生成查询结果。',
])
def test_current_restatement_condition_and_result_are_preserved(field, text):
    value = candidate(); value[field] = text
    assert validate(value) == value
    assert text in render_action_contract(value)


@pytest.mark.parametrize('field', ['reason', 'conditions', 'check'])
@pytest.mark.parametrize('text', [
    '先给trip_2锁价。', '给trip_2锁价。', '先给trip_1付款。', '给trip_1付款。',
    '先给trip_1锁价，再给trip_2锁价。',
    '先给trip_1锁价；付款。',
    '先给trip_1锁价，之后查询状态。',
    '本步成功后，查询状态。', '若失败，查询状态。',
    '若trip_1锁价成功，则为trip_2付款。',
    '本步成功后给trip_1锁价。', '本步成功后，给trip_1锁价。',
    '失败后重试trip_1锁价。', '失败后，若trip_1报价有效则锁价。',
    '失败后给trip_1锁价后预计生成锁价快照。',
    '先给trip_1锁价，若失败再次为trip_1锁价。',
    '先给trip_1锁价，失败后请重试。', '先给trip_1锁价，失败就再试。',
    '再次给trip_1锁价。', '给trip_1重复锁价。',
    '先锁价。', '先给当前行程锁价。', '先给trip_3锁价。',
])
def test_same_kind_target_does_not_exempt_later_or_ambiguous_action(field, text):
    value = candidate(); value[field] = text
    with pytest.raises(ValueError, match='action_explanation_route'):
        validate(value)


def q22_fixture():
    return json.loads((Path(__file__).parent / 'fixtures/q22-current-action.json').read_text(encoding='utf-8'))


def test_complete_original_q22_candidate_formally_delivers_and_binds_confirmation(tmp_path, monkeypatch):
    async def run():
        saved = q22_fixture()
        raw = saved['candidate']['content']
        assert hashlib.sha256(raw.encode()).hexdigest() == saved['candidate_content_sha256']
        value = json.loads(raw)
        state = restore(saved['current_state'])
        assert display_state(state, 'assistant-9') == saved['current_state']
        assert validate_action_contract(raw, state, saved['allowed_pages'], []) == value
        h = harness(tmp_path, value, raw=raw)
        h.facts = state
        request = body().model_dump()
        request.update(page=saved['current_observation']['page'], observation={
            k: v for k, v in saved['current_observation'].items() if k != 'source_kind'})
        result = await post(app_for(monkeypatch, h), Chat(**request))
        assert result.status_code == 200
        sent_context = json.loads(h.calls[0]['messages'][-1]['content'])
        assert sent_context['current_state'] == saved['current_state']
        assert sent_context['current_observation'] == saved['current_observation']
        formal = result.json()
        recorded = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert recorded['candidate'] == saved['candidate']
        assert formal['reply'] == render_action_contract(value)
        assert formal['action_contract'] == value
        assert formal['action_sha256'] == action_digest(value)
        assert formal['action_contract_version'] == ACTION_CONTRACT_VERSION == 'single-action-8'
        for field in ('reason', 'conditions', 'check'):
            assert value[field] in formal['reply']
        confirmed = confirmation(formal)
        assert (confirmed['action'], confirmed['object']) == ('lock', 'trip_1')
        assert task.validate_confirmation(confirmed, formal, task_ref='p31-test', turn=1) == confirmed
        for field in ('reply', 'action_contract_version', 'action_sha256'):
            changed = deepcopy(formal); changed[field] += 'changed'
            with pytest.raises(RuntimeError):
                task.validate_confirmation(confirmed, changed, task_ref='p31-test', turn=1)
    asyncio.run(run())


@pytest.mark.parametrize('field', ['reason', 'conditions', 'check'])
@pytest.mark.parametrize('text', [
    '先给trip_1锁价；trip_1报价为999模拟元。',
    '先给trip_1锁价；两个行程已经完成。',
    '先给trip_1锁价；trip_1锁价当前有效。',
])
def test_accepted_current_action_does_not_bypass_fact_guards(tmp_path, monkeypatch, field, text):
    async def run():
        value = candidate(); value[field] = text
        h = harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503
        assert receipt(h)['reason'] in {'amount_unverified', 'shared_completion_unverified', 'price_validity_conflict'}
    asyncio.run(run())


def test_old_formal_version_cannot_confirm_under_repaired_contract():
    from test_round31_entry import delivered
    formal = delivered(); formal['action_contract_version'] = 'single-action-1'
    with pytest.raises(RuntimeError, match='delivery_binding_required'):
        task.structured_action(formal)


@pytest.mark.parametrize('kind,text', [
    ('pay', '先给trip_1付款。'), ('query', '请为trip_1查询原预订。'),
    ('requote', '本步为trip_1重新报价。'), ('cancel', '请为trip_1取消锁价。'),
])
def test_restatement_is_not_special_cased_to_q22_lock(kind, text):
    value = candidate(kind); value['reason'] = text
    assert validate(value)['reason'] == text
