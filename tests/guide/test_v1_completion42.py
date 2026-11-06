"""Q34只验证新包7次/52总次数门，不重复业务/UI矩阵。"""
import asyncio,json,time
from decimal import Decimal
import httpx,pytest
from test_v1_confirmation42 import ready as q33_ready,permit as q33_permit,q32_ready,q31_ready,q30_ready,q30_permit,q29_ready,q29_permit,q28_ready,q28_permit,q27_ready,q27_permit,q26_ready,q26_permit,q25_ready,q25_permit,q24_ready,q23_ready,q22_ready,q21_ready,q20_ready,q19_ready
from test_q18_budget import settled
from test_q16_budget import case,response
from services.guide.task_budget import create_q19_grant,stop_q19_batch,confirm_q19_round,finish_q19_task,TaskPermitBinding,validated_q19_permit
from services.guide.budget import BudgetUnavailable
from services.guide.model import DeepSeekModelAdapter,ModelConfig,_today
from services.guide.experiment import approval_values
from services.guide.knowledge import fingerprint

@pytest.fixture
def ready(q33_ready):
 l,now,specs,old,_=q33_ready
 for turn in (1,2):
  now[0]+=11;settled(l,l.reserve(experiment=True,q33_permit=q33_permit(q33_ready,turn=turn)))
  if turn==1:confirm_q19_round(l,specs[0]['task_ref'],turn,batch='Q33')
 stop_q19_batch(l,'approval_review_unavailable',batch='Q33');now[0]+=61
 old={p:p.read_bytes() for p in (*old,l.q33_path,l.q33_stop_path)}
 specs=[dict(s,task_ref=s['task_ref'].replace('q33','q34')) for s in specs]
 g=create_q19_grant(l,specs,'a'*64,batch='Q34');return l,now,specs,old,g

def permit(ready,task=1,turn=1,actual='a'*64):
 l,_,specs,*_=ready;s=specs[task-1]
 return validated_q19_permit(TaskPermitBinding(s['task_ref'],task,turn,1,'b'*64,actual,s['scope_hash']),l.q34_path,batch='Q34')

def test_exact_seven_new_requests_reach_52_only_with_q34_and_close(ready):
 l,now,specs,old,g=ready
 assert g['prefix_requests']==45 and len(g['legacy_sha256'])==36 and g['request_ceiling']==7
 with pytest.raises(BudgetUnavailable):l.reserve(experiment=True)
 for task,n in ((1,5),(2,1),(3,1)):
  for turn in range(1,n+1):
   now[0]+=11;settled(l,l.reserve(experiment=True,q34_permit=permit(ready,task,turn)))
   confirm_q19_round(l,specs[task-1]['task_ref'],turn,batch='Q34')
  finish_q19_task(l,specs[task-1]['task_ref'],batch='Q34')
 assert l.summary()['physical_requests']==52 and old=={p:p.read_bytes() for p in old}
 with pytest.raises(BudgetUnavailable):l.reserve(experiment=True,q34_permit=permit(ready))
 l.q34_path.write_text('{}')
 with pytest.raises(BudgetUnavailable):l.summary()

def test_q34_transport_and_scope_single_send(ready,tmp_path):
 l=ready[0];v=case('only transport');p=tmp_path/'approval.json';p.write_text(json.dumps(approval_values(_today())))
 calls=[]
 def handler(req):calls.append(req);return httpx.Response(200,json=response())
 a=DeepSeekModelAdapter(l,config=ModelConfig('synthetic-test-key-not-an-account','synthetic',_today(),'user_reported','test',Decimal(0)),input_validator=lambda x:x,
  transport=httpx.MockTransport(handler),q34_grant_path=l.q34_path,q34_authorizer=lambda x:permit(ready,actual=fingerprint(x)),protocol_report_dir=tmp_path/'protocol')
 asyncio.run(a.generate_experiment(v,time.monotonic()+5,approval_path=p))
 assert len(calls)==1 and l.summary()['physical_requests']==46
 with pytest.raises(BudgetUnavailable):l.reserve(experiment=True,q34_permit=permit(ready,turn=2))
 assert l.q34_stop_path.exists()
