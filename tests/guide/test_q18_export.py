"""Q18精确输入到有序许可与第二题交付门；仅临时账本/MockTransport。"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time

import httpx
import pytest

from services.guide.assistant import authorize_shared_export, corporate_export_scope
from services.guide.budget import BudgetUnavailable, Reservation
from services.guide.experiment import NAVIGATION_TOOL, approval_values
from services.guide.knowledge import fingerprint
from services.guide.model import (DeepSeekModelAdapter, InputTokenCounter, MODEL, PROMPT_SHA256,
                                  ModelConfig, ModelUnavailable, ROOT, _today)
from test_q16_budget import response
from test_q18_budget import CASES, SCOPES, ledger


def invoke(ledger, tmp_path, value, handler, *, batch='q18', validator=None, strict=False):
    approval = tmp_path / 'experiment.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    options = {batch + '_grant_path': getattr(ledger, batch + '_path')} if batch else {}
    adapter = DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(),
                           'user_reported', 'test', Decimal(0)),
        input_validator=validator or (lambda value: value),
        token_counter=InputTokenCounter(MODEL, PROMPT_SHA256, 'mock-only', lambda _: 700),
        transport=httpx.MockTransport(handler), protocol_report_dir=tmp_path / 'd3-26/protocol', **options)
    if strict:
        return asyncio.run(adapter.generate({'goal': '合成目标'}, time.monotonic() + 5))
    return asyncio.run(adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval))


def first_review(ledger, tmp_path):
    """由独立预期构造正式首答证据，不使用模型输出自评。"""
    directory = tmp_path / 'd3-26'
    directory.mkdir(exist_ok=True)
    sample = {'http': 200, 'business_unchanged': True, 'reply': {'content': '合成正式首答'}}
    sample_path = directory / 'sample-1.json'
    sample_path.write_text(json.dumps(sample, ensure_ascii=False), encoding='utf-8')
    sql = {'passed': True, 'sample_sha256': hashlib.sha256(sample_path.read_bytes()).hexdigest()}
    checked = {'passed': True, 'known_usage': True, 'business_unchanged': True,
        'independent_sql': True, 'content_reviewed': True, 'scope_hash': SCOPES[0],
        'reply_hash': fingerprint(sample['reply'])}
    row = ledger.receipt_snapshot()[24]
    raw = {'ledger_row_matched': True, 'scope_hash': SCOPES[0], 'ledger_request_id': row['request_id'],
        'provider_request_id_sha256': hashlib.sha256(row['provider_request_id'].encode()).hexdigest(),
        'usage': {'prompt_tokens': row['prompt_tokens'], 'completion_tokens': row['completion_tokens'],
            'total_tokens': row['prompt_tokens'] + row['completion_tokens'],
            'cached_tokens': row['cache_hit_tokens'], 'cache_miss_tokens': row['cache_miss_tokens']},
        'amount_cny': row['accounted_cny']}
    for name, value in (('sample-1-sql-check.json', sql), ('sample-1-reviewed.json', checked),
                        ('raw-result-1.json', raw)):
        (directory / name).write_text(json.dumps(value), encoding='utf-8')


def test_q18_validated_ordered_scopes_fixed_transport_and_independent_delivery(ledger, tmp_path):
    ledger.create_q18_grant(SCOPES)
    before = ledger.receipt_snapshot()
    stops = {p: p.read_bytes() for p in (ledger.q16_stop_path, ledger.q17_stop_path)}
    calls = []
    def handler(request):
        calls.append(request)
        value = json.loads(request.content)
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert value['thinking'] == {'type': 'disabled'} and value['stream'] is False
        assert value['max_tokens'] == 512 and value['tools'] == [NAVIGATION_TOOL]
        assert value['tool_choice'] == 'auto' and json.loads(value['messages'][-1]['content'])['history'] == []
        return httpx.Response(200, json=response('scratch-q18-' + str(len(calls))))
    invoke(ledger, tmp_path, deepcopy(CASES[0]), handler)
    first_review(ledger, tmp_path)
    invoke(ledger, tmp_path, deepcopy(CASES[1]), handler)
    assert len(calls) == 2 and ledger.summary()['physical_requests'] == 26
    assert ledger.receipt_snapshot()[:24] == before
    assert all(p.read_bytes() == data for p, data in stops.items())
    with pytest.raises(ModelUnavailable, match='q18_budget_stop'):
        invoke(ledger, tmp_path, CASES[0], handler)
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['question', 'system', 'state', 'history', 'tool', 'output',
                                    'json', 'self_id', 'ordinary_adapter', 'validator_reject'])
def test_q18_unapproved_input_or_entry_never_sends_or_reserves(ledger, tmp_path, change):
    ledger.create_q18_grant(SCOPES)
    value = deepcopy(CASES[0]); validator = None
    context = json.loads(value['messages'][-1]['content'])
    if change == 'question': context['question'] += '未批准修改'
    if change == 'state': context['current_state']['delegated_refs'] = ['trip_1']
    if change == 'history': context['history'] = [{'text': '额外历史'}]
    if change == 'system': value['messages'][0]['content'] += '额外系统字段'
    if change == 'tool': value['tools'] = []
    if change == 'output': value.update(max_tokens=1024, json_output=True)
    if change == 'json': value['json_output'] = True
    if change == 'self_id': value['q18'] = 'approved'
    if change == 'validator_reject':
        def validator(_): raise ValueError('scratch rejected')
    value['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False)
    with pytest.raises(ModelUnavailable):
        invoke(ledger, tmp_path, value, lambda _: pytest.fail('must not send'),
               batch=None if change == 'ordinary_adapter' else 'q18', validator=validator)
    assert ledger.summary()['physical_requests'] == 24 and not ledger.q18_stop_path.exists()


def test_q18_uses_copied_validator_result_for_scope(ledger, tmp_path):
    ledger.create_q18_grant(SCOPES)
    def validator(value):
        result = deepcopy(value)
        result['messages'][0]['content'] += '校验后修改'
        return result
    with pytest.raises(ModelUnavailable, match='q18_scope_or_order_mismatch'):
        invoke(ledger, tmp_path, CASES[0], lambda _: pytest.fail('must not send'), validator=validator)
    assert ledger.summary()['physical_requests'] == 24


@pytest.mark.parametrize('old', ['q16', 'q17'])
def test_q18_conflicts_with_each_old_batch_parameter(ledger, old):
    with pytest.raises(ModelUnavailable, match='batch_grant_conflict'):
        DeepSeekModelAdapter(ledger, q18_grant_path=ledger.q18_path,
                             **{old + '_grant_path': getattr(ledger, old + '_path')})


@pytest.mark.parametrize('directory', [None, ROOT / '.local/d3-15', ROOT / '.local/d3-25/protocol'])
def test_q18_requires_explicit_new_protocol_directory(ledger, directory):
    with pytest.raises(ModelUnavailable, match='q18_protocol_directory_'):
        DeepSeekModelAdapter(ledger, q18_grant_path=ledger.q18_path, protocol_report_dir=directory)


@pytest.mark.parametrize('entry', ['strict', 'ordinary', 'q16', 'q17'])
def test_other_entry_cannot_consume_q18(ledger, tmp_path, entry):
    ledger.create_q18_grant(SCOPES)
    before = ledger.path.read_bytes()
    reason = entry + '_batch_stopped' if entry in ('q16', 'q17') else 'q18_scope_required'
    with pytest.raises(ModelUnavailable, match=reason):
        invoke(ledger, tmp_path, CASES[0], lambda _: pytest.fail('must not send'),
               batch=entry if entry in ('q16', 'q17') else None, strict=entry == 'strict')
    assert ledger.path.read_bytes() == before and not ledger.q18_stop_path.exists()


@pytest.mark.parametrize('failure', ['usage_missing', 'timeout', 'http_error', 'candidate_invalid', 'length'])
def test_q18_attempt_failure_stops_before_minimal_receipt_and_never_retries(ledger, tmp_path, failure):
    ledger.create_q18_grant(SCOPES)
    calls = []
    def handler(_):
        calls.append(1)
        if failure == 'timeout': raise httpx.ReadTimeout('scratch')
        if failure == 'http_error': return httpx.Response(503)
        value = response('scratch-q18-error')
        if failure == 'usage_missing': del value['usage']
        if failure == 'candidate_invalid': value['choices'][0]['message']['refusal'] = 'scratch'
        if failure == 'length': value['choices'][0]['finish_reason'] = 'length'
        return httpx.Response(200, json=value)
    with pytest.raises(ModelUnavailable):
        invoke(ledger, tmp_path, CASES[0], handler)
    stop = json.loads(ledger.q18_stop_path.read_text(encoding='utf-8'))
    stored = json.loads(next((tmp_path / 'd3-26/protocol').glob('failure-receipt-*.json')).read_text())
    assert stored['failure_category'] == stop['reason']
    known = failure in ('candidate_invalid', 'length')
    assert stored['usage_verified'] is known
    assert ledger.summary()['unresolved_requests'] == (0 if known else 1)
    if not known:
        row = ledger.receipt_snapshot()[-1]
        ledger.settle(Reservation(row['request_id'], Decimal(row['reserved_cny'])),
            prompt_tokens=100, completion_tokens=20, provider_request_id='scratch-found-usage')
    with pytest.raises(ModelUnavailable, match='q18_batch_stopped'):
        invoke(ledger, tmp_path, CASES[1], handler)
    assert calls == [1]


@pytest.mark.parametrize('change', ['missing', 'http', 'business', 'sql', 'sql_hash', 'review', 'known',
                                    'content', 'scope', 'reply', 'usage', 'request', 'amount'])
def test_q18_second_send_requires_exact_first_formal_delivery_and_review(ledger, tmp_path, change):
    ledger.create_q18_grant(SCOPES)
    invoke(ledger, tmp_path, CASES[0], lambda _: httpx.Response(200, json=response('scratch-first')))
    first_review(ledger, tmp_path)
    directory = tmp_path / 'd3-26'
    if change == 'missing':
        (directory / 'sample-1-reviewed.json').unlink()
    else:
        name, key, replacement = {
            'http': ('sample-1.json', 'http', 503),
            'business': ('sample-1.json', 'business_unchanged', False),
            'sql': ('sample-1-sql-check.json', 'passed', False),
            'sql_hash': ('sample-1-sql-check.json', 'sample_sha256', 'f' * 64),
            'review': ('sample-1-reviewed.json', 'passed', False),
            'known': ('sample-1-reviewed.json', 'known_usage', False),
            'content': ('sample-1-reviewed.json', 'content_reviewed', False),
            'scope': ('sample-1-reviewed.json', 'scope_hash', SCOPES[1]),
            'reply': ('sample-1-reviewed.json', 'reply_hash', 'f' * 64),
            'usage': ('raw-result-1.json', 'usage', None),
            'request': ('raw-result-1.json', 'ledger_request_id', 'f' * 32),
            'amount': ('raw-result-1.json', 'amount_cny', '0'),
        }[change]
        path = directory / name
        value = json.loads(path.read_text(encoding='utf-8')); value[key] = replacement
        path.write_text(json.dumps(value), encoding='utf-8')
    before = ledger.path.read_bytes()
    with pytest.raises(ModelUnavailable, match='q18_first_delivery_not_verified'):
        invoke(ledger, tmp_path, CASES[1], lambda _: pytest.fail('must not send'))
    assert ledger.path.read_bytes() == before and ledger.summary()['physical_requests'] == 25


def test_shared_export_q18_is_explicit_and_bound_to_exact_payload(tmp_path):
    path = tmp_path / 'shared.json'
    path.write_text(json.dumps({'package': 'P26-Q18-two', 'approved': True,
        'source_kind': 'explicit_user_reply', 'scope_hashes': list(SCOPES)}), encoding='utf-8')
    authorize_shared_export(CASES[0], path)
    changed = deepcopy(CASES[0]); changed['messages'][0]['content'] += '变更'
    assert corporate_export_scope(changed) != SCOPES[0]
    with pytest.raises(ModelUnavailable, match='shared_export_not_approved'):
        authorize_shared_export(changed, path)
