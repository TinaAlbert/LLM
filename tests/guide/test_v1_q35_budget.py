"""Q35显式新包：沿原50笔前缀，仅同包许可可达59，旧批不可重开。"""
import asyncio
import json
import time
from decimal import Decimal

import httpx
import pytest

from test_v1_completion42 import (ready as q34_ready, permit as q34_permit,
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
def ready(q34_ready):
    ledger, now, specs, old, _ = q34_ready
    for turn in range(1, 6):
        now[0] += 11
        settled(ledger, ledger.reserve(experiment=True, q34_permit=q34_permit(q34_ready, turn=turn)))
        confirm_q19_round(ledger, specs[0]['task_ref'], turn, batch='Q34')
    stop_q19_batch(ledger, 'execution_or_terminal_failed', batch='Q34')
    now[0] += 61
    old = {path: path.read_bytes() for path in (*old, ledger.q34_path, ledger.q34_stop_path)}
    specs = [{'task_ref': 'q35-' + case_id, 'case_id': case_id, 'group': 'personal',
              'max_rounds': rounds, 'scope_hash': str(index) * 64}
             for index, (case_id, rounds) in enumerate((('V1-AC', 7), ('V1-B', 1), ('V1-D', 1)), 1)]
    grant = create_q19_grant(ledger, specs, 'd' * 64, batch='Q35')
    return ledger, now, specs, old, grant


def permit(ready, task=1, turn=1, actual='a' * 64):
    ledger, _, specs, *_ = ready
    spec = specs[task - 1]
    return validated_q19_permit(TaskPermitBinding(spec['task_ref'], task, turn, 1,
        'b' * 64, actual, spec['scope_hash']), ledger.q35_path, batch='Q35')


def test_nine_new_calls_reach_59_only_with_q35_and_preserve_all_legacy(ready):
    ledger, now, specs, old, grant = ready
    assert grant['prefix_requests'] == 50 and len(grant['legacy_sha256']) == 38
    assert grant['request_ceiling'] == 9 and grant['reserved_ceiling_cny'] == '0.288'
    with pytest.raises(BudgetUnavailable):
        ledger.reserve(experiment=True)
    for task, rounds in ((1, 7), (2, 1), (3, 1)):
        for turn in range(1, rounds + 1):
            now[0] += 11
            settled(ledger, ledger.reserve(experiment=True, q35_permit=permit(ready, task, turn)))
            confirm_q19_round(ledger, specs[task - 1]['task_ref'], turn, batch='Q35')
        finish_q19_task(ledger, specs[task - 1]['task_ref'], batch='Q35')
    assert ledger.summary()['physical_requests'] == 59
    assert old == {path: path.read_bytes() for path in old}
    assert json.loads(ledger.q35_stop_path.read_text())['reason'] == 'completed'
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable):
        ledger.reserve(experiment=True, q35_permit=permit(ready))
    assert ledger.path.read_bytes() == before
    ledger.q35_path.write_text('{}')
    with pytest.raises(BudgetUnavailable):
        ledger.summary()


@pytest.mark.parametrize('failure', [False, True])
def test_q35_scope_and_transport_single_send_then_fail_closed(ready, tmp_path, failure):
    ledger = ready[0]
    payload = case('only bounded transport')
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps(approval_values(_today())))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500) if failure else httpx.Response(200, json=response())
    adapter = DeepSeekModelAdapter(ledger,
        config=ModelConfig('synthetic-test-key-not-an-account', 'synthetic', _today(),
            'user_reported', 'test', Decimal(0)),
        input_validator=lambda value: value, transport=httpx.MockTransport(handler),
        q35_grant_path=ledger.q35_path,
        q35_authorizer=lambda value: permit(ready, actual=fingerprint(value)),
        protocol_report_dir=tmp_path / 'protocol')
    run = adapter.generate_experiment(payload, time.monotonic() + 5, approval_path=approval)
    if failure:
        with pytest.raises(ModelUnavailable):
            asyncio.run(run)
    else:
        asyncio.run(run)
        # 第一轮未忠实确认时不能启动第二轮，更不能花原批余量。
        with pytest.raises(BudgetUnavailable):
            ledger.reserve(experiment=True, q35_permit=permit(ready, turn=2))
    assert len(calls) == 1 and ledger.summary()['physical_requests'] == 51
    assert ledger.q35_stop_path.exists()
