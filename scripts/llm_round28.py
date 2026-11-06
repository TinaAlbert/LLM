"""P28原业务、账本和19–27原件的只读保全；不发模型、不创建许可。"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.llm_round19 import protection, sha, read
from scripts.llm_round27 import stopped_checks
OUT = ROOT / '.local/d3-28'


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def historical():
    return {p.relative_to(ROOT).as_posix(): sha(p) for n in range(19, 28)
            for p in (ROOT / f'.local/d3-{n}').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts
            and not p.is_relative_to(ROOT / '.local/d3-20/sandbox/postgres-data')
            and p != ROOT / '.local/d3-20/sandbox/postgres.log'}


def snapshot(phase):
    from scripts.d3_evaluate import business_snapshot
    from scripts.llm_round17 import terminal
    from services.business.config import load_settings
    prior = read(ROOT / '.local/d3-27/snapshot-after.json') if phase == 'before' else read(OUT / 'snapshot-before.json')
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
        'ledger_and_all_sidecars': value['ledger_sidecars'] == prior['ledger_sidecars'],
        'candidate_and_active': all(a[k] == b[k] for k in ('candidate_hashes', 'active')),
        'old_native': value['old_native'] == prior['old_native'],
        'configuration': all(a['mutable_original_hashes'][k] == b['mutable_original_hashes'][k]
            for k in ('.local/guide-experiment.json', '.local/assistant-releases-test.json',
                      'services/guide/budget.py', 'services/guide/experiment.py')),
        'history_preserved': (value['historical'] == prior['historical'] if phase == 'after' else
            all(value['historical'].get(k) == h for k, h in prior['historical'].items()))}
    assert len(value['dev']['tables']) == len(value['test']['tables']) == 23
    assert value['dev']['tables']['tickets']['rows'] == 6
    assert a['budget']['physical_requests'] == 26 and a['budget']['unresolved_requests'] == 0
    assert a['budget']['accounted_cny'] == '0.09024104'
    assert all(checks.values()), 'P28_preservation_failed'
    value['checks'] = checks
    save(f'snapshot-{phase}.json', value)
    print(json.dumps({'phase': phase, 'checks': checks, 'budget': a['budget'],
                      'historical_files': len(value['historical'])}, ensure_ascii=False))


def audit():
    from scripts.round28_records import summarize
    from services.guide.fair_runtime import fair_input
    before, after = [read(OUT / f'snapshot-{p}.json') for p in ('before', 'after')]
    assert before['historical'] == after['historical'] == historical()
    assert before['ledger_sidecars'] == after['ledger_sidecars'] == {
        p.relative_to(ROOT).as_posix(): sha(p) for p in (ROOT / '.local').glob('guide-usage.json*')}
    current = protection()
    # captured_at是每次只读采集的时间，不能当作受保护内容要求逐字相等。
    stable = lambda value: {k: v for k, v in value.items() if k != 'captured_at'}
    assert stable(current) == stable(before['files']) == stable(after['files'])
    assert all(after['checks'].values())
    old_stops = stopped_checks()
    tasks = OUT / 'tasks-final'
    folders = sorted(p for p in tasks.iterdir() if p.is_dir())
    assert len(folders) == 4
    records = [read(p / 'task-record.json') for p in folders]
    task_summary = read(tasks / 'task-summary.json')
    recomputed = summarize(records)
    # 独立文件枚举按路径排序，实际组序08为B1→B0；按任务身份比较，不改任务内容。
    order = lambda row: (row['group'], row['task_ref'])
    recomputed['tasks'].sort(key=order)
    task_summary['tasks'].sort(key=order)
    assert recomputed == task_summary
    assert task_summary['offline_scripted_tasks'] == 4
    assert task_summary['ai_task_denominator'] == task_summary['confirmed_started_ai_tasks'] == 0
    assert task_summary['completion_rate'] is None and task_summary['accounted_cny'] == '0'
    assert all(not row['model_faithful_complete'] for row in task_summary['tasks'])
    inputs = {}
    duplicates = 0
    for folder in folders:
        binding = read(folder / 'host-binding.json')
        assert all(sha(ROOT / p) == h for p, h in binding['implementation_sha256'].items())
        rows = read(folder / 'fair-inputs.json')
        assert len(rows) == binding['limits']['max_rounds']
        for row in rows:
            payload = row['input']
            context = json.loads(payload['messages'][-1]['content'])
            if binding['group'] == 'B0':
                assert 'current_state' not in context and 'current_observation' not in context
            else:
                assert context['current_state']['contract_version'] == 'assistant-shared-1'
                assert fair_input(payload, 'guide') == payload
            assert row['real_model'] is False
        inputs[(binding['case_id'], binding['group'])] = rows
        guard = read(folder / 'server-guard.json')
        assert guard['send_attempts'] == guard['reserve_attempts'] == 0
        for duplicate in folder.glob('duplicate-*.json'):
            d = read(duplicate)
            assert d['http'] == 409 and d['visible_response'] == {'error': 'payment_exists'}
            assert d['before_business']['tables'] == d['after_business']['tables']
            assert d['before_business']['sha256'] == d['after_business']['sha256']
            duplicates += 1
    assert duplicates == 4
    for case in ('DEV27-05', 'DEV27-08'):
        for left, right in zip(inputs[(case, 'B0')], inputs[(case, 'B1')], strict=True):
            a, b = [json.loads(json.dumps(x['input'])) for x in (left, right)]
            context = json.loads(b['messages'][-1]['content'])
            context.pop('current_state')
            context.pop('current_observation')
            b['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
            assert a == b
    assert read(tasks / 'controller-guard.json') == {'send_attempts': 0, 'reserve_attempts': 0}
    ledger_check = read(tasks / 'ledger-preservation.json')
    assert ledger_check['before'] == ledger_check['after']
    assert all(row['api_exit'] == 0 for row in read(tasks / 'lifecycle.json'))
    runtime = read(OUT / 'runtime-final.json')
    assert runtime['listeners'] == []
    assert runtime['original_postgres_stop_exit'] == runtime['sandbox_postgres_stop_exit'] == 0
    review = read(OUT / 'independent-task-review.json')
    assert review['passed'] is True
    assert all(sha(ROOT / path) == h for path, h in review['source_artifacts'].items())
    assert all(sha(ROOT / path) == h for path, h in review['reviewed_code_sha256'].items())
    listing = subprocess.check_output(['git', '-c', f"safe.directory={ROOT.as_posix()}",
        'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = sorted({p.decode('utf-8') for p in listing.split(bytes([0])) if p})
    secrets = [read(ROOT / '.local/guide-model.json')['api_key'],
        *read(ROOT / '.local/business.json').values(), *read(ROOT / '.local/d3-20/sandbox/business.json').values()]
    leaks = [p for p in paths if any(s.encode() in (ROOT / p).read_bytes()
        for s in secrets if isinstance(s, str) and len(s) > 8)]
    assert not leaks
    py, md = [[p for p in paths if p.endswith(s)] for s in ('.py', '.md')]
    for p in py:
        ast.parse((ROOT / p).read_text(encoding='utf-8-sig'), filename=p)
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
    result = {'deliverables': len(paths), 'python_ast': len(py), 'markdown': len(md), 'local_links': links,
        'secret_matches': leaks, 'preservation': after['checks'], 'budget': current['budget'],
        'historical_files': len(historical()), 'old_stops': old_stops, 'audit_model_calls': 0,
        'offline_scripted_tasks': 4, 'ai_task_denominator': 0,
        'duplicate_payment_counterexamples': duplicates, 'runtime_stopped': True,
        'independent_review_sha256': sha(OUT / 'independent-task-review.json')}
    save('delivery-audit.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('before', 'after', 'audit'))
    action = parser.parse_args().action
    audit() if action == 'audit' else snapshot(action)
