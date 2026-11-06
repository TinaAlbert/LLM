"""无费用控制检查；真实执行线程回收，不计模型或PG成绩。"""
import asyncio
import threading
import time
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from services.guide.coordinator import Coordinator
from services.guide.controls import Busy, Slot
from services.guide.contracts import Next


def test_cancelled_db_await_retains_slot_until_actual_thread_finishes():
    async def run():
        state = Coordinator(None)
        entered, release = threading.Event(), threading.Event()
        def blocking():
            entered.set()
            assert release.wait(3)
        pending = asyncio.create_task(state.db(blocking))
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(.005)
            pending.cancel()
            await asyncio.sleep(.02)
            assert not pending.done()
            with pytest.raises(Busy):
                Slot(8)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            with Slot(8):
                pass
        finally:
            release.set()
            await state.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('expired', [False, True])
def test_each_delivery_rechecks_ttl_and_success_releases_old_candidate(monkeypatch, expired):
    async def run():
        state = Coordinator(None)
        identity = SimpleNamespace(actor_id='synthetic-owner', session_digest='synthetic-session')
        async def db(*args): return identity
        state.db = db
        monkeypatch.setattr('services.guide.coordinator.take_rate', lambda *args: None)
        started, release = asyncio.Event(), asyncio.Event()
        evidence = SimpleNamespace(expired=lambda: expired)
        async def work(request, delegation, round):
            round.evidence = evidence
            started.set()
            await release.wait()
            return {'completion_verified': False, 'delegation_ref': delegation.ref}
        state.work = work
        class Receiver:
            async def is_disconnected(self): return False
        body = Next(action='next', trip_id=UUID(int=1), goal='模拟预订', request_id='delivery')
        pending = asyncio.create_task(state.next(Receiver(), body))
        try:
            await started.wait()
            delegation = next(iter(state.delegations.values()))
            round = delegation.round
            release.set()
            if expired:
                with pytest.raises(HTTPException) as error:
                    await pending
                assert error.value.detail == 'observation_expired'
                assert not state.delegations
            else:
                assert (await pending)['delegation_ref'] == delegation.ref
                assert delegation.round is None and round.parameters == {} and round.evidence is None
        finally:
            await state.aclose()
    asyncio.run(run())


def test_same_round_recheck_detects_replaced_generation_or_round():
    state = Coordinator(None)
    delegation = SimpleNamespace(ref='r', cancelled=False, generation=1, round=None)
    state.delegations['r'] = delegation
    round = SimpleNamespace(generation=1, evidence=None, deadline=time.monotonic()+30)
    try:
        with pytest.raises(HTTPException) as error:
            state.current(delegation, round)
        assert error.value.detail == 'delegation_changed'
    finally:
        state.delegations.clear()
        state.shutdown()


@pytest.mark.parametrize('outcome', ['delivered', 'failed', 'completed'])
def test_no_reference_retry_after_round_cleanup_does_not_create_another_call(monkeypatch, outcome):
    async def run():
        state = Coordinator(None)
        identity = SimpleNamespace(actor_id='synthetic-owner', session_digest='synthetic-session')
        async def db(*args): return identity
        state.db = db
        monkeypatch.setattr('services.guide.coordinator.take_rate', lambda *args: None)
        calls = []
        async def work(request, delegation, round):
            calls.append(delegation.ref)
            if outcome == 'failed':
                raise HTTPException(503, 'controlled_failure')
            return {'completion_verified': outcome == 'completed', 'delegation_ref': delegation.ref}
        state.work = work
        class Receiver:
            async def is_disconnected(self): return False
        body = Next(action='next', trip_id=UUID(int=1), goal='模拟预订', request_id='lost-response')
        try:
            if outcome == 'failed':
                with pytest.raises(HTTPException): await state.next(Receiver(), body)
            else:
                await state.next(Receiver(), body)
            with pytest.raises(HTTPException) as retry:
                await state.next(Receiver(), body)
            assert retry.value.detail == 'state_changed'
            different = body.model_copy(update={'goal':'换个目标'})
            with pytest.raises(HTTPException) as conflict:
                await state.next(Receiver(), different)
            assert conflict.value.detail == 'idempotency_conflict' and len(calls) == 1
            state.invalidate_session(identity.session_digest)
            assert state.request_history == {}
        finally:
            await state.aclose()
    asyncio.run(run())


def test_logout_between_authentication_and_delegate_creation_leaves_no_private_history(monkeypatch):
    async def run():
        state = Coordinator(None)
        identity = SimpleNamespace(actor_id='synthetic-owner', session_digest='revoked-session')
        async def db(*args):
            # 认证线程完成，但尚未恢复next时，退出钩子已执行。
            state.invalidate_session(identity.session_digest)
            return identity
        state.db = db
        monkeypatch.setattr('services.guide.coordinator.take_rate', lambda *args: None)
        async def work(*args):
            # 真实首个快照会重新认证；这里仅控制401/摘要回收协议，不代替PG证据。
            raise HTTPException(401, 'authentication_required')
        state.work = work
        class Receiver:
            async def is_disconnected(self): return False
        body = Next(action='next', trip_id=UUID(int=1), goal='模拟预订', request_id='revoked-race')
        try:
            with pytest.raises(HTTPException) as error:
                await state.next(Receiver(), body)
            assert error.value.status_code == 401
            assert state.request_history == {} and state.delegations == {}
        finally:
            await state.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['failed', 'disconnected'])
def test_failed_continuation_does_not_inherit_previous_delivery(monkeypatch, outcome):
    async def run():
        state = Coordinator(None)
        identity = SimpleNamespace(actor_id='synthetic-owner', session_digest='synthetic-session')
        async def db(*args): return identity
        state.db = db
        monkeypatch.setattr('services.guide.coordinator.take_rate', lambda *args: None)
        calls = []
        async def work(request, delegation, round):
            calls.append(round.request_id)
            if len(calls) == 2:
                if outcome == 'failed':
                    raise HTTPException(503, 'controlled_failure')
                request.disconnected = True
            return {'completion_verified': False, 'delegation_ref': delegation.ref}
        state.work = work
        class Receiver:
            disconnected = False
            async def is_disconnected(self): return self.disconnected
        body = Next(action='next', trip_id=UUID(int=1), goal='模拟预订', request_id='first')
        try:
            result = await state.next(Receiver(), body)
            old = state.delegations[result['delegation_ref']]
            assert old.delivered and old.goal == body.goal
            continued = body.model_copy(update={'request_id': 'continued', 'delegation_ref': old.ref})
            with pytest.raises(HTTPException) as failure:
                await state.next(Receiver(), continued)
            assert failure.value.detail == ('controlled_failure' if outcome == 'failed' else 'delegation_changed')
            assert state.delegations == {} and old.goal == ''
            assert old.round.parameters == {} and old.round.evidence is None
            # 无委托重试也不能因失败清理变成新的物理调用。
            with pytest.raises(HTTPException) as replay:
                await state.next(Receiver(), continued.model_copy(update={'delegation_ref': None}))
            assert replay.value.detail == 'state_changed' and calls == ['first', 'continued']
        finally:
            await state.aclose()
    asyncio.run(run())


def test_initial_auth_rejection_clears_only_matching_session(monkeypatch):
    from services.business.app import token_digest
    from services.guide.coordinator import Delegation
    from services.guide.controls import delegation_slot
    from starlette.requests import Request
    async def run():
        state = Coordinator(None)
        digest = token_digest('synthetic-revoked-token')
        old = Delegation('a' * 32, 'synthetic-owner', digest, UUID(int=1), '模拟预订', delegation_slot())
        state.delegations[old.ref] = old
        state.request_history[(digest, 'previous')] = ('synthetic-digest', old.ref)
        state.request_history[('other-session', 'other-request')] = ('other-digest', 'b' * 32)
        async def rejected(*args): raise HTTPException(401, 'authentication_required')
        state.db = rejected
        request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer synthetic-revoked-token')]})
        body = Next(action='next', trip_id=UUID(int=1), goal='模拟预订', request_id='revoked')
        try:
            with pytest.raises(HTTPException) as failure:
                await state.next(request, body)
            assert failure.value.status_code == 401
            assert old.cancelled and old.goal == '' and state.delegations == {}
            assert state.request_history == {('other-session', 'other-request'): ('other-digest', 'b' * 32)}
        finally:
            await state.aclose()
    asyncio.run(run())
