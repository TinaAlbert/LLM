"""P22具体双目标咨询预览；只有离线模式，不批准额度或外发。"""
import asyncio
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import round20_sandbox as sandbox
from scripts.round21_explore import configuration,ordinary_session,write,sql_evidence
from scripts.d3_evaluate import NoModelTraffic,business_snapshot
from services.business.manage import reset_test_database
from services.guide.assistant import corporate_export_scope
from services.guide.experiment import MAX_INPUT_BYTES,input_size_bytes
from services.guide.model import ModelResult,TokenUsage
from services.guide.releases import Releases
OUT=ROOT/'.local/d3-22'
QUESTION_INITIAL='我想为这两个行程都拿到票，怎样安排才能少花钱？请区分预估和已执行结果，在当前页回答，不导航。'
QUESTION_CHANGED='我先锁定了第二个行程。现在券属于谁，还能调整顺序吗？请核对占用与付款状态，在当前页回答，不导航。'
QUESTIONS=(QUESTION_INITIAL,QUESTION_CHANGED)

def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def configure():
    from scripts.round22_explore import configure_server
    app,settings=configure_server()
    path=OUT/'consult-releases.json'
    if not path.exists(): shutil.copyfile(ROOT/'.local/assistant-releases-test.json',path)
    releases=Releases('test',path); old=releases.active()
    candidate=releases.save_candidate(old['knowledge'],old['observations'],prompt_version='assistant-6')
    releases.publish(candidate['version'])
    assistant=app.state.assistant; assistant.releases=releases; assistant.external_model=False
    assistant.report_dir=OUT/'consult-offline-runs'
    old_status=assistant.status
    assistant.status=lambda who:{**old_status(who),'ready':True,'reason':None}
    def factory(validator):
        async def generate(payload,deadline):
            validator(payload)
            context=json.loads(payload['messages'][-1]['content'])
            assert context['question'] in QUESTIONS
            size=input_size_bytes(payload)
            assert size<=MAX_INPUT_BYTES,'mandatory_input_too_large'
            index=QUESTIONS.index(context['question'])
            save(f'consult-preview-{index+1}.json',{'approved':False,
                'destination':'https://api.deepseek.com/chat/completions','scope_hash':corporate_export_scope(payload),
                'input_bytes':size,'max_tokens':512,'payload':payload,
                'assistant_source_sha256':hashlib.sha256((ROOT/'services/guide/assistant.py').read_bytes()).hexdigest()})
            reply=('两个明确委托的行程共用同一张券，预报价中的优惠只是估算，并未确定最终归属。'
                   '请比较当前基础金额和优惠额度后决定锁价顺序，再核对另一行程的最终金额。'
                   '未执行分支仍需核对报价、库存、期限和资格；当前尚未付款或取得有效票据。') if index==0 else (
                   '共享券当前由trip_2锁价占用，预报价只是估算，另一行程需在后续锁价核对最终金额。'
                   '如要重排，只能在未支付且业务允许取消时取消原锁价，再核对券、库存、期限和资格；'
                   '已消耗券不能通过取消恢复。当前两项目标尚未完成。')
            return ModelResult({'content':reply,'tool_calls':[]},TokenUsage(100,40,140,0,100),
                               'offline-only',Decimal('0'),'offline-only')
        return SimpleNamespace(generate_experiment=generate)
    assistant.adapter_factory=factory
    return app,settings

def prepare():
    import httpx
    config,settings=configuration()
    with sandbox.runner_lock(),httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=35) as client:
        # 先证实HTTP会话真实落在55433，再允许合成test重置。
        with ordinary_session(client,settings): pass
        reset_test_database(config['test_database_url'],confirmation='llm_business_test',fixture='two-trips-one-coupon')
        with ordinary_session(client,settings) as headers:
            write(client,headers,'/payment-profile',{'source_ref':'demo_personal_alice','expected_revision':0},'PUT')
            ids=[write(client,headers,'/trips',{'flight_id':'DEMO-'+size,'travel_date':'2026-10-01','passengers':1})['id'] for size in ('LARGE','SMALL')]
            quotes=[write(client,headers,f'/trips/{ref}/quotes',{'expected_revision':1}) for ref in ids]
            session=None; checks=[]
            for index,question in enumerate(QUESTIONS):
                if index==1: write(client,headers,f"/quotes/{quotes[1]['id']}/lock")
                before=business_snapshot(settings)
                body={'message':question,'page':'trips','trip_id':ids[0],'delegated_trip_ids':ids,
                    'observation':{'page':'trips','title':'我的行程','items':['暂估报价不会占用优惠券，最终金额以成功锁价为准。'],'product_version':'v1'}}
                if session: body['session_id']=session
                response=client.post('/assistant/chat',headers=headers,json=body)
                assert response.status_code==200,response.status_code
                session=response.json()['session_id']
                after=business_snapshot(settings); assert before['sha256']==after['sha256']
                save(f'consult-sample-{index+1}.json',{'question':question,'request':body,
                    'reply':response.json(),'business_before':before,'business_after':after,
                    'sql':sql_evidence(settings),'model':'offline_substitute'})
                checks.append({'sample':index+1,'http':200,'business_unchanged':True})
        save('consult-preparation.json',{'checks':checks,'models':0,'external_approved':False})
    print('两份具体咨询预览已完成真实HTTP/SQL核对，模型0、未批准外发。')

if __name__=='__main__':
    if sys.argv[1:]==['prepare']: prepare()
    elif sys.argv[1:]==['server']:
        import uvicorn
        app,settings=configure(); guard=NoModelTraffic()
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8000,access_log=False,proxy_headers=False))
        async def serve():
            task=asyncio.create_task(server.serve())
            while not task.done():
                if (OUT/'consult-server.stop').exists(): server.should_exit=True
                await asyncio.sleep(.2)
            await task
        try:
            assert not (OUT/'consult-server.stop').exists()
            with guard.installed(): asyncio.run(serve())
        finally: save('consult-transport-guard.json',{'model_send_attempts':guard.send_attempts,'budget_reserve_attempts':guard.reserve_attempts})
    else: raise SystemExit('仅server或prepare；没有真实模型模式。')
