"""P21双对象真实PG/ASGI与独立SQL；只允许round20已核验的55433合成实例。

此文件不加载tests/business的默认reset夹具。模型仅替换推理运输，所有
授权、投影、状态变化、交付和回执走正式实现。由根任务串行运行隔离runner。
"""
from decimal import Decimal
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from services.business.app import create_app
from services.business.manage import reset_test_database
from services.business import worker
from services.guide.assistant import current_state, display_state, identity, validate_reply
from services.guide.model import ModelResult, TokenUsage
from services.guide.releases import Releases
from test_corporate20 import sandbox_settings, login, request, snapshot, write
from test_shared_prompt21 import knowledge, observation
from services.business.db import connect


@pytest.fixture
def shared_settings(sandbox_settings):
    reset_test_database(sandbox_settings.database_url, confirmation='llm_business_test',
                        fixture='two-trips-one-coupon')
    return sandbox_settings


@pytest.fixture
def client21(shared_settings):
    with TestClient(create_app(shared_settings)) as client:
        yield client


@pytest.fixture
def sql21(shared_settings):
    def query(statement, parameters=()):
        with connect(shared_settings.database_url) as conn:
            return conn.execute(statement, parameters).fetchall()
    return query


def pair(client, *, quoted=True):
    headers = login(client, 'personal_alice')
    write(client, headers, '/payment-profile', {'source_ref': 'demo_personal_alice', 'expected_revision': 0}, method='put')
    trips = [write(client, headers, '/trips', {'flight_id': 'DEMO-' + name,
             'travel_date': '2026-10-01', 'passengers': 1}) for name in ('LARGE', 'SMALL')]
    quotes = [write(client, headers, f"/trips/{trip['id']}/quotes", {'expected_revision': 1})
              for trip in trips] if quoted else []
    return SimpleNamespace(headers=headers, trips=trips, quotes=quotes)


def capture(settings, ctx, identifiers=None):
    import time
    source = request(ctx.headers)
    deadline = time.monotonic() + 5
    who = identity(settings, source, deadline)
    identifiers = identifiers or [row['id'] for row in ctx.trips]
    return current_state(settings, source, who, identifiers[0], deadline, identifiers)


def anonymous(state):
    import re
    text = json.dumps(state, ensure_ascii=False)
    assert not re.search(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', text)
    for name in ('personal_alice', 'personal_bob', 'source_ref', 'certificate', 'lock_id', 'coupon_id', 'owner_id'):
        assert name not in text


def chat(ctx, sid=None):
    result = {'message': '两个行程如何安排？共享券当前属于谁？', 'page': 'trips',
              'trip_id': ctx.trips[0]['id'], 'delegated_trip_ids': [row['id'] for row in ctx.trips],
              'observation': observation('trips')}
    if sid: result['session_id'] = sid
    return result


def install(client, tmp_path, *, navigate=False):
    records = SimpleNamespace(calls=[], after=None)
    assistant = client.app.state.assistant
    assistant.releases = Releases('test', tmp_path / 'releases.json')
    candidate = assistant.releases.save_candidate(knowledge(), [observation('profile'), observation('trips')],
                                                  prompt_version='assistant-6')
    assistant.releases.publish(candidate['version'])
    assistant.report_dir = tmp_path / 'runs'
    assistant.external_model = False
    def factory(validator):
        async def generate(value, deadline):
            validator(value)
            records.calls.append(value)
            if records.after: records.after()
            content = '两个委托行程须分别核对报价、锁价和共享券状态。'
            calls = [{'id': 'offline-navigation', 'name': 'navigate', 'arguments': {'page': 'profile'}}] if navigate else []
            return ModelResult({'content': content, 'tool_calls': calls},
                TokenUsage(100, 20, 120, 0, 100), 'offline-round21', Decimal('0'), 'offline-round21')
        return SimpleNamespace(generate_experiment=generate)
    assistant.adapter_factory = factory
    return records


def test_explicit_two_owned_objects_unknowns_and_read_only(client21, shared_settings, sql21):
    ctx = pair(client21, quoted=False)
    before = snapshot(sql21)
    state, _ = capture(shared_settings, ctx)
    assert state['contract_version'] == 'assistant-shared-1'
    assert [row['ref'] for row in state['delegated_trips']] == ['trip_1', 'trip_2']
    assert all(row['unknowns'] == ['current_quote_amount'] for row in state['delegated_trips'])
    assert state['shared_coupon']['status'] == 'available'
    assert state['shared_coupon']['trip_ref'] is None
    assert state['unknowns'] == ['unexecuted_order_outcomes']
    anonymous(state)
    assert snapshot(sql21) == before


def test_quote_estimates_do_not_allocate_shared_coupon(client21, shared_settings, sql21):
    ctx = pair(client21)
    before = snapshot(sql21)
    state, _ = capture(shared_settings, ctx)
    assert [row['data']['quote']['amount_minor'] for row in state['delegated_trips']] == [80000, 48000]
    assert all(row['data']['quote']['coupon_allocation'] == 'estimate' for row in state['delegated_trips'])
    assert state['shared_coupon']['status'] == 'available'
    assert sql21('SELECT status,lock_id FROM coupons') == [{'status': 'available', 'lock_id': None}]
    shown = display_state(state, 'assistant-6')
    assert 'amount_minor' not in json.dumps(shown)
    for answer in ('1400', '1480', '140000', '148000'):
        assert answer not in json.dumps(shown)
    anonymous(shown)
    assert snapshot(sql21) == before


def test_actual_lock_changes_attribution_other_branch_price_and_cancel_releases(client21, shared_settings, sql21):
    ctx = pair(client21)
    small = write(client21, ctx.headers, f"/quotes/{ctx.quotes[1]['id']}/lock")
    state, first_hash = capture(shared_settings, ctx)
    assert state['shared_coupon']['status'] == 'held' and state['shared_coupon']['trip_ref'] == 'trip_2'
    assert state['delegated_trips'][0]['data']['quote']['coupon_allocation'] == 'estimate'
    large = write(client21, ctx.headers, f"/quotes/{ctx.quotes[0]['id']}/lock")
    second, second_hash = capture(shared_settings, ctx)
    assert second_hash != first_hash
    assert second['delegated_trips'][0]['data']['lock']['amount_minor'] == 100000
    assert second['delegated_trips'][0]['data']['lock']['coupon_allocation'] == 'not_applied'
    assert second['delegated_trips'][1]['data']['lock']['amount_minor'] == 48000
    assert sql21('SELECT c.status,l.trip_id FROM coupons c JOIN booking_locks l ON l.id=c.lock_id') == [
        {'status': 'held', 'trip_id': UUID(ctx.trips[1]['id'])}]
    write(client21, ctx.headers, f"/locks/{small['id']}/cancel")
    released, _ = capture(shared_settings, ctx)
    assert released['shared_coupon']['status'] == 'available' and released['shared_coupon']['trip_ref'] is None
    assert released['delegated_trips'][1]['data']['lock']['coupon_allocation'] == 'released'
    assert sql21('SELECT id,reserved,sold FROM flights WHERE travel_date=%s ORDER BY id', ('2026-10-01',)) == [
        {'id': 'DEMO-LARGE', 'reserved': 1, 'sold': 0}, {'id': 'DEMO-SMALL', 'reserved': 0, 'sold': 0}]
    assert large['coupon_id'] is None


def test_consumed_coupon_and_paid_without_ticket_cannot_rollback_or_complete(client21, shared_settings, sql21):
    ctx = pair(client21)
    locked = write(client21, ctx.headers, f"/quotes/{ctx.quotes[0]['id']}/lock")
    payment = write(client21, ctx.headers, f"/locks/{locked['id']}/payments", {'amount_minor': locked['amount_minor']})
    worker.process_payment(shared_settings.database_url, 'test', payment['id'])
    before = snapshot(sql21)
    state, _ = capture(shared_settings, ctx)
    assert state['shared_coupon']['status'] == 'consumed' and state['shared_coupon']['trip_ref'] == 'trip_1'
    first = state['delegated_trips'][0]['data']
    assert first['payment']['status'] == 'succeeded' and first['ticket'] is None
    assert first['lock']['coupon_allocation'] == 'consumed' and first['lock']['valid'] is False
    assert display_state(state, 'assistant-6')['delegated_trips'][0]['data']['payment']['amount_display'] == '800.00 模拟元'
    with pytest.raises(ValueError): validate_reply('两个行程已出票。', state, prompt_version='assistant-6')
    denied = client21.post(f"/locks/{locked['id']}/cancel", headers={**ctx.headers, 'Idempotency-Key': 'cancel-consumed'}, json={})
    assert denied.status_code == 409
    assert sql21('SELECT captured_minor FROM provider_payments') == [{'captured_minor': 80000}]
    assert sql21('SELECT count(*) AS n FROM ticket_tasks') == [{'n': 1}]
    assert sql21('SELECT count(*) AS n FROM tickets') == [{'n': 0}]
    assert snapshot(sql21) == before


def test_expired_lock_does_not_make_raw_coupon_available(client21, shared_settings, sql21):
    ctx = pair(client21)
    write(client21, ctx.headers, f"/quotes/{ctx.quotes[0]['id']}/lock")
    sql21("UPDATE workspaces SET business_time=business_time+interval '301 seconds' RETURNING id")
    before = snapshot(sql21)
    state, _ = capture(shared_settings, ctx)
    assert state['shared_coupon']['status'] == 'held'
    assert state['delegated_trips'][0]['data']['lock']['effective_status'] == 'expired'
    assert state['delegated_trips'][0]['data']['lock']['coupon_allocation'] == 'held'
    assert state['delegated_trips'][0]['unknowns'] == ['current_quote_amount']
    assert snapshot(sql21) == before


def test_other_or_missing_second_object_same_error_and_no_model_call(client21, shared_settings, sql21, tmp_path):
    ctx = pair(client21)
    bob = login(client21, 'personal_bob')
    other = write(client21, bob, '/trips', {'flight_id': 'DEMO-LARGE', 'travel_date': '2026-10-01', 'passengers': 1})
    records = install(client21, tmp_path)
    before = snapshot(sql21)
    for identifier in (other['id'], str(uuid4())):
        response = client21.post('/assistant/chat', headers=ctx.headers, json={
            **chat(ctx), 'delegated_trip_ids': [ctx.trips[0]['id'], identifier]})
        assert response.status_code == 404 and response.json() == {'error': 'not_visible'}
    assert records.calls == [] and snapshot(sql21) == before


def test_undelegated_third_trip_holder_is_unknown_and_never_scanned(client21, shared_settings, sql21):
    ctx = pair(client21)
    third = write(client21, ctx.headers, '/trips', {'flight_id': 'DEMO-LARGE', 'travel_date': '2026-10-01', 'passengers': 2})
    quote = write(client21, ctx.headers, f"/trips/{third['id']}/quotes", {'expected_revision': 1})
    write(client21, ctx.headers, f"/quotes/{quote['id']}/lock")
    before = snapshot(sql21)
    state, _ = capture(shared_settings, ctx)
    assert state['shared_coupon']['status'] == 'held'
    assert state['shared_coupon']['attribution'] == 'unknown' and state['shared_coupon']['trip_ref'] is None
    assert len(state['delegated_trips']) == 2 and third['id'] not in json.dumps(state)
    assert all(row['data']['trip']['passengers'] == 1 for row in state['delegated_trips'])
    assert snapshot(sql21) == before


@pytest.mark.parametrize('change', ['source_owner', 'eligibility', 'other_inventory', 'other_price'])
def test_private_changes_without_revision_or_public_changes_invalidate_hash(change, client21, shared_settings, sql21):
    ctx = pair(client21)
    original, old_hash = capture(shared_settings, ctx)
    statements = {
        'source_owner': "UPDATE payment_sources SET owner_id='personal_bob' WHERE ref='demo_personal_alice' RETURNING ref",
        'eligibility': "UPDATE payment_sources SET eligible=false WHERE ref='demo_personal_alice' RETURNING ref",
        'other_inventory': "UPDATE flights SET reserved=reserved+1 WHERE id='DEMO-SMALL' AND travel_date='2026-10-01' RETURNING id",
        'other_price': "UPDATE flights SET base_minor=base_minor+1 WHERE id='DEMO-SMALL' AND travel_date='2026-10-01' RETURNING id",
    }
    assert sql21(statements[change])
    before = snapshot(sql21)
    fresh, new_hash = capture(shared_settings, ctx)
    for row in (original, fresh):
        row.pop('observed_at'); row.pop('valid_until')
    assert original == fresh and new_hash != old_hash
    assert snapshot(sql21) == before


@pytest.mark.parametrize('change', ['other_trip', 'coupon', 'inventory', 'session'])
def test_late_offline_response_rechecks_other_object_resources_and_identity(change, client21, shared_settings, sql21, tmp_path):
    ctx = pair(client21)
    records = install(client21, tmp_path)
    if change == 'other_trip':
        records.after = lambda: sql21('UPDATE trips SET revision=revision+1 WHERE id=%s RETURNING id', (ctx.trips[1]['id'],))
    if change == 'coupon': records.after = lambda: sql21('UPDATE coupons SET cap_minor=cap_minor+1 RETURNING id')
    if change == 'inventory': records.after = lambda: sql21("UPDATE flights SET capacity=capacity+1 WHERE id='DEMO-SMALL' RETURNING id")
    if change == 'session': records.after = lambda: sql21("UPDATE sessions SET revoked=true WHERE user_id='personal_alice' RETURNING user_id")
    response = client21.post('/assistant/chat', headers=ctx.headers, json=chat(ctx))
    assert response.status_code in {401, 409} and 'reply' not in response.json()
    assert len(records.calls) == 1
    assert all(not session.history for session in client21.app.state.assistant.sessions.values())


def test_other_trip_revision_clears_same_identity_history(client21, shared_settings, sql21, tmp_path):
    ctx = pair(client21)
    records = install(client21, tmp_path)
    first = client21.post('/assistant/chat', headers=ctx.headers, json=chat(ctx))
    assert first.status_code == 200, first.text
    write(client21, ctx.headers, f"/trips/{ctx.trips[1]['id']}", {'expected_revision': 1, 'passengers': 2}, method='patch')
    before = snapshot(sql21)
    second = client21.post('/assistant/chat', headers=ctx.headers, json=chat(ctx, first.json()['session_id']))
    assert second.status_code == 200, second.text
    context = json.loads(records.calls[-1]['messages'][-1]['content'])
    assert context['history'] == []
    assert context['current_state']['delegated_trips'][1]['data']['trip']['revision'] == 2
    assert snapshot(sql21) == before


def test_navigation_receipt_rechecks_both_explicit_objects(client21, shared_settings, sql21, tmp_path):
    ctx = pair(client21)
    install(client21, tmp_path, navigate=True)
    first = client21.post('/assistant/chat', headers=ctx.headers, json=chat(ctx))
    assert first.status_code == 200, first.text
    write(client21, ctx.headers, f"/quotes/{ctx.quotes[1]['id']}/lock")
    before = snapshot(sql21)
    response = client21.post('/assistant/ack', headers=ctx.headers, json={
        'session_id': first.json()['session_id'], 'nonce': first.json()['tool']['nonce'],
        'success': True, 'observation': observation('profile')})
    assert response.status_code == 409 and response.json() == {'error': 'state_changed'}
    assert snapshot(sql21) == before
