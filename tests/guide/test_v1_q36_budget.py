"""Q36只续验B/D：复用55笔前缀，独立2次/.064，禁止重开Q35。"""
import asyncio
import json
import time
from decimal import Decimal

import httpx
import pytest

from test_v1_q35_budget import (ready as q35_ready, permit as q35_permit, q34_ready,
    q33_ready, q32_ready, q31_ready, q30_ready, q30_permit, q29_ready, q29_permit,
    q28_ready, q28_permit, q27_ready, q27_permit, q26_ready, q26_permit,
    q25_ready, q25_permit, q24_ready, q23_ready, q22_ready, q21_ready, q20_ready, q19_ready)
from test_q18_budget import settled
from test_q16_budget import case, response
from services.guide.budget import BudgetUnavailable
from services.guide.experiment import approval_values
from services.guide.knowledge import fingerprint
from services.guide.model import DeepSeekModelAdapter, ModelConfig, ModelUnavailable, _today
from services.guide.task_budget import (TaskPermitBinding, validated_q19_permit,
    create_q19_grant, confirm_q19_round, finish_q19_task, stop_q19_batch)


@pytest.fixture
def ready(q35_ready):
    ledger, now, specs, old, _ = q35_ready
    for turn in range(1, 5):
        now[0] += 11
        settled(ledger, ledger.reserve(experiment=True, q35_permit=q35_permit(q35_ready, turn=turn)))
        confirm_q19_round(ledger, specs[0]['task_ref'], turn, batch='Q35')
    finish_q19_task(ledger, specs[0]['task_ref'], batch='Q35')
    now[0] += 11
    settled(ledger, ledger.reserve(experiment=True, q35_permit=q35_permit(q35_ready, task=2)))
    stop_q19_batch(ledger, 'formal_delivery_failed', batch='Q35')
    now[0] += 61
    old = {path: path.read_bytes() for path in (*old, ledger.q35_path, ledger.q35_stop_path)}
    specs = [{'task_ref': 'q36-' + case_id, 'case_id': case_id, 'group': 'personal',
              'max_rounds': 1, 'scope_hash': str(index) * 64}
             for index, case_id in enumerate(('V1-B', 'V1-D'), 1)]
    grant = create_q19_grant(ledger, specs, 'd' * 64, batch='Q36')
    return ledger, now, specs, old, grant


def permit(ready, task=1, actual='a' * 64):
    ledger, _, specs, *_ = ready
    spec = specs[task - 1]
    return validated_q19_permit(TaskPermitBinding(spec['task_ref'], task, 1, 1,
        'b' * 64, actual, spec['scope_hash']), ledger.q36_path, batch='Q36')


def test_two_bd_calls_reach_57_and_preserve_q35_success_and_failure(ready):
    ledger, now, specs, old, grant = ready
    assert grant['prefix_requests'] == 55 and len(grant['legacy_sha256']) == 40
    assert grant['request_ceiling'] == 2 and grant['reserved_ceiling_cny'] == '0.064'
    assert [(s['case_id'], s['max_rounds']) for s in specs] == [('V1-B', 1), ('V1-D', 1)]
    with pytest.raises(BudgetUnavailable):
        ledger.reserve(experiment=True)
    with pytest.raises(BudgetUnavailable):
        validated_q19_permit(TaskPermitBinding('third-task', 3, 1, 1, 'b' * 64,
            'a' * 64, '1' * 64), ledger.q36_path, batch='Q36')
    for task in (1, 2):
        now[0] += 11
        settled(ledger, ledger.reserve(experiment=True, q36_permit=permit(ready, task)))
        confirm_q19_round(ledger, specs[task - 1]['task_ref'], 1, batch='Q36')
        finish_q19_task(ledger, specs[task - 1]['task_ref'], batch='Q36')
    assert ledger.summary()['physical_requests'] == 57
    assert old == {path: path.read_bytes() for path in old}
    assert json.loads(ledger.q36_stop_path.read_text())['reason'] == 'completed'
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable):
        ledger.reserve(experiment=True, q36_permit=permit(ready))
    assert ledger.path.read_bytes() == before
    ledger.q36_path.write_text('{}')
    with pytest.raises(BudgetUnavailable):
        ledger.summary()


def test_q36_actual_transport_first_failure_stops_without_d_send(ready, tmp_path):
    ledger = ready[0]
    payload = case('only bounded transport')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500)
    adapter = DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(),
            'user_reported', 'test', Decimal(0)),
        input_validator=lambda value: value, transport=httpx.MockTransport(handler),
        q36_grant_path=ledger.q36_path,
        q36_authorizer=lambda value: permit(ready, actual=fingerprint(value)),
        protocol_report_dir=tmp_path / 'protocol')
    with pytest.raises(ModelUnavailable):
        asyncio.run(adapter.generate_experiment(payload, time.monotonic() + 5, approval_path=approval))
    assert len(calls) == 1 and ledger.summary()['physical_requests'] == 56
    assert ledger.q36_stop_path.exists()
    with pytest.raises(BudgetUnavailable):
        ledger.reserve(experiment=True, q36_permit=permit(ready, task=2))
    assert ledger.summary()['physical_requests'] == 56
