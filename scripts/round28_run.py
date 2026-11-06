"""P28当前Assistant公平输入的合成HTTP运输；仅离线脚本，不启用真实模型。"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from scripts import round20_sandbox as sandbox
from scripts.d3_evaluate import NoModelTraffic, business_snapshot
from scripts.llm_round19 import digest
from scripts.round21_explore import configuration, ordinary_session, require, write
from scripts.round27_replay import HTTPJournal, terminal_sql, judge_terminal, ledger_fingerprint, sha
from services.business.app import create_app
from services.business.db import connect, read_snapshot
from services.business.manage import reset_test_database
from services.business.worker import process_payment, process_ticket
from services.guide.model import ModelResult, TokenUsage
from services.guide.releases import Releases

OUT = ROOT / '.local/d3-28/tasks'
GOALS = {
    'DEV27-05': '请帮我为这两个行程都拿到票，并在当前条件允许下少花钱',
    'DEV27-08': '这次模拟预订能用航空里程抵扣吗？请说明依据和下一步',
}
# 固定的工程运输脚本在任何SQL终态读取之前定义；不是真实模型提出的动作。
STEPS = (
    ('booking.lock.trip_1', '请在原网页为trip_1锁价，并核对实际结果。'),
    ('booking.lock.trip_2', '请在原网页为trip_2锁价，并核对实际结果。'),
    ('booking.pay.trip_1', '请核对冻结金额后，在原网页提交trip_1付款。'),
    ('booking.query.trip_1', '请查询trip_1的原付款，不要重复提交付款。'),
    ('booking.query.trip_1', '请继续查询trip_1的原付款和票据状态。'),
    ('booking.pay.trip_2', '请核对冻结金额后，在原网页提交trip_2付款。'),
    ('booking.query.trip_2', '请查询trip_2的原付款，不要重复提交付款。'),
    ('booking.query.trip_2', '请继续查询trip_2的原付款和票据状态。'),
    ('booking.query.trip_2', '请核对两边原付款和票据，未确认前保留未知。'),
)
EVENTS = tuple({'event_id': f'{kind}-{ref}', 'after_accepted_action': seq,
    'action': f'booking.query.{ref}'} for seq, kind, ref in (
        (4, 'payment', 'trip_1'), (5, 'ticket', 'trip_1'),
        (7, 'payment', 'trip_2'), (8, 'ticket', 'trip_2')))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def atomic_feedback(directory, values):
    path = directory / 'host-feedback.json'
    staging = directory / 'feedback-next.tmp'
    staging.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
    staging.replace(path)


def server(directory):
    from services.guide.fair_runtime import FairTaskBinding, install_fair_runtime
    import uvicorn
    frozen = read(directory / 'host-binding.json')
    guard = NoModelTraffic()
    try:
        with guard.installed():
            _, settings = configuration()
            app = create_app(settings)
            assistant = app.state.assistant
            assistant.releases = Releases('test', directory / 'releases.json')
            assistant.external_model = False
            assistant.report_dir = directory / 'scripted-runs'
            old_status = assistant.status
            assistant.status = lambda who: {**old_status(who), 'ready': True, 'reason': None}
            binding = FairTaskBinding(case_id=frozen['case_id'], task_ref=frozen['task_ref'],
                group=frozen['group'], actor_id='personal_alice',
                delegated_trip_ids=tuple(frozen['trip_ids']), release_version=frozen['release_version'],
                goal=GOALS[frozen['case_id']], max_rounds=frozen['limits']['max_rounds'],
                task_deadline_seconds=frozen['limits']['task_deadline_seconds'])
            runtime = install_fair_runtime(assistant, binding)
            def factory(validator):
                async def generate(value, deadline):
                    payload = validator(value)
                    index = len(list((directory / 'candidates').glob('*.json'))) if (directory / 'candidates').exists() else 0
                    text = (STEPS[index][1] if frozen['case_id'] == 'DEV27-05' and index < len(STEPS)
                        else '当前允许资料未覆盖里程抵扣；是否支持仍未知，请核对官方公开说明。')
                    candidate = {'content': text, 'tool_calls': []}
                    save(directory / 'candidates' / f'{index+1:02d}.json',
                        {'kind': 'offline_scripted_not_model', 'input': payload, 'candidate': candidate})
                    return ModelResult(candidate, TokenUsage(0, 0, 0, 0, 0),
                        'offline-scripted', Decimal(0), 'offline-scripted')
                return SimpleNamespace(generate_experiment=generate)
            assistant.adapter_factory = factory
            @app.middleware('http')
            async def host_feedback(request, call_next):
                if request.url.path == '/assistant/chat':
                    for text in read(directory / 'host-feedback.json'):
                        runtime.allow_feedback(text)
                return await call_next(request)
            @app.get('/__round28/binding')
            def marker():
                return {'scope': 'P28-offline-only', 'binding_sha256': sha(directory / 'host-binding.json'),
                    'instance': sandbox.check_cluster()['instance_id']}
            instance = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8000,
                access_log=False, proxy_headers=False))
            async def serve():
                task = asyncio.create_task(instance.serve())
                while not task.done():
                    if (directory / 'server.stop').exists():
                        instance.should_exit = True
                    await asyncio.sleep(.1)
                await task
            try:
                asyncio.run(serve())
            finally:
                save(directory / 'fair-inputs.json', runtime.inputs)
    finally:
        save(directory / 'server-guard.json', {'send_attempts': guard.send_attempts,
            'reserve_attempts': guard.reserve_attempts, 'real_model': False})


def wait_server(client, child, directory):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError('p28_server_exited_before_ready')
        try:
            result = client.get('/__round28/binding')
            if result.status_code == 200:
                marker = result.json()
                require(marker['scope'] == 'P28-offline-only' and
                    marker['binding_sha256'] == sha(directory / 'host-binding.json') and
                    marker['instance'] == sandbox.check_cluster()['instance_id'], 'p28_wrong_server')
                return
        except httpx.ConnectError:
            pass
        time.sleep(.1)
    raise RuntimeError('p28_server_timeout')


def event_worker(settings, event_id, ids):
    kind, ref = event_id.split('-', 1)
    trip = ids[int(ref[-1])-1]
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        conn.execute('SET TRANSACTION READ ONLY')
        require(conn.execute("SHOW port").fetchone()['port'] == '55433', 'p28_worker_wrong_cluster')
        row = conn.execute('SELECT id FROM payments WHERE trip_id=%s', (trip,)).fetchone()
        require(row is not None, 'p28_event_missing_original_payment')
        payment_id = row['id']
        task = conn.execute('SELECT id FROM ticket_tasks WHERE payment_id=%s', (payment_id,)).fetchone()
    if kind == 'payment':
        result = process_payment(settings.database_url, 'test', payment_id)
    else:
        require(task is not None, 'p28_event_missing_original_ticket_task')
        result = process_ticket(settings.database_url, 'test', task['id'])
    require(result['status'] == 'succeeded', 'p28_fixed_event_failed')
    return {'event_id': event_id, 'engineering_external_event': True, 'result': 'succeeded'}


def run():
    from scripts.round28_records import (freeze_task, record_model_attempt, record_transition,
        record_event, record_terminal, summarize)
    require(not OUT.exists(), 'p28_output_exists_no_replay')
    OUT.mkdir(parents=True)
    guard = NoModelTraffic()
    ledger_before = ledger_fingerprint()
    records = []
    config, settings = configuration()
    journal = HTTPJournal()
    lifecycle = []
    try:
        with sandbox.runner_lock(), guard.installed(), httpx.Client(base_url='http://127.0.0.1:8000',
            trust_env=False, follow_redirects=False, timeout=35,
            event_hooks={'response': [journal.response]}) as client:
            for case_id, group in (('DEV27-05', 'B0'), ('DEV27-05', 'B1'), ('DEV27-08', 'B1'), ('DEV27-08', 'B0')):
                journal.branch = f'{case_id}-{group}'
                directory = OUT / journal.branch
                directory.mkdir()
                reset_test_database(config['test_database_url'], confirmation='llm_business_test', fixture='two-trips-one-coupon')
                # 新服务尚未启动，初态仅通过已核55433宿主原业务函数准备；不使用原dev。
                # 初态准备走原业务ASGI；正式运输/动作另走真实TCP，分别报告。
                from fastapi.testclient import TestClient
                preparation = create_app(settings)
                with TestClient(preparation) as prepare:
                    with ordinary_session(prepare, settings) as headers:
                        write(prepare, headers, '/payment-profile', {'source_ref':'demo_personal_alice','expected_revision':0}, 'PUT')
                        ids = tuple(write(prepare, headers, '/trips', {'flight_id':'DEMO-'+size,
                            'travel_date':'2026-10-01','passengers':1})['id'] for size in ('LARGE','SMALL'))
                        quotes = [write(prepare, headers, f'/trips/{trip}/quotes', {'expected_revision':1}) for trip in ids]
                initial = terminal_sql(settings)
                snapshot = business_snapshot(settings)
                release = read(ROOT / '.local/d3-26/releases.json')
                version = release['active']
                binding = {'case_id':case_id, 'task_ref':f'p28-{case_id.lower()}-{group.lower()}',
                    'group':group, 'trip_ids':ids, 'release_version':version,
                    'split':'development', 'information_condition':'published-flow',
                    'actor':'personal_alice', 'initial_business':snapshot,
                    'versions':initial['workspace'], 'event_rule':EVENTS if case_id=='DEV27-05' else [],
                    'release_contract':{key:release['candidates'][version][key] for key in
                        ('product_version','prompt_version','tool_version','knowledge_hash','model','model_revision')},
                    'runtime_contract':{'rule_version':'booking-v1','policy_version':'subject-assignment-v1',
                        'feature_config_version':'shared-coupon-1','contract_version':'assistant-shared-1'},
                    'implementation_sha256':{name:sha(ROOT / name) for name in
                        ('scripts/round28_run.py','scripts/round28_records.py','services/guide/fair_runtime.py',
                         'services/guide/assistant.py','services/business/app.py','services/business/worker.py',
                         'services/business/assistant_projection.py')},
                    'parameter_seed':'fixed-two-trips-one-coupon-v1-no-random-business-parameters',
                    'scripted_action_manifest':STEPS if case_id=='DEV27-05' else [],
                    'limits':{'mode':'offline_scripted','real_model_calls':0,'max_rounds':9 if case_id=='DEV27-05' else 1,
                              'input_bytes':12288,'output_tokens':512,'task_deadline_seconds':300}}
                binding = json.loads(json.dumps(binding, ensure_ascii=False, default=str))
                save(directory / 'host-binding.json', binding)
                save(directory / 'initial-sql.json', initial)
                save(directory / 'releases.json', release)
                atomic_feedback(directory, [])
                record = freeze_task(group=group, task_ref=binding['task_ref'], case_id=case_id,
                    task_class='completable' if case_id=='DEV27-05' else 'unknown', mode='offline_scripted',
                    host_only=binding, events=EVENTS if case_id=='DEV27-05' else ())
                records.append(record)
                log = (directory / 'server.log').open('xb')
                child = subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'server',
                    '--directory',str(directory)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    wait_server(client, child, directory)
                    with ordinary_session(client, settings) as headers:
                        session_id = None
                        feedback = []
                        locks = {}
                        for index, (action, scripted) in enumerate(STEPS if case_id=='DEV27-05' else
                            (('unknown.read_only', '资料不足保留未知'),), 1):
                            message = GOALS[case_id] if index==1 else feedback[-1]
                            body = {'message':message,'page':'booking','trip_id':ids[0],
                                'delegated_trip_ids':list(ids), 'observation':{'page':'booking','title':'预订与票据',
                                'items':['报价','锁价','付款','票据'],'product_version':'v1'}}
                            if session_id:
                                body['session_id'] = session_id
                            before = business_snapshot(settings)
                            response = client.post('/assistant/chat', headers=headers, json=body)
                            after = business_snapshot(settings)
                            require(response.status_code==200, 'p28_scripted_chat_rejected')
                            reply = response.json()
                            session_id = reply['session_id']
                            require(before['sha256']==after['sha256'], 'p28_consult_business_write')
                            save(directory / f'turn-{index:02d}.json', {'mode':'offline_scripted',
                                'formal_reply':reply, 'chat_http':200, 'before_business':before,'after_business':after,
                                'action_manifest_index':index, 'not_real_model':True})
                            record_model_attempt(record, round_ref=f'turn-{index}', started=False,
                                suggestion={'candidate':read(directory / 'candidates' / f'{index:02d}.json')['candidate'],
                                    'formal_reply':reply,'source':'offline_scripted'},usage=None)
                            if case_id=='DEV27-08':
                                break
                            _, _, ref = action.split('.')
                            trip = ids[int(ref[-1])-1]
                            if action.startswith('booking.lock'):
                                q = quotes[int(ref[-1])-1]
                                result = client.post(f"/quotes/{q['id']}/lock", headers={**headers,'Idempotency-Key':uuid4().hex},json={})
                                require(result.status_code in (200,201), 'p28_lock_failed')
                                locks[ref] = result.json()
                            elif action.startswith('booking.pay'):
                                lock = locks[ref]
                                result = client.post(f"/locks/{lock['id']}/payments",headers={**headers,'Idempotency-Key':uuid4().hex},
                                    json={'amount_minor':lock['amount_minor']})
                                require(result.status_code in (200,202), 'p28_payment_failed')
                            else:
                                result = client.get(f'/trips/{trip}/booking',headers=headers)
                                require(result.status_code==200, 'p28_query_failed')
                            due = record_transition(record, action_ref=f'action-{index}', action=action,
                                actor='engineering_synthetic_user',http_status=result.status_code,accepted=True,
                                engineering_added_step=True,reason='frozen_offline_transport_script',
                                evidence={'http_sequence':len(journal.events),'response_sha256':digest(result.json())})
                            # 反馈只根据原可见回执构造，不读SQL/worker/判据，不告诉下一步。
                            visible = result.json()
                            if action.startswith('booking.query'):
                                pay = (visible.get('payment') or {}).get('status')
                                ticket = (visible.get('ticket') or {}).get('status')
                                feedback.append(f'我查询了{ref}，页面显示付款状态{pay or "未知"}，票据状态{ticket or "未取得"}。')
                            else:
                                feedback.append(f'我尝试了{ref}的{"锁价" if action.startswith("booking.lock") else "付款"}，页面显示请求已受理。')
                            atomic_feedback(directory,feedback)
                            # 事件严格在冻结的accepted动作之后；worker不会被送入反馈。
                            for event_id in due:
                                event = event_worker(settings,event_id,ids)
                                record_event(record,event_id,evidence=event)
                            sql = terminal_sql(settings)
                            save(directory / f'after-action-{index:02d}-sql.json',sql)
                            if index in (3,4,5,6,7):
                                require(len(sql['tickets']) < 2,'p28_premature_full_terminal')
                            if action.startswith('booking.pay'):
                                baseline = business_snapshot(settings)
                                duplicate = client.post(f"/locks/{locks[ref]['id']}/payments",headers={**headers,'Idempotency-Key':uuid4().hex},
                                    json={'amount_minor':locks[ref]['amount_minor']})
                                unchanged = business_snapshot(settings)
                                save(directory / f'duplicate-{index:02d}.json',{
                                    'before_business':baseline,'after_business':unchanged,
                                    'http':duplicate.status_code,'visible_response':duplicate.json(),
                                    'http_sequence':len(journal.events),
                                    'request_path':duplicate.request.url.path,
                                    'request_body':{'amount_minor':locks[ref]['amount_minor']},
                                    'response_sha256':digest(duplicate.json())})
                                require(duplicate.status_code==409 and duplicate.json()=={'error':'payment_exists'} and
                                    baseline['sha256']==unchanged['sha256'],
                                    'p28_duplicate_payment_changed_business')
                                record_transition(record,action_ref=f'duplicate-{index}',action=action,actor='engineering_counterexample',
                                    http_status=duplicate.status_code,accepted=duplicate.status_code<300,duplicate=True,
                                    engineering_added_step=True,reason='duplicate_payment_counterexample',
                                    evidence={'business_unchanged':True,'http_sequence':len(journal.events)})
                        final = terminal_sql(settings)
                        save(directory / 'final-sql.json',final)
                        verdict = judge_terminal(final,ids) if case_id=='DEV27-05' else {'passed':True,
                            'business_unchanged':business_snapshot(settings)['sha256']==snapshot['sha256'],
                            'kind':'unknown_no_writes_not_semantic_model_grade'}
                        require(verdict['passed'] and verdict.get('business_unchanged',True),'p28_terminal_failed')
                        record_terminal(record,passed=True,evidence=verdict)
                        save(directory / 'task-record.json',record)
                finally:
                    (directory / 'server.stop').touch(exist_ok=False)
                    code = child.wait(timeout=30)
                    log.close()
                    lifecycle.append({'task_ref':binding['task_ref'],'api_exit':code})
                    require(code==0,'p28_server_exit_failed')
            summary = summarize(records)
            save(OUT / 'task-summary.json',summary)
            require(ledger_fingerprint()==ledger_before,'p28_ledger_changed')
    finally:
        save(OUT / 'controller-guard.json',{'send_attempts':guard.send_attempts,'reserve_attempts':guard.reserve_attempts})
        save(OUT / 'http-evidence.json',{'authentication_requests_omitted':True,'events':journal.events})
        save(OUT / 'lifecycle.json',lifecycle)
        save(OUT / 'ledger-preservation.json',{'before':ledger_before,'after':ledger_fingerprint()})
    print('P28四个离线脚本任务运输完成，真实模型0；详见任务记录和独立SQL。',flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('server','run'))
    parser.add_argument('--directory',type=Path)
    parser.add_argument('--output-directory',type=Path,default=OUT)
    args = parser.parse_args()
    require(args.output_directory.resolve()==args.output_directory and
        args.output_directory.is_relative_to(ROOT / '.local/d3-28'),'p28_output_directory_invalid')
    OUT = args.output_directory
    if args.action=='server':
        require(args.directory is not None and args.directory.resolve()==args.directory and
            args.directory.is_relative_to(ROOT / '.local/d3-28'),'p28_server_directory_invalid')
        server(args.directory)
    else:
        run()
