"""Q23 新批不能复用Q22批准；预算仅临时账本/MockTransport。"""
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
    create_q19_grant, stop_q19_batch)
from test_q19_budget import ready as q19_ready
from test_q20_budget import ready as q20_ready
from test_q21_budget import ready as q21_ready
from test_q22_budget import ready as q22_ready, permit as q22_permit
from test_q18_budget import settled
from test_q16_budget import case, response


@pytest.fixture
def ready(q22_ready):
    ledger, now, specs, old, *_ = q22_ready
    settled(ledger, ledger.reserve(experiment=True, q22_permit=q22_permit(q22_ready)))
    stop_q19_batch(ledger, 'formal_delivery_failed', batch='Q22')
    now[0] += 61
    old = {p: p.read_bytes() for p in (*old, ledger.q22_path, ledger.q22_stop_path)}
    specs = [dict(s, task_ref='q23-' + s['task_ref']) for s in specs]
    prefix = ledger.receipt_snapshot()
    grant = create_q19_grant(ledger, specs, 'f' * 64, batch='Q23')
    return ledger, specs, old, prefix, grant


def permit(ready, actual_hash='a' * 64):
    ledger, specs, *_ = ready
    binding = TaskPermitBinding(specs[0]['task_ref'], 1, 1, 1, 'b' * 64,
                                actual_hash, specs[0]['scope_hash'])
    return validated_q19_permit(binding, ledger.q23_path, batch='Q23')


@pytest.mark.parametrize('outcome', ['success', 'unknown_usage', 'actual_changed'])
def test_new_batch_binds_thirty_prefix_actual_input_and_persistent_stop(ready, tmp_path, outcome):
    ledger, specs, old, prefix, grant = ready
    assert grant['prefix_requests'] == 30 and len(grant['legacy_sha256']) == 14
    assert grant['package'] == 'Q23-P32-current-action'
    assert grant['request_ceiling'] == 20 and grant['reserved_ceiling_cny'] == '0.640'
    value = case('合成当前动作说明核验')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    calls = []
    def handler(request):
        calls.append(request); result = response('synthetic-q23')
        if outcome == 'unknown_usage': result.pop('usage')
        return httpx.Response(200, json=result)
    adapter = DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(), 'user_reported', 'test', Decimal(0)),
        input_validator=lambda x: x, transport=httpx.MockTransport(handler),
        q23_grant_path=ledger.q23_path,
        q23_authorizer=lambda x: permit(ready, '0' * 64 if outcome == 'actual_changed' else fingerprint(x)),
        protocol_report_dir=tmp_path / 'd3-32/protocol')
    async def generate():
        return await adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval)
    if outcome == 'success':
        result = asyncio.run(generate())
        assert ledger.receipt_snapshot()[30]['request_id'] == result.reservation_id
        assert not ledger.q23_stop_path.exists()
    else:
        with pytest.raises(ModelUnavailable): asyncio.run(generate())
        assert ledger.q23_stop_path.exists()
        with pytest.raises(ModelUnavailable): asyncio.run(generate())
    assert len(calls) == (0 if outcome == 'actual_changed' else 1)
    assert ledger.summary()['unresolved_requests'] == (1 if outcome == 'unknown_usage' else 0)
    assert ledger.receipt_snapshot()[:30] == prefix
    assert all(p.read_bytes() == data for p, data in old.items())


def test_q23_requires_settled_closed_thirtieth_request(q22_ready):
    ledger, _, specs, *_ = q22_ready
    with pytest.raises(BudgetUnavailable, match='q23_prefix_mismatch'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q23')
    reservation = ledger.reserve(experiment=True, q22_permit=q22_permit(q22_ready))
    with pytest.raises(BudgetUnavailable, match='usage_unresolved'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q23')
    settled(ledger, reservation)
    with pytest.raises(BudgetUnavailable, match='q23_legacy_invalid'):
        create_q19_grant(ledger, specs, 'f' * 64, batch='Q23')
    assert not ledger.q23_path.exists()


def test_old_permits_and_terminal_failure_cannot_continue_q23(ready, monkeypatch):
    ledger, specs, old, prefix, _ = ready
    with pytest.raises(BudgetUnavailable, match='q23_scope_required'):
        ledger.reserve(experiment=True)
    for batch in ('Q19', 'Q20', 'Q21', 'Q22'):
        old_permit = validated_q19_permit(permit(ready).binding, getattr(ledger, batch.lower() + '_path'), batch=batch)
        with pytest.raises(BudgetUnavailable, match=batch.lower() + '_batch_stopped'):
            ledger.reserve(experiment=True, **{batch.lower() + '_permit': old_permit})
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round32()
    assert task.shared_prompt_version() == 'assistant-9'
    assert task.grant_paths(ledger) == (ledger.q23_path, ledger.q23_stop_path)
    with pytest.raises(RuntimeError, match='independent_terminal_failed'):
        task.finish_task_from_terminal(ledger, specs[0]['task_ref'], {'passed': False})
    with pytest.raises(BudgetUnavailable, match='q23_batch_stopped'):
        ledger.reserve(experiment=True, q23_permit=permit(ready))
    assert ledger.receipt_snapshot() == prefix
    assert all(p.read_bytes() == data for p, data in old.items())


def test_q22_decision_cannot_initialize_new_batch(tmp_path, monkeypatch):
    import services.guide.budget as budget
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round32()
    monkeypatch.setattr(task, 'checked_review', lambda path: {})
    monkeypatch.setattr(budget, 'BudgetLedger', lambda *a: pytest.fail('old decision must fail before ledger'))
    manifest = tmp_path / 'review-manifest.json'; manifest.write_text('{}')
    decision = tmp_path / 'decision.json'
    decision.write_text(json.dumps({'package': 'Q22-P31-structured-task', 'approved': True,
        'source_kind': 'explicit_user_reply', 'source_reply': '批准Q22',
        'review_manifest_sha256': task.sha(manifest)}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='explicit_matching_decision'):
        task.init_grant(tmp_path, decision)
