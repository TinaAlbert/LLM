"""工程侧构建/注册/探测工具；不提供HTTP入口，不向外壳发送秘密或业务数据。"""
import argparse
import ctypes as c
import hashlib
import json
import os
import subprocess
import shutil
import socket
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .windows import ROOT, PROFILE, Child, IsolationUnavailable, api, executable
from . import windows


def build(location, *, exploration=False):
    if location not in {'project', 'ntfs'}: raise ValueError('不支持的运行目录选择')
    directory = ROOT / '.local/guide-runtime' if location == 'project' else windows.ntfs_runtime()
    directory.mkdir(parents=True, exist_ok=True)
    compiler = shutil.which('gcc')
    migrated = ROOT / '.local/reinstall/tools/mingw64/bin/gcc.exe'
    if compiler is None and migrated.is_file(): compiler = str(migrated)
    if compiler is None: raise RuntimeError('需要本机已核验的MinGW gcc；不自动安装')
    compiler_env = dict(os.environ)
    compiler_env['PATH'] = str(Path(compiler).parent) + os.pathsep + compiler_env.get('PATH', '')
    result = subprocess.run([compiler] + (['-DEXPLORE_ONLY'] if exploration else []) + ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                             '-Wl,--dynamicbase,--nxcompat,--high-entropy-va,--image-base,0x140000000',
                             '-Wl,--major-subsystem-version,6,--minor-subsystem-version,2',
                             '-Wl,--export-all-symbols', # 本机binutils2.30用此生成必需的PE重定位表。
                             str(ROOT/'services/guide/shell.c'), '-o',str(directory/('explore-shell.exe' if exploration else 'shell.exe')),
                             '-ladvapi32','-lws2_32'], capture_output=True, env=compiler_env)
    if result.returncode:
        raise RuntimeError('外壳编译失败，exit='+str(result.returncode))
    if not exploration:
        config_path = ROOT / '.local/guide-runtime.json'
        try:
            config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            config = {}
        if type(config) is not dict: raise IsolationUnavailable()
        config['location'] = location
        if location == 'ntfs': config['ntfs_runtime'] = str(directory)
        config_path.write_text(json.dumps(config),encoding='utf8')
    print('外壳编译成功；没有因此宣称隔离通过')


def prepare():
    k,u = api()
    sid = c.c_void_p()
    u.CreateAppContainerProfile.argtypes = [c.c_wchar_p,c.c_wchar_p,c.c_wchar_p,c.c_void_p,c.c_uint,c.POINTER(c.c_void_p)]
    hr = u.CreateAppContainerProfile(PROFILE,PROFILE,'LLM D2 read-only shell',None,0,c.byref(sid))
    if hr not in (0, -2147024713): # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
        raise RuntimeError('profile注册失败，HRESULT='+str(hr))
    if sid:
        adv = c.WinDLL('advapi32'); adv.FreeSid.argtypes=[c.c_void_p]; adv.FreeSid(sid)
    target = executable().parent
    if target == windows.ntfs_runtime():
        sid = c.c_void_p()
        if u.DeriveAppContainerSidFromAppContainerName(PROFILE,c.byref(sid)) != 0:
            raise RuntimeError('无法取得专用SID')
        adv = c.WinDLL('advapi32')
        adv.ConvertSidToStringSidW.argtypes=[c.c_void_p,c.POINTER(c.c_wchar_p)]
        rendered = c.c_wchar_p()
        try:
            if not adv.ConvertSidToStringSidW(sid,c.byref(rendered)):
                raise RuntimeError('无法取得专用SID')
            # 仅本项目明确授权的目录；不给业务树或其他目录增添ACL。
            result = subprocess.run(['icacls.exe',str(target),'/grant',
                                     '*'+rendered.value+':(OI)(CI)RX'],capture_output=True)
            if result.returncode: raise RuntimeError('专用目录ACL设置失败')
        finally:
            adv.FreeSid.argtypes=[c.c_void_p];adv.FreeSid(sid)
            if rendered:
                k.LocalFree.argtypes=[c.c_void_p];k.LocalFree(c.cast(rendered,c.c_void_p))
    print('专用AppContainer profile已准备；尚需真实探测')


def probe():
    # 探测中断先作废原记录；所有协议需在线宿主对照，单独超时不算隔离。
    report_path = ROOT/'.local/d2-01-isolation.json'
    # 探测中断也不能继续沿用上一次成功结论。
    report_path.write_text(json.dumps({'profile':PROFILE,'passed':False,'stage':'probing'}),encoding='utf8')
    files = windows.FILE_CHECKS
    for relative in files:
        if not (ROOT/relative).is_file(): raise RuntimeError('探测前置文件不存在')
    # 在线PG的普通宿主连接作为控制组，不读取任何响应/凭证。
    with socket.create_connection(('127.0.0.1',55432),timeout=2): pass
    host_spawn=subprocess.run([str(executable())],cwd=executable().parent,capture_output=True,timeout=5)
    if host_spawn.returncode!=11: raise RuntimeError('宿主同一二进制启动对照不符合预期')
    results = []
    controls=[]
    marker=bytes.fromhex('d20201000001000000000000076578616d706c6503636f6d0000010001')
    # 公网UDP宿主必须收到对应固定查询的响应，避免把不可用网络误记为阻断。
    udp_destinations=(('8.8.8.8',socket.AF_INET,'udp','arbitrary-udp4'),
                      ('2606:4700:4700::1111',socket.AF_INET6,'udp6','arbitrary-udp6'))
    for host,family,_,_ in udp_destinations:
        with socket.socket(family,socket.SOCK_DGRAM) as control:
            control.settimeout(2);control.sendto(marker,(host,53))
            response,_=control.recvfrom(4096)
            if response[:2]!=marker[:2] or len(response)<12 or not response[2]&0x80:
                raise RuntimeError('公网UDP宿主对照不符合预期')
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as route:
        route.connect(('8.8.8.8',53));lan=route.getsockname()[0]
    if lan=='127.0.0.1': raise RuntimeError('非回环UDP宿主对照不可用')
    with ExitStack() as stack:
        for family,host in ((socket.AF_INET,'127.0.0.1'),(socket.AF_INET6,'::1')):
            for datagram in (False,True):
                kind=socket.SOCK_DGRAM if datagram else socket.SOCK_STREAM
                listener=stack.enter_context(socket.socket(family,kind))
                if family==socket.AF_INET6: listener.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
                listener.bind((host,0)); listener.settimeout(.3)
                if not datagram: listener.listen(4)
                address=listener.getsockname()
                with socket.socket(family,kind) as sender:
                    sender.settimeout(2)
                    if datagram:
                        sender.sendto(marker,address)
                        received,_=listener.recvfrom(64)
                        if received!=marker: raise RuntimeError('UDP宿主对照不符合预期')
                    else:
                        sender.connect(address)
                        accepted,_=listener.accept(); accepted.close()
                command=('udp' if datagram else 'tcp')+('6' if family==socket.AF_INET6 else '')
                label='live-'+('udp' if datagram else 'tcp')+('6' if family==socket.AF_INET6 else '4')
                controls.append((listener,command,host,address[1],label,datagram))
        listener=stack.enter_context(socket.socket(socket.AF_INET,socket.SOCK_DGRAM))
        listener.bind((lan,0));listener.settimeout(.3);address=listener.getsockname()
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
            sender.sendto(marker,address)
            received,_=listener.recvfrom(64)
            if received!=marker: raise RuntimeError('非回环UDP宿主对照不符合预期')
        controls.append((listener,'udp',lan,address[1],'live-lan-udp4',True))
        child=stack.enter_context(Child(probe=True))
        if child.receive() != 'ready\tappcontainer': raise IsolationUnavailable()
        for relative in files:
            child.send('file\t'+str(ROOT/relative))
            results.append({'resource':relative, 'result':child.receive()})
        for command,host,port,label in [
                ('tcp','127.0.0.1',55432,'postgres'),('tcp','127.0.0.1',8000,'business-http'),
                ('tcp','127.0.0.1',5173,'vite'),('tcp','1.1.1.1',443,'arbitrary-network'),
                ('tcp6','2606:4700:4700::1111',443,'arbitrary-tcp6')]:
            child.send(command+'\t'+host+'\t'+str(port))
            results.append({'resource':label,'result':child.receive()})
        for host,_,command,label in udp_destinations:
            child.send(command+'\t'+host+'\t53')
            results.append({'resource':label,'result':child.receive(),'host_control':True})
        for listener,command,host,port,label,datagram in controls:
            child.send(command+'\t'+host+'\t'+str(port))
            result=child.receive()
            try:
                if datagram: listener.recvfrom(64)
                else:
                    accepted,_=listener.accept(); accepted.close()
                received=True
            except socket.timeout: received=False
            results.append({'resource':label,'result':result,'host_control':True,'child_received':received})
        child.send('spawn')
        results.append({'resource':'child-process','result':child.receive()})
        child.send('end')
        child.finish()
    control_checks=[r for r in results if r['resource'].startswith('live-')]
    passed = (all(not r['child_received'] for r in control_checks) and all(
        r['result']=='denied\t5' if r['resource'] in files else
        r['result'] in ('spawn\t5','spawn\t367','spawn\t1816') if r['resource']=='child-process' else
        (r['result'] in ('socket\t10013','udp-no-response') and r.get('host_control') is True)
            if r['resource'].startswith('arbitrary-udp') else
        r['result']=='socket\t10013' if r['resource'].startswith('arbitrary') else
        r['result'] in ('socket\t10013','timeout','udp-no-response') for r in results))
    report = {'schema_version':windows.PROBE_SCHEMA,'profile':PROFILE,
              'network_capabilities':[], 'loopback_exempt':False, 'passed':passed,
              'checked_at':datetime.now(timezone.utc).isoformat(),
              'process_identity':'current-user AppContainer LLM.D2.ReadOnly (parent token verified)',
              'host_pg_connected':True,'host_spawn_exit':host_spawn.returncode,
              'host_control_connected':True,'child_control_accepted':any(r['child_received'] for r in control_checks),
              'loopback_interpretation':'IPv4/IPv6 TCP/UDP each has live host control; timeout alone is insufficient',
              'launcher_sha256':hashlib.sha256(Path(windows.__file__).read_bytes()).hexdigest(),
              'binary_sha256':hashlib.sha256(executable().read_bytes()).hexdigest(), 'checks':results}
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if not passed: raise RuntimeError('隔离探测未通过；不能交付业务观察')
    windows.require_evidence()



def probe_exploration(*, report_path=None):
    """P22实际独立产物探测；保留旧probe，公网控制失败也继续采集本机反例。"""
    report_path = windows.exploration_report_path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema_version': 1, 'profile': PROFILE, 'passed': False, 'stage': 'probing',
        'network_capabilities': [], 'loopback_exempt': False, 'checks': [], 'limitations': [],
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'launcher_sha256': hashlib.sha256(Path(windows.__file__).read_bytes()).hexdigest(),
        'source_sha256': hashlib.sha256((ROOT/'services/guide/shell.c').read_bytes()).hexdigest(),
        'process_identity': 'current-user AppContainer LLM.D2.ReadOnly (parent token verified)'}
    try:
        # 独占认领报告后才探测；失败/中断原件也保留，重跑必须显式选新路径。
        with report_path.open('x', encoding='utf8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    except FileExistsError:
        raise RuntimeError('exploration_report_exists_choose_new_path') from None
    try:
        exe = windows.exploration_executable()
        report['binary_sha256'] = hashlib.sha256(exe.read_bytes()).hexdigest()
        control = subprocess.run([str(exe)], cwd=exe.parent, capture_output=True, timeout=5)
        report['host_spawn_exit'] = control.returncode
        if control.returncode != 11: raise IsolationUnavailable('host_spawn_control')
        marker=bytes.fromhex('d20201000001000000000000076578616d706c6503636f6d0000010001')
        public=(('8.8.8.8',socket.AF_INET,'udp','arbitrary-udp4'),
                ('2606:4700:4700::1111',socket.AF_INET6,'udp6','arbitrary-udp6'))
        public_controls={}
        for host,family,_,label in public:
            valid=False
            try:
                with socket.socket(family,socket.SOCK_DGRAM) as sender:
                    sender.settimeout(2); sender.sendto(marker,(host,53)); response,_=sender.recvfrom(4096)
                    valid=(response[:2]==marker[:2] and len(response)>=12 and bool(response[2]&0x80))
            except OSError: pass
            public_controls[label]=valid
            if not valid: report['limitations'].append(label+'_host_control_unavailable')
        with ExitStack() as stack:
            controls=[]
            for family,host in ((socket.AF_INET,'127.0.0.1'),(socket.AF_INET6,'::1')):
                for datagram in (False,True):
                    kind=socket.SOCK_DGRAM if datagram else socket.SOCK_STREAM
                    listener=stack.enter_context(socket.socket(family,kind))
                    if family==socket.AF_INET6: listener.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
                    listener.bind((host,0)); listener.settimeout(.3)
                    if not datagram: listener.listen(4)
                    address=listener.getsockname()
                    with socket.socket(family,kind) as sender:
                        sender.settimeout(2)
                        if datagram:
                            sender.sendto(marker,address); received,_=listener.recvfrom(64)
                            if received!=marker: raise IsolationUnavailable('host_udp_control')
                        else:
                            sender.connect(address); accepted,_=listener.accept(); accepted.close()
                    command=('udp' if datagram else 'tcp')+('6' if family==socket.AF_INET6 else '')
                    label='live-'+('udp' if datagram else 'tcp')+('6' if family==socket.AF_INET6 else '4')
                    controls.append((listener,command,host,address[1],label,datagram))
            try:
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as route:
                    route.connect(('8.8.8.8',53)); lan=route.getsockname()[0]
                if lan=='127.0.0.1': raise OSError()
                listener=stack.enter_context(socket.socket(socket.AF_INET,socket.SOCK_DGRAM))
                listener.bind((lan,0)); listener.settimeout(.3); address=listener.getsockname()
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                    sender.sendto(marker,address); received,_=listener.recvfrom(64)
                    if received!=marker: raise IsolationUnavailable('host_lan_control')
                controls.append((listener,'udp',lan,address[1],'live-lan-udp4',True))
            except OSError: report['limitations'].append('live-lan-udp4_host_control_unavailable')
            child=stack.enter_context(Child(exploration=True,probe=True))
            if child.receive()!='ready\tappcontainer': raise IsolationUnavailable('missing_ready')
            report['appcontainer_started']=True
            for relative in windows.EXPLORATION_FILE_CHECKS:
                if not (ROOT/relative).is_file(): raise IsolationUnavailable('missing_probe_file')
                child.send('file\t'+str(ROOT/relative))
                report['checks'].append({'resource':relative,'result':child.receive()})
            for command,host,port,label in (
                    ('tcp','127.0.0.1',55432,'postgres'),('tcp','127.0.0.1',8000,'business-http'),
                    ('tcp','1.1.1.1',443,'arbitrary-network'),
                    ('tcp6','2606:4700:4700::1111',443,'arbitrary-tcp6')):
                item={'resource':label}
                if label in ('postgres','business-http'):
                    try:
                        with socket.create_connection((host,port),timeout=2): pass
                        item['host_control']=True
                    except OSError:
                        item['host_control']=False; report['limitations'].append(label+'_host_control_unavailable')
                child.send(command+'\t'+host+'\t'+str(port)); item['result']=child.receive()
                if label.startswith('arbitrary') and item['result']!='socket\t10013':
                    report['limitations'].append(label+'_denial_not_proven')
                report['checks'].append(item)
            for host,_,command,label in public:
                child.send(command+'\t'+host+'\t53')
                result=child.receive()
                report['checks'].append({'resource':label,'result':result,'host_control':public_controls[label]})
                if result not in ('socket\t10013','udp-no-response'):
                    report['limitations'].append(label+'_denial_not_proven')
            for listener,command,host,port,label,datagram in controls:
                child.send(command+'\t'+host+'\t'+str(port)); result=child.receive()
                try:
                    if datagram: listener.recvfrom(64)
                    else: accepted,_=listener.accept(); accepted.close()
                    received=True
                except socket.timeout: received=False
                report['checks'].append({'resource':label,'result':result,'host_control':True,'child_received':received})
            child.send('spawn'); report['checks'].append({'resource':'child-process','result':child.receive()})
            child.send('end'); child.finish()
            report['child_reaped']=True
        report['stage']='complete'
        report['passed']=True
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        try: windows.require_exploration_evidence(report_path=report_path)
        except IsolationUnavailable: report['passed']=False
    except (OSError, subprocess.SubprocessError, IsolationUnavailable) as error:
        report['stage']='failed'; report['failure']=type(error).__name__
        if isinstance(error, IsolationUnavailable): report['failure_code']=str(error)
    finally:
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'passed':report['passed'], 'stage':report['stage'],
                      'appcontainer_started':report.get('appcontainer_started',False),
                      'checks':len(report['checks']), 'limitations':report['limitations']},ensure_ascii=False))
    if not report['passed']: raise RuntimeError('P22隔离探测未完整通过；产品探索入口继续失败关闭')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['build','prepare','probe','build-exploration','probe-exploration'])
    parser.add_argument('--location',choices=['project','ntfs'],default='project')
    parser.add_argument('--exploration-report', help='probe-exploration独立JSON路径；已存在即拒绝覆盖')
    parser.add_argument('--authorized-profile-space',action='store_true',help='仅在用户已授权专用profile和固定NTFS空间后使用')
    args = parser.parse_args()
    if args.exploration_report is not None and args.action != 'probe-exploration':
        parser.error('--exploration-report仅用于probe-exploration')
    if (args.action=='prepare' or args.location=='ntfs' or args.action in ('build-exploration','probe-exploration')) and not args.authorized_profile_space:
        parser.error('此操作需事先取得仓库外专用空间授权；标志本身不构成授权')
    try:
        if args.action=='build-exploration': build('ntfs', exploration=True)
        elif args.action=='probe-exploration': probe_exploration(report_path=args.exploration_report)
        elif args.action=='build': build(args.location)
        elif args.action=='prepare': prepare()
        else: probe()
    except IsolationUnavailable as error:
        raise SystemExit('隔离环境不可用；未回退普通进程 '+str(error)) from None
    except RuntimeError as error:
        raise SystemExit(str(error)) from None


if __name__=='__main__': main()
