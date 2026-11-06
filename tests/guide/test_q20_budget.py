"""Q20 新接入风险；临时27笔账本/MockTransport，不读真实许可。"""
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
    create_q19_grant, confirm_q19_round, finish_q19_task, check_q19_permit, stop_q19_batch)
from test_q19_budget import ready as q19_ready, permit as q19_permit
from test_q18_budget import settled
from test_q16_budget import case, response


@pytest.fixture
def ready(q19_ready):
    ledger, now, specs, old, _, _ = q19_ready
    settled(ledger, ledger.reserve(experiment=True, q19_permit=q19_permit(q19_ready)))
    stop_q19_batch(ledger, 'formal_delivery_failed')
    now[0] += 61
    specs = [dict(spec, task_ref='q20-' + spec['task_ref']) for spec in specs]
    old = {path: path.read_bytes() for path in (*old, ledger.q19_path, ledger.q19_stop_path)}
    prefix = ledger.receipt_snapshot()
    grant = create_q19_grant(ledger, specs, 'e' * 64, batch='Q20')
    return ledger, now, specs, old, prefix, grant


def permit(ready, task=1, round_index=1, physical_index=1, actual_hash='a' * 64):
    ledger, _, specs, *_ = ready
    binding = TaskPermitBinding(specs[task - 1]['task_ref'], task, round_index,
        physical_index, 'b' * 64, actual_hash, specs[task - 1]['scope_hash'])
    return validated_q19_permit(binding, ledger.q20_path, batch='Q20')


def test_new_sidecar_reserves_from_28_and_preserves_27_and_all_closed_sidecars(ready):
    ledger, now, specs, old, prefix, grant = ready
    assert grant['package'] == 'Q20-P30-single-step-task' and grant['prefix_requests'] == 27
    assert len(grant['legacy_sha256']) == 8
    before = ledger.path.read_bytes()
    assert check_q19_permit(ledger, permit(ready), batch='Q20') is True
    assert ledger.path.read_bytes() == before
    for task in range(1, 5):
        now[0] += 61
        reservation = ledger.reserve(experiment=True, q20_permit=permit(ready, task))
        settled(ledger, reservation)
        confirm_q19_round(ledger, specs[task - 1]['task_ref'], 1, batch='Q20')
        finish_q19_task(ledger, specs[task - 1]['task_ref'], batch='Q20')
    assert ledger.receipt_snapshot()[:27] == prefix
    assert old == {path: path.read_bytes() for path in old}
    assert json.loads(ledger.q20_stop_path.read_text())['reason'] == 'completed'


def test_q20_cannot_initialize_before_closed_one_request_q19(q19_ready):
    ledger, _, specs, *_ = q19_ready
    with pytest.raises(BudgetUnavailable, match='q20_prefix_mismatch'):
        create_q19_grant(ledger, specs, 'e' * 64, batch='Q20')
    settled(ledger, ledger.reserve(experiment=True, q19_permit=q19_permit(q19_ready)))
    with pytest.raises(BudgetUnavailable, match='q20_legacy_invalid'):
        create_q19_grant(ledger, specs, 'e' * 64, batch='Q20')
    assert not ledger.q20_path.exists()


@pytest.mark.parametrize('attribute', ['q19_path', 'q19_stop_path'])
def test_q20_rejects_old_sidecar_byte_changes_without_new_request(ready, attribute):
    ledger = ready[0]
    path = getattr(ledger, attribute)
    path.write_bytes(path.read_bytes() + b' ')
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q20_grant_invalid'):
        ledger.reserve(experiment=True, q20_permit=permit(ready))
    assert ledger.path.read_bytes() == before and ledger.q20_stop_path.exists()


def test_q19_remaining_allowance_and_ordinary_entry_cannot_spend_q20(ready):
    ledger = ready[0]
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q20_scope_required'):
        ledger.reserve(experiment=True)
    old = validated_q19_permit(permit(ready).binding, ledger.q19_path)
    with pytest.raises(BudgetUnavailable, match='q19_batch_stopped'):
        ledger.reserve(experiment=True, q19_permit=old)
    assert ledger.path.read_bytes() == before and not ledger.q20_stop_path.exists()


@pytest.mark.parametrize('deadline', ['task', 'batch'])
def test_q20_uses_same_persistent_deadlines_and_its_own_stop(ready, deadline):
    ledger, now, specs, old, *_ = ready
    settled(ledger, ledger.reserve(experiment=True, q20_permit=permit(ready)))
    confirm_q19_round(ledger, specs[0]['task_ref'], 1, batch='Q20')
    if deadline == 'batch':
        finish_q19_task(ledger, specs[0]['task_ref'], batch='Q20')
    now[0] += 300 if deadline == 'task' else 1200
    followup = permit(ready, round_index=2) if deadline == 'task' else permit(ready, task=2)
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q20_' + deadline + '_deadline'):
        ledger.reserve(experiment=True, q20_permit=followup)
    assert ledger.path.read_bytes() == before and ledger.q20_stop_path.exists()
    assert old == {path: path.read_bytes() for path in old}


@pytest.mark.parametrize('failure', ['unknown_usage', 'transport', 'truncated', 'actual_changed'])
def test_q20_adapter_authorization_and_failures_stop_q20_keep_q19_bytes(ready, tmp_path, failure):
    ledger, _, _, old, *_ = ready
    value = case('合成单步任务下一步')
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
        transport=httpx.MockTransport(handler), q20_grant_path=ledger.q20_path,
        q20_authorizer=lambda x: permit(ready, actual_hash='0' * 64 if failure == 'actual_changed' else fingerprint(x)),
        protocol_report_dir=tmp_path / 'd3-30' / 'protocol')
    with pytest.raises(ModelUnavailable):
        asyncio.run(adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval))
    assert len(calls) == (0 if failure == 'actual_changed' else 1)
    assert ledger.q20_stop_path.exists()
    assert ledger.summary()['unresolved_requests'] == (1 if failure in ('transport', 'unknown_usage') else 0)
    assert old == {path: path.read_bytes() for path in old}
    adapter.q20_authorizer = lambda x: permit(ready, actual_hash=fingerprint(x))
    with pytest.raises(ModelUnavailable, match='q20_batch_stopped'):
        asyncio.run(adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval))


def test_q20_write_ahead_interruption_cannot_be_repaired_or_sent_again(ready, monkeypatch):
    ledger = ready[0]
    write = ledger._write
    def interrupted(path, value):
        if path == ledger.path:
            raise OSError('synthetic interruption after intent')
        return write(path, value)
    monkeypatch.setattr(ledger, '_write', interrupted)
    with pytest.raises(BudgetUnavailable, match='ledger_io_or_lock'):
        ledger.reserve(experiment=True, q20_permit=permit(ready))
    monkeypatch.setattr(ledger, '_write', write)
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q20_intent_unresolved'):
        ledger.reserve(experiment=True, q20_permit=permit(ready))
    assert ledger.path.read_bytes() == before and ledger.q20_stop_path.exists()


def test_q20_adapter_success_associates_actual_input_usage_and_new_intent(ready, tmp_path):
    ledger, _, _, old, prefix, _ = ready
    value = case('合成单步任务下一步')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response('synthetic-q20-receipt'))
    adapter = DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(),
            'user_reported', 'test', Decimal(0)), input_validator=lambda x: x,
        transport=httpx.MockTransport(handler), q20_grant_path=ledger.q20_path,
        q20_authorizer=lambda x: permit(ready, actual_hash=fingerprint(x)),
        protocol_report_dir=tmp_path / 'd3-30' / 'protocol')
    result = asyncio.run(adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval))
    rows = ledger.receipt_snapshot()
    intent = json.loads(ledger.q20_path.read_text())['intents'][0]
    assert len(calls) == 1 and len(rows) == 28 and rows[:27] == prefix
    assert rows[27]['state'] == 'settled'
    assert result.reservation_id == intent['request_id'] == rows[27]['request_id']
    assert intent['binding']['actual_hash'] == fingerprint(value)
    assert not ledger.q20_stop_path.exists()
    assert old == {path: path.read_bytes() for path in old}
