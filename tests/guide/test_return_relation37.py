"""P37当前锁价返回来源与有限结果组合；完整原文回放，零模型/PG。"""
import asyncio
from copy import deepcopy
import hashlib
import json
import pytest
from scripts import round29_task as task
from scripts.check_round31 import restore
from services.guide.action_contract import ACTION_CONTRACT_VERSION, _explanation, action_digest, render_action_contract, validate_action_contract
from services.guide.assistant import Chat, display_state, validate_reply
from test_action_contract31 import candidate, validate, tool
from test_current_action35 import current_harness, saved
from test_reply_receipts30 import app_for, post, receipt
from test_round31_entry import confirmation
from test_shared_prompt21 import body


def test_q27_complete_original_formal_replay_keeps_full_list_facts_and_confirmation(tmp_path, monkeypatch):
    async def run():
        f = saved('q27-return-relation.json'); raw = f['candidate']['content']; value = json.loads(raw)
        assert hashlib.sha256(raw.encode()).hexdigest() == f['candidate_content_sha256'] == '8abf6175f2903d83abd540d88de0eff6c0afd6cfd02a08fb52cccf8afe57be66'
        assert value['check'] == '核对trip_1锁价返回的冻结金额、券归属及锁状态。'
        state = restore(f['current_state'])
        assert display_state(state, 'assistant-10') == f['current_state']
        assert validate_action_contract(raw, state, f['allowed_pages'], []) == value
        assert _explanation(value['check'], value['action'], 'check') is None
        rendered = render_action_contract(value)
        assert validate_reply(rendered, state, f['current_observation'], prompt_version='assistant-10') == rendered
        h = current_harness(tmp_path, value, raw=raw); h.facts = state
        request = body().model_dump()
        request.update(page=f['current_observation']['page'], observation={k:v for k,v in f['current_observation'].items() if k != 'source_kind'})
        response = await post(app_for(monkeypatch, h), Chat(**request))
        assert response.status_code == 200
        formal = response.json()
        assert formal['action_contract'] == value and formal['reply'] == rendered
        assert formal['action_contract_version'] == ACTION_CONTRACT_VERSION == 'single-action-8'
        assert formal['action_sha256'] == action_digest(value)
        assert all(value[k] in rendered for k in ('reason','conditions','check'))
        report = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert report['candidate'] == f['candidate'] and report['prompt_version'] == 'assistant-10'
        context = json.loads(h.calls[0]['messages'][-1]['content'])
        assert context['current_state'] == f['current_state'] and context['current_observation'] == f['current_observation']
        confirmed = confirmation(formal)
        assert (confirmed['action'],confirmed['object'],confirmed['quote']) == ('lock','trip_1',rendered.splitlines()[0])
        assert task.validate_confirmation(confirmed,formal,task_ref='p31-test',turn=1) == confirmed
        for k in ('reason','conditions','check'):
            changed=deepcopy(formal); changed['action_contract'][k]+='changed'
            with pytest.raises(RuntimeError):task.validate_confirmation(confirmed,changed,task_ref='p31-test',turn=1)
        for k in ('reply','action_contract_version','action_sha256'):
            changed=deepcopy(formal); changed[k]+='changed'
            with pytest.raises(RuntimeError):task.validate_confirmation(confirmed,changed,task_ref='p31-test',turn=1)
    asyncio.run(run())


@pytest.mark.parametrize('field',['reason','conditions','check'])
def test_return_relation_keeps_neutral_explicit_target_and_field_semantics(field):
    value=candidate(target='trip_2')
    value[field]='核对trip_2锁价返回的冻结金额、券归属及锁状态。'
    assert validate(value)==value and value[field] in render_action_contract(value)


@pytest.mark.parametrize('kind,field,text',[
    ('lock','check','核对锁价返回的冻结金额、券归属及锁状态。'),
    ('lock','check','核对trip_2锁价返回的冻结金额、券归属及锁状态。'),
    ('lock','check','核对trip_1锁价返回的冻结金额、trip_2券归属及锁状态。'),
    ('pay','check','核对trip_1锁价返回的冻结金额。'),
    ('query','check','核对trip_1锁价返回的冻结金额。'),
    ('lock','check','核对trip_1锁价返回的冻结金额，随后查询trip_1付款状态。'),
    ('lock','check','核对trip_1锁价返回的冻结金额；若成功，再为trip_1付款。'),
    ('lock','check','核对trip_1锁价返回的冻结金额，重复执行锁价。'),
    ('lock','check','核对trip_1锁价返回的冻结金额、再次为trip_1锁价。'),
    ('lock','reason','核对trip_1冻结金额、券归属及锁状态。'),
    ('lock','conditions','核对trip_1冻结金额、券归属及锁状态。'),
    ('lock','check','核对trip_1锁价返回的冻结金额、锁价返回的付款状态。'),
])
def test_return_relation_does_not_authorize_missing_other_later_repeat_or_wrong_action(kind,field,text):
    value=candidate(kind); value['reason']='需先为trip_1锁价。' if kind=='lock' else ''
    value[field]=text
    with pytest.raises(ValueError,match='action_explanation_route'):validate(value)


def test_return_relation_does_not_authorize_second_tool():
    value=candidate();value['check']='核对trip_1锁价返回的冻结金额、券归属及锁状态。'
    with pytest.raises(ValueError,match='action_tool_mismatch'):validate(value,calls=[tool()])


@pytest.mark.parametrize('claim,error',[
    ('trip_1的报价为999模拟元。','amount_unverified'),
    ('trip_1锁价当前有效。','price_validity_conflict'),
    ('trip_2的报价为1000模拟元。','shared_amount_attribution_unverified'),
])
def test_return_relation_never_proves_amount_validity_or_attribution(tmp_path,monkeypatch,claim,error):
    async def run():
        value=candidate();value['reason']='需先为trip_1锁价；'+claim
        value['check']='核对trip_1锁价返回的冻结金额、券归属及锁状态。'
        assert validate(value)==value
        h=current_harness(tmp_path,value)
        response=await post(app_for(monkeypatch,h),body())
        assert response.status_code==503 and receipt(h)['reason']==error
        assert all(s.pending is None and not s.history for s in h.sessions.values())
    asyncio.run(run())
