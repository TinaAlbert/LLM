"""P38新生成版本的完整交付/原文拒绝与原守卫；零模型/PG/业务动作。"""
import asyncio
from copy import deepcopy
import hashlib
import json

import pytest

from scripts import round29_task as task
from scripts.check_round31 import restore
from services.guide.action_contract import (
    ACTION_CONTRACT_VERSION, _explanation, action_digest, render_action_contract,
    validate_action_contract,
)
from services.guide.assistant import Chat, SYSTEM_V11, display_state, validate_reply
from services.guide.fair_runtime import fair_input
from test_action_contract31 import candidate, tool, validate
from test_action_delivery31 import harness
from test_assistant import knowledge, observation
from test_current_action35 import saved
from test_reply_receipts30 import app_for, post, receipt
from test_round31_entry import confirmation
from test_shared_prompt21 import body


Q28_HASH = 'a98e6968f7396dd8a5059c9fe69a4c50500452434cdaff25fba2bb3c16d3e0ab'


def new_harness(tmp_path, value, *, raw=None, calls=None):
    h = harness(tmp_path, value, calls, raw=raw)
    release = h.releases.save_candidate(knowledge(),
        [observation('profile'), observation('trips')], prompt_version='assistant-11')
    h.releases.publish(release['version'])
    return h


def request_for(fixture):
    request = body().model_dump()
    request.update(page=fixture['current_observation']['page'], observation={
        k:v for k,v in fixture['current_observation'].items() if k != 'source_kind'})
    return Chat(**request)


def independent_candidate():
    # 独立中立对象合规提案，不是改写Q28或指定真实任务路径。
    value = candidate(target='trip_2')
    value.update(reason='需先为trip_2锁价。', conditions='trip_2报价当前有效。',
                 check='核对trip_2锁价返回的冻结金额、券归属及锁状态。')
    return value


def check_delivery(formal, value):
    rendered = render_action_contract(value)
    assert formal['action_contract'] == value and formal['reply'] == rendered
    assert formal['action_contract_version'] == ACTION_CONTRACT_VERSION == 'single-action-8'
    assert formal['action_sha256'] == action_digest(value)
    assert all(value[k] in rendered for k in ('reason', 'conditions', 'check'))
    confirmed = confirmation(formal)
    assert (confirmed['action'], confirmed['object'], confirmed['quote']) == (
        value['action']['kind'], value['action']['target'], rendered.splitlines()[0])
    assert task.validate_confirmation(confirmed, formal, task_ref='p31-test', turn=1) == confirmed
    for key in ('reason', 'conditions', 'check'):
        changed = deepcopy(formal); changed['action_contract'][key] += 'changed'
        with pytest.raises(RuntimeError):
            task.validate_confirmation(confirmed, changed, task_ref='p31-test', turn=1)
    for key in ('reply', 'action_contract_version', 'action_sha256'):
        changed = deepcopy(formal); changed[key] += 'changed'
        with pytest.raises(RuntimeError):
            task.validate_confirmation(confirmed, changed, task_ref='p31-test', turn=1)


def test_q28_complete_original_still_rejects_without_ref_or_pronoun_repair(tmp_path, monkeypatch):
    async def run():
        f = saved('q28-clause-following.json'); raw = f['candidate']['content']; value = json.loads(raw)
        assert hashlib.sha256(raw.encode()).hexdigest() == f['candidate_content_sha256'] == Q28_HASH
        state = restore(f['current_state'])
        assert display_state(state, 'assistant-11') == f['current_state']
        with pytest.raises(ValueError, match='action_explanation_route'):
            validate_action_contract(raw, state, f['allowed_pages'], [])
        for field in ('reason', 'check'):
            with pytest.raises(ValueError, match='action_explanation_route'):
                _explanation(value[field], value['action'], field)
        assert _explanation(value['conditions'], value['action'], 'conditions') is None
        rendered = render_action_contract(value)
        assert validate_reply(rendered, state, f['current_observation'], prompt_version='assistant-11') == rendered
        h = new_harness(tmp_path, value, raw=raw); h.facts = state
        response = await post(app_for(monkeypatch, h), request_for(f))
        assert response.status_code == 503 and response.json() == {'error':'assistant_result_unavailable'}
        assert receipt(h)['reason'] == 'action_explanation_route'
        report = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert report['candidate'] == f['candidate'] and report['prompt_version'] == 'assistant-11'
        assert h.calls[0]['messages'][0]['content'] == SYSTEM_V11
        assert all(s.pending is None and not s.history for s in h.sessions.values())
    asyncio.run(run())


def test_independent_neutral_complete_delivery_confirmation_and_same_fair_prompt(tmp_path, monkeypatch):
    async def run():
        value = independent_candidate(); assert validate(value) == value
        h = new_harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 200
        formal = response.json(); check_delivery(formal, value)
        assert validate_reply(formal['reply'], h.facts, prompt_version='assistant-11') == formal['reply']
        report = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert json.loads(report['candidate']['content']) == value and report['prompt_version'] == 'assistant-11'
        source = deepcopy(h.calls[0]); before = deepcopy(source)
        assert source['messages'][0]['content'] == SYSTEM_V11
        b1 = fair_input(source, 'B1', prompt_version='assistant-11')
        b0 = fair_input(source, 'B0', prompt_version='assistant-11')
        assert source == before and b1 == source
        assert b0['messages'][0] == b1['messages'][0]
        assert {k:v for k,v in b0.items() if k != 'messages'} == {k:v for k,v in b1.items() if k != 'messages'}
        full = json.loads(b1['messages'][1]['content']); reduced = json.loads(b0['messages'][1]['content'])
        assert set(full) - set(reduced) == {'current_state', 'current_observation'}
        assert reduced == {k:v for k,v in full.items() if k not in {'current_state', 'current_observation'}}
    asyncio.run(run())


def test_q27_complete_original_delivery_survives_new_generation_prompt(tmp_path, monkeypatch):
    async def run():
        f = saved('q27-return-relation.json'); raw = f['candidate']['content']; value = json.loads(raw)
        assert hashlib.sha256(raw.encode()).hexdigest() == f['candidate_content_sha256'] == '8abf6175f2903d83abd540d88de0eff6c0afd6cfd02a08fb52cccf8afe57be66'
        state = restore(f['current_state'])
        assert validate_action_contract(raw, state, f['allowed_pages'], []) == value
        assert validate_reply(render_action_contract(value), state, f['current_observation'], prompt_version='assistant-11') == render_action_contract(value)
        h = new_harness(tmp_path, value, raw=raw); h.facts = state
        response = await post(app_for(monkeypatch, h), request_for(f))
        assert response.status_code == 200; check_delivery(response.json(), value)
        report = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert report['candidate'] == f['candidate'] and report['prompt_version'] == 'assistant-11'
        context = json.loads(h.calls[0]['messages'][1]['content'])
        assert context['current_state'] == f['current_state'] and context['current_observation'] == f['current_observation']
        assert h.calls[0]['messages'][0]['content'] == SYSTEM_V11
    asyncio.run(run())


@pytest.mark.parametrize('field,text', [
    ('reason', '需先锁价。'),
    ('reason', 'trip_2报价当前有效，先锁价。'),
    ('check', '核对锁价返回的冻结金额。'),
    ('check', '核对trip_2锁价返回的其冻结金额。'),
    ('check', '核对trip_1锁价返回的冻结金额。'),
    ('check', '核对trip_2锁价返回的冻结金额，随后查询trip_2付款状态。'),
    ('reason', '成功后需先为trip_2锁价。'),
    ('reason', '需先为trip_2锁价，重复执行。'),
])
def test_clause_target_does_not_come_from_selection_other_fields_or_later_context(field, text):
    value = independent_candidate(); value[field] = text
    with pytest.raises(ValueError, match='action_explanation_route'):
        validate(value)


def test_generation_constraints_do_not_authorize_a_second_tool():
    with pytest.raises(ValueError, match='action_tool_mismatch'):
        validate(independent_candidate(), calls=[tool()])


@pytest.mark.parametrize('claim,error', [
    ('trip_1的报价为999模拟元。', 'amount_unverified'),
    ('trip_1锁价当前有效。', 'price_validity_conflict'),
    ('trip_2的报价为1000模拟元。', 'shared_amount_attribution_unverified'),
])
def test_new_generation_version_keeps_original_fact_guards(tmp_path, monkeypatch, claim, error):
    async def run():
        value = candidate(); value.update(reason='需先为trip_1锁价；' + claim,
            check='核对trip_1锁价返回的冻结金额、券归属及锁状态。')
        assert validate(value) == value
        h = new_harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503 and receipt(h)['reason'] == error
        assert all(s.pending is None and not s.history for s in h.sessions.values())
    asyncio.run(run())


def test_q25_original_later_action_still_has_no_formal_delivery(tmp_path, monkeypatch):
    async def run():
        f = saved('q25-single-step.json'); raw = f['candidate']['content']
        assert hashlib.sha256(raw.encode()).hexdigest() == f['candidate_content_sha256']
        h = new_harness(tmp_path, json.loads(raw), raw=raw); h.facts = restore(f['current_state'])
        response = await post(app_for(monkeypatch, h), request_for(f))
        assert response.status_code == 503 and receipt(h)['reason'] == 'action_explanation_route'
        report = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert report['candidate'] == f['candidate'] and report['prompt_version'] == 'assistant-11'
        assert all(s.pending is None and not s.history for s in h.sessions.values())
    asyncio.run(run())
