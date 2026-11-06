"""真实AppContainer、真实PG；隔离缺失必须失败，不skip、不mock成功。"""
import json

import pytest
from fastapi import HTTPException

from services.business import observation as obs
from services.guide import windows
from services.guide.manage import probe
from test_observation import capture, request, selection, unchanged
from test_corporate import ready as corporate_ready
from test_personal import advance, write


def runtime(settings, headers, selected, envelope=None):
    return windows.exchange(selected.kind,str(selected.object_id),lambda:
        capture(settings,headers,selected) if envelope is None else
        obs.verify_delivery(settings,request(headers),selected,envelope))


def test_actual_files_network_and_independent_business_unchanged(sql, capsys):
    before=unchanged(sql)
    probe()
    output=capsys.readouterr().out
    report=json.loads(output)
    assert report['passed'] is True and report['child_control_accepted'] is False
    assert 'Bearer' not in output and 'password' not in output
    assert unchanged(sql)==before


def test_actual_allowed_current_subject_and_http(client,settings,alice,create,sql):
    trip=create(); selected=selection(trip['id']); before=unchanged(sql)
    value=runtime(settings,alice,selected)
    assert value['simulated_identity']=='personal_alice'
    assert value['data']['trip']['id']==trip['id'] and value['data']['payment'] is None
    response=client.get(f"/guide/observe?kind=trip&object_id={trip['id']}",headers=alice)
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert response.json()['data']==value['data']
    assert unchanged(sql)==before


@pytest.mark.parametrize('change',['role','owner','org','other_id','url','file','sql','reset','seed','clock','outcome','answer'])
def test_actual_shell_cannot_request_extra_capability(change,settings,alice,bob,create,sql):
    trip=create(); other=create('bob-trip',headers=bob); before=unchanged(sql)
    selected='trip\t'+trip['id']
    forged='observe\t'+selected+'\t'+change+'=admin'
    if change=='other_id': forged='observe\ttrip\t'+other['id']
    if change in ('url','file','sql','reset','seed','clock','outcome','answer'): forged=change+'\tforbidden'
    called=[]
    with windows.Child(probe=True) as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('emit\t'+forged)
        with pytest.raises(windows.IsolationUnavailable):
            windows.dispatch(child,'trip',trip['id'],lambda:called.append(True))
    assert called==[] and unchanged(sql)==before


@pytest.mark.parametrize('account',['personal_bob','employee_ding','employee_fang','approver_lin'])
def test_actual_cross_owner_org_lin_association_denied(account,client,settings,login,sql):
    ctx=corporate_ready(client,login)
    headers=login(account); before=unchanged(sql)
    with pytest.raises(HTTPException) as error:
        runtime(settings,headers,selection(ctx['trip']['id']))
    assert error.value.status_code==404 and error.value.detail=='not_visible'
    assert unchanged(sql)==before


def test_actual_lin_only_assigned_summary(client,settings,login,sql):
    ctx=corporate_ready(client,login); before=unchanged(sql)
    value=runtime(settings,ctx['approver'],selection(ctx['approved']['id'],'approval'))
    assert set(value['data'])=={'approval'}
    assert set(value['data']['approval'])==set(obs.FIELDS['assigned'])
    assert not any(key in str(value) for key in ('profile_id','source_ref','owner_id','payment_id','trip_id','lock_id'))
    for account in ('approver_qian','approver_wu','approver_zhao'):
        with pytest.raises(HTTPException) as error:
            runtime(settings,login(account),selection(ctx['approved']['id'],'approval'))
        assert error.value.status_code==404
    assert unchanged(sql)==before


@pytest.mark.parametrize('change',['switch','membership','assignment','role','revision','expiry','knowledge','simulated_source','preview_source','documented_source','unverified_source'])
def test_actual_stale_revoked_or_simulated_result_not_delivered(change,client,settings,login,sql,monkeypatch):
    ctx=corporate_ready(client,login); headers=ctx['approver']
    selected=selection(ctx['approved']['id'],'approval')
    old=runtime(settings,headers,selected)
    if change=='switch':
        switched=client.post('/demo/sessions',headers=headers,json={'account':'personal_bob'})
        assert switched.status_code==201
    elif change=='membership':
        sql("UPDATE memberships SET active=false,revision=revision+1 WHERE user_id='approver_lin' RETURNING user_id")
    elif change=='role':
        sql("UPDATE memberships SET approver=false,revision=revision+1 WHERE user_id='approver_lin' RETURNING user_id")
    elif change=='assignment':
        sql("UPDATE approval_assignments SET active=false WHERE applicant_id='employee_chen' RETURNING applicant_id")
    elif change=='revision':
        write(client,ctx['headers'],f"/trips/{ctx['trip']['id']}",{'expected_revision':1,'passengers':2},method='patch')
    elif change=='expiry': advance(settings,300)
    elif change=='knowledge': monkeypatch.setitem(obs.DOCUMENTS,'approval',{**obs.DOCUMENTS['approval'],'revision':2})
    else: old['source_kind']={'simulated_source':'sandbox_observed','preview_source':'preview','documented_source':'documented','unverified_source':'unverified'}[change]
    before=unchanged(sql)
    with pytest.raises(HTTPException) as error:
        runtime(settings,headers,selected,old)
    assert error.value.status_code in (401,404,409)
    assert unchanged(sql)==before


def test_actual_shell_fabricated_result_is_not_delivered(settings,alice,create,sql):
    trip=create(); before=unchanged(sql)
    # 同一真实受限二进制的工程emit路径让结果文本被篡改；可信分派器必须拒绝。
    with windows.Child(probe=True) as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('emit\tobserve\ttrip\t'+trip['id'])
        child.send('emit\tresult\t{"source_kind":"sandbox_observed","eligible":true}')
        with pytest.raises(windows.IsolationUnavailable):
            windows.dispatch(child,'trip',trip['id'],lambda:capture(settings,alice,selection(trip['id'])))
    assert unchanged(sql)==before


@pytest.mark.parametrize('fault',['exit','silent','partial','utf8','long','flood'])
def test_actual_ipc_failure_closes_process_and_threads(fault,sql):
    import time
    before=unchanged(sql)
    started=time.monotonic()
    with windows.Child(probe=True) as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('fault\t'+fault)
        if fault=='flood':
            assert child.fault.wait(3), '洪泛没有触发有界队列拒绝'
        with pytest.raises(windows.IsolationUnavailable): child.receive()
    assert time.monotonic()-started<15
    assert child.stopped and not child.thread.is_alive()
    assert child.sender is None or not child.sender.is_alive()
    assert unchanged(sql)==before


@pytest.mark.parametrize('mode',['extra','nonzero','hang'])
def test_actual_matched_result_still_requires_clean_exit(mode,settings,alice,create,sql):
    trip=create(); before=unchanged(sql)
    with windows.Child(probe=True) as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('emit\tobserve\ttrip\t'+trip['id'])
        child.send('reply\t'+mode)
        with pytest.raises(windows.IsolationUnavailable):
            windows.dispatch(child,'trip',trip['id'],lambda:capture(settings,alice,selection(trip['id'])))
    assert child.stopped and not child.thread.is_alive()
    assert not child.sender.is_alive() and unchanged(sql)==before


def test_actual_clean_result_exits_normally_and_reaps(settings,alice,create,sql):
    trip=create(); before=unchanged(sql)
    with windows.Child() as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('trip\t'+trip['id'])
        value=windows.dispatch(child,'trip',trip['id'],lambda:capture(settings,alice,selection(trip['id'])))
        assert value['data']['trip']['id']==trip['id']
        assert child.k.WaitForSingleObject(child.pi.process,0)==0
    child.close()  # 正常收尾可重复调用，不遗留句柄或读写线程。
    assert child.stopped and child.reader.closed and child.writer.closed
    assert not child.thread.is_alive() and not child.sender.is_alive()
    assert unchanged(sql)==before


def test_actual_pipe_write_timeout_is_bounded_and_reaped(settings,alice,create,sql):
    import time
    trip=create(); before=unchanged(sql); selected=selection(trip['id'])
    envelope=capture(settings,alice,selected)
    # 工程合成大负载用于耗尽管道缓冲；不是允许交付的产品观察字段。
    envelope['engineering_padding']='x'*24000
    started=time.monotonic()
    with windows.Child(probe=True) as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('stall\ttrip\t'+trip['id'])
        with pytest.raises(windows.IsolationUnavailable):
            windows.dispatch(child,'trip',trip['id'],lambda:envelope)
    assert time.monotonic()-started<15
    assert child.stopped and not child.thread.is_alive() and not child.sender.is_alive()
    assert unchanged(sql)==before


def test_actual_job_close_kills_without_terminateprocess(sql):
    before=unchanged(sql)
    with windows.Child(probe=True) as child:
        assert child.receive()=='ready\tappcontainer'
        child.send('fault\tsilent')
        assert child.k.WaitForSingleObject(child.pi.process,0)==258
        assert child.k.CloseHandle(child.job)
        child.job=None
        assert child.k.WaitForSingleObject(child.pi.process,3000)==0
    assert child.stopped and not child.thread.is_alive()
    assert unchanged(sql)==before


def test_actual_host_abrupt_exit_reaps_appcontainer(sql):
    import ctypes as c
    import queue, subprocess, sys, threading
    before=unchanged(sql)
    script="""import os,sys
from services.guide.windows import Child
child=Child(probe=True)
assert child.receive()=='ready\\tappcontainer'
print(child.pi.pid,flush=True)
sys.stdin.readline()
os._exit(0)
"""
    host=subprocess.Popen([sys.executable,'-X','utf8','-c',script],cwd=windows.ROOT,
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    handle=None
    output=queue.Queue()
    reader=threading.Thread(target=lambda:output.put(host.stdout.readline()),daemon=True)
    reader.start()
    try:
        pid=int(output.get(timeout=10))
        k,_=windows.api()
        k.OpenProcess.argtypes=[windows.w.DWORD,windows.w.BOOL,windows.w.DWORD]
        k.OpenProcess.restype=windows.w.HANDLE
        handle=k.OpenProcess(0x100000,False,pid)
        assert handle and k.WaitForSingleObject(handle,0)==258
        host.communicate('end\n',timeout=10)
        assert host.returncode==0 and k.WaitForSingleObject(handle,3000)==0
    finally:
        if handle: k.CloseHandle(handle)
        if host.poll() is None: host.kill(); host.wait(10)
        host.stdin.close(); host.stdout.close(); reader.join(3)
    assert not reader.is_alive() and unchanged(sql)==before


@pytest.mark.parametrize('change',['expired','future','naive_time','missing_time','binary','launcher',
                                 'profile','capabilities','loopback','schema','checks','host_control',
                                 'child_received','allowed_file','list_json','broken_json'])
def test_probe_evidence_invalid_is_http_503_and_business_unchanged(change,client,alice,create,sql,monkeypatch,tmp_path):
    from copy import deepcopy
    from datetime import datetime,timedelta,timezone
    trip=create(); before=unchanged(sql)
    report=deepcopy(json.loads((windows.ROOT/'.local/d2-01-isolation.json').read_text(encoding='utf8')))
    config=json.loads((windows.ROOT/'.local/guide-runtime.json').read_text(encoding='utf8'))
    assert config['location']=='ntfs'
    if change=='expired': report['checked_at']=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    elif change=='future': report['checked_at']=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    elif change=='naive_time': report['checked_at']='2026-10-03T12:00:00'
    elif change=='missing_time': del report['checked_at']
    elif change in ('binary','launcher'): report[change+'_sha256']='0'*64
    elif change=='profile': report['profile']='other-profile'
    elif change=='capabilities': report['network_capabilities']=['internetClient']
    elif change=='loopback': report['loopback_exempt']=True
    elif change=='schema': report['schema_version']=1
    elif change=='checks': report['checks'].pop()
    elif change in ('host_control','child_received'):
        item=next(r for r in report['checks'] if r['resource']=='live-udp6')
        item[change]=change=='child_received'
    elif change=='allowed_file': report['checks'][0]['result']='ALLOWED'
    elif change=='list_json': report=[]
    local=tmp_path/'.local'; local.mkdir()
    (local/'guide-runtime.json').write_text(json.dumps(config),encoding='utf8')
    (local/'d2-01-isolation.json').write_text('{' if change=='broken_json' else json.dumps(report),encoding='utf8')
    monkeypatch.setattr(windows,'ROOT',tmp_path)
    response=client.get(f"/guide/observe?kind=trip&object_id={trip['id']}",headers=alice)
    assert response.status_code==503 and response.json()=={'error':'isolation_unavailable'}
    assert response.headers['cache-control']=='no-store'
    assert unchanged(sql)==before
