"""V1-02 新范围/一次确认/原连续账本与真实运输入口的零发送反例。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
import time
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from services.guide.assistant import Ack as AssistantAck,Chat,Identity
from services.guide.budget import BudgetLedger,BudgetUnavailable
from services.guide.experiment import approval_values
from services.guide.model import DeepSeekModelAdapter,ModelConfig,ModelUnavailable,ModelResult,TokenUsage,_today
from services.guide.knowledge import fingerprint
from services.guide.task_budget import TaskPermitBinding,validated_q19_permit,create_q19_grant,stop_q19_batch,confirm_q19_round,finish_q19_task
from services.guide.v1_runtime import PersonalRuntime
from services.guide.v1_scope import GOALS,FEEDBACK
from services.guide.action_contract import action_digest
from test_assistant import Harness,Request,knowledge,observation
from test_round40_entry import (ready as q31_ready,permit as q31_permit,q30_ready,q30_permit,q29_ready,q29_permit,q28_ready,q28_permit,q27_ready,q27_permit,q26_ready,q26_permit,q25_ready,q25_permit,q24_ready,q23_ready,q22_ready,q21_ready,q20_ready,q19_ready)
from test_q18_budget import settled
from test_q16_budget import response,case


@pytest.fixture
def ready(q31_ready):
    ledger,now,_,old,*_=q31_ready
    settled(ledger,ledger.reserve(experiment=True,q31_permit=q31_permit(q31_ready)))
    stop_q19_batch(ledger,'formal_delivery_failed',batch='Q31'); now[0]+=61
    old={p:p.read_bytes() for p in (*old,ledger.q31_path,ledger.q31_stop_path)}
    specs=[{'task_ref':'q32-'+c,'case_id':c,'group':'personal','max_rounds':n,'scope_hash':str(i)*64}
        for i,(c,n) in enumerate((('V1-AC',8),('V1-B',1),('V1-D',1)),1)]
    grant=create_q19_grant(ledger,specs,'d'*64,batch='Q32')
    return ledger,now,specs,old,grant


def permit(ready,task=1,round=1,physical=1,actual='a'*64):
    l,_,specs,*_=ready;s=specs[task-1]
    return validated_q19_permit(TaskPermitBinding(s['task_ref'],task,round,physical,'b'*64,actual,s['scope_hash']),l.q32_path,batch='Q32')


def test_q32_same_ledger_three_tasks_ten_calls_and_closed_legacy(ready):
    l,now,specs,old,g=ready
    assert g['prefix_requests']==40 and g['request_ceiling']==10 and g['reserved_ceiling_cny']=='0.320'
    assert len(g['legacy_sha256'])==32
    for task,n in ((1,8),(2,1),(3,1)):
        for turn in range(1,n+1):
            now[0]+=11
            settled(l,l.reserve(experiment=True,q32_permit=permit(ready,task,turn)))
            confirm_q19_round(l,specs[task-1]['task_ref'],turn,batch='Q32')
        finish_q19_task(l,specs[task-1]['task_ref'],batch='Q32')
    assert l.summary()['physical_requests']==50
    assert json.loads(l.q32_stop_path.read_text())['reason']=='completed'
    assert old=={p:p.read_bytes() for p in old}
    with pytest.raises(BudgetUnavailable):l.reserve(experiment=True,q32_permit=permit(ready))


@pytest.mark.parametrize('bad',['missing','order','scope','replay','unconfirmed','deadline','unresolved','fee','mix','changed_legacy','rate'])
def test_q32_fail_closed_without_extra_send(ready,bad):
    l,now,specs,old,g=ready;p=permit(ready)
    if bad in ('replay','unconfirmed','unresolved'):
        r=l.reserve(experiment=True,q32_permit=p)
        if bad!='unresolved':settled(l,r)
    if bad=='order':p=permit(ready,task=2)
    if bad=='scope':p=validated_q19_permit(replace(p.binding,scope_hash='f'*64),l.q32_path,batch='Q32')
    if bad=='replay':p=permit(ready,physical=2,actual='c'*64)
    if bad=='unconfirmed':p=permit(ready,round=2)
    if bad=='deadline':
        settled(l,l.reserve(experiment=True,q32_permit=p));confirm_q19_round(l,specs[0]['task_ref'],1,batch='Q32');now[0]+=300;p=permit(ready,round=2)
    if bad=='changed_legacy':l.q31_stop_path.write_text('{}')
    if bad=='rate':
        for turn in range(1,7):
            settled(l,l.reserve(experiment=True,q32_permit=permit(ready,round=turn)))
            confirm_q19_round(l,specs[0]['task_ref'],turn,batch='Q32')
        p=permit(ready,round=7)
    before=l.path.read_bytes()
    kw={} if bad=='missing' else {'q32_permit':p}
    if bad=='fee':kw['additional_fee_cny']=Decimal('.01')
    if bad=='mix':kw['q31_permit']=p
    with pytest.raises(BudgetUnavailable):l.reserve(experiment=True,**kw)
    assert l.path.read_bytes()==before
    if bad!='missing':assert l.q32_stop_path.exists()


@pytest.mark.parametrize('failure',[None,'transport','usage'])
def test_q32_real_adapter_mock_transport_no_real_network(ready,tmp_path,failure):
    l,_,_,_,_=ready;value=case('只用于运输形状');approval=tmp_path/'approval.json'
    approval.write_text(json.dumps(approval_values(_today())))
    seen=[]
    def handler(request):
        seen.append(request)
        if failure=='transport':return httpx.Response(500)
        body=response()
        if failure=='usage':body.pop('usage')
        return httpx.Response(200,json=body)
    adapter=DeepSeekModelAdapter(l,config=ModelConfig('synthetic-test-key-not-an-account','synthetic',_today(),'user_reported','test',Decimal(0)),
        input_validator=lambda x:x,transport=httpx.MockTransport(handler),q32_grant_path=l.q32_path,
        q32_authorizer=lambda x:permit(ready,actual=fingerprint(x)),protocol_report_dir=tmp_path/'protocol')
    call=adapter.generate_experiment(value,time.monotonic()+10,approval_path=approval)
    if failure:
        with pytest.raises(ModelUnavailable):asyncio.run(call)
        assert l.q32_stop_path.exists() and l.summary()['unresolved_requests']==1
    else:asyncio.run(call)
    assert len(seen)==1 and l.summary()['physical_requests']==41
    actual=json.loads(seen[0].content)
    assert actual['max_tokens']==512 and actual['thinking']=={'type':'disabled'} and actual['stream'] is False


def local(tmp_path):
    h=Harness(tmp_path,{})
    h.current=Identity('synthetic-session','personal_alice')
    release=h.releases.save_candidate(knowledge(),[observation('profile'),observation('trips')],prompt_version='assistant-14')
    h.releases.publish(release['version'])
    obs={'page':'welcome','title':'欢迎与身份','items':['差旅工作台'],'product_version':'v1',
        'semantics':{'schema':'page-semantics-1','selected_trip':True,'zones':[],'controls':[]}}
    booking_obs={**deepcopy(obs),'page':'booking','title':'预订与票据','items':['票据']}
    raw={'product_id':'travel-lab','product_version':'v1','contract_version':'d3-personal-1','knowledge_version':'personal-guide-1',
        'rule_version':'booking-v1','policy_version':'subject-assignment-v1','feature_config_version':'personal-1','source_kind':'observed',
        'environment':'local-demo','simulated_identity':'synthetic_personal','disclosure_scope':'current-session',
        'assumptions':['synthetic_demo_account','simulated_business_services'],'unknowns':[],
        'business_time':'2026-09-27T00:00:00+00:00','state_versions':{'trip':1,'payment_profile':1},
        'payment_profile_state':'usable','flight_state':'available','data':{
        'trip':{'ref':'trip','revision':1,'flight_id':'DEMO-PEK-SHA','travel_date':'2026-10-01','passengers':1,'status':'paid'},
        'quote':{'ref':'quote','trip_revision':1,'amount_minor':110000,'currency':'DEMO','effective_status':'active','valid':True,'expires_at':'2026-09-27T00:05:00+00:00','price_basis':'estimate'},
        'lock':{'ref':'lock','trip_revision':1,'amount_minor':110000,'currency':'DEMO','effective_status':'consumed','valid':False,'expires_at':'2026-09-27T00:05:00+00:00','price_basis':'frozen'},
        'payment':{'ref':'payment','amount_minor':110000,'currency':'DEMO','status':'succeeded'},
        'ticket_task':{'ref':'ticket_task','status':'succeeded'},
        'ticket':{'ref':'ticket','status':'valid','trip_revision':1}}}
    h.private='stable-private-owner-object-state';h.raw=raw
    async def capture(*args):
        now=datetime.now(timezone.utc);return dict(deepcopy(h.raw),observed_at=now.isoformat(),valid_until=(now+timedelta(seconds=30)).isoformat()),h.private
    h.state=capture
    def factory(validator):
        async def generate(payload,deadline):
            validator(payload);h.calls.append(deepcopy(payload));c=json.loads(payload['messages'][1]['content'])['action_evidence']
            v={'v':2,'action':{'kind':'navigate','target':'task','entry':'navigate','params':{'page':'booking'}},'basis':c['basis'],
                'evidence':[{'id':k,'value':c['facts'][k]} for k in ('selected_trip','trip.ticket.status')],'gap':None}
            calls=[{'id':'synthetic-booking-navigation','name':'navigate','arguments':{'page':'booking'}}]
            return ModelResult({'content':json.dumps(v),'tool_calls':calls},TokenUsage(0,0,0,0,0),'offline',Decimal(0),'offline')
        return SimpleNamespace(generate_experiment=generate)
    h.adapter_factory=factory
    trip=uuid4();runtime=PersonalRuntime(h,trip,release=release,observations={'welcome':obs,'booking':booking_obs},ledger=BudgetLedger(tmp_path/'not-created.json'),directory=tmp_path/'receipts',real_model=False)
    body=Chat(message=GOALS[0],page='welcome',trip_id=trip,observation=obs)
    return h,runtime,body


def confirmation(formal):
    return {'action_sha256':formal['action_sha256'],'reply_sha256':action_digest(formal['reply']),'action':formal['action_contract']['action']}


def test_recapture_time_can_change_but_original_evidence_and_semantic_binding_hold(tmp_path):
    async def run():
        h,r,b=local(tmp_path);f=await h.chat(b,Request())
        # 本地合成ACK只验证导航/确认链，不计为真实浏览器或模型验收。
        ack=AssistantAck(session_id=f['session_id'],nonce=f['tool']['nonce'],success=True,
            observation=r.observations['booking'])
        assert (await h.ack(ack,Request()))['navigation_status']=='succeeded'
        a=await r.confirm(confirmation(f),Request())
        assert a['kind']=='navigate' and a['params']=={'page':'booking'}
        r.complete({'kind':'navigate','accepted':True,'page':'booking','dom_verified':True},terminal=True)
        assert r.sequence==2 and r.round==0 and not r.stopped and not r.ledger.path.exists()
    asyncio.run(run())


@pytest.mark.parametrize('bad',['identity','private_state','expiry','reply','action','replay','release','second_question','object','delegation','extra_raw','amount','goal','page_text','unconfirmed'])
def test_runtime_new_boundary_rejections(tmp_path,bad):
    async def run():
        h,r,b=local(tmp_path)
        if bad in ('object','delegation','extra_raw','amount','goal','page_text'):
            if bad=='object':b=b.model_copy(update={'trip_id':uuid4()})
            if bad=='delegation':b=b.model_copy(update={'delegated_trip_ids':(b.trip_id,uuid4())})
            if bad=='extra_raw':h.raw['private_source']='hidden'
            if bad=='amount':h.raw['data']['quote']['amount_minor']=1
            if bad=='goal':b=b.model_copy(update={'message':'改成企业双对象'})
            if bad=='page_text':b.observation.items=['包含不在审查中的文字']
            with pytest.raises(Exception):await h.chat(b,Request())
            assert len(h.calls)==0 and r.stopped;return
        f=await h.chat(b,Request());c=confirmation(f)
        if bad=='identity':h.current=Identity('other','personal_alice')
        if bad=='private_state':h.private='different-object-or-state'
        if bad=='expiry':r.task_started=time.monotonic()-301
        if bad=='reply':c['reply_sha256']='a'*64
        if bad=='action':c['action']=dict(c['action'],kind='pay')
        if bad=='release':h.releases.rollback()
        if bad=='replay':await r.confirm(c,Request())
        if bad in ('second_question','unconfirmed'):
            with pytest.raises(Exception):await h.chat(b.model_copy(update={'session_id':f['session_id'],'message':FEEDBACK}),Request())
        else:
            with pytest.raises(Exception):await r.confirm(c,Request())
        assert r.stopped and not r.ledger.path.exists()
    asyncio.run(run())


@pytest.mark.parametrize('change',['absent','hash','source','question','reply','false','date'])
def test_package_requires_explicit_matching_decision_before_ledger(tmp_path,monkeypatch,change):
    from scripts import v1_package as package
    directory=tmp_path/'review';directory.mkdir();(directory/'manifest.json').write_text('{}')
    decision={'manifest_sha256':package.sha(directory/'manifest.json'),'source_kind':'explicit_user_reply',
        'question_id':'synthetic-question','reply':'同意具体包','approved':True,'approved_on':_today()}
    if change=='hash':decision['manifest_sha256']='a'*64
    if change=='source':decision['source_kind']='inferred'
    if change=='question':decision['question_id']=''
    if change=='reply':decision['reply']=''
    if change=='false':decision['approved']=False
    if change=='date':decision['approved_on']='2026-01-01'
    if change!='absent':(directory/'decision.json').write_text(json.dumps(decision),encoding='utf-8')
    monkeypatch.setattr(package,'checked_package',lambda _: {})
    def unavailable(*a,**kw):raise AssertionError('must not touch ledger before approval')
    monkeypatch.setattr(package,'BudgetLedger',unavailable)
    with pytest.raises((ValueError,FileNotFoundError)):package.init_grant(directory)


def test_package_detects_code_change_and_no_fake_approval(tmp_path,monkeypatch):
    from scripts import v1_package as p
    monkeypatch.setattr(p,'ROOT',tmp_path)
    d=tmp_path/'.local/v1-42/review';d.mkdir(parents=True)
    source=tmp_path/'executed.py';source.write_text('original')
    (d/'package.json').write_text(json.dumps({'approved':False,'execution_ready':True}))
    (d/'experiment.json').write_text(json.dumps(approval_values(_today())))
    (d/'manifest.json').write_text(json.dumps({'schema':'v1-executable-package-1','files':{'executed.py':p.sha(source)}}))
    assert p.checked_package(d)['approved'] is False
    source.write_text('changed')
    with pytest.raises(ValueError,match='frozen_file_changed'):p.checked_package(d)


def test_engineering_routes_require_local_secret_without_database_or_model(tmp_path):
    from scripts.v1_task import make_app
    from fastapi.testclient import TestClient
    from services.business.app import Settings
    from services.guide.releases import Releases
    app,_=make_app(Settings('postgresql://unavailable','test',True,'test'),tmp_path,str(uuid4()),Releases('test',tmp_path/'r.json'))
    with TestClient(app) as c:
        for endpoint in ('install','pending','confirm','complete'):
            assert c.post('/__v1/'+endpoint,json={}).status_code==403
