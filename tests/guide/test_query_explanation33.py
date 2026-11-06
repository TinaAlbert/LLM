"""查询对象描述与追加操作的独立业务预期；原候选完整正式回放，零模型/PG。"""
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
from test_round31_entry import confirmation, delivered


@pytest.mark.parametrize('field', ['reason', 'conditions', 'check'])
@pytest.mark.parametrize('text', [
    '需核对trip_1的锁价与券归属的实际状态。',
    '需要确认服务端trip_1付款与票据状态。',
    '请查看trip_1支付结果。',
    '先为trip_1查询付款状态。',
    'trip_1的锁价与券归属状态需要核对。',
    '当前trip_1的票据状态需确认。',
    'trip_1锁价请求已受理，需核对服务端锁价与券归属的实际状态。',
    '请刷新trip_1的付款状态。',
    '需确认trip_1锁定报价是否生效。',
    'trip_1付款状态仍待确认。',
    '本步前提为trip_1锁价仍有效。',
    '本步成功后预期返回付款状态。',
])
def test_current_query_preserves_object_description_and_conditions(field, text):
    value = candidate('query'); value[field] = text
    assert validate(value) == value
    assert text in render_action_contract(value)


@pytest.mark.parametrize('field', ['reason', 'conditions', 'check'])
@pytest.mark.parametrize('text', [
    '核对trip_1锁价状态后再付款。',
    '需核对trip_1的付款状态；随后为trip_1锁价。',
    '需核对trip_1的锁价状态。付款。',
    '查询并为trip_1锁价。',
    '请查询trip_1并锁价。',
    '需核对trip_1的锁价状态并付款。',
    '需核对trip_1的锁价状态并付款状态。',
    '需核对trip_1的锁价状态并执行付款。',
    '确认trip_1锁价状态后，为trip_2付款。',
    '需核对trip_2的付款状态。',
    'trip_2的付款状态需要核对。',
    '成功后，需核对trip_1的付款状态。',
    '若本步失败；请查询trip_1的付款状态。',
    '核对trip_1的锁价状态，失败就再试。',
    '再次查询trip_1付款状态。',
    '需核对锁价与券归属的实际状态。',
    '需核对当前行程的付款状态。',
    '需核对trip_3的付款状态。',
    '请为trip_1付款状态。',
    '为trip_1付款状态。',
    '请先锁价状态，再查询trip_1。',
    '需核对trip_1付款状态为由锁价。',
    '请以查询trip_1锁价状态为名付款。',
    '需核对trip_1的执行付款状态。',
    '需核对trip_1的锁价状态并为trip_2查询付款状态。',
    'trip_1付款状态需要执行。',
    'trip_1锁价结果必须立即执行。',
    'trip_1付款需办理。',
    '需核对trip_1的锁价状态；trip_1付款需执行。',
])
def test_query_words_and_noun_suffixes_do_not_hide_another_action(field, text):
    value = candidate('query'); value[field] = text
    with pytest.raises(ValueError, match='action_explanation_route'):
        validate(value)


@pytest.mark.parametrize('kind', ['lock', 'pay', 'cancel', 'requote'])
def test_query_description_is_another_operation_when_structure_is_not_query(kind):
    value = candidate(kind); value['reason'] = '需核对trip_1的付款状态。'
    with pytest.raises(ValueError, match='action_explanation_route'):
        validate(value)


def test_query_object_is_not_inferred_from_another_explanation_field():
    value = candidate('query')
    value.update(reason='trip_1付款状态未知。', check='需核对付款状态。')
    with pytest.raises(ValueError, match='action_explanation_route'):
        validate(value)


@pytest.mark.parametrize('label,kind', [('lock', 'lock'), ('query', 'query')])
def test_original_q23_candidate_formally_delivers_without_rewriting(tmp_path, monkeypatch, label, kind):
    async def run():
        saved = json.loads((Path(__file__).parent / f'fixtures/q23-current-{label}.json').read_text(encoding='utf-8'))
        raw = saved['candidate']['content']; value = json.loads(raw)
        assert hashlib.sha256(raw.encode()).hexdigest() == saved['candidate_content_sha256']
        state = restore(saved['current_state'])
        assert display_state(state, 'assistant-9') == saved['current_state']
        assert validate_action_contract(raw, state, saved['allowed_pages'], []) == value
        h = harness(tmp_path, value, raw=raw); h.facts = state
        request = body().model_dump()
        request.update(page=saved['current_observation']['page'], observation={
            k: v for k, v in saved['current_observation'].items() if k != 'source_kind'})
        response = await post(app_for(monkeypatch, h), Chat(**request))
        assert response.status_code == 200
        context = json.loads(h.calls[0]['messages'][-1]['content'])
        assert context['current_state'] == saved['current_state']
        assert context['current_observation'] == saved['current_observation']
        formal = response.json()
        report = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert report['candidate'] == saved['candidate']
        assert formal['reply'] == render_action_contract(value)
        assert formal['action_contract'] == value
        assert formal['action_sha256'] == action_digest(value)
        assert formal['action_contract_version'] == ACTION_CONTRACT_VERSION == 'single-action-8'
        for field in ('reason', 'conditions', 'check'):
            assert value[field] in formal['reply']
        confirmed = confirmation(formal)
        assert (confirmed['action'], confirmed['object']) == (kind, 'trip_1')
        assert task.validate_confirmation(confirmed, formal, task_ref='p31-test', turn=1) == confirmed
        for field in ('reply', 'action_contract_version', 'action_sha256'):
            changed = deepcopy(formal); changed[field] += 'changed'
            with pytest.raises(RuntimeError):
                task.validate_confirmation(confirmed, changed, task_ref='p31-test', turn=1)
        for field in ('reason', 'conditions', 'check'):
            changed = deepcopy(formal); changed['action_contract'][field] += 'changed'
            with pytest.raises(RuntimeError):
                task.validate_confirmation(confirmed, changed, task_ref='p31-test', turn=1)
    asyncio.run(run())


@pytest.mark.parametrize('field', ['reason', 'conditions', 'check'])
@pytest.mark.parametrize('claim,expected', [
    ('trip_1的报价为999模拟元。', 'amount_unverified'),
    ('trip_1锁价当前有效。', 'price_validity_conflict'),
    ('两个行程已经完成。', 'shared_completion_unverified'),
])
def test_read_description_does_not_validate_unverified_facts(tmp_path, monkeypatch, field, claim, expected):
    async def run():
        value = candidate('query'); value[field] = '需核对trip_1付款状态；' + claim
        assert validate(value) == value
        h = harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503 and receipt(h)['reason'] == expected
    asyncio.run(run())


@pytest.mark.parametrize('version', ['single-action-1', 'single-action-2'])
def test_old_contract_cannot_be_confirmed_after_query_repair(version):
    formal = delivered(); formal['action_contract_version'] = version
    with pytest.raises(RuntimeError, match='delivery_binding_required'):
        task.structured_action(formal)
