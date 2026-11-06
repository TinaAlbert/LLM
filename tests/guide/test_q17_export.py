"""Q17已校验输入到内部许可/固定MockTransport；不读本机账本和配置。"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
import time

import httpx
import pytest

from services.guide.budget import BudgetUnavailable, Reservation
from services.guide.experiment import NAVIGATION_TOOL, approval_values
from services.guide.model import DeepSeekModelAdapter, ModelConfig, ModelUnavailable, _today
from test_q16_budget import response
from test_q17_budget import CASES, SCOPES, ledger, stopped_ledger


def invoke(ledger, tmp_path, value, handler, *, enabled=True, validator=None):
    approval = tmp_path/'experiment.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    adapter = DeepSeekModelAdapter(ledger, config=ModelConfig('synthetic-test-key-not-an-account',
        'synthetic', _today(), 'user_reported', 'test', Decimal(0)),
        input_validator=validator or (lambda value: value), transport=httpx.MockTransport(handler),
        q17_grant_path=ledger.q17_path if enabled else None, protocol_report_dir=tmp_path/'protocol')
    return asyncio.run(adapter.generate_experiment(value, time.monotonic()+5, approval_path=approval))


def test_q17_actual_validated_scopes_send_only_to_fixed_official_transport(ledger, tmp_path):
    ledger.create_q17_grant(SCOPES)
    calls = []
    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert payload['thinking'] == {'type': 'disabled'} and payload['stream'] is False
        assert payload['max_tokens'] == 512
        assert payload['tools'] == [NAVIGATION_TOOL] and payload['tool_choice'] == 'auto'
        assert json.loads(payload['messages'][-1]['content'])['history'] == []
        return httpx.Response(200, json=response('scratch-q17-'+str(len(calls))))
    for case in CASES:
        invoke(ledger, tmp_path, deepcopy(case), handler)
    assert len(calls) == 2 and ledger.summary()['physical_requests'] == 25
    with pytest.raises(ModelUnavailable, match='q17_budget_stop'):
        invoke(ledger, tmp_path, CASES[0], handler)
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['question', 'system', 'state', 'history', 'tool', 'output',
                                    'json', 'self_id', 'ordinary_adapter', 'validator_reject'])
def test_q17_changed_input_or_context_refused_without_sending(ledger, tmp_path, change):
    ledger.create_q17_grant(SCOPES)
    case = deepcopy(CASES[0]); validator = None
    context = json.loads(case['messages'][-1]['content'])
    if change == 'question': context['question'] = '未批准咨询'
    if change == 'state': context['current_state']['delegated_refs'] = ['trip_1']
    if change == 'history': context['history'] = [{'source_kind': 'user_reported', 'text': '额外历史'}]
    if change == 'system': case['messages'][0]['content'] += '未审查系统字段'
    if change == 'tool': case['tools'] = []
    if change == 'output': case['max_tokens'] = 1024; case['json_output'] = True
    if change == 'json': case['json_output'] = True
    if change == 'self_id': case['q17'] = 'approved'
    if change == 'validator_reject':
        def validator(_): raise ValueError('scratch rejected')
    case['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False)
    with pytest.raises(ModelUnavailable):
        invoke(ledger, tmp_path, case, lambda _: pytest.fail('must not send'),
               enabled=change != 'ordinary_adapter', validator=validator)
    assert ledger.summary()['physical_requests'] == 23 and not ledger.q17_stop_path.exists()


def test_q17_validation_result_not_original_input_determines_scope(ledger, tmp_path):
    ledger.create_q17_grant(SCOPES)
    def validator(value):
        result = deepcopy(value)
        result['messages'][0]['content'] += '校验后改变提示'
        return result
    with pytest.raises(ModelUnavailable, match='q17_scope_or_order_mismatch'):
        invoke(ledger, tmp_path, CASES[0], lambda _: pytest.fail('must not send'), validator=validator)
    assert ledger.summary()['physical_requests'] == 23


def test_q16_and_q17_grant_paths_cannot_be_combined(ledger):
    with pytest.raises(ModelUnavailable, match='batch_grant_conflict'):
        DeepSeekModelAdapter(ledger, q16_grant_path=ledger.q16_path, q17_grant_path=ledger.q17_path)


@pytest.mark.parametrize('failure', ['usage_missing', 'timeout', 'http_error', 'candidate_invalid'])
def test_q17_failed_attempt_permanently_closes_remaining_scope(ledger, tmp_path, failure):
    ledger.create_q17_grant(SCOPES)
    calls = []
    def handler(_):
        calls.append(1)
        if failure == 'timeout': raise httpx.ReadTimeout('scratch timeout')
        if failure == 'http_error': return httpx.Response(503, json={})
        body = response('scratch-q17-error')
        if failure == 'usage_missing': del body['usage']
        if failure == 'candidate_invalid': body['choices'][0]['message']['refusal'] = 'scratch refusal'
        return httpx.Response(200, json=body)
    with pytest.raises(ModelUnavailable):
        invoke(ledger, tmp_path, CASES[0], handler)
    assert ledger.q17_stop_path.is_file()
    assert ledger.summary()['physical_requests'] == 24
    assert ledger.summary()['unresolved_requests'] == (0 if failure == 'candidate_invalid' else 1)
    with pytest.raises(ModelUnavailable):
        invoke(ledger, tmp_path, CASES[1], handler)
    assert len(calls) == 1
    if failure != 'candidate_invalid':
        # 即使工程之后找到usage并结算，原失败批次也不能自动恢复。
        row = ledger.receipt_snapshot()[-1]
        ledger.settle(Reservation(row['request_id'], Decimal(row['reserved_cny'])),
            prompt_tokens=100, completion_tokens=20, provider_request_id='scratch-found-usage')
    with pytest.raises(BudgetUnavailable, match='q17_batch_stopped'):
        ledger.check_q17_scope(SCOPES[1])
    if failure == 'candidate_invalid':
        assert len(list((tmp_path/'protocol').glob('protocol-*.json'))) == 1
