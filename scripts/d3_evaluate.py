"""D3-01工程评测入口：冻结输入、原POST基线单轮、只读负载、独立票据。

baseline-round仅在全部真实前置成立后经原协调器/外壳/唯一适配器收费；
不初始化账本、不代操作、不重置业务库。本文件及独立判据不作为模型输入。
"""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psycopg
from psycopg.rows import dict_row
import uvicorn

TABLES = ('users', 'organizations', 'departments', 'memberships', 'approval_assignments',
          'payment_sources', 'trips', 'payment_profiles', 'quotes', 'booking_locks',
          'payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets',
          'flights', 'budget_projects', 'trip_budget_selections', 'approval_requests',
          'business_events', 'idempotency', 'coupons', 'workspaces')
SEQUENCE = ('/session', '/booking-options', '/trips')
FEEDBACK_RULE = 'visible-attempt-only-v1'
OUTPUT_ROOT = ROOT / '.local' / 'd3-10'


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), default=str, allow_nan=False).encode()).hexdigest()


def strict_json(path, *, limit=65536):
    raw = Path(path).read_bytes()
    require(len(raw) <= limit, 'input_file_too_large')
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'duplicate_json_field')
            value[key] = item
        return value
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))


def write_new(name, value):
    return record_run(name, lambda: value)[0]


def record_run(name, operation, *, command=None):
    """先排他占用并持久记录，再执行；中断记录不证明零费用。"""
    require(re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', name) is not None, 'invalid_record_name')
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_ROOT / (name + '.json')
    with target.open('x+', encoding='utf-8', newline='\n') as stream:
        def persist(value):
            # 完整序列化成功后才写，编码失败不能先擦掉在途记录。
            text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
            stream.seek(0)
            stream.write(text)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        pending = {'schema': 1, 'kind': 'evaluation_run_record', 'command': command,
            'execution_status': 'in_progress', 'started_at': datetime.now(timezone.utc).isoformat(),
            'physical_reservations': None, 'actual_bill_cny': None, 'ai_task_denominator': None,
            'cost_status': 'unknown_until_existing_ledger_checked',
            'recovery': 'preserve_record_and_existing_ledger_no_automatic_retry'}
        persist(pending)  # 失败则operation尚未执行；竞争者也在operation之前拒绝。
        try:
            result = operation()
            persist(result)
        except BaseException as error:
            pending.update(execution_status='interrupted', failure_class=type(error).__name__)
            try:
                persist(pending)
            except (OSError, ValueError):
                pass  # 原账本/标记保留；磁盘故障时报告可能不完整，不能补零。
            raise
    return target, result


def prepare_input(group, goal, feedback='', detail='brief', observation=None):
    """只冻结契约输入，不将文件投影当新观察/授权，不接通或绕过生产调用。"""
    from services.guide.coordinator import PROMPT_VERSION, validate_guide_input
    from services.guide.knowledge import load_knowledge
    from services.guide.model import MODEL, MODEL_REVISION, PROMPT_SHA256
    require(group in {'B0', 'B1', 'guide'}, 'unknown_group')
    if group == 'B0':
        require(observation is None, 'b0_state_forbidden')
    else:
        require(observation is not None, 'current_projection_required')
    bundle = load_knowledge()
    value = validate_guide_input({'goal': goal, 'feedback': {'source_kind': 'user_reported',
        'text': feedback}, 'detail': detail, 'knowledge': bundle.knowledge,
        'anchors': bundle.anchors, 'observation': observation, 'prompt_version': PROMPT_VERSION})
    return {'schema': 1, 'kind': 'input_contract_preparation', 'group': group,
        'execution_status': 'not_executed', 'model_input': value,
        'host_only': {'model': MODEL, 'model_revision': MODEL_REVISION,
            'prompt_sha256': PROMPT_SHA256, 'knowledge_sha256': bundle.fingerprint,
            'input_sha256': digest(value), 'feedback_rule': FEEDBACK_RULE,
            'mode': {'thinking': 'disabled', 'stream': False, 'response_format': 'json_object',
                     'max_input_tokens': 8000, 'max_output_tokens': 2000},
            'fixture': 'single-trip', 'information_condition': 'published-flow',
            'projection_is_replay_only': observation is not None,
            'task_denominator': 0, 'completion_rate': None,
            'missing_execution': ['live_original_web_task_execution', 'frozen_initial_state_and_event_schedule',
                'current_authorization_and_projection_recheck', 'native_runtime_and_model_prerequisites',
                'real_model_usage_and_cost', 'original_web_actions_and_independent_ticket']}}


def checked_settings():
    from services.business.config import load_settings
    from services.business.db import connect
    from services.business.manage import check_marker, database_purpose
    settings = load_settings(test=True)
    require(settings.workspace_id == 'test' and settings.mode == 'test', 'test_workspace_required')
    with connect(settings.database_url) as conn:
        require(database_purpose(conn) == 'test', 'test_database_required')
        check_marker(conn, 'test')
        row = conn.execute('SELECT fixture_key FROM workspaces WHERE id=%s', ('test',)).fetchone()
        require(row and row['fixture_key'] == 'single-trip', 'single_trip_fixture_required')
    return settings


def business_snapshot(settings):
    from services.business.db import connect, read_snapshot
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        conn.execute('SET TRANSACTION READ ONLY')
        result = {}
        for table in TABLES:  # 固定23表白名单；没有用户可控SQL片段。
            rows = conn.execute(f'SELECT * FROM {table} ORDER BY to_jsonb({table})::text').fetchall()
            result[table] = {'rows': len(rows), 'sha256': digest(rows)}
        sessions = conn.execute('SELECT count(*) AS total, count(*) FILTER (WHERE revoked) AS revoked FROM sessions').fetchone()
        version = conn.execute('SHOW server_version').fetchone()['server_version']
    return {'tables': result, 'sha256': digest(result), 'authentication': sessions, 'pg_version': version}


def ledger_fingerprint():
    """只取三件文件的存在/内容指纹；不解析费用、不创建锁、不读Key。"""
    result = {}
    for suffix in ('', '.initialized', '.lock'):
        path = ROOT / '.local' / ('guide-usage.json' + suffix)
        result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


class NoModelTraffic:
    """off/idle断言：拦截实际HTTP发送边界，任何尝试均失败且计数。

    另拦截费用预留以保护当前三件文件；不伪装模型成功或计AI成绩。
    """
    def __init__(self):
        self.send_attempts = 0
        self.reserve_attempts = 0

    async def send(self, client, request, *args, **kwargs):
        self.send_attempts += 1
        raise RuntimeError('unexpected_model_http_send')

    def reserve(self, ledger, *args, **kwargs):
        self.reserve_attempts += 1
        raise RuntimeError('unexpected_model_reservation')

    @contextmanager
    def installed(self):
        guard = self
        async def send(client, request, *args, **kwargs):
            return await guard.send(client, request, *args, **kwargs)
        def reserve(ledger, *args, **kwargs):
            return guard.reserve(ledger, *args, **kwargs)
        with patch.object(httpx.AsyncClient, 'send', send), patch(
                'services.guide.budget.BudgetLedger.reserve', reserve):
            yield self


def make_app(settings, group):
    from services.business.app import create_app
    require(group in {'off', 'idle'}, 'real_active_driver_unavailable')
    if group == 'off':
        # 工程入口的一次注册注入；生产app/业务端点/引擎文件不修改。
        with patch('services.guide.coordinator.register_routes', lambda app, config: None):
            app = create_app(settings)
    else:
        app = create_app(settings)
    require(hasattr(app.state, 'guide') == (group == 'idle'), 'guide_registration_mismatch')
    return app


@contextmanager
def running_server(app, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', port))  # 端口占用直接失败，不终止其他服务。
    actual_port = listener.getsockname()[1]
    config = uvicorn.Config(app, host='127.0.0.1', port=actual_port, access_log=False,
                            proxy_headers=False, log_level='critical', timeout_graceful_shutdown=8)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]},
                              name='d3-evaluation-http', daemon=True)
    thread.start()
    try:
        until = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < until:
            thread.join(.02)
        require(server.started, 'evaluation_http_start_failed')
        yield f'http://127.0.0.1:{actual_port}', server, thread
    finally:
        server.should_exit = True
        thread.join(12)
        listener.close()
        require(not thread.is_alive(), 'evaluation_http_shutdown_unverified')


def pg_sampler(settings, stop, ready, samples, errors, origin=None):
    try:
        with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row,
                connect_timeout=3, application_name='d3-evaluation-sampler',
                options='-c statement_timeout=1000 -c default_transaction_read_only=on') as conn:
            ready.set()
            started = time.monotonic() if origin is None else origin
            while not stop.is_set():
                row = conn.execute('''SELECT count(*) AS connections,
                    count(*) FILTER (WHERE a.state='active') AS active,
                    count(*) FILTER (WHERE a.xact_start IS NOT NULL) AS transactions,
                    count(*) FILTER (WHERE a.state='idle in transaction') AS idle_in_transaction,
                    count(*) FILTER (WHERE a.wait_event_type='Lock') AS lock_waiters,
                    (SELECT count(*) FROM pg_locks l JOIN pg_stat_activity b ON l.pid=b.pid
                     WHERE b.datname=current_database() AND b.pid<>pg_backend_pid()) AS locks
                    FROM pg_stat_activity a WHERE a.datname=current_database()
                    AND a.pid<>pg_backend_pid()''').fetchone()
                samples.append({'elapsed_ms': round((time.monotonic() - started)*1000, 3), **row})
                if len(samples) >= 10000:
                    raise ValueError('sampling_limit')
                stop.wait(.1)
    except Exception as error:
        errors.append(type(error).__name__)
        ready.set()


def latency_summary(samples):
    durations = sorted(row['elapsed_ms'] for row in samples)
    if not durations:
        return {'count': 0, 'errors': 0, 'p50_ms': None, 'p95_ms': None, 'max_ms': None}
    return {'count': len(samples), 'errors': sum(row['status'] != 200 for row in samples),
        'p50_ms': durations[math.ceil(len(durations)*.50)-1],
        'p95_ms': durations[math.ceil(len(durations)*.95)-1], 'max_ms': durations[-1]}


def load_group(group, cycles, port):
    require(group in {'off', 'idle'}, 'real_active_driver_unavailable')
    require(type(cycles) is int and 1 <= cycles <= 100, 'cycles_out_of_bounds')
    settings = checked_settings()
    before = business_snapshot(settings)
    ledger_before = ledger_fingerprint()
    app = make_app(settings, group)
    samples, occupancy, sampler_errors, errors = [], [], [], []
    stop, ready = threading.Event(), threading.Event()
    sampler = threading.Thread(target=pg_sampler, args=(settings, stop, ready, occupancy,
        sampler_errors), name='d3-evaluation-pg', daemon=True)
    guard = NoModelTraffic()
    report = {'schema': 1, 'kind': 'read_only_business_load', 'group': group,
        'started_at': datetime.now(timezone.utc).isoformat(), 'cycles': cycles,
        'sequence': list(SEQUENCE), 'concurrency': 1, 'fixture': 'single-trip',
        'guide_registered': group == 'idle', 'pg_version': before['pg_version'],
        'python_version': sys.version.split()[0], 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'samples': samples, 'pg_samples': occupancy, 'errors': errors,
        'model_time_ms': None, 'filter_time_ms': None, 'delivery_time_ms': None,
        'active_group': 'not_measured', 'ai_task_denominator': 0, 'ai_completion_rate': None}
    with guard.installed():
        try:
            with running_server(app, port) as (url, server, server_thread):
                report['port'] = int(url.rsplit(':', 1)[1])
                with httpx.Client(base_url=url, trust_env=False, follow_redirects=False,
                                  timeout=12) as client:
                    response = client.post('/demo/sessions', json={'account': 'personal_alice'})
                    require(response.status_code == 201, 'evaluation_login_failed')
                    token = response.json()['access_token']
                    client.headers['Authorization'] = 'Bearer ' + token
                    try:
                        sampler.start()
                        require(ready.wait(5) and not sampler_errors, 'pg_sampler_unavailable')
                        for cycle in range(cycles):
                            for endpoint in SEQUENCE:
                                start = time.perf_counter()
                                status, failure = None, None
                                try:
                                    reply = client.get(endpoint)
                                    status = reply.status_code
                                except httpx.HTTPError as error:
                                    failure = type(error).__name__
                                samples.append({'cycle': cycle, 'endpoint': endpoint,
                                    'elapsed_ms': round((time.perf_counter()-start)*1000, 3),
                                    'status': status, 'failure': failure})
                    finally:
                        stop.set()
                        if sampler.ident is not None:
                            sampler.join(5)
                        if client.delete('/session').status_code != 204:
                            errors.append('logout_failed')
                        client.headers.pop('Authorization', None)
                        token = None
                if group == 'idle':
                    require(not app.state.guide.delegations and not app.state.guide.request_history,
                            'unexpected_guide_task')
            report['server_stopped'] = not server_thread.is_alive()
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            stop.set()
            if sampler.ident is not None:
                sampler.join(5)
    after = business_snapshot(settings)
    report.update({'summary': latency_summary(samples), 'sampler_errors': sampler_errors,
        'sampler_stopped': not sampler.is_alive(), 'sample_interval_ms': 100,
        'business_before': before['tables'], 'business_after': after['tables'],
        'business_unchanged': before['sha256'] == after['sha256'],
        'fixture_state_sha256': before['sha256'], 'authentication_before': before['authentication'],
        'authentication_after': after['authentication'],
        'ledger_files_unchanged': ledger_before == ledger_fingerprint(),
        'http_send_attempts': guard.send_attempts, 'reservation_attempts': guard.reserve_attempts,
        'zero_model_assertion_scope': 'this_process_off_or_idle_during_guard',
        'model_requests_sent_by_this_run': 0})
    report['run_valid'] = (len(samples) == cycles*len(SEQUENCE) and not errors and not sampler_errors
        and bool(occupancy) and report['summary']['errors'] == 0 and report['business_unchanged']
        and report['ledger_files_unchanged'] and guard.send_attempts == guard.reserve_attempts == 0
        and report['sampler_stopped'] and report.get('server_stopped') is True)
    return report


def verify_ticket(trip_id):
    """独立SQL只输出判据布尔值；不接受用户填写成功，不把判据送给模型。"""
    from services.business.db import connect, read_snapshot
    identifier = UUID(trip_id)
    settings = checked_settings()
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        conn.execute('SET TRANSACTION READ ONLY')
        rows = conn.execute('''SELECT t.revision, t.passengers, t.status AS trip_status,
            (l.flight_id=t.flight_id AND l.travel_date=t.travel_date) AS route_matches,
            k.certificate AS ticket_certificate, l.trip_revision AS lock_revision,
            l.amount_minor, l.status AS lock_status, p.amount_minor AS payment_amount,
            p.status AS payment_status, pp.outcome, pp.captured_minor, j.status AS job_status,
            tt.status AS task_status, k.quantity AS ticket_quantity, k.trip_revision AS ticket_revision,
            (k.payment_id=p.id AND k.trip_id=t.id AND tt.trip_id=t.id AND l.trip_id=t.id
             AND k.quantity=l.quantity) AS associations_match,
            (SELECT count(*) FROM payments x WHERE x.workspace_id=t.workspace_id AND x.trip_id=t.id
             AND x.status<>'failed') AS live_payments,
            (SELECT coalesce(sum(h.captured_minor),0) FROM provider_payments h
             JOIN payments x ON (x.workspace_id,x.id)=(h.workspace_id,h.operation_id)
             WHERE x.workspace_id=t.workspace_id AND x.trip_id=t.id) AS trip_captured_minor,
            NOT EXISTS(SELECT 1 FROM payments x WHERE x.workspace_id=t.workspace_id
             AND x.trip_id=t.id AND x.id<>p.id AND (x.status<>'failed' OR NOT EXISTS(
               SELECT 1 FROM provider_payments h WHERE (h.workspace_id,h.operation_id)=(x.workspace_id,x.id)
               AND h.outcome='failed' AND h.captured_minor=0))) AS prior_failed_zero,
            (SELECT count(*) FROM ticket_tasks x WHERE x.workspace_id=p.workspace_id AND x.payment_id=p.id) AS tasks,
            (SELECT count(*) FROM tickets x WHERE x.workspace_id=p.workspace_id AND x.payment_id=p.id) AS tickets
            FROM trips t JOIN payments p ON (p.workspace_id,p.trip_id)=(t.workspace_id,t.id)
            JOIN booking_locks l ON (l.workspace_id,l.id)=(p.workspace_id,p.lock_id)
            JOIN provider_payments pp ON (pp.workspace_id,pp.operation_id)=(p.workspace_id,p.id)
            JOIN payment_jobs j ON (j.workspace_id,j.payment_id)=(p.workspace_id,p.id)
            JOIN ticket_tasks tt ON (tt.workspace_id,tt.payment_id)=(p.workspace_id,p.id)
            JOIN tickets k ON (k.workspace_id,k.task_id)=(tt.workspace_id,tt.id)
            WHERE t.workspace_id='test' AND t.id=%s AND t.owner_id='personal_alice'
            AND t.organization_id IS NULL''', (identifier,)).fetchall()
        row = rows[0] if len(rows) == 1 else None
        bad_inventory = conn.execute('''SELECT count(*) AS n FROM flights f WHERE
            f.workspace_id='test' AND (f.reserved<0 OR f.sold<0 OR f.reserved+f.sold>f.capacity OR
            f.reserved<>(SELECT coalesce(sum(quantity),0) FROM booking_locks l
             WHERE (l.workspace_id,l.flight_id,l.travel_date)=(f.workspace_id,f.id,f.travel_date)
             AND l.status IN ('held','protected')) OR
            f.sold<>(SELECT coalesce(sum(quantity),0) FROM booking_locks l
             WHERE (l.workspace_id,l.flight_id,l.travel_date)=(f.workspace_id,f.id,f.travel_date)
             AND l.status='consumed'))''').fetchone()['n']
    checks = {'one_joined_ticket': row is not None, 'inventory_conserved': bad_inventory == 0}
    if row:
        checks.update({'current_revision': row['revision'] == row['lock_revision'] == row['ticket_revision'],
            'paid_trip': row['trip_status'] == 'paid',
            'frozen_route_matches_current_trip': row['route_matches'],
            'nonempty_ticket_certificate': bool(row['ticket_certificate'].strip()),
            'unique_payment_task_ticket': row['live_payments'] == row['tasks'] == row['tickets'] == 1,
            'prior_failed_payments_have_no_capture': row['prior_failed_zero'],
            'ticket_associations_match': row['associations_match'],
            'single_trip_reference_amount': row['passengers'] == 1 and row['captured_minor'] == 110000,
            'successful_payment_and_ticket': row['payment_status'] == row['outcome'] == row['task_status'] == 'succeeded'
                and row['ticket_quantity'] == row['passengers'],
            'consumed_lock_and_finished_job': row['lock_status'] == 'consumed' and row['job_status'] == 'done',
            'frozen_amount_captured_once': row['amount_minor'] == row['payment_amount']
                == row['captured_minor'] == row['trip_captured_minor']})
    return {'schema': 1, 'kind': 'independent_ticket_check',
        'checked_at': datetime.now(timezone.utc).isoformat(), 'object_sha256': digest(str(identifier)),
        'checks': checks, 'independent_ticket_valid': all(checks.values()),
        'model_quality': 'not_assessed', 'web_loop': 'not_assessed'}


def checked_plan(value):
    """工程共同规程，不进入GuideInput；不是模型选择的测试控制。"""
    from services.guide.contracts import safe_text, STEPS
    require(type(value) is dict and set(value) == {'task_ref', 'scenario', 'goal',
        'initial_state', 'event_schedule', 'feedback_rule', 'group_order'}, 'plan_fields')
    require(type(value['task_ref']) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value['task_ref']), 'plan_task_ref')
    require(value['scenario'] in {'normal', 'progress', 'recovery', 'unknown',
        'payment_pending', 'paid_no_ticket', 'ticket_failed'}, 'plan_scenario')
    require(type(value['initial_state']) is str and 1 <= len(value['initial_state']) <= 160, 'plan_initial_state')
    safe_text(value['initial_state'])
    require(value['feedback_rule'] == FEEDBACK_RULE and type(value['group_order']) is list
        and sorted(value['group_order']) == ['B0', 'B1', 'guide'], 'plan_common_conditions')
    events = value['event_schedule']
    require(type(events) is list and len(events) <= 20, 'plan_events')
    for event in events:
        require(type(event) is dict and set(event) == {'after_action', 'event'}
            and event['after_action'] in STEPS and event['event'] in {
                'none', 'process_payment_job', 'process_ticket_task', 'reconcile_payment'}, 'plan_event')
    prepare_input('B0', value['goal'])  # 原严格目标/资料契约；不发送。
    return value


def model_prerequisites():
    """只检查，不初始化费用记录、收费试探或接受CLI测试计数器。"""
    from services.guide.model import DeepSeekModelAdapter, InputTokenCounter, ModelConfig, ModelUnavailable
    from services.guide.windows import require_evidence, IsolationUnavailable
    from services.guide.budget import BudgetLedger, BudgetUnavailable
    from services.guide.coordinator import validate_guide_input
    missing = []
    try:
        require_evidence()
    except IsolationUnavailable:
        missing.append('isolation_unavailable')
    adapter = DeepSeekModelAdapter(input_validator=validate_guide_input)
    if not isinstance(adapter.token_counter, InputTokenCounter):
        missing.append('token_bound_unverified')
    try:
        ModelConfig.load()
    except ModelUnavailable as error:
        missing.append(error.reason)
    if not all(value is not None for value in ledger_fingerprint().values()):
        missing.append('ledger_not_ready')
    else:
        try:
            BudgetLedger(ROOT / '.local/guide-usage.json').receipt_snapshot()
        except BudgetUnavailable as error:
            missing.append(error.reason)
    return sorted(set(missing))


def receipt_projection(row):
    # 本地技术关联散列；不落供应商响应、真实对象/主体或账户字段。
    return {**{key: row[key] for key in ('phase', 'reserved_cny', 'additional_fee_cny',
        'state', 'accounted_cny', 'prompt_tokens', 'completion_tokens', 'cache_hit_tokens',
        'cache_miss_tokens', 'reason')}, 'reservation_ref': digest(row['request_id']),
        'provider_receipt_ref': digest(row['provider_request_id']) if row['provider_request_id'] else None}


_recorded_round = ContextVar('d3_evaluation_round', default=None)


class MeasuredTransport(httpx.AsyncBaseTransport):
    """工程侧包住原真实HTTP传输，不替换响应、不记录请求/响应内容。"""
    def __init__(self, row, active, origin, inner=None):
        self.row, self.active, self.origin = row, active, origin
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport(verify=True,
            trust_env=False, retries=0, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))

    def stamp(self):
        return round((time.monotonic() - self.origin)*1000, 3)

    async def handle_async_request(self, request):
        from services.guide.model import ENDPOINT
        require(request.method == 'POST' and str(request.url) == ENDPOINT, 'evaluation_destination_mismatch')
        self.row['network_attempts'] += 1
        self.row['network_started_ms'] = self.stamp()
        self.active.set()
        try:
            response = await self.inner.handle_async_request(request)
            self.row['network_headers_ms'] = self.stamp()
        except BaseException:
            self.row['network_closed_ms'] = self.stamp()
            self.active.clear()
            raise
        if response.is_closed:
            self.row['network_closed_ms'] = self.stamp()
            self.active.clear()
            return response
        transport, original = self, response.stream
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                try:
                    async for chunk in original:
                        yield chunk
                finally:
                    transport.row['network_closed_ms'] = transport.stamp()
                    transport.active.clear()

            async def aclose(self):
                try:
                    await original.aclose()
                finally:
                    transport.row['network_closed_ms'] = transport.stamp()
                    transport.active.clear()
        response.stream = Body()
        return response

    async def aclose(self):
        try:
            await self.inner.aclose()
        finally:
            self.active.clear()


@contextmanager
def measured_candidate_checks():
    """只计原解析/两次证据过滤的同步耗时；不复制或放宽原判据。"""
    from services.guide import coordinator as module
    def measured(function):
        def call(*args, **kwargs):
            start = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                row = _recorded_round.get()
                if row is not None:
                    row['candidate_check_ms'] += (time.perf_counter()-start)*1000
                    row['candidate_check_calls'] += 1
        return call
    with patch.object(module, 'parse_candidate', measured(module.parse_candidate)), patch.object(
            module, 'validate_evidence', measured(module.validate_evidence)):
        yield


def baseline_app(settings, group, trace, *, continuous=False, active=None, origin=None):
    from services.business.app import create_app
    from services.guide.coordinator import Coordinator, register_routes
    require(group in {'guide', 'B0', 'B1'}, 'unknown_group')
    class Recorded(Coordinator):
        async def work(self, request, delegation, round):
            if not continuous:
                return await super().work(request, delegation, round)
            require(len(trace['rounds']) < 100, 'evaluation_round_limit')
            row = {'round_ref': secrets.token_hex(8), 'network_attempts': 0,
                'receipts': [], 'receipts_verified': False, 'model_wait_ms': None,
                'candidate_check_ms': 0.0, 'candidate_check_calls': 0, 'snapshots_ms': [],
                'execution_status': 'in_progress', 'adapter_generate_started': False}
            trace['rounds'].append(row)
            request.state.d3_evaluation_round = row
            token = _recorded_round.set(row)
            start = time.perf_counter()
            try:
                result = await super().work(request, delegation, round)
                row['execution_status'] = 'candidate_returned'
                return result
            except BaseException as error:
                row['execution_status'] = 'rejected_or_interrupted'
                row['failure_class'] = type(error).__name__
                code = getattr(error, 'detail', None)
                if type(code) is str and re.fullmatch(r'[a-z_]{1,80}', code):
                    row['program_failure'] = code
                raise
            finally:
                row['work_elapsed_ms'] = round_number((time.perf_counter()-start)*1000)
                row['candidate_check_ms'] = round_number(row['candidate_check_ms'])
                # 独立23表对照由任务外的snapshot命令完成，不把额外SQL塞进30秒指引。
                row['business_unchanged_during_round'] = None
                _recorded_round.reset(token)

        async def snapshot(self, request, delegation, round):
            start = time.perf_counter()
            try:
                return await super().snapshot(request, delegation, round)
            finally:
                row = _recorded_round.get()
                if continuous and row is not None:
                    row['snapshots_ms'].append(round_number((time.perf_counter()-start)*1000))

        async def next(self, request, body, deadline=None):
            start = time.perf_counter()
            if continuous and hasattr(body, 'trip_id'):
                trace['task_attempts'] += 1
                if str(body.trip_id) != trace['_trip_id'] or body.goal != trace['host_only_plan']['goal']:
                    trace['task_scope_matches'] = False
            try:
                result = await super().next(request, body, deadline)
                if continuous and 'candidate' in result:
                    row = getattr(request.state, 'd3_evaluation_round', None)
                    if row is not None:
                        row.update(delivered_candidate=result['candidate'],
                            completion_verified=result['completion_verified'], source_kind=result['source_kind'])
                return result
            finally:
                row = getattr(request.state, 'd3_evaluation_round', None)
                if continuous and row is not None:
                    row['guide_post_ms'] = round_number((time.perf_counter()-start)*1000)

        def make_input(self, delegation, round, bundle, public):
            value = super().make_input(delegation, round, bundle, public)
            target = _recorded_round.get() if continuous else trace
            target['allowed_input'] = value
            target['knowledge_sha256'] = bundle.fingerprint
            return value

        def make_adapter(self):
            adapter = super().make_adapter()
            target = _recorded_round.get() if continuous else trace
            if continuous:
                adapter.transport = MeasuredTransport(target, active, origin)
            class Measured:
                async def generate(self, value, deadline):
                    from services.guide.budget import BudgetUnavailable
                    from services.guide.model import ModelUnavailable
                    try:
                        before = {row['request_id'] for row in adapter.ledger.receipt_snapshot()}
                    except BudgetUnavailable:
                        raise ModelUnavailable('evaluation_receipts_unavailable') from None
                    started = time.perf_counter()
                    target['adapter_generate_started'] = True
                    try:
                        return await adapter.generate(value, deadline)
                    finally:
                        target['model_wait_ms'] = round((time.perf_counter()-started)*1000, 3)
                        try:
                            target['receipts'] = [receipt_projection(row) for row in adapter.ledger.receipt_snapshot()
                                if row['request_id'] not in before]
                            target['receipts_verified'] = True
                        except BudgetUnavailable:
                            target['receipts_verified'] = False
                            raise ModelUnavailable('evaluation_receipts_unavailable') from None
            return Measured()
    coordinator = Recorded(settings, information_condition=group)
    with patch('services.guide.coordinator.register_routes',
            lambda app, config: register_routes(app, config, coordinator=coordinator)):
        return create_app(settings)


def round_number(value):
    return round(value, 3)


class RecordedWeb:
    """被动记录原ASGI路由模板/状态/送出耗时；不读取body、header或UUID路径。"""
    def __init__(self, app, trace, origin):
        self.app, self.trace, self.origin = app, trace, origin

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        start, send_ms, status = time.monotonic(), 0.0, None
        async def measured_send(message):
            nonlocal send_ms, status
            if message['type'] == 'http.response.start':
                status = message['status']
            began = time.perf_counter()
            try:
                await send(message)
            finally:
                send_ms += (time.perf_counter()-began)*1000
        try:
            await self.app(scope, receive, measured_send)
        finally:
            if len(self.trace['http_actions']) < 1000:
                row = scope.get('state', {}).get('d3_evaluation_round')
                self.trace['http_actions'].append({'method': scope['method'],
                    'route': getattr(scope.get('route'), 'path', 'unmatched'), 'status': status,
                    'started_ms': round_number((start-self.origin)*1000),
                    'elapsed_ms': round_number((time.monotonic()-start)*1000),
                    'asgi_send_ms': round_number(send_ms), 'round_ref': row['round_ref'] if row else None})
            elif 'http_record_limit' not in self.trace['errors']:
                self.trace['errors'].append('http_record_limit')


def active_reads(url, active, stop, samples, errors, origin):
    """同一原GET序列；只在真实传输窗口发读请求，无自动next或业务动作。"""
    token = None
    try:
        with httpx.Client(base_url=url, trust_env=False, follow_redirects=False, timeout=3) as client:
            response = client.post('/demo/sessions', json={'account': 'personal_alice'})
            require(response.status_code == 201, 'evaluation_login_failed')
            token = response.json()['access_token']
            client.headers['Authorization'] = 'Bearer ' + token
            try:
                while len(samples) < 30 and not stop.is_set():
                    if not active.wait(.05) or stop.is_set():
                        continue
                    endpoint = SEQUENCE[len(samples) % len(SEQUENCE)]
                    start = time.monotonic()
                    status, failure = None, None
                    try:
                        status = client.get(endpoint).status_code
                    except httpx.HTTPError as error:
                        failure = type(error).__name__
                    samples.append({'cycle': len(samples)//len(SEQUENCE), 'endpoint': endpoint,
                        'started_ms': round_number((start-origin)*1000),
                        'elapsed_ms': round_number((time.monotonic()-start)*1000),
                        'status': status, 'failure': failure, 'network_active_at_end': active.is_set()})
            finally:
                if client.delete('/session').status_code != 204:
                    errors.append('load_logout_failed')
                client.headers.pop('Authorization', None)
                token = None
    except Exception as error:
        errors.append('active_reads_' + type(error).__name__)


def web_session(group, trip_id, plan, port, seconds):
    """有界持续原网页工程宿主；只观察人工操作，不代操作或自动调用模型。"""
    checked_plan(plan)
    identifier = str(UUID(trip_id))
    require(group in {'guide', 'B0', 'B1'}, 'unknown_group')
    require(type(seconds) is int and 1 <= seconds <= 600, 'session_duration_out_of_bounds')
    from services.guide.model import MODEL, MODEL_REVISION, PROMPT_SHA256
    report = {'schema': 1, 'kind': 'continuous_original_web_task', 'group': group,
        'host_only_plan': plan, 'common_plan_sha256': digest(plan), 'object_sha256': digest(identifier),
        'model': MODEL, 'model_revision': MODEL_REVISION, 'prompt_sha256': PROMPT_SHA256,
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'rounds': [], 'http_actions': [], 'task_attempts': 0, 'task_scope_matches': True,
        'errors': [], 'pg_samples': [], 'active_read_samples': [], 'actual_bill_cny': None,
        'B1_and_guide_same_implementation': True, 'completion_rate': None,
        'started_at': datetime.now(timezone.utc).isoformat(), 'seconds_limit': seconds}
    missing = model_prerequisites()
    if missing:
        report.update(execution_status='stopped_before_dispatch', missing=missing,
            physical_reservations=0, ai_task_denominator=0, active_group='not_measured')
        return report
    settings = checked_settings()
    before = business_snapshot(settings)
    report['business_before'] = before
    report['_trip_id'] = identifier  # 仅内存临时绑定；finally移除，不落报告。
    stop, active, ready = threading.Event(), threading.Event(), threading.Event()
    origin = time.monotonic()
    sampler_errors, read_errors = [], []
    sampler = threading.Thread(target=pg_sampler, args=(settings, stop, ready, report['pg_samples'],
        sampler_errors, origin), name='d3-web-pg', daemon=True)
    reads = None
    app = baseline_app(settings, group, report, continuous=True, active=active, origin=origin)
    try:
        with measured_candidate_checks(), running_server(RecordedWeb(app, report, origin), port) as (url, _, server_thread):
            report['port'] = int(url.rsplit(':', 1)[1])
            sampler.start()
            require(ready.wait(5) and not sampler_errors, 'pg_sampler_unavailable')
            reads = threading.Thread(target=active_reads, args=(url, active, stop,
                report['active_read_samples'], read_errors, origin), name='d3-web-active-reads', daemon=True)
            reads.start()
            print(json.dumps({'original_web_api': url, 'seconds_limit': seconds,
                'group': group, 'manual_actions_only': True}), flush=True)
            try:
                until = origin + seconds
                while time.monotonic() < until and not sampler_errors and not read_errors:
                    stop.wait(min(.25, max(0, until-time.monotonic())))
            finally:
                # 读负载先退出并logout；不能等宿主关闭后再发logout。
                stop.set()
                for thread in (sampler, reads):
                    if thread.ident is not None:
                        thread.join(8)
    except BaseException as error:
        report['errors'].append(type(error).__name__)
    finally:
        stop.set()
        for thread in (sampler, reads):
            if thread is not None and thread.ident is not None:
                thread.join(5)
        report.pop('_trip_id', None)
    report['sampler_stopped'] = not sampler.is_alive()
    report['reads_stopped'] = reads is None or not reads.is_alive()
    report['server_stopped'] = 'server_thread' in locals() and not server_thread.is_alive()
    report['errors'].extend(sampler_errors + read_errors)
    try:
        report['business_after'] = business_snapshot(settings)
        report['independent_ticket'] = verify_ticket(identifier)
    except Exception as error:
        report['errors'].append('final_snapshot_' + type(error).__name__)
        report['independent_ticket'] = None
    report['physical_reservations'] = (sum(len(row['receipts']) for row in report['rounds'])
        if all(row['receipts_verified'] or not row['adapter_generate_started'] for row in report['rounds']) else None)
    report['ai_task_denominator'] = int(report['task_attempts'] > 0)
    report['task_counting'] = 'one_attempted_task_per_group_and_task_ref_not_per_POST'
    report['execution_status'] = 'session_recorded'
    report['ended_at'] = datetime.now(timezone.utc).isoformat()
    attempts = sum(row['network_attempts'] for row in report['rounds'])
    report['active_group'] = 'real_transport_samples_recorded' if attempts and report['active_read_samples'] else 'not_measured'
    report['active_load_summary'] = latency_summary(report['active_read_samples'])
    report['active_comparison_complete'] = (len(report['active_read_samples']) == 30
        and all(row['network_active_at_end'] for row in report['active_read_samples']) and not read_errors)
    report['metric_limits'] = ['100ms_pg_sampling_misses_short_occupancy', 'asgi_send_is_not_browser_receipt',
        'model_wait_includes_adapter_preparation_and_settlement', 'candidate_checks_exclude_IPC_and_final_snapshot',
        'active_GETs_may_span_multiple_real_calls_or_be_incomplete', 'passive_recording_adds_timing_overhead',
        'per_refusal_23_table_comparison_requires_separate_independent_snapshot_pair',
        'independent_ticket_does_not_prove_faithful_manual_actions_or_semantic_quality',
        'old_off_idle_require_same_environment_sequence_and_version_compatibility_review']
    report['run_valid'] = (not report['errors'] and report['task_scope_matches'] and report['sampler_stopped']
        and report['reads_stopped'] and report['server_stopped'] and report['task_attempts'] > 0
        and report['physical_reservations'] is not None and report['independent_ticket'] is not None)
    return report


def task_summary(paths):
    """只归并已保存的筛选工程轨迹；不访问模型/PG、不自判模型语义。"""
    require(1 <= len(paths) <= 100, 'summary_record_limit')
    tasks, sources, reservations, stopped = {}, [], {}, 0
    receipt_unknown, conflicts = False, []
    allowed = OUTPUT_ROOT.resolve()
    for source in paths:
        path = Path(source).resolve()
        require(path.parent == allowed and path.suffix == '.json', 'summary_source_outside_records')
        value = strict_json(path, limit=8*1024*1024)
        require(value.get('kind') in {'continuous_original_web_task', 'baseline_single_round'}, 'summary_record_kind')
        plan = checked_plan(value['host_only_plan'])
        require(value.get('group') in {'guide', 'B0', 'B1'}, 'summary_group')
        sources.append({'record': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        if value['execution_status'] == 'stopped_before_dispatch':
            stopped += 1
            continue
        require(value['execution_status'] in {'post_attempted', 'session_recorded'}, 'summary_incomplete_record')
        started = value.get('task_attempts', 1) > 0
        if not started:
            continue
        key = (value['group'], plan['task_ref'])
        task = tasks.setdefault(key, {'group': key[0], 'task_ref': key[1], 'plan_sha256': digest(plan),
            'records': [], 'independent_ticket_results': []})
        require(task['plan_sha256'] == digest(plan), 'summary_plan_conflict')
        task['records'].append(path.name)
        result = value.get('independent_ticket')
        task['independent_ticket_results'].append(result.get('independent_ticket_valid') if result else None)
        rows = value.get('rounds', [value])
        for row in rows:
            if row.get('adapter_generate_started') and not row.get('receipts_verified'):
                receipt_unknown = True
            for receipt in row.get('receipts', []):
                ref = receipt['reservation_ref']
                require(type(ref) is str and re.fullmatch(r'[a-f0-9]{64}', ref), 'summary_receipt_reference')
                require(receipt.get('state') in {'pending', 'unresolved', 'settled'}, 'summary_receipt_state')
                if ref in reservations and reservations[ref] != receipt:
                    conflicts.append(ref)
                else:
                    reservations[ref] = receipt
    return {'schema': 1, 'kind': 'continuous_task_summary', 'execution_status': 'reports_aggregated_only',
        'sources': sources, 'tasks': list(tasks.values()), 'ai_task_denominator': len(tasks),
        'stopped_before_dispatch_records': stopped,
        'physical_reservations_from_reports': None if receipt_unknown or conflicts else len(reservations),
        'receipt_association_unknown': receipt_unknown or bool(conflicts),
        'receipt_conflicts': sorted(set(conflicts)), 'actual_bill_cny': None,
        'unresolved_receipts': sum(row['state'] != 'settled' for row in reservations.values()),
        'model_quality': 'not_assessed', 'completion_rate': None,
        'limits': ['report_receipts_are_technical_records_not_supplier_bill',
            'ticket_result_alone_does_not_prove_faithful_web_trajectory_or_semantics',
            'task_denominator_deduplicated_by_group_task_ref_not_POST_or_plan_count']}


def baseline_round(group, trip_id, plan, feedback, detail, port):
    """一次真实原POST；人工原网页动作/任务控制/独立判定另行执行。

    多轮按同一task_ref关联，不能把每轮当新任务分母。B1与guide当前实现相同。
    """
    from services.guide.model import MODEL, MODEL_REVISION, PROMPT_SHA256
    identifier = str(UUID(trip_id))
    checked_plan(plan)
    require(group in {'guide', 'B0', 'B1'}, 'unknown_group')
    prepare_input('B0', plan['goal'], feedback=feedback, detail=detail)
    report = {'schema': 1, 'kind': 'baseline_single_round', 'group': group,
        'started_at': datetime.now(timezone.utc).isoformat(), 'host_only_plan': plan,
        'common_plan_sha256': digest(plan), 'object_sha256': digest(identifier),
        'model': MODEL, 'model_revision': MODEL_REVISION, 'prompt_sha256': PROMPT_SHA256,
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'B1_and_guide_same_implementation': True, 'receipts': [], 'receipts_verified': False,
        'adapter_generate_started': False, 'model_wait_ms': None,
        'independent_ticket_result': 'not_checked', 'web_loop': 'not_assessed',
        'model_semantic_correctness': 'not_assessed', 'actual_bill_cny': None}
    missing = model_prerequisites()
    if missing:
        report.update(execution_status='stopped_before_dispatch', missing=missing,
            physical_reservations=0, ai_task_denominator=0, completion_rate=None)
        return report
    settings = checked_settings()
    before = business_snapshot(settings)
    report['business_before'] = before['tables']
    report['authentication_before'] = before['authentication']
    app = baseline_app(settings, group, report)
    errors = []
    try:
        with running_server(app, port) as (url, server, thread):
            report['port'] = int(url.rsplit(':', 1)[1])
            with httpx.Client(base_url=url, trust_env=False, follow_redirects=False, timeout=35) as client:
                login = client.post('/demo/sessions', json={'account': 'personal_alice'})
                require(login.status_code == 201, 'evaluation_login_failed')
                token = login.json()['access_token']
                client.headers['Authorization'] = 'Bearer ' + token
                try:
                    start = time.perf_counter()
                    response = client.post('/guide/next', json={'action': 'next', 'trip_id': identifier,
                        'goal': plan['goal'], 'feedback': feedback, 'detail': detail,
                        'request_id': secrets.token_hex(16)})
                    report['post_elapsed_ms'] = round((time.perf_counter()-start)*1000, 3)
                    report['http_status'] = response.status_code
                    # 白名单筛选；不保存完整HTTP响应/会话/委托引用。
                    if response.status_code == 200:
                        body = response.json()
                        from services.guide.contracts import parse_candidate
                        report['delivered_candidate'] = parse_candidate(body['candidate']).model_dump()
                        report['completion_verified'] = body['completion_verified']
                        report['source_kind'] = body['source_kind']
                    else:
                        code = response.json().get('error')
                        report['program_failure'] = code if type(code) is str and re.fullmatch(r'[a-z_]{1,80}', code) else 'http_failure'
                finally:
                    if client.delete('/session').status_code != 204:
                        errors.append('logout_failed')
                    client.headers.pop('Authorization', None)
                    token = None
        report['server_stopped'] = not thread.is_alive()
    except Exception as error:
        errors.append(type(error).__name__)
    try:
        after = business_snapshot(settings)
        report.update(business_after=after['tables'], business_unchanged=before['sha256'] == after['sha256'],
            authentication_after=after['authentication'])
    except Exception as error:
        errors.append('final_snapshot_' + type(error).__name__)
        report.update(business_after=None, business_unchanged=None, authentication_after=None)
    report.update(execution_status='post_attempted', errors=errors,
        physical_reservations=(len(report['receipts'])
            if report['receipts_verified'] else None if report['adapter_generate_started'] else 0),
        ai_task_denominator=None, task_counting='deduplicate_group_and_task_ref_after_completed_trajectory',
        completion_rate=None)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare-input')
    prepare.add_argument('--group', choices=('B0', 'B1', 'guide'), required=True)
    prepare.add_argument('--goal', required=True)
    prepare.add_argument('--feedback', default='')
    prepare.add_argument('--detail', choices=('brief', 'detailed'), default='brief')
    prepare.add_argument('--observation')
    load = commands.add_parser('load')
    load.add_argument('--group', choices=('off', 'idle'), required=True)
    load.add_argument('--cycles', type=int, default=10)
    load.add_argument('--port', type=int, default=8011)
    verify = commands.add_parser('verify-ticket')
    verify.add_argument('--trip-id', required=True)
    baseline = commands.add_parser('baseline-round')
    baseline.add_argument('--group', choices=('guide', 'B0', 'B1'), required=True)
    baseline.add_argument('--trip-id', required=True)
    baseline.add_argument('--plan', required=True)
    baseline.add_argument('--feedback', default='')
    baseline.add_argument('--detail', choices=('brief', 'detailed'), default='brief')
    baseline.add_argument('--port', type=int, default=8011)
    web = commands.add_parser('web-session')
    web.add_argument('--group', choices=('guide', 'B0', 'B1'), required=True)
    web.add_argument('--trip-id', required=True)
    web.add_argument('--plan', required=True)
    web.add_argument('--port', type=int, default=8000)
    web.add_argument('--seconds', type=int, default=300)
    summary = commands.add_parser('task-summary')
    summary.add_argument('--records', nargs='+', required=True)
    snapshot = commands.add_parser('snapshot')
    for command in (prepare, load, verify, baseline, web, summary, snapshot):
        command.add_argument('--name', required=True, help='新记录名；不覆盖现有文件')
    args = parser.parse_args()
    def execute():
        if args.command == 'prepare-input':
            observation = strict_json(args.observation) if args.observation else None
            return prepare_input(args.group, args.goal, args.feedback, args.detail, observation)
        if args.command == 'load':
            require(1024 <= args.port <= 65535, 'loopback_port_out_of_bounds')
            return load_group(args.group, args.cycles, args.port)
        if args.command == 'baseline-round':
            require(1024 <= args.port <= 65535, 'loopback_port_out_of_bounds')
            return baseline_round(args.group, args.trip_id, strict_json(args.plan), args.feedback, args.detail, args.port)
        if args.command == 'web-session':
            require(1024 <= args.port <= 65535, 'loopback_port_out_of_bounds')
            return web_session(args.group, args.trip_id, strict_json(args.plan), args.port, args.seconds)
        if args.command == 'task-summary':
            return task_summary(args.records)
        if args.command == 'snapshot':
            return {'schema': 1, 'kind': 'independent_business_snapshot',
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'snapshot': business_snapshot(checked_settings()), 'model_requests': 0,
                'does_not_refresh_model_observation_or_authorization': True}
        return verify_ticket(args.trip_id)
    # 占用与持久初始记录在任何PG/服务/费用/模型操作之前，不用exists作互斥。
    path, result = record_run(args.name, execute, command=args.command)
    print(json.dumps({'record': str(path), 'kind': result['kind'],
        'run_valid': result.get('run_valid'), 'independent_ticket_valid': result.get('independent_ticket_valid')},
        ensure_ascii=False))
    return 1 if (result.get('run_valid') is False or result.get('independent_ticket_valid') is False
        or (args.command == 'web-session' and result['execution_status'] == 'stopped_before_dispatch')
        or (args.command == 'baseline-round' and (result['execution_status'] != 'post_attempted'
            or result.get('http_status') != 200 or result['errors'] or not result['business_unchanged']))) else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # 不打印SQL、连接配置、原始HTTP/令牌或异常正文。
        print(json.dumps({'evaluation_error': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
