"""P34本步返回结果和追加操作的独立预期；原Q24完整回放零模型/PG。"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import round29_task as task
from scripts.check_round31 import restore
from services.guide.action_contract import ACTION_CONTRACT_VERSION, action_digest, render_action_contract, validate_action_contract
from services.guide.assistant import Chat, display_state
from test_action_contract31 import candidate, validate, tool
from test_action_delivery31 import harness
from test_reply_receipts30 import app_for, post, receipt
from test_shared_prompt21 import body
from test_round31_entry import confirmation, delivered


@pytest.mark.parametrize('kind,text', [
    ('lock', '确认trip_1锁价结果、券归trip_1、冻结金额及有效性。'),
    ('lock', '请核对trip_1的锁定报价结果与最终应付额。'),
    ('lock', 'trip_1的锁价快照需要确认。'),
    ('lock', '检查trip_1锁价是否生效，核对冻结金额和券归属。'),
    ('pay', '确认trip_1付款请求受理结果及付款编号。'),
    ('pay', '核对trip_1支付结果、支付金额。'),
    ('pay', 'trip_1付款尝试状态需确认。'),
    ('cancel', '核对trip_1取消锁价结果、券释放状态及库存占用状态。'),
    ('cancel', 'trip_1取消结果需要检查。'),
    ('requote', '核对trip_1重新报价结果与报价金额、有效期。'),
    ('requote', 'trip_1报价修订需确认。'),
])
def test_check_correlates_result_with_current_action_and_explicit_object(kind, text):
    value=candidate(kind); value['check']=text
    assert validate(value)==value
    assert text in render_action_contract(value)


@pytest.mark.parametrize('field', ['reason','conditions','check'])
@pytest.mark.parametrize('kind,noun', [('lock','锁价结果'),('pay','付款状态'),('cancel','取消锁价结果'),('requote','重新报价结果')])
@pytest.mark.parametrize('template', ['核对本步返回的trip_1{noun}。','trip_1本次回执中的{noun}需确认。','查看本次返回的trip_1{noun}。'])
def test_explicit_return_limits_check_in_all_three_fields(field,kind,noun,template):
    value=candidate(kind); value[field]=template.format(noun=noun)
    assert validate(value)==value


@pytest.mark.parametrize('field', ['reason','conditions'])
@pytest.mark.parametrize('kind,noun', [('lock','锁价结果'),('pay','付款状态'),('cancel','取消锁价结果'),('requote','重新报价结果')])
def test_reason_and_precondition_do_not_inherit_expected_result_meaning(field,kind,noun):
    value=candidate(kind);value[field]='需核对trip_1'+noun+'。'
    with pytest.raises(ValueError,match='action_explanation_route'):validate(value)


@pytest.mark.parametrize('field', ['reason','conditions','check'])
@pytest.mark.parametrize('text', [
    '锁价后再查询trip_1锁价状态。',
    '确认trip_1锁价结果后再付款。',
    '核对本步返回的trip_1锁价结果；随后为trip_1付款。',
    '确认trip_1锁价结果。付款。',
    '确认trip_1锁价结果、再为trip_1锁价。',
    '确认trip_1锁价结果，失败后，给trip_1锁价。',
    '确认trip_1锁价结果并付款状态。',
    '核对本步返回的trip_2锁价结果。',
    '确认trip_1锁价结果、券归trip_2。',
    '确认trip_1锁价结果及trip_2冻结金额。',
    '核对本步返回的锁价结果。',
    '确认当前行程锁价结果。',
    '确认trip_3锁价结果。',
    '确认trip_1锁价结果为由付款。',
    '核对本步返回的trip_1执行付款结果。',
    '确认trip_1锁价结果必须执行。',
    '确认trip_1付款结果。',
    '查询trip_1锁价结果。',
    '刷新trip_1锁价状态。',
    '查看trip_1锁价结果。',
    '成功后，核对本步返回的trip_1锁价结果。',
    '再次核对trip_1锁价结果。',
    '核对本步返回的trip_1锁价结果，再打开付款资料。',
])
def test_result_words_never_hide_later_queries_repeats_other_objects_or_commands(field,text):
    value=candidate();value[field]=text
    with pytest.raises(ValueError,match='action_explanation_route'):validate(value)


def test_result_target_not_inferred_from_selected_structure_other_field_or_previous_sentence():
    for value in [dict(candidate(),reason='trip_1报价有效。',check='确认锁价结果。'),
                  dict(candidate(),check='trip_1报价有效。确认锁价结果。'),
                  dict(candidate(),check='确认trip_1锁价结果。核对冻结金额。')]:
        with pytest.raises(ValueError,match='action_explanation_route'):validate(value)


def test_second_tool_is_rejected_even_with_legal_result_check():
    value=candidate();value['check']='确认trip_1锁价结果。'
    with pytest.raises(ValueError,match='action_tool_mismatch'):validate(value,calls=[tool()])


def test_original_q24_formal_replay_preserves_full_content_and_confirmation(tmp_path,monkeypatch):
    async def run():
        saved=json.loads((Path(__file__).parent/'fixtures/q24-step-result.json').read_text(encoding='utf-8'))
        raw=saved['candidate']['content'];value=json.loads(raw)
        assert hashlib.sha256(raw.encode()).hexdigest()==saved['candidate_content_sha256']
        state=restore(saved['current_state'])
        assert display_state(state,'assistant-9')==saved['current_state']
        assert validate_action_contract(raw,state,saved['allowed_pages'],[])==value
        h=harness(tmp_path,value,raw=raw);h.facts=state
        request=body().model_dump();request.update(page=saved['current_observation']['page'],observation={k:v for k,v in saved['current_observation'].items() if k!='source_kind'})
        response=await post(app_for(monkeypatch,h),Chat(**request))
        assert response.status_code==200
        context=json.loads(h.calls[0]['messages'][-1]['content'])
        assert context['current_state']==saved['current_state']
        assert context['current_observation']==saved['current_observation']
        formal=response.json();recorded=json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert recorded['candidate']==saved['candidate']
        assert formal['reply']==render_action_contract(value)
        assert formal['action_contract']==value and formal['action_sha256']==action_digest(value)
        assert formal['action_contract_version']==ACTION_CONTRACT_VERSION=='single-action-8'
        assert all(value[field] in formal['reply'] for field in ('reason','conditions','check'))
        confirmed=confirmation(formal)
        assert (confirmed['action'],confirmed['object'])==('lock','trip_1')
        assert task.validate_confirmation(confirmed,formal,task_ref='p31-test',turn=1)==confirmed
        for field in ('reply','action_contract_version','action_sha256'):
            changed=deepcopy(formal);changed[field]+='changed'
            with pytest.raises(RuntimeError):task.validate_confirmation(confirmed,changed,task_ref='p31-test',turn=1)
        for field in ('reason','conditions','check'):
            changed=deepcopy(formal);changed['action_contract'][field]+='changed'
            with pytest.raises(RuntimeError):task.validate_confirmation(confirmed,changed,task_ref='p31-test',turn=1)
    asyncio.run(run())


@pytest.mark.parametrize('field',['reason','conditions','check'])
@pytest.mark.parametrize('claim,expected', [('trip_1的报价为999模拟元。','amount_unverified'),('trip_1锁价当前有效。','price_validity_conflict'),('两个行程已经完成。','shared_completion_unverified')])
def test_result_check_does_not_assert_fact_truth(tmp_path,monkeypatch,field,claim,expected):
    async def run():
        value=candidate();value[field]='核对本步返回的trip_1锁价结果；'+claim
        assert validate(value)==value
        h=harness(tmp_path,value);response=await post(app_for(monkeypatch,h),body())
        assert response.status_code==503 and receipt(h)['reason']==expected
    asyncio.run(run())


@pytest.mark.parametrize('version',['single-action-1','single-action-2','single-action-3'])
def test_prior_contract_cannot_be_confirmed(version):
    formal=delivered();formal['action_contract_version']=version
    with pytest.raises(RuntimeError,match='delivery_binding_required'):task.structured_action(formal)
