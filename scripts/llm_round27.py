"""P27 零模型工程回放的原数据/全部账本/旧原件保全与交付审计。"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.llm_round19 import protection, sha, read
OUT = ROOT / '.local/d3-27'


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def historical():
    # 仅实际运行中的PG数据和追加日志可变；旧运行报告、测试原件及sandbox配置均保全。
    return {p.relative_to(ROOT).as_posix(): sha(p) for n in range(19, 27)
            for p in (ROOT / f'.local/d3-{n}').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and not p.is_relative_to(ROOT / '.local/d3-20/sandbox/postgres-data')
            and p != ROOT / '.local/d3-20/sandbox/postgres.log'}


def snapshot(phase):
    from scripts.d3_evaluate import business_snapshot
    from scripts.llm_round17 import terminal
    from services.business.config import load_settings
    prior = read(ROOT / '.local/d3-26/snapshot-after.json') if phase == 'before' else read(OUT / 'snapshot-before.json')
    value = {'captured_at': datetime.now(timezone.utc).isoformat(), 'files': protection(),
        'dev': business_snapshot(load_settings()), 'test': business_snapshot(load_settings(test=True)),
        'terminal': terminal(), 'historical': historical(),
        'old_native': {p: sha(Path(p)) for p in prior['old_native']},
        'ledger_sidecars': {p.relative_to(ROOT).as_posix(): sha(p)
                           for p in sorted((ROOT / '.local').glob('guide-usage.json*')) if p.is_file()}}
    a, b = value['files'], prior['files']
    checks = {
        'original_business': all(value[k]['sha256'] == prior[k]['sha256'] for k in ('dev', 'test')),
        'original_authentication': all(value[k]['authentication'] == prior[k]['authentication'] for k in ('dev', 'test')),
        'original_terminal': value['terminal'] == prior['terminal'],
        'protected_files': a['protected'] == b['protected'],
        'all_26_entries': a['ledger_request_hashes'] == b['ledger_request_hashes'],
        'ledger_identity': a['ledger_identity'] == b['ledger_identity'],
        'ledger_and_all_sidecars': (value['ledger_sidecars'] == prior['ledger_sidecars'] if phase == 'after'
            else all(value['ledger_sidecars'].get(k) == h for k,h in prior['ledger_sidecars'].items())),
        'candidate_and_active': all(a[k] == b[k] for k in ('candidate_hashes', 'active')),
        'old_native': value['old_native'] == prior['old_native'],
        'configuration': all(a['mutable_original_hashes'][k] == b['mutable_original_hashes'][k]
            for k in ('.local/guide-experiment.json', '.local/assistant-releases-test.json',
                      'services/guide/budget.py', 'services/guide/experiment.py'))}
    if phase == 'after':
        checks['all_19_to_26_artifacts'] = value['historical'] == prior['historical']
    else:
        checks['previous_history_still_valid'] = all(value['historical'].get(k) == v for k, v in prior['historical'].items())
    assert len(value['dev']['tables']) == len(value['test']['tables']) == 23
    assert value['dev']['tables']['tickets']['rows'] == 6
    assert a['budget']['physical_requests'] == 26 and a['budget']['unresolved_requests'] == 0
    assert a['budget']['accounted_cny'] == '0.09024104'
    if phase == 'before':
        extra = set(value['ledger_sidecars']) - set(prior['ledger_sidecars'])
        assert extra == {'.local/guide-usage.json.q18-stopped.json'}
        stop = read(ROOT / '.local/guide-usage.json.q18-stopped.json')
        ledger_value = read(ROOT / '.local/guide-usage.json')
        assert stop['reason'] == 'completed' and stop['physical_requests'] == 26
        assert stop['ledger_id'] == ledger_value['ledger_id']
    value['checks'] = checks
    assert all(checks.values()), 'P27_preservation_failed'
    save(f'snapshot-{phase}.json', value)
    print(json.dumps({'phase': phase, 'checks': checks, 'budget': a['budget'],
                      'historical_files': len(value['historical'])}, ensure_ascii=False))


def stopped_checks():
    from services.guide.budget import BudgetLedger, BudgetUnavailable
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    scopes = read(ROOT / '.local/d3-24/decision.json')['scope_hashes']
    result = {}
    for label, fn, scope in (
        ('q16', ledger.check_q16_scope, scopes[1]),
        ('q17_first', ledger.check_q17_scope, scopes[0]),
        ('q17_second', ledger.check_q17_scope, scopes[1])):
        try:
            fn(scope)
        except BudgetUnavailable as error:
            result[label] = error.reason
        else:
            raise AssertionError('old_batch_reopened')
    assert result == {'q16': 'q16_batch_stopped',
                      'q17_first': 'q17_batch_stopped', 'q17_second': 'q17_batch_stopped'}
    for index,scope in enumerate(read(ROOT/'.local/d3-26/decision.json')['scope_hashes'],1):
        try:ledger.check_q18_scope(scope)
        except BudgetUnavailable as error:result[f'q18_{index}']=error.reason
        else:raise AssertionError('q18_batch_reopened')
        assert result[f'q18_{index}']=='q18_batch_stopped'
    return result



def audit():
    """只读原始文件独立重算；不调用回放自己的judge/source_bundle或连接PG。"""
    import hashlib
    from copy import deepcopy
    from services.guide.budget import BudgetLedger

    def canonical(value, *, ascii=False):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=ascii,
            separators=(',', ':'), default=str).encode()).hexdigest()

    def same_business(left, right):
        assert len(left['tables']) == len(right['tables']) == 23
        assert left['tables'] == right['tables']
        assert left['sha256'] == canonical(left['tables']) == right['sha256']

    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in {'observed_at', 'valid_until', 'captured_at'}}
        return [stable(item) for item in value] if isinstance(value, list) else value

    def semantic(payload):
        value = deepcopy(payload)
        for message in value['messages']:
            if message['role'] == 'user':
                message['content'] = stable(json.loads(message['content']))
        return canonical(value, ascii=True)

    def initial_sql(value, *, second_held):
        assert value['workspace']['fixture_key'] == 'two-trips-one-coupon'
        assert value['workspace']['product_version'] == 'v1'
        trips = {row['flight_id']: row for row in value['trips']}
        assert set(trips) == {'DEMO-LARGE', 'DEMO-SMALL'} and len(value['trips']) == 2
        ids = [str(trips[key]['id']) for key in ('DEMO-LARGE', 'DEMO-SMALL')]
        assert len(set(ids)) == 2 and all(t['owner_id'] == 'personal_alice' and
            t['revision'] == 1 and t['status'] == 'draft' for t in trips.values())
        assert all(not value[key] for key in ('payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets'))
        assert len(value['coupons']) == 1
        coupon = value['coupons'][0]
        assert coupon['owner_id'] == 'personal_alice' and coupon['percent'] == 20 and coupon['cap_minor'] == 20000
        assert coupon['status'] == ('held' if second_held else 'available')
        if second_held:
            assert len(value['locks']) == 1
            lock = value['locks'][0]
            assert str(lock['trip_id']) == ids[1] and lock['trip_revision'] == 1 and lock['status'] == 'held'
            assert lock['id'] == coupon['lock_id'] and lock['coupon_id'] == coupon['id']
            assert lock['amount_minor'] == 48000 and lock['coupon_discount_minor'] == 12000
        else:
            assert value['locks'] == [] and coupon['lock_id'] is None
        assert len(value['flights']) == 2
        for flight in value['flights']:
            assert flight['capacity'] == 2 and flight['sold'] == 0
            assert flight['reserved'] == (1 if second_held and flight['id'] == 'DEMO-SMALL' else 0)
        return ids

    before, after = [read(OUT / f'snapshot-{phase}.json') for phase in ('before', 'after')]
    assert len(before['historical']) == 7493
    # 精确重枚举：不能只验证旧清单而漏掉新增文件。派生pycache与这两个PG运行路径是唯一例外。
    history_now = historical()
    sidecars_now = {p.relative_to(ROOT).as_posix(): sha(p)
        for p in sorted((ROOT / '.local').glob('guide-usage.json*')) if p.is_file()}
    assert all(p.is_file() for p in (ROOT / '.local').glob('guide-usage.json*'))
    assert history_now == before['historical'] == after['historical']
    assert sidecars_now == before['ledger_sidecars'] == after['ledger_sidecars']
    current = protection()
    assert current['protected'] == before['files']['protected'] == after['files']['protected']
    assert current['mutable_original_hashes'] == before['files']['mutable_original_hashes'] == after['files']['mutable_original_hashes']
    assert all(current[key] == before['files'][key] == after['files'][key]
        for key in ('ledger_request_hashes', 'ledger_identity', 'candidate_hashes', 'active', 'budget'))
    assert len(current['ledger_request_hashes']) == 26
    assert all(sha(Path(path)) == expected for path, expected in before['old_native'].items())
    assert before['old_native'] == after['old_native']
    for space in ('dev', 'test'):
        same_business(before[space], after[space])
        assert before[space]['authentication'] == after[space]['authentication']
    assert before['dev']['tables']['tickets']['rows'] == after['dev']['tables']['tickets']['rows'] == 6
    assert before['terminal'] == after['terminal']
    assert all(after['checks'].values())

    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    rows = ledger.receipt_snapshot()
    ledger_value = read(ROOT / '.local/guide-usage.json')
    budget = ledger.summary()
    assert len(rows) == 26 and all(row['state'] == 'settled' for row in rows)
    assert [canonical(row) for row in rows] == before['files']['ledger_request_hashes']
    assert canonical({key: value for key, value in ledger_value.items() if key != 'requests'}) == before['files']['ledger_identity']
    assert budget == before['files']['budget'] == after['files']['budget']
    assert budget['physical_requests'] == 26 and budget['accounted_cny'] == '0.09024104'
    assert budget['unresolved_requests'] == 0 and Decimal(budget['unresolved_cny']) == 0

    replay = OUT / 'replay'
    summary = read(replay / 'summary.json')
    expected_ledger = {Path(path).name: expected for path, expected in sidecars_now.items()}
    assert summary['ledger_before'] == summary['ledger_after'] == expected_ledger
    guards = {}
    for role in ('server', 'controller'):
        guard = read(replay / f'{role}-transport-guard.json')
        assert guard['model_send_attempts'] == guard['budget_reserve_attempts'] == 0
        assert guard['model'] == 'none' and guard['offline_replay'] is True
        guards[role] = {'send_attempts': 0, 'reserve_attempts': 0}
    assert summary['complete'] is True and summary['engineering_replay'] is True
    assert summary['model_calls'] == summary['new_permits'] == 0
    assert summary['ai_task_complete'] is summary['executor_isolation_accepted'] is summary['llm_continuous_planning'] is False
    assert len(summary['branches']) == 2
    binding = read(replay / 'source-binding.json')
    assert sha(replay / 'source-binding.json') == summary['source_binding_sha256']
    assert binding['source_round'] == 26 and binding['source_kind'] == 'original_paid_candidates_and_formal_deliveries'
    assert binding['current_mode'] == 'offline_engineering_replay'
    assert binding['new_model_calls'] == binding['new_permits'] == 0
    assert binding['old_observation_ttl_valid'] is binding['old_authorization_reused'] is False
    assert binding['ledger_fingerprint'] == expected_ledger and len(binding['samples']) == 2
    source = ROOT / '.local/d3-26'
    scopes = ('c4689d336ed18fb1724aa19ebcb4f3f992286948fa0f061c02a0029337c6218e',
              '60e1ce7b382ec0f9ca3ccdcd1a0bf1680e76d7a6229e8af99c6e220b935d1e7e')
    decision = read(source / 'decision.json')
    assert decision['approved'] is True and decision['package'] == 'Q18' and decision['source_kind'] == 'explicit_user_reply'
    assert decision['scope_hashes'] == list(scopes) and decision['max_physical_requests'] == 2
    assert decision['source_reply'] == '批准Q18紧凑512：上述两份精确合成输入，最多新增2次，会计预留共0.064元'
    assert decision['max_output_tokens_each'] == 512 and decision['accounting_reserve_total_cny'] == '0.064'
    assert decision['automatic_retry'] is False and decision['failure_stops_remaining'] is True
    assert decision['destination'] == 'https://api.deepseek.com/chat/completions'
    assert read(source / 'shared-export-approval.json') == {'package': 'P26-Q18-two', 'approved': True,
        'source_kind': 'explicit_user_reply', 'scope_hashes': list(scopes)}
    assert sha(replay / 'releases.json') == sha(source / 'releases.json')
    expected_source_files = {source / name for name in ('decision.json', 'shared-export-approval.json', 'snapshot-after.json', 'releases.json')}
    journal = read(replay / 'http-evidence.json')
    assert summary['http_evidence_sha256'] == sha(replay / 'http-evidence.json')
    assert journal['mode'] == 'real_business_http_hashes_only' and journal['authentication_requests_omitted'] is True
    events = journal['events']
    assert [event['sequence'] for event in events] == list(range(1, len(events) + 1))
    for event in events:
        assert set(event) == {'sequence', 'branch', 'method', 'path', 'status_code', 'request_json_sha256', 'response_json_sha256', 'response_bytes_sha256'}
        assert event['branch'] in (None, 1, 2) and event['path'].startswith('/')
        assert event['path'] not in {'/demo/sessions', '/session'} and event['method'] in {'GET', 'POST', 'PUT'}
        assert event['status_code'] in {200, 201, 202, 409}
        assert all(isinstance(event[key], str) and re.fullmatch('[0-9a-f]{64}', event[key])
            for key in ('response_json_sha256', 'response_bytes_sha256'))
        assert event['request_json_sha256'] is None or re.fullmatch('[0-9a-f]{64}', event['request_json_sha256'])
    marker = read(replay / 'server.json')
    marker_events = [event for event in events if event['path'] == '/__round27/binding']
    assert len(marker_events) == 1 and marker_events[0]['status_code'] == 200
    assert marker_events[0]['response_json_sha256'] == canonical(marker)
    assert marker['scope'] == 'P27-explicit-engineering-paid-reply-replay'
    assert marker['source_binding_sha256'] == summary['source_binding_sha256']
    paid_reviews, branch_reviews = [], []
    offline_reports = list((replay / 'offline-replay-runs').glob('*.json'))
    assert len(offline_reports) == 2
    for index, scope in enumerate(scopes, 1):
        sample = read(source / f'sample-{index}.json')
        payload = read(source / f'payload-live-{index}.json')
        raw = read(source / f'raw-result-{index}.json')
        review = read(source / f'sample-{index}-reviewed.json')
        sql_check = read(source / f'sample-{index}-sql-check.json')
        attempt = read(source / f'attempt-{index}.json')
        for name in (f'sample-{index}.json', f'payload-live-{index}.json', f'raw-result-{index}.json',
                     f'sample-{index}-reviewed.json', f'sample-{index}-sql-check.json', f'attempt-{index}.json'):
            expected_source_files.add(source / name)
        report_path = (ROOT / raw['report']).resolve()
        assert report_path.is_relative_to((source / 'live-runs').resolve()) and report_path.suffix == '.json'
        expected_source_files.add(report_path)
        report, row = read(report_path), rows[23 + index]
        original = payload['payload']
        context = json.loads(original['messages'][-1]['content'])
        assert payload['scope_hash'] == semantic(original) == raw['scope_hash'] == attempt['scope_hash'] == scope
        assert original['max_tokens'] == 512 and original['json_output'] is False and context['history'] == []
        assert report['input'] == original and report['input_hash'] == attempt['input_hash'] == canonical(original, ascii=True)
        assert attempt['sample'] == index and attempt['requests_before'] == 23 + index and attempt['automatic_retry'] is False
        assert raw['report_sha256'] == sha(report_path) and report['status'] == 'received' and report['kind'] == 'chat'
        assert report['prompt_version'] == report['inference_prompt'] == 'assistant-6'
        assert sample['http'] == 200 and sample['real_model'] is True
        assert report['candidate']['content'] == sample['reply']['reply'] and report['candidate']['tool_calls'] == []
        assert report['release_version'] == sample['reply']['release_version']
        same_business(sample['business_before'], sample['business_after'])
        initial_sql(sample['sql'], second_held=index == 2)
        assert sql_check['sample_sha256'] == sha(source / f'sample-{index}.json') and sql_check['scope_hash'] == scope
        assert review['reply_hash'] == canonical(sample['reply'], ascii=True) and review['scope_hash'] == scope
        assert all(review[key] is True for key in ('known_usage', 'business_unchanged', 'independent_sql', 'content_reviewed'))
        expected_claims = ({'first_lock_trip_1_from_visible_discounts_200_and_120', 'coupon_attribution_and_second_lock_need_recheck'}
            if index == 1 else {'trip_2_current_held_lock_owns_coupon', 'cancellation_only_if_unpaid_allowed_and_coupon_not_consumed',
                               'release_then_recheck_inventory_and_expiry_before_reordering'})
        assert expected_claims <= set(review['supported_claims'])
        usage = report['usage']
        assert raw['usage'] == usage and raw['ledger_request_id'] == row['request_id']
        assert raw['provider_request_id_sha256'] == hashlib.sha256(row['provider_request_id'].encode()).hexdigest()
        assert all(type(usage[key]) is int and usage[key] >= 0 for key in usage)
        assert usage['prompt_tokens'] == row['prompt_tokens'] == usage['cached_tokens'] + usage['cache_miss_tokens']
        assert usage['completion_tokens'] == row['completion_tokens'] and 0 < usage['completion_tokens'] < 512
        assert usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens']
        assert usage['cached_tokens'] == row['cache_hit_tokens'] and usage['cache_miss_tokens'] == row['cache_miss_tokens']
        fee = (Decimal(usage['cached_tokens']) * Decimal('0.04') + Decimal(usage['cache_miss_tokens']) * 2 + Decimal(usage['completion_tokens']) * 8) / 1000000
        assert fee == Decimal(row['accounted_cny']) == Decimal(raw['amount_cny']) == Decimal(report['amount_cny']) == Decimal(review['recomputed_accounted_cny'])
        bound = binding['samples'][index - 1]
        assert bound['source_sample'] == index and bound['scope_hash'] == scope and bound['ledger_request_id'] == row['request_id']
        assert bound['original_reply_hash'] == canonical(sample['reply'], ascii=True)
        assert bound['reply_text_sha256'] == hashlib.sha256(sample['reply']['reply'].encode()).hexdigest()
        paid_reviews.append({'source_sample': index, 'scope_hash': scope, 'original_reply_hash': bound['original_reply_hash'],
            'known_usage': usage, 'independent_accounted_cny': str(fee), 'source_report_sha256': sha(report_path)})

        current_payload = read(replay / f'current-payload-{index}.json')
        new = current_payload['payload']
        assert current_payload['mode'] == 'offline_replay_not_new_llm'
        assert current_payload['source_binding_sha256'] == summary['source_binding_sha256']
        assert current_payload['source_scope_hash'] == current_payload['current_semantic_scope_hash'] == semantic(new) == scope
        assert current_payload['original_observation_ttl_not_reused'] is True
        assert current_payload['session_goal_reconstructed_from_verified_source'] is True
        new_context = json.loads(new['messages'][-1]['content'])
        assert stable(new_context) == stable(context)
        assert {key: value for key, value in new.items() if key != 'messages'} == {key: value for key, value in original.items() if key != 'messages'}
        assert new['messages'][:-1] == original['messages'][:-1]
        old_time, capture = [datetime.fromisoformat(value['current_state']['observed_at']) for value in (context, new_context)]
        assert capture > old_time and (datetime.fromisoformat(new_context['current_state']['valid_until']) - capture).total_seconds() == 30
        assert new_context['current_state']['business_time'] == context['current_state']['business_time']
        mapping = read(replay / f'branch-{index}-semantic-mapping.json')
        assert mapping['current_payload_sha256'] == sha(replay / f'current-payload-{index}.json')
        assert mapping['http'] == 200 and mapping['old_ttl_or_authorization_reused'] is False
        assert mapping['session_goal_reconstructed_from_verified_source'] is True and mapping['session_history'] == 0
        assert mapping['source_goal_sha256'] == hashlib.sha256(context['goal'].encode()).hexdigest()
        assert mapping['formal_replay_reply']['reply'] == sample['reply']['reply']
        assert mapping['formal_replay_reply']['release_version'] == sample['reply']['release_version']
        same_business(mapping['business_before'], mapping['business_after'])
        matches = [read(path) for path in offline_reports if read(path).get('input_hash') == canonical(new, ascii=True)]
        assert len(matches) == 1 and matches[0]['input'] == new and matches[0]['candidate'] == report['candidate']
        assert matches[0]['status'] == 'received' and all(value == 0 for value in matches[0]['usage'].values())
        assert Decimal(matches[0]['amount_cny']) == 0

        branch = summary['branches'][index - 1]
        names = [f'branch-{index}-{kind}.json' for kind in ('initial-sql', 'semantic-mapping', 'actions', 'counterexamples', 'final-sql')]
        assert branch['files'] == {name: sha(replay / name) for name in names}
        initial, final = [read(replay / f'branch-{index}-{phase}-sql.json') for phase in ('initial', 'final')]
        ids = initial_sql(initial, second_held=index == 2)
        request_body = {'message': context['question'], 'page': context['current_page'], 'trip_id': ids[0],
            'delegated_trip_ids': ids, 'observation': {key: value for key, value in context['current_observation'].items() if key != 'source_kind'}}
        branch_http = [event for event in events if event['branch'] == index]
        chat_events = [event for event in branch_http if event['method'] == 'POST' and event['path'] == '/assistant/chat']
        assert len(chat_events) == 1 and chat_events[0]['status_code'] == 200
        assert chat_events[0]['request_json_sha256'] == canonical(request_body)
        assert chat_events[0]['response_json_sha256'] == canonical(mapping['formal_replay_reply'])
        assert final['workspace'] == initial['workspace'] == sample['sql']['workspace'] and final['migrations'] == initial['migrations'] == sample['sql']['migrations']
        trips = {str(t['id']): t for t in final['trips']}
        assert len(final['trips']) == len(trips) == 2 and set(trips) == set(ids)
        assert all(trips[str(trip['id'])]['flight_id'] == trip['flight_id'] for trip in initial['trips'])
        for key in ('payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets'):
            assert len(final[key]) == 2
        payments = {str(p['id']): p for p in final['payments']}
        jobs = {str(j['payment_id']): j for j in final['payment_jobs']}
        providers = {str(p['operation_id']): p for p in final['provider_payments']}
        tasks = {str(t['id']): t for t in final['ticket_tasks']}
        locks = {str(lock['id']): lock for lock in final['locks']}
        assert len(locks) == len(final['locks']) == (2 if index == 1 else 3)
        assert len(payments) == len(jobs) == len(providers) == len(tasks) == 2
        assert set(payments) == set(jobs) == set(providers)
        assert {str(p['trip_id']) for p in payments.values()} == set(ids)
        assert len({str(ticket['id']) for ticket in final['tickets']}) == 2
        assert {str(ticket['trip_id']) for ticket in final['tickets']} == set(ids)
        assert {str(ticket['payment_id']) for ticket in final['tickets']} == set(payments)
        assert {str(ticket['task_id']) for ticket in final['tickets']} == set(tasks)
        for ticket in final['tickets']:
            trip, payment, task = trips[str(ticket['trip_id'])], payments[str(ticket['payment_id'])], tasks[str(ticket['task_id'])]
            lock = locks[str(payment['lock_id'])]
            assert ticket['certificate_present'] is True and type(ticket['quantity']) is int and ticket['quantity'] == 1
            assert ticket['trip_revision'] == trip['revision'] == lock['trip_revision']
            assert str(payment['trip_id']) == str(task['trip_id']) == str(lock['trip_id']) == str(ticket['trip_id'])
            assert str(task['payment_id']) == str(payment['id']) and trip['owner_id'] == 'personal_alice'
            assert trip['status'] == 'paid' and payment['status'] == task['status'] == 'succeeded'
            assert jobs[str(payment['id'])]['status'] == 'done' and providers[str(payment['id'])]['outcome'] == 'succeeded'
            assert providers[str(payment['id'])]['captured_minor'] == payment['amount_minor'] == lock['amount_minor'] == (80000 if str(trip['id']) == ids[0] else 60000)
            assert lock['status'] == 'consumed' and lock['coupon_discount_minor'] == (20000 if str(trip['id']) == ids[0] else 0)
        assert sum(row['captured_minor'] for row in providers.values()) == 140000
        assert len(final['coupons']) == 1 and final['coupons'][0]['status'] == 'consumed'
        coupon = final['coupons'][0]
        assert all(coupon[key] == initial['coupons'][0][key] for key in ('id', 'owner_id', 'percent', 'cap_minor'))
        allocated = [lock for lock in final['locks'] if lock['coupon_id'] == coupon['id'] and lock['status'] == 'consumed']
        assert len(allocated) == 1 and str(allocated[0]['trip_id']) == ids[0] and allocated[0]['id'] == coupon['lock_id']
        assert [event['count'] for event in final['coupon_events'] if event['action'] == 'coupon.consumed'] == [1]
        assert len(final['flights']) == 2 and {flight['id'] for flight in final['flights']} == {'DEMO-LARGE', 'DEMO-SMALL'}
        assert all(flight['capacity'] == 2 and flight['reserved'] == 0 and flight['sold'] == 1 for flight in final['flights'])
        assert all(lock['status'] in {'cancelled', 'consumed'} for lock in final['locks'])
        action_file = read(replay / f'branch-{index}-actions.json')
        actions, envelope = action_file['actions'], action_file['bounded_observation']
        assert action_file['engineering_execution'] is True and action_file['new_model_calls'] == 0
        assert envelope['evidence_id'] == canonical({key: value for key, value in envelope.items() if key != 'evidence_id'}, ascii=True)
        assert envelope['executor_isolation']['accepted'] is False and envelope['environment'] == 'synthetic-55433'
        trace = envelope['trace']
        assert [entry['sequence'] for entry in trace] == list(range(1, len(trace) + 1))
        assert envelope['used']['operations'] == len(trace)
        for entry in trace:
            assert entry['result'] == ('observed' if entry['operation']['operation'] == 'observe' else 'accepted')
        semantic_actions = [action for action in actions if action['operation'] != 'worker.payment_and_ticket' and action.get('execution_channel') != 'direct_business_http']
        assert len(semantic_actions) == len(trace)
        for action, entry in zip(semantic_actions, trace):
            assert action['operation'] == entry['operation']['operation'] and action.get('target') == entry['operation'].get('target')
            assert action['state'] == entry['state'] and action['result'] == entry['result']
            assert action['trace_sequence'] == entry['sequence']
            start, end = action['http_sequence_start'], action['http_sequence_end']
            assert type(start) is type(end) is int and start <= end
            window = [event for event in branch_http if start <= event['sequence'] <= end]
            assert len(window) == end - start + 1 and all(event['status_code'] in {200, 201, 202} for event in window)
            writes = [event for event in window if event['method'] != 'GET']
            if action['operation'] == 'observe':
                assert writes == []
            else:
                assert len(writes) == 1 and writes[0]['method'] == 'POST'
                mutation = writes[0]
                position = ('trip_1', 'trip_2').index(action['target'])
                if action['operation'] == 'booking.quote':
                    assert mutation['path'] == f'/trips/{ids[position]}/quotes'
                    expected_body = {'expected_revision': 1}
                elif action['operation'] == 'booking.lock':
                    assert re.fullmatch(r'/quotes/[0-9a-f-]{36}/lock', mutation['path'])
                    expected_body = {}
                else:
                    assert action['operation'] == 'booking.pay'
                    payment = next(payment for payment in payments.values() if str(payment['trip_id']) == ids[position])
                    assert mutation['path'] == f"/locks/{payment['lock_id']}/payments"
                    expected_body = {'amount_minor': payment['amount_minor']}
                assert mutation['request_json_sha256'] == canonical(expected_body)
        additions = [action for action in actions if action['attribution'] == 'engineer_completion']
        assert additions and any(action['operation'] == 'booking.pay' for action in additions)
        assert len([action for action in additions if action['operation'] == 'worker.payment_and_ticket']) == 2
        assert branch['engineer_completion_actions'] == additions and branch['ai_task_complete'] is False
        supported = [action for action in actions if action['attribution'] == 'engineering_execution_of_original_model_suggestion']
        assert branch['model_supported_actions'] == supported
        assert [(action['operation'], action['target']) for action in supported] == (
            [('booking.lock', 'trip_1'), ('booking.lock', 'trip_2')] if index == 1 else
            [('booking.cancel_lock', 'trip_2'), ('booking.quote', 'trip_1'), ('booking.lock', 'trip_1')])
        assert all(action.get('source_sample') == index for action in supported)
        last = trace[-1]['state']
        assert last['shared_coupon']['status'] == 'consumed' and last['shared_coupon']['trip_ref'] == 'trip_1'
        assert all(trip['data']['ticket']['status'] == 'valid' and trip['data']['ticket']['trip_revision'] == 1 and
            trip['data']['payment']['status'] == 'succeeded' for trip in last['delegated_trips'])
        counters = read(replay / f'branch-{index}-counterexamples.json')
        assert [counter['condition'] for counter in counters] == (['unpaid_held_can_cancel'] if index == 2 else []) + ['payment_processing_protected', 'coupon_consumed']
        consumed_http_sequences = set()
        for counter in counters:
            assert counter['actual_business_http'] is True and counter['method'] == 'POST'
            assert counter['before_sql_sha256'] == canonical(counter['before_sql'])
            assert counter['after_sql_sha256'] == canonical(counter['after_sql'])
            assert counter['request_json'] == {} and counter['response_json_sha256'] == canonical(counter['response_json'])
            matches = [event for event in branch_http if event['method'] == 'POST' and event['path'] == counter['request_path']
                and event['status_code'] == counter['http'] and event['request_json_sha256'] == canonical({})
                and event['response_json_sha256'] == canonical(counter['response_json']) and event['sequence'] not in consumed_http_sequences]
            assert matches
            actual_http = matches[0]
            consumed_http_sequences.add(actual_http['sequence'])
            # 原HTTP字节摘要由FastAPI JSONResponse的固定序列化从保存的JSON独立重算。
            assert actual_http['response_bytes_sha256'] == hashlib.sha256(json.dumps(counter['response_json'],
                ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
            if counter['condition'] == 'unpaid_held_can_cancel':
                assert counter['http'] == 200 and counter['choice_by'] == 'engineering_evaluator'
                initial_sql(counter['before_sql'], second_held=True)
                assert counter['request_path'] == f"/locks/{counter['before_sql']['locks'][0]['id']}/cancel"
                assert counter['response_json']['status'] == 'cancelled'
                assert counter['after_sql']['coupons'][0]['status'] == 'available' and counter['after_sql']['coupons'][0]['lock_id'] is None
                assert all(lock['status'] == 'cancelled' for lock in counter['after_sql']['locks'])
                assert all(flight['reserved'] == flight['sold'] == 0 for flight in counter['after_sql']['flights'])
                assert all(not counter['after_sql'][key] for key in ('payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets'))
            else:
                assert counter['http'] == 409 and counter['error'] == 'payment_protected'
                assert counter['response_json'] == {'error': 'payment_protected'}
                same_business(counter['before_business'], counter['after_business'])
                assert counter['before_sql'] == counter['after_sql']
                payment = [payment for payment in counter['before_sql']['payments'] if str(payment['trip_id']) == ids[0]]
                assert len(payment) == 1 and counter['request_path'] == f"/locks/{payment[0]['lock_id']}/cancel"
                assert payment[0]['status'] == ('processing' if counter['condition'] == 'payment_processing_protected' else 'succeeded')
                assert counter['before_sql']['coupons'][0]['status'] == ('protected' if counter['condition'] == 'payment_processing_protected' else 'consumed')
        branch_reviews.append({'source_sample': index, 'current_semantic_scope_hash': scope,
            'current_payload_sha256': sha(replay / f'current-payload-{index}.json'), 'final_sql_sha256': sha(replay / f'branch-{index}-final-sql.json'),
            'independently_recomputed_valid_tickets': 2, 'captured_minor': 140000, 'coupon_consumed': 1,
            'inventory_conserved': True, 'recovery_http_counterexamples': 2, 'engineer_completion_actions': len(additions),
            'http_events': len(branch_http), 'ai_task_complete': False})
    assert binding['files'] == {path.relative_to(ROOT).as_posix(): sha(path) for path in expected_source_files}
    assert all(before['historical'].get(path) == expected for path, expected in binding['files'].items())

    runtime = read(OUT / 'runtime-final.json')
    assert runtime['api_exit'] == runtime['original_postgres_stop_exit'] == runtime['sandbox_postgres_stop_exit'] == 0
    assert runtime['listeners'] == []
    listing = subprocess.check_output(['git', '-c', f"safe.directory={ROOT.as_posix()}",
        'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = sorted({path.decode('utf-8') for path in listing.split(bytes([0])) if path})
    secrets = [read(ROOT / '.local/guide-model.json')['api_key'],
        *read(ROOT / '.local/business.json').values(), *read(ROOT / '.local/d3-20/sandbox/business.json').values()]
    leaks = [path for path in paths if any(secret.encode() in (ROOT / path).read_bytes()
        for secret in secrets if isinstance(secret, str) and len(secret) > 8)]
    assert not leaks
    py, md = [[path for path in paths if path.endswith(suffix)] for suffix in ('.py', '.md')]
    for path in py:
        ast.parse((ROOT / path).read_text(encoding='utf-8-sig'), filename=path)
    links = 0
    for name in md:
        path = ROOT / name
        content = path.read_text(encoding='utf-8-sig')
        assert sum(line.startswith('```') for line in content.splitlines()) % 2 == 0, name
        for match in re.finditer(r'\]\(([^)]+)\)', content):
            target = match[1].strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            target = unquote(target.split('#', 1)[0])
            if target:
                links += 1
                assert (path.parent / target).exists(), (name, target)
    review_evidence = {'method': 'independent_read_only_saved_raw_sql_http_and_trace_recomputation',
        'paid_source_reviews': paid_reviews, 'branch_reviews': branch_reviews, 'model_guards': guards,
        'historical_files_exactly_preserved': len(history_now), 'ledger_sidecars_exactly_preserved': len(sidecars_now),
        'explicit_preservation_exclusions': ['derived __pycache__ files', '.local/d3-20/sandbox/postgres-data/**', '.local/d3-20/sandbox/postgres.log'],
        'audit_model_calls': 0, 'audit_http_calls': 0, 'audit_pg_calls': 0, 'llm_full_task_success': False}
    old_stops = stopped_checks()
    assert {path.relative_to(ROOT).as_posix(): sha(path) for path in (ROOT / '.local').glob('guide-usage.json*')} == sidecars_now
    assert historical() == history_now
    save('independent-replay-review.json', review_evidence)
    result = {'deliverables': len(paths), 'python_ast': len(py), 'markdown': len(md), 'local_links': links,
        'secret_matches': leaks, 'preservation': after['checks'], 'budget': budget,
        'engineering_replay_complete': True, 'llm_full_task_success': False, 'model_calls_by_audit': 0,
        'independent_review_sha256': sha(OUT / 'independent-replay-review.json'),
        'old_stops': old_stops, 'runtime_stopped': True}
    # 停止检查不得创建新侧车或修改旧账本；同样禁止审计时悄悄追加历史原件。
    assert {path.relative_to(ROOT).as_posix(): sha(path) for path in (ROOT / '.local').glob('guide-usage.json*')} == sidecars_now
    assert historical() == history_now
    save('delivery-audit.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('before','after','audit'))
    action=parser.parse_args().action
    audit() if action=='audit' else snapshot(action)
