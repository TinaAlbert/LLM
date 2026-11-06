"""P29新增风险：正式交付引用、对象歧义、拒绝/重复不推进相对事件。"""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest

from scripts.round29_task import (digest, validate_confirmation, relative_events,
    recorded_relative_event, round_receipt, EVENT_RULE)
from scripts.round28_records import freeze_task, record_transition


def confirmation(formal):
    return {'task_ref': 'p29-test', 'turn': 1, 'formal_reply_sha256': digest(formal),
        'quote': formal['reply'], 'action': 'pay', 'object': 'trip_1',
        'actor': 'engineering_synthetic_user', 'faithful': True, 'ambiguous': False, 'note': ''}


def test_formal_quote_is_required_even_when_human_says_faithful():
    formal = {'reply': '请为trip_1付款。'}
    good = confirmation(formal)
    assert validate_confirmation(good, formal, task_ref='p29-test', turn=1) == good
    for change in ({'formal_reply_sha256': '0'*64}, {'quote': '请为trip_2付款。'},
                   {'object': 'trip_2'}, {'ambiguous': True}, {'turn': 2}):
        bad = good | change
        with pytest.raises(RuntimeError):
            validate_confirmation(bad, formal, task_ref='p29-test', turn=1)
    with pytest.raises(RuntimeError, match='replay'):
        validate_confirmation(good, formal, task_ref='p29-test', turn=1, consumed=[digest(good)])


@pytest.mark.parametrize('text', ['如果可以，请为trip_1付款。', '不要为trip_1付款。',
    '请核对trip_1和trip_2后付款。', 'trip_1已付款。'])
def test_condition_negative_multiple_objects_or_past_fact_cannot_authorize_payment(text):
    formal = {'reply': text}
    with pytest.raises(RuntimeError):
        validate_confirmation(confirmation(formal), formal, task_ref='p29-test', turn=1)


def test_relative_events_follow_object_payment_and_only_accepted_unique_queries():
    record = freeze_task(group='B1', task_ref='p29-test', case_id='DEV27-05',
        task_class='completable', mode='offline_scripted', host_only={'event_rule': EVENT_RULE})
    state = {}
    def action(kind, ref, status=200, duplicate=False):
        value = f'booking.{kind}.{ref}'
        record_transition(record, action_ref=str(len(record['transitions'])+1), action=value,
            actor='synthetic', http_status=status, accepted=status < 300, duplicate=duplicate,
            evidence={'receipt': True})
        return relative_events(record, state, value, accepted=status < 300, duplicate=duplicate)
    assert action('query', 'trip_2') == []
    assert action('pay', 'trip_2') == []
    assert action('query', 'trip_1') == []
    assert action('query', 'trip_2', 409) == []
    assert action('query', 'trip_2', duplicate=True) == []
    assert action('query', 'trip_2') == ['payment-trip_2']
    recorded_relative_event(record, state, 'payment-trip_2', {'actual_worker': True})
    assert action('query', 'trip_2') == ['ticket-trip_2']
    recorded_relative_event(record, state, 'ticket-trip_2', {'actual_worker': True})
    assert action('query', 'trip_2') == []


@pytest.mark.parametrize('state,expected', [('settled', True), ('pending', None), ('no_intent', False)])
def test_failed_delivery_start_is_associated_by_task_intent_not_candidate_file(tmp_path, state, expected):
    path = tmp_path / 'q19.json'
    intents = [] if state == 'no_intent' else [{'binding': {'task_ref': 'p29-test',
        'round_index': 2}, 'request_id': 'actual-reservation'}]
    path.write_text(json.dumps({'intents': intents}), encoding='utf-8')
    ledger = SimpleNamespace(q19_path=path, receipt_snapshot=lambda: [{'request_id': 'actual-reservation',
        'state': state, 'accounted_cny': '0.002'}])
    started, usage = round_receipt(ledger, 'p29-test', 2)
    assert started is expected
    assert usage == ({'reservation_ref': 'actual-reservation', 'state': 'settled',
        'accounted_cny': '0.002'} if expected is True else None)
