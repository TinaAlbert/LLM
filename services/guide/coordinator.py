"""D3-01单条个人按需任务；身份、网络和业务操作不交给模型。"""
import asyncio
import json
import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from pydantic import ValidationError

from services.business.app import fail
from .contracts import Cancel, Next, parse_candidate, validate_evidence
from .controls import Busy, Slot, delegation_slot, take_rate

PROMPT_VERSION = "personal-next-1"


def validate_guide_input(value):
    from services.business.guide_projection import validate_public
    from .knowledge import load_knowledge
    bundle = load_knowledge()
    if not isinstance(value, dict) or set(value) != {
            "goal", "feedback", "detail", "knowledge", "anchors", "observation", "prompt_version"}:
        raise ValueError("guide_input_fields")
    from .contracts import safe_text
    if not isinstance(value["goal"], str) or not 1 <= len(value["goal"]) <= 600:
        raise ValueError("guide_input_goal")
    safe_text(value["goal"])
    if not isinstance(value["feedback"], dict) or value["feedback"] != {
            "source_kind": "user_reported", "text": value["feedback"].get("text")}:
        raise ValueError("guide_input_feedback")
    text = value["feedback"]["text"]
    if not isinstance(text, str) or len(text) > 600:
        raise ValueError("guide_input_feedback")
    safe_text(text)
    if value["detail"] not in {"brief", "detailed"} or value["prompt_version"] != PROMPT_VERSION:
        raise ValueError("guide_input_version")
    if value["knowledge"] != bundle.knowledge or value["anchors"] != bundle.anchors:
        raise ValueError("guide_input_knowledge")
    if value["observation"] is not None:
        validate_public(value["observation"])
    return value


@dataclass
class Round:
    request_id: str
    parameters: dict
    deadline: float
    generation: int
    loop: object
    task: object = None
    waiters: int = 0
    snapshots: int = 0
    evidence: object = None


@dataclass
class Delegation:
    ref: str
    actor_id: str
    session_digest: str
    trip_id: object
    goal: str
    slot: object
    generation: int = 1
    cancelled: bool = False
    completed: bool = False
    delivered: bool = False
    seen_ids: dict = field(default_factory=dict)
    round: Round | None = None
    touched: float = field(default_factory=time.monotonic)


class Coordinator:
    def __init__(self, settings, *, information_condition="guide"):
        if information_condition not in {"guide", "B0", "B1"}:
            raise ValueError("information_condition_invalid")
        if information_condition != "guide" and (
                settings is None or settings.mode != "test" or settings.workspace_id != "test"):
            raise ValueError("baseline_requires_protected_test")
        self.settings = settings
        # 组别只能由可信工程构造；网页请求没有此字段。
        self.information_condition = information_condition
        self.delegations = {}
        # 同一登录中的请求冲突摘要；不保留目标/反馈/候选，退出时清理。
        self.request_history = {}
        # 长时IPC不占业务通用线程池；槽位先占后提交，无等待队列。
        self.db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="guide-db")
        self.ipc_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="guide-ipc")

    async def db(self, function, *args):
        with Slot(8):
            future = asyncio.get_running_loop().run_in_executor(self.db_pool, function, *args)
            return await self.finish_submitted(future)

    @staticmethod
    async def drain(future):
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break

    async def finish_submitted(self, future, cleanup=None):
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # 不把外层取消当线程/DB/进程已退出；完成真实有界回收后释放槽位。
            await self.drain(future)
            if cleanup and not future.cancelled() and future.exception() is None:
                closing = asyncio.get_running_loop().run_in_executor(self.ipc_pool, cleanup, future.result())
                await self.drain(closing)
            raise

    async def ipc(self, function, *args, cleanup=None):
        future = asyncio.get_running_loop().run_in_executor(self.ipc_pool, function, *args)
        return await self.finish_submitted(future, cleanup)

    def invalidate(self, delegation):
        delegation.cancelled = True
        delegation.generation += 1
        delegation.goal = ""
        delegation.seen_ids.clear()
        round = delegation.round
        if round and round.task and not round.task.done():
            round.loop.call_soon_threadsafe(round.task.cancel)
        else:
            self.remove(delegation)

    def remove(self, delegation):
        self.delegations.pop(delegation.ref, None)
        delegation.slot.close()
        delegation.goal = ""
        if delegation.round:
            delegation.round.parameters.clear()
            delegation.round.evidence = None

    def invalidate_session(self, digest):
        for delegation in list(self.delegations.values()):
            if delegation.session_digest == digest:
                self.invalidate(delegation)
        for key in list(self.request_history):
            if key[0] == digest:
                self.request_history.pop(key, None)

    def current(self, delegation, round):
        if (delegation.cancelled or delegation.generation != round.generation or
                self.delegations.get(delegation.ref) is not delegation or delegation.round is not round):
            fail(409, "delegation_changed")
        if round.evidence is not None and round.evidence.expired():
            fail(409, "observation_expired")
        if time.monotonic() >= round.deadline:
            fail(409, "guide_deadline")

    async def snapshot(self, request, delegation, round):
        from services.business.guide_projection import capture
        self.current(delegation, round)
        if round.snapshots >= 4:
            fail(503, "observation_limit")
        take_rate("observation")
        round.snapshots += 1
        evidence = await self.db(capture, self.settings, request, delegation.trip_id, round.deadline)
        if (evidence.actor_id != delegation.actor_id or evidence.session_digest != delegation.session_digest):
            fail(409, "session_changed")
        self.current(delegation, round)
        return evidence

    def make_input(self, delegation, round, bundle, public):
        return validate_guide_input({
            "goal": delegation.goal,
            "feedback": {"source_kind": "user_reported", "text": round.parameters["feedback"]},
            "detail": round.parameters["detail"], "knowledge": bundle.knowledge,
            "anchors": bundle.anchors,
            "observation": None if self.information_condition == "B0" else public,
            "prompt_version": PROMPT_VERSION})

    def make_adapter(self):
        from .model import DeepSeekModelAdapter
        return DeepSeekModelAdapter(input_validator=validate_guide_input)

    async def work(self, request, delegation, round):
        from services.guide.windows import begin_guide, IsolationUnavailable
        from .knowledge import load_knowledge
        from .model import ModelUnavailable
        session = None
        lane = None
        try:
            # 同一进程以及多个宿主均只有一个IPC/模型工作；不排队。
            lane = Slot(9)
            if lane:
                async with asyncio.timeout_at(round.deadline):
                    initial = await self.snapshot(request, delegation, round)
                    round.evidence = initial
                    bundle = load_knowledge()
                    session = await self.ipc(begin_guide, secrets.token_hex(16), round.deadline - time.monotonic(),
                                             cleanup=lambda created: created.abort())
                    before_send = await self.snapshot(request, delegation, round)
                    self.same(initial, before_send)
                    guide_input = self.make_input(delegation, round, bundle, initial.public)
                    self.current(delegation, round)
                    adapter = self.make_adapter()
                    result = await adapter.generate(guide_input, round.deadline)
                    candidate = parse_candidate(result.candidate)
                    if self.information_condition == "B0" and (
                            "observation:current" in candidate.references or candidate.amount_ref is not None):
                        fail(503, "candidate_rejected")
                    validate_evidence(candidate, initial.public, bundle.knowledge, bundle.anchors)
                    returned = await self.ipc(session.complete, candidate.model_dump())
                    if returned != candidate.model_dump():
                        fail(503, "candidate_rejected")
                    final = await self.snapshot(request, delegation, round)
                    self.same(initial, final)
                    self.current(delegation, round)
                    validate_evidence(candidate, final.public, bundle.knowledge, bundle.anchors)
                    anchor = bundle.anchors["anchors"].get(candidate.step_id)
                    amount = None
                    if candidate.amount_ref:
                        row = final.public["data"][candidate.amount_ref.split(".")[0]]
                        amount = {"amount_minor": row["amount_minor"], "currency": row["currency"]}
                    return {"delegation_ref": delegation.ref, "generation": delegation.generation,
                            "candidate": candidate.model_dump(), "entry": anchor,
                            "amount": amount, "observed_at": initial.observed_at.isoformat(),
                            "valid_until": initial.valid_until.isoformat(),
                            "completion_verified": self.information_condition != "B0" and candidate.status == "completion_candidate",
                            "knowledge_version": bundle.knowledge["knowledge_version"],
                            "source_kind": "unverified" if self.information_condition == "B0" else "observed",
                            "environment": "local-demo"}
        except IsolationUnavailable:
            fail(503, "isolation_unavailable")
        except ModelUnavailable as error:
            fail(503, error.reason)
        except (ValidationError, ValueError):
            fail(503, "candidate_rejected")
        except TimeoutError:
            fail(503, "guide_deadline")
        finally:
            try:
                if session:
                    await self.ipc(session.abort)
            finally:
                if lane:
                    lane.close()
                if delegation.cancelled:
                    self.remove(delegation)

    @staticmethod
    def same(old, new):
        if old.expired():
            fail(409, "observation_expired")
        if old.fingerprint != new.fingerprint:
            fail(409, "state_changed")

    async def next(self, request, body, deadline=None):
        from services.business.guide_projection import auth_only
        if deadline is None:
            deadline = time.monotonic() + 30
        try:
            identity = await self.db(auth_only, self.settings, request, deadline)
        except HTTPException as error:
            if error.status_code == 401:
                # 直接撤权/到期也可能在首个认证点被发现，尚未进入轮次清理区。
                from services.business.app import token_digest, token_from
                try:
                    token = token_from(request, optional=True)
                except HTTPException:
                    token = None
                if token is not None:
                    self.invalidate_session(token_digest(token))
            raise
        for old in list(self.delegations.values()):
            if not old.round or old.round.task.done():
                if time.monotonic() - old.touched > 300:
                    self.invalidate(old)
        if isinstance(body, Cancel):
            old = self.delegations.get(body.delegation_ref)
            if not old or (old.actor_id, old.session_digest) != (identity.actor_id, identity.session_digest):
                fail(404, "not_visible")
            self.invalidate(old)
            return {"status": "cancelled"}
        parameters = body.model_dump(mode="json", exclude={"delegation_ref"})
        parameter_digest = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
        request_key = (identity.session_digest, body.request_id)
        previous_request = self.request_history.get(request_key)
        if previous_request and previous_request[0] != parameter_digest:
            fail(409, "idempotency_conflict")
        old = self.delegations.get(body.delegation_ref) if body.delegation_ref else (
            self.delegations.get(previous_request[1]) if previous_request else None)
        if body.delegation_ref and old is None:
            fail(404, "not_visible")
        if old and (old.actor_id, old.session_digest) != (identity.actor_id, identity.session_digest):
            fail(404, "not_visible")
        if previous_request and (not old or not old.round or old.round.request_id != body.request_id):
            fail(409, "state_changed")
        if old and body.request_id in old.seen_ids:
            if old.seen_ids[body.request_id] != parameter_digest:
                fail(409, "idempotency_conflict")
            if not old.round or old.round.request_id != body.request_id:
                fail(409, "state_changed")
        if old and old.round and old.round.request_id == body.request_id:
            if old.round.parameters != parameters:
                fail(409, "idempotency_conflict")
            if old.round.task.done():
                fail(409, "state_changed")
            round = old.round
        else:
            if old and (old.trip_id != body.trip_id or old.goal != body.goal):
                self.invalidate(old)
                old = None
            if old and old.round and (not old.round.task.done() or old.round.waiters):
                fail(409, "guide_in_progress")
            if old and len(old.seen_ids) >= 100:
                fail(429, "request_history_limit")
            if len(self.request_history) >= 800:
                fail(429, "request_history_limit")
            take_rate("next")
            if old is None:
                old = Delegation(secrets.token_hex(16), identity.actor_id, identity.session_digest,
                                 body.trip_id, body.goal, delegation_slot())
                self.delegations[old.ref] = old
            old.seen_ids[body.request_id] = parameter_digest
            self.request_history[request_key] = (parameter_digest, old.ref)
            old.touched = time.monotonic()
            # 交付标记属于本物理轮次；上一轮成功不能使本轮失败后保留上下文。
            old.delivered = False
            round = Round(body.request_id, parameters, deadline, old.generation, asyncio.get_running_loop())
            old.round = round
            round.task = asyncio.create_task(self.work(request, old, round))
        if round.waiters >= 4:
            fail(429, "waiter_limit")
        round.waiters += 1
        try:
            while not round.task.done():
                if await request.is_disconnected():
                    raise asyncio.CancelledError()
                await asyncio.wait({round.task}, timeout=0.05)
            self.current(old, round)
            result = round.task.result()
            # 同时合并交付共享刚完成的最终授权快照；每个等待者仍查断连/代次/TTL。
            if await request.is_disconnected():
                raise asyncio.CancelledError()
            self.current(old, round)
            if result["completion_verified"]:
                old.completed = True
            old.delivered = True
            return result
        except HTTPException as error:
            if error.status_code == 401:
                # auth_only返回与logout钩子之间可能竞争；快照发现撤销后也清技术摘要。
                self.invalidate_session(identity.session_digest)
            raise
        except asyncio.CancelledError:
            fail(409, "delegation_changed")
        finally:
            round.waiters -= 1
            if round.waiters == 0:
                if not round.task.done():
                    self.invalidate(old)
                elif old.cancelled or old.completed or not old.delivered:
                    self.remove(old)
                else:
                    round.evidence = None
                    round.parameters.clear()
                    old.round = None
                    # 保存同标识参数只为冲突检查；旧候选不作私人缓存。

    def shutdown(self):
        for delegation in list(self.delegations.values()):
            self.invalidate(delegation)
        self.db_pool.shutdown(wait=True, cancel_futures=True)
        self.ipc_pool.shutdown(wait=True, cancel_futures=True)
        self.request_history.clear()

    async def aclose(self):
        tasks = [d.round.task for d in self.delegations.values() if d.round and d.round.task]
        for delegation in list(self.delegations.values()):
            self.invalidate(delegation)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.shutdown()


def register_routes(app, settings, *, coordinator=None):
    from services.business.guide_projection import ObservationTimeout
    if coordinator is None:
        coordinator = Coordinator(settings)
    elif not isinstance(coordinator, Coordinator) or coordinator.settings != settings:
        raise ValueError("coordinator_settings_mismatch")
    app.state.guide = coordinator

    @app.post("/guide/next")
    async def guide_next(request: Request):
        # 流式读取有界请求，不先接受任意大的JSON或未知字段。
        deadline = time.monotonic() + 30
        raw = bytearray()
        try:
            async with asyncio.timeout(3):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 8192:
                        fail(422, "invalid_request")
        except TimeoutError:
            fail(422, "invalid_request")
        try:
            def unique_pairs(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError("duplicate_field")
                    value[key] = item
                return value
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            if not isinstance(value, dict):
                raise ValueError()
            kind = value.get("action")
            if kind == "next":
                # JSON严格模式仅允许UUID的标准文本表示，其余字段不做隐式类型转换。
                body = Next.model_validate_json(json.dumps(value), strict=True)
            elif kind == "cancel":
                body = Cancel.model_validate(value)
            else:
                raise ValueError()
        except (ValueError, ValidationError, UnicodeError, RecursionError):
            fail(422, "invalid_request")
        try:
            return await coordinator.next(request, body, deadline)
        except Busy as error:
            fail(429, str(error))
        except ObservationTimeout:
            fail(503, "observation_timeout")

    previous = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with previous(application) as state:
            try:
                yield state
            finally:
                await coordinator.aclose()
    app.router.lifespan_context = lifespan
