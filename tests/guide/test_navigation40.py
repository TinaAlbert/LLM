"""P40导航生成版本与真实格式工具解析/交付；零模型/PG/实际浏览器。"""
import asyncio
from copy import deepcopy
import hashlib,json
import pytest
from fastapi import HTTPException
from scripts import round29_task as task
from scripts.check_round31 import restore
from services.guide.action_contract import ACTION_CONTRACT_VERSION,action_digest,render_action_contract,validate_action_contract
from services.guide.assistant import Ack,Chat,SYSTEM_V11,SYSTEM_V12,validate_reply
from services.guide.experiment import parse_response
from services.guide.fair_runtime import fair_input
from services.guide.model import ModelUnavailable
from test_action_delivery31 import harness
from test_assistant import Request,knowledge,observation
from test_current_action35 import saved
from test_generation38 import request_for
from test_reply_receipts30 import app_for,post,receipt
from test_round31_entry import confirmation
from test_shared_prompt21 import body


def neutral():
    return {'v':1,'action':{'kind':'navigate','target':'task','entry':'navigate','params':{'page':'profile'}},
        'reason':'当前需要公开资料。','conditions':'等待浏览器回执。','check':'核对本步页面回执。'}


def wire(value, *, page='profile', calls=1, content='value'):
    raw = json.dumps(value,ensure_ascii=False,separators=(',',':')) if content=='value' else content
    message={'role':'assistant','content':raw}
    if calls:
        message['tool_calls']=[{'id':'synthetic_nav_'+str(i),'type':'function',
            'function':{'name':'navigate','arguments':json.dumps({'page':page})}} for i in range(calls)]
    return {'choices':[{'index':0,'finish_reason':'tool_calls' if calls else 'stop','message':message}]}


def h12(tmp_path, parsed):
    value=json.loads(parsed['content']) if parsed['content'] else neutral()
    h=harness(tmp_path,value,parsed['tool_calls'],raw=parsed['content'])
    release=h.releases.save_candidate(knowledge(),[observation('profile'),observation('trips')],prompt_version='assistant-12')
    h.releases.publish(release['version']);return h


def test_q30_complete_original_still_rejected_without_tool_repair(tmp_path,monkeypatch):
    async def run():
        f=saved('q30-navigation-tool.json');raw=f['candidate']['content']
        assert hashlib.sha256(raw.encode()).hexdigest()==f['candidate_content_sha256']=='098a1c37a643e30dd0fccc30ff362ff17ce8e3a23a3e400ef2f1a3279ba8cd70'
        state=restore(f['current_state'])
        with pytest.raises(ValueError,match='action_tool_mismatch'):
            validate_action_contract(raw,state,f['allowed_pages'],f['candidate']['tool_calls'])
        h=h12(tmp_path,f['candidate']);h.facts=state
        response=await post(app_for(monkeypatch,h),request_for(f))
        assert response.status_code==503 and receipt(h)['reason']=='action_tool_mismatch'
        assert all(s.pending is None and not s.history for s in h.sessions.values())
        report=json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert report['candidate']==f['candidate'] and report['prompt_version']=='assistant-12' and 'browser_ack' not in report
    asyncio.run(run())


@pytest.mark.parametrize('current_page',['trips','profile'])
def test_real_format_navigation_full_parse_delivery_confirmation_and_synthetic_ack(tmp_path,monkeypatch,current_page):
    async def run():
        value=neutral();parsed=parse_response(wire(value),True,False)
        assert json.loads(parsed['content'])==value and parsed['tool_calls']==[{'id':'synthetic_nav_0','name':'navigate','arguments':{'page':'profile'}}]
        h=h12(tmp_path,parsed)
        request=Chat(**{**body().model_dump(),'page':current_page,'observation':observation(current_page)})
        response=await post(app_for(monkeypatch,h),request)
        assert response.status_code==200
        formal=response.json();rendered=render_action_contract(value)
        assert formal['reply']==rendered and formal['action_contract']==value
        assert formal['action_sha256']==action_digest(value) and formal['action_contract_version']==ACTION_CONTRACT_VERSION=='single-action-8'
        assert formal['navigation_status']=='proposed' and formal['tool']['arguments']=={'page':'profile'}
        assert validate_reply(rendered,h.facts,prompt_version='assistant-12')==rendered
        confirmed=confirmation(formal)
        with pytest.raises(RuntimeError,match='actual_navigation_receipt_required'):
            task.validate_confirmation(confirmed,formal,task_ref='p31-test',turn=1)
        confirmed['navigation_receipt']={'source_kind':'synthetic_unit_fixture_not_browser'}
        assert task.validate_confirmation(confirmed,formal,task_ref='p31-test',turn=1)==confirmed
        for key in ('quote','action_sha256','action_contract_version','formal_reply_sha256'):
            changed=deepcopy(confirmed);changed[key]+='changed'
            with pytest.raises(RuntimeError):task.validate_confirmation(changed,formal,task_ref='p31-test',turn=1)
        source=deepcopy(h.calls[0]);assert source['messages'][0]['content']==SYSTEM_V12 and SYSTEM_V12.startswith(SYSTEM_V11)
        b1=fair_input(source,'B1',prompt_version='assistant-12');b0=fair_input(source,'B0',prompt_version='assistant-12')
        full=json.loads(b1['messages'][1]['content']);reduced=json.loads(b0['messages'][1]['content'])
        assert b1==source and source==h.calls[0] and b0['messages'][0]==b1['messages'][0]
        assert full['current_page']==reduced['current_page']==current_page
        assert reduced=={k:v for k,v in full.items() if k not in {'current_state','current_observation'}}
        assert {k:v for k,v in b0.items() if k!='messages'}=={k:v for k,v in b1.items() if k!='messages'}
        report_path=next(h.report_dir.glob('*.json'));report=json.loads(report_path.read_text(encoding='utf-8'))
        assert report['candidate']==parsed and 'browser_ack' not in report
        # 只测本地ACK路径，未打开真实浏览器，不能当真实任务回执。
        done=await h.ack(Ack(session_id=formal['session_id'],nonce=formal['tool']['nonce'],success=True,observation=observation('profile')),Request())
        assert done['navigation_status']=='succeeded'
        with pytest.raises(HTTPException):
            await h.ack(Ack(session_id=formal['session_id'],nonce=formal['tool']['nonce'],success=True,observation=observation('profile')),Request())
    asyncio.run(run())


@pytest.mark.parametrize('case,expected',[
    ('missing','action_tool_mismatch'),('wrong_page','action_tool_mismatch'),
    ('duplicate','tool_call_invalid'),('non_nav','action_tool_mismatch'),
    ('tool_only','action_format_invalid'),
])
def test_tool_shape_never_bypasses_complete_action_or_matching_page(tmp_path,monkeypatch,case,expected):
    async def run():
        value=neutral();options={}
        if case=='missing':options['calls']=0
        if case=='wrong_page':options['page']='trips'
        if case=='duplicate':options['calls']=2
        if case=='non_nav':value['action']={'kind':'unknown','target':'task','entry':'task','params':{}}
        if case=='tool_only':options['content']=None
        response=wire(value,**options)
        if case=='duplicate':
            with pytest.raises(ModelUnavailable,match=expected):parse_response(response,True,False)
            return
        parsed=parse_response(response,True,False);h=h12(tmp_path,parsed)
        result=await post(app_for(monkeypatch,h),body())
        assert result.status_code==503 and receipt(h)['reason']==expected
        assert all(s.pending is None and not s.history for s in h.sessions.values())
    asyncio.run(run())
