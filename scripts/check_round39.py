"""P39批准前最小保全/新包/输入检查；不发送模型、不启动HTTP或PG。"""
from copy import deepcopy
import ast
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_round31 import read, save, sha
from scripts import round29_task as task
from services.guide.assistant import SYSTEM_V11
from services.guide.budget import BudgetLedger
from services.guide.experiment import input_size_bytes
from services.guide.fair_runtime import fair_input
from services.guide.knowledge import fingerprint
OUT = ROOT / '.local/d3-39'


def check():
    task.configure_round39()
    before = read(OUT / 'preservation-before.json')
    assert all(sha(ROOT / row['archive']) == row['sha256'] for row in before['implementation'])
    assert sha(OUT / 'frozen-q29/review-manifest.json') == before['source_manifest_sha256']
    assert all(sha(OUT / 'frozen-q29/review' / name) == value for name, value in before['review_files'].items())
    assert all(sha(ROOT / name) == value for name, value in before['evidence_sha256'].items())
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    summary = ledger.summary()
    assert summary == before['ledger'] and summary['physical_requests'] == 38 and summary['unresolved_requests'] == 0
    assert len(before['legacy_sidecars']) == 28
    assert ledger.q29_stop_path.exists() and not ledger.q30_path.exists() and not ledger.q30_stop_path.exists()
    assert not (OUT / 'decision.json').exists()
    review = OUT / 'review'
    manifest = task.checked_review(review)
    assert manifest['package'] == 'Q30-P39-lock-amount' and manifest['action_contract_version'] == 'single-action-8'
    assert (manifest['request_ceiling'], manifest['reserved_ceiling_cny'], manifest['prefix_requests'], manifest['legacy_sidecars']) == (12, '0.384', 38, 28)
    assert len(manifest['implementation_sha256']) == 19 and len(manifest['files']) == 16
    old = read(OUT / 'frozen-q29/review-manifest.json')['implementation_sha256']
    for name in ('services/guide/assistant.py', 'services/business/assistant_projection.py', 'scripts/round28_records.py', 'scripts/round28_run.py'):
        assert sha(ROOT / name) == old[name]
    def prompt(path):
        return next(ast.literal_eval(n.value) for n in ast.parse(path.read_text(encoding='utf-8-sig')).body
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'ACTION_PROMPT' for t in n.targets))
    assert prompt(ROOT / 'services/guide/action_contract.py') == prompt(OUT / 'frozen-q29/services/guide/action_contract.py')
    # 整个AST仅允许版本、lock字面名词及四处锁定额fail-closed守卫；注释不改变执行语义。
    prior_contract = (OUT / 'frozen-q29/services/guide/action_contract.py').read_text(encoding='utf-8-sig')
    expected_contract = prior_contract.replace('ACTION_CONTRACT_VERSION = "single-action-7"',
        'ACTION_CONTRACT_VERSION = "single-action-8"').replace('归属券ref|冻结金额|frozen金额|',
        '归属券ref|冻结金额|frozen金额|锁定额|')
    expected_contract = expected_contract.replace(
        '    if lock_return and action["kind"] != "lock":',
        '    lock_amount = "锁定额" in clause\n    if (lock_return or lock_amount) and action["kind"] != "lock":')
    expected_contract = expected_contract.replace(
        '    if match is None and not continued:\n        return False',
        '    if match is None and not continued:\n        if lock_amount:\n            _fail("action_explanation_route")\n        return False')
    expected_contract = expected_contract.replace(
        '    if pattern.fullmatch(obj) is None:\n        if match:',
        '    if pattern.fullmatch(obj) is None:\n        if match or lock_amount:')
    expected_contract = expected_contract.replace(
        'if match and re.search(r"(?:仍待|待)$", match["object"]):',
        'if match and not lock_amount and re.search(r"(?:仍待|待)$", match["object"]):')
    assert ast.dump(ast.parse((ROOT / 'services/guide/action_contract.py').read_text(encoding='utf-8-sig'))) == ast.dump(ast.parse(expected_contract))
    tasks = read(review / 'review-tasks.json')
    sizes = []
    for seq, group in enumerate(('B1', 'B0', 'B0', 'B1'), 1):
        source, actual = (read(review / f'initial-{seq}-{kind}.json') for kind in ('source', 'actual'))
        assert source['messages'][0]['content'] == SYSTEM_V11
        assert actual == fair_input(source, group, prompt_version='assistant-11')
        frozen = tasks[seq - 1]
        assert frozen['source_sha256'] == fingerprint(source) and frozen['actual_sha256'] == fingerprint(actual)
        assert frozen['prompt_version'] == 'assistant-11' and frozen['group'] == group
        assert frozen['physical_batch_limit'] == 12
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
        estimates[label] = {group: input_size_bytes(fair_input(payload, group, prompt_version='assistant-11')) for group in ('B0', 'B1')}
        assert all(value <= 12288 for value in estimates[label].values())
    result = {'kind': 'p39_preservation_and_input_check', 'q29_archive_files': len(before['implementation']),
        'old_evidence_files': len(before['evidence_sha256']), 'legacy_sidecars': before['legacy_sidecars'],
        'initial_sizes': sizes, 'required_final_state_fixed_feedback_estimates': estimates,
        'ledger': summary, 'manifest_sha256': sha(review / 'review-manifest.json'),
        'batch_request_ceiling': 12, 'batch_reserved_ceiling_cny': '0.384', 'earlier_first_phase_remaining_requests': 12,
        'model_calls': 0, 'http_calls': 0, 'database_calls': 0, 'new_permission_created': False,
        'reason_generation_limit': 'Original Q29 reason uses aggregate no-ref language; finite host acceptance does not prove assistant-11 fully followed.',
        'limitation': 'Finite Chinese guard; estimates do not guarantee future inputs; per-round source/actual gates and earlier stops remain enforced.'}
    assert not (OUT / 'targeted-check.json').exists(), 'do_not_overwrite_evidence'
    save(OUT / 'targeted-check.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    check()
