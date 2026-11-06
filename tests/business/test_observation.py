"""D2受控投影的真实PG检查；不把这些宿主测试算作进程隔离证据。"""
from datetime import datetime, timedelta, timezone
from copy import deepcopy

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from services.business import observation as obs
from test_corporate import ready as corporate_ready
from test_personal import ready, advance, write, pay, process_payment, process_ticket


def request(headers):
    return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


def selection(identifier, kind="trip", **extra):
    return obs.Selection(kind=kind, object_id=identifier, **extra)


def capture(settings, headers, selected):
    return obs.capture(settings, request(headers), selected)


def unchanged(sql):
    tables=('users','organizations','departments','memberships','approval_assignments','payment_sources',
            'trips','payment_profiles','quotes','booking_locks','payments','payment_jobs','provider_payments',
            'ticket_tasks','tickets','flights','budget_projects','trip_budget_selections','approval_requests',
            'business_events','idempotency','coupons','workspaces')
    fragments=[f"'{name}', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {name} t)"
               for name in tables]
    return sql('SELECT jsonb_build_object('+','.join(fragments)+') AS state')[0]['state']


def test_unknown_whitelist_and_published_sources(settings, alice, create, sql):
    trip = create()
    before = unchanged(sql)
    result = capture(settings, alice, selection(trip['id']))
    assert result['source_kind'] == 'observed'
    assert result['simulated_identity'] == 'personal_alice'
    assert result['environment'] == 'local-demo'
    assert result['assumptions'] == ['synthetic_demo_account', 'simulated_business_services']
    assert result['unknowns'] == ['current_quote_amount']
    assert result['data'] == {'trip': {'id':trip['id'], 'revision':1,'flight_id':'DEMO-PEK-SHA',
                                    'travel_date':'2026-10-01','passengers':1,'status':'draft'},
                              'quote':None,'lock':None,'payment':None,'ticket_task':None,'ticket':None,'approval':None}
    assert result['documented']['source_kind'] == 'documented'
    assert result['documented']['revision'] == 1
    assert result['documented']['source'].startswith('product-published:')
    assert unchanged(sql) == before


def test_ticket_observation_does_not_disclose_payment_source_or_certificate(client, settings, alice, create, sql):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    task = process_payment(settings, payment)
    process_ticket(settings, task['ticket_task_id'])
    before = unchanged(sql)
    result = capture(settings, alice, selection(trip['id']))
    assert result['data']['payment'] == {'id':payment['id'],'status':'succeeded','amount_minor':110000,'currency':'DEMO'}
    ticket = result['data']['ticket']
    assert ticket == {'id': sql('SELECT id FROM tickets')[0]['id'].__str__(), 'status':'valid','trip_revision':1}
    assert set(result['data']['lock']) == {'id','trip_revision','amount_minor','currency','effective_status','valid','expires_at','price_basis'}
    assert not any(key in str(result) for key in ('source_ref','profile_id','certificate','captured_minor'))
    assert unchanged(sql) == before


def test_cross_owner_org_and_lin_linked_ids_are_not_observations(client, settings, login, sql):
    ctx = corporate_ready(client, login)
    before = unchanged(sql)
    for account in ('personal_bob','employee_ding','employee_fang','approver_lin'):
        with pytest.raises(HTTPException) as error:
            capture(settings, login(account), selection(ctx['trip']['id']))
        assert (error.value.status_code, error.value.detail) == (404,'not_visible')
    selected = selection(ctx['approved']['id'], 'approval')
    result = capture(settings, ctx['approver'], selected)
    assert set(result['data']['approval']) == {'id','revision','trip_revision','applicant_name','flight_id','travel_date','passengers','amount_minor','currency','effective_status','valid'}
    for account in ('approver_qian','approver_wu','approver_zhao','employee_fang'):
        with pytest.raises(HTTPException) as error:
            capture(settings, login(account), selected)
        assert (error.value.status_code,error.value.detail) == (404,'not_visible')
    assert unchanged(sql) == before


@pytest.mark.parametrize('change', ['revision','expiry','knowledge','source','version','extra','deadline'])
def test_stale_or_forged_delivery_is_rejected(change, client, settings, alice, create, sql, monkeypatch):
    trip, _, _, _ = ready(client, alice, create)
    selected = selection(trip['id'])
    old = capture(settings, alice, selected)
    if change == 'revision':
        write(client, alice, f"/trips/{trip['id']}", {'expected_revision':1,'passengers':2}, method='patch')
    elif change == 'expiry':
        advance(settings, 300)
    elif change == 'knowledge':
        monkeypatch.setitem(obs.DOCUMENTS, 'trip', {**obs.DOCUMENTS['trip'], 'revision':2})
    elif change == 'source':
        old = deepcopy(old); old['source_kind'] = 'sandbox_observed'
    elif change == 'version': old['product_version'] = 'v2'
    elif change == 'extra': old['raw_response'] = 'untrusted'
    else: old['valid_until'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    before = unchanged(sql)
    with pytest.raises(HTTPException) as error:
        obs.verify_delivery(settings, request(alice), selected, old)
    assert error.value.status_code == 409
    if change == 'expiry':
        fresh = capture(settings, alice, selected)
        assert fresh['data']['quote']['effective_status'] == fresh['data']['lock']['effective_status'] == 'expired'
        assert fresh['unknowns'] == ['current_quote_amount']
    assert unchanged(sql) == before


@pytest.mark.parametrize('change', ['session','membership','assignment','role'])
def test_old_delivery_rechecks_revocation(change, client, settings, login, sql):
    ctx = corporate_ready(client, login)
    headers = ctx['approver']
    selected = selection(ctx['approved']['id'], 'approval')
    old = capture(settings, headers, selected)
    if change == 'session':
        response = client.post('/demo/sessions', headers=headers, json={'account':'personal_bob'})
        assert response.status_code == 201
    elif change == 'membership':
        sql("UPDATE memberships SET active=false,revision=revision+1 WHERE user_id='approver_lin' RETURNING user_id")
    elif change == 'role':
        sql("UPDATE memberships SET approver=false,revision=revision+1 WHERE user_id='approver_lin' RETURNING user_id")
    else:
        sql("UPDATE approval_assignments SET active=false WHERE applicant_id='employee_chen' RETURNING applicant_id")
    before = unchanged(sql)
    with pytest.raises(HTTPException) as error:
        obs.verify_delivery(settings, request(headers), selected, old)
    assert error.value.status_code in (401,404)
    assert unchanged(sql) == before


def test_evidence_is_private_to_session_and_revision(client, settings, alice, bob, login, create, sql):
    trip = create()
    selected = selection(trip['id'])
    old = capture(settings, alice, selected)
    other = login('personal_alice')
    assert capture(settings, other, selected)['evidence_id'] != old['evidence_id']
    before = unchanged(sql)
    with pytest.raises(HTTPException) as error:
        capture(settings, other, selection(trip['id'], expected_evidence=old['evidence_id']))
    assert error.value.detail == 'state_changed'
    with pytest.raises(HTTPException) as error:
        obs.verify_delivery(settings, request(bob), selected, old)
    assert error.value.detail == 'not_visible'
    assert unchanged(sql) == before


@pytest.mark.parametrize('extra', ['role=admin','owner=personal_bob','org=org-B','url=http://127.0.0.1:55432',
                                 'path=services/business/app.py','sql=select+1','kind=approval','clock=300',
                                 'outcome=succeeded'])
def test_http_rejects_identity_and_control_fields(extra, client, alice, create, sql):
    trip = create()
    before = unchanged(sql)
    response = client.get(f"/guide/observe?kind=trip&object_id={trip['id']}&{extra}", headers=alice)
    assert response.status_code == 422
    assert response.json() == {'error':'invalid_request'}
    assert unchanged(sql) == before


def test_http_authorization_precedes_runtime(client, alice, bob, create, sql):
    trip = create()
    before = unchanged(sql)
    path = f"/guide/observe?kind=trip&object_id={trip['id']}"
    assert client.get(path).status_code == 401
    response = client.get(path, headers={**bob,'X-Role':'admin','X-Owner':'personal_alice','X-Org':'org-A'})
    assert response.status_code == 404 and response.json() == {'error':'not_visible'}
    assert client.post(path,headers=alice,json={'reset':True}).status_code == 405
    assert unchanged(sql) == before


def test_runtime_missing_is_fail_closed_not_a_success_mock(client, alice, create, sql, monkeypatch, tmp_path):
    from services.guide import windows
    # 仅验证拒绝分支：不存在的工程配置不能回退普通子进程。不是成功隔离证据。
    monkeypatch.setattr(windows,'ROOT',tmp_path)
    trip = create()
    before = unchanged(sql)
    response = client.get(f"/guide/observe?kind=trip&object_id={trip['id']}",headers=alice)
    assert response.status_code == 503 and response.json() == {'error':'isolation_unavailable'}
    assert response.headers['cache-control'] == 'no-store'
    assert unchanged(sql) == before


def test_missing_required_quote_leaf_fails_closed_without_business_change(
    client, alice, create, sql, monkeypatch,
):
    # 只注入可信宿主视图的契约故障；不替代实际AppContainer成功验收。
    trip, _, _, _ = ready(client, alice, create)
    original = obs.view

    def missing_amount(conn, ws, kind, row):
        projected = original(conn, ws, kind, row)
        if kind == 'quote':
            projected = dict(projected)
            del projected['amount_minor']
        return projected

    monkeypatch.setattr(obs, 'view', missing_amount)
    before = unchanged(sql)
    response = client.get(
        f"/guide/observe?kind=trip&object_id={trip['id']}", headers=alice,
    )
    assert response.status_code == 503
    assert response.json() == {'error': 'observation_unavailable'}
    assert response.headers['cache-control'] == 'no-store'
    assert unchanged(sql) == before


@pytest.mark.parametrize('kind', ['trip', 'approval'])
def test_published_help_contains_only_traceable_public_procedure(
    kind, client, settings, alice, create, login, sql,
):
    import json
    import re

    if kind == 'trip':
        # 未绑定账户不得因产品说明获知资格或确定金额。
        trip = create()
        headers = alice
        selected = selection(trip['id'])
    else:
        context = corporate_ready(client, login)
        headers = context['approver']
        selected = selection(context['approved']['id'], 'approval')

    before = unchanged(sql)
    result = capture(settings, headers, selected)
    document = result['documented']
    assert set(document) == {'id', 'revision', 'source_kind', 'source', 'text', 'ui_anchor'}
    assert document['source_kind'] == 'documented'
    assert isinstance(document['revision'], int) and document['revision'] >= 1
    assert re.fullmatch(r'product-published:travel-lab/v1/[a-z0-9-]+', document['source'])
    assert document['source'].rsplit('/', 1)[1] == document['id']
    assert document['text'] and document['ui_anchor']

    # 审核实际发布的两份帮助内容；不扫描合法观察中的真实业务金额。
    published = json.dumps(document, ensure_ascii=False)
    forbidden = ('services/', 'services\\', 'tests/', 'tests\\', '.local',
                 'source_ref', 'profile_id', 'certificate', 'evaluator_only',
                 'scenario_id', 'ticket_count', 'total_paid', 'duplicate_payment_count')
    assert not any(marker in published for marker in forbidden)
    assert not re.search(r'\b(?:[ACDFS]\d{2}|D\d-\d{2})\b', published)
    assert not re.search(r'\b(?:110000|100000|140000|148000|1100|1000|1400|1480)\b', published)
    assert not any(claim in document['text'] for claim in
                   ('你已具备资格', '你具有资格', '你已获批准', '你的价格是', '你已完成'))

    # 资格、付款类别及来源不属于受控观察字段；公开流程不补造这些事实。
    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert keys(result['data']).isdisjoint(
        {'eligible', 'eligibility', 'qualification', 'payment_kind', 'payment_class',
         'source_ref', 'profile_id'},
    )
    if kind == 'trip':
        assert result['state_versions']['payment_profile'] == 0
        assert 'current_quote_amount' in result['unknowns']
        assert result['data']['quote'] is None and result['data']['lock'] is None
    assert unchanged(sql) == before
