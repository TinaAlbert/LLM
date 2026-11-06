"""无费用协议/预算反例；MockTransport不是AI/真实隔离验收。"""
import asyncio
from decimal import Decimal
import json
import multiprocessing
import os
import time

import httpx
import pytest

from services.guide.budget import BudgetLedger, BudgetUnavailable, Reservation, usage_cost
from services.guide.model import (ENDPOINT, MODEL, MODEL_REVISION, PROMPT_SHA256, InputTokenCounter,
                                  ModelConfig, ModelUnavailable, DeepSeekModelAdapter, _today)


def candidate():
    return {'status': 'unknown', 'step_id': None, 'anchor_id': None,
            'explanation': '当前资料不足。', 'preconditions': [], 'expected_change': '',
            'references': ['personal-introduction'], 'unknowns': ['当前状态未知'], 'amount_ref': None}


def response_body(**changes):
    result = {'id': 'chatcmpl-test-synthetic', 'model': MODEL, 'object': 'chat.completion',
              'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150,
                        'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100},
              'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                  'role': 'assistant', 'content': json.dumps(candidate(), ensure_ascii=False)}}]}
    result.update(changes)
    return result


@pytest.fixture
def ledger(tmp_path):
    current = BudgetLedger(tmp_path / 'guide-usage.json')
    current.initialize()
    return current


def validator(value):
    if type(value) is not dict or set(value) != {'goal'} or value['goal'] != '合成目标':
        raise ValueError('not the permitted synthetic input')
    return value


def adapter(ledger, handler, *, counter=True, config=True):
    counted = InputTokenCounter(MODEL, PROMPT_SHA256, 'protocol-test-only', lambda messages: 700) if counter else None
    configured = ModelConfig('synthetic-test-key-not-an-account', 'synthetic-workspace', _today(),
                             'protocol-test-only', 'protocol-test-only') if config else None
    return DeepSeekModelAdapter(ledger, input_validator=validator, token_counter=counted,
                            transport=httpx.MockTransport(handler), config=configured,
                            config_path=ledger.path.parent / 'absent-model.json')


def generate(current, value=None):
    return asyncio.run(current.generate({'goal': '合成目标'} if value is None else value,
                                        time.monotonic() + 5))


def settled(ledger, *, extra=Decimal(0)):
    reserved = ledger.reserve(additional_fee_cny=extra)
    ledger.settle(reserved, prompt_tokens=100, completion_tokens=50,
                  provider_request_id='synthetic-' + reserved.request_id)
    return reserved


LEDGER_OPERATIONS = ('summary', 'reserve', 'settle', 'mark_unresolved',
                     'finish_first_round', 'receipt_snapshot')


def ledger_artifacts(current):
    return {path.name: path.read_bytes() if path.exists() else None
            for path in (current.path, current.marker, current.lock_path)}


def operate_ledger(current, operation, reservation):
    if operation == 'settle':
        return current.settle(reservation, prompt_tokens=1, completion_tokens=1,
                              provider_request_id='synthetic-preservation-receipt')
    if operation == 'mark_unresolved':
        return current.mark_unresolved(reservation, 'synthetic_failure')
    return getattr(current, operation)()


@pytest.mark.parametrize('operation', LEDGER_OPERATIONS)
def test_uninitialized_operations_preserve_absent_artifacts(tmp_path, operation):
    current = BudgetLedger(tmp_path / 'guide-usage.json')
    before = ledger_artifacts(current)
    reservation = Reservation('a' * 32, Decimal('0.032'))
    with pytest.raises(BudgetUnavailable, match='ledger_uninitialized'):
        operate_ledger(current, operation, reservation)
    assert ledger_artifacts(current) == before


@pytest.mark.parametrize('operation', LEDGER_OPERATIONS)
@pytest.mark.parametrize('damage,reason', [('missing', 'ledger_missing'), ('empty', 'ledger_corrupt')])
def test_damaged_lock_operations_preserve_all_artifacts(ledger, operation, damage, reason):
    reservation = ledger.reserve()
    if damage == 'missing':
        ledger.lock_path.unlink()
    else:
        ledger.lock_path.write_bytes(b'')
    before = ledger_artifacts(ledger)
    with pytest.raises(BudgetUnavailable, match=reason):
        operate_ledger(ledger, operation, reservation)
    assert ledger_artifacts(ledger) == before


@pytest.mark.parametrize('remnant', ['empty_lock', 'lock_byte', 'ledger_only', 'marker_only'])
def test_initialization_preserves_any_existing_remnant(tmp_path, remnant):
    current = BudgetLedger(tmp_path / 'guide-usage.json')
    target, content = {'empty_lock': (current.lock_path, b''),
                       'lock_byte': (current.lock_path, b'0'),
                       'ledger_only': (current.path, b'unknown-history'),
                       'marker_only': (current.marker, b'unknown-history')}[remnant]
    target.write_bytes(content)
    before = ledger_artifacts(current)
    with pytest.raises(BudgetUnavailable, match='initialization_already_recorded'):
        current.initialize()
    assert ledger_artifacts(current) == before


def test_explicit_initialization_and_lost_ledger_never_reset(tmp_path):
    current = BudgetLedger(tmp_path / 'guide-usage.json')
    with pytest.raises(BudgetUnavailable, match='ledger_uninitialized'):
        current.reserve()
    current.initialize()
    assert current.summary()['physical_requests'] == 0
    current.path.unlink()
    with pytest.raises(BudgetUnavailable, match='ledger_missing'):
        BudgetLedger(current.path).reserve()
    with pytest.raises(BudgetUnavailable, match='initialization_already_recorded'):
        current.initialize()


def test_initialization_marker_loss_and_corruption_fail_closed(ledger):
    ledger.marker.unlink()
    with pytest.raises(BudgetUnavailable, match='ledger_missing'):
        ledger.reserve()
    ledger.marker.write_text('{}', encoding='utf-8')
    with pytest.raises(BudgetUnavailable, match='ledger_corrupt'):
        ledger.reserve()


@pytest.mark.parametrize('corrupt', ['duplicate', 'nonfinite'])
def test_malformed_ledger_cannot_replace_request_history_with_empty_quota(ledger, corrupt):
    ledger.reserve()
    original = ledger.path.read_text(encoding='utf-8')
    if corrupt == 'duplicate':
        malformed = original[:-1] + ',"requests":[]}'
    else:
        malformed = original.replace('"accounted_cny":null', '"accounted_cny":NaN')
        # 初始化JSON使用紧凑格式，未决金额原为null；若格式改变不默许空测试。
        assert malformed != original
    ledger.path.write_text(malformed, encoding='utf-8')
    with pytest.raises(BudgetUnavailable, match='ledger_corrupt'):
        BudgetLedger(ledger.path).reserve()
    assert ledger.path.read_text(encoding='utf-8') == malformed


def test_duplicate_keys_in_initialization_anchor_fail_closed(ledger):
    original = ledger.lock_path.read_bytes()
    malformed = original[:-1] + b',"package":"DS-1007"}'
    ledger.lock_path.write_bytes(malformed)
    with pytest.raises(BudgetUnavailable, match='ledger_corrupt'):
        ledger.reserve()


@pytest.mark.parametrize('receipt', ['control', 'duplicate'])
def test_settled_receipt_corruption_is_rejected_when_ledger_is_reopened(ledger, receipt):
    first = ledger.reserve()
    ledger.settle(first, prompt_tokens=1, completion_tokens=1, provider_request_id='receipt-first')
    second = ledger.reserve()
    ledger.settle(second, prompt_tokens=1, completion_tokens=1, provider_request_id='receipt-second')
    data = json.loads(ledger.path.read_text(encoding='utf8'))
    data['requests'][1]['provider_request_id'] = 'receipt-first' if receipt == 'duplicate' else 'receipt\nsecond'
    ledger.path.write_text(json.dumps(data), encoding='utf8')
    with pytest.raises(BudgetUnavailable, match='ledger_corrupt'):
        BudgetLedger(ledger.path).reserve()


def test_atomic_reservation_and_precise_actual_cost(ledger):
    reserved = ledger.reserve()
    assert reserved.amount_cny == Decimal('0.032')
    assert ledger.summary()['unresolved_cny'] == '0.032'
    actual = ledger.settle(reserved, prompt_tokens=100, completion_tokens=50,
                          provider_request_id='synthetic-request')
    assert actual == Decimal('0.0006')
    assert ledger.summary() == {'package': 'DS-1007', 'phase': 'first', 'physical_requests': 1,
                                'accounted_cny': '0.0006', 'unresolved_cny': '0', 'unresolved_requests': 0}


@pytest.mark.parametrize('hit,miss', [(None, None), ('1', None), (True, None),
                                    (1.0, None), (0, '100'), (0, False), (-1, None), (101, None)])
def test_invalid_cache_counts_fail_closed_before_arithmetic(hit, miss):
    with pytest.raises(BudgetUnavailable, match='usage_out_of_bounds'):
        usage_cost(100, 50, hit, miss)


def test_cached_usage_retains_hit_miss_and_uses_reviewed_high_peak_prices(ledger):
    reserved = ledger.reserve()
    accounted = ledger.settle(reserved, prompt_tokens=100, completion_tokens=50,
                              cache_hit_tokens=60, cache_miss_tokens=40,
                              provider_request_id='synthetic-cache-request')
    assert accounted == Decimal('0.0004824')
    stored = json.loads(ledger.path.read_text(encoding='utf-8'))['requests'][0]
    assert stored['cache_hit_tokens'] == 60 and stored['cache_miss_tokens'] == 40
    assert BudgetLedger(ledger.path).summary()['accounted_cny'] == '0.0004824'


def test_crash_equivalent_pending_survives_new_instance(ledger):
    ledger.reserve()
    recovered = BudgetLedger(ledger.path)
    with pytest.raises(BudgetUnavailable, match='usage_unresolved'):
        recovered.reserve()
    assert recovered.summary()['unresolved_cny'] == '0.032'


def test_unresolved_only_trusted_associated_usage_can_settle(ledger):
    reserved = ledger.reserve()
    ledger.mark_unresolved(reserved, 'model_timeout')
    with pytest.raises(BudgetUnavailable, match='usage_out_of_bounds'):
        ledger.settle(reserved, prompt_tokens=8001, completion_tokens=50, provider_request_id='synthetic')
    with pytest.raises(BudgetUnavailable, match='usage_unassociated'):
        ledger.settle(reserved, prompt_tokens=100, completion_tokens=50, provider_request_id='')
    assert ledger.summary()['unresolved_requests'] == 1
    ledger.settle(reserved, prompt_tokens=100, completion_tokens=50, provider_request_id='trusted-synthetic')
    assert ledger.summary()['unresolved_requests'] == 0


def test_durable_six_per_minute_limit(tmp_path):
    now = [10000.0]
    current = BudgetLedger(tmp_path / 'usage.json', clock=lambda: now[0])
    current.initialize()
    for _ in range(6):
        settled(current)
    with pytest.raises(BudgetUnavailable, match='model_rate_stop'):
        BudgetLedger(current.path, clock=lambda: now[0]).reserve()
    now[0] += 61
    settled(current)
    assert current.summary()['physical_requests'] == 7


def test_first_50_and_cumulative_100_are_hard_caps(tmp_path):
    now = [10000.0]
    current = BudgetLedger(tmp_path / 'usage.json', clock=lambda: now[0])
    current.initialize()
    for _ in range(50):
        settled(current)
        now[0] += 61
    with pytest.raises(BudgetUnavailable, match='first_round_budget_stop'):
        current.reserve()
    current.finish_first_round()
    for _ in range(50):
        settled(current)
        now[0] += 61
    with pytest.raises(BudgetUnavailable, match='cumulative_budget_stop'):
        current.reserve()
    assert current.summary()['physical_requests'] == 100


def test_amount_caps_include_verified_additional_fees(tmp_path):
    now = [10000.0]
    current = BudgetLedger(tmp_path / 'usage.json', clock=lambda: now[0])
    current.initialize()
    for _ in range(6):
        settled(current, extra=Decimal('0.3'))
        now[0] += 61
    with pytest.raises(BudgetUnavailable, match='first_round_budget_stop'):
        current.reserve(additional_fee_cny=Decimal('0.3'))
    current.finish_first_round()
    for _ in range(10):
        settled(current, extra=Decimal('0.3'))
        now[0] += 61
    with pytest.raises(BudgetUnavailable, match='cumulative_budget_stop'):
        current.reserve(additional_fee_cny=Decimal('0.3'))


def _reserve_process(path, queue):
    try:
        BudgetLedger(path).reserve()
        queue.put('reserved')
    except BudgetUnavailable as error:
        queue.put(error.reason)


def _crash_process(path):
    BudgetLedger(path).reserve()
    os._exit(23)


def test_actual_multiprocess_lock_and_crash_retention(ledger):
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    with ledger._locked():
        child = context.Process(target=_reserve_process, args=(str(ledger.path), queue))
        child.start()
        child.join(15)
        assert child.exitcode == 0
        assert queue.get(timeout=2) == 'ledger_io_or_lock'
    crash = context.Process(target=_crash_process, args=(str(ledger.path),))
    crash.start()
    crash.join(15)
    assert crash.exitcode == 23
    assert BudgetLedger(ledger.path).summary()['unresolved_cny'] == '0.032'
    with pytest.raises(BudgetUnavailable, match='usage_unresolved'):
        ledger.reserve()
    queue.close()


def _initialize_and_reserve_process(path, barrier, queue):
    barrier.wait(timeout=10)
    current = BudgetLedger(path)
    try:
        current.initialize()
        current.reserve()
        queue.put('initialized_and_reserved')
    except BudgetUnavailable as error:
        queue.put(error.reason)


def test_actual_competing_initializers_cannot_erase_reservation(tmp_path):
    context = multiprocessing.get_context('spawn')
    barrier, queue = context.Barrier(2), context.Queue()
    path = tmp_path / 'guide-usage.json'
    children = [context.Process(target=_initialize_and_reserve_process,
                                args=(str(path), barrier, queue)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        assert sorted(queue.get(timeout=2) for _ in children) == [
            'initialization_already_recorded', 'initialized_and_reserved']
        summary = BudgetLedger(path).summary()
        assert summary['physical_requests'] == 1
        assert summary['unresolved_requests'] == 1 and summary['unresolved_cny'] == '0.032'
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        queue.close()


def test_clock_rollback_cannot_bypass_frequency(tmp_path):
    now = [10000.0]
    current = BudgetLedger(tmp_path / 'usage.json', clock=lambda: now[0])
    current.initialize()
    settled(current)
    now[0] -= 1
    with pytest.raises(BudgetUnavailable, match='clock_or_ledger_invalid'):
        current.reserve()


def test_adapter_fixed_payload_destination_and_complete_usage(ledger):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_body())
    result = generate(adapter(ledger, handler))
    assert len(requests) == 1
    assert str(requests[0].url) == ENDPOINT
    body = json.loads(requests[0].content)
    assert set(body) == {'model', 'messages', 'thinking', 'stream', 'max_tokens', 'response_format'}
    assert body['model'] == MODEL
    assert body['thinking'] == {'type': 'disabled'} and body['stream'] is False
    assert body['max_tokens'] == 2000
    assert body['response_format'] == {'type': 'json_object'}
    assert 'JSON' in body['messages'][0]['content']
    assert result.candidate == candidate()
    assert result.usage.total_tokens == 150
    assert result.amount_cny == Decimal('0.0006')
    assert ledger.summary()['unresolved_requests'] == 0


@pytest.mark.parametrize('value', [{'goal': '合成目标', 'role': 'admin'}, {'goal': '真实凭证'}])
def test_input_rejection_is_zero_external_requests_and_zero_reservations(ledger, value):
    seen = []
    current = adapter(ledger, lambda request: seen.append(request))
    with pytest.raises(ModelUnavailable, match='input_rejected'):
        generate(current, value)
    assert not seen and ledger.summary()['physical_requests'] == 0


def test_unverified_full_template_token_bound_is_zero_outbound(ledger):
    seen = []
    current = adapter(ledger, lambda request: seen.append(request), counter=False)
    with pytest.raises(ModelUnavailable, match='token_bound_unverified'):
        generate(current)
    assert not seen and ledger.summary()['physical_requests'] == 0


def test_missing_credentials_zero_outbound(ledger):
    seen = []
    with pytest.raises(ModelUnavailable, match='credentials_missing'):
        generate(adapter(ledger, lambda request: seen.append(request), config=False))
    assert not seen and ledger.summary()['physical_requests'] == 0


@pytest.mark.parametrize('count', [True, 0, 8001, 8000.0])
def test_counter_invalid_or_excess_rejects_before_budget(ledger, count):
    current = adapter(ledger, lambda request: pytest.fail('must not send'))
    current.token_counter = InputTokenCounter(MODEL, PROMPT_SHA256, 'protocol-test', lambda messages: count)
    with pytest.raises(ModelUnavailable, match='input_token_limit'):
        generate(current)
    assert ledger.summary()['physical_requests'] == 0


@pytest.mark.parametrize('failure', ['429', 'redirect', 'timeout', 'network', 'oversized',
                                     'usage_missing', 'usage_bounds', 'model_changed', 'json'])
def test_uncertain_failures_preserve_maximum_and_block_restart(ledger, failure):
    seen = []
    def handler(request):
        seen.append(request)
        if failure == '429':
            return httpx.Response(429, json={'secret-error': 'not persisted'})
        if failure == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://unapproved.example'})
        if failure == 'timeout':
            raise httpx.ReadTimeout('synthetic-secret-detail')
        if failure == 'network':
            raise httpx.ConnectError('synthetic-secret-detail')
        if failure == 'oversized':
            return httpx.Response(200, content=b' ' * 65537)
        if failure == 'json':
            return httpx.Response(200, content=b'{"model":"bad", "model":"duplicate"}')
        result = response_body()
        if failure == 'usage_missing':
            result.pop('usage')
        if failure == 'usage_bounds':
            result['usage'] = {'prompt_tokens': 8001, 'completion_tokens': 0, 'total_tokens': 8001}
        if failure == 'model_changed':
            result['model'] = 'deepseek-v4-pro'
        return httpx.Response(200, json=result)
    current = adapter(ledger, handler)
    with pytest.raises(ModelUnavailable):
        generate(current)
    assert len(seen) == 1
    saved = ledger.path.read_text(encoding='utf-8')
    assert 'secret' not in saved and 'unapproved' not in saved
    assert ledger.summary()['unresolved_cny'] == '0.032'
    with pytest.raises(ModelUnavailable, match='usage_unresolved'):
        generate(adapter(BudgetLedger(ledger.path), handler))
    assert len(seen) == 1


@pytest.mark.parametrize('failure', ['truncated', 'tool', 'extra_field', 'url', 'empty'])
def test_candidate_failure_can_settle_trusted_usage_without_ai_success(ledger, failure):
    body = response_body()
    message = body['choices'][0]['message']
    if failure == 'truncated':
        body['choices'][0]['finish_reason'] = 'length'
    if failure == 'tool':
        message['tool_calls'] = [{'function': {'name': 'pay'}}]
    if failure == 'empty':
        message['content'] = ''
    if failure in {'extra_field', 'url'}:
        changed = candidate()
        if failure == 'extra_field':
            changed['tool'] = 'pay'
        else:
            changed['explanation'] = 'https://unapproved.example'
        message['content'] = json.dumps(changed)
    with pytest.raises(ModelUnavailable):
        generate(adapter(ledger, lambda request: httpx.Response(200, json=body)))
    assert ledger.summary()['physical_requests'] == 1
    assert ledger.summary()['accounted_cny'] == '0.0006'
    assert ledger.summary()['unresolved_cny'] == '0'


def test_cancellation_and_global_single_model_slot(ledger):
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=response_body())
        first = asyncio.create_task(adapter(ledger, handler).generate({'goal': '合成目标'}, time.monotonic() + 5))
        await entered.wait()
        with pytest.raises(ModelUnavailable, match='model_busy'):
            await adapter(ledger, handler).generate({'goal': '合成目标'}, time.monotonic() + 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    asyncio.run(run())
    assert ledger.summary()['physical_requests'] == 1
    assert ledger.summary()['unresolved_cny'] == '0.032'


def config_source():
    return {'package': 'DS-1007', 'model': MODEL, 'model_revision': MODEL_REVISION,
            'service': 'deepseek-official', 'account_ref': 'synthetic', 'api_key': 'synthetic-test-key-not-an-account',
            'account_review': {'checked_on': _today(), 'existing_account': True,
                'model_access': True, 'balance_available': True,
                'evidence_ref': 'account-review-test'},
            'price_review': {'checked_on': _today(), 'input_per_million': '2',
                'cache_hit_per_million': '0.04', 'output_per_million': '8',
                'additional_fee_upper_cny': '0', 'evidence_ref': 'price-review-test'}}


@pytest.mark.parametrize('change', ['alias', 'region', 'endpoint', 'stale', 'price',
                                  'cache_price', 'cache_price_missing', 'cache_price_unknown',
                                  'unknown_fee', 'new_account'])
def test_configuration_cannot_expand_package_or_unknown_price(tmp_path, change):
    body = config_source()
    if change == 'alias': body['model'] = 'deepseek-v4-flash'
    if change == 'region': body['service'] = 'aliyun'
    if change == 'endpoint': body['endpoint'] = 'https://unapproved.example'
    if change == 'stale': body['price_review']['checked_on'] = '2020-01-01'
    if change == 'price': body['price_review']['output_per_million'] = '3'
    if change == 'cache_price': body['price_review']['cache_hit_per_million'] = '0.05'
    if change == 'cache_price_missing': body['price_review'].pop('cache_hit_per_million')
    if change == 'cache_price_unknown': body['price_review']['cache_hit_per_million'] = 'unknown'
    if change == 'unknown_fee': body['price_review']['additional_fee_upper_cny'] = 'unknown'
    if change == 'new_account': body['account_review']['existing_account'] = False
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(body), encoding='utf-8')
    with pytest.raises(ModelUnavailable):
        ModelConfig.load(path)


def test_reviewed_config_still_requires_code_verified_counter(tmp_path, ledger):
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(config_source()), encoding='utf-8')
    assert 'synthetic-test-key' not in repr(ModelConfig.load(path))
    current = DeepSeekModelAdapter(ledger, config_path=path, input_validator=validator,
                               transport=httpx.MockTransport(lambda request: pytest.fail('no token evidence')))
    with pytest.raises(ModelUnavailable, match='token_bound_unverified'):
        generate(current)
    assert ledger.summary()['physical_requests'] == 0


# Q14沿用原三件账本；独立进程必须看到同一累计次数与未决停止。
def _reserve_experiment_process(path, queue):
    try:
        BudgetLedger(path).reserve(experiment=True)
        queue.put('reserved')
    except BudgetUnavailable as error:
        queue.put(error.reason)


@pytest.mark.parametrize('state,expected', [
    ('available', 'reserved'), ('exhausted', 'experiment_budget_stop'),
    ('pending', 'usage_unresolved'), ('unresolved', 'usage_unresolved')])
def test_q14_same_ledger_limit_and_unknown_usage_hold_across_processes(tmp_path, state, expected):
    now = [time.time() - 1800]
    current = BudgetLedger(tmp_path / 'usage.json', clock=lambda: now[0])
    current.initialize()
    count = 21 if state == 'available' else 22 if state == 'exhausted' else 16
    for _ in range(count):
        reservation = current.reserve(experiment=True)
        current.settle(reservation, prompt_tokens=100, completion_tokens=50,
                       provider_request_id='synthetic-' + reservation.request_id)
        now[0] += 61
    if state in ('pending', 'unresolved'):
        reservation = current.reserve(experiment=True)
        if state == 'unresolved':
            current.mark_unresolved(reservation, 'synthetic_unknown_usage')
    before = ledger_artifacts(current)
    original = current.receipt_snapshot()
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    child = context.Process(target=_reserve_experiment_process, args=(str(current.path), queue))
    try:
        child.start()
        child.join(15)
        assert child.exitcode == 0
        assert queue.get(timeout=2) == expected
        recovered = BudgetLedger(current.path)
        assert recovered.receipt_snapshot()[:len(original)] == original
        after = ledger_artifacts(recovered)
        assert after[current.marker.name] == before[current.marker.name]
        assert after[current.lock_path.name] == before[current.lock_path.name]
        if state == 'available':
            assert recovered.summary()['physical_requests'] == 22
            assert recovered.summary()['unresolved_requests'] == 1
        else:
            assert after == before
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
        queue.close()


def test_q14_half_yuan_stop_does_not_reset_or_wait_for_request_ceiling(tmp_path):
    now = [10000.0]
    current = BudgetLedger(tmp_path / 'usage.json', clock=lambda: now[0])
    current.initialize()
    # 历史账本包括原DS-1007费用；实验必须沿同账本执行0.5元提前停止。
    for _ in range(5):
        settled(current, extra=Decimal('0.1'))
        now[0] += 61
    before = ledger_artifacts(current)
    with pytest.raises(BudgetUnavailable, match='experiment_budget_stop'):
        BudgetLedger(current.path, clock=lambda: now[0]).reserve(experiment=True)
    assert current.summary()['physical_requests'] == 5
    assert ledger_artifacts(current) == before
