"""第25轮最小脱敏失败收据；只用tmp_path与MockTransport，不读取本机许可。"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import time

import httpx
import pytest

from services.guide.budget import BudgetLedger, BudgetUnavailable, Reservation
from services.guide.experiment import NAVIGATION_TOOL, approval_values
from services.guide.model import (DeepSeekModelAdapter, InputTokenCounter, MODEL, PROMPT_SHA256,
                                  ModelConfig, ModelUnavailable, _today)
from test_q17_budget import CASES, SCOPES, stopped_ledger


CASE = {'messages': [{'role': 'user', 'content': '合成输入'}],
        'tools': [], 'max_tokens': 512, 'json_output': False}
SECRET = 'synthetic-private-response-marker'
PROVIDER_ID = 'synthetic-provider-id-not-persisted'


@pytest.fixture
def ledger(tmp_path):
    value = BudgetLedger(tmp_path / 'usage.json')
    value.initialize()
    return value


def body():
    return {'id': PROVIDER_ID, 'model': MODEL,
            'usage': {'prompt_tokens': 100, 'completion_tokens': 512, 'total_tokens': 612,
                      'prompt_cache_hit_tokens': 60, 'prompt_cache_miss_tokens': 40,
                      'provider_private': SECRET},
            'choices': [{'index': 0, 'finish_reason': 'length', 'message': {
                'role': 'assistant', 'content': SECRET,
                'tool_calls': [{'id': SECRET, 'type': 'function', 'function': {
                    'name': 'navigate', 'arguments': SECRET}}]}}],
            'private_network_response': SECRET}


def adapter(ledger, tmp_path, handler, *, batch=None, explicit_dir=True):
    options = {'q17_grant_path': ledger.q17_path} if batch == 'q17' else (
              {'q16_grant_path': ledger.q16_path} if batch == 'q16' else {})
    if explicit_dir:
        options['protocol_report_dir'] = tmp_path / 'protocol'
    return DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(),
                           'user_reported', 'synthetic-price-review'),
        token_counter=InputTokenCounter(MODEL, PROMPT_SHA256, 'mock-only', lambda _: 700),
        input_validator=lambda value: value, transport=httpx.MockTransport(handler), **options)


def invoke(current, tmp_path, case=CASE, *, strict=False):
    if strict:
        return asyncio.run(current.generate({'goal': '合成目标'}, time.monotonic() + 5))
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    return asyncio.run(current.generate_experiment(case, time.monotonic() + 5,
                                                 approval_path=approval))


def receipt(tmp_path):
    paths = list((tmp_path / 'protocol').glob('failure-receipt-*.json'))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text(encoding='utf-8'))


def test_truncated_receipt_links_only_internal_request_and_normalized_settled_usage(ledger, tmp_path):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        assert payload['max_tokens'] == 512 and payload['thinking'] == {'type': 'disabled'}
        assert payload['stream'] is False
        calls.append(1)
        return httpx.Response(200, json=body(), headers={'x-private-header': SECRET})
    with pytest.raises(ModelUnavailable, match='^output_truncated$'):
        invoke(adapter(ledger, tmp_path, handler), tmp_path)
    row = ledger.receipt_snapshot()[-1]
    path, stored = receipt(tmp_path)
    assert path.name == 'failure-receipt-' + row['request_id'] + '.json'
    assert stored == {'schema': 1, 'request_id': row['request_id'],
        'failure_category': 'output_truncated', 'finish_reason': 'length',
        'usage_verified': True, 'usage': {'prompt_tokens': 100, 'completion_tokens': 512,
            'total_tokens': 612, 'cache_hit_tokens': 60, 'cache_miss_tokens': 40},
        'accounted_cny': '0.0041784'}
    assert row['state'] == 'settled' and row['accounted_cny'] == '0.0041784'
    assert ledger.summary()['unresolved_requests'] == 0 and calls == [1]
    all_evidence = '\n'.join(p.read_text(encoding='utf-8') for p in path.parent.iterdir())
    assert SECRET not in all_evidence and PROVIDER_ID not in all_evidence
    assert 'synthetic-test-key-not-an-account' not in all_evidence
    shape = json.loads(next(path.parent.glob('protocol-*.json')).read_text(encoding='utf-8'))
    assert set(shape) == {'reason', 'choice_count', 'tool_count', 'tool_shapes'}


@pytest.mark.parametrize('failure,reason,finish', [
    ('missing', 'usage_missing', 'length'),
    ('cache', 'usage_cache_invalid', 'length'),
    ('identity', 'model_identity_changed', 'length'),
    ('cap', 'output_limit_violated', 'length'),
    ('association', 'usage_unassociated', 'length'),
    ('json', 'invalid_json', None),
])
def test_unknown_or_unassociated_usage_never_becomes_verified_receipt(ledger, tmp_path,
                                                                     failure, reason, finish):
    if failure == 'association':
        previous = ledger.reserve()
        ledger.settle(previous, prompt_tokens=1, completion_tokens=1,
                      provider_request_id=PROVIDER_ID)
    calls = []
    def handler(_):
        calls.append(1)
        value = body()
        if failure == 'missing': del value['usage']
        if failure == 'cache': value['usage']['prompt_cache_miss_tokens'] = 41
        if failure == 'identity': value['model'] = SECRET
        if failure == 'cap': value['usage'].update(completion_tokens=513, total_tokens=613)
        if failure == 'json': return httpx.Response(200, content=b'{"duplicate":0,"duplicate":1}')
        return httpx.Response(200, json=value)
    current = adapter(ledger, tmp_path, handler)
    with pytest.raises(ModelUnavailable, match='^' + reason + '$'):
        invoke(current, tmp_path)
    _, stored = receipt(tmp_path)
    assert stored == {'schema': 1, 'request_id': ledger.receipt_snapshot()[-1]['request_id'],
        'failure_category': reason, 'finish_reason': finish, 'usage_verified': False,
        'usage': None, 'accounted_cny': None}
    assert ledger.receipt_snapshot()[-1]['state'] == 'unresolved'
    with pytest.raises(ModelUnavailable, match='^usage_unresolved$'):
        invoke(current, tmp_path)
    assert calls == [1] and len(list((tmp_path / 'protocol').glob('failure-receipt-*.json'))) == 1
    assert SECRET not in json.dumps(stored) and PROVIDER_ID not in json.dumps(stored)


@pytest.mark.parametrize('failure,reason,finish', [
    ('role', 'candidate_invalid', 'stop'),
    ('tool', 'tool_arguments_invalid', 'tool_calls'),
    ('json', 'invalid_json', 'stop'),
    ('finish', 'candidate_invalid', 'unrecognized'),
])
def test_candidate_parse_failures_keep_settled_receipt_without_candidate(ledger, tmp_path,
                                                                        failure, reason, finish):
    value = body()
    message = value['choices'][0]['message']
    message.pop('tool_calls')
    value['choices'][0]['finish_reason'] = 'stop'
    case = deepcopy(CASE)
    if failure == 'role': message['role'] = 'user'
    if failure == 'tool':
        case['tools'] = [NAVIGATION_TOOL]
        value['choices'][0]['finish_reason'] = 'tool_calls'
        message['tool_calls'] = [{'id': 'synthetic-call', 'type': 'function',
                                 'function': {'name': 'navigate', 'arguments': '{"page":"' + SECRET + '"}'}}]
    if failure == 'json': case['json_output'] = True
    if failure == 'finish': value['choices'][0]['finish_reason'] = SECRET
    with pytest.raises(ModelUnavailable, match='^' + reason + '$'):
        invoke(adapter(ledger, tmp_path, lambda _: httpx.Response(200, json=value)), tmp_path, case)
    _, stored = receipt(tmp_path)
    assert stored['failure_category'] == reason and stored['finish_reason'] == finish
    assert stored['usage_verified'] is True and stored['accounted_cny'] == '0.0041784'
    assert ledger.summary()['unresolved_requests'] == 0
    assert SECRET not in json.dumps(stored) and PROVIDER_ID not in json.dumps(stored)


def test_strict_candidate_parser_uses_same_receipt_without_changing_its_output_limit(ledger, tmp_path):
    def handler(request):
        assert json.loads(request.content)['max_tokens'] == 2000
        return httpx.Response(200, json=body())
    with pytest.raises(ModelUnavailable, match='^output_truncated$'):
        invoke(adapter(ledger, tmp_path, handler), tmp_path, strict=True)
    assert receipt(tmp_path)[1]['usage_verified'] is True


@pytest.mark.parametrize('unknown', [False, True])
def test_q17_failure_still_stops_after_settlement_and_receipt_does_not_reopen(unknown, tmp_path):
    ledger = stopped_ledger(tmp_path)
    original = ledger.receipt_snapshot()
    legacy = {p: p.read_bytes() for p in (ledger.marker, ledger.lock_path,
                                        ledger.q16_path, ledger.q16_stop_path)}
    ledger.create_q17_grant(SCOPES)
    calls = []
    def handler(_):
        calls.append(1)
        value = body()
        if unknown: del value['usage']
        return httpx.Response(200, json=value)
    current = adapter(ledger, tmp_path, handler, batch='q17')
    reason = 'usage_missing' if unknown else 'output_truncated'
    with pytest.raises(ModelUnavailable, match='^' + reason + '$'):
        invoke(current, tmp_path, CASES[0])
    path, stored = receipt(tmp_path)
    original_receipt = path.read_bytes()
    row = ledger.receipt_snapshot()[-1]
    grant = json.loads(ledger.q17_path.read_text(encoding='utf-8'))
    assert stored['request_id'] == grant['intents'][0]['request_id'] == row['request_id']
    assert stored['usage_verified'] is (not unknown)
    assert json.loads(ledger.q17_stop_path.read_text())['reason'] == reason
    if unknown:
        ledger.settle(Reservation(row['request_id'], Decimal(row['reserved_cny'])),
            prompt_tokens=100, completion_tokens=512, cache_hit_tokens=60, cache_miss_tokens=40,
            provider_request_id='synthetic-found-usage')
    for scope in SCOPES:
        with pytest.raises(BudgetUnavailable, match='^q17_batch_stopped$'):
            ledger.check_q17_scope(scope)
    with pytest.raises(ModelUnavailable, match='^q17_batch_stopped$'):
        invoke(current, tmp_path, CASES[1])
    assert calls == [1] and path.read_bytes() == original_receipt
    assert ledger.receipt_snapshot()[:23] == original
    assert all(p.read_bytes() == content for p, content in legacy.items())


@pytest.mark.parametrize('entry', ['strict', 'ordinary', 'q16'])
def test_other_entry_cannot_use_q17_or_create_failure_receipt(tmp_path, entry):
    ledger = stopped_ledger(tmp_path)
    ledger.create_q17_grant(SCOPES)
    before = {p: p.read_bytes() for p in (ledger.path, ledger.marker, ledger.lock_path,
              ledger.q16_path, ledger.q16_stop_path, ledger.q17_path)}
    current = adapter(ledger, tmp_path, lambda _: pytest.fail('must not send'),
                      batch='q16' if entry == 'q16' else None)
    with pytest.raises(ModelUnavailable, match='^q17_scope_required$'):
        invoke(current, tmp_path, CASES[0] if entry == 'q16' else CASE, strict=entry == 'strict')
    assert all(p.read_bytes() == content for p, content in before.items())
    assert not (tmp_path / 'protocol').exists() and not ledger.q17_stop_path.exists()


def test_existing_receipt_cannot_be_overwritten(ledger, tmp_path):
    previous = b'previous-independent-evidence\n'
    saved = []
    def handler(_):
        row = ledger.receipt_snapshot()[-1]
        path = tmp_path / 'protocol' / ('failure-receipt-' + row['request_id'] + '.json')
        path.parent.mkdir()
        path.write_bytes(previous)
        saved.append(path)
        return httpx.Response(200, json=body())
    with pytest.raises(ModelUnavailable, match='^output_truncated$'):
        invoke(adapter(ledger, tmp_path, handler), tmp_path)
    assert saved[0].read_bytes() == previous and ledger.summary()['unresolved_requests'] == 0


def test_receipt_io_failure_preserves_original_failure_and_q17_stop(tmp_path, monkeypatch):
    ledger = stopped_ledger(tmp_path)
    ledger.create_q17_grant(SCOPES)
    original_open = Path.open
    def blocked(path, *args, **kwargs):
        if path.name.startswith('failure-receipt-'):
            raise PermissionError('synthetic-receipt-write-denied')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', blocked)
    with pytest.raises(ModelUnavailable, match='^output_truncated$'):
        invoke(adapter(ledger, tmp_path, lambda _: httpx.Response(200, json=body()), batch='q17'),
               tmp_path, CASES[0])
    assert ledger.summary()['unresolved_requests'] == 0
    with pytest.raises(BudgetUnavailable, match='^q17_batch_stopped$'):
        ledger.check_q17_scope(SCOPES[1])
    assert json.loads(ledger.q17_stop_path.read_text())['reason'] == 'output_truncated'
    assert not list((tmp_path / 'protocol').glob('failure-receipt-*.json'))


def test_new_receipt_requires_explicit_current_round_directory(ledger, tmp_path):
    current = adapter(ledger, tmp_path, lambda _: httpx.Response(200, json=body()), explicit_dir=False)
    # 严格入口既有失败不保存旧协议；新功能也不能默认写历史目录。
    with pytest.raises(ModelUnavailable, match='^output_truncated$'):
        invoke(current, tmp_path, strict=True)
    assert current.failure_receipt_dir is None
    assert not (tmp_path / 'protocol').exists()



def test_protocol_shape_counts_unknown_keys_without_persisting_provider_key_names(ledger, tmp_path):
    value = body()
    call = value['choices'][0]['message']['tool_calls'][0]
    call[SECRET + '-unknown-call-key'] = SECRET
    call['function'][SECRET + '-unknown-function-key'] = SECRET
    with pytest.raises(ModelUnavailable, match='^output_truncated$'):
        invoke(adapter(ledger, tmp_path, lambda _: httpx.Response(200, json=value)), tmp_path)
    path, stored = receipt(tmp_path)
    protocol_path = next(path.parent.glob('protocol-*.json'))
    shape = json.loads(protocol_path.read_text(encoding='utf-8'))
    assert set(shape) == {'reason', 'choice_count', 'tool_count', 'tool_shapes'}
    assert shape['tool_shapes'] == [{
        'fields': ['function', 'id', 'type'], 'unknown_field_count': 1,
        'id_length': len(SECRET), 'id_regex_valid': True,
        'function_fields': ['arguments', 'name'], 'function_unknown_field_count': 1,
        'name_is_navigate': True, 'arguments_type': 'str'}]
    evidence = protocol_path.read_text(encoding='utf-8') + path.read_text(encoding='utf-8')
    assert SECRET not in evidence and PROVIDER_ID not in evidence
    assert stored['failure_category'] == 'output_truncated' and stored['usage_verified'] is True


@pytest.mark.parametrize('stage', ['mkdir', 'write'])
def test_protocol_io_failure_keeps_original_error_settlement_receipt_and_q17_stop(tmp_path,
                                                                                monkeypatch, stage):
    ledger = stopped_ledger(tmp_path)
    ledger.create_q17_grant(SCOPES)
    blocked = []
    if stage == 'mkdir':
        original_mkdir = Path.mkdir
        def deny_first_protocol_mkdir(path, *args, **kwargs):
            if path == tmp_path / 'protocol' and not blocked:
                blocked.append(1)
                raise PermissionError('synthetic-private-protocol-directory-error')
            return original_mkdir(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'mkdir', deny_first_protocol_mkdir)
    else:
        original_write = Path.write_text
        def deny_protocol_write(path, *args, **kwargs):
            if path.name.startswith('protocol-'):
                blocked.append(1)
                raise PermissionError('synthetic-private-protocol-write-error')
            return original_write(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'write_text', deny_protocol_write)
    with pytest.raises(ModelUnavailable, match='^output_truncated$'):
        invoke(adapter(ledger, tmp_path, lambda _: httpx.Response(200, json=body()), batch='q17'),
               tmp_path, CASES[0])
    assert blocked == [1] and ledger.summary()['unresolved_requests'] == 0
    assert ledger.receipt_snapshot()[-1]['accounted_cny'] == '0.0041784'
    with pytest.raises(BudgetUnavailable, match='^q17_batch_stopped$'):
        ledger.check_q17_scope(SCOPES[1])
    assert json.loads(ledger.q17_stop_path.read_text())['reason'] == 'output_truncated'
    assert not list((tmp_path / 'protocol').glob('protocol-*.json'))
    _, stored = receipt(tmp_path)
    assert stored['failure_category'] == 'output_truncated' and stored['usage_verified'] is True
    assert SECRET not in json.dumps(stored) and PROVIDER_ID not in json.dumps(stored)
