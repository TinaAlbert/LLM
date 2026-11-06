"""评测入口的无费用边界；不连接PG、不启动服务、不将合成输入算AI样本。"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json

import httpx
import pytest

from scripts import d3_evaluate as evaluation
from services.business.config import Settings
from services.guide.budget import BudgetLedger
from services.guide.model import ENDPOINT


def common_plan():
    return {'task_ref': 'normal-first', 'scenario': 'normal', 'goal': '帮我完成这次模拟预订并确认出票',
        'initial_state': '个人行程未绑定付款资料', 'event_schedule': [],
        'feedback_rule': evaluation.FEEDBACK_RULE, 'group_order': ['guide', 'B0', 'B1']}


def projection():
    return {'product_id': 'travel-lab', 'product_version': 'v1', 'contract_version': 'd3-personal-1',
        'knowledge_version': 'personal-guide-1', 'rule_version': 'booking-v1',
        'policy_version': 'subject-assignment-v1', 'feature_config_version': 'personal-1',
        'source_kind': 'observed', 'environment': 'local-demo', 'simulated_identity': 'synthetic_personal',
        'disclosure_scope': 'current-session',
        'assumptions': ['synthetic_demo_account', 'simulated_business_services'],
        'unknowns': ['current_quote_amount'], 'business_time': '2026-09-27T00:00:00+00:00',
        'state_versions': {'trip': 1, 'payment_profile': 0}, 'payment_profile_state': 'unbound',
        'flight_state': 'available', 'data': {'trip': {'ref': 'trip', 'revision': 1,
            'flight_id': 'DEMO-PEK-SHA', 'travel_date': '2026-10-01', 'passengers': 1, 'status': 'draft'},
            'quote': None, 'lock': None, 'payment': None, 'ticket_task': None, 'ticket': None},
        'observed_at': '2026-10-08T00:00:00+00:00', 'valid_until': '2026-10-08T00:00:30+00:00'}


def test_fair_inputs_differ_only_in_observation_and_do_not_claim_execution():
    b0 = evaluation.prepare_input('B0', '帮我确认这次预订是否已出票')
    b1 = evaluation.prepare_input('B1', '帮我确认这次预订是否已出票', observation=projection())
    expected = deepcopy(b1['model_input'])
    expected['observation'] = None
    assert b0['model_input'] == expected
    assert b0['host_only']['mode'] == b1['host_only']['mode']
    assert b0['host_only']['prompt_sha256'] == b1['host_only']['prompt_sha256']
    assert b0['execution_status'] == b1['execution_status'] == 'not_executed'
    assert b0['host_only']['completion_rate'] is None
    assert 'group' not in b0['model_input'] and 'fixture' not in b0['model_input']


def test_b0_rejects_state_instead_of_silently_using_or_dropping_it():
    with pytest.raises(ValueError, match='b0_state_forbidden'):
        evaluation.prepare_input('B0', '帮我预订', observation=projection())


def test_web_session_missing_proof_stops_before_pg_server_or_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'model_prerequisites', lambda: ['token_bound_unverified'])
    monkeypatch.setattr(evaluation, 'checked_settings', lambda: pytest.fail('缺依据不能连PG'))
    before = evaluation.ledger_fingerprint()
    result = evaluation.web_session('guide', '00000000-0000-0000-0000-000000000001', common_plan(), 8000, 300)
    assert result['execution_status'] == 'stopped_before_dispatch'
    assert result['ai_task_denominator'] == result['physical_reservations'] == 0
    assert result['active_group'] == 'not_measured' and evaluation.ledger_fingerprint() == before


def test_transport_records_real_send_boundary_and_body_without_private_content():
    import threading
    import time
    active, row = threading.Event(), {'network_attempts': 0}
    async def respond(request):
        assert active.is_set()
        return httpx.Response(200, content=b'raw_response_private_sentinel')
    async def run():
        transport = evaluation.MeasuredTransport(row, active, time.monotonic(), httpx.MockTransport(respond))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.post(ENDPOINT, headers={'Authorization': 'secret_header_sentinel'},
                json={'goal': 'raw_request_private_sentinel'})
            assert response.content == b'raw_response_private_sentinel'
        assert not active.is_set()
    asyncio.run(run())
    assert row['network_attempts'] == 1 and row['network_closed_ms'] >= row['network_started_ms']
    assert all(value not in json.dumps(row) for value in
        ('secret_header_sentinel', 'raw_request_private_sentinel', 'raw_response_private_sentinel'))


@pytest.mark.parametrize('failure', [httpx.ConnectError, asyncio.CancelledError])
def test_transport_failure_or_cancel_clears_active_window(failure):
    import threading
    import time
    active, row = threading.Event(), {'network_attempts': 0}
    async def respond(request):
        raise failure('private_failure_sentinel')
    async def run():
        transport = evaluation.MeasuredTransport(row, active, time.monotonic(), httpx.MockTransport(respond))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(failure):
                await client.post(ENDPOINT)
        assert not active.is_set()
    asyncio.run(run())
    assert row['network_attempts'] == 1 and 'private_failure_sentinel' not in json.dumps(row)


def test_transport_does_not_dispatch_another_destination():
    import threading
    import time
    active, row = threading.Event(), {'network_attempts': 0}
    async def respond(request): pytest.fail('未知目的地不能进入传输')
    async def run():
        transport = evaluation.MeasuredTransport(row, active, time.monotonic(), httpx.MockTransport(respond))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(ValueError, match='evaluation_destination_mismatch'):
                await client.post('https://other.invalid/chat/completions')
        assert not active.is_set()
    asyncio.run(run())
    assert row['network_attempts'] == 0


def test_passive_web_trace_uses_route_template_and_never_body_header_or_uuid():
    import time
    from types import SimpleNamespace
    trace = {'http_actions': [], 'errors': []}
    scope = {'type': 'http', 'method': 'POST', 'path': '/locks/private-uuid/payments',
        'headers': [(b'authorization', b'private_bearer')], 'state': {}}
    async def app(scope, receive, send):
        scope['route'] = SimpleNamespace(path='/locks/{lock_id}/payments')
        await send({'type': 'http.response.start', 'status': 202, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'private_payment_response'})
    async def receive(): pytest.fail('被动记录器不能另读请求body')
    async def send(message): pass
    asyncio.run(evaluation.RecordedWeb(app, trace, time.monotonic())(scope, receive, send))
    assert trace['http_actions'][0]['route'] == '/locks/{lock_id}/payments'
    assert trace['http_actions'][0]['status'] == 202
    assert not any(value in json.dumps(trace) for value in ('private-uuid', 'private_bearer', 'private_payment_response'))


def test_candidate_measurement_keeps_original_rejection_and_restores_function():
    from services.guide import coordinator
    original = coordinator.parse_candidate
    row = {'candidate_check_ms': 0, 'candidate_check_calls': 0}
    token = evaluation._recorded_round.set(row)
    try:
        with evaluation.measured_candidate_checks(), pytest.raises(ValueError):
            coordinator.parse_candidate({'extra_private_field': True})
    finally:
        evaluation._recorded_round.reset(token)
    assert coordinator.parse_candidate is original
    assert row['candidate_check_calls'] == 1 and row['candidate_check_ms'] >= 0


def test_actual_local_http_read_driver_waits_then_sends_original_sequence_and_logs_out():
    """真实loopback HTTP协议；传输窗由测试控制，不冒充真实模型负载。"""
    import threading
    import time
    from fastapi import FastAPI, Response
    app, logged_in, logged_out = FastAPI(), threading.Event(), threading.Event()
    active, stop, samples, errors, seen = threading.Event(), threading.Event(), [], [], []
    @app.post('/demo/sessions', status_code=201)
    async def login():
        logged_in.set()
        return {'access_token': 'synthetic_secret_not_recorded'}
    @app.delete('/session')
    async def logout():
        logged_out.set()
        return Response(status_code=204)
    def reader(endpoint):
        async def read():
            seen.append(endpoint)
            return {}
        return read
    for endpoint in evaluation.SEQUENCE:
        app.add_api_route(endpoint, reader(endpoint), methods=['GET'])
    with evaluation.running_server(app, 0) as (url, _, server_thread):
        thread = threading.Thread(target=evaluation.active_reads,
            args=(url, active, stop, samples, errors, time.monotonic()))
        thread.start()
        try:
            assert logged_in.wait(5) and seen == [] and samples == []
            active.set()
            thread.join(10)
            assert not thread.is_alive() and logged_out.is_set()
        finally:
            stop.set(); thread.join(5)
    assert not server_thread.is_alive() and not errors
    assert seen == list(evaluation.SEQUENCE)*10 and len(samples) == 30
    assert evaluation.latency_summary(samples)['errors'] == 0
    assert 'synthetic_secret_not_recorded' not in json.dumps(samples)


def summary_record(plan=None, **changes):
    return {'kind': 'continuous_original_web_task', 'host_only_plan': plan or common_plan(),
        'group': 'guide', 'execution_status': 'session_recorded', 'task_attempts': 4,
        'rounds': [], 'independent_ticket': None, **changes}


def test_summary_deduplicates_task_and_does_not_count_prepared_or_stopped_records(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', tmp_path)
    paths = []
    for name, value in [('first', summary_record()), ('more_rounds', summary_record()),
        ('stopped', summary_record(execution_status='stopped_before_dispatch', task_attempts=0)),
        ('B0', summary_record(group='B0'))]:
        path = tmp_path / (name + '.json'); path.write_text(json.dumps(value)); paths.append(path)
    result = evaluation.task_summary(paths)
    assert result['ai_task_denominator'] == 2 and result['stopped_before_dispatch_records'] == 1
    assert result['completion_rate'] is result['actual_bill_cny'] is None
    assert len(result['tasks'][0]['records']) == 2


def test_summary_receipt_failure_stays_unknown_and_conflicting_receipts_not_double_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', tmp_path)
    paths = []
    for name, row in [('pending', {'adapter_generate_started': True, 'receipts_verified': True,
            'receipts': [{'reservation_ref': 'a'*64, 'state': 'unresolved'}]}),
        ('changed', {'adapter_generate_started': True, 'receipts_verified': True,
            'receipts': [{'reservation_ref': 'a'*64, 'state': 'settled'}]}),
        ('unknown', {'adapter_generate_started': True, 'receipts_verified': False, 'receipts': []})]:
        path = tmp_path / (name + '.json'); path.write_text(json.dumps(summary_record(rounds=[row]))); paths.append(path)
    result = evaluation.task_summary(paths)
    assert result['physical_reservations_from_reports'] is None and result['receipt_association_unknown']
    assert result['receipt_conflicts'] == ['a'*64] and result['unresolved_receipts'] == 1


def test_continuous_rounds_keep_separate_inputs_receipts_without_delaying_guide_for_audit_sql(tmp_path, monkeypatch):
    import threading
    import time
    from types import SimpleNamespace
    from starlette.requests import Request
    from services.guide.coordinator import Coordinator
    from services.guide.knowledge import load_knowledge
    ledger = BudgetLedger(tmp_path/'guide-usage.json'); ledger.initialize()
    async def generate(value, deadline):
        reservation = ledger.reserve()
        ledger.settle(reservation, prompt_tokens=20, completion_tokens=10,
            provider_request_id='synthetic-provider-' + reservation.request_id,
            cache_hit_tokens=0, cache_miss_tokens=20)
        return SimpleNamespace(candidate={})
    monkeypatch.setattr(Coordinator, 'make_adapter', lambda self: SimpleNamespace(ledger=ledger, generate=generate))
    async def work(self, request, delegation, round):
        value = self.make_input(delegation, round, load_knowledge(), projection())
        await self.make_adapter().generate(value, round.deadline)
        return {'candidate': {}}
    monkeypatch.setattr(Coordinator, 'work', work)
    monkeypatch.setattr(evaluation, 'business_snapshot', lambda settings: pytest.fail('不得增加指引路径SQL'))
    settings = Settings('postgresql://unused', workspace_id='test', demo_login=True, mode='test')
    trace = {'rounds': [], 'host_only_plan': common_plan(), 'task_attempts': 0}
    app = evaluation.baseline_app(settings, 'B0', trace, continuous=True,
        active=threading.Event(), origin=time.monotonic())
    async def run():
        try:
            for feedback in ('刚刚查看了资料', '刚刚刷新了页面'):
                request = Request({'type': 'http', 'state': {}})
                await app.state.guide.work(request, SimpleNamespace(goal=common_plan()['goal']),
                    SimpleNamespace(parameters={'feedback': feedback, 'detail': 'brief'}, deadline=time.monotonic()+30))
        finally:
            await app.state.guide.aclose()
    asyncio.run(run())
    first, second = trace['rounds']
    assert first['allowed_input']['feedback']['text'] != second['allowed_input']['feedback']['text']
    assert first['allowed_input']['observation'] is second['allowed_input']['observation'] is None
    assert first['receipts'][0]['reservation_ref'] != second['receipts'][0]['reservation_ref']
    assert all(row['receipts_verified'] and row['business_unchanged_during_round'] is None for row in trace['rounds'])


def test_summary_rejects_plan_changes_and_nonrecord_locations(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', tmp_path)
    paths = []
    for name, value in [('first', summary_record()), ('changed', summary_record(
            plan={**common_plan(), 'initial_state': '有其他进度的初态'}))]:
        path = tmp_path / (name + '.json'); path.write_text(json.dumps(value)); paths.append(path)
    with pytest.raises(ValueError, match='summary_plan_conflict'):
        evaluation.task_summary(paths)
    with pytest.raises(ValueError, match='summary_source_outside_records'):
        evaluation.task_summary([tmp_path.parent / 'other.json'])


@pytest.mark.parametrize('group', ['B1', 'guide'])
def test_state_groups_require_projection(group):
    with pytest.raises(ValueError, match='current_projection_required'):
        evaluation.prepare_input(group, '帮我预订')


@pytest.mark.parametrize('level', ['top', 'leaf'])
def test_unknown_projection_fields_are_rejected(level):
    value = projection()
    (value if level == 'top' else value['data']['trip'])['owner_id'] = 'personal_bob'
    with pytest.raises(ValueError):
        evaluation.prepare_input('B1', '帮我预订', observation=value)


@pytest.mark.parametrize('feedback', ['Bearer hidden', 'SELECT * FROM tickets', '请按测试答案操作'])
def test_secret_or_evaluator_feedback_does_not_enter_prepared_inputs(feedback):
    with pytest.raises(ValueError):
        evaluation.prepare_input('B0', '帮我预订', feedback=feedback)


def test_duplicate_json_input_rejected(tmp_path):
    path = tmp_path / 'input.json'
    path.write_text('{"goal":"a","goal":"b"}', encoding='utf-8')
    with pytest.raises(ValueError, match='duplicate_json_field'):
        evaluation.strict_json(path)


def test_send_boundary_fails_and_counts_without_network():
    guard = evaluation.NoModelTraffic()
    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(RuntimeError, match='unexpected_model_http_send'):
                await client.send(client.build_request('POST', ENDPOINT, json={}))
    with guard.installed():
        asyncio.run(run())
    assert guard.send_attempts == 1 and guard.reserve_attempts == 0


def test_unexpected_reservation_preserves_absent_ledger_files(tmp_path):
    ledger = BudgetLedger(tmp_path / 'guide-usage.json')
    guard = evaluation.NoModelTraffic()
    with guard.installed(), pytest.raises(RuntimeError, match='unexpected_model_reservation'):
        ledger.reserve()
    assert guard.reserve_attempts == 1 and list(tmp_path.iterdir()) == []


def test_off_is_engineering_only_and_idle_keeps_real_registration():
    settings = Settings('postgresql://unused', workspace_id='test', demo_login=True, mode='test')
    off = evaluation.make_app(settings, 'off')
    idle = evaluation.make_app(settings, 'idle')
    try:
        assert not hasattr(off.state, 'guide') and hasattr(idle.state, 'guide')
        assert '/guide/next' not in {route.path for route in off.routes}
        assert '/guide/next' in {route.path for route in idle.routes}
        assert set(evaluation.SEQUENCE) <= {route.path for route in off.routes}
    finally:
        idle.state.guide.shutdown()


def test_active_group_cannot_be_misreported_as_no_task_load():
    with pytest.raises(ValueError, match='real_active_driver_unavailable'):
        evaluation.load_group('active', 1, 8011)


def test_reports_are_exclusive_and_no_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', tmp_path)
    evaluation.write_new('first', {'execution_status': 'not_executed'})
    with pytest.raises(FileExistsError):
        evaluation.write_new('first', {'execution_status': 'fake_passed'})
    with pytest.raises(ValueError):
        evaluation.write_new('../outside', {})
    assert json.loads((tmp_path / 'first.json').read_text())['execution_status'] == 'not_executed'


def test_report_is_claimed_and_persisted_before_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', tmp_path)
    def operation():
        pending = json.loads((tmp_path / 'claimed.json').read_text())
        assert pending['execution_status'] == 'in_progress'
        assert pending['physical_reservations'] is None
        with pytest.raises(FileExistsError):
            evaluation.record_run('claimed', lambda: pytest.fail('竞争者不能执行'))
        return {'kind': 'synthetic_check', 'execution_status': 'not_executed'}
    path, result = evaluation.record_run('claimed', operation, command='prepare-input')
    assert json.loads(path.read_text()) == result


def test_report_directory_failure_stops_before_operation(tmp_path, monkeypatch):
    blocked = tmp_path / 'regular-file'
    blocked.write_bytes(b'preserve')
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', blocked)
    with pytest.raises(FileExistsError):
        evaluation.record_run('blocked', lambda: pytest.fail('不能开始DB/模型工作'))
    assert blocked.read_bytes() == b'preserve'


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_interrupted_record_preserves_unknown_cost_without_exception_text(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', tmp_path)
    def operation():
        raise failure('private_exception_sentinel')
    with pytest.raises(failure):
        evaluation.record_run('interrupted', operation, command='baseline-round')
    report = json.loads((tmp_path / 'interrupted.json').read_text())
    assert report['execution_status'] == 'interrupted'
    assert report['physical_reservations'] is report['actual_bill_cny'] is report['ai_task_denominator'] is None
    assert 'private_exception_sentinel' not in json.dumps(report)
    with pytest.raises(FileExistsError):
        evaluation.record_run('interrupted', lambda: pytest.fail('中断不能自动重跑'))


def test_final_serialization_failure_keeps_a_readable_unknown_record(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', tmp_path)
    with pytest.raises(ValueError):
        evaluation.record_run('nonfinite', lambda: {'cost': float('nan')})
    report = json.loads((tmp_path / 'nonfinite.json').read_text())
    assert report['execution_status'] == 'interrupted' and report['physical_reservations'] is None


def test_actual_two_process_cli_record_competition_dispatches_once(tmp_path):
    """真实两进程/原CLI，执行函数受控；无PG/模型/真实费用。"""
    import subprocess
    import sys
    import time
    from pathlib import Path
    child = '''
import json, sys
from pathlib import Path
from scripts import d3_evaluate as evaluation
root, label = Path(sys.argv[1]), sys.argv[2]
evaluation.OUTPUT_ROOT = root
def operation(*args, **kwargs):
    (root / (label + '.dispatch')).write_text('entered', encoding='utf-8')
    sys.stdin.readline()
    return {'kind': 'input_contract_preparation', 'execution_status': 'not_executed'}
evaluation.prepare_input = operation
sys.argv = ['d3_evaluate.py', 'prepare-input', '--group', 'B0', '--goal', 'synthetic', '--name', 'competing']
try:
    raise SystemExit(evaluation.main())
except FileExistsError:
    raise SystemExit(1)
'''
    args = [sys.executable, '-X', 'utf8', '-c', child, str(tmp_path)]
    first = subprocess.Popen([*args, 'first'], cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    second = None
    try:
        until = time.monotonic() + 10
        while not (tmp_path / 'first.dispatch').exists() and first.poll() is None and time.monotonic() < until:
            time.sleep(.01)
        assert (tmp_path / 'first.dispatch').exists(), '首进程未进入受控工作'
        second = subprocess.Popen([*args, 'second'], cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        second.communicate('continue\n', timeout=10)
        first.communicate('continue\n', timeout=10)
        assert not (tmp_path / 'second.dispatch').exists(), '竞争者在报告拒绝之前已做工作'
        assert first.returncode == 0 and second.returncode == 1
        assert json.loads((tmp_path / 'competing.json').read_text())['execution_status'] == 'not_executed'
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_actual_hard_exit_keeps_initial_record_and_blocks_reuse(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    child = '''
import os, sys
from pathlib import Path
from scripts import d3_evaluate as evaluation
evaluation.OUTPUT_ROOT = Path(sys.argv[1])
evaluation.record_run('crashed', lambda: os._exit(7), command='baseline-round')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', child, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=10)
    assert result.returncode == 7
    report = json.loads((tmp_path / 'crashed.json').read_text())
    assert report['execution_status'] == 'in_progress' and report['physical_reservations'] is None
    monkeypatch.setattr(evaluation, 'OUTPUT_ROOT', tmp_path)
    with pytest.raises(FileExistsError):
        evaluation.record_run('crashed', lambda: pytest.fail('崩溃后不能当首次运行'))


def test_fixed_engineering_condition_cannot_be_selected_in_production_or_request():
    from services.guide.coordinator import Coordinator
    from services.guide.contracts import Next
    from pydantic import ValidationError
    settings = Settings('postgresql://unused', workspace_id='demo', demo_login=True, mode='local')
    with pytest.raises(ValueError, match='protected_test'):
        Coordinator(settings, information_condition='B0')
    with pytest.raises(ValidationError):
        Next.model_validate_json(json.dumps({'action': 'next', 'trip_id': '00000000-0000-0000-0000-000000000001',
            'goal': '模拟预订', 'request_id': 'fixed-group', 'information_condition': 'B0'}))


def test_actual_coordinator_inputs_keep_b0_state_out_and_b1_identical_to_guide():
    from services.guide.coordinator import Coordinator
    from services.guide.knowledge import load_knowledge
    from types import SimpleNamespace
    settings = Settings('postgresql://unused', workspace_id='test', demo_login=True, mode='test')
    states = [Coordinator(settings, information_condition=group) for group in ('guide', 'B0', 'B1')]
    try:
        delegation = SimpleNamespace(goal=common_plan()['goal'])
        round = SimpleNamespace(parameters={'feedback': '', 'detail': 'brief'})
        inputs = [state.make_input(delegation, round, load_knowledge(), projection()) for state in states]
        assert inputs[0] == inputs[2]
        expected = deepcopy(inputs[0]); expected['observation'] = None
        assert inputs[1] == expected
    finally:
        for state in states: state.shutdown()


@pytest.mark.parametrize('group,references,allowed', [
    ('B0', ['knowledge:booking.refresh', 'observation:current'], False),
    ('B1', ['knowledge:booking.refresh', 'observation:current'], True),
    ('B0', ['knowledge:booking.refresh'], True)])
def test_b0_hidden_state_reference_rejected_without_repair(monkeypatch, group, references, allowed):
    """仅控制协议候选，不计模型/PG/真实隔离；验证安全层不修补B0。"""
    import time
    from types import SimpleNamespace
    from fastapi import HTTPException
    from services.guide.coordinator import Coordinator
    candidate = {'status': 'next_step', 'step_id': 'booking.refresh', 'anchor_id': 'booking.refresh',
        'explanation': '请刷新当前预订', 'preconditions': [], 'expected_change': '查看付款和票据进度',
        'references': references, 'unknowns': [], 'amount_ref': None}
    ipc = []
    session = SimpleNamespace(complete=lambda value: ipc.append(value) or value, abort=lambda: None)
    monkeypatch.setattr('services.guide.windows.begin_guide', lambda *args: session)
    async def run():
        settings = Settings('postgresql://unused', workspace_id='test', demo_login=True, mode='test')
        state = Coordinator(settings, information_condition=group)
        round = SimpleNamespace(parameters={'feedback': '', 'detail': 'brief'}, deadline=time.monotonic()+30,
            generation=1, evidence=None)
        delegation = SimpleNamespace(goal=common_plan()['goal'], generation=1, cancelled=False,
            ref='a'*32, round=round)
        state.delegations[delegation.ref] = delegation
        evidence = SimpleNamespace(public=projection(), fingerprint='same', expired=lambda: False,
            observed_at=datetime.now(timezone.utc), valid_until=datetime.now(timezone.utc))
        async def snapshot(*args): return evidence
        async def generate(*args): return SimpleNamespace(candidate=candidate)
        state.snapshot = snapshot
        state.make_adapter = lambda: SimpleNamespace(generate=generate)
        try:
            if allowed:
                result = await state.work(None, delegation, round)
                assert result['candidate'] == candidate and len(ipc) == 1
                if group == 'B0':
                    assert result['source_kind'] == 'unverified' and result['completion_verified'] is False
            else:
                with pytest.raises(HTTPException) as error:
                    await state.work(None, delegation, round)
                assert error.value.detail == 'candidate_rejected' and ipc == []
        finally:
            state.delegations.clear()
            await state.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('state', ['pending', 'unresolved', 'settled'])
def test_receipt_snapshot_preserves_actual_records_and_files(tmp_path, state):
    ledger = BudgetLedger(tmp_path/'guide-usage.json')
    ledger.initialize()
    reservation = ledger.reserve()
    if state == 'unresolved': ledger.mark_unresolved(reservation, 'model_timeout')
    if state == 'settled':
        ledger.settle(reservation, prompt_tokens=101, completion_tokens=17,
            provider_request_id='synthetic-receipt', cache_hit_tokens=2, cache_miss_tokens=99)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    rows = ledger.receipt_snapshot()
    report = evaluation.receipt_projection(rows[0])
    assert rows[0]['state'] == report['state'] == state
    assert reservation.request_id not in json.dumps(report) and 'synthetic-receipt' not in json.dumps(report)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    if state == 'settled':
        assert report['prompt_tokens'] == 101 and report['cache_hit_tokens'] == 2
    else:
        assert report['accounted_cny'] is None and report['reserved_cny'] == '0.032'


def test_receipt_snapshot_never_creates_or_repairs_missing_or_empty_anchor(tmp_path):
    from services.guide.budget import BudgetUnavailable
    ledger = BudgetLedger(tmp_path/'guide-usage.json')
    with pytest.raises(BudgetUnavailable): ledger.receipt_snapshot()
    assert list(tmp_path.iterdir()) == []
    ledger.lock_path.write_bytes(b'')
    with pytest.raises(BudgetUnavailable): ledger.receipt_snapshot()
    assert ledger.lock_path.read_bytes() == b''


@pytest.mark.parametrize('failure', ['candidate_invalid', 'model_timeout'])
def test_adapter_failure_still_associates_settled_or_unresolved_cost(tmp_path, monkeypatch, failure):
    """真实临时账本、受控适配器异常；无模型请求，不计AI。"""
    from types import SimpleNamespace
    from services.guide.coordinator import Coordinator
    from services.guide.model import ModelUnavailable
    ledger = BudgetLedger(tmp_path/'guide-usage.json'); ledger.initialize()
    async def controlled_generate(*args):
        reservation = ledger.reserve()
        if failure == 'candidate_invalid':
            ledger.settle(reservation, prompt_tokens=20, completion_tokens=10,
                provider_request_id='rejected-candidate-usage', cache_hit_tokens=0, cache_miss_tokens=20)
        else:
            ledger.mark_unresolved(reservation, failure)
        raise ModelUnavailable(failure)
    monkeypatch.setattr(Coordinator, 'make_adapter', lambda self:
        SimpleNamespace(ledger=ledger, generate=controlled_generate))
    settings = Settings('postgresql://unused', workspace_id='test', demo_login=True, mode='test')
    trace = {}
    app = evaluation.baseline_app(settings, 'B0', trace)
    try:
        async def run():
            with pytest.raises(ModelUnavailable):
                await app.state.guide.make_adapter().generate({}, 0)
        asyncio.run(run())
        assert trace['receipts_verified'] and len(trace['receipts']) == 1
        row = trace['receipts'][0]
        assert row['state'] == ('settled' if failure == 'candidate_invalid' else 'unresolved')
        assert row['completion_tokens'] == (10 if failure == 'candidate_invalid' else None)
    finally:
        app.state.guide.shutdown()


@pytest.mark.parametrize('group', ['guide', 'B0', 'B1'])
def test_current_real_prerequisites_stop_baseline_before_pg_or_dispatch(monkeypatch, group):
    from uuid import UUID
    monkeypatch.setattr(evaluation, 'checked_settings', lambda: pytest.fail('前置未齐不能启动PG/服务'))
    guard = evaluation.NoModelTraffic()
    before = evaluation.ledger_fingerprint()
    with guard.installed():
        result = evaluation.baseline_round(group, str(UUID(int=1)), common_plan(), '', 'brief', 8011)
    assert result['execution_status'] == 'stopped_before_dispatch'
    assert 'token_bound_unverified' in result['missing']
    assert result['physical_reservations'] == result['ai_task_denominator'] == 0
    assert guard.send_attempts == guard.reserve_attempts == 0
    assert evaluation.ledger_fingerprint() == before


def test_final_business_snapshot_failure_keeps_known_baseline_cost(tmp_path, monkeypatch):
    """只控制HTTP/收尾异常，临时真实账本；不计模型或PG运行证据。"""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from uuid import UUID
    ledger = BudgetLedger(tmp_path/'guide-usage.json'); ledger.initialize()
    reservation = ledger.reserve()
    ledger.settle(reservation, prompt_tokens=25, completion_tokens=7,
        provider_request_id='synthetic-final-snapshot', cache_hit_tokens=0, cache_miss_tokens=25)
    projected = evaluation.receipt_projection(ledger.receipt_snapshot()[0])
    monkeypatch.setattr(evaluation, 'model_prerequisites', lambda: [])
    monkeypatch.setattr(evaluation, 'checked_settings', lambda: None)
    snapshots = iter([{'tables': {}, 'sha256': 'initial', 'authentication': {}}])
    def snapshot(*args):
        try: return next(snapshots)
        except StopIteration: raise RuntimeError('controlled_final_snapshot_failure')
    monkeypatch.setattr(evaluation, 'business_snapshot', snapshot)
    def app(settings, group, trace):
        trace.update(receipts=[projected], receipts_verified=True, adapter_generate_started=True)
        return None
    monkeypatch.setattr(evaluation, 'baseline_app', app)
    @contextmanager
    def server(*args): yield 'http://127.0.0.1:8011', None, SimpleNamespace(is_alive=lambda: False)
    monkeypatch.setattr(evaluation, 'running_server', server)
    class Client:
        headers = {}
        def __init__(self, *args, **kwargs): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, path, **kwargs):
            return SimpleNamespace(status_code=201 if path == '/demo/sessions' else 503,
                json=lambda: {'access_token': 'synthetic-login'} if path == '/demo/sessions' else {'error': 'candidate_rejected'})
        def delete(self, path): return SimpleNamespace(status_code=204)
    monkeypatch.setattr(httpx, 'Client', Client)
    report = evaluation.baseline_round('B0', str(UUID(int=1)), common_plan(), '', 'brief', 8011)
    assert report['business_unchanged'] is None and report['business_after'] is None
    assert report['physical_reservations'] == 1 and report['receipts'] == [projected]
    assert 'final_snapshot_RuntimeError' in report['errors']
    assert 'synthetic-login' not in json.dumps(report)
