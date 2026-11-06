"""真实工程 complete 端点：确认后的主体/状态仍约束虚拟事件推进。"""
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from scripts import v1_task
from services.business.app import Settings
from services.guide.assistant import Identity
from services.guide.releases import Releases


@pytest.fixture
def host_app(tmp_path, monkeypatch):
    effects = {'events': [], 'completed': [], 'stops': [], 'state_reads': 0, 'sql': 0,
        'business_calls': 0}
    monkeypatch.setattr(v1_task.CompletedTurnWorker, 'advance',
        lambda self, event_id: effects['events'].append(event_id))
    app, host = v1_task.make_app(Settings('postgresql://unavailable', 'test', True, 'test'),
        tmp_path, str(uuid4()), Releases('test', tmp_path / 'r.json'))
    owner = Identity('alice-session', 'personal_alice')
    current = {'who': owner, 'hash': 'confirmed-state', 'active': True}
    async def who(request, deadline):
        return current['who']
    async def state(request, identity, trip_id, deadline):
        effects['state_reads'] += 1
        return {}, current['hash']
    def check():
        if not current['active']:
            raise ValueError('task_stopped_or_expired')
    def complete(receipt, *, terminal):
        effects['completed'].append((dict(receipt), terminal))
    def independent_terminal(url, trip_id):
        effects['sql'] += 1
        raise ValueError('no_valid_ticket')
    monkeypatch.setattr(v1_task, 'independent_terminal', independent_terminal)
    app.state.assistant.who = who
    runtime = SimpleNamespace(check=check, stop=effects['stops'].append, complete=complete,
        _state=state, owner=owner, body=SimpleNamespace(trip_id=str(uuid4())),
        pending={'owner': owner, 'private_hash': 'confirmed-state',
            'formal': {'action_contract': {'action': {'kind': 'wait'}}}},
        sequence=1, round=3, stopped=False,
        scope=SimpleNamespace(spec={'case_id':'V1-AC'}))
    host['runtime'] = runtime
    key = json.loads((tmp_path / 'host-secret.json').read_text(encoding='utf-8'))['key']
    return app, host, runtime, current, effects, {'X-V1-Host': key}


@pytest.mark.parametrize('kind', ['wait', 'end', 'navigate'])
@pytest.mark.parametrize('changed', ['actor', 'session', 'state', 'stopped'])
def test_changed_current_authorization_never_completes_or_advances(host_app, kind, changed):
    app, _, runtime, current, effects, headers = host_app
    runtime.pending['formal']['action_contract']['action']['kind'] = kind
    if changed == 'actor':
        current['who'] = Identity('bob-session', 'personal_bob')
    elif changed == 'session':
        current['who'] = Identity('new-alice-session', 'personal_alice')
    elif changed == 'state':
        current['hash'] = 'new-object-or-state'
    else:
        current['active'] = False
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.post('/__v1/complete', headers=headers, json={'kind': kind})
    assert result.status_code >= 400
    assert effects['stops'] == ['execution_or_terminal_failed']
    assert effects['completed'] == effects['events'] == []
    assert effects['sql'] == 0


def test_current_wait_completes_and_advances_one_event(host_app):
    app, _, _, _, effects, headers = host_app
    with TestClient(app) as client:
        result = client.post('/__v1/complete', headers=headers, json={'kind': 'wait'})
    assert result.status_code == 200
    assert effects['completed'] == [({'kind': 'wait', 'accepted': True}, False)]
    assert effects['events'] == ['1-03']
    assert effects['state_reads'] == 1 and effects['stops'] == []


def test_successful_business_write_uses_receipt_instead_of_changed_prestate(host_app):
    app, host, runtime, current, effects, headers = host_app
    runtime.pending['formal']['action_contract']['action']['kind'] = 'pay'
    current['hash'] = 'changed-by-original-accepted-payment'
    host['receipt'] = {'kind': 'pay', 'accepted': True, 'status': 202}
    with TestClient(app) as client:
        result = client.post('/__v1/complete', headers=headers, json={'kind': 'pay'})
    assert result.status_code == 200
    assert effects['completed'] == [({'kind': 'pay', 'accepted': True, 'status': 202}, False)]
    assert effects['events'] == ['1-03']
    assert effects['state_reads'] == 0 and effects['stops'] == []


def test_wrong_end_does_not_trigger_event_to_manufacture_valid_ticket(host_app):
    app, _, runtime, _, effects, headers = host_app
    runtime.pending['formal']['action_contract']['action']['kind'] = 'end'
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.post('/__v1/complete', headers=headers, json={'kind': 'end'})
    assert result.status_code >= 400
    assert effects['sql'] == 1
    assert effects['completed'] == effects['events'] == []
    assert effects['stops'] == ['execution_or_terminal_failed']


@pytest.mark.parametrize('case_id', ['V1-B', 'V1-D'])
def test_readonly_task_blocks_confirmed_business_write_before_business_layer(host_app, case_id):
    app, host, runtime, _, effects, headers = host_app
    runtime.scope.spec['case_id'] = case_id
    # 即使工程侧已确认一个合法形状的query，也不能借只读续验执行写入。
    runtime.pending['formal']['action_contract']['action']['kind'] = 'query'
    runtime.pending['confirmed'] = True
    host['operation'] = ('POST', '/synthetic-business-write', {})
    @app.post('/synthetic-business-write')
    async def business_write():
        effects['business_calls'] += 1
        return {'unexpected': 'business mutation reached'}
    with TestClient(app) as client:
        result = client.post('/synthetic-business-write', headers=headers, json={})
    assert result.status_code == 409 and result.json() == {'error': 'v1_readonly_task'}
    assert effects['stops'] == ['readonly_task_business_write_forbidden']
    assert effects['business_calls'] == 0
    assert effects['completed'] == effects['events'] == []
