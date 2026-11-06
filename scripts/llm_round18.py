"""第18轮工程侧配对驱动：原Assistant授权/守卫/回执，stdio仅连接工程浏览器。

不提供HTTP入口，不发布临时Prompt，不初始化账本。每个配对共用30秒投影。
浏览器令牌只经管道留内存；模型输入和独立SQL判据分开。
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi import HTTPException
from starlette.requests import Request
from services.business.config import load_settings
from services.guide.assistant import Assistant, Chat, Ack
from services.guide.budget import BudgetLedger
from services.guide.experiment import load_approval
from services.guide.knowledge import fingerprint
from services.guide.model import DeepSeekModelAdapter, ModelUnavailable
from services.guide.releases import Releases

OUT = ROOT / '.local/d3-18'


def save_new(name, value):
    with (OUT / name).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)


class ExperimentRelease:
    """仅本配对内把原知识组合assistant-3；原发布文件与版本不修改。"""
    def __init__(self):
        self.source = Releases('test')
        self.original = self.source.active()

    def active(self):
        current = self.source.active()
        if current != self.original:
            raise ValueError('paired_release_changed')
        return {**deepcopy(current), 'prompt_version': 'assistant-3'}

    def read(self):
        value = deepcopy(self.source.read())
        self.active()
        value['candidates'][self.original['version']]['prompt_version'] = 'assistant-3'
        return value


class PairAssistant(Assistant):
    def __init__(self):
        super().__init__(load_settings(test=True), releases=ExperimentRelease(),
                         report_dir=OUT / 'pair-runs')
        self.frozen = None
        self.frozen_hash = None
        self.owner = None
        self.frozen_deadline = None
        self.inputs = []
        self.calls = 0

    def remaining(self):
        if self.frozen is None:
            raise ValueError('paired_state_missing')
        wall_remaining = (datetime.fromisoformat(self.frozen['valid_until']) - datetime.now(timezone.utc)).total_seconds()
        return min(wall_remaining, self.frozen_deadline - time.monotonic())

    async def state(self, request, who, trip_id, deadline):
        capture_started = time.monotonic()
        actual, actual_hash = await super().state(request, who, trip_id, deadline)
        if self.frozen is None:
            self.frozen, self.frozen_hash, self.owner = deepcopy(actual), actual_hash, who
            self.frozen_deadline = capture_started + 30
        if actual_hash != self.frozen_hash or who != self.owner:
            raise ValueError('paired_state_changed')
        if self.remaining() <= 0:
            raise ValueError('paired_observation_expired')
        return deepcopy(self.frozen), self.frozen_hash

    async def infer(self, payload, kind, version):
        if self.calls >= 2 or self.remaining() <= 1:
            raise ValueError('paired_limit_or_expired')
        value = deepcopy(payload)
        context = json.loads(value['messages'][-1]['content'])
        if context['history']:
            raise ValueError('paired_history_not_empty')
        if self.calls == 0:
            enhanced = deepcopy(value)
            context['page_observation'].pop('semantics')
            value['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
            self.inputs = [deepcopy(value), enhanced]
            save_new('pair-inputs.json', {
                'source_release': version, 'published_prompt': self.releases.original['prompt_version'],
                'experimental_prompt': 'assistant-3', 'temporary_release_published': False,
                'only_difference': 'page_observation.semantics',
                'state_fingerprint': self.frozen_hash, 'payloads': self.inputs,
                'input_hashes': [fingerprint(x) for x in self.inputs]})
        elif value != self.inputs[1]:
            raise ValueError('paired_other_factor_changed')
        expires = time.monotonic() + self.remaining()

        class DeadlineAdapter(DeepSeekModelAdapter):
            async def generate_experiment(inner, experiment_input, deadline, **kwargs):
                return await super().generate_experiment(experiment_input, min(deadline, expires), **kwargs)

        self.adapter_factory = lambda validator: DeadlineAdapter(input_validator=validator)
        self.calls += 1
        return await super().infer(value, 'paired_labels' if self.calls == 1 else 'paired_semantics', version)


async def request_for(token):
    if type(token) is not str or not token.startswith('Bearer '):
        raise ValueError('browser_authorization_required')
    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    return Request({'type': 'http', 'headers': [(b'authorization', token.encode())],
                    'client': ('127.0.0.1', 1), 'method': 'POST', 'path': '/assistant/chat'}, receive)


async def bridge():
    OUT.mkdir(exist_ok=True)
    load_approval()
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    before = ledger.summary()
    if before['physical_requests'] != 12 or before['unresolved_requests']:
        raise ValueError('paired_original_twelve_required')
    assistant = PairAssistant()
    save_new('pair-host.json', {'state': 'started', 'budget_before': before})
    events = []
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            command = json.loads(line)
            op = command.get('op')
            if op == 'close':
                break
            try:
                request = await request_for(command.get('authorization'))
                if op == 'chat':
                    body = dict(command['body'])
                    body.pop('session_id', None)  # 两组均从空历史开始。
                    # 前一工具必须已由真实浏览器回执，再开始第二组。
                    if any(session.pending for session in assistant.sessions.values()):
                        raise ValueError('paired_navigation_unconfirmed')
                    result = await assistant.chat(Chat.model_validate(body), request)
                elif op == 'ack':
                    result = await assistant.ack(Ack.model_validate(command['body']), request)
                else:
                    raise ValueError('unsupported_engineering_operation')
                response = {'status': 200, 'body': result}
                events.append({'operation': op, 'status': 200,
                               'reply': result.get('reply'), 'tool_page': result.get('tool', {}).get('arguments', {}).get('page')})
            except (HTTPException, ValueError, ModelUnavailable) as exc:
                reason = getattr(exc, 'reason', None) or getattr(exc, 'detail', None) or str(exc)
                response = {'status': getattr(exc, 'status_code', 409), 'body': {'detail': reason}}
                events.append({'operation': op, 'status': response['status'], 'reason': reason})
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        value = {'state': 'finished', 'events': events, 'budget_before': before,
                 'budget_after': ledger.summary(), 'infer_attempts': assistant.calls,
                 'all_tool_receipts_received': not any(s.pending for s in assistant.sessions.values()),
                 'limitation': '工程stdio宿主复用原Assistant；临时Prompt只用于配对，未发布；模型提案由原网页执行。'}
        (OUT / 'pair-host.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    if sys.argv[1:] != ['bridge']:
        raise SystemExit('Usage: python scripts/llm_round18.py bridge (engineering browser stdin only)')
    asyncio.run(bridge())
