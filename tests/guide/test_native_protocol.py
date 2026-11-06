"""D3无费用协议验收；实际AppContainer，不能替代完整probe或真实模型成绩。"""
import json
import time

import pytest

from services.guide import windows


REFERENCE = '0123456789abcdef0123456789abcdef'
CANDIDATE = {'status': 'unknown', 'explanation': '工程协议检查', 'unknowns': ['model_not_called']}


@pytest.mark.parametrize('reference', ['', 'A'*32, '0'*31, '0'*32+'\tadmin', None])
def test_reference_rejected_before_native_start(reference):
    with pytest.raises(windows.IsolationUnavailable):
        windows.GuideSession(reference, 30)


@pytest.mark.parametrize('remaining', [0, -1, 30.1, float('inf'), float('nan'), True, '30'])
def test_invalid_overall_limit_rejected_before_native_start(remaining):
    with pytest.raises(windows.IsolationUnavailable):
        windows.GuideSession(REFERENCE, remaining)


@pytest.mark.parametrize('candidate', [[], {'text': 'x'*16384}, {'value': float('nan')}, {'value': object()}])
def test_candidate_bytes_and_json_are_rejected(candidate):
    with pytest.raises(windows.IsolationUnavailable): windows._guide_candidate(candidate)


def test_product_entry_refuses_missing_complete_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, 'ROOT', tmp_path)
    with pytest.raises(windows.IsolationUnavailable): windows.begin_guide(REFERENCE, 30)


def test_product_entry_refuses_missing_current_credential_before_process(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, 'ROOT', tmp_path)
    monkeypatch.setattr(windows, 'require_evidence', lambda: None)
    monkeypatch.setattr(windows, 'Child', lambda **kwargs: pytest.fail('missing file cannot prove access denial'))
    with pytest.raises(windows.IsolationUnavailable): windows.begin_guide(REFERENCE, 30)


def test_actual_product_entry_checks_new_credential_and_separate_deadline(tmp_path, monkeypatch):
    # 只补工程证据入口，实际两次Child/SID/Job/文件拒绝均未替换；不是完整probe成功。
    runtime_directory = windows.ntfs_runtime()
    (tmp_path/'.local').mkdir()
    (tmp_path/'.local/guide-runtime.json').write_text(json.dumps({'location':'ntfs','ntfs_runtime':str(runtime_directory)}), encoding='utf8')
    (tmp_path/'.local/guide-model.json').write_text('{"synthetic":"local file canary"}', encoding='utf8')
    monkeypatch.setattr(windows, 'ROOT', tmp_path)
    monkeypatch.setattr(windows, 'require_evidence', lambda: None)
    started = time.monotonic()
    with windows.begin_guide(REFERENCE, 30) as session:
        # 两次monotonic读取有调用开销；不得把整个文件probe耗时重新加回截止。
        assert session.deadline <= started + 30.01
        assert session.complete(CANDIDATE) == CANDIDATE


def test_actual_guide_complete_and_idempotent_reap():
    session = windows.GuideSession(REFERENCE, 30)
    child = session.child
    assert session.complete(CANDIDATE) == CANDIDATE
    session.close()
    assert child.stopped and not child.thread.is_alive() and not child.sender.is_alive()
    assert child.reader.closed and child.writer.closed
    with pytest.raises(windows.IsolationUnavailable): session.complete(CANDIDATE)


def test_actual_maximum_candidate_is_accepted():
    candidate = {'text': 'x'*(windows.GUIDE_CANDIDATE_BYTES-len('{"text":""}'))}
    assert len(windows._guide_candidate(candidate).encode('ascii')) == 16384
    with windows.GuideSession(REFERENCE, 30) as session:
        assert session.complete(candidate) == candidate


def test_actual_model_wait_does_not_consume_empty_pipe_timeout():
    # 只验证管道等待分离；sleep不是模型或业务负载证据。
    with windows.GuideSession(REFERENCE, 30) as session:
        time.sleep(8.2)
        assert session.complete(CANDIDATE) == CANDIDATE


def test_actual_deadline_expired_rejects_candidate_and_reaps():
    session = windows.GuideSession(REFERENCE, 30)
    session.deadline = time.monotonic()-0.001
    with pytest.raises(windows.IsolationUnavailable): session.complete(CANDIDATE)
    assert session.child.stopped and not session.child.thread.is_alive()


def test_actual_abort_rejects_late_candidate_and_reaps():
    session = windows.GuideSession(REFERENCE, 30)
    session.abort()
    session.abort()
    with pytest.raises(windows.IsolationUnavailable): session.complete(CANDIDATE)
    assert session.child.stopped and not session.child.thread.is_alive()


@pytest.mark.parametrize('mode', ['extra', 'nonzero', 'hang'])
def test_actual_matching_guide_result_requires_unique_eof_and_exit(mode):
    started = time.monotonic()
    with windows.Child(probe=True) as child:
        assert child.receive() == 'ready\tappcontainer'
        child.send('guide-reply\t'+mode+'\t'+REFERENCE)
        assert child.receive() == 'guide-request\t'+REFERENCE
        with pytest.raises(windows.IsolationUnavailable):
            windows._complete_guide(child, REFERENCE, windows._guide_candidate(CANDIDATE), time.monotonic()+1)
    assert time.monotonic()-started < 8
    assert child.stopped and not child.thread.is_alive() and not child.sender.is_alive()


@pytest.mark.parametrize('frame', [
    'guide-candidate\t'+'f'*32+'\t{}',
    'guide-candidate\t'+REFERENCE+'\t[]',
    'guide-candidate\t'+REFERENCE+'\t{"text":"'+'x'*16384+'"}',
])
def test_actual_native_protocol_rejects_changed_reference_or_invalid_candidate(frame):
    with windows.Child() as child:
        assert child.receive() == 'ready\tappcontainer'
        child.send('start-guide\t'+REFERENCE)
        assert child.receive() == 'guide-request\t'+REFERENCE
        child.send(frame)
        with pytest.raises(windows.IsolationUnavailable): child.receive()
    assert child.stopped and not child.thread.is_alive()


def test_actual_model_secret_environment_is_not_inherited(monkeypatch):
    monkeypatch.setenv('GUIDE_MODEL_API_KEY', 'synthetic-environment-canary')
    with windows.Child(probe=True) as child:
        assert child.receive() == 'ready\tappcontainer'
        child.send('environment\tGUIDE_MODEL_API_KEY')
        assert child.receive() == 'environment\tabsent'
        child.send('end')
        child.finish()
    assert child.stopped and not child.thread.is_alive()


def test_actual_new_secret_file_cannot_be_opened(tmp_path):
    # 只用合成哨兵；真实guide-model.json配置后仍须专项实测，不读文件内容。
    path = tmp_path/'guide-model-canary.json'
    path.write_text(json.dumps({'api_key': 'synthetic-file-canary'}), encoding='utf8')
    with windows.Child(probe=True) as child:
        assert child.receive() == 'ready\tappcontainer'
        child.send('file\t'+str(path))
        assert child.receive() == 'denied\t5'
        child.send('end')
        child.finish()
    assert child.stopped and not child.thread.is_alive()


def test_actual_d2_echo_still_uses_same_bounded_path():
    with windows.Child() as child:
        assert child.receive() == 'ready\tappcontainer'
        child.send('trip\t00000000-0000-0000-0000-000000000000')
        assert child.receive() == 'observe\ttrip\t00000000-0000-0000-0000-000000000000'
        child.send('{"engineering":"d2-regression"}')
        assert child.receive() == 'result\t{"engineering":"d2-regression"}'
        child.finish()
    assert child.stopped and not child.thread.is_alive() and not child.sender.is_alive()
