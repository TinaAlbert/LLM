"""V1-01零模型单行程演练与输入审查。仅已有55433合成test；产物独占，不允许外发。"""
from contextlib import ExitStack
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from services.business.app import create_app
from services.business.config import Settings
from services.business.db import connect
from services.business.manage import reset_test_database
from services.business import worker
from services.guide import assistant as module
from services.guide.action_contract import ACTION_ENTRIES, confirm_evidence_delivery, action_digest
from services.guide.experiment import input_size_bytes, MAX_INPUT_BYTES
from services.guide.model import ModelResult,TokenUsage
from services.guide.releases import Releases
from scripts import round20_sandbox as sandbox


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,default=str)


def main():
    if len(sys.argv)!=2 or not sys.argv[1].startswith('rehearsal-') or not sys.argv[1].replace('-','').isalnum():
        raise RuntimeError('explicit_unique_rehearsal_label_required')
    out=ROOT/'.local/v1-41'/sys.argv[1]
    if out.exists():raise RuntimeError('exclusive_output_exists')
    sandbox.bind_helper();sandbox.check_runner();sandbox.check_cluster()
    with sandbox.runner_lock():
        if sandbox.pg.running():raise RuntimeError('existing_cluster_busy')
        out.mkdir(parents=True)
        sandbox.pg.start()
        guards=ExitStack()
        try:
            sandbox.check_live();cfg=sandbox.checked_config();purpose=sandbox.check_business(cfg)
            save(out/'environment.json',{'port':55433,'data':str(sandbox.DATA),'verified':purpose,'original_55432_touched':False})
            url=cfg['test_database_url'];settings=Settings(url,'test',True,'test')
            reset_test_database(url,confirmation='llm_business_test',fixture='single-trip')
            old=Releases('test',ROOT/'.local/d3-40/candidate-releases.json').active()
            releases=Releases('test',out/'releases.json')
            release=releases.save_candidate(old['knowledge'],old['observations'],prompt_version='assistant-13')
            releases.publish(release['version'])
            save(out/'knowledge-provenance.json',{'source':'.local/d3-40/candidate-releases.json','source_release':old['version'],
                'source_file_sha256':hashlib.sha256((ROOT/'.local/d3-40/candidate-releases.json').read_bytes()).hexdigest(),
                'new_release':release['version'],'knowledge_unchanged':old['knowledge']==release['knowledge'],
                'built_this_round':False,'published_only_in_rehearsal':True})
            selected={'kind':'lock'};inputs=[];candidates=[]
            def factory(validator):
                async def generate(payload,deadline):
                    validator(payload);assert input_size_bytes(payload)<=MAX_INPUT_BYTES
                    inputs.append(payload);context=json.loads(payload['messages'][-1]['content']);e=context['action_evidence']
                    kind=selected['kind']
                    refs={'lock':['trip.quote.valid','trip.quote.amount_display'],'pay':['trip.lock.valid','trip.lock.amount_display'],
                        'query':['trip.payment.status'],'end':['trip.ticket.status'],'navigate':['selected_trip'],
                        'unknown':['selected_trip']}[kind]
                    candidate={'v':2,'action':{'kind':kind,'target':'trip' if kind in {'lock','pay','query'} else 'task',
                        'entry':ACTION_ENTRIES[kind],'params':{'page':'booking'} if kind=='navigate' else {}},
                        'basis':e['basis'],'evidence':[{'id':k,'value':e['facts'][k]} for k in refs],
                        'gap':'unsupported' if kind=='unknown' else None}
                    candidates.append(candidate)
                    calls=[{'id':'synthetic-navigation','name':'navigate','arguments':{'page':'booking'}}] if kind=='navigate' else []
                    return ModelResult({'content':json.dumps(candidate,ensure_ascii=False),'tool_calls':calls},TokenUsage(0,0,0,0,0),'offline',Decimal(0),'offline')
                return SimpleNamespace(generate_experiment=generate)
            assistant=module.Assistant(settings,releases=releases,adapter_factory=factory,report_dir=out/'model-runs')
            # Transport doubly disabled: no adapter HTTP and no accounting reservation.
            guards.enter_context(patch('services.guide.model.DeepSeekModelAdapter.generate_experiment',side_effect=AssertionError('external_forbidden')))
            guards.enter_context(patch('services.guide.budget.BudgetLedger.reserve',side_effect=AssertionError('reserve_forbidden')))
            with patch.object(module,'Assistant',lambda settings:assistant):
                app=create_app(settings)
            # Route closure holds the injected zero-model Assistant; no global route modifications.
            with TestClient(app) as c:
                login=c.post('/demo/sessions',json={'account':'personal_alice'});assert login.status_code==201
                headers={'Authorization':'Bearer '+login.json()['access_token']}
                def write(path,body=None,method='post'):
                    r=getattr(c,method)(path,headers={**headers,'Idempotency-Key':'v1-'+str(len(events))},json=body or {})
                    assert r.status_code in {200,201,202},(path,r.status_code,r.text)
                    events.append({'path':path,'status':r.status_code});return r.json()
                events=[]
                trip=write('/trips',{'flight_id':'DEMO-PEK-SHA','travel_date':'2026-10-01','passengers':1})
                write('/payment-profile',{'source_ref':'demo_personal_alice','expected_revision':0},'put')
                quote=write(f"/trips/{trip['id']}/quotes",{'expected_revision':trip['revision']})
                sid=None;turns=[]
                def chat(kind,page='booking',question='请根据本轮状态继续帮助我为当前本人行程取得有效票。'):
                    nonlocal sid
                    selected['kind']=kind
                    body={'message':question,'page':page,'trip_id':trip['id'],
                        'observation':{'page':page,'title':'预订与票据' if page=='booking' else '欢迎','items':['公开页面标签（离线合成）'],'product_version':'v1'}}
                    if sid:body['session_id']=sid
                    r=c.post('/assistant/chat',headers=headers,json=body);assert r.status_code==200,(r.status_code,r.text)
                    formal=r.json();sid=formal['session_id']
                    turns.append({'source_kind':'scripted_candidate_not_ai','formal':formal,'input':inputs[-1],'candidate':candidates[-1]})
                    return formal
                chat('lock');booking=write(f"/quotes/{quote['id']}/lock")
                chat('pay');payment=write(f"/locks/{booking['id']}/payments",{'amount_minor':booking['amount_minor']})
                chat('query');r=c.get(f"/payments/{payment['id']}",headers=headers);assert r.status_code==200
                worker.process_payment(url,'test',payment['id'],outcome='succeeded')
                chat('query');paid=c.get(f"/payments/{payment['id']}",headers=headers).json()
                assert paid['status']=='succeeded'
                with connect(url) as conn:assert conn.execute('SELECT count(*) AS n FROM tickets').fetchone()['n']==0
                worker.process_ticket(url,'test',paid['ticket_task_id'],outcome='succeeded')
                chat('end')
                sid=None;navigation=chat('navigate','welcome','请带我去预订与票据页面查看当前行程。')
                ack=c.post('/assistant/ack',headers=headers,json={'session_id':navigation['session_id'],'nonce':navigation['tool']['nonce'],
                    'success':True,'observation':{'page':'booking','title':'预订与票据','items':['合成回执，非浏览器'],'product_version':'v1'}})
                assert ack.status_code==200
                sid=None;chat('unknown',question='能否为我办理该产品未说明的国际签证？')
                # Independent associations and conservation; these values never feed candidates.
                with connect(url) as conn:
                    tables={name:conn.execute('SELECT * FROM '+name).fetchall() for name in ('trips','booking_locks','payments','payment_jobs','provider_payments','ticket_tasks','tickets','flights')}
                assert all(len(tables[k])==1 for k in ('trips','booking_locks','payments','payment_jobs','provider_payments','ticket_tasks','tickets'))
                p=tables['payments'][0];t=tables['tickets'][0];job=tables['payment_jobs'][0];provider=tables['provider_payments'][0];tt=tables['ticket_tasks'][0];lock=tables['booking_locks'][0]
                assert p['status']=='succeeded' and tt['status']=='succeeded'
                assert t['trip_revision']==trip['revision']==lock['trip_revision'] and t['quantity']==1 and bool(t['certificate'])
                assert tables['trips'][0]['owner_id']=='personal_alice' and tables['trips'][0]['status']=='paid'
                assert lock['status']=='consumed' and provider['outcome']=='succeeded' and provider['captured_minor']==110000
                assert str(t['trip_id'])==str(p['trip_id'])==trip['id'] and t['payment_id']==p['id'] and t['task_id']==tt['id']
                assert job['payment_id']==provider['operation_id']==p['id'] and p['lock_id']==lock['id']
                assert p['amount_minor']==lock['amount_minor']==provider['amount_minor']==110000
                flight=next(f for f in tables['flights'] if str(f['travel_date'])=='2026-10-01')
                assert (flight['capacity'],flight['reserved'],flight['sold'])==(2,0,1)
                save(out/'turns.json',turns)
                save(out/'independent-sql.json',tables)
                save(out/'summary.json',{'model_calls':0,'ai_task_denominator':0,'scripted_turns':len(turns),'business_events':events,
                    'valid_tickets':1,'unique_payment':True,'amount_minor':p['amount_minor'],'paid_before_ticket_checked':True,
                    'browser_ack':'synthetic_not_browser','input_sizes':[input_size_bytes(x) for x in inputs],
                    'knowledge_reused':True,'not_v1_acceptance':True})
                print('Offline rehearsal: 7 formal replies, 1 independent valid ticket, model calls 0; synthetic ACK only.')
        finally:
            guards.close()
            sandbox.check_live()
            r=sandbox.pg.command('pg_ctl','-D',str(sandbox.DATA),'-m','fast','-w','stop')
            save(out/'lifecycle.json',{'exit':r.returncode,'running_after':sandbox.pg.running(),'data_retained':True,'original_55432_touched':False})


if __name__=='__main__':main()
