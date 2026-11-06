"""第19轮独立保护、只读终态和保存回答复验；没有模型/发布/业务写入口。"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / '.local/d3-19'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)


def protection():
    # 秘密配置只计算完整文件摘要，值不解析、不输出、不复制。
    fixed = ['services/business/requirements.lock', 'apps/web/package-lock.json',
             'docs/需求定义.md', 'docs/开发指导.md', '.local/guide-model.json',
             '.local/business.json', '.local/postgres-admin.json', '.local/postgres-project.json',
             '.local/assistant-admin.json', '.local/guide-usage.json.initialized',
             '.local/guide-usage.json.lock']
    paths = [ROOT / value for value in fixed]
    paths += sorted((ROOT / 'services/business/migrations').glob('00[1-4]*.sql'))
    paths += sorted((ROOT / '.local/assistant-runs').glob('*.json'))
    for round_number in (16, 17, 18):
        paths += [p for p in (ROOT / f'.local/d3-{round_number}').rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts]
    ledger = read(ROOT / '.local/guide-usage.json')
    releases = read(ROOT / '.local/assistant-releases-test.json')
    active = releases['candidates'][releases['active']]
    from services.guide.budget import BudgetLedger
    return {'captured_at': datetime.now(timezone.utc).isoformat(),
            'protected': {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(paths))},
            'mutable_original_hashes': {p: sha(ROOT / p) for p in (
                '.local/guide-usage.json', '.local/guide-experiment.json',
                '.local/assistant-releases-test.json', 'services/guide/budget.py',
                'services/guide/experiment.py')},
            'ledger_identity': digest({k: v for k, v in ledger.items() if k != 'requests'}),
            'ledger_request_hashes': [digest(item) for item in ledger['requests']],
            'budget': BudgetLedger(ROOT / '.local/guide-usage.json').summary(),
            'candidate_hashes': {k: digest(v) for k, v in releases['candidates'].items()},
            'active': {'version': releases['active'], 'prompt_version': active['prompt_version'],
                       'knowledge_hash': active['knowledge_hash']}}


def files(phase):
    value = protection()
    if phase == 'before':
        assert value['budget']['physical_requests'] == 16
        policy_dir = OUT / 'policy-before'
        policy_dir.mkdir(parents=True, exist_ok=True)
        for name in ('budget.py', 'experiment.py'):
            with (policy_dir / name).open('xb') as handle:
                handle.write((ROOT / 'services/guide' / name).read_bytes())
    else:
        before = read(OUT / 'files-before.json')
        value['checks'] = {
            'protected_unchanged': all(value['protected'].get(k) == h
                                       for k, h in before['protected'].items()),
            'original_16_unchanged': value['ledger_request_hashes'][:16] == before['ledger_request_hashes'],
            'ledger_identity_unchanged': value['ledger_identity'] == before['ledger_identity'],
            'old_candidates_unchanged': all(value['candidate_hashes'].get(k) == h
                                            for k, h in before['candidate_hashes'].items())}
    save(f'files-{phase}.json', value)
    print(json.dumps({'phase': phase, 'protected_files': len(value['protected']),
                      'budget': value['budget'], 'active': value['active'],
                      'checks': value.get('checks')}, ensure_ascii=False))
    if phase == 'after':
        assert all(value['checks'].values()), 'preservation_failed'


def database(phase):
    from scripts.d3_evaluate import business_snapshot
    from scripts.llm_round17 import terminal
    from services.business.config import load_settings
    value = {'captured_at': datetime.now(timezone.utc).isoformat(),
             'dev': business_snapshot(load_settings()),
             'test': business_snapshot(load_settings(test=True)), 'terminal': terminal()}
    assert len(value['dev']['tables']) == len(value['test']['tables']) == 23
    assert value['dev']['tables']['tickets']['rows'] == 6
    if phase == 'after':
        before = read(OUT / 'database-before.json')
        value['business_unchanged'] = all(value[k]['sha256'] == before[k]['sha256']
                                          for k in ('dev', 'test'))
    save(f'database-{phase}.json', value)
    print(json.dumps({'phase': phase, 'tables_per_database': 23,
                      'terminal': value['terminal'],
                      'authentication': {k: value[k]['authentication'] for k in ('dev', 'test')},
                      'business_unchanged': value.get('business_unchanged')}, ensure_ascii=False, default=str))
    if phase == 'after':
        assert value['business_unchanged'], 'business_snapshot_changed'


def original_state(shown):
    """仅内存逆变换历史金额表示；必须由生产展示函数往返验证，不修改原件。"""
    from services.guide.assistant import display_state
    state = deepcopy(shown)
    for node in state.get('data', {}).values():
        if isinstance(node, dict) and 'amount_display' in node:
            amount = node.pop('amount_display')
            node['amount_minor'] = None if amount is None else int(Decimal(amount.split()[0]) * 100)
    assert display_state(state, 'assistant-3') == shown, 'display_inverse_mismatch'
    return state


def replay(suffix=""):
    from services.guide.assistant import validate_reply
    from services.guide.releases import PROMPT_VERSION
    sources = [
        ('.local/d3-18/pair-runs/ab639f7c6d107ee7920b8284d1123ceb.json', 'accepted'),
        ('.local/d3-18/pair-runs/cdd1b03a16a9e69b06238f566885bfe6.json', 'accepted'),
        ('.local/assistant-runs/baa1763d5de0246fd5ddbaa42a1cbd90.json', 'accepted'),
        ('.local/assistant-runs/708084aa178567b6cc7e51f361b89663.json', 'amount_unverified'),
        ('.local/assistant-runs/6e2aaae0fe8df58fb44d9d0b5e82b29b.json', 'amount_unverified')]
    results = []
    for path, expected in sources:
        source = read(ROOT / path)
        context = json.loads(source['input']['messages'][-1]['content'])
        state = context['current_state']
        if any(isinstance(v, dict) and 'amount_display' in v for v in state.get('data', {}).values()):
            state = original_state(state)
        for prompt in dict.fromkeys(('assistant-3', PROMPT_VERSION)):
            try:
                validate_reply(source['candidate']['content'], state, context.get('page_observation'),
                               prompt_version=prompt)
                actual = 'accepted'
            except ValueError as exc:
                actual = str(exc)
            results.append({'source': path, 'source_sha256': sha(ROOT / path), 'prompt_version': prompt,
                            'actual': actual, 'expected': expected})
    # 独立反例确保新Prompt实际进入语义守卫，不能靠未知版本旁路得到接受。
    semantic_checks = []
    for prompt in dict.fromkeys(('assistant-3', PROMPT_VERSION)):
        for text, expected in (
                ('报价仍为 active 但属估算价（expires_at 已过）', 'business_time_conflict'),
                ('此页只说明流程，不能直接查进度', 'page_capability_unverified')):
            try:
                validate_reply(text, state, context.get('page_observation'), prompt_version=prompt)
                actual = 'accepted'
            except ValueError as exc:
                actual = str(exc)
            semantic_checks.append({'prompt_version': prompt, 'text': text,
                                    'actual': actual, 'expected': expected})
    value = {'kind': 'offline_saved_response_replay', 'model_requests': 0,
             'original_terminal_http': 503, 'original_outputs_modified': False,
             'implementation_sha256': {p: sha(ROOT / p) for p in
                 ('services/guide/assistant.py', 'services/guide/releases.py', 'scripts/llm_round19.py')},
             'results': results, 'semantic_guard_checks': semantic_checks,
             'passed': all(r['actual'] == r['expected'] for r in results + semantic_checks),
             'limitation': '只证明保存回答经过当前守卫；不替代新模型、实时交付或通用事实正确性。'}
    save(f'replay{suffix}.json', value)
    print(json.dumps(value, ensure_ascii=False))
    assert value['passed'], 'saved_response_replay_failed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('files-before', 'files-after', 'database-before', 'database-after', 'replay'))
    parser.add_argument('--suffix', default='', help='离线回放证据后缀；不覆盖旧文件')
    args = parser.parse_args()
    if args.suffix and (args.action != 'replay' or not re.fullmatch(r'[a-z0-9_-]{1,32}', args.suffix)):
        parser.error('suffix is allowed only for replay and must be 1-32 safe filename characters')
    action = args.action
    if action.startswith('files-'):
        files(action.split('-')[1])
    elif action.startswith('database-'):
        database(action.split('-')[1])
    else:
        replay('-' + args.suffix if args.suffix else '')
