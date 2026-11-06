"""D1-05白名单读取：权限无变化、终态重读与独立PG并发快照。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient
from services.business import reads
from test_personal import ready, pay, process_payment, process_ticket, write, quote
from test_corporate import ready as corporate_ready, snapshot


def test_options_and_list_are_subject_scoped(client, alice, bob, create, login, sql):
    trip = create()
    before = snapshot(sql)
    options = client.get('/booking-options', headers=alice).json()
    assert {s['ref'] for s in options['sources']} == {'demo_personal_alice', 'demo_personal_alice_alt'}
    assert all(set(s) == {'ref', 'kind'} for s in options['sources'])
    assert all(set(f) == {'id', 'travel_date'} for f in options['flights'])
    assert [t['id'] for t in client.get('/trips', headers=alice).json()['trips']] == [trip['id']]
    assert client.get('/trips', headers=bob).json() == {'trips': []}
    for headers in (bob, login('approver_lin'), login('employee_ding'), login('employee_fang')):
        for identifier in (trip['id'], '00000000-0000-0000-0000-000000000001', 'bad'):
            response = client.get(f'/trips/{identifier}/booking', headers=headers)
            assert response.status_code == 404 and response.json() == {'error': 'not_visible'}
    lin = login('approver_lin')
    for path in ('/trips', '/booking-options'):
        assert client.get(path, headers=lin).status_code == 404
    for path in ('/trips', '/booking-options', f"/trips/{trip['id']}/booking"):
        assert client.get(path).status_code == 401
    assert snapshot(sql) == before


def test_personal_aggregate_recovers_independent_ticket(client, alice, create, settings, sql):
    trip, profile, quotation, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    result = process_payment(settings, payment)
    process_ticket(settings, result['ticket_task_id'])
    before = snapshot(sql)
    result = client.get(f"/trips/{trip['id']}/booking", headers=alice).json()
    assert set(result) == {'trip','quote','lock','payment','ticket_task','ticket','approval'}
    assert result['payment']['id'] == payment['id']
    assert result['payment']['status'] == 'succeeded'
    assert result['ticket_task']['status'] == 'succeeded'
    assert result['ticket']['status'] == 'valid'
    assert result['ticket']['trip_revision'] == result['lock']['trip_revision'] == result['trip']['revision'] == 1
    assert result['approval'] is None
    assert snapshot(sql) == before


def test_corporate_aggregate_authorizes_before_following_ids(client, login, sql):
    ctx = corporate_ready(client, login)
    before = snapshot(sql)
    path = f"/trips/{ctx['trip']['id']}/booking"
    result = client.get(path, headers=ctx['headers']).json()
    assert result['approval']['id'] == ctx['approved']['id']
    assert result['approval']['valid'] is True
    assert client.get(path, headers=ctx['approver']).status_code == 404
    sql("UPDATE memberships SET employee=false WHERE user_id='employee_chen' RETURNING user_id")
    for path in (path, '/trips', '/booking-options'):
        assert client.get(path, headers=ctx['headers']).status_code == 404
    assert snapshot(sql) == before


def test_aggregate_uses_creation_event_order(client, alice, create):
    trip, _, first, _ = ready(client, alice, create)
    write(client, alice, f"/trips/{trip['id']}", {'expected_revision':1,'passengers':2}, method='patch')
    second = quote(client, alice, trip, revision=2, key='second')
    result = client.get(f"/trips/{trip['id']}/booking", headers=alice).json()
    assert first['expires_at'] == second['expires_at']
    assert result['quote']['id'] == second['id']
    assert result['lock']['valid'] is False


def test_aggregate_is_one_pg_snapshot_during_mutation(client, app, alice, create, monkeypatch):
    trip, _, quotation, booking = ready(client, alice, create)
    entered, release = Event(), Event()
    original = reads.owned_trip
    def paused(*args, **kwargs):
        row = original(*args, **kwargs)
        entered.set()
        assert release.wait(20)
        return row
    monkeypatch.setattr(reads, 'owned_trip', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def read():
            with TestClient(app) as another:
                return another.get(f"/trips/{trip['id']}/booking", headers=alice)
        future = pool.submit(read)
        try:
            assert entered.wait(15)
            write(client, alice, f"/trips/{trip['id']}", {'expected_revision':1,'passengers':2}, method='patch')
            newer = quote(client, alice, trip, revision=2, key='newer')
        finally:
            release.set()
        result = future.result(timeout=20).json()
    assert result['trip']['revision'] == result['quote']['trip_revision'] == result['lock']['trip_revision'] == 1
    assert result['quote']['id'] == quotation['id'] and result['lock']['valid']
    result = client.get(f"/trips/{trip['id']}/booking", headers=alice).json()
    assert result['trip']['revision'] == 2 and result['quote']['id'] == newer['id']
    assert not result['lock']['valid']
