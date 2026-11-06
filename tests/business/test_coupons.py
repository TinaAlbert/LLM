"""C03/C04/C09/C11：真实PG券归属、顺序终态、恢复和并发。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.business import personal
from services.business.db import connect
from services.business.manage import seed
from test_personal import (advance, assert_error, bind, expire,
                           lock, pay, process_payment, process_ticket, quote,
                           snapshot as personal_snapshot, synchronize_connections, write)

pytestmark = [pytest.mark.integration, pytest.mark.coupon]


@contextmanager
def fail_coupon_events(settings):
    # 只在券已转移后的事件写入失败，不能在更早的provider事件失败冒充券回滚。
    with connect(settings.database_url) as conn:
        conn.execute("""CREATE FUNCTION test_reject_coupon() RETURNS trigger LANGUAGE plpgsql
            AS $$ BEGIN RAISE EXCEPTION 'coupon event fault'; END $$""")
        conn.execute("""CREATE TRIGGER test_reject_coupon BEFORE INSERT ON business_events
            FOR EACH ROW WHEN (NEW.object_type='coupon') EXECUTE FUNCTION test_reject_coupon()""")
    try:
        yield
    finally:
        with connect(settings.database_url) as conn:
            conn.execute('DROP TRIGGER IF EXISTS test_reject_coupon ON business_events')
            conn.execute('DROP FUNCTION IF EXISTS test_reject_coupon()')


def setup_pair(client, alice, create):
    bind(client, alice)
    trips = [create('trip-' + size, parameters={"flight_id": 'DEMO-' + size,
             "travel_date": "2026-10-01", "passengers": 1}) for size in ('LARGE', 'SMALL')]
    quotes = [quote(client, alice, trip, key='quote-' + str(i)) for i, trip in enumerate(trips)]
    return trips, quotes


def state(sql):
    result = personal_snapshot(sql)
    result['coupons'] = sql('SELECT * FROM coupons ORDER BY id')
    return result


def coupon_state(sql, expected):
    rows = sql('SELECT status,lock_id FROM coupons')
    assert len(rows) == 1 and rows[0]['status'] == expected
    assert (rows[0]['lock_id'] is None) == (expected == 'available')
    assert sql("""SELECT c.id FROM coupons c WHERE
        (SELECT count(*) FROM booking_locks l WHERE l.coupon_id=c.id
           AND l.status IN ('held','protected','consumed')) <> CASE WHEN c.status='available' THEN 0 ELSE 1 END""") == []
    assert sql("""SELECT f.id FROM flights f WHERE f.reserved <>
        (SELECT coalesce(sum(l.quantity),0) FROM booking_locks l WHERE l.flight_id=f.id
         AND l.travel_date=f.travel_date AND l.status IN ('held','protected')) OR f.sold <>
        (SELECT coalesce(sum(l.quantity),0) FROM booking_locks l WHERE l.flight_id=f.id
         AND l.travel_date=f.travel_date AND l.status='consumed')""") == []


def finish(client, alice, settings, booking, key):
    payment = pay(client, alice, booking, key=key, amount=booking['amount_minor'])
    settled = process_payment(settings, payment)
    task = process_ticket(settings, settled['ticket_task_id'])
    assert task['status'] == 'succeeded'
    return payment


def terminal(sql, total):
    rows = sql("""SELECT t.revision,k.trip_revision,l.trip_revision AS locked_revision,
        l.status AS lock_status,p.status AS payment_status,j.status AS job_status,
        task.status AS task_status,pp.outcome,pp.captured_minor,p.amount_minor,
        (SELECT count(*) FROM payments x WHERE x.trip_id=t.id) AS payments,
        (SELECT count(*) FROM ticket_tasks x WHERE x.payment_id=p.id) AS tasks,
        (SELECT count(*) FROM tickets x WHERE x.payment_id=p.id) AS tickets
        FROM trips t JOIN tickets k ON k.trip_id=t.id JOIN payments p ON p.id=k.payment_id
        JOIN booking_locks l ON l.id=p.lock_id JOIN payment_jobs j ON j.payment_id=p.id
        JOIN ticket_tasks task ON task.id=k.task_id JOIN provider_payments pp ON pp.operation_id=p.id""")
    assert len(rows) == 2
    assert sum(row['captured_minor'] for row in rows) == total
    for row in rows:
        assert row['revision'] == row['trip_revision'] == row['locked_revision'] == 1
        assert row['lock_status'] == 'consumed'
        assert row['payment_status'] == row['task_status'] == row['outcome'] == 'succeeded'
        assert row['job_status'] == 'done'
        assert row['payments'] == row['tasks'] == row['tickets'] == 1
        assert row['captured_minor'] == row['amount_minor']
    assert sql('SELECT count(*) AS n FROM tickets') == [{'n': 2}]
    assert sql("SELECT count(*) AS n FROM business_events WHERE action='coupon.consumed'") == [{'n': 1}]
    coupon_state(sql, 'consumed')
    assert sql('SELECT id,reserved,sold FROM flights ORDER BY id') == [
        {'id': 'DEMO-LARGE', 'reserved': 0, 'sold': 1}, {'id': 'DEMO-SMALL', 'reserved': 0, 'sold': 1}]


@pytest.mark.parametrize('order,amounts,total', [((0, 1), (80000, 60000), 140000), ((1, 0), (48000, 100000), 148000)])
def test_c03_two_legal_orders_have_distinct_independent_terminals(order, amounts, total, client, alice, create, sql, settings):
    trips, quotes = setup_pair(client, alice, create)
    assert [q['amount_minor'] for q in quotes] == [80000, 48000]
    assert [q['base_amount_minor'] for q in quotes] == [100000, 60000]
    assert all(q['price_basis'] == 'estimate' for q in quotes)
    coupon_state(sql, 'available')
    bookings = [lock(client, alice, quotes[i], key='lock-' + str(i)) for i in order]
    assert tuple(b['amount_minor'] for b in bookings) == amounts
    assert bookings[0]['coupon_id'] and bookings[1]['coupon_id'] is None
    assert all(b['price_basis'] == 'frozen' for b in bookings)
    for i, booking in enumerate(bookings):
        finish(client, alice, settings, booking, 'pay-' + str(i))
    terminal(sql, total)
    before = state(sql)
    seed(settings.database_url, settings.workspace_id, fixture='two-trips-one-coupon')
    assert state(sql) == before


def test_cancel_small_then_reorder_uses_released_coupon_once(client, alice, create, sql, settings):
    trips, quotes = setup_pair(client, alice, create)
    small = lock(client, alice, quotes[1])
    write(client, alice, f"/locks/{small['id']}/cancel")
    write(client, alice, f"/locks/{small['id']}/cancel")
    coupon_state(sql, 'available')
    for i in (0, 1):
        booking = lock(client, alice, quotes[i], key='relock-' + str(i))
        finish(client, alice, settings, booking, 'pay-' + str(i))
    terminal(sql, 140000)


@pytest.mark.parametrize('reason', ['time', 'inventory'])
def test_reorder_requires_current_inventory_and_quote_deadline(reason, client, alice, bob, create, sql, settings):
    trips, quotes = setup_pair(client, alice, create)
    small = lock(client, alice, quotes[1])
    write(client, alice, f"/locks/{small['id']}/cancel")
    if reason == 'time':
        advance(settings, 300)
    else:
        bind(client, bob, source='demo_personal_bob')
        other = create('other', headers=bob, parameters={"flight_id": "DEMO-LARGE", "travel_date": "2026-10-01", "passengers": 2})
        lock(client, bob, quote(client, bob, other), key='bob-lock')
    before = state(sql)
    response = client.post(f"/quotes/{quotes[0]['id']}/lock", headers={**alice, 'Idempotency-Key': 'retry'}, json={})
    assert_error(response, 409, 'quote_expired' if reason == 'time' else 'inventory_unavailable')
    assert state(sql) == before
    coupon_state(sql, 'available')


@pytest.mark.parametrize('change', ['date', 'profile'])
def test_legal_change_releases_coupon_and_stale_lock_cannot_pay(change, client, alice, create, sql):
    trips, quotes = setup_pair(client, alice, create)
    booking = lock(client, alice, quotes[0])
    if change == 'date':
        write(client, alice, f"/trips/{trips[0]['id']}", {'expected_revision': 1, 'travel_date': '2026-10-02'}, method='patch')
    else:
        bind(client, alice, source='demo_personal_alice_alt', revision=1, key='change')
    coupon_state(sql, 'available')
    before = state(sql)
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, 'Idempotency-Key': 'old'}, json={'amount_minor': 80000})
    assert_error(response, 409, 'state_changed')
    assert state(sql) == before


def test_exact_300_does_not_mutate_on_read_then_releases_once(client, alice, create, sql, settings):
    trips, quotes = setup_pair(client, alice, create)
    booking = lock(client, alice, quotes[0])
    advance(settings, 300)
    before = state(sql)
    assert client.get(f"/locks/{booking['id']}", headers=alice).json()['valid'] is False
    response = client.post(f"/locks/{booking['id']}/payments", headers={**alice, 'Idempotency-Key': 'late'}, json={'amount_minor': 80000})
    assert_error(response, 409, 'quote_expired')
    assert state(sql) == before
    coupon_state(sql, 'held')
    expire(settings)
    coupon_state(sql, 'available')
    after = state(sql)
    expire(settings)
    assert state(sql) == after


@pytest.mark.parametrize('outcome', ['unknown', 'lost_response', 'failed', 'succeeded'])
def test_299_acceptance_protection_and_original_operation_recovery(outcome, client, alice, create, sql, settings):
    trips, quotes = setup_pair(client, alice, create)
    booking = lock(client, alice, quotes[0])
    advance(settings, 299)
    payment = pay(client, alice, booking, amount=80000)
    advance(settings, 2)
    expire(settings)
    coupon_state(sql, 'protected')
    process_payment(settings, payment, outcome)
    if outcome in ('unknown', 'lost_response'):
        coupon_state(sql, 'protected')
        before = state(sql)
        for path, body, method in [
            (f"/locks/{booking['id']}/cancel", {}, 'post'),
            (f"/trips/{trips[0]['id']}", {'expected_revision': 1, 'passengers': 2}, 'patch'),
            ('/payment-profile', {'source_ref': 'demo_personal_alice_alt', 'expected_revision': 1}, 'put'),
            (f"/locks/{booking['id']}/payments", {'amount_minor': 80000}, 'post')]:
            response = getattr(client, method)(path, headers={**alice, 'Idempotency-Key': str(uuid4())}, json=body)
            assert response.status_code == 409
        assert state(sql) == before
        reconciled = write(client, alice, f"/payments/{payment['id']}/reconcile")
        if outcome == 'unknown':
            assert reconciled['status'] == 'unknown'
            coupon_state(sql, 'protected')
            process_payment(settings, payment)
    coupon_state(sql, 'available' if outcome == 'failed' else 'consumed')
    assert sql('SELECT count(*) AS n,sum(captured_minor) AS amount FROM provider_payments') == [
        {'n': 1, 'amount': 0 if outcome == 'failed' else 80000}]
    before = state(sql)
    process_payment(settings, payment)
    assert state(sql) == before
    if outcome == 'failed':
        fresh = quote(client, alice, trips[1], key='fresh')
        assert lock(client, alice, fresh, key='next')['amount_minor'] == 48000
    else:
        task = sql('SELECT id FROM ticket_tasks')[0]['id']
        process_ticket(settings, task, 'failed')
        assert sql('SELECT count(*) AS n FROM tickets') == [{'n': 0}]
        coupon_state(sql, 'consumed')
        process_ticket(settings, task)
        assert sql('SELECT count(*) AS n FROM tickets') == [{'n': 0}]


@pytest.mark.parametrize('stage', ['hold', 'cancel', 'pay', 'settle', 'expire'])
def test_event_failure_rolls_back_coupon_with_other_resources(stage, client, alice, create, sql, settings):
    trips, quotes = setup_pair(client, alice, create)
    booking = lock(client, alice, quotes[0]) if stage != 'hold' else None
    payment = pay(client, alice, booking, amount=80000) if stage == 'settle' else None
    if stage == 'expire':
        advance(settings, 300)
    before = state(sql)
    with fail_coupon_events(settings):
        if stage in ('settle', 'expire'):
            with pytest.raises(psycopg.Error):
                process_payment(settings, payment) if stage == 'settle' else expire(settings)
        else:
            path = (f"/quotes/{quotes[0]['id']}/lock" if stage == 'hold' else f"/locks/{booking['id']}/" + ('payments' if stage == 'pay' else 'cancel'))
            response = client.post(path, headers={**alice, 'Idempotency-Key': 'fault'}, json={'amount_minor': 80000} if stage == 'pay' else {})
            assert_error(response, 503, 'storage_unavailable')
    assert state(sql) == before


def test_coupon_visibility_ownership_strict_input_and_replay(client, alice, bob, login, create, sql):
    trips, quotes = setup_pair(client, alice, create)
    coupon = client.get('/coupons', headers=alice).json()['coupons'][0]
    assert set(coupon) == {'id', 'percent', 'cap_minor', 'currency', 'status'}
    actors = [bob, login('employee_chen'), login('employee_fang'), login('approver_lin')]
    before = state(sql)
    for actor in actors:
        assert client.get('/coupons', headers=actor).json() == {'coupons': []}
        for identifier in (coupon['id'], str(uuid4()), 'invalid'):
            assert_error(client.get('/coupons/' + identifier, headers=actor), 404, 'not_visible')
        assert_error(client.post(f"/quotes/{quotes[0]['id']}/lock", headers={**actor, 'Idempotency-Key': 'steal'}, json={}), 404, 'not_visible')
    for extra in ({'coupon_id': coupon['id']}, {'owner_id': 'personal_alice'}, {'organization_id': 'org-A'}, {'discount_minor': 20000}):
        assert_error(client.post(f"/quotes/{quotes[0]['id']}/lock", headers={**alice, 'Idempotency-Key': 'inject'}, json=extra), 422, 'invalid_request')
    assert state(sql) == before
    booking = lock(client, alice, quotes[0], key='same')
    assert lock(client, alice, quotes[0], key='same') == booking
    assert_error(client.post(f"/quotes/{quotes[1]['id']}/lock", headers={**alice, 'Idempotency-Key': 'same'}, json={}), 409, 'idempotency_conflict')
    before = state(sql)
    assert_error(client.post(f"/quotes/{quotes[0]['id']}/lock", headers={**alice, 'Idempotency-Key': 'new'}, json={}), 409, 'active_lock_exists')
    assert state(sql) == before
    sql("UPDATE trips SET owner_id='personal_bob' WHERE id=%s RETURNING id", (trips[0]['id'],))
    before = state(sql)
    assert_error(client.post(f"/quotes/{quotes[0]['id']}/lock", headers={**alice, 'Idempotency-Key': 'same'}, json={}), 404, 'not_visible')
    assert state(sql) == before


@pytest.mark.parametrize('mode', ['two-trips', 'same-key', 'new-key'])
def test_concurrent_two_trips_compete_for_one_coupon(mode, app, client, alice, create, sql, monkeypatch):
    trips, quotes = setup_pair(client, alice, create)
    backends = synchronize_connections(monkeypatch, personal)
    def request(i):
        with TestClient(app) as other:
            target = i if mode == 'two-trips' else 0
            key = 'race' if mode == 'same-key' else 'race-' + str(i)
            return other.post(f"/quotes/{quotes[target]['id']}/lock", headers={**alice, 'Idempotency-Key': key}, json={})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, (0, 1)))
    assert len(backends) == 2
    if mode == 'two-trips':
        assert [r.status_code for r in results] == [201, 201]
        bookings = [r.json() for r in results]
        assert sum(b['coupon_id'] is not None for b in bookings) == 1
        assert sum(b['amount_minor'] for b in bookings) in (140000, 148000)
    elif mode == 'same-key':
        assert [r.status_code for r in results] == [201, 201]
        assert results[0].json() == results[1].json()
    else:
        assert sorted(r.status_code for r in results) == [201, 409]
    assert sql("SELECT count(*) AS n FROM business_events WHERE action='coupon.held'") == [{'n': 1}]
    coupon_state(sql, 'held')


def test_payment_requires_frozen_not_preview_amount_and_coupon_rules_stay_frozen(client, alice, create, sql, settings):
    trips, quotes = setup_pair(client, alice, create)
    large = lock(client, alice, quotes[0], key='large')
    small = lock(client, alice, quotes[1], key='small')
    before = state(sql)
    response = client.post(f"/locks/{small['id']}/payments", headers={**alice, 'Idempotency-Key': 'preview'},
                           json={'amount_minor': quotes[1]['amount_minor']})
    assert_error(response, 409, 'amount_mismatch')
    assert state(sql) == before
    sql('UPDATE coupons SET percent=10,cap_minor=5000 RETURNING id')
    assert client.get(f"/locks/{large['id']}", headers=alice).json()['amount_minor'] == 80000
    assert sql('SELECT coupon_percent,coupon_cap_minor,coupon_discount_minor FROM booking_locks WHERE id=%s',
               (large['id'],)) == [{'coupon_percent': 20, 'coupon_cap_minor': 20000, 'coupon_discount_minor': 20000}]
    finish(client, alice, settings, large, 'large-pay')
    finish(client, alice, settings, small, 'small-pay')
    terminal(sql, 140000)


@pytest.mark.parametrize('base,expected', [(60003, 12000), (200003, 20000)])
def test_discount_floors_minor_units_then_caps(base, expected, client, alice, create, sql):
    sql("UPDATE flights SET base_minor=%s WHERE id='DEMO-LARGE' RETURNING id", (base,))
    trips, quotes = setup_pair(client, alice, create)
    booking = lock(client, alice, quotes[0])
    assert booking['base_amount_minor'] == base
    assert booking['coupon_discount_minor'] == expected
    assert booking['amount_minor'] == base - expected


def test_c03_does_not_grant_enterprise_discount_or_mix_single_trip(client, login, sql, settings):
    chen = login('employee_chen')
    write(client, chen, '/payment-profile', {'source_ref': 'demo_corporate_chen', 'expected_revision': 0}, method='put')
    trip = write(client, chen, '/trips', {'flight_id': 'DEMO-LARGE', 'travel_date': '2026-10-01', 'passengers': 1})
    before = state(sql)
    response = client.post(f"/trips/{trip['id']}/quotes", headers={**chen, 'Idempotency-Key': 'quote'}, json={'expected_revision': 1})
    assert_error(response, 409, 'payment_profile_ineligible')
    with pytest.raises(ValueError):
        seed(settings.database_url, settings.workspace_id, fixture='single-trip')
    assert state(sql) == before
    assert sql("SELECT id FROM flights WHERE id='DEMO-PEK-SHA'") == []
