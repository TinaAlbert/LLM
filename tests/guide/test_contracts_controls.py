"""独立合成预期与真实OS资源锁；不连接PG、不调用模型、不计AI成绩。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from services.guide import controls
from services.guide.contracts import parse_candidate, validate_evidence
from services.guide.knowledge import load_knowledge


def candidate(**changes):
    return {"status": "next_step", "step_id": "booking.refresh", "anchor_id": "booking.refresh",
            "explanation": "请刷新原预订的当前状态", "preconditions": [],
            "expected_change": "查看原付款和票据进度", "references": ["knowledge:booking.refresh", "observation:current"],
            "unknowns": [], "amount_ref": None, **changes}


def public():
    # 金额刻意不同，避免同一实现/固定业务夹具金额掩盖错误引用。
    return {"payment_profile_state": "usable", "flight_state": "available", "data": {
        "trip": {"revision": 2}, "quote": {"valid": True, "trip_revision": 2,
                                             "amount_minor": 137, "currency": "DEMO"},
        "lock": {"valid": True, "trip_revision": 2, "amount_minor": 259, "currency": "DEMO"},
        "payment": None, "ticket": None}}


def verify(payload, projection=None):
    bundle = load_knowledge()
    value = parse_candidate(payload)
    return validate_evidence(value, public() if projection is None else projection, bundle.knowledge, bundle.anchors)


@pytest.mark.parametrize('field', ['role', 'object_id', 'tools', 'url', 'parameters'])
def test_extra_candidate_fields_fail_closed(field):
    with pytest.raises(ValueError): parse_candidate(candidate(**{field: 'forbidden'}))


@pytest.mark.parametrize('change', [
    {'references': ['knowledge:unpublished']}, {'references': ['observation:other']},
    {'anchor_id': 'private.payment'}, {'anchor_id': 'https://example.invalid/pay'},
    {'step_id': 'payment.automatic'},
])
def test_unknown_knowledge_observation_and_entry_are_rejected(change):
    with pytest.raises(ValueError): verify(candidate(**change))


@pytest.mark.parametrize('text', ['请支付1100元', '付款金额1100 CNY', '金额￥1100'])
def test_explicit_generated_currency_amount_is_rejected(text):
    with pytest.raises(ValueError): parse_candidate(candidate(explanation=text))


@pytest.mark.parametrize('text', ['应付金额为1100', '应付一千一百元', '支付１１００元',
                                'Please pay eleven hundred DEMO', '应付١١٠٠元', '应付⑩元'])
def test_generated_amount_without_currency_suffix_or_with_chinese_number_is_rejected(text):
    with pytest.raises(ValueError): parse_candidate(candidate(explanation=text))


def test_payment_confirmation_must_reference_current_frozen_amount():
    with pytest.raises(ValueError):
        verify(candidate(step_id='payment.confirm', anchor_id='payment.confirm',
                         references=['knowledge:payment.confirm', 'observation:current'],
                         amount_ref='quote.amount_minor'))


def test_valid_frozen_amount_reference_is_not_model_generated_number():
    value = verify(candidate(step_id='payment.confirm', anchor_id='payment.confirm',
                             references=['knowledge:payment.confirm', 'observation:current'],
                             amount_ref='lock.amount_minor'))
    assert value.amount_ref == 'lock.amount_minor'


def test_definite_failed_payment_allows_confirmation_with_new_current_valid_lock():
    value = public()
    value['data']['payment'] = {'status': 'failed', 'amount_minor': 137, 'currency': 'DEMO'}
    confirmed = verify(candidate(step_id='payment.confirm', anchor_id='payment.confirm',
                                 references=['knowledge:payment.confirm', 'observation:current'],
                                 amount_ref='lock.amount_minor'), value)
    assert confirmed.amount_ref == 'lock.amount_minor'


@pytest.mark.parametrize('change', [None, {'valid': False}, {'trip_revision': 1}])
def test_failed_payment_cannot_confirm_without_current_valid_lock(change):
    value = public()
    value['data']['payment'] = {'status': 'failed', 'amount_minor': 137, 'currency': 'DEMO'}
    if change is None:
        value['data']['lock'] = None
    else:
        value['data']['lock'].update(change)
    with pytest.raises(ValueError, match='付款前提未成立'):
        verify(candidate(step_id='payment.confirm', anchor_id='payment.confirm',
                         references=['knowledge:payment.confirm', 'observation:current'],
                         amount_ref='lock.amount_minor'), value)


@pytest.mark.parametrize('kind', ['quote', 'lock'])
def test_stale_revision_amount_is_rejected_even_if_valid_flag_is_true(kind):
    value = public()
    value['data'][kind]['trip_revision'] = 1
    with pytest.raises(ValueError): verify(candidate(amount_ref=kind+'.amount_minor'), value)


@pytest.mark.parametrize('state', ['processing', 'unknown', 'succeeded'])
@pytest.mark.parametrize('step', ['profile.bind', 'quote.request', 'quote.lock', 'payment.confirm', 'trip.revise'])
def test_existing_payment_cannot_restart_booking_or_payment(state, step):
    value = public()
    value['data']['payment'] = {'status': state, 'amount_minor': 259, 'currency': 'DEMO'}
    with pytest.raises(ValueError):
        verify(candidate(step_id=step, anchor_id=step, references=['knowledge:'+step],
                         amount_ref='lock.amount_minor' if step == 'payment.confirm' else None), value)


@pytest.mark.parametrize('ticket', [None, {'status': 'valid', 'trip_revision': 1},
                                  {'status': 'failed', 'trip_revision': 2}])
def test_completion_requires_current_valid_ticket(ticket):
    value = public()
    value['data']['ticket'] = ticket
    with pytest.raises(ValueError): verify(candidate(status='completion_candidate'), value)


@pytest.mark.parametrize('change', [('payment_profile_state', 'unknown'), ('payment_profile_state', 'unusable'),
                                  ('flight_state', 'unknown'), ('flight_state', 'unavailable')])
def test_unknown_or_unavailable_prerequisite_cannot_confirm_payment(change):
    value = public()
    value[change[0]] = change[1]
    with pytest.raises(ValueError): verify(candidate(step_id='payment.confirm', anchor_id='payment.confirm'), value)


def test_real_thread_cannot_take_same_slot_and_released_slot_can_be_reused(tmp_path):
    outcomes = []
    with controls.Slot(9, tmp_path):
        def compete():
            try:
                with controls.Slot(9, tmp_path): outcomes.append('acquired')
            except controls.Busy: outcomes.append('busy')
        thread = threading.Thread(target=compete)
        thread.start(); thread.join(2)
        assert not thread.is_alive() and outcomes == ['busy']
    with controls.Slot(9, tmp_path): pass


SLOT_SCRIPT = '''from pathlib import Path
import sys
from services.guide.controls import Slot, Busy
try:
    with Slot(int(sys.argv[2]), Path(sys.argv[1])): pass
except Busy:
    sys.exit(23)
'''


def competitor(root, index):
    return subprocess.run([sys.executable, '-X', 'utf8', '-c', SLOT_SCRIPT, str(root), str(index)],
                          capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)


def test_real_process_cannot_take_same_slot_and_distinct_lane_remains_available(tmp_path):
    with controls.Slot(9, tmp_path):
        assert competitor(tmp_path, 9).returncode == 23
        assert competitor(tmp_path, 8).returncode == 0
    assert competitor(tmp_path, 9).returncode == 0


def test_active_delegation_limit_is_eight_and_release_is_reusable(tmp_path):
    slots = [controls.delegation_slot(tmp_path) for _ in range(8)]
    try:
        with pytest.raises(controls.Busy, match='active_delegation_limit'):
            controls.delegation_slot(tmp_path)
        assert competitor(tmp_path, 0).returncode == 23
        slots.pop().close()
        replacement = controls.delegation_slot(tmp_path)
        replacement.close()
    finally:
        for slot in slots: slot.close()


@pytest.mark.parametrize('kind,limit', [('next', 6), ('observation', 24)])
def test_minute_limit_and_exact_expiry_boundary(tmp_path, monkeypatch, kind, limit):
    now = [1_000_000.0]
    monkeypatch.setattr(controls, 'time', SimpleNamespace(time=lambda: now[0]))
    for _ in range(limit): controls.take_rate(kind, tmp_path)
    with pytest.raises(controls.Busy, match='guide_rate_limited'): controls.take_rate(kind, tmp_path)
    now[0] += 59.999
    with pytest.raises(controls.Busy, match='guide_rate_limited'): controls.take_rate(kind, tmp_path)
    now[0] += .001
    controls.take_rate(kind, tmp_path)
    saved = json.loads((tmp_path/'guide-resource-rates.json').read_text('utf8'))
    assert saved[kind] == [now[0]]


def test_physical_next_and_observation_rates_are_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(controls, 'time', SimpleNamespace(time=lambda: 1_000_000.0))
    for _ in range(6): controls.take_rate('next', tmp_path)
    for _ in range(24): controls.take_rate('observation', tmp_path)
    with pytest.raises(controls.Busy): controls.take_rate('next', tmp_path)
    with pytest.raises(controls.Busy): controls.take_rate('observation', tmp_path)


@pytest.mark.parametrize('change', [
    {'next': ['corrupt']}, {'next': [float('nan')]}, {'next': [1_000_001]},
    {'last_time': float('nan')},
])
def test_corrupted_rate_timestamps_fail_closed_instead_of_restoring_quota(tmp_path, monkeypatch, change):
    monkeypatch.setattr(controls, 'time', SimpleNamespace(time=lambda: 1_000_000.0))
    payload = {'schema': 1, 'last_time': 1_000_000.0, 'next': [], 'observation': [], **change}
    (tmp_path/'guide-resource-rates.json').write_text(json.dumps(payload), encoding='utf8')
    with pytest.raises(controls.Busy, match='resource_accounting_unavailable'):
        controls.take_rate('next', tmp_path)


@pytest.mark.parametrize('body', [
    '{"schema":1,"last_time":1000000,"next":[1000000,1000000,1000000,1000000,1000000,1000000],"next":[],"observation":[]}',
    '{"schema":1,"schema":1,"last_time":1000000,"next":[],"observation":[]}',
    '{"schema":true,"last_time":1000000,"next":[],"observation":[]}',
    '{"schema":1.0,"last_time":1000000,"next":[],"observation":[]}',
    '{"schema":1,"last_time":1000000,"next":[999900,999900,999900,999900,999900,999900,999900],"observation":[]}',
    '{"schema":1,"last_time":-1,"next":[],"observation":[]}',
    '[]',
    'null',
])
def test_malformed_rate_record_is_not_rewritten_or_used_to_restore_quota(tmp_path, monkeypatch, body):
    monkeypatch.setattr(controls, 'time', SimpleNamespace(time=lambda: 1_000_000.0))
    path = tmp_path / 'guide-resource-rates.json'
    path.write_text(body, encoding='utf8')
    with pytest.raises(controls.Busy, match='resource_accounting_unavailable'):
        controls.take_rate('next', tmp_path)
    assert path.read_text('utf8') == body
    assert not path.with_suffix('.tmp').exists()
