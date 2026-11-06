"""Q15已批准的单个企业样本：原正式Assistant/账本/HTTP，最多一次，不重试。"""
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import round20_sandbox as sandbox
from services.business.app import create_app
from services.business.config import Settings
from services.guide.assistant import corporate_export_scope, authorize_corporate_export
from services.guide.budget import BudgetLedger
from services.guide.experiment import load_approval
from services.guide.knowledge import fingerprint
from services.guide.releases import Releases

OUT = ROOT / '.local/d3-20'


def configure():
    sandbox.bind_helper()
    sandbox.check_runner()
    config = sandbox.checked_config()
    sandbox.check_business(config)
    preview = json.loads((OUT / 'payload-preview.json').read_text(encoding='utf-8'))
    decision = json.loads((OUT / 'q15-decision.json').read_text(encoding='utf-8'))
    assert decision['source_kind'] == 'explicit_user_reply' and decision['approved'] is True
    assert decision['scope_hash'] == preview['scope_hash']
    assert not (OUT / 'q15-attempted.json').exists(), 'Q15_no_automatic_retry'
    assert not (OUT / 'q15-server.stop').exists(), 'Q15_previous_stop_record_present'
    load_approval()
    budget = BudgetLedger(ROOT / '.local/guide-usage.json').summary()
    assert budget['physical_requests'] == 21 and budget['unresolved_requests'] == 0
    approved = json.loads((OUT / 'corporate-export-approval.json').read_text(encoding='utf-8'))
    assert approved['scope_hash'] == preview['scope_hash']
    settings = Settings(config['test_database_url'], workspace_id='test', demo_login=True, mode='test')
    app = create_app(settings)
    assistant = app.state.assistant
    assistant.releases = Releases('test', OUT / 'releases.json')
    assert assistant.releases.active()['prompt_version'] == 'assistant-5'
    assistant.report_dir = OUT / 'live-runs'
    original_infer = assistant.infer

    async def one_shot(payload, kind, version):
        authorize_corporate_export(payload)
        assert kind == 'chat' and payload['max_tokens'] == 512
        assert corporate_export_scope(payload) == preview['scope_hash'], 'Q15_scope_changed'
        budget = BudgetLedger(ROOT / '.local/guide-usage.json').summary()
        assert budget['physical_requests'] == 21 and budget['unresolved_requests'] == 0
        # 任何同范围第二次请求在此拒绝；网络异常/取消也不自动重发。
        with (OUT / 'q15-attempted.json').open('x', encoding='utf-8') as handle:
            json.dump({'source': 'Q15_explicit_user_approval', 'scope_hash': preview['scope_hash'],
                       'input_hash': fingerprint(payload), 'kind': kind, 'version': version,
                       'started_at_epoch': time.time()}, handle, ensure_ascii=False, indent=2)
        with (OUT / 'payload-live.json').open('x', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return await original_infer(payload, kind, version)
    assistant.infer = one_shot

    @app.middleware('http')
    async def writes_closed(request, call_next):
        from starlette.responses import JSONResponse
        allowed = {('/demo/sessions', 'POST'), ('/session', 'DELETE'),
                   ('/assistant/chat', 'POST'), ('/assistant/ack', 'POST')}
        if request.method not in {'GET', 'HEAD'} and (request.url.path, request.method) not in allowed:
            return JSONResponse({'error': 'q15_business_writes_disabled'}, status_code=403)
        return await call_next(request)
    return app


if __name__ == '__main__':
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(configure(), host='127.0.0.1', port=8000,
        access_log=False, proxy_headers=False, log_level='warning'))
    def stop_when_requested():
        while not server.should_exit:
            if (OUT / 'q15-server.stop').exists():
                server.should_exit = True
                return
            time.sleep(.2)
    threading.Thread(target=stop_when_requested, daemon=True).start()
    server.run()
