"""Q16两精确样本的无费用反例；只使用临时合成账本及MockTransport。"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
import multiprocessing
import time

import httpx
import pytest

from services.guide.assistant import corporate_export_scope
from services.guide.budget import (BudgetLedger, BudgetUnavailable, EXPERIMENT_REQUEST_CEILING,
                                  _Q16Permit, _q16_permit_for_validated_input)
from services.guide.experiment import approval_values
from services.guide.model import DeepSeekModelAdapter, ModelConfig, ModelUnavailable, MODEL, _today


def case(question):
    context={'question':question,'current_state':{'contract_version':'assistant-shared-1',
        'delegated_refs':['trip_1','trip_2'],'observed_at':'2026-10-17T01:00:00Z'},'history':[]}
    return {'messages':[{'role':'system','content':'只使用允许的合成观察，不导航。'},
                        {'role':'user','content':json.dumps(context,ensure_ascii=False)}],
            'tools':[],'max_tokens':512,'json_output':False}


CASES=(case('初态双目标合成咨询'),case('状态变化后双目标合成咨询'))
SCOPES=tuple(corporate_export_scope(value) for value in CASES)


@pytest.fixture
def ledger(tmp_path):
    now=[time.time()-1800]
    result=BudgetLedger(tmp_path/'usage.json',clock=lambda:now[0]);result.initialize()
    for index in range(22):
        reservation=result.reserve(experiment=True)
        result.settle(reservation,prompt_tokens=100,completion_tokens=20,
                      provider_request_id=f'synthetic-old-{index}')
        now[0]+=61
    return result


def permit(ledger,index):
    return _q16_permit_for_validated_input(SCOPES[index],ledger.q16_path)


def settle(ledger,reservation):
    ledger.settle(reservation,prompt_tokens=100,completion_tokens=20,
                  provider_request_id='synthetic-q16-'+reservation.request_id)


def response(identifier='synthetic-q16-receipt'):
    return {'id':identifier,'model':MODEL,'usage':{'prompt_tokens':100,'completion_tokens':20,
        'total_tokens':120,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100},
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'合成回答。'}}]}


def invoke(ledger,tmp_path,value,handler,*,enabled=True,validator=None):
    approval=tmp_path/'experiment.json';approval.write_text(json.dumps(approval_values(_today())),encoding='utf8')
    adapter=DeepSeekModelAdapter(ledger,config=ModelConfig('synthetic-test-key-not-an-account','synthetic',
        _today(),'user_reported','test',Decimal(0)),input_validator=validator or (lambda value:value),
        transport=httpx.MockTransport(handler),q16_grant_path=ledger.q16_path if enabled else None)
    return asyncio.run(adapter.generate_experiment(value,time.monotonic()+5,approval_path=approval))


def test_q16_same_ledger_two_ordered_scopes_and_old_prefix_unchanged(ledger):
    before=json.loads(ledger.path.read_text(encoding='utf8'))
    anchors=(ledger.marker.read_bytes(),ledger.lock_path.read_bytes())
    assert EXPERIMENT_REQUEST_CEILING==22
    ledger.create_q16_grant(SCOPES)
    assert ledger.check_q16_scope(SCOPES[0])
    for index in range(2):
        reservation=ledger.reserve(experiment=True,q16_permit=permit(ledger,index));settle(ledger,reservation)
    after=json.loads(ledger.path.read_text(encoding='utf8'))
    assert len(after['requests'])==24 and after['requests'][:22]==before['requests']
    assert {k:v for k,v in before.items() if k!='requests'}=={k:v for k,v in after.items() if k!='requests'}
    assert anchors==(ledger.marker.read_bytes(),ledger.lock_path.read_bytes())
    with pytest.raises(BudgetUnavailable,match='q16_budget_stop'):
        ledger.reserve(experiment=True,q16_permit=permit(ledger,0))
    with pytest.raises(BudgetUnavailable,match='q16_scope_required'): ledger.reserve(experiment=True)
    assert ledger.summary()['physical_requests']==24


@pytest.mark.parametrize('scope', [(),('a'*64,),('a'*64,'a'*64),('Z'*64,'b'*64),([],{}),None])
def test_bad_grant_scope_refused(ledger,scope):
    with pytest.raises(BudgetUnavailable): ledger.create_q16_grant(scope)
    assert not ledger.q16_path.exists()


def test_grant_never_reinitializes_or_changes_old_approval(ledger):
    ledger.create_q16_grant(SCOPES);before=ledger.q16_path.read_bytes()
    with pytest.raises(BudgetUnavailable,match='q16_already_recorded'): ledger.create_q16_grant(SCOPES)
    assert ledger.q16_path.read_bytes()==before
    assert approval_values(_today())['request_ceiling']==22


@pytest.mark.parametrize('experiment', [True,False])
def test_other_context_cannot_take_new_requests(ledger,experiment):
    ledger.create_q16_grant(SCOPES);before=ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable,match='q16_scope_required'): ledger.reserve(experiment=experiment)
    assert ledger.path.read_bytes()==before


def test_no_sidecar_no_new_request(ledger):
    with pytest.raises(BudgetUnavailable,match='experiment_budget_stop'): ledger.reserve(experiment=True)
    with pytest.raises(BudgetUnavailable): ledger.reserve(experiment=True,q16_permit=permit(ledger,0))


@pytest.mark.parametrize('raw', ['Q16',{'scope_hash':SCOPES[0]},True])
def test_self_claimed_id_is_not_a_valid_internal_permit(ledger,raw):
    ledger.create_q16_grant(SCOPES)
    with pytest.raises(BudgetUnavailable,match='q16_scope_invalid'):
        ledger.reserve(experiment=True,q16_permit=raw)


def test_forged_permit_and_different_grant_path_refused(ledger,tmp_path):
    ledger.create_q16_grant(SCOPES)
    invalid=[_Q16Permit(SCOPES[0],ledger.q16_path,object()),
             _q16_permit_for_validated_input(SCOPES[0],tmp_path/'other-grant.json')]
    for value in invalid:
        with pytest.raises(BudgetUnavailable,match='q16_scope_invalid'):
            ledger.reserve(experiment=True,q16_permit=value)


def test_wrong_order_duplicate_and_unknown_scope_refused(ledger):
    ledger.create_q16_grant(SCOPES)
    for value in [permit(ledger,1),_q16_permit_for_validated_input('f'*64,ledger.q16_path)]:
        with pytest.raises(BudgetUnavailable,match='q16_scope_or_order_mismatch'):
            ledger.reserve(experiment=True,q16_permit=value)
    first=ledger.reserve(experiment=True,q16_permit=permit(ledger,0));settle(ledger,first)
    with pytest.raises(BudgetUnavailable,match='q16_scope_or_order_mismatch'):
        ledger.reserve(experiment=True,q16_permit=permit(ledger,0))


@pytest.mark.parametrize('change',['ledger_id','prefix_sha256','scope_hashes','extra','bool_schema','intent'])
def test_tampered_sidecar_refused(ledger,change):
    ledger.create_q16_grant(SCOPES);value=json.loads(ledger.q16_path.read_text(encoding='utf8'))
    if change=='ledger_id': value['ledger_id']='f'*32
    if change=='prefix_sha256': value['prefix_sha256']='f'*64
    if change=='scope_hashes': value['scope_hashes']=[SCOPES[0],SCOPES[0]]
    if change=='extra': value['extra']=True
    if change=='bool_schema': value['schema']=True
    if change=='intent': value['intents']=[{'sequence':1,'scope_hash':SCOPES[0],
        'request_id':'e'*32,'state':'intent'}]
    ledger.q16_path.write_text(json.dumps(value),encoding='utf8')
    with pytest.raises(BudgetUnavailable): ledger.reserve(experiment=True,q16_permit=permit(ledger,0))
    assert ledger.summary()['physical_requests']==22


def test_write_ahead_intent_blocks_restart_before_ledger_append(ledger,monkeypatch):
    ledger.create_q16_grant(SCOPES);original=ledger._write
    def crashing(path,value):
        if path==ledger.path: raise RuntimeError('synthetic-crash-before-ledger')
        original(path,value)
    monkeypatch.setattr(ledger,'_write',crashing)
    with pytest.raises(RuntimeError): ledger.reserve(experiment=True,q16_permit=permit(ledger,0))
    recovered=BudgetLedger(ledger.path)
    with pytest.raises(BudgetUnavailable,match='q16_intent_unresolved'):
        recovered.reserve(experiment=True,q16_permit=permit(recovered,0))
    assert recovered.summary()['physical_requests']==22


def test_crash_after_ledger_append_does_not_dispatch_or_free_reserved_slot(ledger,monkeypatch):
    ledger.create_q16_grant(SCOPES);original=ledger._write
    def crashing(path,value):
        if path==ledger.q16_path and value['intents'][-1]['state']=='reserved':
            raise RuntimeError('synthetic-crash-after-ledger')
        original(path,value)
    monkeypatch.setattr(ledger,'_write',crashing)
    with pytest.raises(RuntimeError): ledger.reserve(experiment=True,q16_permit=permit(ledger,0))
    recovered=BudgetLedger(ledger.path)
    with pytest.raises(BudgetUnavailable,match='usage_unresolved'):
        recovered.reserve(experiment=True,q16_permit=permit(recovered,0))
    assert recovered.summary()['physical_requests']==23 and recovered.summary()['unresolved_requests']==1


def test_actual_validated_payload_scope_reaches_fixed_transport_and_same_ledger(ledger,tmp_path):
    ledger.create_q16_grant(SCOPES);calls=[]
    def handler(request):
        calls.append(request)
        assert str(request.url)=='https://api.deepseek.com/chat/completions'
        assert json.loads(request.content)['max_tokens']==512
        return httpx.Response(200,json=response(f'synthetic-q16-{len(calls)}'))
    for value in CASES: invoke(ledger,tmp_path,deepcopy(value),handler)
    assert len(calls)==2 and ledger.summary()['physical_requests']==24
    with pytest.raises(ModelUnavailable,match='q16_budget_stop'):
        invoke(ledger,tmp_path,CASES[0],handler)
    assert len(calls)==2


@pytest.mark.parametrize('change',['question','target','tools','self_id','ordinary_adapter','validator_reject'])
def test_payload_or_context_change_refused_before_dispatch(ledger,tmp_path,change):
    ledger.create_q16_grant(SCOPES);value=deepcopy(CASES[0]);validator=None
    if change in ('question','target'):
        context=json.loads(value['messages'][1]['content']);context[change]='not-approved'
        value['messages'][1]['content']=json.dumps(context)
    if change=='tools': value['tools']=[{'name':'shell'}]
    if change=='self_id': value['q16']='approved'
    if change=='validator_reject':
        def validator(value): raise ValueError('synthetic-unapproved-input')
    with pytest.raises(ModelUnavailable): invoke(ledger,tmp_path,value,lambda _:pytest.fail('must not dispatch'),
        enabled=change!='ordinary_adapter',validator=validator)
    assert ledger.summary()['physical_requests']==22


def test_time_only_change_matches_existing_semantic_approval(ledger,tmp_path):
    ledger.create_q16_grant(SCOPES);value=deepcopy(CASES[0])
    context=json.loads(value['messages'][1]['content']);context['current_state']['observed_at']='2026-10-17T02:00:00Z'
    value['messages'][1]['content']=json.dumps(context)
    invoke(ledger,tmp_path,value,lambda _:httpx.Response(200,json=response()))
    assert ledger.summary()['physical_requests']==23


def test_unknown_usage_stops_all_followups(ledger,tmp_path):
    ledger.create_q16_grant(SCOPES);calls=[]
    def handler(_):
        calls.append(1);body=response();del body['usage'];return httpx.Response(200,json=body)
    with pytest.raises(ModelUnavailable,match='usage_missing'): invoke(ledger,tmp_path,CASES[0],handler)
    for value in CASES:
        with pytest.raises(ModelUnavailable,match='usage_unresolved'): invoke(ledger,tmp_path,value,handler)
    assert len(calls)==1 and ledger.summary()['unresolved_requests']==1


def _other_process(path,index,output):
    current=BudgetLedger(path)
    try:
        current.reserve(experiment=True,q16_permit=permit(current,index));output.put('reserved')
    except BudgetUnavailable as error: output.put(error.reason)


def test_separate_process_sees_consumed_scope_and_pending_hold(ledger):
    ledger.create_q16_grant(SCOPES)
    first=ledger.reserve(experiment=True,q16_permit=permit(ledger,0))
    context=multiprocessing.get_context('spawn');output=context.Queue()
    child=context.Process(target=_other_process,args=(str(ledger.path),1,output))
    try:
        child.start();child.join(15)
        assert child.exitcode==0 and output.get(timeout=2)=='usage_unresolved'
    finally:
        if child.is_alive(): child.terminate();child.join(5)
        output.close()
    settle(ledger,first)
    assert ledger.check_q16_scope(SCOPES[1])


def test_known_delivery_failure_closes_remaining_scope_without_unknown_usage(ledger):
    ledger.create_q16_grant(SCOPES)
    reservation=ledger.reserve(experiment=True,q16_permit=permit(ledger,0));settle(ledger,reservation)
    before=ledger.path.read_bytes();ledger.stop_q16_batch('chat_http_503')
    assert ledger.path.read_bytes()==before
    with pytest.raises(BudgetUnavailable,match='q16_batch_stopped'):ledger.check_q16_scope(SCOPES[1])
    with pytest.raises(BudgetUnavailable,match='q16_batch_stopped'):
        ledger.reserve(experiment=True,q16_permit=permit(ledger,1))
    assert ledger.summary()['physical_requests']==23 and ledger.summary()['unresolved_requests']==0
