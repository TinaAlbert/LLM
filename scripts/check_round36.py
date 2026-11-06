"""P36批准前最小保全/新包/输入检查；不发送模型、不启动HTTP或PG。"""
from copy import deepcopy
import ast
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_round31 import read, save, sha
from scripts import round29_task as task
from services.guide.assistant import SYSTEM_V10
from services.guide.budget import BudgetLedger
from services.guide.experiment import input_size_bytes
from services.guide.fair_runtime import fair_input
OUT = ROOT / '.local/d3-36'


def check():
    task.configure_round36()
    before = read(OUT / 'preservation-before.json')
    assert all(sha(ROOT / row['archive']) == row['sha256'] for row in before['implementation'])
    assert sha(OUT / 'frozen-q26/review-manifest.json') == before['source_manifest_sha256']
    assert all(sha(OUT / 'frozen-q26/review' / name) == value for name, value in before['review_files'].items())
    assert all(sha(ROOT / name) == value for name, value in before['evidence_sha256'].items())
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    summary = ledger.summary()
    assert summary == before['ledger'] and summary['physical_requests'] == 35 and summary['unresolved_requests'] == 0
    assert len(before['legacy_sidecars']) == 22
    assert ledger.q26_stop_path.exists() and not ledger.q27_path.exists() and not ledger.q27_stop_path.exists()
    assert not (OUT / 'decision.json').exists()
    review = OUT / 'review'
    manifest = task.checked_review(review)
    assert manifest['package'] == 'Q27-P36-current-result' and manifest['action_contract_version'] == 'single-action-6'
    assert (manifest['request_ceiling'], manifest['reserved_ceiling_cny'], manifest['prefix_requests'], manifest['legacy_sidecars']) == (15, '0.480', 35, 22)
    old = read(OUT / 'frozen-q26/review-manifest.json')['implementation_sha256']
    for name in ('services/guide/assistant.py', 'services/business/assistant_projection.py', 'scripts/round28_records.py', 'scripts/round28_run.py'):
        assert sha(ROOT / name) == old[name]
    def prompt(path):
        return next(ast.literal_eval(n.value) for n in ast.parse(path.read_text(encoding='utf-8-sig')).body
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ACTION_PROMPT' for t in n.targets))
    assert prompt(ROOT / 'services/guide/action_contract.py') == prompt(OUT / 'frozen-q26/services/guide/action_contract.py')
    sizes = []
    for seq, group in enumerate(('B1', 'B0', 'B0', 'B1'), 1):
        source, actual = (read(review / f'initial-{seq}-{kind}.json') for kind in ('source', 'actual'))
        assert source['messages'][0]['content'] == SYSTEM_V10
        assert actual == fair_input(source, group, prompt_version='assistant-10')
        row = {'sequence': seq, 'source': input_size_bytes(source), 'actual': input_size_bytes(actual),
               'full_actual': len(json.dumps(actual, ensure_ascii=False, separators=(',', ':')).encode())}
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
        estimates[label] = {group: input_size_bytes(fair_input(payload, group, prompt_version='assistant-10')) for group in ('B0', 'B1')}
        assert all(value <= 12288 for value in estimates[label].values())
    result = {'kind': 'p36_preservation_and_input_check', 'q26_archive_files': len(before['implementation']),
        'old_evidence_files': len(before['evidence_sha256']), 'legacy_sidecars': before['legacy_sidecars'],
        'initial_sizes': sizes, 'required_final_state_fixed_feedback_estimates': estimates,
        'ledger': summary, 'manifest_sha256': sha(review / 'review-manifest.json'),
        'batch_request_ceiling': 15, 'batch_reserved_ceiling_cny': '0.480', 'earlier_first_phase_remaining_requests': 15,
        'model_calls': 0, 'http_calls': 0, 'database_calls': 0, 'new_permission_created': False,
        'limitation': 'Finite Chinese guard; estimates do not guarantee future inputs; per-round source/actual gates and earlier stops remain enforced.'}
    assert not (OUT / 'targeted-check.json').exists(), 'do_not_overwrite_evidence'
    save(OUT / 'targeted-check.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    check()
