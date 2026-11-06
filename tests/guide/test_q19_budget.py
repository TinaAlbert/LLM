"""Q19 新风险；只使用临时26笔账本和 MockTransport，不读取真实许可。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
import json
import time

import httpx
import pytest

from services.guide.budget import BudgetUnavailable
from services.guide.experiment import approval_values
from services.guide.knowledge import fingerprint
from services.guide.model import DeepSeekModelAdapter, ModelConfig, ModelUnavailable, _today
from services.guide.task_budget import (TaskPermitBinding, validated_q19_permit,
    create_q19_grant, confirm_q19_round, finish_q19_task, check_q19_permit)
from test_q18_budget import stopped_ledger, SCOPES, settled
from test_q16_budget import case, response


@pytest.fixture
def ready(tmp_path):
    ledger = stopped_ledger(tmp_path)
    ledger.create_q18_grant(SCOPES)
    from services.guide.budget import _q18_permit
    for scope in SCOPES:
        settled(ledger, ledger.reserve(experiment=True,
            q18_permit=_q18_permit(scope, ledger.q18_path)))
    ledger.stop_q18_batch('completed')
    now = [ledger.clock() + 61]
    ledger.clock = lambda: now[0]
    specs = [{'task_ref': 'synthetic-' + str(i + 1), 'case_id': c, 'group': g,
              'max_rounds': rounds, 'scope_hash': str(i + 1) * 64}
             for i, (c, g, rounds) in enumerate((('DEV27-05', 'B1', 9),
                 ('DEV27-05', 'B0', 9), ('DEV27-08', 'B0', 1), ('DEV27-08', 'B1', 1)))]
    old = {path: path.read_bytes() for path in (ledger.marker, ledger.lock_path,
        ledger.q16_path, ledger.q16_stop_path, ledger.q17_path, ledger.q17_stop_path,
        ledger.q18_path, ledger.q18_stop_path)}
    prefix = ledger.receipt_snapshot()
    grant = create_q19_grant(ledger, specs, 'd' * 64)
    return ledger, now, specs, old, prefix, grant


def permit(ready, task=1, round_index=1, physical_index=1, actual_hash='a' * 64):
    ledger, _, specs, *_ = ready
    binding = TaskPermitBinding(specs[task - 1]['task_ref'], task, round_index,
        physical_index, 'b' * 64, actual_hash, specs[task - 1]['scope_hash'])
    return validated_q19_permit(binding, ledger.q19_path)


def test_ordered_confirmed_rounds_preserve_original_ledger_and_all_old_sidecars(ready):
    ledger, now, specs, old, prefix, grant = ready
    assert grant['request_ceiling'] == 20 and grant['reserved_ceiling_cny'] == '0.640'
    before = ledger.path.read_bytes()
    assert check_q19_permit(ledger, permit(ready)) is True
    assert ledger.path.read_bytes() == before
    for task in range(1, 5):
        now[0] += 61
        reservation = ledger.reserve(experiment=True, q19_permit=permit(ready, task))
        settled(ledger, reservation)
        confirm_q19_round(ledger, specs[task - 1]['task_ref'], 1)
        finish_q19_task(ledger, specs[task - 1]['task_ref'])
    assert ledger.receipt_snapshot()[:26] == prefix
    assert old == {path: path.read_bytes() for path in old}
    assert json.loads(ledger.q19_stop_path.read_text())['reason'] == 'completed'


@pytest.mark.parametrize('change', ['task_order', 'scope', 'replay', 'delivery'])
def test_scope_order_replay_and_unconfirmed_delivery_stop_without_extra_reservation(ready, change):
    ledger = ready[0]
    original = permit(ready)
    if change in ('replay', 'delivery'):
        settled(ledger, ledger.reserve(experiment=True, q19_permit=original))
    changed = (permit(ready, task=2) if change == 'task_order' else
        validated_q19_permit(replace(original.binding, scope_hash='e' * 64), ledger.q19_path)
        if change == 'scope' else permit(ready, round_index=2)
        if change == 'delivery' else original)
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable):
        ledger.reserve(experiment=True, q19_permit=changed)
    assert ledger.path.read_bytes() == before and ledger.q19_stop_path.is_file()
    with pytest.raises(BudgetUnavailable, match='q19_batch_stopped'):
        ledger.reserve(experiment=True, q19_permit=original)


def test_ordinary_entry_cannot_take_q19_requests(ready):
    ledger = ready[0]
    before = ledger.path.read_bytes()
    for experiment in (False, True):
        with pytest.raises(BudgetUnavailable, match='q19_scope_required'):
            ledger.reserve(experiment=experiment)
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize('deadline', ['task', 'batch'])
def test_persisted_deadlines_cannot_be_reset_by_recreating_permit(ready, deadline):
    ledger, now, specs, *_ = ready
    settled(ledger, ledger.reserve(experiment=True, q19_permit=permit(ready)))
    confirm_q19_round(ledger, specs[0]['task_ref'], 1)
    if deadline == 'batch':
        finish_q19_task(ledger, specs[0]['task_ref'])
    now[0] += 300 if deadline == 'task' else 1200
    next_permit = permit(ready, round_index=2) if deadline == 'task' else permit(ready, task=2)
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q19_' + deadline + '_deadline'):
        ledger.reserve(experiment=True, q19_permit=next_permit)
    assert ledger.path.read_bytes() == before and ledger.q19_stop_path.exists()


def test_physical_request_limit_includes_same_round_followups(ready):
    ledger, now, *_ = ready
    for physical in range(1, 21):
        now[0] += 11
        item = ledger.reserve(experiment=True,
            q19_permit=permit(ready, physical_index=physical, actual_hash=f'{physical:064x}'))
        settled(ledger, item)
    before = ledger.path.read_bytes()
    # The 21st physical request cannot be minted, even within one logical chat.
    binding = replace(permit(ready).binding, physical_index=20)
    with pytest.raises(BudgetUnavailable, match='q19_request_limit'):
        ledger.reserve(experiment=True,
            q19_permit=validated_q19_permit(binding, ledger.q19_path))
    assert len(ledger.receipt_snapshot()) == 46 and ledger.path.read_bytes() == before


def test_write_ahead_interruption_cannot_be_repaired_or_sent_again(ready, monkeypatch):
    ledger = ready[0]
    write = ledger._write
    def interrupted(path, value):
        if path == ledger.path:
            raise OSError('synthetic interruption after intent')
        return write(path, value)
    monkeypatch.setattr(ledger, '_write', interrupted)
    with pytest.raises(BudgetUnavailable, match='ledger_io_or_lock'):
        ledger.reserve(experiment=True, q19_permit=permit(ready))
    monkeypatch.setattr(ledger, '_write', write)
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q19_intent_unresolved'):
        ledger.reserve(experiment=True, q19_permit=permit(ready))
    assert ledger.path.read_bytes() == before and ledger.q19_stop_path.exists()


@pytest.mark.parametrize('failure', ['unknown_usage', 'transport', 'truncated'])
def test_adapter_failures_stop_batch_and_keep_unknown_usage_reserved(ready, tmp_path, failure):
    ledger = ready[0]
    value = case('合成动态任务下一步')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    calls = []
    def handler(request):
        calls.append(request)
        if failure == 'transport':
            raise httpx.ConnectError('synthetic', request=request)
        body = response()
        if failure == 'unknown_usage':
            body.pop('usage')
        else:
            body['choices'][0]['finish_reason'] = 'length'
        return httpx.Response(200, json=body)
    adapter = DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(),
            'user_reported', 'test', Decimal(0)), input_validator=lambda x: x,
        transport=httpx.MockTransport(handler), q19_grant_path=ledger.q19_path,
        q19_authorizer=lambda x: permit(ready, actual_hash=fingerprint(x)),
        protocol_report_dir=tmp_path / 'd3-29' / 'protocol')
    with pytest.raises(ModelUnavailable):
        asyncio.run(adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval))
    assert len(calls) == 1 and ledger.q19_stop_path.exists()
    assert ledger.summary()['unresolved_requests'] == (0 if failure == 'truncated' else 1)
    with pytest.raises(ModelUnavailable, match='q19_batch_stopped'):
        asyncio.run(adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval))
    assert len(calls) == 1
