"""Q17独立精确批次；只创建tmp_path合成账本，不读取本机账本或发送模型。"""
from copy import deepcopy
import hashlib
import json
import multiprocessing
import time

import pytest

from services.guide.budget import (BudgetLedger, BudgetUnavailable, EXPERIMENT_REQUEST_CEILING,
                                  _Q17Permit, _q16_permit_for_validated_input,
                                  _q17_permit_for_validated_input)
from services.guide.assistant import corporate_export_scope
from services.guide.experiment import NAVIGATION_TOOL
from test_q16_budget import CASES as PREVIOUS_CASES

# 与获批原预览一致保留navigate定义；实际导航仍由controller独立拒绝。
CASES = tuple({**deepcopy(case), 'tools': [deepcopy(NAVIGATION_TOOL)]} for case in PREVIOUS_CASES)
SCOPES = tuple(corporate_export_scope(case) for case in CASES)


def settled(ledger, reservation):
    ledger.settle(reservation, prompt_tokens=100, completion_tokens=20,
                  provider_request_id='scratch-q17-' + reservation.request_id)


def stopped_ledger(tmp_path):
    now = [time.time() - 2500]
    ledger = BudgetLedger(tmp_path / 'usage.json', clock=lambda: now[0])
    ledger.initialize()
    for index in range(22):
        reservation = ledger.reserve(experiment=True)
        settled(ledger, reservation)
        now[0] += 61
    ledger.create_q16_grant(SCOPES)
    reservation = ledger.reserve(experiment=True,
        q16_permit=_q16_permit_for_validated_input(SCOPES[0], ledger.q16_path))
    settled(ledger, reservation)
    ledger.stop_q16_batch('chat_http_503')
    return ledger


@pytest.fixture
def ledger(tmp_path):
    return stopped_ledger(tmp_path)


def permit(ledger, index):
    return _q17_permit_for_validated_input(SCOPES[index], ledger.q17_path)


def test_q17_two_ordered_requests_same_identity_and_all_23_rows_preserved(ledger):
    before = json.loads(ledger.path.read_text(encoding='utf-8'))
    preserved = {p: p.read_bytes() for p in (ledger.marker, ledger.lock_path, ledger.q16_path, ledger.q16_stop_path)}
    grant = ledger.create_q17_grant(SCOPES)
    assert grant['prefix_requests'] == 23 and grant['ledger_id'] == before['ledger_id']
    independent = hashlib.sha256(json.dumps(before['requests'], ensure_ascii=True, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
    assert grant['prefix_sha256'] == independent
    assert EXPERIMENT_REQUEST_CEILING == 22 and ledger.check_q17_scope(SCOPES[0])
    for index in range(2):
        settled(ledger, ledger.reserve(experiment=True, q17_permit=permit(ledger, index)))
    after = json.loads(ledger.path.read_text(encoding='utf-8'))
    assert len(after['requests']) == 25 and after['requests'][:23] == before['requests']
    assert {k: v for k, v in before.items() if k != 'requests'} == {k: v for k, v in after.items() if k != 'requests'}
    assert preserved == {p: p.read_bytes() for p in preserved}
    with pytest.raises(BudgetUnavailable, match='q17_budget_stop'):
        ledger.reserve(experiment=True, q17_permit=permit(ledger, 0))
    with pytest.raises(BudgetUnavailable, match='q16_batch_stopped'):
        ledger.check_q16_scope(SCOPES[1])


@pytest.mark.parametrize('experiment', [False, True])
def test_ordinary_or_old_q16_context_cannot_use_q17(ledger, experiment):
    ledger.create_q17_grant(SCOPES)
    before = ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable, match='q17_scope_required'):
        ledger.reserve(experiment=experiment)
    with pytest.raises(BudgetUnavailable, match='q17_scope_required'):
        ledger.reserve(experiment=True, q16_permit=_q16_permit_for_validated_input(SCOPES[1], ledger.q16_path))
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize('change', ['missing_stop', 'wrong_stop_identity', 'wrong_stop_count', 'no_first_intent', 'bool_sequence'])
def test_create_requires_original_q16_first_failure_and_stopped_identity(ledger, change):
    if change == 'missing_stop':
        ledger.q16_stop_path.unlink()
    elif change == 'bool_sequence':
        data = json.loads(ledger.q16_path.read_text(encoding='utf-8'))
        data['intents'][0]['sequence'] = True
        ledger.q16_path.write_text(json.dumps(data), encoding='utf-8')
    elif change == 'no_first_intent':
        data = json.loads(ledger.q16_path.read_text(encoding='utf-8'))
        data['intents'] = []
        ledger.q16_path.write_text(json.dumps(data), encoding='utf-8')
    else:
        data = json.loads(ledger.q16_stop_path.read_text(encoding='utf-8'))
        data['ledger_id' if change == 'wrong_stop_identity' else 'physical_requests'] = 'f' * 32 if change == 'wrong_stop_identity' else 22
        ledger.q16_stop_path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(BudgetUnavailable, match='q17_legacy_invalid'):
        ledger.create_q17_grant(SCOPES)
    assert not ledger.q17_path.exists()


@pytest.mark.parametrize('change', ['q16_grant', 'q16_stop', 'prefix_row', 'prefix_request_id'])
def test_registered_q17_detects_any_original_evidence_change(ledger, change):
    ledger.create_q17_grant(SCOPES)
    if change.startswith('q16_'):
        path = ledger.q16_path if change == 'q16_grant' else ledger.q16_stop_path
        path.write_bytes(path.read_bytes() + b'\n')
    else:
        value = json.loads(ledger.path.read_text(encoding='utf-8'))
        value['requests'][22]['provider_request_id' if change == 'prefix_row' else 'request_id'] = 'e' * 32
        ledger.path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(BudgetUnavailable):
        ledger.check_q17_scope(SCOPES[0])
    assert len(ledger.receipt_snapshot()) == 23


@pytest.mark.parametrize('change', ['ledger_id', 'prefix_sha256', 'q16_grant_sha256', 'scope_hashes', 'extra', 'bool_schema', 'intent'])
def test_tampered_q17_sidecar_refused_before_reservation(ledger, change):
    ledger.create_q17_grant(SCOPES)
    value = json.loads(ledger.q17_path.read_text(encoding='utf-8'))
    if change in ('ledger_id', 'prefix_sha256', 'q16_grant_sha256'):
        value[change] = 'f' * (32 if change == 'ledger_id' else 64)
    if change == 'scope_hashes': value[change] = [SCOPES[0], SCOPES[0]]
    if change == 'extra': value['extra'] = True
    if change == 'bool_schema': value['schema'] = True
    if change == 'intent': value['intents'] = [{'sequence': 1, 'scope_hash': SCOPES[0], 'request_id': 'e'*32, 'state': 'intent'}]
    ledger.q17_path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(BudgetUnavailable):
        ledger.reserve(experiment=True, q17_permit=permit(ledger, 0))
    assert ledger.summary()['physical_requests'] == 23


def test_q17_wrong_order_replay_forged_permit_and_wrong_path_are_not_authority(ledger, tmp_path):
    ledger.create_q17_grant(SCOPES)
    for invalid in ('Q17', {'scope_hash': SCOPES[0]}, True,
                    _Q17Permit(SCOPES[0], ledger.q17_path, object()),
                    _q17_permit_for_validated_input(SCOPES[0], tmp_path/'other.json')):
        with pytest.raises(BudgetUnavailable, match='q17_scope_invalid'):
            ledger.reserve(experiment=True, q17_permit=invalid)
    with pytest.raises(BudgetUnavailable, match='q17_scope_or_order_mismatch'):
        ledger.reserve(experiment=True, q17_permit=permit(ledger, 1))
    settled(ledger, ledger.reserve(experiment=True, q17_permit=permit(ledger, 0)))
    with pytest.raises(BudgetUnavailable, match='q17_scope_or_order_mismatch'):
        ledger.reserve(experiment=True, q17_permit=permit(ledger, 0))
    with pytest.raises(BudgetUnavailable, match='q17_scope_invalid'):
        ledger.reserve(experiment=False, q17_permit=permit(ledger, 1))


@pytest.mark.parametrize('position', ['before_ledger', 'after_ledger'])
def test_write_ahead_interruption_cannot_send_or_resume(ledger, monkeypatch, position):
    ledger.create_q17_grant(SCOPES)
    original = ledger._write
    def crashing(path, value):
        if position == 'before_ledger' and path == ledger.path:
            raise RuntimeError('scratch interruption before ledger')
        if position == 'after_ledger' and path == ledger.q17_path and value['intents'][-1]['state'] == 'reserved':
            raise RuntimeError('scratch interruption after ledger')
        original(path, value)
    monkeypatch.setattr(ledger, '_write', crashing)
    with pytest.raises(RuntimeError):
        ledger.reserve(experiment=True, q17_permit=permit(ledger, 0))
    recovered = BudgetLedger(ledger.path)
    with pytest.raises(BudgetUnavailable, match='q17_intent_unresolved|usage_unresolved'):
        recovered.reserve(experiment=True, q17_permit=permit(recovered, 0))
    assert recovered.summary()['physical_requests'] == (23 if position == 'before_ledger' else 24)


def child_reserve(path, output):
    ledger = BudgetLedger(path)
    try:
        ledger.reserve(experiment=True, q17_permit=permit(ledger, 1))
        output.put('reserved')
    except BudgetUnavailable as error:
        output.put(error.reason)


def test_q17_separate_process_observes_same_file_lock_and_pending_hold(ledger):
    ledger.create_q17_grant(SCOPES)
    context = multiprocessing.get_context('spawn')
    for held in (True, False):
        if not held:
            reservation = ledger.reserve(experiment=True, q17_permit=permit(ledger, 0))
        output = context.Queue()
        child = context.Process(target=child_reserve, args=(str(ledger.path), output))
        def check():
            child.start(); child.join(15)
            assert child.exitcode == 0
            assert output.get(timeout=2) == ('ledger_io_or_lock' if held else 'usage_unresolved')
        try:
            if held:
                with ledger._locked(): check()
            else:
                check()
        finally:
            if child.is_alive(): child.terminate(); child.join(5)
            output.close()
    settled(ledger, reservation)
    assert ledger.check_q17_scope(SCOPES[1])


def test_q17_failure_stop_is_permanent_idempotent_and_does_not_reopen_q16(ledger):
    ledger.create_q17_grant(SCOPES)
    settled(ledger, ledger.reserve(experiment=True, q17_permit=permit(ledger, 0)))
    before = {p: p.read_bytes() for p in (ledger.path, ledger.q16_path, ledger.q16_stop_path)}
    ledger.stop_q17_batch('chat_http_503')
    stopped = ledger.q17_stop_path.read_bytes()
    ledger.stop_q17_batch('another_failure')
    assert stopped == ledger.q17_stop_path.read_bytes()
    assert before == {p: p.read_bytes() for p in before}
    with pytest.raises(BudgetUnavailable, match='q17_batch_stopped'):
        ledger.check_q17_scope(SCOPES[1])
    ledger.q17_path.unlink()
    with pytest.raises(BudgetUnavailable, match='q17_already_recorded'):
        ledger.create_q17_grant(SCOPES)
