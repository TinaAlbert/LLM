"""Q16明确批准的两份双目标咨询；正式Assistant/同账本，首失败即停。"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.round21_explore import configuration,ordinary_session,write,sql_evidence
from scripts.round22_consult import QUESTIONS
from scripts.d3_evaluate import business_snapshot
from services.business.app import create_app
from services.business.manage import reset_test_database
from services.guide.assistant import corporate_export_scope,authorize_shared_export
from services.guide.budget import BudgetLedger
from services.guide.experiment import input_size_bytes,MAX_INPUT_BYTES,load_approval
from services.guide.knowledge import fingerprint
from services.guide.model import DeepSeekModelAdapter
from services.guide.releases import Releases
OUT=ROOT/'.local/d3-22/q16'
PREV=ROOT/'.local/d3-22'
STOP=OUT/'server.stop'

def read(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def save(name,value):
    with (OUT/name).open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,default=str)
def ledger(): return BudgetLedger(ROOT/'.local/guide-usage.json')
def scopes():
    decision=read(OUT/'decision.json')
    assert decision['approved'] is True and decision['source_kind']=='explicit_user_reply'
    previews=[read(PREV/f'consult-preview-{n}.json') for n in (1,2)]
    values=[p['scope_hash'] for p in previews]
    assert decision['scope_hashes']==values
    assert [p['payload']['max_tokens'] for p in previews]==[512,512]
    return values

def init():
    values=scopes(); load_approval()
    release_bytes=(PREV/'consult-releases.json').read_bytes()
    if (OUT/'releases.json').exists():
        # 仅允许本地初始化中断后复用逐字相同副本，不能覆盖或重复真实尝试。
        assert not (OUT/'attempt-1.json').exists()
        assert (OUT/'releases.json').read_bytes()==release_bytes
    else:
        with (OUT/'releases.json').open('xb') as f:f.write(release_bytes)
    assert Releases('test',OUT/'releases.json').active()['prompt_version']=='assistant-6'
    ledger().create_q16_grant(tuple(values))
    save('initialization.json',{'scopes':values,'source':'Q16_explicit_user_reply','original_requests':22,
         'ordinary_ceiling':22,'scoped_ceiling':24,'usage_ledger':'original_continuous'})
    print('Q16精确两hash许可已创建，原22条未改，模型0。')

def configure():
    from scripts import round20_sandbox as sandbox
    config,settings=configuration(); values=scopes();load_approval()
    assert not STOP.exists() and not (OUT/'attempt-1.json').exists(),'Q16_no_restart_or_retry'
    ledger().check_q16_scope(values[0])
    app=create_app(settings);assistant=app.state.assistant
    assistant.releases=Releases('test',OUT/'releases.json')
    assistant.report_dir=OUT/'live-runs'
    assistant.shared_export_approval_path=OUT/'shared-export-approval.json'
    assistant.adapter_factory=lambda validator:DeepSeekModelAdapter(input_validator=validator,
         q16_grant_path=ledger().q16_path)
    # create_app默认正式模型标志保持true，原外发守卫仍运行。
    assert assistant.external_model is True
    original=assistant.infer
    async def one_shot(payload,kind,version):
        assert kind=='chat' and payload['max_tokens']==512 and not payload['json_output']
        value=corporate_export_scope(payload);assert value in values,'Q16_scope_changed'
        index=values.index(value)+1
        assert input_size_bytes(payload)<=MAX_INPUT_BYTES
        authorize_shared_export(payload,OUT/'shared-export-approval.json')
        ledger().check_q16_scope(value)
        save(f'attempt-{index}.json',{'scope_hash':value,'input_hash':fingerprint(payload),
             'kind':kind,'version':version,'started_at_epoch':time.time(),'sample':index})
        save(f'payload-live-{index}.json',{'scope_hash':value,'input_bytes':input_size_bytes(payload),'payload':payload})
        result,path=await original(payload,kind,version)
        from services.guide.model import MODEL_REVISION
        report=read(path)
        report.update(serving_model_revision=MODEL_REVISION,
                      knowledge_model_revision=assistant.releases.active()['model_revision'])
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return result,path
    assistant.infer=one_shot
    marker={'scope':'P22-Q16-exact-consult-only','instance':sandbox.check_cluster()['instance_id']}
    @app.get('/__q16/binding')
    def binding(): return marker
    save('server.json',marker)
    return app

def run():
    import httpx
    from scripts import round20_sandbox as sandbox
    config,settings=configuration();values=scopes()
    result={'complete':False,'models_before':ledger().summary(),'samples':[],
            'real_model':True,'automatic_retry':False}
    try:
        with sandbox.runner_lock(),httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=40) as client:
            response=client.get('/__q16/binding');assert response.status_code==200
            assert response.json()==read(OUT/'server.json')
            with ordinary_session(client,settings): pass
            assert ledger().summary()['physical_requests']==22
            reset_test_database(config['test_database_url'],confirmation='llm_business_test',fixture='two-trips-one-coupon')
            with ordinary_session(client,settings) as headers:
                write(client,headers,'/payment-profile',{'source_ref':'demo_personal_alice','expected_revision':0},'PUT')
                ids=[write(client,headers,'/trips',{'flight_id':'DEMO-'+size,'travel_date':'2026-10-01','passengers':1})['id'] for size in ('LARGE','SMALL')]
                quotes=[write(client,headers,f'/trips/{ref}/quotes',{'expected_revision':1}) for ref in ids]
                session=None
                for index,question in enumerate(QUESTIONS,1):
                    if index==2:
                        # 独立语义审查未通过时绝不继续；许可文件不是用户新确认，而是工程判据。
                        until=time.monotonic()+600
                        while not (OUT/'sample-1-reviewed.json').exists():
                            assert time.monotonic()<until,'Q16_review_timeout_no_second_request'
                            time.sleep(.2)
                        checked=read(OUT/'sample-1-reviewed.json')
                        assert checked['passed'] is True and checked['scope_hash']==values[0]
                        assert checked['reply_hash']==fingerprint(result['samples'][0]['reply'])
                        write(client,headers,f"/quotes/{quotes[1]['id']}/lock")
                    before=business_snapshot(settings)
                    body={'message':question,'page':'trips','trip_id':ids[0],'delegated_trip_ids':ids,
                        'observation':{'page':'trips','title':'我的行程','items':['暂估报价不会占用优惠券，最终金额以成功锁价为准。'],'product_version':'v1'}}
                    if session: body['session_id']=session
                    response=client.post('/assistant/chat',headers=headers,json=body)
                    after=business_snapshot(settings);budget=ledger().summary()
                    item={'sample':index,'http':response.status_code,'reply':response.json(),
                         'business_before':before,'business_after':after,
                         'business_unchanged':before['sha256']==after['sha256'],
                         'sql':sql_evidence(settings),'budget':budget,'real_model':True}
                    result['samples'].append(item);save(f'sample-{index}.json',item)
                    assert response.status_code==200,'Q16_chat_failed_stop'
                    assert item['business_unchanged'],'Q16_consult_wrote_business'
                    assert budget['physical_requests']==22+index and budget['unresolved_requests']==0
                    assert 'tool' not in item['reply'],'Q16_unrequested_navigation_stop'
                    assert item['reply'].get('reply'),'Q16_empty_reply_stop'
                    session=item['reply']['session_id']
                    print(f'Q16_SAMPLE_{index}_HTTP_200_SETTLED_BUSINESS_UNCHANGED',flush=True)
                    if index==1: print('Q16_WAIT_INDEPENDENT_CONTENT_REVIEW',flush=True)
                result['complete']=True
    except BaseException as error:
        result['failure_type']=type(error).__name__
        ledger().stop_q16_batch('controller_failed')
        raise
    finally:
        result['models_after']=ledger().summary();save('controller-result.json',result)
    print('Q16两份真实咨询已交付；终态与内容结论仍依独立审查。',flush=True)

async def serve():
    import uvicorn
    server=uvicorn.Server(uvicorn.Config(configure(),host='127.0.0.1',port=8000,
          access_log=False,proxy_headers=False,log_level='warning'))
    task=asyncio.create_task(server.serve())
    while not task.done():
        if STOP.exists(): server.should_exit=True
        await asyncio.sleep(.2)
    await task

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('init','server','run'))
    mode=parser.parse_args().action
    try:
        if mode=='init':init()
        elif mode=='server':asyncio.run(serve())
        else:run()
    except (KeyboardInterrupt,AssertionError,RuntimeError,ValueError,KeyError,OSError):
        raise SystemExit('Q16停止；检查新q16目录的脱敏证据，不自动重试或输出凭证。') from None
