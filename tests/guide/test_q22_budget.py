"""Q22 直接接入风险；仅临时29笔账本和MockTransport，不读真实许可。"""
import asyncio
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
from test_q19_budget import ready as q19_ready
from test_q20_budget import ready as q20_ready
from test_q21_budget import ready as q21_ready, permit as q21_permit
from test_q18_budget import settled
from test_q16_budget import case, response


@pytest.fixture
def ready(q21_ready):
    ledger, now, specs, old, *_ = q21_ready
    settled(ledger, ledger.reserve(experiment=True, q21_permit=q21_permit(q21_ready)))
    stop_q19_batch(ledger, 'formal_delivery_failed', batch='Q21')
    now[0] += 61
    specs = [dict(spec, task_ref='q22-' + spec['task_ref']) for spec in specs]
    old = {path: path.read_bytes() for path in (*old, ledger.q21_path, ledger.q21_stop_path)}
    prefix = ledger.receipt_snapshot()
    grant = create_q19_grant(ledger, specs, 'f' * 64, batch='Q22')
    return ledger, now, specs, old, prefix, grant


def permit(ready, task=1, round_index=1, actual_hash='a' * 64):
    ledger, _, specs, *_ = ready
    binding = TaskPermitBinding(specs[task - 1]['task_ref'], task, round_index,
        1, 'b' * 64, actual_hash, specs[task - 1]['scope_hash'])
    return validated_q19_permit(binding, ledger.q22_path, batch='Q22')


def test_q22_starts_at_30_keeps_29_prefix_and_twelve_closed_sidecars(ready):
    ledger, now, specs, old, prefix, grant = ready
    assert grant['package'] == 'Q22-P31-structured-task' and grant['prefix_requests'] == 29
    assert len(grant['legacy_sha256']) == 12
    assert grant['request_ceiling'] == 20 and grant['reserved_ceiling_cny'] == '0.640'
    assert [s['max_rounds'] for s in grant['task_specs']] == [9, 9, 1, 1]
    assert (grant['task_deadline_seconds'], grant['batch_deadline_seconds']) == (300, 1200)
    before = ledger.path.read_bytes()
    assert check_q19_permit(ledger, permit(ready), batch='Q22') is True
    assert ledger.path.read_bytes() == before
    for task in range(1, 5):
        now[0] += 61
        reservation = ledger.reserve(experiment=True, q22_permit=permit(ready, task))
        settled(ledger, reservation)
        confirm_q19_round(ledger, specs[task - 1]['task_ref'], 1, batch='Q22')
        finish_q19_task(ledger, specs[task - 1]['task_ref'], batch='Q22')
    assert ledger.receipt_snapshot()[:29] == prefix
    assert old == {path: path.read_bytes() for path in old}
    assert json.loads(ledger.q22_stop_path.read_text())['reason'] == 'completed'


def test_q22_requires_closed_settled_29_prefix(q21_ready):
    ledger, _, specs, *_ = q21_ready
    with pytest.raises(BudgetUnavailable, match='q22_prefix_mismatch'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q22')
    reservation = ledger.reserve(experiment=True, q21_permit=q21_permit(q21_ready))
    with pytest.raises(BudgetUnavailable, match='usage_unresolved'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q22')
    settled(ledger, reservation)
    with pytest.raises(BudgetUnavailable, match='q22_legacy_invalid'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q22')
    assert not ledger.q22_path.exists()


@pytest.mark.parametrize('attribute', ['q21_path', 'q21_stop_path'])
def test_q22_detects_changed_q21_bytes_before_new_request(ready, attribute):
    ledger = ready[0]
    path = getattr(ledger, attribute)
    path.write_bytes(path.read_bytes() + b' ')
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q22_grant_invalid'):
        ledger.reserve(experiment=True, q22_permit=permit(ready))
    assert ledger.path.read_bytes() == before and ledger.q22_stop_path.exists()


def test_closed_q19_q20_q21_and_ordinary_entries_cannot_take_q22(ready):
    ledger = ready[0]
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q22_scope_required'):
        ledger.reserve(experiment=True)
    for batch in ('Q19', 'Q20', 'Q21'):
        old = validated_q19_permit(permit(ready).binding, getattr(ledger, batch.lower() + '_path'), batch=batch)
        with pytest.raises(BudgetUnavailable, match=batch.lower() + '_batch_stopped'):
            ledger.reserve(experiment=True, **{batch.lower() + '_permit': old})
    assert ledger.path.read_bytes() == before and not ledger.q22_stop_path.exists()


@pytest.mark.parametrize('outcome', ['success', 'unknown_usage', 'actual_changed'])
def test_q22_adapter_routes_actual_input_and_usage_or_failure_only_to_q22(ready, tmp_path, outcome):
    ledger, _, _, old, prefix, _ = ready
    value = case('合成有效性恢复下一步')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    calls = []
    def handler(request):
        calls.append(request)
        body = response('synthetic-q22-receipt')
        if outcome == 'unknown_usage':
            body.pop('usage')
        return httpx.Response(200, json=body)
    adapter = DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(),
            'user_reported', 'test', Decimal(0)), input_validator=lambda x: x,
        transport=httpx.MockTransport(handler), q22_grant_path=ledger.q22_path,
        q22_authorizer=lambda x: permit(ready, actual_hash='0' * 64 if outcome == 'actual_changed' else fingerprint(x)),
        protocol_report_dir=tmp_path / 'd3-31' / 'protocol')
    async def generate():
        return await adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval)
    if outcome == 'success':
        result = asyncio.run(generate())
        rows = ledger.receipt_snapshot()
        intent = json.loads(ledger.q22_path.read_text())['intents'][0]
        assert result.reservation_id == intent['request_id'] == rows[29]['request_id']
        assert intent['binding']['actual_hash'] == fingerprint(value)
        assert rows[:29] == prefix and not ledger.q22_stop_path.exists()
    else:
        with pytest.raises(ModelUnavailable):
            asyncio.run(generate())
        assert ledger.q22_stop_path.exists()
        adapter.q22_authorizer = lambda x: permit(ready, actual_hash=fingerprint(x))
        with pytest.raises(ModelUnavailable, match='q22_batch_stopped'):
            asyncio.run(generate())
    assert len(calls) == (0 if outcome == 'actual_changed' else 1)
    assert ledger.summary()['unresolved_requests'] == (1 if outcome == 'unknown_usage' else 0)
    assert old == {path: path.read_bytes() for path in old}


def test_q22_persisted_task_deadline_cannot_restart(ready):
    ledger, now, specs, old, *_ = ready
    settled(ledger, ledger.reserve(experiment=True, q22_permit=permit(ready)))
    confirm_q19_round(ledger, specs[0]['task_ref'], 1, batch='Q22')
    now[0] += 300
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q22_task_deadline'):
        ledger.reserve(experiment=True, q22_permit=permit(ready, round_index=2))
    assert ledger.path.read_bytes() == before and ledger.q22_stop_path.exists()
    assert old == {path: path.read_bytes() for path in old}


def test_q22_write_ahead_interruption_cannot_resume(ready, monkeypatch):
    ledger = ready[0]
    write = ledger._write
    def interrupted(path, value):
        if path == ledger.path:
            raise OSError('synthetic interruption after intent')
        return write(path, value)
    monkeypatch.setattr(ledger, '_write', interrupted)
    with pytest.raises(BudgetUnavailable, match='ledger_io_or_lock'):
        ledger.reserve(experiment=True, q22_permit=permit(ready))
    monkeypatch.setattr(ledger, '_write', write)
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q22_intent_unresolved'):
        ledger.reserve(experiment=True, q22_permit=permit(ready))
    assert ledger.path.read_bytes() == before and ledger.q22_stop_path.exists()


@pytest.mark.parametrize('prior', ['d3-29', 'd3-30', 'd3-30-recovery'])
def test_q22_cannot_write_protocol_evidence_under_historical_round(ready, prior):
    from services.guide.model import ROOT
    ledger = ready[0]
    before = ledger.path.read_bytes()
    with pytest.raises(ModelUnavailable, match='q22_protocol_directory_historical'):
        DeepSeekModelAdapter(ledger, q22_grant_path=ledger.q22_path,
            q22_authorizer=lambda value: permit(ready),
            protocol_report_dir=ROOT / '.local' / prior / 'protocol')
    assert ledger.path.read_bytes() == before and not ledger.q22_stop_path.exists()


def test_independent_failed_terminal_stops_entire_q22_without_advancing_task(ready, monkeypatch):
    from scripts import round29_task as task
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round31()
    ledger, _, specs, old, *_ = ready
    before = ledger.path.read_bytes()
    with pytest.raises(RuntimeError, match='independent_terminal_failed'):
        task.finish_task_from_terminal(ledger, specs[0]['task_ref'], {'passed': False, 'reason': 'two_tickets_missing'})
    assert ledger.path.read_bytes() == before
    stopped = json.loads(ledger.q22_stop_path.read_text())
    assert stopped['reason'] == 'terminal_task_failed'
    assert json.loads(ledger.q22_path.read_text())['tasks'][0]['finished'] is False
    with pytest.raises(BudgetUnavailable, match='q22_batch_stopped'):
        ledger.reserve(experiment=True, q22_permit=permit(ready, task=2))
    assert old == {path: path.read_bytes() for path in old}
