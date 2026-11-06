"""助手权限/状态/工具/发布反例；全为离线fake，无数据库及付费模型。"""
import asyncio
from decimal import Decimal
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from services.guide.assistant import (Assistant,Identity,Chat,Ack,Train,Observation,Explore,LearningToken,LearningCapture,
                                      is_admin,validate_reply)
from services.guide.releases import Releases,validate_generated
from services.guide.model import ModelResult,TokenUsage


def observation(page):
    return {'page':page,'title':{'profile':'付款资料','trips':'我的行程','welcome':'欢迎'}[page],
            'items':['公开功能说明'],'product_version':'v1'}


def knowledge():
    return {'entries':[{'page':p,'summary':'可查看公开功能','source_index':i,'quote':'公开功能说明'}
                       for i,p in enumerate(('profile','trips'))], 'unknowns':['未验证提交操作效果']}


class Request:
    async def is_disconnected(self):return False


class Harness(Assistant):
    def __init__(self,tmp_path,candidate):
        self.current=Identity('synthetic-session','synthetic-actor')
        self.facts={'source_kind':'observed','payment_profile_state':'unbound'}
        self.calls=[];self.after=None
        async def generate(value,deadline):
            self.calls.append(value)
            if self.after:self.after()
            return ModelResult(candidate,TokenUsage(100,50,150,0,100),'synthetic-receipt',Decimal('0.0006'),'synthetic-reservation')
        def factory(validator):
            async def wrapped(value,deadline):
                validator(value)
                return await generate(value,deadline)
            return SimpleNamespace(generate_experiment=wrapped)
        super().__init__(SimpleNamespace(workspace_id='test'),releases=Releases('test',tmp_path/'releases.json'),
            adapter_factory=factory,admin_path=tmp_path/'admin.json',report_dir=tmp_path/'runs')
    async def who(self,request,deadline):return self.current
    async def state(self,request,who,trip_id,deadline):return self.facts,json.dumps(self.facts,sort_keys=True)


def chat_body(session_id=None,page='trips'):
    return Chat(**({'session_id':session_id} if session_id else {}),message='帮我找到付款资料',page=page,observation=observation(page))


def tool_candidate(page='profile'):
    return {'content':'','tool_calls':[{'id':'synthetic-call','name':'navigate','arguments':{'page':page}}]}


def test_tool_requires_actual_ack_and_replay_fails(tmp_path):
    async def scenario():
        h=Harness(tmp_path,tool_candidate());r=await h.chat(chat_body(),Request())
        assert r['reply']=='正在尝试打开相关页面。'
        assert 'browser_ack' not in json.loads(next((tmp_path/'runs').glob('*.json')).read_text())
        body=Ack(session_id=r['session_id'],nonce=r['tool']['nonce'],success=True,observation=observation('profile'))
        done=await h.ack(body,Request());assert done['reply']=='页面已打开，可以继续咨询。'
        with pytest.raises(HTTPException):await h.ack(body,Request())
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['other_identity','bad_nonce','expired','failed_dom'])
def test_navigation_counterexamples(tmp_path,case):
    async def scenario():
        h=Harness(tmp_path,tool_candidate());r=await h.chat(chat_body(),Request())
        nonce=r['tool']['nonce']
        if case=='other_identity':h.current=Identity('other','other')
        if case=='bad_nonce':nonce='f'*32
        if case=='expired':h.sessions[r['session_id']].pending['expires']=0
        body=Ack(session_id=r['session_id'],nonce=nonce,success=case!='failed_dom',observation=observation('profile'))
        if case=='failed_dom':assert '未能打开' in (await h.ack(body,Request()))['reply']
        else:
            with pytest.raises(HTTPException):await h.ack(body,Request())
    asyncio.run(scenario())


def test_unauthorized_model_tool_rejected(tmp_path):
    async def scenario():
        h=Harness(tmp_path,tool_candidate('approvals'))
        with pytest.raises(HTTPException) as error:await h.chat(chat_body(),Request())
        assert error.value.status_code==403
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['identity','facts','release'])
def test_late_answer_revalidated(tmp_path,change):
    async def scenario():
        h=Harness(tmp_path,{'content':'请先查看资料。','tool_calls':[]})
        def after():
            if change=='identity':h.current=Identity('different','different')
            if change=='facts':h.facts={'source_kind':'observed','payment_profile_state':'bound'}
            if change=='release':
                c=h.releases.save_candidate(knowledge(),[observation('profile'),observation('trips')]);h.releases.publish(c['version'])
        h.after=after
        with pytest.raises(HTTPException) as error:await h.chat(chat_body(),Request())
        assert error.value.status_code==409
    asyncio.run(scenario())


def test_admin_is_independent_of_approver_and_chat_claim(tmp_path):
    settings=SimpleNamespace(workspace_id='test');p=tmp_path/'admin.json'
    who=Identity('session','synthetic-actor',('approver',))
    assert who.allowed_pages==['welcome','approvals']
    assert not is_admin(settings,who,p)
    p.write_text(json.dumps({'workspace':'test','actor_ids':['synthetic-actor']}))
    assert is_admin(settings,who,p)
    assert not is_admin(settings,Identity('s','other'),p)
    assert not is_admin(SimpleNamespace(workspace_id='demo'),who,p)


def test_unauthorized_training_zero_model_calls(tmp_path):
    async def scenario():
        h=Harness(tmp_path,{'content':'{}','tool_calls':[]})
        with pytest.raises(HTTPException):await h.train(Train(observations=[observation('profile'),observation('trips')]),Request())
        assert not h.calls
    asyncio.run(scenario())


def test_candidate_citations_publish_rollback_and_failed_publish(tmp_path):
    store=Releases('test',tmp_path/'releases.json');obs=[observation('profile'),observation('trips')]
    candidate=store.save_candidate(knowledge(),obs)
    assert store.active()['version']=='builtin-v1'
    store.publish(candidate['version']);assert Releases('test',store.path).active()['version']==candidate['version']
    before=store.path.read_bytes()
    with pytest.raises(ValueError):store.publish('kb-'+'0'*20)
    assert store.path.read_bytes()==before
    assert store.rollback()=='builtin-v1'
    invalid=knowledge();invalid['entries'][0]['quote']='不存在的依据'
    with pytest.raises(ValueError):validate_generated(invalid,obs)


@pytest.mark.parametrize('text',['已付款不等于出票。','如果已付款，请勿重复支付。','当前尚未出票。'])
def test_general_explanations_are_not_current_success(text):assert validate_reply(text,{})==text


@pytest.mark.parametrize('text',['你的行程已经成功出票。','您的行程已成功付款。','请支付123元。'])
def test_unsupported_current_claims_and_amounts(text):
    with pytest.raises(ValueError):validate_reply(text,{})


def admin(h):
    h.admin_path.write_text(json.dumps({'workspace':'test','actor_ids':[h.current.actor]}))


def test_bounded_learning_native_roundtrip_and_single_use(tmp_path):
    from services.guide.assistant import Explore
    from services.guide.experiment import validate_messages
    async def scenario():
        h=Harness(tmp_path,tool_candidate('trips'));admin(h)
        first=await h.explore(Explore(observation=observation('profile')),Request())
        ack=Ack(session_id=first['session_id'],nonce=first['tool']['nonce'],success=True,observation=observation('trips'))
        with pytest.raises(HTTPException):await h.ack(ack,Request())
        async def infer(payload,kind,version):
            validate_messages(payload['messages'])
            assert payload['messages'][-2]['role']=='tool'
            tool=json.loads(payload['messages'][-2]['content'])
            assert [o['page'] for o in tool['observations']]==['profile','trips']
            return SimpleNamespace(candidate={'content':json.dumps(knowledge()),'tool_calls':[]}),None
        h.infer=infer
        final=await h.explore_finish(ack,Request())
        assert not final['coverage_missing']
        assert h.releases.active()['version']=='builtin-v1'
        with pytest.raises(HTTPException):await h.explore_finish(ack,Request())
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['failed','wrong_page','expired','identity','state','released','admin_revoked'])
def test_learning_invalid_receipts_do_not_call_model(tmp_path,case):
    from services.guide.assistant import Explore
    async def scenario():
        h=Harness(tmp_path,tool_candidate('trips'));admin(h)
        first=await h.explore(Explore(observation=observation('profile')),Request())
        if case=='expired':h.sessions[first['session_id']].pending['expires']=0
        if case=='identity':h.current=Identity('other','other')
        if case=='state':h.facts={'payment_profile_state':'bound'}
        if case=='admin_revoked':h.admin_path.write_text('{}')
        if case=='released':
            c=h.releases.save_candidate(knowledge(),[observation('profile'),observation('trips')]);h.releases.publish(c['version'])
        ack=Ack(session_id=first['session_id'],nonce=first['tool']['nonce'],success=case!='failed',observation=observation('profile' if case=='wrong_page' else 'trips'))
        with pytest.raises(HTTPException):await h.explore_finish(ack,Request())
        assert len(h.calls)==1
    asyncio.run(scenario())


@pytest.mark.parametrize('destination',['profile','approvals','booking'])
def test_learning_rejects_same_or_nonpublic_target(tmp_path,destination):
    from services.guide.assistant import Explore
    async def scenario():
        h=Harness(tmp_path,tool_candidate(destination));admin(h)
        with pytest.raises(HTTPException):await h.explore(Explore(observation=observation('profile')),Request())
        assert h.releases.active()['version']=='builtin-v1'
    asyncio.run(scenario())


def test_admin_role_does_not_grant_learning_business_access(tmp_path):
    from services.guide.assistant import Explore
    async def scenario():
        h=Harness(tmp_path,tool_candidate());h.current=Identity('s','synthetic-actor',('approver',));admin(h)
        with pytest.raises(HTTPException):await h.explore(Explore(observation=observation('profile')),Request())
        assert not h.calls
    asyncio.run(scenario())


def test_publish_rejects_losing_existing_page_coverage(tmp_path):
    store=Releases('test',tmp_path/'releases.json')
    c=store.save_candidate(knowledge(),[observation('profile'),observation('trips')]);store.publish(c['version'])
    k=knowledge();k['entries'][1]['page']='welcome'
    smaller=store.save_candidate(k,[observation('profile'),observation('welcome')])
    with pytest.raises(ValueError,match='coverage_regression'):store.publish(smaller['version'])
    assert store.active()['version']==c['version']


def test_document_condition_changes_only_information_and_tools():
    from services.guide.assistant import document_only_payload,SYSTEM
    from services.guide.experiment import NAVIGATION_TOOL
    context={'current_state':{'payment':'succeeded'},'page_observation':observation('profile'),
             'allowed_pages':['profile'],'question':'我的预订完成了吗？','knowledge':knowledge(),'history':[],'current_page':'profile'}
    payload={'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(context)}],
             'tools':[NAVIGATION_TOOL],'max_tokens':512,'json_output':False}
    before=json.dumps(payload)
    baseline=document_only_payload(payload)
    assert json.dumps(payload)==before
    expected={k:v for k,v in context.items() if k not in {'current_state','page_observation','allowed_pages'}}
    assert json.loads(baseline['messages'][1]['content'])==expected
    assert baseline['messages'][0]==payload['messages'][0] and baseline['max_tokens']==512 and baseline['tools']==[]


def test_minor_units_never_become_display_amount_and_invalid_lock_unknown():
    from services.guide.assistant import display_state
    facts={'data':{'payment':{'amount_minor':110000,'currency':'DEMO','status':'succeeded'},
                   'lock':{'amount_minor':110000,'currency':'DEMO','valid':False}}}
    projected=display_state(facts)
    assert projected['data']['payment']['amount_display']=='1100.00 模拟元'
    assert projected['data']['lock']['amount_display'] is None
    assert 'amount_minor' not in projected['data']['payment']
    assert facts['data']['payment']['amount_minor']==110000
    assert validate_reply('已经支付1100.00 模拟元。',facts)
    for wrong in ['金额110000 DEMO','金额110000 模拟元','金额110001元']:
        with pytest.raises(ValueError,match='amount_unverified'):validate_reply(wrong,facts)


def test_old_prompt_bundle_stays_replayable_and_rollbackable(tmp_path):
    from services.guide.knowledge import fingerprint
    from services.guide.assistant import SYSTEM,SYSTEM_V1
    store=Releases('test',tmp_path/'releases.json')
    c=store.save_candidate(knowledge(),[observation('profile'),observation('trips')])
    state=store.read();old=dict(state['candidates'][c['version']])
    old['prompt_version']='assistant-1'
    old.pop('version');old.pop('fingerprint')
    old['version']='kb-'+fingerprint(old)[:20];old['fingerprint']=fingerprint(old)
    state['candidates'][old['version']]=old;state['active']=old['version'];store.write(state)
    assert store.active()['prompt_version']=='assistant-1'
    store.publish(c['version'])
    assert store.active()['prompt_version']=='assistant-4' and SYSTEM!=SYSTEM_V1
    store.rollback();assert store.active()['version']==old['version']


def test_legacy_training_also_enforces_business_permission(tmp_path):
    async def scenario():
        h=Harness(tmp_path,{'content':'{}','tool_calls':[]})
        h.current=Identity('s','synthetic-actor',('approver',));admin(h)
        with pytest.raises(HTTPException):await h.train(Train(observations=[observation('profile'),observation('trips')]),Request())
        assert not h.calls
    asyncio.run(scenario())


@pytest.mark.parametrize('requests,accounted,unresolved,expected',[
    (22,'0.02530056',0,'experiment_limit'),
    (12,'0.02530056',1,'usage_unresolved'),
    (22,'0.02530056',1,'usage_unresolved'),
    (12,'0.5',0,'experiment_limit')])
def test_budget_stop_remains_visible_after_price_date_expires(tmp_path,monkeypatch,requests,accounted,unresolved,expected):
    from services.guide import budget,assistant
    monkeypatch.setattr(budget.BudgetLedger,'summary',lambda self:{'physical_requests':requests,'accounted_cny':accounted,'unresolved_requests':unresolved})
    def expired():raise AssertionError('Exhausted or unresolved ledger must stop before daily price check')
    monkeypatch.setattr(assistant,'load_approval',expired)
    h=Harness(tmp_path,{'content':'unused','tool_calls':[]})
    status=h.status(h.current)
    assert status['ready'] is False and status['reason']==expected


@pytest.mark.parametrize('requests',[16,21])
@pytest.mark.parametrize('approval_current',[True,False])
def test_q14_status_checks_current_approval_with_remaining_requests(tmp_path,monkeypatch,requests,approval_current):
    from services.guide import budget,assistant
    from services.guide.experiment import approval_values
    from services.guide.model import ModelUnavailable,_today
    monkeypatch.setattr(assistant,'ROOT',tmp_path)
    (tmp_path/'.local').mkdir()
    (tmp_path/'.local/guide-model.json').write_text('{}')
    monkeypatch.setattr(budget.BudgetLedger,'summary',lambda self:{'physical_requests':requests,'accounted_cny':'0.02530056','unresolved_requests':0})
    checked=[]
    def approval():
        checked.append(True)
        if not approval_current:raise ModelUnavailable('experiment_not_approved')
        return approval_values(_today())
    monkeypatch.setattr(assistant,'load_approval',approval)
    h=Harness(tmp_path,{'content':'unused','tool_calls':[]})
    status=h.status(h.current)
    assert checked==[True]
    assert status['ready'] is approval_current
    assert status['reason']==(None if approval_current else 'experiment_not_approved')


# 17集中离线检查：合成模型只验证宿主边界，不计模型学习/真实API实验。
def semantic_observation(selected=False):
    return {**observation('trips'), 'semantics': {'schema':'page-semantics-1',
            'selected_trip':selected,
            'zones':[{'id':'booking.detail','present':selected,'visible':selected}],
            'controls':[{'id':'nav.booking','present':True,'visible':True,'enabled':selected}]}}


def conditional_knowledge():
    result=knowledge()
    result['entries'][1].update(source_index=2,condition={'selected_trip':True},
        visibility_change={'zone_id':'booking.detail','before_source_index':1})
    return result


def condition_sources():
    return [observation('profile'),semantic_observation(False),semantic_observation(True)]


def test_legacy_observation_does_not_gain_semantics_or_claim_absence():
    from services.guide.page_observation import observation_payload
    source=observation('trips')
    assert observation_payload(Observation(**source))==source
    current=Observation(**semantic_observation(False))
    assert not current.semantics.zones[0].present and not current.semantics.zones[0].visible
    empty={**source,'semantics':{'schema':'page-semantics-1','selected_trip':False,'zones':[],'controls':[]}}
    assert Observation(**empty).semantics.zones==[]  # 没有采集某元素，不能替换成present=false。


@pytest.mark.parametrize('case',['unregistered_zone','other_page_control','duplicate','invisible_absent',
                                'enabled_absent','enabled_hidden','value','identity','coerced_bool'])
def test_page_semantics_rejects_unregistered_and_private_fields(case):
    value=semantic_observation(True)
    if case=='unregistered_zone':value['semantics']['zones'][0]['id']='hidden.database'
    if case=='other_page_control':value['semantics']['controls'][0]['id']='booking.pay'
    if case=='duplicate':value['semantics']['zones']*=2
    if case=='invisible_absent':value['semantics']['zones'][0]['present']=False
    if case=='enabled_absent':value['semantics']['controls'][0].update(present=False,visible=False)
    if case=='enabled_hidden':value['semantics']['controls'][0]['visible']=False
    if case=='value':value['semantics']['controls'][0]['value']='private'
    if case=='identity':value['semantics']['role']='administrator'
    if case=='coerced_bool':value['semantics']['selected_trip']='true'
    with pytest.raises(ValueError):Observation(**value)


def test_unbound_three_observations_require_capture_session(tmp_path):
    async def scenario():
        h=Harness(tmp_path,{'content':json.dumps(conditional_knowledge()),'tool_calls':[]});admin(h)
        with pytest.raises(HTTPException) as error:
            await h.train(Train(observations=condition_sources()),Request())
        assert error.value.detail=='learning_session_required'
        assert not h.calls
        assert h.releases.active()['version']=='builtin-v1'
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['missing_semantics','wrong_condition','unknown_condition','wrong_before',
                                'before_visible','after_hidden','zone_uncollected','other_page','other_source','null_change','private_scope'])
def test_condition_knowledge_cannot_expand_or_invent_visibility(case):
    value=conditional_knowledge();sources=condition_sources()
    entry=value['entries'][1]
    if case=='missing_semantics':sources[2].pop('semantics')
    if case=='wrong_condition':entry['condition']['selected_trip']=False
    if case=='unknown_condition':entry['condition']['payment_eligible']=True
    if case=='wrong_before':entry['visibility_change']['before_source_index']=2
    if case=='before_visible':sources[1]['semantics']['zones'][0].update(present=True,visible=True)
    if case=='after_hidden':sources[2]['semantics']['zones'][0]['visible']=False
    if case=='zone_uncollected':sources[1]['semantics']['zones']=[]
    if case=='other_page':entry['visibility_change']['before_source_index']=0
    if case=='other_source':sources[2]['source_kind']='observed'
    if case=='null_change':entry['visibility_change']=None
    if case=='private_scope':sources[2]['disclosure_scope']='current-session'
    with pytest.raises(ValueError):validate_generated(value,sources)


def test_condition_coverage_prevents_release_regression_but_explicit_rollback_remains(tmp_path):
    store=Releases('test',tmp_path/'releases.json')
    old=store.save_candidate(knowledge(),[observation('profile'),observation('trips')]);store.publish(old['version'])
    conditioned=store.save_candidate(conditional_knowledge(),condition_sources());store.publish(conditioned['version'])
    before=store.path.read_bytes()
    with pytest.raises(ValueError,match='condition_coverage_regression'):store.publish(old['version'])
    assert store.path.read_bytes()==before
    assert store.rollback()==old['version']
    assert store.rollback()==conditioned['version']


def time_facts():
    return {'business_time':'2026-09-27T00:00:00+00:00','observed_at':'2026-10-12T13:21:49+00:00',
            'valid_until':'2026-10-12T13:22:19+00:00',
            'data':{'quote':{'valid':True,'effective_status':'active','amount_minor':110000,
                             'expires_at':'2026-09-27T00:05:00+00:00'},
                    'lock':{'valid':False,'effective_status':'consumed','amount_minor':110000},
                    'payment':{'status':'succeeded','amount_minor':110000},'ticket':None}}


@pytest.mark.parametrize('text',['报价已过期。','报价仍为 active 但属估算价（expires_at 已过）。'])
def test_authorized_valid_price_cannot_expire_from_real_capture_date(text):
    with pytest.raises(ValueError,match='business_time_conflict'):
        validate_reply(text,time_facts(),prompt_version='assistant-3')
    assert validate_reply(text,time_facts(),prompt_version='assistant-2')==text  # 历史回放保持旧行为。


def test_time_presentation_preserves_both_clocks_and_authorized_validity():
    from services.guide.assistant import display_state,prompt_system,SYSTEM,SYSTEM_V1,SYSTEM_V3
    facts=time_facts();old=json.dumps(facts)
    projected=display_state(facts,'assistant-3')
    assert projected['business_time']==facts['business_time'] and projected['observed_at']==facts['observed_at']
    assert projected['data']['quote']['valid'] is True
    assert projected['time_contract']['business_clock']=='business_time'
    assert 'time_contract' not in display_state(facts,'assistant-2') and json.dumps(facts)==old
    assert prompt_system('assistant-1')==SYSTEM_V1 and prompt_system('assistant-2')==SYSTEM
    assert prompt_system('assistant-3')==SYSTEM_V3 and SYSTEM_V3!=SYSTEM
    good='报价仍然有效；锁价已消耗；已支付1100.00模拟元，但尚未出票。'
    assert validate_reply(good,facts,prompt_version='assistant-3')==good


def test_other_price_expiry_and_unknown_validity_are_separate():
    facts=time_facts();facts['data']['lock']['effective_status']='expired'
    text='报价仍然有效，锁价已经过期。'
    assert validate_reply(text,facts,prompt_version='assistant-3')==text
    with pytest.raises(ValueError,match='price_validity_unverified'):
        validate_reply('报价已过期。',{},prompt_version='assistant-3')
    with pytest.raises(ValueError,match='price_validity_conflict'):
        validate_reply('锁价当前有效。',facts,prompt_version='assistant-3')


def test_round16_parenthesized_demo_amount_fails_only_new_guard():
    text='金额 110000（币种 DEMO）'
    with pytest.raises(ValueError,match='amount_unverified'):
        validate_reply(text,time_facts(),prompt_version='assistant-3')
    assert validate_reply(text,time_facts(),prompt_version='assistant-2')==text


@pytest.mark.parametrize('observation_value',[None,observation('trips'),semantic_observation(False)])
def test_missing_or_hidden_element_is_not_page_capability_evidence(observation_value):
    with pytest.raises(ValueError,match='page_capability_unverified'):
        validate_reply('此页只说明流程，不能直接查进度。',{},observation_value,prompt_version='assistant-3')
    text='没有采集足够页面证据，不能确认当前页的进度入口。'
    assert validate_reply(text,{},observation_value,prompt_version='assistant-3')==text


def test_current_visibility_claim_needs_registered_visible_zone():
    text='当前页可见预订区域。'
    with pytest.raises(ValueError,match='page_visibility_unverified'):
        validate_reply(text,{},semantic_observation(False),prompt_version='assistant-3')
    assert validate_reply(text,{},semantic_observation(True),prompt_version='assistant-3')==text


@pytest.mark.parametrize('text',['报价将于五分钟后到期。','报价有效期到期前仍可查看。',
                                '锁价尚未过期但已消耗。'])
def test_future_expiry_and_consumed_price_are_not_current_expiry_claims(text):
    assert validate_reply(text,time_facts(),prompt_version='assistant-3')==text


@pytest.mark.parametrize('text',[
    '锁价已消耗，并非报价过期。',
    '现实采集时间较晚，不能据此判定报价过期；报价仍然有效。',
    '不能判断报价已过期。',
    '无法据此确认锁价到期。',
])
def test_round18_expiry_denial_and_uncertainty_are_not_positive_claims(text):
    assert validate_reply(text,time_facts(),prompt_version='assistant-3')==text


@pytest.mark.parametrize('text',[
    '不能据此判定报价过期但报价已过期。',
    '不能判断报价过期但报价已经过期。',
    '并非报价过期但报价现在过期。',
    '报价未过期但已过期。',
])
def test_expiry_denial_does_not_exempt_a_later_positive_claim(text):
    with pytest.raises(ValueError,match='business_time_conflict'):
        validate_reply(text,time_facts(),prompt_version='assistant-3')


def test_direct_denial_of_known_expiry_still_conflicts():
    facts=time_facts();facts['data']['quote'].update(valid=False,effective_status='expired')
    with pytest.raises(ValueError,match='price_validity_conflict'):
        validate_reply('并非报价过期。',facts,prompt_version='assistant-3')
    text='不能据此判定报价过期。'
    assert validate_reply(text,facts,prompt_version='assistant-3')==text


def learning_public():
    return {**observation('profile'),'semantics':{'schema':'page-semantics-1',
            'selected_trip':False,'zones':[],'controls':[]}}


def learning_token(progress):
    return LearningToken(session_id=progress['session_id'],nonce=progress['nonce'])


async def collect_learning(h):
    first=await h.learning_start(Explore(observation=learning_public()),Request())
    before=await h.learning_capture(LearningCapture(**learning_token(first).model_dump(),
        observation=semantic_observation(False)),Request())
    ready=await h.learning_capture(LearningCapture(**learning_token(before).model_dump(),
        trip_id='00000000-0000-0000-0000-000000000019',observation=semantic_observation(True)),Request())
    return first,before,ready


def test_tool_explanation_is_delivered_and_receipt_separate(tmp_path):
    async def scenario():
        candidate=tool_candidate();candidate['content']='已支付1100.00模拟元，锁价已消耗，并非报价过期。'
        h=Harness(tmp_path,candidate);h.facts=time_facts()
        reply=await h.chat(chat_body(),Request())
        assert reply['reply']==candidate['content'] and reply['navigation_status']=='proposed'
        session=h.sessions[reply['session_id']]
        assert session.history[-1]['text']==candidate['content']
        ack=Ack(session_id=reply['session_id'],nonce=reply['tool']['nonce'],success=False,observation=observation('trips'))
        result=await h.ack(ack,Request())
        assert result['navigation_status']=='failed' and '未能打开' in result['reply']
        assert any(item.get('text')==candidate['content'] for item in session.history)
    asyncio.run(scenario())


@pytest.mark.parametrize('text,reason',[
    ('金额110000模拟元。','amount_unverified'),
    ('报价已经过期。','business_time_conflict'),
    ('你的行程已经成功出票。','completion_unverified'),
    ('当前页可见预订区域。','page_visibility_unverified'),
    ('已为您打开付款资料页面。','navigation_unconfirmed')])
def test_tool_text_has_same_fact_checks_before_proposal(tmp_path,text,reason):
    async def scenario():
        candidate=tool_candidate();candidate['content']=text
        h=Harness(tmp_path,candidate);h.facts=time_facts()
        with pytest.raises(ValueError,match=reason):await h.chat(chat_body(),Request())
        assert all(session.pending is None for session in h.sessions.values())
    asyncio.run(scenario())


def test_navigation_receipt_rejects_changed_business_state(tmp_path):
    async def scenario():
        h=Harness(tmp_path,tool_candidate());reply=await h.chat(chat_body(),Request())
        h.facts={'payment_profile_state':'bound'}
        ack=Ack(session_id=reply['session_id'],nonce=reply['tool']['nonce'],success=True,observation=observation('profile'))
        with pytest.raises(HTTPException) as error:await h.ack(ack,Request())
        assert error.value.detail=='state_changed'
    asyncio.run(scenario())


def test_manual_learning_binds_three_sources_and_one_call(tmp_path):
    async def scenario():
        h=Harness(tmp_path,{'content':json.dumps(conditional_knowledge()),'tool_calls':[]});admin(h)
        first,before,ready=await collect_learning(h)
        assert [p['stage'] for p in (first,before,ready)]==['before_selection','after_selection','ready']
        assert len({p['nonce'] for p in (first,before,ready)})==3 and not h.calls
        result=await h.learning_build(learning_token(ready),Request())
        assert len(h.calls)==1 and result['review']['coverage']['conditions']
        sources=json.loads(h.calls[0]['messages'][-1]['content'])
        assert [o['page'] for o in sources]==['profile','trips','trips']
        assert [o['semantics']['selected_trip'] for o in sources]==[False,False,True]
        raw=json.dumps(h.calls[0])
        assert ready['nonce'] not in raw and h.current.actor not in raw and '000000000019' not in raw
        assert h.releases.active()['version']=='builtin-v1'
        assert h.releases.read()['candidates'][result['candidate_version']]['prompt_version']=='assistant-4'
        with pytest.raises(HTTPException):await h.learning_build(learning_token(ready),Request())
        assert len(h.calls)==1
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['identity','admin_revoked','expired','nonce','state','release','cancel','incomplete'])
def test_invalid_manual_learning_never_calls_model(tmp_path,case):
    async def scenario():
        h=Harness(tmp_path,{'content':json.dumps(conditional_knowledge()),'tool_calls':[]});admin(h)
        first,before,ready=await collect_learning(h)
        token=learning_token(ready)
        if case=='identity':h.current=Identity('other','other');admin(h)
        if case=='admin_revoked':h.admin_path.write_text('{}')
        if case=='expired':h.sessions[ready['session_id']].training['expires']=0
        if case=='nonce':token.nonce='f'*32
        if case=='state':h.facts={'payment_profile_state':'changed'}
        if case=='release':
            release=h.releases.save_candidate(knowledge(),[observation('profile'),observation('trips')])
            h.releases.publish(release['version'])
        if case=='cancel':assert (await h.learning_cancel(token,Request()))['cancelled']
        if case=='incomplete':h.sessions[ready['session_id']].training['stage']='after_selection'
        with pytest.raises(HTTPException):await h.learning_build(token,Request())
        assert not h.calls
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['wrong_page','missing_semantics','selected_before','missing_trip','replay','wrong_version','unauthorized_trip'])
def test_capture_rejects_unpaired_or_unowned_sources(tmp_path,case):
    async def scenario():
        h=Harness(tmp_path,{'content':'unused','tool_calls':[]});admin(h)
        first=await h.learning_start(Explore(observation=learning_public()),Request())
        value=semantic_observation(False)
        if case=='wrong_page':value=learning_public()
        if case=='missing_semantics':value=observation('trips')
        if case=='selected_before':value=semantic_observation(True)
        token=learning_token(first)
        if case in {'missing_trip','replay','wrong_version','unauthorized_trip'}:
            before=await h.learning_capture(LearningCapture(**token.model_dump(),observation=value),Request())
            if case!='replay':token=learning_token(before)
            value=semantic_observation(True)
        if case=='wrong_version':h.sessions[first['session_id']].training['product_version']='v2'
        if case=='unauthorized_trip':
            original=h.state
            async def authorized(request,who,trip_id,deadline):
                if trip_id is not None:raise HTTPException(404,'not_visible')
                return await original(request,who,trip_id,deadline)
            h.state=authorized
        args={'trip_id':'00000000-0000-0000-0000-000000000019'} if case in {'wrong_version','unauthorized_trip'} else {}
        with pytest.raises(HTTPException):
            await h.learning_capture(LearningCapture(**token.model_dump(),observation=value,**args),Request())
        assert not h.calls
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['identity','state','expired','release','cancelled','disconnected'])
def test_manual_learning_discards_late_results(tmp_path,change):
    async def scenario():
        h=Harness(tmp_path,{'content':json.dumps(conditional_knowledge()),'tool_calls':[]});admin(h)
        _,_,ready=await collect_learning(h)
        class Connection(Request):
            closed=False
            async def is_disconnected(self):return self.closed
        request=Connection()
        def after():
            if change=='identity':h.current=Identity('other','other');admin(h)
            if change=='state':h.facts={'payment_profile_state':'changed'}
            if change=='expired':h.sessions[ready['session_id']].training['expires']=0
            if change=='release':
                release=h.releases.save_candidate(knowledge(),[observation('profile'),observation('trips')]);h.releases.publish(release['version'])
            if change=='cancelled':h.sessions.pop(ready['session_id'])
            if change=='disconnected':request.closed=True
        h.after=after
        with pytest.raises(HTTPException):await h.learning_build(learning_token(ready),request)
        assert len(h.calls)==1 and ready['session_id'] not in h.sessions
        assert not any(r['knowledge']==conditional_knowledge() for r in h.releases.read()['candidates'].values())
    asyncio.run(scenario())


def test_manual_cancel_stops_inflight_build_without_candidate(tmp_path):
    async def scenario():
        h=Harness(tmp_path,{'content':'unused','tool_calls':[]});admin(h)
        _,_,ready=await collect_learning(h);started=asyncio.Event()
        async def wait_forever(payload,kind,version):
            started.set();await asyncio.Future()
        h.infer=wait_forever
        task=asyncio.create_task(h.learning_build(learning_token(ready),Request()))
        await started.wait()
        assert (await h.learning_cancel(learning_token(ready),Request()))['cancelled']
        with pytest.raises(asyncio.CancelledError):await task
        assert not h.busy and not h.releases.read()['candidates']
    asyncio.run(scenario())


def test_review_is_admin_only_and_does_not_publish(tmp_path):
    async def scenario():
        h=Harness(tmp_path,{'content':'unused','tool_calls':[]})
        release=h.releases.save_candidate(conditional_knowledge(),condition_sources())
        with pytest.raises(HTTPException):await h.review(release['version'],Request())
        admin(h);review=await h.review(release['version'],Request())
        assert review['publishable'] and review['sources'][2]['selected_trip'] is True
        assert h.releases.active()['version']=='builtin-v1' and not h.calls
    asyncio.run(scenario())


def test_prompt4_separates_published_conditions_from_current_observation(tmp_path):
    async def scenario():
        h=Harness(tmp_path,{'content':'历史学习显示，选择行程后出现预订区；本次尚未选择行程，当前业务资格未知。','tool_calls':[]})
        release=h.releases.save_candidate(conditional_knowledge(),condition_sources());h.releases.publish(release['version'])
        body=Chat(message='选择前后有什么变化',page='trips',observation=semantic_observation(False))
        await h.chat(body,Request())
        context=json.loads(h.calls[0]['messages'][-1]['content'])
        assert context['published_learning']['knowledge']['entries'][1]['condition']['selected_trip'] is True
        assert context['published_learning']['conditional_structure']=='matched'
        assert context['current_observation']['semantics']['selected_trip'] is False
        assert 'knowledge' not in context and 'page_observation' not in context
        from services.guide.assistant import document_only_payload,prompt_system,SYSTEM_V3
        assert prompt_system('assistant-3')==SYSTEM_V3
        baseline=json.loads(document_only_payload(h.calls[0])['messages'][-1]['content'])
        assert 'current_observation' not in baseline and 'current_state' not in baseline
        assert baseline['published_learning']==context['published_learning']
    asyncio.run(scenario())


@pytest.mark.parametrize('kind,actual_prompt',[
    ('chat','assistant-3'),('knowledge_build','knowledge-build-4'),
    ('knowledge_selection_build','knowledge-build-4'),('knowledge_after_navigation','knowledge-build-4'),
    ('knowledge_choose_page','knowledge-choose-page-1')])
def test_inference_report_distinguishes_release_from_actual_prompt(tmp_path,kind,actual_prompt):
    async def scenario():
        h=Harness(tmp_path,{'content':'离线测试回答','tool_calls':[]})
        _,path=await h.infer({'messages':[],'tools':[],'max_tokens':512,'json_output':False},kind,'builtin-v1')
        report=json.loads(path.read_text(encoding='utf-8'))
        assert report['prompt_version']=='assistant-3' and report['inference_prompt']==actual_prompt
        assert report['release_version']=='builtin-v1'
    asyncio.run(scenario())


@pytest.mark.parametrize('with_tool',[False,True])
@pytest.mark.parametrize('text',[
    '下面帮你打开“我的行程”查看进度（提出此操作不代表已打开，需浏览器确认）：',
    '这并非已经打开页面，只是导航提案。',
    '尚未成功跳转，需要等待浏览器确认。',
    '没有成功打开页面，当前仍需浏览器确认。',
    '不能据此确认已打开。',
    '提出导航不表示已切换到目标页面。'])
def test_navigation_denial_delivers_same_explanation_with_or_without_tool(tmp_path,with_tool,text):
    async def scenario():
        candidate=tool_candidate() if with_tool else {'tool_calls':[]}
        candidate['content']=text
        h=Harness(tmp_path,candidate)
        result=await h.chat(chat_body(),Request())
        assert result['reply']==text
        assert result.get('navigation_status')==('proposed' if with_tool else None)
        assert bool(result.get('tool')) is with_tool
    asyncio.run(scenario())


@pytest.mark.parametrize('text',[
    '已打开页面。','已经打开页面。','成功跳转。','已经为您打开付款资料。',
    '已切换到我的行程。','成功导航到目标页面。',
    '提出此操作不代表已打开，但已打开页面。',
    '并非已经打开，但已经成功跳转。',
    '没有成功打开，但已为您打开目标页面。',
    '不能确认已打开但成功跳转到目标页面。'])
def test_navigation_denial_never_exempts_positive_proposal_claim(tmp_path,text):
    async def scenario():
        candidate=tool_candidate();candidate['content']=text
        h=Harness(tmp_path,candidate)
        with pytest.raises(ValueError,match='navigation_unconfirmed'):
            await h.chat(chat_body(),Request())
        assert all(session.pending is None for session in h.sessions.values())
    asyncio.run(scenario())
