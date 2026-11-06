"""第20轮独立保护与交付核查；不调用模型、不写保留业务空间。"""
import argparse
import json
from pathlib import Path
import sys
import ast
import re
import subprocess
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.llm_round19 import protection
from scripts.d3_evaluate import business_snapshot
from scripts.llm_round17 import terminal
from services.business.config import load_settings

OUT = ROOT / '.local/d3-20'


def snapshot(phase):
    value = {'files': protection(), 'dev': business_snapshot(load_settings()),
             'test': business_snapshot(load_settings(test=True)), 'terminal': terminal()}
    # 第19轮所有原件与当前全部源码在修改前额外留摘要。
    from scripts.llm_round19 import sha
    value['round19'] = {p.relative_to(ROOT).as_posix(): sha(p)
                        for p in (ROOT / '.local/d3-19').rglob('*') if p.is_file()}
    if phase == 'before':
        assert value['files']['budget']['physical_requests'] == 21
        assert value['files']['budget']['unresolved_requests'] == 0
    else:
        before = json.loads((OUT / 'snapshot-before.json').read_text(encoding='utf-8'))
        a, b = value['files'], before['files']
        value['checks'] = {
            'business_unchanged': all(value[k]['sha256'] == before[k]['sha256'] for k in ('dev', 'test')),
            'protected_files': all(a['protected'].get(k) == h for k, h in b['protected'].items()),
            'round19_unchanged': value['round19'] == before['round19'],
            'ledger_prefix': a['ledger_request_hashes'][:21] == b['ledger_request_hashes'],
            'ledger_identity': a['ledger_identity'] == b['ledger_identity'],
            'old_candidates': all(a['candidate_hashes'].get(k) == h for k, h in b['candidate_hashes'].items()),
        }
        assert all(value['checks'].values()), 'preservation_failed'
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f'snapshot-{phase}.json').open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({'phase': phase, 'budget': value['files']['budget'],
                      'active': value['files']['active'], 'terminal': value['terminal']['counts'],
                      'protected_files': len(value['files']['protected']),
                      'checks': value.get('checks')}, ensure_ascii=False))


def audit():
    """只读静态交付/既有证据核验；结果写本轮目录，不连接PG或模型。"""
    from scripts.llm_round19 import sha
    read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
    q15 = (OUT / 'snapshot-after-q15.json').exists()
    before = read(OUT / 'snapshot-before.json')
    after = read(OUT / ('snapshot-after-q15.json' if q15 else 'snapshot-after.json'))
    assert all(after['checks'].values())
    assert all(sha(ROOT / p) == h for p, h in before['files']['protected'].items())
    assert all(sha(ROOT / p) == h for p, h in before['round19'].items())
    files = subprocess.check_output(['git', '-c', f"safe.directory={ROOT.as_posix()}",
        'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = sorted(set(p.decode('utf-8') for p in files.split(b'\0') if p))
    credentials = read(ROOT / '.local/guide-model.json')
    secret_values = [credentials['api_key'], *read(ROOT / '.local/business.json').values(),
                     *read(OUT / 'sandbox/business.json').values()]
    secret_matches = [p for p in paths if any(value.encode() in (ROOT / p).read_bytes()
                                              for value in secret_values if len(value) > 8)]
    assert not secret_matches, secret_matches
    python_files = [p for p in paths if p.endswith('.py')]
    for name in python_files:
        ast.parse((ROOT / name).read_text(encoding='utf-8-sig'), filename=name)
    markdown = [p for p in paths if p.endswith('.md')]
    link_count = 0
    for name in markdown:
        path = ROOT / name
        content = path.read_text(encoding='utf-8-sig')
        assert sum(1 for line in content.splitlines() if line.startswith('```')) % 2 == 0, name
        for match in re.finditer(r'\]\(([^)]+)\)', content):
            target = match.group(1).strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            target = unquote(target.split('#', 1)[0])
            if target:
                link_count += 1
                assert (path.parent / target).exists(), (name, target)
    for script in ('apps/web/tests/assistant-round20.mjs', 'apps/web/tests/assistant-round20-live.mjs'):
        subprocess.run([str(ROOT / '.local/reinstall/tools/node/node.exe'), '--check', str(ROOT / script)], check=True)
    from services.guide.budget import BudgetLedger
    budget = BudgetLedger(ROOT / '.local/guide-usage.json').summary()
    assert budget == after['files']['budget']
    assert budget['physical_requests'] == (22 if q15 else 21) and budget['unresolved_requests'] == 0
    if q15:
        decision = read(OUT / 'q15-decision.json')
        approval = read(OUT / 'corporate-export-approval.json')
        preview = read(OUT / 'payload-preview.json')
        assert decision['approved'] is True and decision['source_kind'] == 'explicit_user_reply'
        assert approval['scope_hash'] == decision['scope_hash'] == preview['scope_hash']
        review = read(OUT / 'q15-review.json')
        assert review['scope_matches'] and review['browser_chat_http'] == 200
        assert review['fully_correct_sample'] is False and not review['model_retried']
    else:
        assert not (OUT / 'corporate-export-approval.json').exists()
    web = read(OUT / 'offline-browser.json')
    assert web['state'] == 'passed' and len(web['checks']) == 5
    assert web['physical_model_requests'] == web['business_writes'] == 0
    runtime = read(OUT / ('runtime-q15-final.json' if q15 else 'runtime-final.json'))
    assert runtime['listeners'] == [] and runtime['original_postgres_stop_exit'] == runtime['sandbox_postgres_stop_exit'] == 0
    source_paths = ['services/business/assistant_projection.py', 'services/guide/assistant.py',
                    'services/guide/releases.py', 'apps/web/src/Assistant.tsx',
                    'scripts/round20_sandbox.py', 'scripts/round20_server.py', 'scripts/llm_round20.py',
                    'tests/guide/test_corporate20.py', 'tests/guide/test_corporate_prompt20.py',
                    'apps/web/tests/assistant-round20.mjs', 'scripts/round20_live.py',
                    'apps/web/tests/assistant-round20-live.mjs']
    value = {'deliverables': len(paths), 'python_ast': len(python_files), 'markdown': len(markdown),
             'local_links': link_count, 'secret_matches': secret_matches,
             'protected_unchanged': len(before['files']['protected']), 'round19_unchanged': True,
             'business_unchanged': True, 'ledger_budget': budget, 'corporate_export_approved': q15,
             'source_hashes': {p: sha(ROOT / p) for p in source_paths},
             'runtime_stopped': True, 'physical_models_called_by_audit': 0}
    (OUT / ('delivery-q15-audit.json' if q15 else 'delivery-audit.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: item for key, item in value.items() if key != 'source_hashes'}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['before', 'after', 'after-q15', 'audit'])
    phase = parser.parse_args().phase
    audit() if phase == 'audit' else snapshot(phase)
