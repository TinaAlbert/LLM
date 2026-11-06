"""Q24只用临时32笔账本和MockTransport；不读写真实许可。"""
import asyncio
from decimal import Decimal
import json
import time

import httpx
import pytest

from scripts import round29_task as task
from services.guide.budget import BudgetUnavailable
from services.guide.experiment import approval_values
from services.guide.knowledge import fingerprint
from services.guide.model import DeepSeekModelAdapter, ModelConfig, ModelUnavailable, _today
from services.guide.task_budget import (TaskPermitBinding, validated_q19_permit,
    create_q19_grant, stop_q19_batch, confirm_q19_round, finish_q19_task)
from test_q19_budget import ready as q19_ready
from test_q20_budget import ready as q20_ready
from test_q21_budget import ready as q21_ready
from test_q22_budget import ready as q22_ready
from test_round32_entry import ready as q23_ready, permit as q23_permit
from test_q18_budget import settled
from test_q16_budget import case, response


def settle_q23_two(ledger, specs, q23_ready):
    settled(ledger, ledger.reserve(experiment=True, q23_permit=q23_permit(q23_ready)))
    confirm_q19_round(ledger, specs[0]['task_ref'], 1, batch='Q23')
    binding = TaskPermitBinding(specs[0]['task_ref'], 1, 2, 1, 'b' * 64, 'a' * 64, specs[0]['scope_hash'])
    reservation = ledger.reserve(experiment=True, q23_permit=validated_q19_permit(binding, ledger.q23_path, batch='Q23'))
    return reservation


@pytest.fixture
def ready(q23_ready, q22_ready):
    ledger, specs, old, *_ = q23_ready
    settled(ledger, settle_q23_two(ledger, specs, q23_ready))
    stop_q19_batch(ledger, 'formal_delivery_failed', batch='Q23')
    now = q22_ready[1]; now[0] += 61
    old = {p: p.read_bytes() for p in (*old, ledger.q23_path, ledger.q23_stop_path)}
    specs = [dict(s, task_ref='q24-' + s['task_ref']) for s in specs]
    prefix = ledger.receipt_snapshot()
    grant = create_q19_grant(ledger, specs, 'f' * 64, batch='Q24')
    return ledger, now, specs, old, prefix, grant


def permit(ready, actual_hash='a' * 64, task_index=1, round_index=1):
    ledger, _, specs, *_ = ready
    spec = specs[task_index - 1]
    binding = TaskPermitBinding(spec['task_ref'], task_index, round_index, 1, 'b' * 64,
                                actual_hash, spec['scope_hash'])
    return validated_q19_permit(binding, ledger.q24_path, batch='Q24')


@pytest.mark.parametrize('outcome', ['success', 'unknown_usage', 'actual_changed'])
def test_adapter_binds_32_prefix_and_actual_hash_then_persists_failure(ready, tmp_path, outcome):
    ledger, _, specs, old, prefix, grant = ready
    assert grant['prefix_requests'] == 32 and len(grant['legacy_sha256']) == 16
    assert grant['package'] == 'Q24-P33-query-state'
    assert grant['request_ceiling'] == 20 and grant['reserved_ceiling_cny'] == '0.640'
    value = case('合成查询对象说明核验')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    calls = []
    def handler(request):
        calls.append(request); result = response('synthetic-q24')
        if outcome == 'unknown_usage': result.pop('usage')
        return httpx.Response(200, json=result)
    adapter = DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(), 'user_reported', 'test', Decimal(0)),
        input_validator=lambda x: x, transport=httpx.MockTransport(handler),
        q24_grant_path=ledger.q24_path,
        q24_authorizer=lambda x: permit(ready, '0' * 64 if outcome == 'actual_changed' else fingerprint(x)),
        protocol_report_dir=tmp_path / 'd3-33/protocol')
    async def generate():
        return await adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval)
    if outcome == 'success':
        result = asyncio.run(generate())
        assert ledger.receipt_snapshot()[32]['request_id'] == result.reservation_id
        assert not ledger.q24_stop_path.exists()
    else:
        with pytest.raises(ModelUnavailable): asyncio.run(generate())
        assert ledger.q24_stop_path.exists()
        with pytest.raises(ModelUnavailable): asyncio.run(generate())
    assert len(calls) == (0 if outcome == 'actual_changed' else 1)
    assert ledger.summary()['unresolved_requests'] == (1 if outcome == 'unknown_usage' else 0)
    assert ledger.receipt_snapshot()[:32] == prefix
    assert all(p.read_bytes() == data for p, data in old.items())


def test_requires_both_q23_receipts_settled_and_batch_closed(q23_ready):
    ledger, specs, *_ = q23_ready
    with pytest.raises(BudgetUnavailable, match='q24_prefix_mismatch'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q24')
    reservation = settle_q23_two(ledger, specs, q23_ready)
    with pytest.raises(BudgetUnavailable, match='usage_unresolved'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q24')
    settled(ledger, reservation)
    with pytest.raises(BudgetUnavailable, match='q24_legacy_invalid'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q24')
    assert not ledger.q24_path.exists()


def test_old_permits_and_terminal_failure_cannot_continue_q24(ready, monkeypatch):
    ledger, _, specs, old, prefix, _ = ready
    with pytest.raises(BudgetUnavailable, match='q24_scope_required'):
        ledger.reserve(experiment=True)
    for batch in ('Q19', 'Q20', 'Q21', 'Q22', 'Q23'):
        old_permit = validated_q19_permit(permit(ready).binding, getattr(ledger, batch.lower() + '_path'), batch=batch)
        with pytest.raises(BudgetUnavailable, match=batch.lower() + '_batch_stopped'):
            ledger.reserve(experiment=True, **{batch.lower() + '_permit': old_permit})
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round33()
    assert task.shared_prompt_version() == 'assistant-9'
    assert task.grant_paths(ledger) == (ledger.q24_path, ledger.q24_stop_path)
    with pytest.raises(RuntimeError, match='independent_terminal_failed'):
        task.finish_task_from_terminal(ledger, specs[0]['task_ref'], {'passed': False})
    with pytest.raises(BudgetUnavailable, match='q24_batch_stopped'):
        ledger.reserve(experiment=True, q24_permit=permit(ready))
    assert ledger.receipt_snapshot() == prefix
    assert all(p.read_bytes() == data for p, data in old.items())


def test_q23_decision_cannot_initialize_q24(tmp_path, monkeypatch):
    import services.guide.budget as budget
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round33()
    monkeypatch.setattr(task, 'checked_review', lambda path: {})
    monkeypatch.setattr(budget, 'BudgetLedger', lambda *a: pytest.fail('old decision must fail before ledger'))
    manifest = tmp_path / 'review-manifest.json'; manifest.write_text('{}')
    decision = tmp_path / 'decision.json'
    decision.write_text(json.dumps({'package': 'Q23-P32-current-action', 'approved': True,
        'source_kind': 'explicit_user_reply', 'source_reply': '批准Q23',
        'review_manifest_sha256': task.sha(manifest)}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='explicit_matching_decision'):
        task.init_grant(tmp_path, decision)


@pytest.mark.parametrize('attribute', ['q23_path', 'q23_stop_path'])
def test_changed_q23_sidecar_stops_before_reserving(ready, attribute):
    ledger = ready[0]; path = getattr(ledger, attribute)
    path.write_bytes(path.read_bytes() + b' ')
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q24_grant_invalid'):
        ledger.reserve(experiment=True, q24_permit=permit(ready))
    assert ledger.path.read_bytes() == before and ledger.q24_stop_path.exists()


def test_original_first_phase_50_request_stop_is_not_raised_to_52(ready):
    ledger, now, specs, old, prefix, _ = ready
    for task_index in (1, 2):
        for round_index in range(1, 10):
            now[0] += 11
            settled(ledger, ledger.reserve(experiment=True,
                q24_permit=permit(ready, task_index=task_index, round_index=round_index)))
            confirm_q19_round(ledger, specs[task_index - 1]['task_ref'], round_index, batch='Q24')
        finish_q19_task(ledger, specs[task_index - 1]['task_ref'], batch='Q24')
    assert ledger.summary()['physical_requests'] == 50
    now[0] += 11
    with pytest.raises(BudgetUnavailable, match='first_round_budget_stop'):
        ledger.reserve(experiment=True, q24_permit=permit(ready, task_index=3))
    assert ledger.receipt_snapshot()[:32] == prefix
    assert all(p.read_bytes() == data for p, data in old.items())
    assert json.loads(ledger.q24_stop_path.read_text())['reason'] == 'first_round_budget_stop'
