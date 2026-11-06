"""P39锁定额仅作当前lock冻结结果；完整原文/交付/事实，零模型/PG/业务动作。"""
import asyncio
from copy import deepcopy
import hashlib
import json
import pytest
from scripts import round29_task as task
from scripts.check_round31 import restore
from services.guide.action_contract import (ACTION_CONTRACT_VERSION, _explanation,
    action_digest, render_action_contract, validate_action_contract)
from services.guide.assistant import SYSTEM_V11, validate_reply
from test_action_contract31 import candidate, tool, validate
from test_generation38 import new_harness, request_for
from test_current_action35 import saved
from test_reply_receipts30 import app_for, post, receipt
from test_round31_entry import confirmation
from test_shared_prompt21 import body


def delivery(formal, value):
    rendered = render_action_contract(value)
    assert formal['action_contract'] == value and formal['reply'] == rendered
    assert formal['action_contract_version'] == ACTION_CONTRACT_VERSION == 'single-action-8'
    assert formal['action_sha256'] == action_digest(value)
    assert all(value[k] in rendered for k in ('reason', 'conditions', 'check'))
    confirmed = confirmation(formal)
    assert (confirmed['action'], confirmed['object'], confirmed['quote']) == (
        value['action']['kind'], value['action']['target'], rendered.splitlines()[0])
    assert task.validate_confirmation(confirmed, formal, task_ref='p31-test', turn=1) == confirmed
    for key in ('reason', 'conditions', 'check', 'reply', 'action_contract_version', 'action_sha256'):
        changed = deepcopy(formal)
        if key in ('reason', 'conditions', 'check'):
            changed['action_contract'][key] += 'changed'
        else:
            changed[key] += 'changed'
        with pytest.raises(RuntimeError):
            task.validate_confirmation(confirmed, changed, task_ref='p31-test', turn=1)


@pytest.mark.parametrize('name,expected_hash', [
    ('q29-lock-amount.json', '9fe8aa0fb2f22f4a4be7e8422ab0cc58deca9ae68fcd5a36b9471cd4f284e732'),
    ('q27-return-relation.json', '8abf6175f2903d83abd540d88de0eff6c0afd6cfd02a08fb52cccf8afe57be66'),
])
def test_complete_original_host_contract_fact_delivery_and_confirmation(tmp_path, monkeypatch, name, expected_hash):
    async def run():
        f = saved(name); raw = f['candidate']['content']; value = json.loads(raw)
        assert hashlib.sha256(raw.encode()).hexdigest() == f['candidate_content_sha256'] == expected_hash
        state = restore(f['current_state'])
        assert validate_action_contract(raw, state, f['allowed_pages'], []) == value
        assert all(_explanation(value[k], value['action'], k) is None for k in ('reason', 'conditions', 'check'))
        rendered = render_action_contract(value)
        assert validate_reply(rendered, state, f['current_observation'], prompt_version='assistant-11') == rendered
        h = new_harness(tmp_path, value, raw=raw); h.facts = state
        response = await post(app_for(monkeypatch, h), request_for(f))
        assert response.status_code == 200; delivery(response.json(), value)
        report = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert report['candidate'] == f['candidate'] and report['prompt_version'] == 'assistant-11'
        assert h.calls[0]['messages'][0]['content'] == SYSTEM_V11
        context = json.loads(h.calls[0]['messages'][1]['content'])
        assert context['current_state'] == f['current_state'] and context['current_observation'] == f['current_observation']
        if name.startswith('q29'):
            # 宿主有限链通过，并未证明逐ref生成遵循；不改写原文或追认历史503。
            assert value['reason'] == '两行程均仅有估算报价、无锁价；共享券可用，首个合格锁价将确定整券归属与最终应付额。'
            assert 'trip_1' not in value['reason'] and 'trip_2' not in value['reason']
    asyncio.run(run())


def neutral():
    v = candidate(target='trip_2')
    v.update(reason='需先为trip_2锁价。', conditions='trip_2报价当前有效。',
        check='核对trip_2锁价结果、锁定额与最终应付额。')
    return v


def test_neutral_trip2_full_delivery_and_bounded_list_and_fields(tmp_path, monkeypatch):
    async def run():
        value = neutral(); assert validate(value) == value
        h = new_harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 200; delivery(response.json(), value)
        for field in ('reason', 'conditions', 'check'):
            v = neutral(); v[field] = '核对trip_2本步返回的锁定额、券归属与锁状态。'
            assert validate(v) == v
        for field in ('reason', 'conditions'):
            v = neutral(); v[field] = '核对trip_2锁定额。'
            with pytest.raises(ValueError, match='action_explanation_route'): validate(v)
    asyncio.run(run())


@pytest.mark.parametrize('kind,field,text', [
    ('lock', 'check', '核对锁定额。'),
    ('lock', 'check', 'trip_2报价有效。核对锁定额。'),
    ('lock', 'check', '核对trip_1锁定额。'),
    ('pay', 'check', '核对trip_2锁定额。'),
    ('query', 'check', '核对trip_2锁定额。'),
    ('lock', 'check', '核对trip_2锁定额，随后查询trip_2付款状态。'),
    ('lock', 'check', '成功后核对trip_2锁定额。'),
    ('lock', 'check', '再次核对trip_2锁定额。'),
    ('lock', 'check', '核对trip_2锁定额与余额。'),
    ('lock', 'check', '核对trip_2预估锁定额。'),
    ('lock', 'check', '核对trip_2未来锁定额。'),
    pytest.param('lock', 'check', '核对trip_2锁定额仍待', id='wait_suffix'),
    ('lock', 'check', '核对trip_2锁定额及估价。'),
])
def test_result_word_never_repairs_object_field_or_authorizes_other_amounts_or_steps(kind, field, text):
    value = candidate(kind, target='trip_2'); value[field] = text
    with pytest.raises(ValueError, match='action_explanation_route'): validate(value)


def test_second_tool_and_cross_field_object_cannot_be_added():
    with pytest.raises(ValueError, match='action_tool_mismatch'): validate(neutral(), calls=[tool()])
    v = neutral(); v.update(reason='trip_2报价当前有效。', check='核对锁定额。')
    with pytest.raises(ValueError, match='action_explanation_route'): validate(v)


@pytest.mark.parametrize('claim,error', [
    ('trip_1的报价为999模拟元。', 'amount_unverified'),
    ('trip_1锁价当前有效。', 'price_validity_conflict'),
    ('trip_2的报价为1000模拟元。', 'shared_amount_attribution_unverified'),
])
def test_original_fact_guards_still_check_complete_presentation(tmp_path, monkeypatch, claim, error):
    async def run():
        value = candidate(); value.update(reason='需先为trip_1锁价；' + claim, check='核对trip_1锁定额。')
        assert validate(value) == value
        h = new_harness(tmp_path, value)
        response = await post(app_for(monkeypatch, h), body())
        assert response.status_code == 503 and receipt(h)['reason'] == error
        assert all(s.pending is None and not s.history for s in h.sessions.values())
    asyncio.run(run())


def test_q28_complete_missing_ref_and_pronoun_still_rejected(tmp_path, monkeypatch):
    async def run():
        f = saved('q28-clause-following.json'); raw = f['candidate']['content']; value = json.loads(raw)
        assert hashlib.sha256(raw.encode()).hexdigest() == f['candidate_content_sha256']
        state = restore(f['current_state'])
        with pytest.raises(ValueError, match='action_explanation_route'):
            validate_action_contract(raw, state, f['allowed_pages'], [])
        for field in ('reason', 'check'):
            with pytest.raises(ValueError, match='action_explanation_route'):
                _explanation(value[field], value['action'], field)
        h = new_harness(tmp_path, value, raw=raw); h.facts = state
        response = await post(app_for(monkeypatch, h), request_for(f))
        assert response.status_code == 503 and receipt(h)['reason'] == 'action_explanation_route'
    asyncio.run(run())
