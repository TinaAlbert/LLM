"""Q31仅用临时39笔账本与MockTransport；不读取真实许可、不发送模型。"""
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
from services.guide.task_budget import (TaskPermitBinding, validated_q19_permit, create_q19_grant,
    stop_q19_batch, confirm_q19_round, finish_q19_task)
from test_round39_entry import (ready as q30_ready, permit as q30_permit, q29_ready, q29_permit, q28_ready, q28_permit, q27_ready, q27_permit, q26_ready, q26_permit, q25_ready, q25_permit,
    q24_ready, q23_ready, q22_ready, q21_ready, q20_ready, q19_ready)
from test_q18_budget import settled
from test_q16_budget import case, response


@pytest.fixture
def ready(q30_ready):
    ledger, now, specs, old, *_ = q30_ready
    settled(ledger, ledger.reserve(experiment=True, q30_permit=q30_permit(q30_ready)))
    stop_q19_batch(ledger, 'formal_delivery_failed', batch='Q30')
    now[0] += 61
    old = {p: p.read_bytes() for p in (*old, ledger.q30_path, ledger.q30_stop_path)}
    specs = [dict(s, task_ref='q31-' + s['task_ref']) for s in specs]
    prefix = ledger.receipt_snapshot()
    grant = create_q19_grant(ledger, specs, 'f' * 64, batch='Q31')
    return ledger, now, specs, old, prefix, grant


def permit(ready, actual_hash='a' * 64, task_index=1, round_index=1):
    ledger, _, specs, *_ = ready
    spec = specs[task_index - 1]
    binding = TaskPermitBinding(spec['task_ref'], task_index, round_index, 1, 'b' * 64, actual_hash, spec['scope_hash'])
    return validated_q19_permit(binding, ledger.q31_path, batch='Q31')


@pytest.mark.parametrize('unknown_usage', [False, True])
def test_adapter_binds_39_prefix_30_sidecars_and_unknown_usage_stops(ready, tmp_path, unknown_usage):
    ledger, _, _, old, prefix, grant = ready
    assert (grant['prefix_requests'], len(grant['legacy_sha256'])) == (39, 30)
    assert (grant['package'], grant['request_ceiling'], grant['reserved_ceiling_cny']) == ('Q31-P40-navigation-following', 11, '0.352')
    value = case('合成本步返回核对')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())), encoding='utf-8')
    calls = []
    def handler(request):
        calls.append(request)
        result = response('synthetic-q31')
        if unknown_usage:
            result.pop('usage')
        return httpx.Response(200, json=result)
    adapter = DeepSeekModelAdapter(ledger, config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(), 'user_reported', 'test', Decimal(0)),
        input_validator=lambda x: x, transport=httpx.MockTransport(handler), q31_grant_path=ledger.q31_path,
        q31_authorizer=lambda x: permit(ready, fingerprint(x)), protocol_report_dir=tmp_path / 'd3-40/protocol')
    async def generate():
        return await adapter.generate_experiment(value, time.monotonic() + 5, approval_path=approval)
    if unknown_usage:
        with pytest.raises(ModelUnavailable):
            asyncio.run(generate())
        assert ledger.q31_stop_path.exists() and ledger.summary()['unresolved_requests'] == 1
        with pytest.raises(ModelUnavailable):
            asyncio.run(generate())
    else:
        result = asyncio.run(generate())
        assert ledger.receipt_snapshot()[39]['request_id'] == result.reservation_id
        assert not ledger.q31_stop_path.exists()
    assert len(calls) == 1 and ledger.receipt_snapshot()[:39] == prefix
    assert all(p.read_bytes() == raw for p, raw in old.items())


def test_old_q30_decision_cannot_initialize_q31(tmp_path, monkeypatch):
    import services.guide.budget as budget
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round40()
    assert task.shared_prompt_version() == 'assistant-12'
    monkeypatch.setattr(task, 'checked_review', lambda path: {})
    monkeypatch.setattr(budget, 'BudgetLedger', lambda *a: pytest.fail('reject old decision before ledger'))
    manifest = tmp_path / 'review-manifest.json'
    manifest.write_text('{}')
    decision = tmp_path / 'decision.json'
    decision.write_text(json.dumps({'package': 'Q30-P39-lock-amount', 'approved': True,
        'source_kind': 'explicit_user_reply', 'source_reply': '批准Q30', 'review_manifest_sha256': task.sha(manifest)}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='explicit_matching_decision'):
        task.init_grant(tmp_path, decision)


def test_old_permit_and_changed_prior_sidecar_cannot_reserve(ready):
    ledger, _, _, _, prefix, _ = ready
    with pytest.raises(BudgetUnavailable, match='q31_scope_required'):
        ledger.reserve(experiment=True)
    old_permit = validated_q19_permit(permit(ready).binding, ledger.q30_path, batch='Q30')
    with pytest.raises(BudgetUnavailable, match='q30_batch_stopped'):
        ledger.reserve(experiment=True, q30_permit=old_permit)
    ledger.q30_stop_path.write_bytes(ledger.q30_stop_path.read_bytes() + b' ')
    with pytest.raises(BudgetUnavailable, match='q31_grant_invalid'):
        ledger.reserve(experiment=True, q31_permit=permit(ready))
    assert ledger.receipt_snapshot() == prefix and ledger.q31_stop_path.exists()


def test_eleventh_new_request_is_last_at_original_first_phase_limit(ready):
    ledger, now, specs, old, prefix, _ = ready
    for task_index, rounds in ((1, 9), (2, 2)):
        for round_index in range(1, rounds + 1):
            now[0] += 11
            settled(ledger, ledger.reserve(experiment=True, q31_permit=permit(ready, task_index=task_index, round_index=round_index)))
            confirm_q19_round(ledger, specs[task_index - 1]['task_ref'], round_index, batch='Q31')
        if task_index == 1:
            finish_q19_task(ledger, specs[0]['task_ref'], batch='Q31')
    assert ledger.summary()['physical_requests'] == 50
    before = ledger.path.read_bytes()
    now[0] += 11
    with pytest.raises(BudgetUnavailable, match='q31_request_limit'):
        ledger.reserve(experiment=True, q31_permit=permit(ready, task_index=2, round_index=3))
    assert ledger.path.read_bytes() == before and ledger.receipt_snapshot()[:39] == prefix
    assert all(p.read_bytes() == raw for p, raw in old.items())
    assert json.loads(ledger.q31_stop_path.read_text())['reason'] == 'q31_request_limit'
