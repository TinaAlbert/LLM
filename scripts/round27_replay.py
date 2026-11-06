"""P27真实建议的显式工程HTTP回放；模型0/新许可0，不自启动PG。

server/run仅使用已核验55433合成test与原业务API。offline_replay返回26原候选以
核对同版本/业务时钟/初态语义，不复活旧30秒观察或Q18许可，不计新模型回答。
工程补步、原建议支持、独立SQL终态分列；不产生LLM完整任务或隔离验收结论。
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psycopg

from scripts import round20_sandbox as sandbox
from scripts.d3_evaluate import NoModelTraffic, business_snapshot
from scripts.llm_round19 import digest
from scripts.round21_explore import configuration, ordinary_session, require, sql_evidence, write
from scripts.round23_consult import check_sql
from services.business.app import create_app
from services.business.db import connect, read_snapshot
from services.business.manage import reset_test_database
from services.business.worker import process_payment, process_ticket
from services.guide.assistant import corporate_export_scope
from services.guide.budget import BudgetLedger
from services.guide.exploration import BoundedExploration, ExplorationError, Recipient, REFS, TARGET
from services.guide.knowledge import fingerprint
from services.guide.model import ModelResult, TokenUsage
from services.guide.releases import Releases

SOURCE = ROOT / '.local/d3-26'
OUT = ROOT / '.local/d3-27/replay'
BINDING = '/__round27/binding'
SERVER_SCOPE = 'P27-explicit-engineering-paid-reply-replay'
EXPECTED_SCOPES = (
    'c4689d336ed18fb1724aa19ebcb4f3f992286948fa0f061c02a0029337c6218e',
    '60e1ce7b382ec0f9ca3ccdcd1a0bf1680e76d7a6229e8af99c6e220b935d1e7e',
)
REQUIRED_CLAIMS = (
    {'first_lock_trip_1_from_visible_discounts_200_and_120',
     'coupon_attribution_and_second_lock_need_recheck'},
    {'trip_2_current_held_lock_owns_coupon',
     'cancellation_only_if_unpaid_allowed_and_coupon_not_consumed',
     'release_then_recheck_inventory_and_expiry_before_reordering'},
)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def ledger_fingerprint():
    """只读原账本及所有既有侧车，任何新增/删除/改字节都使回放失败。"""
    paths = sorted((ROOT / '.local').glob('guide-usage.json*'))
    require(len(paths) >= 9 and all(p.is_file() for p in paths), 'p27_ledger_or_sidecars_missing')
    return {p.name: sha(p) for p in paths}


def source_bundle():
    """核对原批准→精确scope→raw输入/候选→正式交付→review→逐笔账本全链。"""
    current = BudgetLedger(ROOT / '.local/guide-usage.json')
    rows = current.receipt_snapshot()
    require(len(rows) == 26 and all(r['state'] == 'settled' for r in rows), 'p27_settled_26_required')
    ledger = read(current.path)
    after = read(SOURCE / 'snapshot-after.json')
    require(after['files']['ledger_request_hashes'] == [digest(row) for row in rows] and
            after['files']['ledger_identity'] == digest({k: v for k, v in ledger.items() if k != 'requests'}),
            'p27_original_26_or_identity_changed')
    for relative, expected in after['ledger_sidecars'].items():
        require(sha(ROOT / relative) == expected, 'p27_original_ledger_sidecar_changed')
    for batch, count in (('q16', 23), ('q17', 24), ('q18', 26)):
        stopped = read(getattr(current, batch + '_stop_path'))
        require(stopped['ledger_id'] == ledger['ledger_id'] and stopped['physical_requests'] == count,
                'p27_original_batch_not_stopped')
    decision = read(SOURCE / 'decision.json')
    require(decision['package'] == 'Q18' and decision['approved'] is True and
            decision['source_kind'] == 'explicit_user_reply' and decision['approved_on'] == '2026-10-18' and
            decision['source_reply'] == '批准Q18紧凑512：上述两份精确合成输入，最多新增2次，会计预留共0.064元' and
            decision['destination'] == 'https://api.deepseek.com/chat/completions' and
            decision['max_input_bytes'] == 12288 and
            decision['scope_hashes'] == list(EXPECTED_SCOPES) and
            type(decision['max_physical_requests']) is int and decision['max_physical_requests'] == 2 and
            decision['original_used'] == 24 and decision['scoped_ceiling'] == 26 and
            decision['max_output_tokens_each'] == 512 and decision['automatic_retry'] is False and
            decision['failure_stops_remaining'] is True and
            decision['accounting_reserve_each_cny'] == '0.032' and
            decision['accounting_reserve_total_cny'] == '0.064', 'p27_original_approval_changed')
    approval = read(SOURCE / 'shared-export-approval.json')
    require(approval == {'package': 'P26-Q18-two', 'approved': True,
        'source_kind': 'explicit_user_reply', 'scope_hashes': list(EXPECTED_SCOPES)},
        'p27_original_export_approval_changed')
    paths = [SOURCE / name for name in ('decision.json', 'shared-export-approval.json',
             'snapshot-after.json', 'releases.json')]
    samples, bindings = [], []
    for index, scope in enumerate(EXPECTED_SCOPES, 1):
        names = [f'{kind}-{index}.json' for kind in ('payload-live', 'sample', 'raw-result')]
        names += [f'sample-{index}-reviewed.json', f'sample-{index}-sql-check.json', f'attempt-{index}.json']
        paths += [SOURCE / name for name in names]
        payload = read(SOURCE / f'payload-live-{index}.json')
        sample = read(SOURCE / f'sample-{index}.json')
        raw = read(SOURCE / f'raw-result-{index}.json')
        review = read(SOURCE / f'sample-{index}-reviewed.json')
        sql_check = read(SOURCE / f'sample-{index}-sql-check.json')
        attempt = read(SOURCE / f'attempt-{index}.json')
        report_path = (ROOT / raw['report']).resolve()
        require(report_path.is_relative_to((SOURCE / 'live-runs').resolve()) and report_path.suffix == '.json',
                'p27_raw_report_path_outside_original_round')
        paths.append(report_path)
        report = read(report_path)
        value = payload['payload']
        context = json.loads(value['messages'][-1]['content'])
        require(payload['scope_hash'] == scope == corporate_export_scope(value) and
                value['max_tokens'] == 512 and value['json_output'] is False and context['history'] == [] and
                report['input'] == value and report['input_hash'] == fingerprint(value) and
                attempt['input_hash'] == fingerprint(value) and attempt['scope_hash'] == scope and
                attempt['sample'] == index and attempt['requests_before'] == 23 + index and
                attempt['automatic_retry'] is False, 'p27_original_scope_or_input_binding_changed')
        require(raw['report_sha256'] == sha(report_path) and raw['scope_hash'] == scope and
                raw['raw_model_received'] is True and raw['ledger_row_matched'] is True and
                report['status'] == 'received' and report['kind'] == 'chat' and
                report['prompt_version'] == report['inference_prompt'] == 'assistant-6' and
                report['candidate']['tool_calls'] == [] and
                report['candidate']['content'] == sample['reply']['reply'] and
                report['release_version'] == sample['reply']['release_version'],
                'p27_original_raw_or_formal_reply_changed')
        require(sample['sample'] == index and sample['http'] == 200 and sample['real_model'] is True and
                sample['business_unchanged'] is True and
                sample['business_before']['tables'] == sample['business_after']['tables'] and
                sample['business_before']['sha256'] == sample['business_after']['sha256'] and
                sample['raw_model_result'] == f'raw-result-{index}.json' and
                sql_check['passed'] is True and sql_check['unpaid_unticketed'] is True and
                sql_check['sample_sha256'] == sha(SOURCE / f'sample-{index}.json') and
                sql_check['scope_hash'] == scope, 'p27_original_delivery_or_sql_binding_changed')
        original_ids = [str(r['id']) for r in sample['sql']['trips']]
        check_sql(sample['sql'], original_ids, changed=index == 2)
        require(all(review.get(key) is True for key in ('passed', 'known_usage', 'business_unchanged',
                    'independent_sql', 'content_reviewed')) and review['scope_hash'] == scope and
                review['reply_hash'] == fingerprint(sample['reply']) and
                REQUIRED_CLAIMS[index - 1] <= set(review['supported_claims']),
                'p27_original_independent_content_review_changed')
        row, usage = rows[23 + index], raw['usage']
        require(raw['ledger_request_id'] == row['request_id'] and
                raw['provider_request_id_sha256'] == hashlib.sha256(row['provider_request_id'].encode()).hexdigest() and
                usage == report['usage'] and usage['prompt_tokens'] == row['prompt_tokens'] and
                usage['completion_tokens'] == row['completion_tokens'] and
                usage['cached_tokens'] == row['cache_hit_tokens'] and
                usage['cache_miss_tokens'] == row['cache_miss_tokens'] and
                usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'] and
                usage['cached_tokens'] + usage['cache_miss_tokens'] == usage['prompt_tokens'] and
                raw['amount_cny'] == report['amount_cny'] == row['accounted_cny'] and
                Decimal(review['recomputed_accounted_cny']) == Decimal(row['accounted_cny']),
                'p27_original_paid_usage_binding_changed')
        samples.append({'sample': sample, 'payload': value, 'context': context,
                        'candidate': report['candidate'], 'review': review})
        bindings.append({'source_sample': index, 'scope_hash': scope,
            'original_reply_hash': fingerprint(sample['reply']), 'ledger_request_id': row['request_id'],
            'reply_text_sha256': hashlib.sha256(sample['reply']['reply'].encode()).hexdigest(),
            'known_usage_and_paid_binding': True})
    manifest = {'source_round': 26, 'source_kind': 'original_paid_candidates_and_formal_deliveries',
        'current_mode': 'offline_engineering_replay', 'new_model_calls': 0, 'new_permits': 0,
        'old_observation_ttl_valid': False, 'old_authorization_reused': False,
        'files': {p.relative_to(ROOT).as_posix(): sha(p) for p in paths},
        'samples': bindings, 'ledger_fingerprint': ledger_fingerprint()}
    return {'manifest': manifest, 'samples': samples}


def configure():
    require(not any((OUT / name).exists() for name in ('server.json', 'server.stop',
        'source-binding.json', 'releases.json', 'run-started.json')), 'p27_server_no_restart_or_overwrite')
    bundle = source_bundle()
    OUT.mkdir(parents=True, exist_ok=True)
    _, settings = configuration()
    app = create_app(settings)
    with (OUT / 'releases.json').open('xb') as stream:
        stream.write((SOURCE / 'releases.json').read_bytes())
    assistant = app.state.assistant
    assistant.releases = Releases('test', OUT / 'releases.json')
    require(assistant.releases.active()['prompt_version'] == 'assistant-6', 'p27_original_knowledge_required')
    assistant.external_model = False
    assistant.report_dir = OUT / 'offline-replay-runs'
    old_status = assistant.status
    assistant.status = lambda who: {**old_status(who), 'ready': True, 'reason': None}
    # 两个独立工程分支均创建新会话；B仍须保留26已核的总体goal。
    # 不复活旧session/history/TTL；只接受来源中两个精确问题及对应已核goal。
    old_session = assistant.session
    def engineering_session(sid, who, version, question):
        matches = [s for s in bundle['samples'] if s['context']['question'] == question]
        require(sid is None and len(matches) == 1 and
                version == matches[0]['sample']['reply']['release_version'],
                'p27_only_verified_new_engineering_sessions')
        return old_session(None, who, version, matches[0]['context']['goal'])
    assistant.session = engineering_session
    def factory(validator):
        async def generate(value, deadline):
            payload = validator(value)
            scope = corporate_export_scope(payload)
            require(scope in EXPECTED_SCOPES, 'p27_current_semantic_scope_mismatch')
            index = EXPECTED_SCOPES.index(scope)
            require(payload['max_tokens'] == 512 and payload['json_output'] is False and
                    json.loads(payload['messages'][-1]['content'])['history'] == [],
                    'p27_current_replay_contract_changed')
            save(f'current-payload-{index + 1}.json', {'mode': 'offline_replay_not_new_llm',
                'source_sample': index + 1, 'source_scope_hash': scope, 'current_semantic_scope_hash': scope,
                'original_observation_ttl_not_reused': True, 'payload': payload,
                'session_goal_reconstructed_from_verified_source': True,
                'source_binding_sha256': sha(OUT / 'source-binding.json')})
            return ModelResult(bundle['samples'][index]['candidate'], TokenUsage(0, 0, 0, 0, 0),
                               'offline-engineering-replay', Decimal(0), 'offline-engineering-replay')
        return SimpleNamespace(generate_experiment=generate)
    assistant.adapter_factory = factory
    marker = {'scope': SERVER_SCOPE, 'instance': sandbox.check_cluster()['instance_id'],
        'runtime': uuid4().hex, 'mode': 'explicit_engineering_no_model_no_isolation_claim',
        'scope_hashes': list(EXPECTED_SCOPES), 'source_binding_sha256': None}
    save('source-binding.json', bundle['manifest'])
    marker['source_binding_sha256'] = sha(OUT / 'source-binding.json')
    @app.get(BINDING)
    def binding():
        return marker
    save('server.json', marker)
    return app


def server():
    import uvicorn
    OUT.mkdir(parents=True, exist_ok=True)
    guard = NoModelTraffic()
    try:
        with guard.installed():
            app = configure()
            instance = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8000,
                                      access_log=False, proxy_headers=False))
            async def serve():
                task = asyncio.create_task(instance.serve())
                while not task.done():
                    if (OUT / 'server.stop').exists(): instance.should_exit = True
                    await asyncio.sleep(.2)
                await task
            asyncio.run(serve())
    finally:
        save('server-transport-guard.json', {'model_send_attempts': guard.send_attempts,
             'budget_reserve_attempts': guard.reserve_attempts, 'model': 'none', 'offline_replay': True})


def terminal_sql(settings):
    evidence = sql_evidence(settings)
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        conn.execute('SET TRANSACTION READ ONLY')
        require(conn.execute("SELECT current_setting('port') AS port").fetchone()['port'] == '55433',
                'p27_terminal_sql_cluster_mismatch')
        evidence['tickets'] = conn.execute('''SELECT id,trip_id,trip_revision,payment_id,task_id,quantity,
            (length(certificate)>0) AS certificate_present FROM tickets ORDER BY id''').fetchall()
    return evidence


def judge_terminal(evidence, trip_ids):
    """仅独立判据持预期140000；不作为动作输入/模型上下文或动态规划依据。"""
    require(len(evidence['trips']) == 2 and {str(t['id']) for t in evidence['trips']} == set(trip_ids),
            'p27_judge_trip_scope_failed')
    for key in ('payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets'):
        require(len(evidence[key]) == 2, 'p27_judge_unique_terminal_rows_failed')
    payments = {str(r['id']): r for r in evidence['payments']}
    jobs = {str(r['payment_id']): r for r in evidence['payment_jobs']}
    providers = {str(r['operation_id']): r for r in evidence['provider_payments']}
    tasks = {str(r['id']): r for r in evidence['ticket_tasks']}
    trips = {str(r['id']): r for r in evidence['trips']}
    locks = {str(r['id']): r for r in evidence['locks']}
    require(len(payments) == len(jobs) == len(providers) == len(tasks) == 2 and
            len({str(r['id']) for r in evidence['tickets']}) == 2 and
            all(r['owner_id'] == 'personal_alice' for r in trips.values()) and
            {str(p['trip_id']) for p in payments.values()} == set(trip_ids) and
            set(jobs) == set(providers) == set(payments), 'p27_judge_duplicate_or_association_failed')
    for ticket in evidence['tickets']:
        trip, payment, task = trips[str(ticket['trip_id'])], payments[str(ticket['payment_id'])], tasks[str(ticket['task_id'])]
        lock = locks.get(str(payment['lock_id']))
        require(str(payment['trip_id']) == str(task['trip_id']) == str(ticket['trip_id']) and
                str(task['payment_id']) == str(payment['id']) and trip['revision'] == ticket['trip_revision'] and
                ticket['quantity'] == 1 and ticket['certificate_present'] is True and trip['status'] == 'paid' and
                payment['status'] == task['status'] == 'succeeded' and jobs[str(payment['id'])]['status'] == 'done' and
                providers[str(payment['id'])]['outcome'] == 'succeeded' and
                providers[str(payment['id'])]['captured_minor'] == payment['amount_minor'],
                'p27_judge_ticket_payment_job_task_failed')
        require(lock is not None and str(lock['trip_id']) == str(trip['id']) and
                lock['trip_revision'] == trip['revision'] and lock['status'] == 'consumed' and
                lock['amount_minor'] == payment['amount_minor'], 'p27_judge_current_paid_lock_failed')
    require({str(t['trip_id']) for t in evidence['tickets']} == set(trip_ids) and
            sum(r['captured_minor'] for r in providers.values()) == 140000,
            'p27_judge_actual_capture_total_failed')
    coupon = evidence['coupons']
    require(len(coupon) == 1 and coupon[0]['status'] == 'consumed', 'p27_judge_coupon_not_consumed')
    owner_locks = [r for r in evidence['locks'] if r['coupon_id'] == coupon[0]['id'] and r['status'] == 'consumed']
    require(len(owner_locks) == 1 and str(owner_locks[0]['trip_id']) == trip_ids[0] and
            owner_locks[0]['id'] == coupon[0]['lock_id'], 'p27_judge_coupon_attribution_failed')
    require([r['count'] for r in evidence['coupon_events'] if r['action'] == 'coupon.consumed'] == [1],
            'p27_judge_coupon_consumed_twice')
    require(len(evidence['flights']) == 2 and
            {r['id'] for r in evidence['flights']} == {r['flight_id'] for r in trips.values()} and
            all(r['capacity'] == 2 and r['reserved'] == 0 and r['sold'] == 1
            for r in evidence['flights']), 'p27_judge_inventory_conservation_failed')
    return {'passed': True, 'tickets': 2, 'payments': 2, 'payment_jobs': 2, 'ticket_tasks': 2,
        'captured_minor': 140000, 'coupon_consumed': 1, 'inventory_conserved': True,
        'method': 'independent_read_only_sql', 'ai_task_complete': False}


def unpaid_cancellation(client, headers, settings, trip_id):
    """实际选择取消由工程评测器作出；原第二答仅支持有条件考虑这条恢复路径。"""
    before_sql, before = terminal_sql(settings), business_snapshot(settings)
    response = client.get(f'/trips/{trip_id}/booking', headers=headers)
    require(response.status_code == 200, 'p27_unpaid_booking_failed')
    booking = response.json()
    require(booking['lock']['status'] == 'held' and booking['lock']['valid'] is True and
            booking['payment'] is None, 'p27_unpaid_cancellation_not_allowed')
    require(len(before_sql['coupons']) == 1 and before_sql['coupons'][0]['status'] == 'held' and
            str(before_sql['coupons'][0]['lock_id']) == booking['lock']['id'] and
            any(str(r['id']) == booking['lock']['id'] and str(r['trip_id']) == str(trip_id) and
                r['status'] == 'held' for r in before_sql['locks']),
            'p27_unpaid_original_coupon_lock_mismatch')
    response = client.post(f"/locks/{booking['lock']['id']}/cancel",
        headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    require(response.status_code == 200 and response.json()['status'] == 'cancelled',
            'p27_unpaid_cancellation_http_failed')
    after_sql, after = terminal_sql(settings), business_snapshot(settings)
    require(after_sql['coupons'][0]['status'] == 'available' and after_sql['coupons'][0]['lock_id'] is None and
            all(r['reserved'] == r['sold'] == 0 for r in after_sql['flights']) and
            any(str(r['id']) == booking['lock']['id'] and r['status'] == 'cancelled' for r in after_sql['locks']) and
            not any(after_sql[key] for key in ('payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets')),
            'p27_unpaid_cancellation_release_failed')
    return {'condition': 'unpaid_held_can_cancel', 'choice_by': 'engineering_evaluator',
        'actual_business_http': True, 'method': 'POST', 'path_template': '/locks/{current_lock}/cancel',
        'http': 200, 'unpaid': True, 'coupon_released': True,
        'request_path': response.request.url.path, 'request_json': {},
        'response_json': response.json(), 'response_json_sha256': digest(response.json()),
        'before_business': before, 'after_business': after, 'before_sql': before_sql, 'after_sql': after_sql,
        'before_sql_sha256': digest(before_sql), 'after_sql_sha256': digest(after_sql)}


def cancellation_counterexample(client, headers, settings, trip_id, expected):
    """直接原HTTP取消端点，必须409；两份独立SQL与23表摘要证明无副作用。"""
    require(expected in {'protected', 'consumed'}, 'p27_counterexample_unknown_condition')
    before_sql, before = terminal_sql(settings), business_snapshot(settings)
    booking = client.get(f'/trips/{trip_id}/booking', headers=headers)
    require(booking.status_code == 200, 'p27_counterexample_booking_failed')
    current = booking.json()
    require(current['lock']['status'] == expected and current['payment']['status'] ==
            ('processing' if expected == 'protected' else 'succeeded'), 'p27_counterexample_state_mismatch')
    response = client.post(f"/locks/{current['lock']['id']}/cancel",
        headers={**headers, 'Idempotency-Key': uuid4().hex}, json={})
    require(response.status_code == 409 and response.json() == {'error': 'payment_protected'},
            'p27_counterexample_http_not_rejected')
    after_sql, after = terminal_sql(settings), business_snapshot(settings)
    require(before['sha256'] == after['sha256'] and digest(before_sql) == digest(after_sql),
            'p27_counterexample_business_changed')
    return {'condition': 'payment_processing_protected' if expected == 'protected' else 'coupon_consumed',
        'actual_business_http': True, 'method': 'POST', 'path_template': '/locks/{current_lock}/cancel',
        'http': 409, 'error': 'payment_protected', 'business_unchanged': True,
        'request_path': response.request.url.path, 'request_json': {},
        'response_json': response.json(), 'response_json_sha256': digest(response.json()),
        'before_business': before, 'after_business': after, 'before_sql': before_sql, 'after_sql': after_sql,
        'before_sql_sha256': digest(before_sql), 'after_sql_sha256': digest(after_sql)}


def advance_workers(settings, trip_id):
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        conn.execute('SET TRANSACTION READ ONLY')
        require(conn.execute("SELECT current_setting('port') AS port").fetchone()['port'] == '55433',
                'p27_worker_cluster_mismatch')
        rows = conn.execute("SELECT id FROM payments WHERE trip_id=%s AND status='processing'", (trip_id,)).fetchall()
    require(len(rows) == 1, 'p27_engineering_worker_expected_one_payment')
    paid = process_payment(settings.database_url, 'test', rows[0]['id'])
    require(paid['status'] == 'succeeded', 'p27_engineering_payment_failed')
    issued = process_ticket(settings.database_url, 'test', paid['ticket_task_id'])
    require(issued['status'] == 'succeeded', 'p27_engineering_ticket_failed')
    return {'operation': 'worker.payment_and_ticket', 'attribution': 'engineer_completion',
            'payment_status': paid['status'], 'ticket_task_status': issued['status']}


def replay_current_candidate(client, headers, settings, trip_ids, index, source):
    before = business_snapshot(settings)
    context = source['context']
    public = context['current_observation']
    body = {'message': context['question'], 'page': context['current_page'], 'trip_id': trip_ids[0],
        'delegated_trip_ids': list(trip_ids), 'observation': {k: v for k, v in public.items() if k != 'source_kind'}}
    response = client.post('/assistant/chat', headers=headers, json=body)
    after = business_snapshot(settings)
    require(response.status_code == 200 and response.json()['reply'] == source['sample']['reply']['reply'],
            'p27_offline_original_candidate_guard_replay_failed')
    require(before['sha256'] == after['sha256'], 'p27_offline_consult_changed_business')
    captured = read(OUT / f'current-payload-{index}.json')
    require(captured['mode'] == 'offline_replay_not_new_llm' and
            captured['source_scope_hash'] == captured['current_semantic_scope_hash'] == EXPECTED_SCOPES[index - 1],
            'p27_current_semantic_mapping_failed')
    result = {'source_sample': index, 'mode': 'offline_replay_not_new_llm', 'http': 200,
        'same_version_business_clock_and_initial_semantics': True, 'semantic_scope_match': True,
        'scope_hash': EXPECTED_SCOPES[index - 1], 'old_ttl_or_authorization_reused': False,
        'opaque_ids': 'new_synthetic_ids_are_anonymized_by_existing_projection',
        'capture_time': 'new_capture_clock_not_old_observation_revival', 'business_unchanged': True,
        'session_goal_reconstructed_from_verified_source': True, 'session_history': 0,
        'source_goal_sha256': hashlib.sha256(context['goal'].encode()).hexdigest(),
        'reply_text_equal': True, 'current_payload_sha256': sha(OUT / f'current-payload-{index}.json'),
        'formal_replay_reply': response.json(), 'business_before': before, 'business_after': after}
    save(f'branch-{index}-semantic-mapping.json', result)
    return result


class HTTPJournal:
    """仅记业务HTTP关联/哈希；认证请求与凭证、完整响应均不落盘。"""
    def __init__(self):
        self.events = []
        self.branch = None

    def response(self, response):
        request = response.request
        if request.url.path in {'/demo/sessions', '/session'}:
            return
        response.read()
        try:
            response_hash = digest(response.json())
        except ValueError:
            response_hash = None
        try:
            request_hash = digest(json.loads(request.content)) if request.content else None
        except ValueError:
            request_hash = None
        self.events.append({'sequence': len(self.events) + 1, 'branch': self.branch,
            'method': request.method, 'path': request.url.path, 'status_code': response.status_code,
            'request_json_sha256': request_hash, 'response_json_sha256': response_hash,
            'response_bytes_sha256': hashlib.sha256(response.content).hexdigest()})


def run():
    require(not any((OUT / n).exists() for n in ('run-started.json', 'summary.json', 'controller-transport-guard.json')),
            'p27_run_no_restart_or_overwrite')
    bundle = source_bundle()
    require(read(OUT / 'source-binding.json') == bundle['manifest'], 'p27_source_binding_changed_before_run')
    config, settings = configuration()
    guard = NoModelTraffic()
    journal = HTTPJournal()
    before_ledger = ledger_fingerprint()
    summary = {'complete': False, 'engineering_replay': True, 'model_calls': 0, 'new_permits': 0,
        'ai_task_complete': False, 'executor_isolation_accepted': False, 'llm_continuous_planning': False,
        'source_binding_sha256': sha(OUT / 'source-binding.json'), 'branches': [],
        'ledger_before': before_ledger, 'http': 'real_tcp_127.0.0.1_8000', 'sql': 'independent_55433_only'}
    save('run-started.json', {'engineering_replay': True, 'model_calls': 0, 'new_permits': 0})
    try:
        with sandbox.runner_lock(), guard.installed(), httpx.Client(base_url=TARGET, trust_env=False,
                follow_redirects=False, timeout=35, event_hooks={'response': [journal.response]}) as client:
            marker = read(OUT / 'server.json')
            response = client.get(BINDING)
            require(response.status_code == 200 and response.json() == marker and
                    marker['scope'] == SERVER_SCOPE and marker['instance'] == sandbox.check_cluster()['instance_id'] and
                    marker['source_binding_sha256'] == summary['source_binding_sha256'], 'p27_server_binding_mismatch')
            with ordinary_session(client, settings): pass
            for index, source in enumerate(bundle['samples'], 1):
                journal.branch = index
                reset_test_database(config['test_database_url'], confirmation='llm_business_test', fixture='two-trips-one-coupon')
                actions, counters = [], []
                with ordinary_session(client, settings) as headers:
                    write(client, headers, '/payment-profile', {'source_ref': 'demo_personal_alice', 'expected_revision': 0}, 'PUT')
                    trip_ids = tuple(write(client, headers, '/trips', {'flight_id': 'DEMO-' + size,
                        'travel_date': '2026-10-01', 'passengers': 1})['id'] for size in ('LARGE', 'SMALL'))
                    quotes = [write(client, headers, f'/trips/{trip}/quotes', {'expected_revision': 1}) for trip in trip_ids]
                    if index == 2:
                        write(client, headers, f"/quotes/{quotes[1]['id']}/lock")
                    initial_sql = terminal_sql(settings)
                    check_sql(initial_sql, trip_ids, changed=index == 2)
                    save(f'branch-{index}-initial-sql.json', initial_sql)
                    mapping = replay_current_candidate(client, headers, settings, trip_ids, index, source)
                    recipient = Recipient('engineering-replay-reviewer', uuid4().hex, 1)
                    session = BoundedExploration(client, headers, trip_ids, recipient, lambda: recipient)
                    try:
                        def act(name, target=None, *, supported=False):
                            http_start = len(journal.events) + 1
                            record = session.act({'operation': name, **({'target': target} if target else {})})
                            require(record['result'] == ('observed' if name == 'observe' else 'accepted'),
                                    'p27_engineering_action_not_accepted')
                            actions.append({'operation': name, 'target': target, 'trace_sequence': record['sequence'],
                                'http_sequence_start': http_start, 'http_sequence_end': len(journal.events),
                                'attribution': 'engineering_execution_of_original_model_suggestion' if supported else 'engineer_completion',
                                'source_sample': index if supported else None, 'result': record['result'], 'state': record['state']})
                            return record['state']
                        state = act('observe')
                        if index == 2:
                            require(state['shared_coupon']['status'] == 'held' and state['shared_coupon']['trip_ref'] == 'trip_2' and
                                    all(r['data']['payment'] is None for r in state['delegated_trips']),
                                    'p27_unpaid_cancellation_conditions_missing')
                            counters.append(unpaid_cancellation(client, headers, settings, trip_ids[1]))
                            state = act('observe')
                            actions.append({'operation': 'booking.cancel_lock', 'target': 'trip_2',
                                'attribution': 'engineering_execution_of_original_model_suggestion',
                                'source_sample': index, 'choice_by': 'engineering_evaluator',
                                'execution_channel': 'direct_business_http', 'http': 200, 'result': 'accepted', 'state': state})
                            require(state['shared_coupon']['status'] == 'available', 'p27_unpaid_coupon_not_released')
                            act('booking.quote', 'trip_1', supported=True)
                        state = act('booking.lock', 'trip_1', supported=True)
                        require(state['shared_coupon']['trip_ref'] == 'trip_1' and state['shared_coupon']['status'] == 'held',
                                'p27_current_first_lock_attribution_failed')
                        if index == 2: act('booking.quote', 'trip_2')
                        act('booking.lock', 'trip_2', supported=index == 1)
                        for position, ref in enumerate(REFS):
                            act('booking.pay', ref)
                            if position == 0:
                                counters.append(cancellation_counterexample(client, headers, settings, trip_ids[0], 'protected'))
                            actions.append({**advance_workers(settings, trip_ids[position]), 'target': ref})
                            act('observe')
                            if position == 0:
                                counters.append(cancellation_counterexample(client, headers, settings, trip_ids[0], 'consumed'))
                        envelope = session.deliver()
                        final = terminal_sql(settings)
                        verdict = judge_terminal(final, trip_ids)
                        save(f'branch-{index}-actions.json', {'engineering_execution': True, 'new_model_calls': 0,
                            'actions': actions, 'bounded_observation': envelope})
                        save(f'branch-{index}-counterexamples.json', counters)
                        save(f'branch-{index}-final-sql.json', final)
                        item = {'branch': 'A-original-lock-order' if index == 1 else 'B-unpaid-release-reorder',
                            'source_sample': index, 'semantic_scope_match': mapping['semantic_scope_match'],
                            'model_supported_actions': [a for a in actions if a['attribution'] == 'engineering_execution_of_original_model_suggestion'],
                            'engineer_completion_actions': [a for a in actions if a['attribution'] == 'engineer_completion'],
                            'recovery_counterexamples': [{k: v for k, v in c.items() if k not in (
                                'before_business', 'after_business', 'before_sql', 'after_sql')} for c in counters],
                            'independent_verdict': verdict, 'ai_task_complete': False,
                            'cancellation_choice_by': 'engineering_evaluator' if index == 2 else None,
                            'used': envelope['used'],
                            'files': {name: sha(OUT / name) for name in (
                                f'branch-{index}-initial-sql.json', f'branch-{index}-semantic-mapping.json',
                                f'branch-{index}-actions.json', f'branch-{index}-counterexamples.json',
                                f'branch-{index}-final-sql.json')}}
                        summary['branches'].append(item)
                    finally:
                        session.dispose()
            require(guard.send_attempts == guard.reserve_attempts == 0, 'p27_unexpected_model_traffic')
            require(ledger_fingerprint() == before_ledger, 'p27_original_ledger_or_sidecars_changed')
            require(source_bundle()['manifest'] == bundle['manifest'], 'p27_original_source_changed_during_run')
            summary['complete'] = True
    except BaseException as error:
        summary['failure_type'] = type(error).__name__
        raise
    finally:
        summary['ledger_after'] = ledger_fingerprint()
        summary['ledger_unchanged'] = summary['ledger_after'] == before_ledger
        save('controller-transport-guard.json', {'model_send_attempts': guard.send_attempts,
            'budget_reserve_attempts': guard.reserve_attempts, 'model': 'none', 'offline_replay': True})
        save('http-evidence.json', {'mode': 'real_business_http_hashes_only',
            'authentication_requests_omitted': True, 'events': journal.events})
        summary['http_evidence_sha256'] = sha(OUT / 'http-evidence.json')
        save('summary.json', summary)
    print('P27两分支真实业务HTTP/独立SQL工程回放通过；模型0/许可0，不计LLM完整任务成功。', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('server', 'run'))
    parser.add_argument('--output-directory', type=Path, default=OUT)
    args = parser.parse_args()
    selected = args.output_directory if args.output_directory.is_absolute() else ROOT / args.output_directory
    require(selected.resolve() == selected and selected.is_relative_to(ROOT / '.local/d3-27'),
            'p27_output_must_be_new_round27_project_directory')
    OUT = selected
    try:
        server() if args.action == 'server' else run()
    except (RuntimeError, ExplorationError) as error:
        raise SystemExit(str(error)) from None
    except (psycopg.Error, httpx.HTTPError, ValueError, KeyError, OSError):
        raise SystemExit('P27工程回放失败；检查新目录证据/55433/专用服务，不输出凭证或完整响应。') from None
