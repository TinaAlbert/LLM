"""V1-01真实格式候选→正式ASGI→可信呈现/确认/回执；全部替身，零模型/PG。"""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from services.guide.action_contract import (ACTION_ENTRIES, EVIDENCE_CONTRACT_VERSION, action_digest,
    evidence_context, validate_evidence_contract, render_evidence_contract, confirm_evidence_delivery)
from services.guide.assistant import Ack, Chat, display_state, SYSTEM_V13
from services.guide.experiment import parse_response, input_size_bytes, MAX_INPUT_BYTES
from test_assistant import Harness, Request, knowledge, observation
from test_reply_receipts30 import app_for, post, receipt


def state():
    return {'contract_version':'d3-personal-1','source_kind':'observed',
        'payment_profile_state':'usable','flight_state':'available','business_time':'2026-10-29T01:00:00+00:00',
        'state_versions':{'trip':1,'payment_profile':1},
        'data':{'trip':{'ref':'trip','revision':1,'status':'draft'},
                'quote':{'ref':'quote','amount_minor':87500,'valid':True,'effective_status':'active','price_basis':'estimate'},
                'lock':None,'payment':None,'ticket_task':None,'ticket':None}}


def candidate(h, kind='lock', *, target=None, fields=None, gap=None):
    c=evidence_context(h.facts,h.releases.active()['knowledge_hash'])
    fields=fields or {'lock':['trip.quote.valid','trip.quote.amount_display'], 'pay':['trip.lock.valid','trip.lock.amount_display'],
        'query':['trip.payment.status'], 'wait':['trip.ticket_task.status'], 'end':['trip.ticket.status']}.get(kind,['selected_trip'])
    return {'v':2,'action':{'kind':kind,'target':target or ('trip' if kind in {'lock','pay','query','requote','cancel'} else 'task'),
        'entry':ACTION_ENTRIES[kind],'params':{'page':'profile'} if kind=='navigate' else {}},
        'basis':c['basis'],'evidence':[{'id':k,'value':c['facts'][k]} for k in fields], 'gap':gap}


def harness(tmp_path):
    h=Harness(tmp_path,{});h.facts=state()
    release=h.releases.save_candidate(knowledge(),[observation('profile'),observation('trips')],prompt_version='assistant-13')
    h.releases.publish(release['version'])
    return h


def configure(h,value,calls=None):
    # Harness's adapter closure supplies the same dictionary, fill it via a fresh factory.
    from services.guide.model import ModelResult,TokenUsage
    from decimal import Decimal
    from types import SimpleNamespace
    def factory(validator):
        async def generate(payload,deadline):
            validator(payload);h.calls.append(payload)
            if h.after:h.after()
            parsed=parse_response({'choices':[{'index':0,'finish_reason':'tool_calls' if calls else 'stop','message':{
                'role':'assistant','content':json.dumps(value,ensure_ascii=False),**({'tool_calls':[{'id':x['id'],'type':'function','function':{'name':x['name'],'arguments':json.dumps(x['arguments'])}} for x in calls]} if calls else {})}}]},True,False)
            return ModelResult(parsed,TokenUsage(100,50,150,0,100),'synthetic',Decimal('0'),'synthetic')
        return SimpleNamespace(generate_experiment=generate)
    h.adapter_factory=factory


def request(**changes):
    return Chat(message='请帮助我为当前本人行程取得有效票。',page='trips',observation=observation('trips'),trip_id=changes.pop('trip_id',uuid4()),**changes)


def confirmed(formal):
    return {'action_sha256':formal['action_sha256'],'reply_sha256':action_digest(formal['reply']),'action':formal['action_contract']['action']}


def test_formal_lock_evidence_presentation_confirmation_and_input(tmp_path,monkeypatch):
    async def run():
        h=harness(tmp_path);v=candidate(h);configure(h,v)
        result=await post(app_for(monkeypatch,h),request());assert result.status_code==200
        formal=result.json();assert formal['action_contract']==v and formal['action_contract_version']==EVIDENCE_CONTRACT_VERSION
        assert '875.00 模拟元' in formal['reply'] and '本轮选中行程' in formal['reply']
        assert formal['reply']==render_evidence_contract(v)
        ctx=json.loads(h.calls[0]['messages'][1]['content'])['action_evidence']
        assert confirm_evidence_delivery(formal,confirmed(formal),state=h.facts,context=ctx,allowed_pages=h.current.allowed_pages)==v['action']
        assert h.calls[0]['messages'][0]['content']==SYSTEM_V13 and input_size_bytes(h.calls[0])<=MAX_INPUT_BYTES
        bad=confirmed(formal);bad['action']={**v['action'],'kind':'pay'}
        with pytest.raises(ValueError,match='confirmation'):confirm_evidence_delivery(formal,bad,state=h.facts,context=ctx,allowed_pages=h.current.allowed_pages)
        report=json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert json.loads(report['candidate']['content'])==v and report['candidate']['content']!=formal['reply']
    asyncio.run(run())


@pytest.mark.parametrize('case,reason',[
    ('amount','action_fact_conflict'),('status','action_fact_conflict'),('eligibility','action_fact_conflict'),
    ('type','action_fact_conflict'),('foreign','action_target_invalid'),('role','action_fields_invalid'),
    ('missing','action_evidence_required'),('unknown_id','action_evidence_invalid'),('duplicate','action_evidence_invalid'),
    ('stale','action_evidence_stale'),('knowledge','action_evidence_stale'),('free_text','action_fields_invalid'),
    ('multiaction','action_fields_invalid'),('shape','action_kind_invalid'),('version','action_version_invalid')])
def test_formal_rejections_do_not_correct_or_display_bad_candidate(tmp_path,monkeypatch,case,reason):
    async def run():
        h=harness(tmp_path);v=candidate(h)
        if case=='amount':v['evidence'][1]['value']='1.00 模拟元'
        if case=='status':v['evidence'][0]['value']=False
        if case=='eligibility':v['evidence'].append({'id':'payment_profile_state','value':'unusable'})
        if case=='type':v['evidence'][0]['value']=1
        if case=='foreign':v['action']['target']='trip_2'
        if case=='role':v['role']='admin'
        if case=='missing':v['evidence']=v['evidence'][:1]
        if case=='unknown_id':v['evidence'][0]['id']='private.owner'
        if case=='duplicate':v['evidence'].append(v['evidence'][0])
        if case=='stale':v['basis']['state']='old'
        if case=='knowledge':v['basis']['knowledge']='old'
        if case=='free_text':v['reason']='应付额随便说，之后再付款。'
        if case=='multiaction':v['action']=[v['action'],v['action']]
        if case=='shape':v['action']['kind']=[]
        if case=='version':v['v']=1
        configure(h,v);result=await post(app_for(monkeypatch,h),request())
        assert result.status_code==503 and receipt(h)['reason']==reason
        assert all(not s.history and s.pending is None for s in h.sessions.values())
    asyncio.run(run())


@pytest.mark.parametrize('case',['expired','unusable','unavailable','processing','paid','corporate','no_selection'])
def test_correct_evidence_cannot_authorize_unavailable_lock(tmp_path,monkeypatch,case):
    async def run():
        h=harness(tmp_path)
        if case=='expired':h.facts['data']['quote'].update(valid=False,effective_status='expired')
        if case=='unusable':h.facts['payment_profile_state']='unusable'
        if case=='unavailable':h.facts['flight_state']='unavailable'
        if case in {'processing','paid'}:h.facts['data']['payment']={'status':'processing' if case=='processing' else 'succeeded','amount_minor':87500}
        if case=='corporate':h.facts['contract_version']='assistant-corporate-1'
        if case=='no_selection':h.facts.pop('data')
        configure(h,candidate(h));result=await post(app_for(monkeypatch,h),request())
        assert result.status_code==503
    asyncio.run(run())


@pytest.mark.parametrize('kind',['pay','query','wait','end','unknown','clarify'])
def test_other_normal_paths_and_unknown_are_deliverable(tmp_path,monkeypatch,kind):
    async def run():
        h=harness(tmp_path)
        if kind=='pay':h.facts['data']['lock']={'ref':'lock','amount_minor':87500,'valid':True,'effective_status':'held','price_basis':'frozen'}
        if kind in {'query','wait'}:
            h.facts['data'].update(payment={'status':'succeeded','amount_minor':87500},ticket_task={'status':'pending'})
        if kind=='end':h.facts['data']['ticket']={'status':'valid','trip_revision':1}
        v=candidate(h,kind,gap='evidence' if kind in {'unknown','clarify'} else None);configure(h,v)
        r=await post(app_for(monkeypatch,h),request());assert r.status_code==200
        if kind=='wait':assert '尚无有效票据' in r.json()['reply'] and '不等于' not in r.json()['reply'].splitlines()[0]
    asyncio.run(run())


def test_paid_is_not_completion(tmp_path,monkeypatch):
    async def run():
        h=harness(tmp_path);h.facts['data']['payment']={'status':'succeeded','amount_minor':87500}
        configure(h,candidate(h,'end'));r=await post(app_for(monkeypatch,h),request())
        assert r.status_code==503 and receipt(h)['reason']=='completion_unverified'
    asyncio.run(run())


@pytest.mark.parametrize('change',['identity','facts','release'])
def test_late_answer_never_crosses_identity_state_or_release(tmp_path,monkeypatch,change):
    async def run():
        h=harness(tmp_path);v=candidate(h)
        def after():
            if change=='identity':h.current=type(h.current)('other','other')
            if change=='facts':h.facts['data']['trip']['revision']=2
            if change=='release':h.releases.rollback()
        h.after=after;configure(h,v)
        r=await post(app_for(monkeypatch,h),request());assert r.status_code==409
        assert all(not s.history for s in h.sessions.values())
    asyncio.run(run())


@pytest.mark.parametrize('mode',['missing','mismatch','valid','denied'])
def test_navigation_requires_real_tool_and_keeps_ack_lifecycle(tmp_path,monkeypatch,mode):
    async def run():
        h=harness(tmp_path);v=candidate(h,'navigate')
        page='trips' if mode=='mismatch' else 'approvals' if mode=='denied' else 'profile'
        if mode=='denied':v['action']['params']['page']=page
        calls=[] if mode=='missing' else [{'id':'synthetic-tool','name':'navigate','arguments':{'page':page}}]
        configure(h,v,calls);r=await post(app_for(monkeypatch,h),request())
        if mode!='valid':assert r.status_code==503;return
        formal=r.json();assert formal['navigation_status']=='proposed'
        path=next(h.report_dir.glob('*.json'));assert 'browser_ack' not in json.loads(path.read_text(encoding='utf-8'))
        ack=Ack(session_id=formal['session_id'],nonce=formal['tool']['nonce'],success=True,observation=observation('profile'))
        # 这是本地合成ACK，不能记成V1-B真实浏览器证据。
        assert (await h.ack(ack,Request()))['navigation_status']=='succeeded'
        with pytest.raises(HTTPException):await h.ack(ack,Request())
    asyncio.run(run())


def test_confirmation_rejects_stale_state_even_with_old_context(tmp_path):
    h=harness(tmp_path);v=candidate(h)
    ctx=evidence_context(h.facts,h.releases.active()['knowledge_hash'])
    formal={'action_contract_version':EVIDENCE_CONTRACT_VERSION,'action_contract':v,'action_sha256':action_digest(v),'reply':render_evidence_contract(v)}
    h.facts['data']['trip']['revision']=2
    with pytest.raises(ValueError,match='evidence_stale'):
        confirm_evidence_delivery(formal,confirmed(formal),state=h.facts,context=ctx,allowed_pages=h.current.allowed_pages)


def test_new_release_can_rollback_without_changing_old_candidate(tmp_path):
    h=harness(tmp_path);new=deepcopy(h.releases.active());assert new['prompt_version']=='assistant-13'
    assert h.releases.rollback()=='builtin-v1'
    h.releases.publish(new['version']);assert h.releases.active()==new


def test_new_external_fields_cannot_use_legacy_approval(tmp_path):
    from services.guide.model import ModelUnavailable
    async def run():
        h=harness(tmp_path);h.external_model=True
        with pytest.raises(ModelUnavailable,match='v1_export_not_approved'):
            await h.chat(request(),Request())
        assert h.calls==[] and not h.report_dir.exists()
    asyncio.run(run())
