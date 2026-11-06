"""P40批准前最小保全/新包/输入检查；不发送模型、不启动HTTP或PG。"""
from copy import deepcopy
import ast
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_round31 import read, save, sha
from scripts import round29_task as task
from services.guide.assistant import SYSTEM_V12
from services.guide.budget import BudgetLedger
from services.guide.experiment import input_size_bytes
from services.guide.fair_runtime import fair_input
from services.guide.knowledge import fingerprint
OUT = ROOT / '.local/d3-40'


def check():
    task.configure_round40()
    before = read(OUT / 'preservation-before.json')
    assert all(sha(ROOT / row['archive']) == row['sha256'] for row in before['implementation'])
    assert sha(OUT / 'frozen-q30/review-manifest.json') == before['source_manifest_sha256']
    assert all(sha(OUT / 'frozen-q30/review' / name) == value for name, value in before['review_files'].items())
    assert all(sha(ROOT / name) == value for name, value in before['evidence_sha256'].items())
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    summary = ledger.summary()
    assert summary == before['ledger'] and summary['physical_requests'] == 39 and summary['unresolved_requests'] == 0
    assert len(before['legacy_sidecars']) == 30
    assert ledger.q30_stop_path.exists() and not ledger.q31_path.exists() and not ledger.q31_stop_path.exists()
    assert not (OUT / 'decision.json').exists()
    review = OUT / 'review'
    manifest = task.checked_review(review)
    assert manifest['package'] == 'Q31-P40-navigation-following' and manifest['action_contract_version'] == 'single-action-8'
    assert (manifest['request_ceiling'], manifest['reserved_ceiling_cny'], manifest['prefix_requests'], manifest['legacy_sidecars']) == (11, '0.352', 39, 30)
    assert len(manifest['implementation_sha256']) == 19 and len(manifest['files']) == 16
    old = read(OUT / 'frozen-q30/review-manifest.json')['implementation_sha256']
    for name in ('services/guide/action_contract.py', 'services/business/assistant_projection.py', 'scripts/round28_records.py', 'scripts/round28_run.py'):
        assert sha(ROOT / name) == old[name]
    def prompt(path):
        return next(ast.literal_eval(n.value) for n in ast.parse(path.read_text(encoding='utf-8-sig')).body
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ACTION_PROMPT' for t in n.targets))
    assert prompt(ROOT / 'services/guide/action_contract.py') == prompt(OUT / 'frozen-q30/services/guide/action_contract.py')
    # 旧SYSTEM赋值表达式全部原样保留，仅追加新的共同生成版本。
    def assignments(path, prefix):
        return {t.id: ast.dump(n.value) for n in ast.parse(path.read_text(encoding='utf-8-sig')).body
                if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Name) and t.id.startswith(prefix)}
    old_system = assignments(OUT / 'frozen-q30/services/guide/assistant.py', 'SYSTEM')
    current_system = assignments(ROOT / 'services/guide/assistant.py', 'SYSTEM')
    assert set(current_system) == set(old_system) | {'SYSTEM_V12'}
    assert all(current_system[name] == value for name, value in old_system.items())
    new_system = next(n.value for n in ast.parse((ROOT / 'services/guide/assistant.py').read_text(encoding='utf-8-sig')).body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SYSTEM_V12' for t in n.targets))
    assert isinstance(new_system, ast.BinOp) and isinstance(new_system.op, ast.Add)
    assert isinstance(new_system.left, ast.Name) and new_system.left.id == 'SYSTEM_V11'
    assert isinstance(new_system.right, ast.Constant) and isinstance(new_system.right.value, str)
    # 模型响应解析和实际工具schema不随生成要求改变。
    def function(path, name):
        return next(ast.dump(n) for n in ast.parse(path.read_text(encoding='utf-8-sig')).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    for name in ('_json_object', '_candidate'):
        assert function(ROOT / 'services/guide/model.py', name) == function(OUT / 'frozen-q30/services/guide/model.py', name)
    assert function(ROOT / 'services/guide/experiment.py', 'parse_response') == function(OUT / 'frozen-q30/services/guide/experiment.py', 'parse_response')
    assert assignments(ROOT / 'services/guide/experiment.py', 'NAVIGATION_TOOL') == assignments(OUT / 'frozen-q30/services/guide/experiment.py', 'NAVIGATION_TOOL')
    tasks = read(review / 'review-tasks.json')
    sizes = []
    for seq, group in enumerate(('B1', 'B0', 'B0', 'B1'), 1):
        source, actual = (read(review / f'initial-{seq}-{kind}.json') for kind in ('source', 'actual'))
        assert source['messages'][0]['content'] == SYSTEM_V12
        assert actual == fair_input(source, group, prompt_version='assistant-12')
        frozen = tasks[seq - 1]
        assert frozen['source_sha256'] == fingerprint(source) and frozen['actual_sha256'] == fingerprint(actual)
        assert frozen['prompt_version'] == 'assistant-12' and frozen['group'] == group
        source_context = json.loads(source['messages'][-1]['content'])
        actual_context = json.loads(actual['messages'][-1]['content'])
        assert source_context['current_page'] == actual_context['current_page'] == 'booking'
        assert actual_context == ({k:v for k,v in source_context.items() if k not in {'current_state', 'current_observation'}} if group == 'B0' else source_context)
        assert frozen['physical_batch_limit'] == 11
        row = {'sequence': seq, 'source': input_size_bytes(source), 'actual': input_size_bytes(actual),
               'full_actual': len(json.dumps(actual, ensure_ascii=False, separators=(',', ':')).encode()),
               'source_hash': fingerprint(source), 'actual_hash': fingerprint(actual)}
        assert row['source'] <= 12288 and row['actual'] <= 12288
        sizes.append(row)
    source = read(review / 'initial-1-source.json')
    final = read(ROOT / '.local/d3-28/tasks-final/DEV27-05-B1/fair-inputs.json')[-1]
    context = json.loads(source['messages'][-1]['content'])
    end = json.loads((final.get('source_input') or final['input'])['messages'][-1]['content'])
    for key in ('current_state', 'current_observation', 'current_page'):
        context[key] = deepcopy(end[key])
    from services.business.assistant_projection import SHARED_DOCUMENT_V2
    context['current_state']['documented'] = deepcopy(SHARED_DOCUMENT_V2)
    context['history'] = []
    estimates = {}
    for label, question in {
        'accepted_lock': '我尝试了trip_1的锁价，页面返回请求已受理。',
        'query_succeeded': '我查询了trip_1，页面显示付款状态succeeded，票据状态succeeded。',
        'error_32ascii_estimate': '我尝试了trip_1的锁价，页面返回请求被拒绝，错误为' + 'x' * 32 + '。',
    }.items():
        context['question'] = question
        payload = deepcopy(source)
        payload['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
        estimates[label] = {group: input_size_bytes(fair_input(payload, group, prompt_version='assistant-12')) for group in ('B0', 'B1')}
        assert all(value <= 12288 for value in estimates[label].values())
    result = {'kind': 'p40_preservation_and_input_check', 'q30_archive_files': len(before['implementation']),
        'old_evidence_files': len(before['evidence_sha256']), 'legacy_sidecars': before['legacy_sidecars'],
        'initial_sizes': sizes, 'required_final_state_fixed_feedback_estimates': estimates,
        'ledger': summary, 'manifest_sha256': sha(review / 'review-manifest.json'),
        'batch_request_ceiling': 11, 'batch_reserved_ceiling_cny': '0.352', 'earlier_first_phase_remaining_requests': 11,
        'model_calls': 0, 'http_calls': 0, 'database_calls': 0, 'new_permission_created': False,
        'generation_limit': 'Prompt changes do not prove real-model navigation/tool, current-page or per-ref compliance; original Q30 remains a failed historical delivery and Q29 reason generation limit remains.',
        'limitation': 'Finite Chinese guard; estimates do not guarantee future inputs; per-round source/actual gates and earlier stops remain enforced.'}
    assert not (OUT / 'targeted-check.json').exists(), 'do_not_overwrite_evidence'
    save(OUT / 'targeted-check.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    check()
