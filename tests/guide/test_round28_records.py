"""P28纯记录反例：临时数据，无PG、模型、账本或许可写入。"""
from copy import deepcopy

import pytest

from scripts import round28_records as module


def freeze(*, group='B1', task_ref='task-a', task_class='completable',
           mode='real_model', events=()):
    return module.freeze_task(group=group, task_ref=task_ref,
        case_id='DEV27-08' if task_class == 'unknown' else 'DEV27-05',
        task_class=task_class, mode=mode,
        host_only={'principal': 'synthetic-alice', 'objects': ['trip-a', 'trip-b'],
            'initial_23_table_sha256': 'synthetic-snapshot', 'business_time': '2026-10-19T00:00:00Z',
            'versions': {'product': 'synthetic-v1', 'prompt': 'assistant-6'},
            'information_condition': 'published-flow', 'round_limit': 2}, events=events)


def model_round(record, ref='round-1', *, started=True, suggestion='查询这笔原预订',
                usage=None):
    return module.record_model_attempt(record, round_ref=ref, started=started,
        suggestion=suggestion, usage=usage)


def action(record, ref='query-1', name='query', *, accepted=True, status=200,
           duplicate=False, suggestion='round-1', added=False):
    return module.record_transition(record, action_ref=ref, action=name,
        actor='synthetic-engineering-user', http_status=status, accepted=accepted,
        duplicate=duplicate, suggestion_ref=suggestion, engineering_added_step=added,
        reason='工程补足遗漏动作' if added else None,
        evidence={'method': 'GET', 'path': '/trips/synthetic/booking',
                  'response_sha256': 'synthetic-visible-receipt'})


def terminal(record, passed=True):
    module.record_terminal(record, passed=passed,
        evidence={'tickets': 2, 'captured_minor': 140000, 'independent_sql_sha256': 'synthetic'})


def receipt(ref):
    return {'reservation_ref': ref, 'state': 'settled', 'accounted_cny': '0.001'}


def test_same_task_multiple_rounds_and_snapshots_count_once():
    record = freeze()
    prepared = deepcopy(record)
    model_round(record, usage=receipt('request-1'))
    action(record)
    first_round = deepcopy(record)
    model_round(record, 'round-2', usage=receipt('request-2'))
    action(record, 'query-2', suggestion='round-2')
    terminal(record)
    summary = module.summarize([prepared, first_round, record, record])
    assert summary['ai_task_denominator'] == 1
    assert summary['physical_reservations_from_records'] == 2
    assert summary['accounted_cny'] == '0.002'
    assert summary['actual_bill_cny'] is None
    assert summary['task_classes']['completable']['model_faithful_complete'] == 1
    assert len(summary['tasks'][0]['real_model_suggestions']) == 2
    assert len(summary['tasks'][0]['actual_actions']) == 2


def test_group_is_part_of_task_identity_and_class_denominators_are_separate():
    records = []
    for group, category in [('B0', 'completable'), ('B1', 'completable'), ('B1', 'unknown')]:
        record = freeze(group=group, task_ref='unknown' if category == 'unknown' else 'same',
                        task_class=category)
        model_round(record, usage=receipt(group + category))
        action(record)
        terminal(record)
        records.append(record)
    summary = module.summarize(records)
    assert summary['ai_task_denominator'] == 3
    assert summary['task_classes']['completable']['denominator'] == 2
    assert summary['task_classes']['unknown']['denominator'] == 1
    assert summary['task_classes']['infeasible']['denominator'] == 0
    assert summary['completion_rate'] is None


def test_offline_success_does_not_create_ai_start_suggestion_usage_or_success():
    record = freeze(mode='offline_scripted')
    model_round(record, started=False)
    action(record)
    terminal(record)
    summary = module.summarize([record])
    assert summary['ai_task_denominator'] == 0
    assert summary['offline_scripted_tasks'] == 1
    task = summary['tasks'][0]
    assert task['independent_terminal_passed'] is True
    assert task['model_faithful_complete'] is False
    assert task['real_model_suggestions'] == []
    assert len(task['scripted_suggestions']) == 1
    assert summary['physical_reservations_from_records'] == 0


@pytest.mark.parametrize('started', [True, None])
def test_scripted_stub_cannot_claim_a_real_model_start(started):
    with pytest.raises(ValueError, match='offline_cannot_start'):
        model_round(freeze(mode='offline_scripted'), started=started)


def test_prepare_and_pre_dispatch_stop_are_zero_but_unknown_start_stays_unknown():
    prepared = freeze(task_ref='prepare')
    stopped = freeze(task_ref='stopped')
    model_round(stopped, started=False, suggestion=None)
    uncertain = freeze(task_ref='interrupted')
    model_round(uncertain, started=None, suggestion=None)
    baseline = module.summarize([prepared, stopped])
    assert baseline['ai_task_denominator'] == 0
    assert baseline['accounted_cny'] == '0'
    summary = module.summarize([prepared, stopped, uncertain])
    assert summary['ai_task_denominator'] is None
    assert summary['confirmed_started_ai_tasks'] == 0
    assert summary['unknown_start_tasks'] == 1
    assert summary['task_classes']['completable']['denominator'] is None
    assert summary['physical_reservations_from_records'] is None
    assert summary['accounted_cny'] is None


def test_known_started_failure_remains_in_denominator_with_unknown_usage():
    record = freeze()
    model_round(record, suggestion=None)
    terminal(record, False)
    summary = module.summarize([record])
    assert summary['ai_task_denominator'] == 1
    assert summary['task_classes']['completable']['independent_terminal_passed'] == 0
    assert summary['physical_reservations_from_records'] is None
    assert summary['accounted_cny'] is None


def test_pending_receipt_does_not_turn_unknown_cost_into_zero():
    record = freeze()
    model_round(record, usage={'reservation_ref': 'request-1', 'state': 'unresolved',
                               'accounted_cny': None})
    summary = module.summarize([record])
    assert summary['physical_reservations_from_records'] == 1
    assert summary['unresolved_receipts'] == 1
    assert summary['accounted_cny'] is None
    assert summary['known_settled_accounted_cny'] == '0'


def test_human_added_correct_action_can_prove_terminal_but_never_model_faithfulness():
    record = freeze()
    model_round(record, usage=receipt('request-1'))
    action(record)
    action(record, 'missing-lock', 'lock', suggestion=None, added=True)
    terminal(record)
    summary = module.summarize([record])
    task = summary['tasks'][0]
    assert task['independent_terminal_passed'] is True
    assert task['model_faithful_complete'] is False
    assert len(task['engineering_added_steps']) == 1
    assert summary['ai_task_denominator'] == 1


def test_unattributed_action_does_not_get_credit_from_terminal_success():
    record = freeze()
    model_round(record, usage=receipt('request-1'))
    action(record, suggestion=None)
    terminal(record)
    assert module.summarize([record])['tasks'][0]['model_faithful_complete'] is False


def test_independent_unknown_terminal_is_not_guessed_failure_or_success():
    record = freeze()
    model_round(record, usage=receipt('request-1'))
    action(record)
    terminal(record, None)
    task = module.summarize([record])['tasks'][0]
    assert task['independent_terminal_passed'] is None
    assert task['model_faithful_complete'] is False


def test_rejected_and_duplicate_payment_do_not_advance_event_schedule():
    events = [{'event_id': 'payment-worker', 'after_accepted_action': 2, 'action': 'query'}]
    record = freeze(events=events)
    model_round(record, usage=receipt('request-1'))
    assert action(record, 'pay', 'pay') == []
    assert action(record, 'pay-again', 'pay', duplicate=True) == []
    assert action(record, 'forbidden', 'pay', accepted=False, status=409) == []
    assert record['accepted_action_sequence'] == 1
    with pytest.raises(ValueError, match='event_not_due'):
        module.record_event(record, 'payment-worker', evidence={'worker': 'not-run'})
    assert action(record, 'query') == ['payment-worker']
    with pytest.raises(ValueError, match='event_must_precede'):
        action(record, 'query-too-soon')
    module.record_event(record, 'payment-worker', evidence={'worker': 'actual-success'})
    assert action(record, 'next-query') == []
    with pytest.raises(ValueError, match='event_not_due'):
        module.record_event(record, 'payment-worker', evidence={'worker': 'duplicate'})


def test_event_requires_matching_action_at_frozen_sequence():
    record = freeze(events=[{'event_id': 'ticket-worker', 'after_accepted_action': 1, 'action': 'query'}])
    model_round(record, usage=receipt('request-1'))
    assert action(record, 'wrong', 'lock') == []
    assert action(record, 'late-query', 'query') == []
    with pytest.raises(ValueError, match='event_not_due'):
        module.record_event(record, 'ticket-worker', evidence={'worker': 'not-run'})


def test_http_rejection_cannot_be_labelled_accepted():
    record = freeze()
    with pytest.raises(ValueError, match='rejected_http'):
        action(record, accepted=True, status=409, suggestion=None)
    assert record['accepted_action_sequence'] == 0
    assert record['transitions'] == []


@pytest.mark.parametrize('bad_value', [True, 0, -1, 1.0, '1'])
def test_event_sequence_is_positive_integer_not_bool(bad_value):
    with pytest.raises(ValueError, match='event_trigger'):
        freeze(events=[{'event_id': 'worker', 'after_accepted_action': bad_value, 'action': 'query'}])


@pytest.mark.parametrize('bad_value', [0, 1, 'unknown', [], {}])
def test_started_requires_explicit_tristate(bad_value):
    with pytest.raises(ValueError, match='started_tristate'):
        model_round(freeze(), started=bad_value, suggestion=None)


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '-0.1', 'oops', 0])
def test_bad_settled_cost_does_not_enter_record(amount):
    record = freeze()
    usage = {'reservation_ref': 'request', 'state': 'settled', 'accounted_cny': amount}
    with pytest.raises(ValueError, match='usage_amount'):
        model_round(record, usage=usage)
    assert record['rounds'] == []


def test_conflicting_freeze_or_receipt_association_is_rejected():
    first, second = freeze(), freeze()
    second['freeze']['host_only']['business_time'] = 'changed'
    second['freeze_sha256'] = module.digest(second['freeze'])
    with pytest.raises(ValueError, match='summary_freeze_conflict'):
        module.summarize([first, second])
    first, second = freeze(task_ref='first'), freeze(task_ref='second')
    model_round(first, usage=receipt('same-request'))
    usage = receipt('same-request')
    usage['accounted_cny'] = '0.002'
    model_round(second, usage=usage)
    with pytest.raises(ValueError, match='summary_usage_conflict'):
        module.summarize([first, second])


def test_frozen_initial_state_cannot_change_after_first_record():
    record = freeze()
    record['freeze']['host_only']['objects'].append('other-users-trip')
    with pytest.raises(ValueError, match='frozen_task_changed'):
        model_round(record)
    assert record['rounds'] == []


def test_record_keeps_advice_action_terminal_and_human_attribution_in_separate_fields():
    record = freeze()
    model_round(record, usage=receipt('request-1'))
    action(record)
    terminal(record)
    row = record['transitions'][0]
    assert row['suggestion_ref'] == 'round-1'
    assert row['actual_original_api_or_web'] is True
    assert 'independent_sql_sha256' not in row['original_action_evidence']
    assert 'real_model_suggestion' not in record['terminals'][0]['evidence']
    assert 'host_only' not in record['rounds'][0]
    with pytest.raises(ValueError, match='independent_terminal_method'):
        module.record_terminal(record, passed=True, evidence={'model_says': 'done'},
                               method='model_self_report')


def test_duplicate_identity_and_invalid_reference_do_not_create_extra_actions():
    record = freeze()
    model_round(record, usage=receipt('request-1'))
    action(record)
    with pytest.raises(ValueError, match='action_identity'):
        action(record)
    with pytest.raises(ValueError, match='suggestion_reference'):
        action(record, 'other', suggestion='not-recorded')
    assert len(record['transitions']) == 1



def test_no_suggestion_or_no_original_action_cannot_prove_completable_model_success():
    record = freeze()
    model_round(record, suggestion=None, usage=receipt('request-1'))
    terminal(record)
    assert module.summarize([record])['tasks'][0]['model_faithful_complete'] is False
    another = freeze()
    model_round(another, usage=receipt('request-2'))
    terminal(another)
    assert module.summarize([another])['tasks'][0]['model_faithful_complete'] is False


def test_terminal_must_follow_last_action_and_actual_scheduled_event():
    record = freeze(events=[{'event_id': 'worker', 'after_accepted_action': 1, 'action': 'query'}])
    model_round(record, usage=receipt('request-1'))
    assert action(record) == ['worker']
    terminal(record)  # 事件尚未实际推进，不能给持续规划轨迹盖章。
    module.record_event(record, 'worker', evidence={'worker': 'actual-success'})
    assert module.summarize([record])['tasks'][0]['model_faithful_complete'] is False
    terminal(record)
    assert module.summarize([record])['tasks'][0]['model_faithful_complete'] is True
    action(record, 'later-query')
    assert module.summarize([record])['tasks'][0]['model_faithful_complete'] is False


def test_skipped_frozen_worker_event_cannot_be_fixed_by_unrelated_terminal_boolean():
    record = freeze(events=[{'event_id': 'worker', 'after_accepted_action': 1, 'action': 'query'}])
    model_round(record, usage=receipt('request-1'))
    action(record, name='different-action')
    terminal(record)
    assert module.summarize([record])['tasks'][0]['model_faithful_complete'] is False
