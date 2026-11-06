"""Q26使用临时34笔账本与MockTransport，真实许可和费用均0。"""
import asyncio
from decimal import Decimal
import json
import time
import httpx
import pytest
from scripts import round29_task as task
from services.guide.budget import BudgetUnavailable
from services.guide.experiment import approval_values
from services.guide.knowledge import fingerprint
from services.guide.model import DeepSeekModelAdapter,ModelConfig,ModelUnavailable,_today
from services.guide.task_budget import TaskPermitBinding,validated_q19_permit,create_q19_grant,stop_q19_batch,confirm_q19_round,finish_q19_task
from test_round34_entry import ready as q25_ready,permit as q25_permit,q24_ready,q23_ready,q22_ready,q21_ready,q20_ready,q19_ready
from test_q18_budget import settled
from test_q16_budget import case,response

@pytest.fixture
def ready(q25_ready):
    ledger,now,specs,old,*_=q25_ready
    settled(ledger,ledger.reserve(experiment=True,q25_permit=q25_permit(q25_ready)))
    stop_q19_batch(ledger,'formal_delivery_failed',batch='Q25');now[0]+=61
    old={p:p.read_bytes() for p in (*old,ledger.q25_path,ledger.q25_stop_path)}
    specs=[dict(s,task_ref='q26-'+s['task_ref']) for s in specs]
    prefix=ledger.receipt_snapshot();grant=create_q19_grant(ledger,specs,'f'*64,batch='Q26')
    return ledger,now,specs,old,prefix,grant

def permit(ready,actual_hash='a'*64,task_index=1,round_index=1):
    ledger,_,specs,*_=ready;s=specs[task_index-1]
    binding=TaskPermitBinding(s['task_ref'],task_index,round_index,1,'b'*64,actual_hash,s['scope_hash'])
    return validated_q19_permit(binding,ledger.q26_path,batch='Q26')

@pytest.mark.parametrize('outcome',['success','unknown_usage','actual_changed'])
def test_adapter_binds_34_prefix_20_sidecars_and_freezes_failed_batch(ready,tmp_path,outcome):
    ledger,_,specs,old,prefix,grant=ready
    assert (grant['prefix_requests'],len(grant['legacy_sha256']))==(34,20)
    assert grant['package']=='Q26-P35-explanation-following'
    assert (grant['request_ceiling'],grant['reserved_ceiling_cny'])==(16,'0.512')
    value=case('合成本步结果核对');approval=tmp_path/'approval.json'
    approval.write_text(json.dumps(approval_values(_today())),encoding='utf-8');calls=[]
    def handler(request):
        calls.append(request);result=response('synthetic-q26')
        if outcome=='unknown_usage':result.pop('usage')
        return httpx.Response(200,json=result)
    adapter=DeepSeekModelAdapter(ledger,config=ModelConfig('synthetic-test-key-not-an-account','synthetic',_today(),'user_reported','test',Decimal(0)),input_validator=lambda x:x,transport=httpx.MockTransport(handler),q26_grant_path=ledger.q26_path,q26_authorizer=lambda x:permit(ready,'0'*64 if outcome=='actual_changed' else fingerprint(x)),protocol_report_dir=tmp_path/'d3-35/protocol')
    async def generate():return await adapter.generate_experiment(value,time.monotonic()+5,approval_path=approval)
    if outcome=='success':
        result=asyncio.run(generate());assert ledger.receipt_snapshot()[34]['request_id']==result.reservation_id
        assert not ledger.q26_stop_path.exists()
    else:
        with pytest.raises(ModelUnavailable):asyncio.run(generate())
        assert ledger.q26_stop_path.exists()
        with pytest.raises(ModelUnavailable):asyncio.run(generate())
    assert len(calls)==(0 if outcome=='actual_changed' else 1)
    assert ledger.summary()['unresolved_requests']==(1 if outcome=='unknown_usage' else 0)
    assert ledger.receipt_snapshot()[:34]==prefix
    assert all(p.read_bytes()==data for p,data in old.items())

def test_q25_must_be_settled_and_closed_before_new_grant(q25_ready):
    ledger,_,specs,*_=q25_ready
    with pytest.raises(BudgetUnavailable,match='q26_prefix_mismatch'):create_q19_grant(ledger,specs,'f'*64,batch='Q26')
    reservation=ledger.reserve(experiment=True,q25_permit=q25_permit(q25_ready))
    with pytest.raises(BudgetUnavailable,match='usage_unresolved'):create_q19_grant(ledger,specs,'f'*64,batch='Q26')
    settled(ledger,reservation)
    with pytest.raises(BudgetUnavailable,match='q26_legacy_invalid'):create_q19_grant(ledger,specs,'f'*64,batch='Q26')
    assert not ledger.q26_path.exists()

def test_old_permits_cannot_continue_and_terminal_failure_stops_q26(ready,monkeypatch):
    ledger,_,specs,old,prefix,_=ready
    with pytest.raises(BudgetUnavailable,match='q26_scope_required'):ledger.reserve(experiment=True)
    for batch in ('Q19','Q20','Q21','Q22','Q23','Q24','Q25'):
        old_permit=validated_q19_permit(permit(ready).binding,getattr(ledger,batch.lower()+'_path'),batch=batch)
        with pytest.raises(BudgetUnavailable,match=batch.lower()+'_batch_stopped'):ledger.reserve(experiment=True,**{batch.lower()+'_permit':old_permit})
    for name in ('ROUND','PACKAGE','LOCAL','ENTRY'):monkeypatch.setattr(task,name,getattr(task,name))
    task.configure_round35();assert task.shared_prompt_version()=='assistant-10'
    assert task.grant_paths(ledger)==(ledger.q26_path,ledger.q26_stop_path)
    with pytest.raises(RuntimeError,match='independent_terminal_failed'):task.finish_task_from_terminal(ledger,specs[0]['task_ref'],{'passed':False})
    with pytest.raises(BudgetUnavailable,match='q26_batch_stopped'):ledger.reserve(experiment=True,q26_permit=permit(ready))
    assert ledger.receipt_snapshot()==prefix and all(p.read_bytes()==data for p,data in old.items())

def test_q25_decision_cannot_initialize_q26(tmp_path,monkeypatch):
    import services.guide.budget as budget
    for name in ('ROUND','PACKAGE','LOCAL','ENTRY'):monkeypatch.setattr(task,name,getattr(task,name))
    task.configure_round35();monkeypatch.setattr(task,'checked_review',lambda path:{})
    monkeypatch.setattr(budget,'BudgetLedger',lambda *a:pytest.fail('reject old decision before ledger'))
    manifest=tmp_path/'review-manifest.json';manifest.write_text('{}')
    decision=tmp_path/'decision.json';decision.write_text(json.dumps({'package':'Q25-P34-step-result','approved':True,'source_kind':'explicit_user_reply','source_reply':'批准Q25','review_manifest_sha256':task.sha(manifest)}),encoding='utf-8')
    with pytest.raises(RuntimeError,match='explicit_matching_decision'):task.init_grant(tmp_path,decision)

@pytest.mark.parametrize('attribute',['q25_path','q25_stop_path'])
def test_changed_previous_sidecar_stops_without_reservation(ready,attribute):
    ledger=ready[0];p=getattr(ledger,attribute);p.write_bytes(p.read_bytes()+b' ');before=ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable,match='q26_grant_invalid'):ledger.reserve(experiment=True,q26_permit=permit(ready))
    assert ledger.path.read_bytes()==before and ledger.q26_stop_path.exists()

@pytest.mark.parametrize('field,value',[('request_ceiling',17),('reserved_ceiling_cny','0.544')])
def test_new_ceiling_cannot_be_replaced_with_previous_nominal_budget(ready,field,value):
    ledger=ready[0];grant=json.loads(ledger.q26_path.read_text());grant[field]=value
    ledger.q26_path.write_text(json.dumps(grant));before=ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable,match='q26_grant_invalid'):ledger.reserve(experiment=True,q26_permit=permit(ready))
    assert ledger.path.read_bytes()==before

def test_sixteenth_new_request_is_last_even_if_task_rounds_remain(ready):
    ledger,now,specs,old,prefix,_=ready
    for task_index,rounds in ((1,9),(2,7)):
        for round_index in range(1,rounds+1):
            now[0]+=11;settled(ledger,ledger.reserve(experiment=True,q26_permit=permit(ready,task_index=task_index,round_index=round_index)))
            confirm_q19_round(ledger,specs[task_index-1]['task_ref'],round_index,batch='Q26')
        if task_index==1:finish_q19_task(ledger,specs[0]['task_ref'],batch='Q26')
    assert ledger.summary()['physical_requests']==50
    before=ledger.path.read_bytes();now[0]+=11
    with pytest.raises(BudgetUnavailable,match='q26_request_limit'):ledger.reserve(experiment=True,q26_permit=permit(ready,task_index=2,round_index=8))
    assert ledger.path.read_bytes()==before and ledger.receipt_snapshot()[:34]==prefix
    assert all(p.read_bytes()==data for p,data in old.items())
    assert json.loads(ledger.q26_stop_path.read_text())['reason']=='q26_request_limit'
