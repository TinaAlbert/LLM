"""21轮原网页离线运输；固定合成实例，无真实模型模式。"""
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.round21_explore import configure_server, ordinary_session, write
from scripts import round20_sandbox as sandbox
from scripts.d3_evaluate import NoModelTraffic, business_snapshot
from services.business.manage import reset_test_database
from services.guide.model import ModelResult, TokenUsage
from services.guide.releases import Releases
from services.guide.assistant import corporate_export_scope

OUT = ROOT / '.local/d3-21'
STOP = OUT / 'server.stop'


def configure():
    app, settings = configure_server()
    path = OUT / 'releases.json'
    if not path.exists():
        shutil.copyfile(ROOT / '.local/assistant-releases-test.json', path)
    releases = Releases('test', path)
    old = releases.active()
    candidate = releases.save_candidate(old['knowledge'], old['observations'], prompt_version='assistant-6')
    releases.publish(candidate['version'])
    assistant = app.state.assistant
    assistant.releases = releases
    assistant.external_model = False
    assistant.report_dir = OUT / 'offline-runs'
    original_status = assistant.status
    assistant.status = lambda who: {**original_status(who), 'ready': True, 'reason': None}

    def factory(validator):
        async def generate(payload, deadline):
            validator(payload)
            context = json.loads(payload['messages'][-1]['content'])
            state = context['current_state']
            if '晚到' in context['question']:
                await asyncio.sleep(1.2)
            if state.get('contract_version') == 'assistant-shared-1':
                reply = ('本次共同核对两个明确委托的本人行程。两个预报价中的券优惠只是估算，'
                         '当前券可竞争，尚未确定最终归属，不能把同一张券重复使用。'
                         '请比较两个行程的基础金额和优惠上限；锁价后重新核对另一个行程。'
                         '目前没有付款或有效票据，两项目标尚未完成。')
                preview = {'assistant_source_sha256': __import__('hashlib').sha256((ROOT / 'services/guide/assistant.py').read_bytes()).hexdigest(),
                    'destination': 'https://api.deepseek.com/chat/completions', 'approved': False,
                    'scope_hash': corporate_export_scope(payload), 'payload': payload,
                    'physical_requests_requested': 0,
                    'input_bytes': len(json.dumps({'messages': payload['messages'], 'tools': payload['tools']},
                        ensure_ascii=False, separators=(',', ':')).encode('utf-8'))}
                from uuid import uuid4
                (OUT / ('payload-preview-' + uuid4().hex + '.json')).write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding='utf-8')
            else:
                reply = '本次只核对当前对象。缺少双行程的明确委托，不能推断另一行程的状态。'
            calls = ([{'id': 'offline-navigation', 'name': 'navigate', 'arguments': {'page': 'trips'}}]
                     if '导航' in context['question'] else [])
            return ModelResult({'content': reply, 'tool_calls': calls}, TokenUsage(100, 40, 140, 0, 100),
                               'offline-only', Decimal('0'), 'offline-only')
        return SimpleNamespace(generate_experiment=generate)
    assistant.adapter_factory = factory
    return app, settings


def prepare_sample():
    import httpx
    from scripts.round21_explore import configuration, SERVER_MARKER, TARGET
    config, settings = configuration()
    with sandbox.runner_lock(), httpx.Client(base_url=TARGET, trust_env=False, timeout=15) as client:
        marker = json.loads(SERVER_MARKER.read_text(encoding='utf-8'))
        assert client.get('/__round21/binding').json() == marker
        assert marker['instance'] == sandbox.check_cluster()['instance_id']
        with ordinary_session(client, settings):
            pass
        reset_test_database(config['test_database_url'], confirmation='llm_business_test', fixture='two-trips-one-coupon')
        with ordinary_session(client, settings) as headers:
            write(client, headers, '/payment-profile', {'source_ref': 'demo_personal_alice', 'expected_revision': 0}, 'PUT')
            trips = [write(client, headers, '/trips', {'flight_id': 'DEMO-' + size,
                     'travel_date': '2026-10-01', 'passengers': 1})['id'] for size in ('LARGE', 'SMALL')]
            for trip in trips:
                write(client, headers, f'/trips/{trip}/quotes', {'expected_revision': 1})
        snapshot = business_snapshot(settings)
        assert snapshot['tables']['payments']['rows'] == snapshot['tables']['tickets']['rows'] == 0
        (OUT / 'sample.json').write_text(json.dumps({'trip_ids': trips, 'business_before': snapshot,
            'preparation': 'real_tcp_http_and_independent_sql', 'model_calls': 0}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('双行程合成样本已准备；仅预报价，付款/票据0。')


if __name__ == '__main__':
    if sys.argv[1:] == ['prepare-sample']:
        prepare_sample()
    elif not sys.argv[1:]:
        import uvicorn
        app, settings = configure()
        guard = NoModelTraffic()
        if STOP.exists():
            raise SystemExit('本轮停止标记已存在；先核对旧进程已退出再由工程侧移除标记。')
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8000,
                                access_log=False, proxy_headers=False))
        async def serve():
            task = asyncio.create_task(server.serve())
            while not task.done():
                if STOP.exists():
                    server.should_exit = True
                await asyncio.sleep(0.2)
            await task
        try:
            with guard.installed():
                asyncio.run(serve())
        finally:
            (OUT / 'server-transport-guard.json').write_text(json.dumps({'model_send_attempts': guard.send_attempts,
                'budget_reserve_attempts': guard.reserve_attempts, 'model': 'offline_substitute'}, indent=2), encoding='utf-8')
    else:
        raise SystemExit('只支持离线server或prepare-sample；无真实模型模式。')
