"""P25 零模型预览、原数据保全及交付审计；没有真实许可或发送入口。"""
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
OUT = ROOT / '.local/d3-25'


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def historical():
    # 仅实际运行中的PG数据和追加日志可变；旧运行报告、测试原件及sandbox配置均保全。
    return {p.relative_to(ROOT).as_posix(): sha(p) for n in range(19, 25)
            for p in (ROOT / f'.local/d3-{n}').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and not p.is_relative_to(ROOT / '.local/d3-20/sandbox/postgres-data')
            and p != ROOT / '.local/d3-20/sandbox/postgres.log'}


def snapshot(phase):
    from scripts.d3_evaluate import business_snapshot
    from scripts.llm_round17 import terminal
    from services.business.config import load_settings
    prior = read(ROOT / '.local/d3-24/snapshot-after.json') if phase == 'before' else read(OUT / 'snapshot-before.json')
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
        'all_24_entries': a['ledger_request_hashes'] == b['ledger_request_hashes'],
        'ledger_identity': a['ledger_identity'] == b['ledger_identity'],
        'ledger_and_all_sidecars': value['ledger_sidecars'] == prior['ledger_sidecars'],
        'candidate_and_active': all(a[k] == b[k] for k in ('candidate_hashes', 'active')),
        'old_native': value['old_native'] == prior['old_native'],
        'configuration': all(a['mutable_original_hashes'][k] == b['mutable_original_hashes'][k]
            for k in ('.local/guide-experiment.json', '.local/assistant-releases-test.json',
                      'services/guide/budget.py', 'services/guide/experiment.py'))}
    if phase == 'after':
        checks['all_19_to_24_artifacts'] = value['historical'] == prior['historical']
    else:
        checks['previous_history_still_valid'] = all(value['historical'].get(k) == v for k, v in prior['historical'].items())
    assert len(value['dev']['tables']) == len(value['test']['tables']) == 23
    assert value['dev']['tables']['tickets']['rows'] == 6
    assert a['budget']['physical_requests'] == 24 and a['budget']['unresolved_requests'] == 0
    assert a['budget']['accounted_cny'] == '0.08382128'
    value['checks'] = checks
    assert all(checks.values()), 'P25_preservation_failed'
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
    return result


def review():
    from services.guide.assistant import corporate_export_scope
    from services.guide.experiment import input_size_bytes, MAX_INPUT_BYTES
    from scripts.round23_consult import check_sql
    from scripts.round25_consult import QUESTIONS
    checks = []
    def normalized_context(payload):
        obj = json.loads(payload['messages'][-1]['content'])
        # 请求本身明确改变的goal/question，以及重新采集的真实时刻；其余事实逐值保留。
        obj.pop('question')
        obj.pop('goal')
        # 只放行这两个已知采集时间路径；业务时钟、报价期限、所有其余事实保持逐值比较。
        obj['current_state'].pop('observed_at')
        obj['current_state'].pop('valid_until')
        return obj
    for i in (1, 2):
        preview = read(OUT / f'consult-ready/consult-preview-{i}.json')
        sample = read(OUT / f'consult-ready/consult-sample-{i}.json')
        old = read(ROOT / f'.local/d3-24/preflight-preview-{i}.json')
        payload = preview['payload']
        context = json.loads(payload['messages'][-1]['content'])
        assert context['question'] == QUESTIONS[i-1] and context['goal'] == QUESTIONS[0]
        assert context['history'] == [] and payload['max_tokens'] == 512
        assert not preview['approved'] and preview['new_explicit_approval_required']
        assert preview['scope_hash'] == corporate_export_scope(payload) != old['scope_hash']
        assert payload['messages'][0] == old['payload']['messages'][0]
        assert {k: v for k, v in payload.items() if k != 'messages'} == {
            k: v for k, v in old['payload'].items() if k != 'messages'}
        assert len(payload['messages']) == len(old['payload']['messages']) == 2
        assert payload['messages'][-1]['role'] == old['payload']['messages'][-1]['role'] == 'user'
        assert normalized_context(payload) == normalized_context(old['payload'])
        assert input_size_bytes(payload) == preview['input_bytes'] <= MAX_INPUT_BYTES
        assert sample['http'] == 200 and sample['business_unchanged'] and not sample['real_model']
        ids = sample['request']['delegated_trip_ids']
        check_sql(sample['sql'], ids, changed=i == 2)
        paths = []
        def leaves(v, path=''):
            if isinstance(v, dict):
                for k, x in v.items():
                    leaves(x, path + '.' + k if path else k)
            elif isinstance(v, list):
                for j, x in enumerate(v):
                    leaves(x, path + f'[{j}]')
            else:
                paths.append(path)
        leaves(context)
        checks.append({'sample': i, 'question': QUESTIONS[i-1], 'scope_hash': preview['scope_hash'],
            'input_bytes': preview['input_bytes'], 'max_tokens': 512, 'history': 0,
            'preview_file_sha256': sha(OUT / f'consult-ready/consult-preview-{i}.json'),
            'independent_saved_sql': True, 'business_unchanged': True,
            'only_goal_question_and_collection_times_changed': True, 'field_leaf_paths': paths})
    for name in ('prepare-transport-guard.json', 'server-transport-guard.json'):
        guard = read(OUT / 'consult-ready' / name)
        assert guard['model_send_attempts'] == guard['budget_reserve_attempts'] == 0
        assert guard['ledger_unchanged']
    assert sha(OUT / 'consult-ready/consult-releases.json') == sha(ROOT / '.local/d3-22/consult-releases.json')
    value = {'approved': False, 'real_model_calls': 0, 'new_grant_created': False,
        'recommendation': 'compact_current_next_step_512', 'samples': checks,
        'old_stops': stopped_checks(), 'new_batch_proposed_max_requests': 2,
        'new_batch_proposed_accounting_reserve_cny': '0.064',
        'ordinary_request_ceiling_unchanged': 22,
        'shorter_prompt_does_not_prove_no_truncation': True}
    save('recovery-review.json', value)
    print(json.dumps({k: v for k, v in value.items() if k != 'samples'}, ensure_ascii=False))
    print(json.dumps([ {k: v for k, v in x.items() if k != 'field_leaf_paths'} for x in checks], ensure_ascii=False))


def audit():
    from services.guide.budget import BudgetLedger
    before, after = [read(OUT / f'snapshot-{p}.json') for p in ('before', 'after')]
    assert all(after['checks'].values())
    for collection in (before['historical'], before['files']['protected'], before['ledger_sidecars']):
        assert all(sha(ROOT / p) == v for p, v in collection.items())
    assert all(sha(Path(p)) == h for p, h in before['old_native'].items())
    listing = subprocess.check_output(['git', '-c', f"safe.directory={ROOT.as_posix()}",
        'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = sorted({p.decode('utf-8') for p in listing.split(bytes([0])) if p})
    secrets = [read(ROOT / '.local/guide-model.json')['api_key'],
               *read(ROOT / '.local/business.json').values(),
               *read(ROOT / '.local/d3-20/sandbox/business.json').values()]
    leaks = [p for p in paths if any(s.encode() in (ROOT / p).read_bytes() for s in secrets if len(s) > 8)]
    assert not leaks
    py, md = [p for p in paths if p.endswith('.py')], [p for p in paths if p.endswith('.md')]
    for name in py:
        ast.parse((ROOT / name).read_text(encoding='utf-8-sig'), filename=name)
    links = 0
    for name in md:
        path = ROOT / name
        content = path.read_text(encoding='utf-8-sig')
        assert sum(line.startswith('```') for line in content.splitlines()) % 2 == 0, name
        for m in re.finditer(r'\]\(([^)]+)\)', content):
            target = m[1].strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            target = unquote(target.split('#', 1)[0])
            if target:
                links += 1
                assert (path.parent / target).exists(), (name, target)
    current = protection()
    assert current['ledger_request_hashes'] == before['files']['ledger_request_hashes']
    assert current['ledger_identity'] == before['files']['ledger_identity']
    assert current['candidate_hashes'] == before['files']['candidate_hashes']
    assert current['active'] == before['files']['active']
    runtime = read(OUT / 'runtime-final.json')
    assert runtime['listeners'] == [] and runtime['api_exit'] == 0
    assert runtime['original_postgres_stop_exit'] == runtime['sandbox_postgres_stop_exit'] == 0
    recovery = read(OUT / 'recovery-review.json')
    assert not recovery['approved'] and not recovery['new_grant_created'] and recovery['real_model_calls'] == 0
    budget = BudgetLedger(ROOT / '.local/guide-usage.json').summary()
    assert budget == before['files']['budget']
    value = {'deliverables': len(paths), 'python_ast': len(py), 'markdown': len(md),
        'local_links': links, 'secret_matches': leaks, 'preservation': after['checks'],
        'budget': budget, 'real_model_calls': 0, 'old_stops': stopped_checks(),
        'runtime_stopped': True, 'recovery_approved': False, 'model_calls_by_audit': 0}
    save('delivery-audit.json', value)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('before', 'after', 'review', 'audit'))
    action = parser.parse_args().action
    if action in ('before', 'after'):
        snapshot(action)
    else:
        globals()[action]()
