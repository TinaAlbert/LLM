"""P33批准前定向保全和输入检查；零模型/HTTP/PG，不运行旧32前置。"""
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_round31 import read, save, sha
from services.guide.assistant import SYSTEM_V9
from services.guide.budget import BudgetLedger
from services.guide.experiment import input_size_bytes
from services.guide.fair_runtime import fair_input

OUT = ROOT / '.local/d3-33'


def check():
    before = read(OUT / 'preservation-before.json')
    for row in before['implementation']:
        assert sha(ROOT / row['archive']) == row['sha256'], row['source']
    assert sha(OUT / 'frozen-q23/review-manifest.json') == before['source_manifest_sha256']
    assert all(sha(ROOT / p) == h for p, h in before['evidence_sha256'].items())
    # 32已有的旧关键原件映射继续核验；账本在Q23实发时已正常从30增至32，
    # 因而本轮账本/身份/16侧车以本轮修改前快照为准，不拿旧30笔hash冒充现状。
    inherited = read(ROOT / '.local/d3-32/preservation-before.json')['evidence_sha256']
    inherited = {p: h for p, h in inherited.items() if not p.startswith('.local/guide-usage.json')}
    assert all(sha(ROOT / p) == h for p, h in inherited.items())
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    summary = ledger.summary()
    assert summary == before['ledger']
    assert summary['physical_requests'] == 32 and summary['unresolved_requests'] == 0
    assert not ledger.q24_path.exists() and not ledger.q24_stop_path.exists()
    assert not (OUT / 'decision.json').exists()
    review = OUT / 'review'; manifest = read(review / 'review-manifest.json')
    assert manifest['package'] == 'Q24-P33-query-state'
    assert manifest['action_contract_version'] == 'single-action-3'
    assert all(sha(ROOT / p) == h for p, h in manifest['implementation_sha256'].items())
    assert all(sha(review / p) == h for p, h in manifest['files'].items())
    # assistant-9和知识/独立判据本轮未变；只修复宿主及显式新批接线。
    old = read(OUT / 'frozen-q23/review-manifest.json')['implementation_sha256']
    for p in ('services/guide/assistant.py', 'services/business/assistant_projection.py',
              'services/guide/task_scope.py', 'scripts/round28_records.py', 'scripts/round28_run.py'):
        assert sha(ROOT / p) == old[p]
    sizes = []
    for index, group in enumerate(('B1', 'B0', 'B0', 'B1'), 1):
        source, actual = (read(review / f'initial-{index}-{kind}.json') for kind in ('source', 'actual'))
        assert source['messages'][0]['content'] == SYSTEM_V9
        assert actual == fair_input(source, group, prompt_version='assistant-9')
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
        estimates[label] = {group: input_size_bytes(fair_input(payload, group, prompt_version='assistant-9'))
                            for group in ('B0', 'B1')}
        assert all(size <= 12288 for size in estimates[label].values())
    result = {'kind': 'p33_preservation_and_input_check',
        'q23_archive_files': len(before['implementation']), 'old_evidence_files': len(before['evidence_sha256']),
        'inherited_nonledger_files': len(inherited), 'legacy_sidecars': before['legacy_sidecars'],
        'initial_sizes': sizes, 'required_final_state_fixed_feedback_estimates': estimates,
        'ledger': summary, 'manifest_sha256': sha(review / 'review-manifest.json'),
        'batch_request_ceiling': 20, 'earlier_first_phase_remaining_requests': 18,
        'model_calls': 0, 'http_calls': 0, 'database_calls': 0, 'new_permission_created': False,
        'limitation': 'Finite Chinese guard. Final-state estimates are not future-input guarantees; per-round source/actual gates remain enforced. Original first-phase 50-request stop remains active.'}
    save(OUT / 'targeted-check.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    check()
