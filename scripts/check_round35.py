"""P35批准前定向保全和输入检查；零模型/HTTP/PG，不运行旧34前置。"""
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_round31 import read, save, sha
from services.guide.assistant import SYSTEM_V10
from services.guide.budget import BudgetLedger
from services.guide.experiment import input_size_bytes
from services.guide.fair_runtime import fair_input

OUT = ROOT / '.local/d3-35'


def check():
    before = read(OUT / 'preservation-before.json')
    for row in before['implementation']:
        assert sha(ROOT / row['archive']) == row['sha256'], row['source']
    assert sha(OUT / 'frozen-q25/review-manifest.json') == before['source_manifest_sha256']
    assert all(sha(ROOT / p) == h for p, h in before['evidence_sha256'].items())
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    summary = ledger.summary()
    assert summary == before['ledger']
    assert summary['physical_requests'] == 34 and summary['unresolved_requests'] == 0
    assert not ledger.q26_path.exists() and not ledger.q26_stop_path.exists()
    assert not (OUT / 'decision.json').exists()
    review = OUT / 'review'; manifest = read(review / 'review-manifest.json')
    assert manifest['package'] == 'Q26-P35-explanation-following'
    assert manifest['action_contract_version'] == 'single-action-5'
    assert (manifest['request_ceiling'], manifest['reserved_ceiling_cny'],
            manifest['prefix_requests'], manifest['legacy_sidecars']) == (16, '0.512', 34, 20)
    assert all(sha(ROOT / p) == h for p, h in manifest['implementation_sha256'].items())
    assert all(sha(review / p) == h for p, h in manifest['files'].items())
    # 业务资料、原事件与独立判据未变；生成端新10单独版本化。
    old = read(OUT / 'frozen-q25/review-manifest.json')['implementation_sha256']
    for p in ('services/business/assistant_projection.py', 'scripts/round28_records.py', 'scripts/round28_run.py'):
        assert sha(ROOT / p) == old[p]
    import ast
    def nodes(path):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        return {n.name if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) else n.targets[0].id: ast.dump(n, include_attributes=False)
                for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
    previous = nodes(OUT/'frozen-q25/services/guide/assistant.py')
    current = nodes(ROOT/'services/guide/assistant.py')
    for name in ('SYSTEM_V9', 'validate_shared_reply', 'validate_semantic_reply', 'validate_single_action_reply'):
        assert current[name] == previous[name], name
    assert nodes(ROOT/'services/guide/action_contract.py')['ACTION_PROMPT'] == nodes(OUT/'frozen-q25/services/guide/action_contract.py')['ACTION_PROMPT']
    sizes = []
    for index, group in enumerate(('B1', 'B0', 'B0', 'B1'), 1):
        source, actual = (read(review / f'initial-{index}-{kind}.json') for kind in ('source', 'actual'))
        assert source['messages'][0]['content'] == SYSTEM_V10
        assert actual == fair_input(source, group, prompt_version='assistant-10')
        row = {'sequence': index, 'source': input_size_bytes(source), 'actual': input_size_bytes(actual),
               'full_actual': len(json.dumps(actual, ensure_ascii=False, separators=(',', ':')).encode())}
        assert row['source'] <= 12288 and row['actual'] <= 12288
        sizes.append(row)
    source = read(review / 'initial-1-source.json')
    final = read(ROOT / '.local/d3-28/tasks-final/DEV27-05-B1/fair-inputs.json')[-1]
    context = json.loads(source['messages'][-1]['content'])
    end_context = json.loads((final.get('source_input') or final['input'])['messages'][-1]['content'])
    for key in ('current_state', 'current_observation', 'current_page'):
        context[key] = deepcopy(end_context[key])
    from services.business.assistant_projection import SHARED_DOCUMENT_V2
    context['current_state']['documented'] = deepcopy(SHARED_DOCUMENT_V2)
    context['history'] = []
    estimates = {}
    for label, question in {
        'accepted_lock': '我尝试了trip_1的锁价，页面返回请求已受理。',
        'query_succeeded': '我查询了trip_1，页面显示付款状态succeeded，票据状态succeeded。',
        'error_32ascii_estimate': '我尝试了trip_1的锁价，页面返回请求被拒绝，错误为' + 'x' * 32 + '。',
    }.items():
        context['question'] = question; payload = deepcopy(source)
        payload['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
        estimates[label] = {group: input_size_bytes(fair_input(payload, group, prompt_version='assistant-10'))
                            for group in ('B0', 'B1')}
        assert all(size <= 12288 for size in estimates[label].values())
    result = {'kind': 'p35_preservation_and_input_check',
        'q25_archive_files': len(before['implementation']), 'old_evidence_files': len(before['evidence_sha256']),
        'legacy_sidecars': before['legacy_sidecars'],
        'initial_sizes': sizes, 'required_final_state_fixed_feedback_estimates': estimates,
        'ledger': summary, 'manifest_sha256': sha(review / 'review-manifest.json'),
        'batch_request_ceiling': 16, 'batch_reserved_ceiling_cny': '0.512',
        'earlier_first_phase_remaining_requests': 16,
        'model_calls': 0, 'http_calls': 0, 'database_calls': 0, 'new_permission_created': False,
        'limitation': 'Finite Chinese guard. Final-state estimates are not future-input guarantees; per-round source/actual gates remain enforced. Original first-phase 50-request stop remains active.'}
    save(OUT / 'targeted-check.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    check()
