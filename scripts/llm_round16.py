"""Prompt 16工程侧独立判据与配对基线；不向模型发送SQL/测试预期。"""
import argparse,asyncio,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from services.business.config import load_settings
from services.business.db import connect
from services.business.manage import check_marker,database_purpose
from scripts.d3_evaluate import business_snapshot
from services.guide.assistant import Assistant,document_only_payload
from services.guide.budget import BudgetLedger

OUT=ROOT/'.local/d3-16'
def save(name,value):
    with (OUT/name).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)

def state(phase):
    prepared=json.loads((OUT/'browser-prepare.json').read_text(encoding='utf-8-sig'))
    s=load_settings(test=True)
    with connect(s.database_url) as c:
        check_marker(c,'test');assert database_purpose(c)=='test'
        c.execute('SET TRANSACTION READ ONLY')
        rows=c.execute('''SELECT t.revision,t.passengers,t.status AS trip_status,p.status AS payment_status,
          p.amount_minor,l.amount_minor AS locked_minor,l.trip_revision,l.status AS lock_status,
          pp.outcome,pp.captured_minor,tt.status AS task_status,k.trip_revision AS ticket_revision,
          k.quantity,k.payment_id=p.id AS ticket_payment_matches,k.trip_id=t.id AS ticket_trip_matches
          FROM trips t JOIN payments p ON (p.workspace_id,p.trip_id)=(t.workspace_id,t.id)
          JOIN booking_locks l ON (l.workspace_id,l.id)=(p.workspace_id,p.lock_id)
          JOIN provider_payments pp ON (pp.workspace_id,pp.operation_id)=(p.workspace_id,p.id)
          JOIN ticket_tasks tt ON (tt.workspace_id,tt.payment_id)=(p.workspace_id,p.id)
          LEFT JOIN tickets k ON (k.workspace_id,k.task_id)=(tt.workspace_id,tt.id)
          WHERE t.workspace_id=%s AND t.id=%s AND p.id=%s''',('test',prepared['trip_id'],prepared['payment_id'])).fetchall()
        assert len(rows)==1
        r=rows[0];assert r['trip_status']=='paid' and r['payment_status']==r['outcome']=='succeeded'
        assert r['amount_minor']==r['locked_minor']==r['captured_minor']==110000
        assert r['revision']==r['trip_revision']==1 and r['lock_status']=='consumed'
        counts={table:c.execute(f'SELECT count(*) AS n FROM {table} WHERE workspace_id=%s',('test',)).fetchone()['n'] for table in ('payments','ticket_tasks','tickets')}
        assert counts['payments']==counts['ticket_tasks']==1
        if phase=='pending':assert counts['tickets']==0 and r['task_status']=='pending' and r['ticket_revision'] is None
        else:
            assert counts['tickets']==1 and r['task_status']=='succeeded' and r['ticket_revision']==r['revision']
            assert r['quantity']==r['passengers']==1 and r['ticket_payment_matches'] and r['ticket_trip_matches']
    snapshot=business_snapshot(s)
    result={'phase':phase,'independent_sql':'passed','state':r,'counts':counts,'business_sha256':snapshot['sha256']}
    save(f'state-{phase}.json',result);print(json.dumps(result,ensure_ascii=False))

async def documents():
    source=json.loads((OUT/'browser-enhanced.json').read_text(encoding='utf-8'))
    runs=[json.loads((ROOT/'.local/assistant-runs'/f).read_text(encoding='utf-8')) for f in source['run_files']]
    record=next(r for r in runs if json.loads(r['input']['messages'][-1]['content'])['question']==source['question'])
    payload=document_only_payload(record['input'])
    path=OUT/'documents.json'
    with path.open('x',encoding='utf-8') as f:json.dump({'state':'started','input':payload,'paired_input_hash':record['input_hash']},f,ensure_ascii=False,indent=2)
    assistant=Assistant(load_settings(test=True),report_dir=OUT/'document-runs')
    try:
        result,report=await assistant.infer(payload,'document_only',record['release_version'])
        value={'state':'received','report':report.name,'candidate':result.candidate,'paired_input_hash':record['input_hash']}
    except Exception as e:
        value={'state':'failed','error_type':type(e).__name__,'reason':getattr(e,'reason',None)}
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');raise
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['pending','issued','documents','budget']);args=parser.parse_args()
    if args.phase=='documents':asyncio.run(documents())
    elif args.phase=='budget':print(json.dumps(BudgetLedger(ROOT/'.local/guide-usage.json').summary()))
    else:state(args.phase)
