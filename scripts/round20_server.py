"""20原网页离线运输验收：真实业务/HTTP，模型替身；不提供真实外发模式。"""
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import round20_sandbox as sandbox
from services.business.app import create_app
from services.business.config import Settings
from services.business.manage import reset_test_database
from services.guide.assistant import corporate_export_scope
from services.guide.model import ModelResult, TokenUsage
from services.guide.releases import Releases

OUT = ROOT / '.local/d3-20'
QUESTION = '我已获批准后改了出行日期，现在能直接付款吗？卡在哪，该找谁？请在当前页解释，不代操作。'
REPLY = ('当前行程已修改，原批准绑定旧修订，现已失效，不能直接用于当前付款。'
         '请先在原页面重新获取报价、核对锁价，再提交部门审批；由该行程当前指派的部门审批人决定。'
         '具体审批人姓名未提供，不能推测。预算项目已选择不代表余额充足；当前付款和有效票据均未产生。')


def configure():
    sandbox.bind_helper()
    sandbox.check_runner()
    config = sandbox.checked_config()
    sandbox.check_business(config)
    settings = Settings(config['test_database_url'], workspace_id='test', demo_login=True, mode='test')
    app = create_app(settings)
    assistant = app.state.assistant
    release_path = OUT / 'releases.json'
    if not release_path.exists():
        shutil.copyfile(ROOT / '.local/assistant-releases-test.json', release_path)
    releases = Releases('test', release_path)
    old = releases.active()
    new = releases.save_candidate(old['knowledge'], old['observations'], prompt_version='assistant-5')
    releases.publish(new['version'])
    assistant.releases = releases
    assistant.report_dir = OUT / 'offline-runs'
    assistant.external_model = False
    real_status = assistant.status
    assistant.status = lambda who: {**real_status(who), 'ready': True, 'reason': None}

    def factory(validator):
        async def generate(payload, deadline):
            validator(payload)
            context = json.loads(payload['messages'][-1]['content'])
            question = context['question']
            if question == QUESTION:
                preview = {'destination': 'https://api.deepseek.com/chat/completions',
                    'physical_requests_requested': 1, 'approved': False,
                    'scope_hash': corporate_export_scope(payload), 'payload': payload,
                    'input_bytes': len(json.dumps({'messages': payload['messages'], 'tools': payload['tools']},
                                                 ensure_ascii=False, separators=(',', ':')).encode('utf-8'))}
                (OUT / 'payload-preview.json').write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding='utf-8')
            if '晚到' in question:
                await asyncio.sleep(1.2)
            state = context['current_state']
            if state.get('selected_trip'):
                text = REPLY
            else:
                text = '本轮只允许查看当前指派的待办摘要；当前摘要为空，不能据此判断其他申请不存在，也不能读取申请人的付款或票据。'
            calls = ([{'id': 'offline-navigation', 'name': 'navigate', 'arguments': {'page': 'trips'}}]
                     if '导航' in question and state.get('selected_trip') else [])
            return ModelResult({'content': text, 'tool_calls': calls}, TokenUsage(100, 30, 130, 0, 100),
                               'offline-only', Decimal('0'), 'offline-only')
        return SimpleNamespace(generate_experiment=generate)
    assistant.adapter_factory = factory
    return app, settings


def prepare_sample(base_url='http://127.0.0.1:8000'):
    """仅在核验后的隔离库重置；所有业务准备通过真实TCP HTTP，不调用付款。"""
    import httpx
    sandbox.bind_helper()
    sandbox.check_runner()
    config = sandbox.checked_config()
    sandbox.check_business(config)
    with sandbox.runner_lock():
        reset_test_database(config['test_database_url'], confirmation='llm_business_test')
        with httpx.Client(base_url=base_url, trust_env=False, timeout=20) as client:
            tokens = []
            def login(account):
                result = client.post('/demo/sessions', json={'account': account})
                result.raise_for_status()
                headers = {'Authorization': 'Bearer ' + result.json()['access_token']}
                tokens.append(headers)
                return headers
            from uuid import uuid4
            def write(headers, path, body=None, method='POST'):
                result = client.request(method, path, headers={**headers, 'Idempotency-Key': str(uuid4())},
                                        json={} if body is None else body)
                result.raise_for_status()
                return result.json()
            try:
                chen = login('employee_chen')
                trip = write(chen, '/trips', {'flight_id': 'DEMO-PEK-SHA', 'travel_date': '2026-10-01', 'passengers': 1})
                write(chen, '/payment-profile', {'source_ref': 'demo_corporate_chen', 'expected_revision': 0}, 'PUT')
                quote = write(chen, f"/trips/{trip['id']}/quotes", {'expected_revision': 1})
                lock = write(chen, f"/quotes/{quote['id']}/lock")
                write(chen, f"/trips/{trip['id']}/budget", {'project_id': 'A-eng', 'expected_revision': 1,
                      'expected_selection_revision': 0}, 'PUT')
                approval = write(chen, f"/locks/{lock['id']}/approvals", {'expected_revision': 1})
                lin = login('approver_lin')
                write(lin, f"/approvals/{approval['id']}/decision", {'expected_revision': 1, 'decision': 'approved'})
                changed = write(chen, f"/trips/{trip['id']}", {'expected_revision': 1, 'travel_date': '2026-10-02'}, 'PATCH')
                assert changed['revision'] == 2
                from services.business.db import connect
                with connect(config['test_database_url']) as conn:
                    conn.execute('SET TRANSACTION READ ONLY')
                    state = conn.execute('SELECT trip_revision,status,resource_status FROM approval_requests').fetchall()
                    assert state == [{'trip_revision': 1, 'status': 'invalidated', 'resource_status': 'released'}]
                    counts = {table: conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']
                              for table in ('payments', 'ticket_tasks', 'tickets')}
                    assert counts == {'payments': 0, 'ticket_tasks': 0, 'tickets': 0}
                (OUT / 'sample.json').write_text(json.dumps({'trip_id': trip['id'], 'question': QUESTION,
                      'trip_revision': 2, 'approval': state, 'counts': counts, 'preparation': 'real_tcp_http_and_sql'},
                      ensure_ascii=False, indent=2), encoding='utf-8')
            finally:
                for headers in tokens:
                    assert client.delete('/session', headers=headers).status_code == 204
    print('隔离企业样本准备完成：旧批准修订1、当前行程修订2，付款/任务/票据均0。')


if __name__ == '__main__':
    if sys.argv[1:] == ['prepare-sample']:
        prepare_sample()
    elif not sys.argv[1:]:
        import uvicorn
        from scripts.d3_evaluate import NoModelTraffic
        app, _ = configure()
        guard = NoModelTraffic()
        try:
            with guard.installed():
                uvicorn.run(app, host='127.0.0.1', port=8000, access_log=False, proxy_headers=False)
        finally:
            (OUT / 'transport-guard.json').write_text(json.dumps({
                'model_send_attempts': guard.send_attempts, 'budget_reserve_attempts': guard.reserve_attempts,
                'model': 'offline_substitute'}, indent=2), encoding='utf-8')
    else:
        raise SystemExit('仅支持离线server或prepare-sample；没有真实模型模式。')
