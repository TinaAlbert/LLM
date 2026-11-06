"""V1 宿主接点：正式交付、一次确认、实时权限复核和整批停止。"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import time

from .action_contract import action_digest, confirm_evidence_delivery
from .knowledge import fingerprint
from .model import ModelUnavailable
from .page_observation import observation_payload
from .task_budget import confirm_q19_round, finish_q19_task, stop_q19_batch
from .v1_scope import BATCH, PersonalScope, GOALS, FEEDBACK, PAGES, reject


class PersonalRuntime:
    def __init__(self, assistant, trip_id, *, release, observations, ledger, directory, real_model):
        self.assistant, self.trip_id = assistant, str(trip_id)
        self.release, self.observations = deepcopy(release), deepcopy(observations)
        self.ledger, self.directory, self.real_model = ledger, directory, real_model
        self._chat, self._infer, self._state, self._ack = assistant.chat, assistant.infer, assistant.state, assistant.ack
        self.sequence, self.round = 1, 0
        self.owner = self.sid = self.pending = self.scope = None
        self.stopped = False
        self.task_started = None
        self._install_scope()
        assistant.chat, assistant.infer, assistant.state, assistant.ack = self.chat, self.infer, self.state, self.ack
        assistant.dynamic_export_authorizer = self.authorize_export if real_model else None
        assistant.external_model = real_model

    def _install_scope(self):
        self.scope = PersonalScope(self.release, self.observations, self.sequence, getattr(self.ledger, BATCH.lower() + "_path"))

    def save(self, name, value):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # 单次收据不可覆盖；latest 由调用者明确命名另行管理。
        with path.open('x', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, default=str)

    def stop(self, reason):
        self.stopped = True
        if self.real_model and getattr(self.ledger, BATCH.lower() + "_path").exists():
            stop_q19_batch(self.ledger, reason, batch=BATCH)
        if not (self.directory / 'stopped.json').exists():
            self.save('stopped.json', {'reason': reason, 'sequence': self.sequence, 'round': self.round})

    def expire_if_due(self):
        """工程客户端失联也持久停批；不等待下一次收费/确认请求触发。"""
        if (not self.stopped and self.task_started is not None
                and time.monotonic() - self.task_started >= 300):
            self.stop('task_deadline')
            return True
        return False

    def check(self):
        if self.stopped or (self.real_model and getattr(self.ledger, BATCH.lower() + "_stop_path").exists()):
            reject('batch_stopped')
        if self.task_started is not None and time.monotonic() - self.task_started >= 300:
            reject('task_deadline')
        active = self.assistant.releases.active()
        if not fingerprint(active) == fingerprint(self.release):
            reject('release_changed')
        if self.assistant.external_model is not self.real_model:
            reject('model_mode_changed')
        if self.real_model and self.assistant.dynamic_export_authorizer != self.authorize_export:
            reject('export_gate_changed')

    async def state(self, request, who, trip_id, deadline, delegated_trip_ids=()):
        if (str(trip_id) != self.trip_id or delegated_trip_ids or who.actor != 'personal_alice'
                or who.roles or who.allowed_pages != PAGES or (self.owner and who != self.owner)):
            reject('identity_or_object_changed')
        result = await self._state(request, who, trip_id, deadline)
        self.capture = (deepcopy(result[0]), result[1], who, time.monotonic() + 30)
        return result

    async def infer(self, payload, kind, version):
        self.check()
        if kind != 'chat' or version != self.release['version']:
            reject('chat_only')
        body = self.body
        session = next(s for s in self.assistant.sessions.values() if s.task is not None)
        raw, private_hash, owner, expires = self.capture
        context = self.scope.freeze(payload, raw, history=deepcopy(session.history[-4:]),
            question=body.message, page=body.page, observation=observation_payload(body.observation),
            round_index=self.round + 1)
        self.bound = {'raw': raw, 'private_hash': private_hash, 'owner': owner, 'expires': expires,
            'context': context['action_evidence']}
        self.save(f'{self.sequence}-{self.round+1:02d}-input.json', payload)
        try:
            return await self._infer(payload, kind, version)
        finally:
            self.scope.pending = None

    def authorize_export(self, payload):
        self.scope.check_actual(payload)

    async def chat(self, body, request):
        try:
            self.check()
            if self.pending is not None or body.delegated_trip_ids:
                reject('delivery_unconfirmed')
            if (str(body.trip_id) != self.trip_id or body.session_id != self.sid
                    or body.message != (GOALS[self.sequence-1] if self.round == 0 else FEEDBACK)):
                reject('task_input_changed')
            if self.task_started is None:
                self.task_started = time.monotonic()
            self.body = body
            formal = await self._chat(body, request)
            self.round += 1
            self.owner = self.bound['owner']
            self.sid = formal['session_id']
            self.pending = dict(self.bound, formal=deepcopy(formal), confirmed=False, ack=None)
            self.save(f'{self.sequence}-{self.round:02d}-formal.json', formal)
            return formal
        except BaseException:
            self.stop('formal_delivery_failed')
            raise

    async def ack(self, body, request):
        try:
            self.check()
            pending = self.pending
            if (not pending or pending['ack'] or body.session_id != self.sid
                    or pending['formal']['action_contract']['action']['kind'] != 'navigate'):
                reject('unexpected_ack')
            result = await self._ack(body, request)
            if result['navigation_status'] != 'succeeded':
                reject('navigation_failed')
            pending['ack'] = {'request': body.model_dump(mode='json', by_alias=True), 'response': result}
            self.save(f'{self.sequence}-{self.round:02d}-ack.json', pending['ack'])
            return result
        except BaseException:
            self.stop('navigation_failed')
            raise

    async def confirm(self, confirmation, request):
        """工程人员读取正式动作后调用，随后才能点击原网页；不执行业务。"""
        try:
            self.check()
            p = self.pending
            if not p or p['confirmed']:
                reject('confirmation_replay')
            who = await self.assistant.who(request, time.monotonic()+5)
            fresh_raw, private_hash = await self._state(request, who, self.body.trip_id, time.monotonic()+5)
            if (who != p['owner'] or who != self.owner or private_hash != p['private_hash']
                    or datetime.now(timezone.utc) >= datetime.fromisoformat(fresh_raw['valid_until'])):
                reject('confirmation_stale')
            action = confirm_evidence_delivery(p['formal'], confirmation, state=p['raw'],
                context=p['context'], allowed_pages=who.allowed_pages)
            # 原快照只证明原答复的忠实性；操作授权来自刚重新采集的同主体/对象/语义状态。
            self.check()
            p['expires'] = time.monotonic() + min(30, (datetime.fromisoformat(fresh_raw['valid_until']) - datetime.now(timezone.utc)).total_seconds())
            self.save(f'{self.sequence}-{self.round:02d}-revalidation.json', {
                'original_observed_at':p['raw']['observed_at'], 'fresh_observed_at':fresh_raw['observed_at'],
                'fresh_valid_until':fresh_raw['valid_until'], 'identity_object_state_unchanged':True,
                'old_snapshot_used_as_current_authorization':False})
            p['confirmed'] = True
            self.save(f'{self.sequence}-{self.round:02d}-confirmation.json', confirmation)
            return action
        except BaseException:
            self.stop('confirmation_failed')
            raise

    def complete(self, receipt, *, terminal=False):
        """只接受工程服务器从真实原API响应/DOM核验形成的回执。"""
        try:
            self.check()
            p = self.pending
            if not p or not p['confirmed'] or time.monotonic() >= p['expires']:
                reject('execution_unconfirmed')
            action = p['formal']['action_contract']['action']
            if receipt.get('kind') != action['kind'] or receipt.get('accepted') is not True:
                reject('execution_failed')
            if action['kind'] == 'navigate':
                if not p['ack'] or receipt.get('page') != action['params']['page'] or receipt.get('dom_verified') is not True:
                    reject('actual_navigation_required')
            if self.real_model:
                confirm_q19_round(self.ledger, self.scope.spec['task_ref'], self.round, batch=BATCH)
            self.save(f'{self.sequence}-{self.round:02d}-execution.json', receipt)
            if terminal:
                if self.real_model:
                    finish_q19_task(self.ledger, self.scope.spec['task_ref'], batch=BATCH)
                self.save(f'{self.sequence}-terminal.json', receipt)
                if self.sequence == len(GOALS):
                    self.stopped = True
                else:
                    self.sequence += 1
                    self.round = 0
                    self.task_started = None
                    self.sid = None
                    self._install_scope()
            elif self.round >= self.scope.spec['max_rounds']:
                reject('round_limit')
            self.pending = None
        except BaseException:
            self.stop('execution_or_terminal_failed')
            raise
