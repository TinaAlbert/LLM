"""P27临时/Mock反例；不访问现有PG/账本/许可，不计真实HTTP或模型成绩。"""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from scripts import round27_replay as module


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def source_case(tmp_path, monkeypatch):
    source, out = tmp_path / '.local/d3-26', tmp_path / '.local/d3-27/replay'
    source.mkdir(parents=True)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'SOURCE', source)
    monkeypatch.setattr(module, 'OUT', out)
    monkeypatch.setattr(module, 'check_sql', lambda evidence, ids, changed: None)
    ledger_path = tmp_path / '.local/guide-usage.json'
    rows = [{'request_id': str(i), 'state': 'settled'} for i in range(24)]
    usage = {'prompt_tokens': 100, 'completion_tokens': 10, 'cached_tokens': 20,
             'cache_miss_tokens': 80, 'total_tokens': 110}
    for i in (25, 26):
        rows.append({'request_id': str(i), 'provider_request_id': f'synthetic-provider-{i}',
            'state': 'settled', 'prompt_tokens': 100, 'completion_tokens': 10,
            'cache_hit_tokens': 20, 'cache_miss_tokens': 80, 'accounted_cny': '0.0002408'})
    ledger = {'ledger_id': 'synthetic-identity', 'requests': rows}
    put(ledger_path, ledger)
    fake = SimpleNamespace(path=ledger_path, receipt_snapshot=lambda: module.read(ledger_path)['requests'])
    sidecars = []
    for key, count in (('q16', 23), ('q17', 24), ('q18', 26)):
        grant = ledger_path.with_name(ledger_path.name + f'.{key}.json')
        stop = ledger_path.with_name(ledger_path.name + f'.{key}-stopped.json')
        put(grant, {'synthetic': key})
        put(stop, {'ledger_id': ledger['ledger_id'], 'physical_requests': count})
        setattr(fake, key + '_stop_path', stop)
        sidecars += [grant, stop]
    for suffix in ('.lock', '.synthetic-extra'):
        path = ledger_path.with_name(ledger_path.name + suffix)
        path.write_text('synthetic nonsecret', encoding='utf-8')
        sidecars.append(path)
    monkeypatch.setattr(module, 'BudgetLedger', lambda path: fake)
    payloads = []
    for index in (1, 2):
        context = {'goal': '共同总体任务', 'question': f'精确问题{index}', 'history': [],
                   'current_page': 'trips', 'current_observation': {'page': 'trips'}}
        payloads.append({'messages': [{'role': 'system', 'content': '公开知识'},
            {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}],
            'tools': [], 'max_tokens': 512, 'json_output': False})
    scopes = tuple(module.corporate_export_scope(value) for value in payloads)
    monkeypatch.setattr(module, 'EXPECTED_SCOPES', scopes)
    decision = {'package': 'Q18', 'approved': True, 'source_kind': 'explicit_user_reply',
        'approved_on': '2026-10-18', 'source_reply': '批准Q18紧凑512：上述两份精确合成输入，最多新增2次，会计预留共0.064元',
        'destination': 'https://api.deepseek.com/chat/completions', 'max_input_bytes': 12288,
        'scope_hashes': list(scopes), 'max_physical_requests': 2, 'original_used': 24,
        'scoped_ceiling': 26, 'max_output_tokens_each': 512, 'automatic_retry': False,
        'failure_stops_remaining': True, 'accounting_reserve_each_cny': '0.032',
        'accounting_reserve_total_cny': '0.064'}
    put(source / 'decision.json', decision)
    put(source / 'shared-export-approval.json', {'package': 'P26-Q18-two', 'approved': True,
        'source_kind': 'explicit_user_reply', 'scope_hashes': list(scopes)})
    put(source / 'snapshot-after.json', {'files': {
        'ledger_request_hashes': [module.digest(row) for row in rows],
        'ledger_identity': module.digest({'ledger_id': ledger['ledger_id']})},
        'ledger_sidecars': {p.relative_to(tmp_path).as_posix(): module.sha(p) for p in sidecars}})
    put(source / 'releases.json', {'synthetic': 'knowledge'})
    for index, value in enumerate(payloads, 1):
        scope, row = scopes[index - 1], rows[index + 23]
        reply = {'reply': f'已核原回答{index}', 'release_version': 'synthetic-v1', 'session_id': 'old'}
        sample = {'sample': index, 'http': 200, 'real_model': True, 'business_unchanged': True,
            'business_before': {'tables': {}, 'sha256': 'same'},
            'business_after': {'tables': {}, 'sha256': 'same'}, 'reply': reply,
            'raw_model_result': f'raw-result-{index}.json', 'sql': {'trips': [{'id': 't1'}, {'id': 't2'}]}}
        report = {'input': value, 'input_hash': module.fingerprint(value), 'status': 'received',
            'kind': 'chat', 'prompt_version': 'assistant-6', 'inference_prompt': 'assistant-6',
            'candidate': {'content': reply['reply'], 'tool_calls': []}, 'release_version': 'synthetic-v1',
            'usage': usage, 'amount_cny': row['accounted_cny']}
        report_path = source / f'live-runs/report-{index}.json'
        put(report_path, report)
        put(source / f'payload-live-{index}.json', {'payload': value, 'scope_hash': scope})
        put(source / f'sample-{index}.json', sample)
        put(source / f'raw-result-{index}.json', {'report': report_path.relative_to(tmp_path).as_posix(),
            'report_sha256': module.sha(report_path), 'scope_hash': scope, 'raw_model_received': True,
            'ledger_row_matched': True, 'ledger_request_id': row['request_id'],
            'provider_request_id_sha256': hashlib.sha256(row['provider_request_id'].encode()).hexdigest(),
            'usage': usage, 'amount_cny': row['accounted_cny']})
        put(source / f'sample-{index}-reviewed.json', {'passed': True, 'known_usage': True,
            'business_unchanged': True, 'independent_sql': True, 'content_reviewed': True,
            'scope_hash': scope, 'reply_hash': module.fingerprint(reply),
            'supported_claims': sorted(module.REQUIRED_CLAIMS[index - 1]),
            'recomputed_accounted_cny': row['accounted_cny']})
        put(source / f'sample-{index}-sql-check.json', {'passed': True, 'unpaid_unticketed': True,
            'sample_sha256': module.sha(source / f'sample-{index}.json'), 'scope_hash': scope})
        put(source / f'attempt-{index}.json', {'input_hash': module.fingerprint(value),
            'scope_hash': scope, 'sample': index, 'requests_before': 23 + index, 'automatic_retry': False})
    return SimpleNamespace(source=source, out=out, ledger=ledger_path, fake=fake,
                           decision=decision, monkeypatch=monkeypatch)


def test_original_paid_source_chain_is_read_only_and_closes_old_authority(source_case):
    before = module.ledger_fingerprint()
    bundle = module.source_bundle()
    assert len(bundle['samples']) == 2 and len(bundle['manifest']['files']) == 18
    assert bundle['manifest']['old_authorization_reused'] is False
    assert bundle['manifest']['old_observation_ttl_valid'] is False
    assert bundle['manifest']['new_model_calls'] == bundle['manifest']['new_permits'] == 0
    assert module.ledger_fingerprint() == before


@pytest.mark.parametrize('name,field,value', [
    ('decision', 'source_reply', '继续完成'), ('decision', 'approved', 1),
    ('decision', 'max_physical_requests', True), ('decision', 'max_output_tokens_each', 1024),
    ('sample-1-reviewed', 'known_usage', False), ('sample-1-reviewed', 'reply_hash', 'changed'),
    ('sample-2-reviewed', 'supported_claims', []), ('raw-result-1', 'report_sha256', 'changed'),
    ('raw-result-2', 'ledger_request_id', '25'), ('sample-1-sql-check', 'sample_sha256', 'changed'),
    ('attempt-2', 'requests_before', 24), ('sample-2', 'http', 503),
])
def test_changed_authority_reply_usage_or_review_cannot_be_replayed(source_case, name, field, value):
    path = source_case.source / f'{name}.json'
    data = module.read(path); data[field] = value; put(path, data)
    with pytest.raises(RuntimeError):
        module.source_bundle()


def test_original_batch_stop_is_bound_to_ledger_identity(source_case):
    put(source_case.fake.q18_stop_path, {'ledger_id': 'other', 'physical_requests': 26})
    with pytest.raises(RuntimeError): module.source_bundle()


@pytest.fixture
def terminal():
    trips, locks, payments, jobs, providers, tasks, tickets, flights = ([] for _ in range(8))
    for index, amount in ((1, 80000), (2, 60000)):
        trip, lock, payment, task = (f'{name}{index}' for name in ('trip', 'lock', 'payment', 'task'))
        trips.append({'id': trip, 'owner_id': 'personal_alice', 'flight_id': f'flight{index}',
                      'revision': 1, 'status': 'paid'})
        locks.append({'id': lock, 'trip_id': trip, 'trip_revision': 1, 'status': 'consumed',
            'amount_minor': amount, 'coupon_id': 'coupon' if index == 1 else None})
        payments.append({'id': payment, 'trip_id': trip, 'lock_id': lock, 'status': 'succeeded', 'amount_minor': amount})
        jobs.append({'payment_id': payment, 'status': 'done'})
        providers.append({'operation_id': payment, 'outcome': 'succeeded', 'captured_minor': amount})
        tasks.append({'id': task, 'payment_id': payment, 'trip_id': trip, 'status': 'succeeded'})
        tickets.append({'id': f'ticket{index}', 'trip_id': trip, 'trip_revision': 1, 'payment_id': payment,
                        'task_id': task, 'quantity': 1, 'certificate_present': True})
        flights.append({'id': f'flight{index}', 'capacity': 2, 'reserved': 0, 'sold': 1})
    return {'trips': trips, 'locks': locks, 'payments': payments, 'payment_jobs': jobs,
        'provider_payments': providers, 'ticket_tasks': tasks, 'tickets': tickets, 'flights': flights,
        'coupons': [{'id': 'coupon', 'status': 'consumed', 'lock_id': 'lock1'}],
        'coupon_events': [{'action': 'coupon.consumed', 'count': 1}]}


def test_independent_terminal_requires_both_valid_paid_tickets(terminal):
    result = module.judge_terminal(terminal, ('trip1', 'trip2'))
    assert result['captured_minor'] == 140000 and result['tickets'] == 2
    assert result['ai_task_complete'] is False


@pytest.mark.parametrize('table,index,field,value', [
    ('trips', 0, 'owner_id', 'other'), ('trips', 1, 'revision', 2),
    ('tickets', 0, 'certificate_present', False), ('tickets', 1, 'quantity', 2),
    ('tickets', 1, 'id', 'ticket1'), ('tickets', 1, 'payment_id', 'payment1'),
    ('ticket_tasks', 1, 'payment_id', 'payment1'), ('payment_jobs', 1, 'status', 'pending'),
    ('payments', 1, 'lock_id', 'lock1'), ('locks', 0, 'status', 'held'),
    ('locks', 1, 'amount_minor', 1), ('provider_payments', 0, 'captured_minor', 1),
    ('coupons', 0, 'lock_id', 'lock2'), ('coupon_events', 0, 'count', 2),
    ('flights', 1, 'id', 'unrelated'), ('flights', 0, 'reserved', 1),
])
def test_independent_terminal_rejects_invalid_ticket_association_or_conservation(terminal, table, index, field, value):
    terminal[table][index][field] = value
    with pytest.raises((RuntimeError, KeyError)):
        module.judge_terminal(terminal, ('trip1', 'trip2'))


def cancellation_case(monkeypatch, *, unpaid=False, changed=False, response_code=None):
    before = {'locks': [{'id': 'lock', 'trip_id': 'trip', 'status': 'held'}],
        'coupons': [{'id': 'coupon', 'lock_id': 'lock', 'status': 'held'}],
        'flights': [{'reserved': 1, 'sold': 0}], **{k: [] for k in (
            'payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets')}}
    after = deepcopy(before)
    if unpaid:
        after['locks'][0]['status'] = 'cancelled'
        after['coupons'][0].update(status='available', lock_id=None)
        after['flights'][0]['reserved'] = 0
    elif changed:
        after['flights'][0]['reserved'] = 0
    sql = iter((before, after))
    snapshots = iter(({'sha256': 'before'}, {'sha256': 'changed' if unpaid or changed else 'before'}))
    monkeypatch.setattr(module, 'terminal_sql', lambda settings: next(sql))
    monkeypatch.setattr(module, 'business_snapshot', lambda settings: next(snapshots))
    seen = []
    expected = ['protected']
    def handler(request):
        seen.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json={'lock': {'id': 'lock', 'status': 'held' if unpaid else expected[0], 'valid': True},
                'payment': None if unpaid else {'status': 'processing' if expected[0] == 'protected' else 'succeeded'}})
        code = response_code if response_code is not None else (200 if unpaid else 409)
        return httpx.Response(code, json={'status': 'cancelled'} if code == 200 else {'error': 'payment_protected'})
    client = httpx.Client(base_url=module.TARGET, transport=httpx.MockTransport(handler))
    return client, seen, expected


def test_unpaid_release_uses_cancel_http_and_independent_after_sql(monkeypatch):
    client, seen, _ = cancellation_case(monkeypatch, unpaid=True)
    with client:
        evidence = module.unpaid_cancellation(client, {}, None, 'trip')
    assert [(r.method, r.url.path) for r in seen] == [('GET', '/trips/trip/booking'), ('POST', '/locks/lock/cancel')]
    assert evidence['response_json_sha256'] == module.digest(evidence['response_json'])
    assert evidence['choice_by'] == 'engineering_evaluator' and evidence['coupon_released'] is True


@pytest.mark.parametrize('condition', ['protected', 'consumed'])
def test_protected_or_consumed_http_409_has_no_sql_side_effects(monkeypatch, condition):
    client, seen, expected = cancellation_case(monkeypatch)
    expected[0] = condition
    with client:
        evidence = module.cancellation_counterexample(client, {}, None, 'trip', condition)
    assert seen[-1].method == 'POST' and evidence['http'] == 409
    assert evidence['before_sql_sha256'] == evidence['after_sql_sha256']


@pytest.mark.parametrize('changed,response_code', [(True, 409), (False, 200)])
def test_cancel_rejection_needs_real_rejection_and_unchanged_state(monkeypatch, changed, response_code):
    client, _, _ = cancellation_case(monkeypatch, changed=changed, response_code=response_code)
    with client, pytest.raises(RuntimeError):
        module.cancellation_counterexample(client, {}, None, 'trip', 'protected')


def test_http_journal_excludes_credentials_and_correlates_response_hashes():
    journal = module.HTTPJournal()
    journal.branch = 2
    def handler(request):
        return httpx.Response(200, json={'access_token': 'never-store'} if request.url.path == '/demo/sessions' else {'status': 'ok'})
    with httpx.Client(base_url=module.TARGET, transport=httpx.MockTransport(handler),
                      event_hooks={'response': [journal.response]}) as client:
        client.post('/demo/sessions', json={'account': 'synthetic'})
        client.post('/locks/synthetic/cancel', headers={'Authorization': 'Bearer never-store'}, json={})
    assert len(journal.events) == 1 and journal.events[0]['branch'] == 2
    assert journal.events[0]['response_json_sha256'] == module.digest({'status': 'ok'})
    assert 'never-store' not in json.dumps(journal.events)


def test_exclusive_save_preserves_failed_or_prior_run(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'OUT', tmp_path)
    module.save('summary.json', {'complete': False})
    with pytest.raises(FileExistsError): module.save('summary.json', {'complete': True})
    assert module.read(tmp_path / 'summary.json') == {'complete': False}


def test_no_model_traffic_guard_blocks_both_http_and_budget(tmp_path):
    guard = module.NoModelTraffic()
    async def attempted_send():
        async with httpx.AsyncClient() as client:
            await client.get('https://example.invalid/')
    with guard.installed():
        with pytest.raises(RuntimeError, match='unexpected_model_http_send'):
            asyncio.run(attempted_send())
        with pytest.raises(RuntimeError, match='unexpected_model_reservation'):
            module.BudgetLedger(tmp_path / 'no-ledger.json').reserve()
    assert guard.send_attempts == guard.reserve_attempts == 1


def test_b_engineering_session_preserves_original_goal_with_new_identity_and_empty_history(source_case, monkeypatch):
    calls = []
    assistant = SimpleNamespace(status=lambda who: {'ready': False, 'reason': 'experiment_limit'},
        session=lambda sid, who, version, goal: calls.append((sid, who, version, goal)) or ('new', SimpleNamespace(history=[])))
    app = SimpleNamespace(state=SimpleNamespace(assistant=assistant), get=lambda path: lambda function: function)
    monkeypatch.setattr(module, 'configuration', lambda: (None, None))
    monkeypatch.setattr(module, 'create_app', lambda settings: app)
    monkeypatch.setattr(module, 'Releases', lambda *args: SimpleNamespace(active=lambda: {'prompt_version': 'assistant-6'}))
    monkeypatch.setattr(module.sandbox, 'check_cluster', lambda: {'instance_id': 'synthetic'})
    module.configure()
    sid, session = assistant.session(None, 'new-current-identity', 'synthetic-v1', '精确问题2')
    assert calls == [(None, 'new-current-identity', 'synthetic-v1', '共同总体任务')]
    assert sid == 'new' and session.history == [] and assistant.external_model is False
    with pytest.raises(RuntimeError): assistant.session('old', 'new-current-identity', 'synthetic-v1', '精确问题2')
    with pytest.raises(RuntimeError): assistant.session(None, 'new-current-identity', 'synthetic-v1', '任意新问题')
    with pytest.raises(RuntimeError): assistant.session(None, 'new-current-identity', 'other-version', '精确问题2')
