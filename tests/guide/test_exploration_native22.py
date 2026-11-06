"""P22原生协议；actual用例必须创建真实AppContainer，无普通进程回退。"""
import json
import threading
import time

import pytest

from services.guide import windows

REFERENCE = '0123456789abcdef0123456789abcdef'
OBSERVATION = {'delegated_trips': [], 'unknowns': ['engineering_protocol_only']}


@pytest.mark.parametrize('operation,target,seq', [
    ('sql', None, 1), ('reset', None, 1), ('worker', None, 1), ('clock', None, 1),
    ('observe', 'trip_1', 1), ('booking.pay', None, 1), ('booking.pay', 'trip_3', 1),
    ('booking.pay', 'http://127.0.0.1:8000', 1), ('booking.pay', '../answer', 1),
    ('booking.pay', '00000000-0000-0000-0000-000000000000', 1),
    ('booking.pay', 'trip_1', 0), ('booking.pay', 'trip_1', 29),
    ('booking.pay', 'trip_1', True), ('booking.pay', 'trip_1', 1.0),
])
def test_invalid_action_refused_before_process(operation, target, seq, monkeypatch):
    monkeypatch.setattr(windows, 'Child', lambda **_: pytest.fail('invalid action created child'))
    with windows.ExplorationSession(REFERENCE, 180) as session:
        with pytest.raises(windows.IsolationUnavailable):
            session.authorize(OBSERVATION, operation, target, seq)


@pytest.mark.parametrize('value', [[], {'value': float('nan')}, {'value': object()}, {'text': 'x'*12288}])
def test_observation_limits_refused_before_process(value, monkeypatch):
    monkeypatch.setattr(windows, 'Child', lambda **_: pytest.fail('invalid observation created child'))
    with windows.ExplorationSession(REFERENCE, 180) as session:
        with pytest.raises(windows.IsolationUnavailable): session.authorize(value, 'observe', None, 1)


@pytest.mark.parametrize('remaining', [0, -1, 180.1, float('inf'), float('nan'), True, '180'])
def test_limit_refused_before_process(remaining):
    with pytest.raises(windows.IsolationUnavailable): windows.ExplorationSession(REFERENCE, remaining)


def test_product_entry_refuses_incomplete_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, 'ROOT', tmp_path)
    monkeypatch.setattr(windows, 'Child', lambda **_: pytest.fail('invalid evidence created child'))
    with pytest.raises(windows.IsolationUnavailable): windows.begin_exploration(REFERENCE, 180)


def test_product_rechecks_evidence_before_every_candidate(monkeypatch):
    calls=[]
    def evidence():
        calls.append(True)
        if len(calls)>1: raise windows.IsolationUnavailable()
    monkeypatch.setattr(windows, 'require_exploration_evidence', evidence)
    monkeypatch.setattr(windows, 'Child', lambda **_: pytest.fail('changed evidence created child'))
    with windows.begin_exploration(REFERENCE, 180) as session:
        with pytest.raises(windows.IsolationUnavailable): session.authorize(OBSERVATION, 'observe', None, 1)
    assert len(calls)==2


def test_closed_or_late_candidate_refused_before_process(monkeypatch):
    monkeypatch.setattr(windows, 'Child', lambda **_: pytest.fail('closed/deadline created child'))
    session=windows.ExplorationSession(REFERENCE, 180)
    session.deadline=time.monotonic()-1
    with pytest.raises(windows.IsolationUnavailable): session.authorize(OBSERVATION, 'observe', None, 1)
    session.abort(); session.abort()
    with pytest.raises(windows.IsolationUnavailable): session.authorize(OBSERVATION, 'observe', None, 1)


def test_out_of_order_refused_before_process(monkeypatch):
    monkeypatch.setattr(windows, 'Child', lambda **_: pytest.fail('out of order created child'))
    with windows.ExplorationSession(REFERENCE, 180) as session:
        with pytest.raises(windows.IsolationUnavailable): session.authorize(OBSERVATION, 'observe', None, 2)


@pytest.mark.parametrize('operation,target', [
    ('observe', None), ('booking.quote', 'trip_1'), ('booking.lock', 'trip_2'),
    ('booking.cancel_lock', 'trip_1'), ('booking.pay', 'trip_2'),
])
def test_actual_native_whitelist_and_reap(operation, target):
    with windows.ExplorationSession(REFERENCE, 180) as session:
        assert session.authorize(OBSERVATION, operation, target, 1) is None
        assert session._sequence==1 and session._child is None
        with pytest.raises(windows.IsolationUnavailable): session.authorize(OBSERVATION, operation, target, 1)


def test_actual_native_operation_quota():
    with windows.ExplorationSession(REFERENCE, 180) as session:
        for seq in range(1,29): session.authorize(OBSERVATION, 'observe', None, seq)
        with pytest.raises(windows.IsolationUnavailable): session.authorize(OBSERVATION, 'observe', None, 29)
        assert session._sequence==28 and session._child is None


def _start(child, mode=None):
    assert child.receive()=='ready\tappcontainer'
    child.send(('start-explore\t' if mode is None else 'explore-reply\t'+mode+'\t')+REFERENCE+'\t1')
    assert child.receive()=='explore-observe-request\t'+REFERENCE+'\t1'


def _observe(child):
    child.send('explore-observation\t'+REFERENCE+'\t1\t{}')
    assert child.receive()=='explore-candidate-request\t'+REFERENCE+'\t1'


@pytest.mark.parametrize('frame', [
    'explore-action\t'+'f'*32+'\t1\tbooking.pay\ttrip_1',
    'explore-action\t'+REFERENCE+'\t2\tbooking.pay\ttrip_1',
    'explore-action\t'+REFERENCE+'\t1\tbooking.pay\ttrip_1\tamount=1',
    'explore-action\t'+REFERENCE+'\t1\tsql\ttrip_1',
    'explore-action\t'+REFERENCE+'\t1\tworker\ttrip_1',
    'explore-action\t'+REFERENCE+'\t1\tbooking.pay\thttps://example.com',
    'explore-action\t'+REFERENCE+'\t1\tbooking.pay\t00000000-0000-0000-0000-000000000000',
    'explore-action\t'+REFERENCE+'\t1\tobserve\ttrip_1',
])
def test_actual_native_rejects_malicious_action(frame):
    with windows.Child(exploration=True) as child:
        _start(child); _observe(child); child.send(frame)
        with pytest.raises(windows.IsolationUnavailable): child.receive()
    assert child.stopped and not child.thread.is_alive() and not child.sender.is_alive()


@pytest.mark.parametrize('frame', [
    'explore-observation\t'+'f'*32+'\t1\t{}',
    'explore-observation\t'+REFERENCE+'\t2\t{}',
    'explore-observation\t'+REFERENCE+'\t1\t[]',
    'explore-observation\t'+REFERENCE+'\t1\t{"x":"'+'x'*12288+'"}',
    'explore-action\t'+REFERENCE+'\t1\tbooking.pay\ttrip_1',
])
def test_actual_native_rejects_bad_or_out_of_order_observation(frame):
    with windows.Child(exploration=True) as child:
        _start(child); child.send(frame)
        with pytest.raises(windows.IsolationUnavailable): child.receive()
    assert child.stopped and not child.thread.is_alive()


@pytest.mark.parametrize('mode', ['extra', 'nonzero', 'hang'])
def test_actual_permit_requires_unique_eof_and_success(mode):
    with windows.Child(exploration=True,probe=True) as child:
        _start(child,mode); _observe(child)
        child.send('explore-action\t'+REFERENCE+'\t1\tbooking.pay\ttrip_1')
        assert child.receive()=='explore-permit\t'+REFERENCE+'\t1\tbooking.pay\ttrip_1'
        with pytest.raises(windows.IsolationUnavailable): child.finish(deadline=time.monotonic()+.3)
    assert child.stopped and not child.thread.is_alive() and not child.sender.is_alive()


def test_actual_abort_kills_job_and_late_result(monkeypatch):
    session=windows.ExplorationSession(REFERENCE,180)
    child=windows.Child(exploration=True)
    with session._child_lock: session._child=child
    assert child.receive()=='ready\tappcontainer'
    child.send('start-explore\t'+REFERENCE+'\t1')
    assert child.receive()=='explore-observe-request\t'+REFERENCE+'\t1'
    session.abort(); session.abort()
    assert child.stopped and not child.thread.is_alive() and not child.sender.is_alive()
    assert child.reader.closed and child.writer.closed and child.job is None and child.pi.process is None
    with pytest.raises(windows.IsolationUnavailable): session.authorize(OBSERVATION,'observe',None,1)


def test_actual_environment_secret_absent(monkeypatch):
    monkeypatch.setenv('GUIDE_MODEL_API_KEY', 'synthetic-p22-canary')
    with windows.Child(exploration=True,probe=True) as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('environment\tGUIDE_MODEL_API_KEY')
        assert child.receive()=='environment\tabsent'
        child.send('end'); child.finish()


@pytest.mark.parametrize('mode', ['extra', 'nonzero', 'hang'])
def test_actual_session_refuses_faulty_native_permit_before_return(mode, monkeypatch):
    original=windows.Child
    class FaultyNative(original):
        def __init__(self, **kwargs): super().__init__(exploration=True,probe=True)
        def send(self, line, timeout=windows.IPC_TIMEOUT):
            if line.startswith('start-explore\t'):
                line='explore-reply\t'+mode+'\t'+line.removeprefix('start-explore\t')
            return super().send(line,timeout)
    monkeypatch.setattr(windows,'Child',FaultyNative)
    with windows.ExplorationSession(REFERENCE,180) as session:
        session.deadline=time.monotonic()+.4
        with pytest.raises(windows.IsolationUnavailable): session.authorize(OBSERVATION,'booking.pay','trip_1',1)
        assert session._sequence==0 and session._child is None


def test_actual_abort_during_native_completion_discards_late_permit(monkeypatch):
    original=windows.Child; started=threading.Event(); processes=[]
    class HangingNative(original):
        def __init__(self, **kwargs):
            super().__init__(exploration=True,probe=True); processes.append(self)
        def send(self, line, timeout=windows.IPC_TIMEOUT):
            if line.startswith('start-explore\t'):
                line='explore-reply\thang\t'+line.removeprefix('start-explore\t')
            result=super().send(line,timeout)
            if line.startswith('explore-action\t'): started.set()
            return result
    monkeypatch.setattr(windows,'Child',HangingNative)
    session=windows.ExplorationSession(REFERENCE,180); results=[]
    def run():
        try: session.authorize(OBSERVATION,'booking.pay','trip_1',1); results.append('unexpected_permit')
        except windows.IsolationUnavailable: results.append('refused')
    runner=threading.Thread(target=run); runner.start()
    assert started.wait(2)
    session.abort(); runner.join(3)
    assert not runner.is_alive() and results==['refused'] and session._sequence==0
    child=processes[0]
    assert child.stopped and child.job is None and not child.thread.is_alive() and not child.sender.is_alive()


@pytest.mark.parametrize('mode', ['partial', 'utf8', 'long', 'flood', 'exit', 'silent'])
def test_actual_exploration_child_rejects_malformed_or_missing_output(mode):
    with windows.Child(exploration=True,probe=True) as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('fault\t'+mode)
        with pytest.raises(windows.IsolationUnavailable):
            if mode=='flood': child.finish(deadline=time.monotonic()+.2)
            else: child.receive(.2)
    assert child.stopped and child.job is None and not child.thread.is_alive() and not child.sender.is_alive()
