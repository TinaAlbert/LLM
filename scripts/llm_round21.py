"""21轮只读保护及静态交付核查；不调用模型、不重置业务库。"""
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
from scripts.llm_round19 import protection, sha
from scripts.d3_evaluate import business_snapshot
from scripts.llm_round17 import terminal
from services.business.config import load_settings

OUT = ROOT / '.local/d3-21'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def snapshot(phase):
    value = {'captured_at': datetime.now(timezone.utc).isoformat(),
             'files': protection(), 'dev': business_snapshot(load_settings()),
             'test': business_snapshot(load_settings(test=True)), 'terminal': terminal()}
    value['previous_rounds'] = {p.relative_to(ROOT).as_posix(): sha(p)
        for number in (19, 20) for p in (ROOT / f'.local/d3-{number}').rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and 'sandbox' not in p.parts}
    prior = read(ROOT / '.local/d3-20/snapshot-after-q15.json')
    compare = prior if phase == 'before' else read(OUT / 'snapshot-before.json')
    a, b = value['files'], compare['files']
    value['checks'] = {
        'business_unchanged': all(value[k]['sha256'] == compare[k]['sha256'] for k in ('dev', 'test')),
        'authentication_unchanged': all(value[k]['authentication'] == compare[k]['authentication'] for k in ('dev', 'test')),
        'protected_files': all(a['protected'].get(k) == h for k, h in b['protected'].items()),
        'ledger_all_22_unchanged': a['ledger_request_hashes'] == b['ledger_request_hashes'],
        'ledger_identity': a['ledger_identity'] == b['ledger_identity'],
        'old_candidates': a['candidate_hashes'] == b['candidate_hashes'],
        'active_release': a['active'] == b['active'],
    }
    if phase == 'after':
        value['checks']['previous_rounds'] = value['previous_rounds'] == compare['previous_rounds']
    assert a['budget']['physical_requests'] == 22 and a['budget']['unresolved_requests'] == 0
    assert len(value['dev']['tables']) == len(value['test']['tables']) == 23
    assert value['dev']['tables']['tickets']['rows'] == 6
    assert all(value['checks'].values()), 'preservation_failed'
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f'snapshot-{phase}.json').open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({'phase': phase, 'budget': a['budget'], 'active': a['active'],
        'checks': value['checks'], 'protected_files': len(a['protected']),
        'terminal_counts': value['terminal']['counts']}, ensure_ascii=False))


def sample_review():
    from scripts.round21_explore import configuration
    _, settings = configuration()
    before = read(OUT / 'sample.json')
    after = business_snapshot(settings)
    assert after['sha256'] == before['business_before']['sha256'], 'synthetic_browser_business_changed'
    assert after['authentication']['total'] == after['authentication']['revoked']
    previews = []
    import hashlib
    for path in OUT.glob('payload-preview-*.json'):
        row = read(path)
        payload = row['payload']
        content = json.loads(payload['messages'][-1]['content'])
        serialized = json.dumps({'messages': payload['messages'], 'tools': payload['tools']}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        assert len(serialized) == row['input_bytes'] and payload['max_tokens'] == 512
        assert not re.search(r'(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', serialized.decode('utf-8'))
        assert not any(value in json.dumps(content['current_state']) for value in ('1400', '1480', 'source_ref', 'certificate', 'coupon_id', 'owner_id'))
        previews.append({'file': path.name, 'assistant_source_sha256': row.get('assistant_source_sha256'),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'input_bytes': len(serialized), 'within_12kib': len(serialized) <= 12 * 1024,
            'history_entries': len(content['history']), 'question': content['question'], 'scope_hash': row['scope_hash']})
    current_hash = hashlib.sha256((ROOT / 'services/guide/assistant.py').read_bytes()).hexdigest()
    current_previews = [row for row in previews if row.get('assistant_source_sha256') == current_hash]
    assert current_previews and all(row['within_12kib'] for row in current_previews), 'current_input_exceeds_limit'
    minimal = [row for row in current_previews if row['history_entries'] == 0]
    assert minimal, 'no_empty_history_shared_preview'
    value = {'business_unchanged': True, 'tables': len(after['tables']),
             'authentication': after['authentication'], 'previews': previews,
             'current_source_previews': len(current_previews), 'current_inputs_within_12kib': True,
             'minimum_empty_history': min(minimal, key=lambda row: row['input_bytes']),
             'real_model_calls': 0, 'approved_to_send': False,
             'stop_reasons': ['22_request_limit_exhausted', 'new_shared_fields_not_approved']}
    (OUT / 'sample-review.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in value.items() if k != 'previews'}, ensure_ascii=False))


def audit():
    before, after = (read(OUT / f'snapshot-{phase}.json') for phase in ('before', 'after'))
    assert all(after['checks'].values())
    for collection in (before['files']['protected'], before['previous_rounds']):
        assert all(sha(ROOT / p) == h for p, h in collection.items())
    output = subprocess.check_output(['git', '-c', f"safe.directory={ROOT.as_posix()}", 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = sorted({p.decode('utf-8') for p in output.split(b'\0') if p})
    secrets = [read(ROOT / '.local/guide-model.json')['api_key'],
        *read(ROOT / '.local/business.json').values(),
        *read(ROOT / '.local/d3-20/sandbox/business.json').values()]
    matches = [p for p in paths if any(v.encode() in (ROOT / p).read_bytes() for v in secrets if len(v) > 8)]
    assert not matches, matches
    python_files = [p for p in paths if p.endswith('.py')]
    for p in python_files:
        ast.parse((ROOT / p).read_text(encoding='utf-8-sig'), filename=p)
    markdown = [p for p in paths if p.endswith('.md')]
    links = 0
    for name in markdown:
        path = ROOT / name
        content = path.read_text(encoding='utf-8-sig')
        assert sum(line.startswith('```') for line in content.splitlines()) % 2 == 0, name
        for match in re.finditer(r'\]\(([^)]+)\)', content):
            target = match.group(1).strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            target = unquote(target.split('#', 1)[0])
            if target:
                links += 1
                assert (path.parent / target).exists(), (name, target)
    from services.guide.budget import BudgetLedger
    budget = BudgetLedger(ROOT / '.local/guide-usage.json').summary()
    assert budget == before['files']['budget'] == after['files']['budget']
    web = read(OUT / 'offline-browser.json')
    sample = read(OUT / 'sample-review.json')
    runtime = read(OUT / 'runtime-final.json')
    guard = read(OUT / 'server-transport-guard.json')
    assert web['state'] == 'passed' and len(web['checks']) == 5
    assert web['business_writes'] == web['physical_model_requests'] == 0
    assert sample['business_unchanged'] and sample['current_inputs_within_12kib']
    assert runtime['listeners'] == [] and runtime['api_exit'] == 0
    assert runtime['original_postgres_stop_exit'] == runtime['sandbox_postgres_stop_exit'] == 0
    assert guard['model_send_attempts'] == guard['budget_reserve_attempts'] == 0
    value = {'deliverables': len(paths), 'python_ast': len(python_files), 'markdown': len(markdown),
             'local_links': links, 'secret_matches': matches, 'preservation': after['checks'],
             'budget': budget, 'audit_model_calls': 0, 'web_checks': len(web['checks']),
             'current_inputs_within_12kib': True, 'runtime_stopped': True, 'model_transport_attempts': 0}
    (OUT / 'delivery-audit.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('before', 'after', 'audit', 'sample'))
    phase = parser.parse_args().phase
    sample_review() if phase == 'sample' else (audit() if phase == 'audit' else snapshot(phase))
