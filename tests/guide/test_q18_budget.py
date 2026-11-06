"""Q18独立紧凑512批预算反例；仅tmp_path合成账本，不读取真实许可或发送模型。"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import multiprocessing

import pytest

from services.guide.assistant import corporate_export_scope
from services.guide.budget import (BudgetLedger, BudgetUnavailable, EXPERIMENT_REQUEST_CEILING,
    Q18_PACKAGE, _Q18Permit, _q18_permit, _q16_permit_for_validated_input,
    _q17_permit_for_validated_input)
from test_q17_budget import (CASES as PREVIOUS_CASES, SCOPES as PREVIOUS_SCOPES,
                            stopped_ledger as previous_stopped_ledger)


QUESTIONS = (
    '我想为这两个行程都拿到票，并尽量少花钱。只答当前下一步、必要条件和核对点，100字内；区分预估与已执行，在当前页回答，不导航。',
    '我先锁定了第二个行程。券现在属于谁，还能调整顺序吗？只答当前下一步、必要条件和核对点，100字内；区分预估与已执行，在当前页回答，不导航。',
)
CASES = tuple(deepcopy(case) for case in PREVIOUS_CASES)
for index, case in enumerate(CASES):
    context = json.loads(case['messages'][-1]['content'])
    context.update(question=QUESTIONS[index], goal=QUESTIONS[0])
    case['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False)
SCOPES = tuple(corporate_export_scope(case) for case in CASES)


def settled(ledger, reservation):
    return ledger.settle(reservation, prompt_tokens=100, completion_tokens=20,
                        provider_request_id='synthetic-q18-' + reservation.request_id)


def stopped_ledger(tmp_path):
    ledger = previous_stopped_ledger(tmp_path)
    ledger.create_q17_grant(PREVIOUS_SCOPES)
    reservation = ledger.reserve(experiment=True,
        q17_permit=_q17_permit_for_validated_input(PREVIOUS_SCOPES[0], ledger.q17_path))
    settled(ledger, reservation)
    ledger.stop_q17_batch('output_truncated')
    return ledger


@pytest.fixture
def ledger(tmp_path):
    return stopped_ledger(tmp_path)


def permit(ledger, index):
    return _q18_permit(SCOPES[index], ledger.q18_path)


def artifacts(ledger, *, include_q18=False):
    paths = (ledger.path, ledger.marker, ledger.lock_path, ledger.q16_path, ledger.q16_stop_path,
             ledger.q17_path, ledger.q17_stop_path)
    if include_q18:
        paths += (ledger.q18_path, ledger.q18_stop_path)
    return {p: p.read_bytes() if p.exists() else None for p in paths}


def assert_artifacts(before):
    assert before == {p: p.read_bytes() if p.exists() else None for p in before}


def test_q18_two_ordered_scopes_preserve_24_prefix_four_sidecars_and_amounts(ledger):
    before = ledger.receipt_snapshot()
    preserved = artifacts(ledger)
    grant = ledger.create_q18_grant(SCOPES)
    assert grant['package'] == Q18_PACKAGE == 'Q18-P26-compact-two'
    assert grant['request_ceiling'] == 2 and grant['prefix_requests'] == 24
    assert grant['reserved_per_request_cny'] == '0.032' and grant['reserved_ceiling_cny'] == '0.064'
    independent = hashlib.sha256(json.dumps(before, ensure_ascii=True, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
    assert grant['prefix_sha256'] == independent
    for name, path in (('q16_grant', ledger.q16_path), ('q16_stop', ledger.q16_stop_path),
                       ('q17_grant', ledger.q17_path), ('q17_stop', ledger.q17_stop_path)):
        assert grant[name + '_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert_artifacts(preserved)
    assert ledger.check_q18_scope(SCOPES[0]) is True and EXPERIMENT_REQUEST_CEILING == 22
    reservations = []
    for index in range(2):
        reservation = ledger.reserve(experiment=True, q18_permit=permit(ledger, index))
        reservations.append(reservation)
        assert reservation.amount_cny == Decimal('0.032')
        settled(ledger, reservation)
    assert sum((r.amount_cny for r in reservations), Decimal(0)) == Decimal('0.064')
    assert ledger.summary()['physical_requests'] == 26
    assert ledger.receipt_snapshot()[:24] == before
    assert_artifacts({p: value for p, value in preserved.items() if p != ledger.path})
    with pytest.raises(BudgetUnavailable, match='^q18_budget_stop$'):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))
    with pytest.raises(BudgetUnavailable, match='^q16_batch_stopped$'):
        ledger.check_q16_scope(PREVIOUS_SCOPES[1])
    with pytest.raises(BudgetUnavailable, match='^q17_batch_stopped$'):
        ledger.check_q17_scope(PREVIOUS_SCOPES[1])


@pytest.mark.parametrize('entry', ['strict', 'ordinary', 'q16', 'q17'])
@pytest.mark.parametrize('pending', [False, True])
def test_other_entries_cannot_occupy_q18_and_old_stops_remain_first(ledger, entry, pending):
    ledger.create_q18_grant(SCOPES)
    if pending:
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))
    before = artifacts(ledger, include_q18=True)
    options = {'experiment': entry != 'strict'}
    reason = 'q18_scope_required'
    if entry == 'q16':
        options['q16_permit'] = _q16_permit_for_validated_input(PREVIOUS_SCOPES[1], ledger.q16_path)
        reason = 'q16_batch_stopped'
    if entry == 'q17':
        options['q17_permit'] = _q17_permit_for_validated_input(PREVIOUS_SCOPES[1], ledger.q17_path)
        reason = 'q17_batch_stopped'
    with pytest.raises(BudgetUnavailable, match='^' + reason + '$'):
        ledger.reserve(**options)
    for batch in ('q16', 'q17'):
        with pytest.raises(BudgetUnavailable, match='^' + batch + '_batch_stopped$'):
            getattr(ledger, 'check_' + batch + '_scope')(PREVIOUS_SCOPES[1])
    assert_artifacts(before)


@pytest.mark.parametrize('change', ['q16_missing_stop', 'q17_missing_stop', 'q16_bool_sequence',
                                    'q17_bool_sequence', 'q17_wrong_identity', 'q17_wrong_stop_count'])
def test_create_q18_requires_both_original_batches_closed_and_associated(ledger, change):
    if change.endswith('missing_stop'):
        getattr(ledger, change[:3] + '_stop_path').unlink()
    else:
        batch = change[:3]
        stop = change.endswith('wrong_stop_count')
        path = getattr(ledger, batch + ('_stop_path' if stop else '_path'))
        value = json.loads(path.read_text())
        if change.endswith('bool_sequence'): value['intents'][0]['sequence'] = True
        if change.endswith('wrong_identity'): value['ledger_id'] = 'f' * 32
        if stop: value['physical_requests'] = 23
        path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(BudgetUnavailable, match='^q18_legacy_invalid$'):
        ledger.create_q18_grant(SCOPES)
    assert not ledger.q18_path.exists()


@pytest.mark.parametrize('artifact', ['q16_path', 'q16_stop_path', 'q17_path', 'q17_stop_path', 'prefix'])
def test_q18_detects_any_original_bytes_or_24_prefix_change(ledger, artifact):
    ledger.create_q18_grant(SCOPES)
    if artifact == 'prefix':
        value = json.loads(ledger.path.read_text())
        value['requests'][23]['reason'] = 'changed_original_receipt'
        ledger.path.write_text(json.dumps(value), encoding='utf-8')
    else:
        path = getattr(ledger, artifact)
        path.write_bytes(path.read_bytes() + b'\n')
    before = artifacts(ledger, include_q18=True)
    with pytest.raises(BudgetUnavailable, match='^q18_(?:legacy_invalid|legacy_changed|grant_invalid)$'):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))
    assert_artifacts(before)


@pytest.mark.parametrize('change', ['ledger_id', 'prefix_sha256', 'q17_stop_sha256', 'scope_hashes',
                                    'bool_schema', 'request_ceiling', 'per_reservation', 'total_reservation'])
def test_q18_tampered_grant_does_not_reserve(ledger, change):
    ledger.create_q18_grant(SCOPES)
    value = json.loads(ledger.q18_path.read_text())
    if change == 'ledger_id': value[change] = 'f' * 32
    if change in ('prefix_sha256', 'q17_stop_sha256'): value[change] = 'f' * 64
    if change == 'scope_hashes': value[change] = [SCOPES[0], SCOPES[0]]
    if change == 'bool_schema': value['schema'] = True
    if change == 'request_ceiling': value[change] = 3
    if change == 'per_reservation': value['reserved_per_request_cny'] = '0.033'
    if change == 'total_reservation': value['reserved_ceiling_cny'] = '0.096'
    ledger.q18_path.write_text(json.dumps(value), encoding='utf-8')
    before = artifacts(ledger, include_q18=True)
    with pytest.raises(BudgetUnavailable):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))
    assert_artifacts(before)


def test_q18_wrong_order_replay_forged_or_conflicting_permits_are_not_authority(ledger, tmp_path):
    ledger.create_q18_grant(SCOPES)
    for invalid in ('Q18', {'scope_hash': SCOPES[0]}, True,
                    _Q18Permit(SCOPES[0], ledger.q18_path, object()),
                    _q18_permit(SCOPES[0], tmp_path / 'other.json')):
        with pytest.raises(BudgetUnavailable, match='^q18_scope_invalid$'):
            ledger.reserve(experiment=True, q18_permit=invalid)
    with pytest.raises(BudgetUnavailable, match='^q18_scope_invalid$'):
        ledger.reserve(experiment=False, q18_permit=permit(ledger, 0))
    for old in ('q16', 'q17'):
        with pytest.raises(BudgetUnavailable, match='^q18_scope_invalid$'):
            ledger.reserve(experiment=True, q18_permit=permit(ledger, 0), **{old + '_permit': object()})
    with pytest.raises(BudgetUnavailable, match='^q18_scope_or_order_mismatch$'):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 1))
    settled(ledger, ledger.reserve(experiment=True, q18_permit=permit(ledger, 0)))
    with pytest.raises(BudgetUnavailable, match='^q18_scope_or_order_mismatch$'):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))


@pytest.mark.parametrize('position', ['before_ledger', 'after_ledger'])
def test_q18_write_ahead_interruption_cannot_resume(ledger, monkeypatch, position):
    ledger.create_q18_grant(SCOPES)
    original_write = ledger._write
    def crashing(path, value):
        if position == 'before_ledger' and path == ledger.path:
            raise RuntimeError('synthetic interrupted before ledger write')
        if position == 'after_ledger' and path == ledger.q18_path and value['intents'][-1]['state'] == 'reserved':
            raise RuntimeError('synthetic interrupted after ledger write')
        original_write(path, value)
    monkeypatch.setattr(ledger, '_write', crashing)
    with pytest.raises(RuntimeError):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))
    recovered = BudgetLedger(ledger.path)
    before = artifacts(recovered, include_q18=True)
    with pytest.raises(BudgetUnavailable, match='^(?:q18_intent_unresolved|usage_unresolved)$'):
        recovered.reserve(experiment=True, q18_permit=permit(recovered, 0))
    assert recovered.summary()['physical_requests'] == (24 if position == 'before_ledger' else 25)
    assert_artifacts(before)


def child_reserve(path, output):
    ledger = BudgetLedger(path)
    try:
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 1))
        output.put('reserved')
    except BudgetUnavailable as error:
        output.put(error.reason)


def test_q18_separate_process_observes_same_lock_and_pending_hold(ledger):
    ledger.create_q18_grant(SCOPES)
    context = multiprocessing.get_context('spawn')
    for held in (True, False):
        if not held:
            reservation = ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))
        output = context.Queue()
        child = context.Process(target=child_reserve, args=(str(ledger.path), output))
        def check():
            child.start(); child.join(15)
            assert child.exitcode == 0
            assert output.get(timeout=2) == ('ledger_io_or_lock' if held else 'usage_unresolved')
        try:
            if held:
                with ledger._locked(): check()
            else: check()
        finally:
            if child.is_alive(): child.terminate(); child.join(5)
            output.close()
    settled(ledger, reservation)
    assert ledger.check_q18_scope(SCOPES[1]) is True


def test_q18_unknown_usage_and_idempotent_stop_remain_closed_after_later_settlement(ledger):
    ledger.create_q18_grant(SCOPES)
    reservation = ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))
    ledger.mark_unresolved(reservation, 'usage_missing')
    ledger.stop_q18_batch('usage_missing')
    stopped = ledger.q18_stop_path.read_bytes()
    ledger.stop_q18_batch('another_failure')
    assert ledger.q18_stop_path.read_bytes() == stopped
    for scope in SCOPES:
        with pytest.raises(BudgetUnavailable, match='^q18_batch_stopped$'):
            ledger.check_q18_scope(scope)
    settled(ledger, reservation)
    before = artifacts(ledger, include_q18=True)
    with pytest.raises(BudgetUnavailable, match='^q18_batch_stopped$'):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 1))
    assert_artifacts(before)
    ledger.q18_path.unlink()
    with pytest.raises(BudgetUnavailable, match='^q18_already_recorded$'):
        ledger.create_q18_grant(SCOPES)


def test_q18_rejects_additional_fee_and_preserves_experiment_accounted_stop(ledger, monkeypatch):
    ledger.create_q18_grant(SCOPES)
    before = artifacts(ledger, include_q18=True)
    with pytest.raises(BudgetUnavailable, match='^q18_reservation_invalid$'):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 0), additional_fee_cny=Decimal('0.001'))
    assert_artifacts(before)
    monkeypatch.setattr(ledger, '_amount', lambda _: Decimal('0.5'))
    with pytest.raises(BudgetUnavailable, match='^experiment_budget_stop$'):
        ledger.reserve(experiment=True, q18_permit=permit(ledger, 0))
    assert_artifacts(before)


@pytest.mark.parametrize('change', ['invalid_scopes', 'duplicate_scopes', 'missing_prefix', 'unresolved_prefix'])
def test_create_q18_rejects_invalid_scope_or_nonsettled_24_prefix(ledger, change):
    scopes = SCOPES
    if change == 'invalid_scopes': scopes = (SCOPES[0], 'unapproved')
    if change == 'duplicate_scopes': scopes = (SCOPES[0], SCOPES[0])
    if change.endswith('prefix'):
        value = json.loads(ledger.path.read_text())
        if change == 'missing_prefix': value['requests'].pop()
        else:
            value['requests'][-1].update(state='unresolved', accounted_cny=None,
                prompt_tokens=None, completion_tokens=None, provider_request_id=None,
                cache_hit_tokens=None, cache_miss_tokens=None, reason='usage_missing')
        ledger.path.write_text(json.dumps(value), encoding='utf-8')
    before = artifacts(ledger, include_q18=True)
    reason = ('q18_scope_invalid' if change.endswith('scopes') else
              'q18_prefix_mismatch' if change == 'missing_prefix' else 'usage_unresolved')
    with pytest.raises(BudgetUnavailable, match='^' + reason + '$'):
        ledger.create_q18_grant(scopes)
    assert_artifacts(before)
