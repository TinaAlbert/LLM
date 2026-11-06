"""Q33只修复工程审读与观察新鲜度混用；Q32保持已关闭历史。"""
import asyncio,json,time
from decimal import Decimal
import httpx,pytest
from services.guide.task_budget import create_q19_grant,stop_q19_batch,confirm_q19_round,finish_q19_task,TaskPermitBinding,validated_q19_permit
from services.guide.model import DeepSeekModelAdapter,ModelConfig,ModelUnavailable,_today
from services.guide.experiment import approval_values
from services.guide.knowledge import fingerprint
from services.guide.budget import BudgetUnavailable
from test_v1_execution42 import (ready as q32_ready,permit as q32_permit,local,confirmation,q31_ready,q30_ready,q30_permit,q29_ready,q29_permit,q28_ready,q28_permit,q27_ready,q27_permit,q26_ready,q26_permit,q25_ready,q25_permit,q24_ready,q23_ready,q22_ready,q21_ready,q20_ready,q19_ready)
from test_q18_budget import settled
from test_q16_budget import response,case
from test_assistant import Request

@pytest.fixture
def ready(q32_ready):
    l,now,specs,old,_=q32_ready
    for turn in (1,2,3):
        now[0]+=11;settled(l,l.reserve(experiment=True,q32_permit=q32_permit(q32_ready,round=turn)))
        if turn<3:confirm_q19_round(l,specs[0]['task_ref'],turn,batch='Q32')
    stop_q19_batch(l,'operator_confirmation_timeout',batch='Q32');now[0]+=61
    old={p:p.read_bytes() for p in (*old,l.q32_path,l.q32_stop_path)}
    specs=[{'task_ref':'q33-'+c,'case_id':c,'group':'personal','max_rounds':n,'scope_hash':str(i)*64} for i,(c,n) in enumerate((('V1-AC',5),('V1-B',1),('V1-D',1)),1)]
    grant=create_q19_grant(l,specs,'d'*64,batch='Q33')
    return l,now,specs,old,grant

def permit(ready,task=1,turn=1,actual='a'*64):
    l,_,specs,*_=ready;s=specs[task-1]
    return validated_q19_permit(TaskPermitBinding(s['task_ref'],task,turn,1,'b'*64,actual,s['scope_hash']),l.q33_path,batch='Q33')

def test_q33_max_seven_new_calls_same_43_prefix_all_old_sidecars(ready):
    l,now,specs,old,g=ready
    assert g['prefix_requests']==43 and len(g['legacy_sha256'])==34 and g['request_ceiling']==7 and g['reserved_ceiling_cny']=='0.224'
    for task,n in ((1,5),(2,1),(3,1)):
        for turn in range(1,n+1):
            now[0]+=11;settled(l,l.reserve(experiment=True,q33_permit=permit(ready,task,turn)))
            confirm_q19_round(l,specs[task-1]['task_ref'],turn,batch='Q33')
        finish_q19_task(l,specs[task-1]['task_ref'],batch='Q33')
    assert l.summary()['physical_requests']==50 and old=={p:p.read_bytes() for p in old}
    with pytest.raises(BudgetUnavailable):l.reserve(experiment=True,q33_permit=permit(ready))

@pytest.mark.parametrize('failure',[False,True])
def test_q33_actual_adapter_single_transport_failure_closes_batch(ready,tmp_path,failure):
    l=ready[0];v=case('仅运输测试');p=tmp_path/'approval.json';p.write_text(json.dumps(approval_values(_today())))
    calls=[]
    def handler(req):calls.append(req);return httpx.Response(500) if failure else httpx.Response(200,json=response())
    a=DeepSeekModelAdapter(l,config=ModelConfig('synthetic-test-key-not-an-account','synthetic',_today(),'user_reported','test',Decimal(0)),
        input_validator=lambda x:x,transport=httpx.MockTransport(handler),q33_grant_path=l.q33_path,
        q33_authorizer=lambda x:permit(ready,actual=fingerprint(x)),protocol_report_dir=tmp_path/'protocol')
    run=a.generate_experiment(v,time.monotonic()+5,approval_path=p)
    if failure:
        with pytest.raises(ModelUnavailable):asyncio.run(run)
        assert l.q33_stop_path.exists()
    else:asyncio.run(run)
    assert len(calls)==1 and l.summary()['physical_requests']==44


def test_original_observation_expiry_is_not_current_authorization(tmp_path):
    async def run():
        h,r,b=local(tmp_path);f=await h.chat(b,Request());r.pending['expires']=0
        await r.confirm(confirmation(f),Request())
        proof=json.loads((tmp_path/'receipts/1-01-revalidation.json').read_text(encoding='utf-8'))
        assert proof['old_snapshot_used_as_current_authorization'] is False
        assert proof['fresh_observed_at']!=proof['original_observed_at'] and r.pending['expires']>time.monotonic()
        with pytest.raises(ModelUnavailable):await r.confirm(confirmation(f),Request())
    asyncio.run(run())



def test_watchdog_persists_deadline_without_next_request(tmp_path):
    h,r,b=local(tmp_path)
    r.task_started=time.monotonic()-301
    assert r.expire_if_due() is True
    assert json.loads((tmp_path/'receipts/stopped.json').read_text(encoding='utf-8'))['reason']=='task_deadline'
    assert r.expire_if_due() is False


def test_browser_abort_requires_host_key_and_stops_runtime_without_business(tmp_path):
    from scripts.v1_task import make_app
    from services.business.app import Settings
    from services.guide.releases import Releases
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from uuid import uuid4
    app,host=make_app(Settings('postgresql://unavailable','test',True,'test'),tmp_path,str(uuid4()),Releases('test',tmp_path/'r.json'))
    reasons=[];host['runtime']=SimpleNamespace(stop=reasons.append,stopped=False)
    key=json.loads((tmp_path/'host-secret.json').read_text())['key']
    with TestClient(app) as c:
        assert c.post('/__v1/abort',json={}).status_code==403 and reasons==[]
        assert c.post('/__v1/abort',headers={'X-V1-Host':key},json={}).json()=={'stopped':True}
    assert reasons==['engineering_browser_failed']
