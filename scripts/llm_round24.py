"""P24原数据/前23笔/历史证据保护和静态交付审计，不发送模型或重置业务。"""
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
OUT = ROOT / '.local/d3-24'


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def historical():
    return {p.relative_to(ROOT).as_posix(): sha(p) for number in (19, 20, 21, 22, 23)
            for p in (ROOT / f'.local/d3-{number}').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and 'sandbox' not in p.parts}


def snapshot(phase):
    from scripts.d3_evaluate import business_snapshot
    from scripts.llm_round17 import terminal
    from services.business.config import load_settings
    prior = read(ROOT / '.local/d3-23/snapshot-after.json') if phase == 'before' else read(OUT / 'snapshot-before.json')
    sidecars = sorted((ROOT / '.local').glob('guide-usage.json*'))
    value = {'captured_at': datetime.now(timezone.utc).isoformat(), 'files': protection(),
             'dev': business_snapshot(load_settings()), 'test': business_snapshot(load_settings(test=True)),
             'terminal': terminal(), 'historical': historical(),
             'old_native': {name: sha(Path(name)) for name in prior['old_native']},
             'ledger_sidecars': {p.relative_to(ROOT).as_posix(): sha(p) for p in sidecars if p.is_file()},
             'experiment_review': read(ROOT / '.local/guide-experiment.json')}
    a, b = value['files'], prior['files']
    value['checks'] = {
        'business_unchanged': all(value[k]['sha256'] == prior[k]['sha256'] for k in ('dev', 'test')),
        'authentication_unchanged': all(value[k]['authentication'] == prior[k]['authentication'] for k in ('dev', 'test')),
        'terminal_unchanged': value['terminal'] == prior['terminal'],
        'protected_files': all(a['protected'].get(k) == v for k, v in b['protected'].items()),
        'original_23_ledger_entries': a['ledger_request_hashes'][:23] == b['ledger_request_hashes'][:23],
        'ledger_identity': a['ledger_identity'] == b['ledger_identity'],
        'candidates': a['candidate_hashes'] == b['candidate_hashes'], 'active': a['active'] == b['active'],
        'old_native': value['old_native'] == prior['old_native']}
    old_sidecars = prior['ledger_sidecars']
    value['checks']['old_ledger_sidecars'] = all(value['ledger_sidecars'].get(k) == v
        for k, v in old_sidecars.items() if k != '.local/guide-usage.json')
    if phase == 'after':
        value['checks']['history'] = value['historical'] == prior['historical']
        expected = {**prior['experiment_review'], 'price_checked_on': '2026-10-18'}
        value['checks']['only_recorded_review_date_changed'] = value['experiment_review'] == expected
        assert a['budget']['physical_requests'] in (23, 24, 25)
    else:
        assert a['budget']['physical_requests'] == 23 and a['budget']['unresolved_requests'] == 0
    assert len(value['dev']['tables']) == len(value['test']['tables']) == 23
    assert value['dev']['tables']['tickets']['rows'] == 6
    assert all(value['checks'].values()), 'P24_preservation_failed'
    save(f'snapshot-{phase}.json', value)
    print(json.dumps({'phase': phase, 'checks': value['checks'], 'budget': a['budget'],
                      'historical_files': len(value['historical'])}, ensure_ascii=False))


def review_failure():
    """独立重算保存证据；无模型、HTTP或PG调用，也不改写原样本。"""
    from services.guide.budget import BudgetLedger, BudgetUnavailable
    from services.guide.assistant import corporate_export_scope
    from services.guide.experiment import input_size_bytes
    from services.guide.knowledge import fingerprint
    from scripts.round23_consult import check_sql
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    rows = read(ledger.path)['requests']
    grant = read(ledger.q17_path)
    sample = read(OUT / 'sample-1.json')
    payload = read(OUT / 'payload-live-1.json')
    attempt = read(OUT / 'attempt-1.json')
    decision = read(OUT / 'decision.json')
    assert len(rows) == 24 and len(grant['intents']) == 1
    row = rows[-1]
    assert grant['intents'][0] == {'sequence': 1, 'scope_hash': decision['scope_hashes'][0],
                                'request_id': row['request_id'], 'state': 'reserved'}
    assert row['state'] == 'settled' and row['completion_tokens'] == 512
    assert payload['scope_hash'] == attempt['scope_hash'] == decision['scope_hashes'][0]
    assert corporate_export_scope(payload['payload']) == payload['scope_hash']
    assert input_size_bytes(payload['payload']) == payload['input_bytes'] == 10894
    assert fingerprint(payload['payload']) == attempt['input_hash']
    context = json.loads(payload['payload']['messages'][-1]['content'])
    assert context['history'] == [] and payload['payload']['max_tokens'] == 512
    reports = list((OUT / 'live-runs').glob('*.json'))
    protocols = list((OUT / 'protocol').glob('*.json'))
    assert len(reports) == len(protocols) == 1
    live, protocol = read(reports[0]), read(protocols[0])
    assert live['input'] == payload['payload'] and live['status'] == 'unavailable'
    assert live['reason'] == protocol['reason'] == 'output_truncated'
    assert row['request_id'] in protocols[0].name
    assert 'candidate' not in live and 'usage' not in live
    assert sample['http'] == 503 and sample['reply'] == {'error': 'output_truncated'}
    assert sample['business_before']['sha256'] == sample['business_after']['sha256']
    trips = tuple(str(next(r['id'] for r in sample['sql']['trips'] if r['flight_id'] == 'DEMO-' + size))
                  for size in ('LARGE', 'SMALL'))
    check_sql(sample['sql'], trips, changed=False)
    assert not any((OUT / name).exists() for name in ('attempt-2.json', 'payload-live-2.json',
        'sample-2.json', 'raw-result-1.json', 'raw-result-2.json'))
    stopped = {}
    for name, check, scope in [('q17_first', ledger.check_q17_scope, decision['scope_hashes'][0]),
                              ('q17_second', ledger.check_q17_scope, decision['scope_hashes'][1]),
                              ('q16_second', ledger.check_q16_scope, decision['scope_hashes'][1])]:
        try:
            check(scope)
        except BudgetUnavailable as error:
            stopped[name] = error.reason
        else:
            raise AssertionError('stopped_scope_reopened')
    assert stopped == {'q17_first': 'q17_batch_stopped', 'q17_second': 'q17_batch_stopped',
                       'q16_second': 'q16_batch_stopped'}
    cost = (Decimal(row['cache_miss_tokens']) * 2 + Decimal(row['cache_hit_tokens']) * Decimal('0.04')
            + Decimal(row['completion_tokens']) * 8) / 1000000
    assert row['cache_miss_tokens'] + row['cache_hit_tokens'] == row['prompt_tokens']
    assert cost == Decimal(row['accounted_cny'])
    value = {'integrity_passed': True, 'real_model_calls': 1, 'first_delivery_http': 503,
        'delivery_passed': False, 'reason': 'output_truncated', 'second_sample_sent': False,
        'request_id': row['request_id'], 'scope_hash': payload['scope_hash'],
        'prompt_tokens': row['prompt_tokens'], 'completion_tokens': row['completion_tokens'],
        'cache_hit_tokens': row['cache_hit_tokens'], 'cache_miss_tokens': row['cache_miss_tokens'],
        'recomputed_cny': str(cost), 'budget': ledger.summary(), 'business_unchanged': True,
        'independent_saved_sql': 'passed_unpaid_unticketed_coupon_available',
        'usage_source': 'settled_ledger_from_usage_validated_before_candidate_parser',
        'raw_model_text_preserved': False, 'original_http_usage_preserved': False,
        'content_review': 'unavailable_no_usable_candidate', 'guard_replay': 'not_possible_without_raw_text',
        'full_two_ticket_task_complete': False, 'stopped_scope_checks': stopped,
        'live_report': reports[0].relative_to(ROOT).as_posix(), 'live_report_sha256': sha(reports[0]),
        'protocol': protocols[0].relative_to(ROOT).as_posix(), 'protocol_sha256': sha(protocols[0]),
        'model_calls_by_review': 0, 'http_calls_by_review': 0, 'pg_calls_by_review': 0}
    save('failure-review.json', value)
    print(json.dumps(value, ensure_ascii=False))


def audit():
    from services.guide.budget import BudgetLedger
    from services.guide.assistant import corporate_export_scope
    from services.guide.experiment import input_size_bytes, MAX_INPUT_BYTES
    from services.guide.knowledge import fingerprint
    before, after = (read(OUT / f'snapshot-{p}.json') for p in ('before', 'after'))
    assert all(after['checks'].values())
    for collection in (before['historical'], before['files']['protected']):
        assert all(sha(ROOT / p) == v for p, v in collection.items())
    for name, value in before['ledger_sidecars'].items():
        if name != '.local/guide-usage.json':
            assert sha(ROOT / name) == value
    assert all(sha(Path(p)) == value for p, value in before['old_native'].items())
    listing = subprocess.check_output(['git', '-c', f"safe.directory={ROOT.as_posix()}", 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = sorted({p.decode('utf-8') for p in listing.split(bytes([0])) if p})
    secrets = [read(ROOT / '.local/guide-model.json')['api_key'], *read(ROOT / '.local/business.json').values(),
               *read(ROOT / '.local/d3-20/sandbox/business.json').values()]
    leaks = [p for p in paths if any(v.encode() in (ROOT / p).read_bytes() for v in secrets if len(v) > 8)]
    assert not leaks, leaks
    py = [p for p in paths if p.endswith('.py')]
    for p in py:
        ast.parse((ROOT / p).read_text(encoding='utf-8-sig'), filename=p)
    md = [p for p in paths if p.endswith('.md')]
    links = 0
    for name in md:
        p = ROOT / name
        content = p.read_text(encoding='utf-8-sig')
        assert sum(line.startswith('```') for line in content.splitlines()) % 2 == 0, name
        for match in re.finditer(r'\]\(([^)]+)\)', content):
            target = match.group(1).strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            target = unquote(target.split('#', 1)[0])
            if target:
                links += 1
                assert (p.parent / target).exists(), (name, target)
    runtime = read(OUT / 'runtime-final.json')
    assert runtime['listeners'] == [] and runtime['api_exit'] == 0
    assert runtime['original_postgres_stop_exit'] == runtime['sandbox_postgres_stop_exit'] == 0
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    budget = ledger.summary()
    assert budget == after['files']['budget']
    current = protection()
    assert current['ledger_request_hashes'][:23] == before['files']['ledger_request_hashes']
    assert current['ledger_identity'] == before['files']['ledger_identity']
    assert current['candidate_hashes'] == before['files']['candidate_hashes'] and current['active'] == before['files']['active']
    for name in ('preflight-server-transport-guard.json', 'preflight-controller-transport-guard.json'):
        guard = read(OUT / name)
        assert guard['model_send_attempts'] == guard['budget_reserve_attempts'] == 0
    preparation = read(OUT / 'preflight-controller-result.json')
    assert preparation['complete'] and preparation['real_model'] is False
    decision = read(OUT / 'decision.json')
    assert decision['package'] == 'Q17' and decision['approved'] is True
    assert decision['source_reply'] == '批准Q17：最多新增2次并外发上述两份精确合成字段'
    assert ledger.q16_stop_path.is_file()
    controller = read(OUT / 'controller-result.json')
    added = budget['physical_requests'] - 23
    assert 0 <= added <= 2
    assert len(controller['samples']) <= 2
    for index, sample in enumerate(controller['samples'], 1):
        payload = read(OUT / f'payload-live-{index}.json')
        attempt = read(OUT / f'attempt-{index}.json')
        assert payload['scope_hash'] == attempt['scope_hash'] == decision['scope_hashes'][index - 1]
        assert corporate_export_scope(payload['payload']) == payload['scope_hash']
        assert input_size_bytes(payload['payload']) == payload['input_bytes'] <= MAX_INPUT_BYTES
        assert payload['payload']['max_tokens'] == 512
        assert sample['business_before']['sha256'] == sample['business_after']['sha256']
        if sample['http'] == 200:
            checked = read(OUT / f'sample-{index}-reviewed.json')
            assert checked['scope_hash'] == payload['scope_hash']
            assert checked['reply_hash'] == fingerprint(sample['reply'])
    if not controller['complete']:
        assert ledger.q17_stop_path.is_file()
        failure = read(OUT / 'failure-review.json')
        assert failure['integrity_passed'] and not failure['delivery_passed'] and not failure['second_sample_sent']
        assert failure['reason'] == 'output_truncated' and added == 1
        row = read(ledger.path)['requests'][-1]
        assert failure['recomputed_cny'] == row['accounted_cny'] and failure['request_id'] == row['request_id']
        assert all(failure[k] == row[k] for k in ('prompt_tokens', 'completion_tokens', 'cache_hit_tokens', 'cache_miss_tokens'))
        grant = read(ledger.q17_path)
        assert grant['intents'] == [{'sequence': 1, 'scope_hash': decision['scope_hashes'][0],
                                     'request_id': row['request_id'], 'state': 'reserved'}]
        assert failure['budget'] == budget and budget['unresolved_requests'] == 0
        assert sha(ROOT / failure['live_report']) == failure['live_report_sha256']
        assert sha(ROOT / failure['protocol']) == failure['protocol_sha256']
        assert not failure['raw_model_text_preserved'] and not failure['original_http_usage_preserved']
    else:
        assert added == 2 and budget['unresolved_requests'] == 0
        assert all(read(OUT / f'sample-{i}-reviewed.json')['passed'] for i in (1, 2))
    value = {'deliverables': len(paths), 'python_ast': len(py), 'markdown': len(md), 'local_links': links,
             'secret_matches': leaks, 'preservation': after['checks'], 'budget': budget,
             'q17_real_model_calls': added, 'q17_approved': True, 'runtime_stopped': True,
             'controller_complete': controller['complete'], 'old_q16_stopped': True,
             'model_calls_by_audit': 0}
    save('delivery-audit.json', value)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('before', 'after', 'review-failure', 'audit'))
    action = parser.parse_args().action
    if action == 'audit':
        audit()
    elif action == 'review-failure':
        review_failure()
    else:
        snapshot(action)
