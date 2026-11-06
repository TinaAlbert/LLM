"""V1 虚拟轮次事件：实际 PostgreSQL、无补 query、独立付款/票据判据。"""

import json

import psycopg
import pytest

from scripts.v1_task import CompletedTurnWorker, independent_terminal
from services.business import worker
from test_personal import fail_business_events, pay, ready, snapshot


pytestmark = pytest.mark.integration


def pending_payment(client, alice, create):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    assert payment['status'] == 'processing'
    return trip, payment


def test_completed_turns_reach_ticket_without_any_reconcile_query(
    client, alice, create, settings, sql, tmp_path,
):
    trip, payment = pending_payment(client, alice, create)
    clock = CompletedTurnWorker(settings.database_url, trip['id'], tmp_path)

    assert clock.advance('1-02')['processed'] == 'payment'
    assert sql('SELECT status FROM payments WHERE id=%s', (payment['id'],)) == [
        {'status': 'succeeded'}
    ]
    assert sql('SELECT status FROM ticket_tasks') == [{'status': 'pending'}]
    assert sql('SELECT count(*) AS n FROM tickets') == [{'n': 0}]
    checkpoint = json.loads((tmp_path / 'paid-before-ticket.json').read_text(encoding='utf-8'))
    assert checkpoint == {
        'payment_succeeded': True, 'ticket_count': 0, 'event_id': '1-02',
    }

    # 对应忠实等待完成后的下一事件；没有请求 reconcile 业务入口。
    assert clock.advance('1-03')['processed'] == 'ticket'
    terminal = independent_terminal(settings.database_url, trip['id'])
    assert terminal['valid_ticket'] and terminal['unique_payment']
    assert terminal['captured_minor'] == 110000
    assert sql("SELECT count(*) AS n FROM idempotency WHERE action='payment.reconciled'") == [
        {'n': 0}
    ]


def test_replayed_event_and_repeated_ticket_worker_do_not_duplicate_effects(
    client, alice, create, settings, sql, tmp_path,
):
    trip, payment = pending_payment(client, alice, create)
    clock = CompletedTurnWorker(settings.database_url, trip['id'], tmp_path)
    first = clock.advance('1-02')
    paid_snapshot = snapshot(sql)
    first_file = (tmp_path / 'worker-event-1-02.json').read_bytes()
    assert clock.advance('1-02') == first
    assert snapshot(sql) == paid_snapshot
    assert (tmp_path / 'worker-event-1-02.json').read_bytes() == first_file
    assert sql('SELECT count(*) AS n FROM tickets') == [{'n': 0}]

    second = clock.advance('1-03')
    issued_snapshot = snapshot(sql)
    assert clock.advance('1-03') == second
    task_id = sql('SELECT id FROM ticket_tasks')[0]['id']
    worker.process_ticket(settings.database_url, 'test', task_id, outcome='succeeded')
    worker.process_payment(settings.database_url, 'test', payment['id'], outcome='succeeded')
    assert clock.advance('1-04')['processed'] is None
    assert snapshot(sql) == issued_snapshot
    assert sql('SELECT count(*) AS n, sum(captured_minor) AS captured FROM provider_payments') == [
        {'n': 1, 'captured': 110000}
    ]
    assert sql('SELECT count(*) AS n FROM tickets') == [{'n': 1}]


def test_event_for_another_trip_cannot_advance_existing_payment(
    client, alice, create, settings, sql, tmp_path,
):
    _, payment = pending_payment(client, alice, create)
    other = create(key='other-trip')
    clock = CompletedTurnWorker(settings.database_url, other['id'], tmp_path)
    before = snapshot(sql)
    assert clock.advance('1-02')['processed'] is None
    assert snapshot(sql) == before
    assert sql('SELECT status FROM payments WHERE id=%s', (payment['id'],)) == [
        {'status': 'processing'}
    ]
    assert not (tmp_path / 'paid-before-ticket.json').exists()


@pytest.mark.parametrize('phase', ['payment', 'ticket'])
def test_worker_database_failure_does_not_record_successful_event(
    client, alice, create, settings, sql, tmp_path, phase,
):
    trip, _ = pending_payment(client, alice, create)
    clock = CompletedTurnWorker(settings.database_url, trip['id'], tmp_path)
    if phase == 'ticket':
        clock.advance('1-02')
    event_id = '1-02' if phase == 'payment' else '1-03'
    before = snapshot(sql)
    with fail_business_events(settings):
        with pytest.raises(psycopg.Error):
            clock.advance(event_id)
    assert snapshot(sql) == before
    assert event_id not in clock.events
    assert not (tmp_path / f'worker-event-{event_id}.json').exists()
    assert sql('SELECT count(*) AS n FROM tickets') == [{'n': 0}]
    if phase == 'payment':
        assert not (tmp_path / 'paid-before-ticket.json').exists()
