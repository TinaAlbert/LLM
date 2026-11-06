"""P42 工程入口：原网页/真实55433/连续账本；默认零模型。"""
import argparse
import asyncio
from contextlib import ExitStack
from decimal import Decimal
import json
from pathlib import Path
import secrets
import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from services.business.app import Settings, create_app
from services.business.db import connect
from services.business.manage import reset_test_database
from services.business import worker
from services.guide.action_contract import ACTION_ENTRIES, action_digest
from services.guide.budget import BudgetLedger
from services.guide.model import DeepSeekModelAdapter, ModelResult, TokenUsage, ModelUnavailable
from services.guide.releases import Releases
from services.guide.v1_runtime import PersonalRuntime
from services.guide.v1_scope import BATCH, GOALS, FEEDBACK
from scripts import round20_sandbox as sandbox


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def independent_terminal(url, trip_id):
    with connect(url) as conn:
        tables = {n: conn.execute('SELECT * FROM ' + n + " WHERE workspace_id='test'").fetchall()
            for n in ('trips','booking_locks','payments','payment_jobs','provider_payments','ticket_tasks','tickets','flights')}
    one = ('trips','booking_locks','payments','payment_jobs','provider_payments','ticket_tasks','tickets')
    if not all(len(tables[k]) == 1 for k in one): raise ValueError('independent_cardinality_failed')
    trip, lock, pay, job, provider, task, ticket = [tables[k][0] for k in one]
    expected = [trip['owner_id'] == 'personal_alice', trip['status'] == 'paid',
        str(trip['id']) == str(pay['trip_id']) == str(ticket['trip_id']) == trip_id,
        pay['status'] == task['status'] == provider['outcome'] == 'succeeded',
        lock['status'] == 'consumed', ticket['trip_revision'] == lock['trip_revision'] == trip['revision'] == 1,
        ticket['quantity'] == 1, bool(ticket['certificate']), ticket['payment_id'] == pay['id'],
        ticket['task_id'] == task['id'], job['payment_id'] == provider['operation_id'] == pay['id'],
        pay['lock_id'] == lock['id'], pay['amount_minor'] == lock['amount_minor'] == provider['amount_minor'] == provider['captured_minor'] == 110000]
    flight = next(f for f in tables['flights'] if f['id'] == 'DEMO-PEK-SHA' and str(f['travel_date']) == '2026-10-01')
    expected.append((flight['capacity'],flight['reserved'],flight['sold']) == (2,0,1))
    if not all(expected): raise ValueError('independent_terminal_failed')
    return {'valid_ticket':True,'unique_payment':True,'captured_minor':110000,'sql':tables}


def expected_operation(url, trip_id, action):
    """只映射已经确认的动作，不选择下一步。"""
    from services.business.reads import latest
    from services.business.db import read_snapshot
    with connect(url) as conn:
        read_snapshot(conn)
        values = {kind: latest(conn,'test',trip_id,kind,event) for kind,event in (
            ('quote','quote.created'),('lock','lock.created'),('payment','payment.accepted'))}
    q, l, p = values['quote'],values['lock'],values['payment']
    kind = action['kind']
    if kind == 'lock' and q: return ('POST',f"/quotes/{q['id']}/lock",{})
    if kind == 'pay' and l: return ('POST',f"/locks/{l['id']}/payments",{'amount_minor':l['amount_minor']})
    if kind == 'cancel' and l: return ('POST',f"/locks/{l['id']}/cancel",{})
    if kind == 'requote': return ('POST',f'/trips/{trip_id}/quotes',{'expected_revision':1})
    if kind == 'query' and p: return ('POST',f"/payments/{p['id']}/reconcile",{})
    if kind in ('navigate','wait','clarify','unknown','end'): return None
    raise ValueError('original_entry_unavailable')


class CompletedTurnWorker:
    """测试控制面的虚拟事件时钟；每个已完成轮次一个事件，与模型动作无关。

    付款任务在首个事件结算，出票在下一个事件处理。保留一次已付无票
    的重新观察机会；不向模型暴露时钟、SQL或结果选择权限。
    """
    def __init__(self, url, trip_id, out):
        self.url, self.trip_id, self.out = url, trip_id, out
        self.events = {}

    def advance(self, event_id):
        if event_id in self.events:
            return self.events[event_id]
        with connect(self.url) as conn:
            payment = conn.execute(
                "SELECT id,status FROM payments WHERE workspace_id='test' AND trip_id=%s",
                (self.trip_id,)).fetchone()
            task = conn.execute(
                "SELECT id,status FROM ticket_tasks WHERE workspace_id='test' AND trip_id=%s",
                (self.trip_id,)).fetchone()
        receipt = {'event_id':event_id, 'source_kind':'synthetic_completed_turn_event', 'processed':None}
        if payment and payment['status'] in ('processing','unknown'):
            worker.process_payment(self.url,'test',str(payment['id']),outcome='succeeded')
            with connect(self.url) as conn:
                paid = conn.execute("SELECT status FROM payments WHERE workspace_id='test' AND id=%s",(payment['id'],)).fetchone()
                count = conn.execute("SELECT count(*) AS n FROM tickets WHERE workspace_id='test' AND trip_id=%s",(self.trip_id,)).fetchone()['n']
            if paid['status'] != 'succeeded' or count != 0:
                raise ValueError('paid_before_ticket_failed')
            save(self.out/'paid-before-ticket.json',{'payment_succeeded':True,'ticket_count':0,'event_id':event_id})
            receipt['processed'] = 'payment'
        elif payment and payment['status']=='succeeded' and task and task['status']=='pending':
            worker.process_ticket(self.url,'test',str(task['id']),outcome='succeeded')
            receipt['processed'] = 'ticket'
        save(self.out/f'worker-event-{event_id}.json',receipt)
        self.events[event_id] = receipt
        return receipt


def make_app(settings, out, trip_id, releases, *, real=False, reviewed=None):
    app = create_app(settings)
    a = app.state.assistant
    a.releases, a.report_dir, a.external_model = releases, out/'model-runs', False
    a.admin_path = out/'admin.json'
    save(a.admin_path,{'workspace':'test','actor_ids':['personal_alice']})
    def no_model(_):
        async def generate(*args,**kwargs): raise ModelUnavailable('v1_runtime_not_installed')
        return SimpleNamespace(generate_experiment=generate)
    a.adapter_factory = no_model
    ledger = BudgetLedger(ROOT/'.local/guide-usage.json')
    host = {'runtime':None,'operation':None,'receipt':None}
    event_worker = CompletedTurnWorker(settings.database_url,trip_id,out)
    secret = secrets.token_hex(32)
    save(out/'host-secret.json',{'key':secret})
    def require_host(request):
        if request.headers.get('X-V1-Host') != secret: raise HTTPException(403,'engineering_host_only')
    old_status = a.status
    def status(who):
        value = old_status(who)
        r = host['runtime']
        if r: value.update(ready=not r.stopped and (not real or ledger.q36_path.exists() and not ledger.q36_stop_path.exists()),reason=None)
        return value
    a.status = status

    @app.post('/__v1/install')
    async def install(request: Request):
        require_host(request)
        if host['runtime'] is not None: raise HTTPException(409,'already_installed')
        observations = await request.json()
        if real and observations != reviewed['observations']: raise HTTPException(409,'reviewed_observations_changed')
        r = PersonalRuntime(a,trip_id,release=releases.active(),observations=observations,ledger=ledger,directory=out/'turns',real_model=real)
        host['runtime'] = r
        def factory(validator):
            async def generate(payload,deadline):
                if real:
                    adapter = DeepSeekModelAdapter(ledger,input_validator=validator,q36_grant_path=ledger.q36_path,
                        q36_authorizer=r.scope.authorize_actual,protocol_report_dir=out/'protocol')
                    return await adapter.generate_experiment(payload,deadline,approval_path=reviewed['experiment_path'])
                validator(payload); r.scope.authorize_actual(payload)
                # 唯一预排路线仅存在零模型工程夹具；实发分支没有它。
                kind = 'navigate' if r.scope.spec['case_id'] == 'V1-B' else 'unknown'
                c = json.loads(payload['messages'][1]['content'])['action_evidence']
                refs = {'lock':['trip.quote.valid','trip.quote.amount_display'],'pay':['trip.lock.valid','trip.lock.amount_display'],
                    'query':['trip.payment.status'],'wait':['trip.ticket_task.status'],'end':['trip.ticket.status']}.get(kind,['selected_trip'])
                candidate = {'v':2,'action':{'kind':kind,'target':'trip' if kind in ('lock','pay','query') else 'task',
                    'entry':ACTION_ENTRIES[kind],'params':{'page':'booking'} if kind=='navigate' else {}},
                    'basis':c['basis'],'evidence':[{'id':k,'value':c['facts'][k]} for k in refs],
                    'gap':'unsupported' if kind=='unknown' else None}
                calls = [{'id':'offline-navigation','name':'navigate','arguments':{'page':'booking'}}] if kind=='navigate' else []
                return ModelResult({'content':json.dumps(candidate,ensure_ascii=False),'tool_calls':calls},TokenUsage(0,0,0,0,0),'offline',Decimal(0),'offline')
            return SimpleNamespace(generate_experiment=generate)
        a.adapter_factory = factory
        save(out/'observations.json',observations)
        save(out/'task-releases.json',releases.read())
        return {'mode':'real' if real else 'scripted_zero_model','goals':GOALS,'feedback':FEEDBACK,
            'initial_pages':['welcome','booking']}

    @app.post('/__v1/abort')
    async def abort(request: Request):
        require_host(request)
        runtime = host['runtime']
        if runtime is None:
            raise HTTPException(409,'runtime_required')
        runtime.stop('engineering_browser_failed')
        return {'stopped':True}

    @app.post('/__v1/pending')
    async def pending(request: Request):
        require_host(request)
        formal = host['runtime'].pending['formal']
        return {'remaining_seconds':max(0,300-(time.monotonic()-host['runtime'].task_started)),
            'confirmation':{'action_sha256':formal['action_sha256'],
            'reply_sha256':action_digest(formal['reply']),'action':formal['action_contract']['action']}}

    @app.post('/__v1/confirm')
    async def confirm(request: Request):
        require_host(request)
        r = host['runtime']
        if r is None: raise HTTPException(409,'runtime_required')
        try:
            action = await r.confirm(await request.json(),request)
            host['operation'] = expected_operation(settings.database_url,trip_id,action)
            host['receipt'] = None
            return {'action':action}
        except Exception:
            r.stop('confirmation_failed'); raise

    @app.middleware('http')
    async def observed_operation(request, call_next):
        r, path = host['runtime'],request.url.path
        business_write = request.method in ('POST','PUT','DELETE','PATCH') and not path.startswith(('/assistant','/__v1','/demo'))
        if r and business_write:
            try:
                r.check()
                if r.scope.spec['case_id'] in {'V1-B','V1-D'}:
                    r.stop('readonly_task_business_write_forbidden')
                    return JSONResponse({'error':'v1_readonly_task'},status_code=409)
                expected = host['operation']
                if not expected or not r.pending or not r.pending['confirmed'] or host['receipt']: raise ValueError('unconfirmed_business_write')
                who = await a.who(request,time.monotonic()+5)
                _, state_hash = await r._state(request,who,r.body.trip_id,time.monotonic()+5)
                if (who != r.owner or state_hash != r.pending['private_hash'] or time.monotonic() >= r.pending['expires']
                        or (request.method,path) != expected[:2] or await request.json() != expected[2]
                        or not request.headers.get('Idempotency-Key')): raise ValueError('business_operation_changed')
                response = await call_next(request)
                kind = r.pending['formal']['action_contract']['action']['kind']
                accepted = 200 <= response.status_code < 300
                host['receipt'] = {'kind':kind,'accepted':accepted,'method':request.method,'path':path,'status':response.status_code}
                host['operation'] = None
                if not accepted: r.stop('original_operation_failed')
                return response
            except Exception:
                r.stop('business_execution_failed')
                return JSONResponse({'error':'v1_execution_stopped'},status_code=409)
        if r and (real or not r.stopped) and path.startswith('/assistant/admin') and request.method != 'GET':
            r.stop('release_operation_during_task')
            return JSONResponse({'error':'v1_task_active'},status_code=409)
        return await call_next(request)

    @app.post('/__v1/complete')
    async def complete(request: Request):
        require_host(request)
        r = host['runtime']
        if r is None: raise HTTPException(409,'runtime_required')
        try:
            r.check()
            if not r.pending: raise ValueError('pending_action_required')
            who = await a.who(request,time.monotonic()+5)
            if who != r.owner or who != r.pending['owner']:
                raise ValueError('completion_identity_changed')
            # 原业务写入已经复核并改变前态；无写入的等待/结束/导航仍须
            # 绑定当前状态，避免确认后换身份或改变状态再推进模拟事件。
            if host['receipt'] is None:
                _, state_hash = await r._state(request,who,r.body.trip_id,time.monotonic()+5)
                if state_hash != r.pending['private_hash']:
                    raise ValueError('completion_state_changed')
            body = await request.json()
            kind = r.pending['formal']['action_contract']['action']['kind']
            receipt = host['receipt'] or {'kind':kind,'accepted':True}
            if body.get('kind') != kind: raise ValueError('browser_action_changed')
            terminal = False
            case_id = r.scope.spec['case_id']
            if kind == 'navigate':
                if not r.pending['ack'] or body.get('dom_verified') is not True: raise ValueError('real_dom_required')
                receipt.update(page=body['page'],dom_verified=True,source_kind='browser_dom_and_nonce_ack')
                terminal = case_id == 'V1-B' and body['page'] == 'booking'
            elif kind in ('unknown','clarify'):
                terminal = case_id == 'V1-D'
                if not terminal: raise ValueError('task_goal_not_reached')
            elif kind == 'end':
                save(out/'independent-sql.json',independent_terminal(settings.database_url,trip_id))
                receipt['independent_valid_ticket'] = True
                terminal = case_id == 'V1-AC'
                if not terminal: raise ValueError('unexpected_end')
            elif kind != 'wait' and host['receipt'] is None: raise ValueError('original_http_receipt_required')
            event_id = f'{r.sequence}-{r.round:02d}'
            r.complete(receipt,terminal=terminal); host['receipt'] = None
            if not terminal and case_id == 'V1-AC':
                event_worker.advance(event_id)
            return {'sequence':r.sequence,'round':r.round,'completed':r.stopped,'next_goal':GOALS[r.sequence-1]}
        except Exception:
            r.stop('execution_or_terminal_failed'); raise
    return app,host


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode',choices=['rehearsal','live']); parser.add_argument('label'); parser.add_argument('--review',type=Path)
    args = parser.parse_args()
    if not args.label.replace('-','').isalnum(): raise ValueError('exclusive_label_required')
    out = ROOT/'.local/v1-42'/args.label
    if out.exists(): raise ValueError('exclusive_output_exists')
    reviewed = None
    if args.mode == 'live':
        from scripts.v1_package import checked_package,checked_grant
        reviewed = checked_package(args.review); checked_grant(args.review)
    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1',8000)) == 0: raise ValueError('port_busy')
    sandbox.bind_helper(); sandbox.check_runner(); sandbox.check_cluster()
    with sandbox.runner_lock():
        if sandbox.pg.running(): raise ValueError('sandbox_busy')
        out.mkdir(parents=True); sandbox.pg.start()
        with ExitStack() as guards:
            try:
                sandbox.check_live(); cfg = sandbox.checked_config(); sandbox.check_business(cfg)
                # Q36只续验B/D；保留Q35已经独立验证的票，不重做A/C或重置数据。
                url = cfg['test_database_url']
                source_binding = read(ROOT/'.local/v1-42/run-04/binding.json')
                trip = {'id':source_binding['trip_id']}
                initial = independent_terminal(url,trip['id'])
                save(out/'initial-independent-sql.json',initial)
                settings = Settings(url,'test',True,'test'); releases = Releases('test',out/'releases.json')
                if reviewed: releases.path.write_bytes((args.review/'releases.json').read_bytes())
                else:
                    old = Releases('test',ROOT/'.local/d3-40/candidate-releases.json').active()
                    previous = releases.save_candidate(old['knowledge'],old['observations'],prompt_version='assistant-12'); releases.publish(previous['version'])
                    candidate = releases.save_candidate(old['knowledge'],old['observations'],prompt_version='assistant-14'); releases.publish(candidate['version'])
                save(out/'binding.json',{'trip_id':trip['id'],'mode':args.mode,'release':releases.active()['version'],'knowledge_hash':releases.active()['knowledge_hash'],'model_calls_allowed':args.mode=='live'})
                if args.mode == 'rehearsal':
                    guards.enter_context(patch.object(DeepSeekModelAdapter,'generate_experiment',side_effect=AssertionError('external_forbidden')))
                    guards.enter_context(patch.object(BudgetLedger,'reserve',side_effect=AssertionError('reserve_forbidden')))
                app, host = make_app(settings,out,trip['id'],releases,real=args.mode=='live',reviewed=reviewed)
                import uvicorn
                server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8000,access_log=False,proxy_headers=False))
                async def serve():
                    task = asyncio.create_task(server.serve())
                    while not task.done():
                        runtime = host['runtime']
                        if args.mode == 'live' and runtime is not None:
                            runtime.expire_if_due()
                        if (out/'server.stop').exists(): server.should_exit = True
                        await asyncio.sleep(.1)
                    await task
                asyncio.run(serve())
                r = host['runtime']
                if args.mode == 'live' and (not r or not r.stopped):
                    from services.guide.task_budget import stop_q19_batch
                    stop_q19_batch(BudgetLedger(ROOT/'.local/guide-usage.json'),'server_interrupted',batch=BATCH)
            except BaseException:
                if args.mode == 'live':
                    from services.guide.task_budget import stop_q19_batch
                    stop_q19_batch(BudgetLedger(ROOT/'.local/guide-usage.json'),'server_failed',batch=BATCH)
                raise
            finally:
                sandbox.check_live(); stopped = sandbox.pg.command('pg_ctl','-D',str(sandbox.DATA),'-m','fast','-w','stop')
                save(out/'lifecycle.json',{'exit':stopped.returncode,'running_after':sandbox.pg.running(),'original_55432_touched':False})


if __name__ == '__main__': main()
