"""可信Windows启动器：零网络capability、显式句柄继承、干净环境、限时管道。

本文件属于可信宿主，不是助手可读代码。没有普通进程回退，也不自动注册profile。
"""
import ctypes as c
from ctypes import wintypes as w
import json
import hashlib
import os
from pathlib import Path
import queue
import subprocess
import threading
import math
import re
import time
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "LLM.D2.ReadOnly"
PROBE_SCHEMA = 2
PROBE_MAX_AGE = timedelta(hours=24)
IPC_TIMEOUT = 8
GUIDE_TIMEOUT = 30
GUIDE_CANDIDATE_BYTES = 16384
FILE_CHECKS = ('services/business/app.py','tests/business/test_personal.py',
               'apps/web/tests/control.py','.local/business.json','.local/postgres-admin.json',
               'services/business/manage.py','services/business/worker_cli.py')
NETWORK_CHECKS = ('postgres','business-http','vite','arbitrary-network',
                  'arbitrary-tcp6','arbitrary-udp4','arbitrary-udp6',
                  'live-tcp4','live-tcp6','live-udp4','live-udp6','live-lan-udp4')


class IsolationUnavailable(Exception):
    pass


def ntfs_runtime():
    """仅可信宿主的忽略配置选择运行目录；模型/请求不能传路径。"""
    config_path = ROOT / '.local' / 'guide-runtime.json'
    try:
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError, TypeError):
        raise IsolationUnavailable() from None
    if type(config) is not dict:
        raise IsolationUnavailable()
    value = config.get('ntfs_runtime')
    if 'ntfs_runtime' not in config:
        local_app_data = os.environ.get('LOCALAPPDATA')
        base = Path(local_app_data) if local_app_data else Path.home() / 'AppData' / 'Local'
        selected = base / 'LLM-runtime'
    elif type(value) is str and value and '\0' not in value:
        selected = Path(value)
    else:
        raise IsolationUnavailable()
    try:
        if not selected.is_absolute() or '..' in selected.parts:
            raise IsolationUnavailable()
        resolved, workspace = selected.resolve(), ROOT.resolve()
        if (resolved != selected or resolved.parent == resolved
                or resolved.is_relative_to(workspace) or workspace.is_relative_to(resolved)):
            raise IsolationUnavailable()
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise IsolationUnavailable() from None


def _child_environment():
    allowed = {name: os.environ[name] for name in
               ('SystemRoot', 'SystemDrive', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP')
               if name in os.environ}
    system_root = allowed.get('SystemRoot')
    if not system_root or not Path(system_root).is_absolute():
        raise IsolationUnavailable()
    allowed['windir'] = system_root
    return allowed


class SI(c.Structure):
    _fields_ = [("cb",w.DWORD),("reserved",w.LPWSTR),("desktop",w.LPWSTR),("title",w.LPWSTR),
                ("x",w.DWORD),("y",w.DWORD),("xs",w.DWORD),("ys",w.DWORD),("xc",w.DWORD),
                ("yc",w.DWORD),("fill",w.DWORD),("flags",w.DWORD),("show",w.WORD),
                ("cb2",w.WORD),("r2",c.c_void_p),("stdin",w.HANDLE),("stdout",w.HANDLE),("stderr",w.HANDLE)]


class SX(c.Structure):
    _fields_ = [("si",SI),("attrs",c.c_void_p)]


class PI(c.Structure):
    _fields_ = [("process",w.HANDLE),("thread",w.HANDLE),("pid",w.DWORD),("tid",w.DWORD)]


class SC(c.Structure):
    _fields_ = [("sid",c.c_void_p),("caps",c.c_void_p),("count",w.DWORD),("reserved",w.DWORD)]


class SID_ATTRIBUTES(c.Structure):
    _fields_ = [('sid',c.c_void_p),('attributes',w.DWORD)]


class JOB_BASIC(c.Structure):
    _fields_ = [('process_time',c.c_longlong),('job_time',c.c_longlong),('flags',w.DWORD),
                ('min_working_set',c.c_size_t),('max_working_set',c.c_size_t),
                ('active_processes',w.DWORD),('affinity',c.c_size_t),
                ('priority',w.DWORD),('scheduling',w.DWORD)]


class JOB_LIMITS(c.Structure):
    _fields_ = [('basic',JOB_BASIC),('io',c.c_ulonglong*6),('process_memory',c.c_size_t),
                ('job_memory',c.c_size_t),('peak_process_memory',c.c_size_t),('peak_job_memory',c.c_size_t)]


def api():
    if os.name != 'nt':
        raise IsolationUnavailable()
    k = c.WinDLL('kernel32', use_last_error=True)
    u = c.WinDLL('userenv', use_last_error=True)
    k.InitializeProcThreadAttributeList.argtypes = [c.c_void_p,w.DWORD,w.DWORD,c.POINTER(c.c_size_t)]
    k.UpdateProcThreadAttribute.argtypes = [c.c_void_p,w.DWORD,c.c_size_t,c.c_void_p,c.c_size_t,c.c_void_p,c.c_void_p]
    k.DeleteProcThreadAttributeList.argtypes = [c.c_void_p]
    k.CreateProcessW.argtypes = [w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,w.LPCWSTR,c.POINTER(SX),c.POINTER(PI)]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.WaitForSingleObject.argtypes = [w.HANDLE,w.DWORD]
    k.TerminateProcess.argtypes = [w.HANDLE,w.UINT]
    k.GetExitCodeProcess.argtypes = [w.HANDLE,c.POINTER(w.DWORD)]
    k.CreateJobObjectW.argtypes = [c.c_void_p,w.LPCWSTR]
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
    u.DeriveAppContainerSidFromAppContainerName.argtypes = [w.LPCWSTR,c.POINTER(c.c_void_p)]
    return k,u


def check_token(k, process, expected_sid):
    adv = c.WinDLL('advapi32',use_last_error=True)
    adv.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)]
    adv.EqualSid.argtypes=[c.c_void_p,c.c_void_p]
    token=w.HANDLE(); returned=w.DWORD(); is_container=w.DWORD()
    if not adv.OpenProcessToken(process,8,c.byref(token)): raise IsolationUnavailable()
    try:
        if (not adv.GetTokenInformation(token,29,c.byref(is_container),4,c.byref(returned))
                or is_container.value!=1): raise IsolationUnavailable()
        for info in (30,31):  # TokenCapabilities, TokenAppContainerSid
            adv.GetTokenInformation(token,info,None,0,c.byref(returned))
            if not returned.value: raise IsolationUnavailable()
            value=c.create_string_buffer(returned.value)
            if not adv.GetTokenInformation(token,info,value,returned.value,c.byref(returned)):
                raise IsolationUnavailable()
            if info==30:
                if c.cast(value,c.POINTER(w.DWORD)).contents.value!=0: raise IsolationUnavailable()
            elif not adv.EqualSid(c.cast(value,c.POINTER(c.c_void_p)).contents.value,expected_sid):
                raise IsolationUnavailable()
    finally: k.CloseHandle(token)


def check_loopback(k, expected_sid):
    firewall=c.WinDLL('FirewallAPI',use_last_error=True)
    firewall.NetworkIsolationGetAppContainerConfig.argtypes=[c.POINTER(w.DWORD),c.POINTER(c.POINTER(SID_ATTRIBUTES))]
    adv=c.WinDLL('advapi32'); adv.EqualSid.argtypes=[c.c_void_p,c.c_void_p]
    k.GetProcessHeap.restype=w.HANDLE
    k.HeapFree.argtypes=[w.HANDLE,w.DWORD,c.c_void_p]
    count=w.DWORD(); entries=c.POINTER(SID_ATTRIBUTES)()
    if firewall.NetworkIsolationGetAppContainerConfig(c.byref(count),c.byref(entries))!=0:
        raise IsolationUnavailable()
    try:
        if any(adv.EqualSid(entries[i].sid,expected_sid) for i in range(count.value)):
            raise IsolationUnavailable()
    finally:
        heap=k.GetProcessHeap()
        for i in range(count.value): k.HeapFree(heap,0,entries[i].sid)
        if entries: k.HeapFree(heap,0,c.cast(entries,c.c_void_p))


def executable():
    # 路径只由工程配置选择，两处固定位置；请求/外壳不能提供可执行文件。
    config = ROOT / '.local' / 'guide-runtime.json'
    try:
        selected = json.loads(config.read_text(encoding='utf8'))['location']
    except (OSError, KeyError, ValueError, TypeError):
        raise IsolationUnavailable() from None
    if selected == 'project':
        target = ROOT / '.local' / 'guide-runtime' / 'shell.exe'
    elif selected == 'ntfs':
        target = ntfs_runtime() / 'shell.exe'
    else:
        raise IsolationUnavailable()
    if not target.is_file(): raise IsolationUnavailable()
    return target


class Child:
    def __init__(self, *, probe=False, exploration=False):
        import msvcrt
        self.k, u = api()
        self.pi = PI()
        self.reader = self.writer = None
        self.job = None
        self.lines = queue.Queue(maxsize=4)
        self.fault = threading.Event()
        self.thread = self.sender = None
        sid = c.c_void_p()
        attrs = None
        attrs_initialized = False
        handles = []
        try:
            exe = exploration_executable() if exploration else executable()
            if u.DeriveAppContainerSidFromAppContainerName(PROFILE,c.byref(sid)) != 0:
                raise IsolationUnavailable()
            check_loopback(self.k,sid)
            self.job = self.k.CreateJobObjectW(None,None)
            limits=JOB_LIMITS(); limits.basic.flags=0x2008; limits.basic.active_processes=1
            if not self.job or not self.k.SetInformationJobObject(self.job,9,c.byref(limits),c.sizeof(limits)):
                raise IsolationUnavailable()
            read_in, write_in = os.pipe(); handles.extend((read_in,write_in))
            read_out, write_out = os.pipe(); handles.extend((read_out,write_out))
            for fd in handles: os.set_inheritable(fd, False)
            for fd in (read_in, write_out): os.set_inheritable(fd, True)
            inherited = (w.HANDLE * 2)(msvcrt.get_osfhandle(read_in),msvcrt.get_osfhandle(write_out))
            size = c.c_size_t()
            self.k.InitializeProcThreadAttributeList(None,4,0,c.byref(size))
            attrs = c.create_string_buffer(size.value)
            if not self.k.InitializeProcThreadAttributeList(attrs,4,0,c.byref(size)): raise IsolationUnavailable()
            attrs_initialized = True
            caps = SC(sid,None,0,0)
            child_policy = w.DWORD(1) # PROCESS_CREATION_CHILD_PROCESS_RESTRICTED
            jobs=(w.HANDLE*1)(self.job) # 创建时即入job，不留父进程中止导致孤儿的窗口。
            for attribute,value in ((0x20009,caps),(0x20002,inherited),(0x2000E,child_policy),(0x2000D,jobs)):
                if not self.k.UpdateProcThreadAttribute(attrs,0,attribute,c.byref(value),c.sizeof(value),None,None):
                    raise IsolationUnavailable()
            startup = SX(); startup.si.cb = c.sizeof(SX)
            startup.attrs = c.cast(attrs,c.c_void_p); startup.si.flags = 0x100
            startup.si.stdin = inherited[0]; startup.si.stdout = startup.si.stderr = inherited[1]
            # Windows创建AppContainer还需展开用户profile路径；只传系统路径，不继承业务变量。
            allowed_env = _child_environment()
            env = c.create_unicode_buffer('\0'.join(name+'='+value for name,value in sorted(allowed_env.items()))+'\0\0')
            cmd = subprocess.list2cmdline([str(exe)] + (['--probe'] if probe else []))
            # DETACHED_PROCESS避免受限token创建控制台时STATUS_DLL_INIT_FAILED；仅继承管道。
            ok = self.k.CreateProcessW(str(exe),c.create_unicode_buffer(cmd),None,None,True,
                                      0x80000 | 0x400 | 0x8,env,str(exe.parent),c.byref(startup),c.byref(self.pi))
            if not ok: raise IsolationUnavailable('create_process:'+str(c.get_last_error()))
            # 从可信父进程检查实际token；不能只相信外壳的ready文本。
            check_token(self.k,self.pi.process,sid)
            self.reader = os.fdopen(read_out,'rb'); handles.remove(read_out)
            self.writer = os.fdopen(write_in,'wb',buffering=0); handles.remove(write_in)
        except (OSError,ValueError,IsolationUnavailable) as error:
            self.close()
            if isinstance(error,IsolationUnavailable): raise
            raise IsolationUnavailable() from None
        finally:
            for fd in handles: os.close(fd)
            if attrs_initialized: self.k.DeleteProcThreadAttributeList(attrs)
            if sid:
                adv = c.WinDLL('advapi32'); adv.FreeSid.argtypes=[c.c_void_p]; adv.FreeSid(sid)
        def read():
            try:
                while True:
                    line = self.reader.readline(65538)
                    if not line:
                        self.lines.put_nowait(None); break
                    if len(line)>65536 or not line.endswith(b'\n'): raise ValueError()
                    decoded=line[:-1].removesuffix(b'\r').decode('utf8',errors='strict')
                    if '\r' in decoded or '\x00' in decoded: raise ValueError()
                    self.lines.put_nowait(decoded)
            except (OSError,ValueError,UnicodeError,queue.Full):
                self.fault.set()
                try: self.lines.put_nowait(None)
                except queue.Full: pass
        self.thread = threading.Thread(target=read,daemon=True)
        self.thread.start()

    def next_line(self, timeout=IPC_TIMEOUT):
        if self.fault.is_set(): raise IsolationUnavailable()
        if timeout <= 0 or timeout > IPC_TIMEOUT: raise IsolationUnavailable()
        try: line = self.lines.get(timeout=timeout)
        except queue.Empty: raise IsolationUnavailable() from None
        if self.fault.is_set(): raise IsolationUnavailable()
        return line

    def receive(self, timeout=IPC_TIMEOUT):
        line=self.next_line(timeout)
        if line is None: raise IsolationUnavailable()
        return line

    def send(self, line, timeout=IPC_TIMEOUT):
        if timeout <= 0 or timeout > IPC_TIMEOUT: raise IsolationUnavailable()
        try: encoded=line.encode('utf8')
        except (AttributeError,UnicodeError): raise IsolationUnavailable() from None
        if '\n' in line or '\r' in line or '\x00' in line or len(encoded)>32768:
            raise IsolationUnavailable()
        failed=threading.Event()
        def write():
            try:
                remaining=memoryview(encoded+b'\n')
                while remaining:
                    written=self.writer.write(remaining)
                    if not written: raise OSError()
                    remaining=remaining[written:]
            except (OSError,ValueError): failed.set()
        self.sender=threading.Thread(target=write,daemon=True)
        self.sender.start(); self.sender.join(timeout)
        if self.sender.is_alive() or failed.is_set(): raise IsolationUnavailable()

    def finish(self, deadline=None):
        # 匹配文本不足以交付：必须唯一完整结果、EOF及正常退出。
        timeout = IPC_TIMEOUT if deadline is None else _guide_remaining(deadline)
        if self.next_line(timeout) is not None: raise IsolationUnavailable()
        timeout = IPC_TIMEOUT if deadline is None else _guide_remaining(deadline)
        if self.k.WaitForSingleObject(self.pi.process,int(timeout*1000))!=0:
            raise IsolationUnavailable()
        code=w.DWORD()
        if not self.k.GetExitCodeProcess(self.pi.process,c.byref(code)) or code.value!=0:
            raise IsolationUnavailable()

    def close(self):
        if self.job:
            self.k.CloseHandle(self.job); self.job=None
        if self.pi.process:
            if self.k.WaitForSingleObject(self.pi.process,3000) != 0:
                self.k.TerminateProcess(self.pi.process,14)
                self.k.WaitForSingleObject(self.pi.process,3000)
            self.stopped=self.k.WaitForSingleObject(self.pi.process,0)==0
            self.k.CloseHandle(self.pi.process); self.pi.process=None
        if self.pi.thread: self.k.CloseHandle(self.pi.thread); self.pi.thread=None
        for thread in (self.sender,self.thread):
            if thread: thread.join(3)
        for stream in (self.writer,self.reader):
            if stream:
                try: stream.close()
                except (OSError,ValueError): pass
        if (getattr(self,'stopped',True) is not True
                or any(thread and thread.is_alive() for thread in (self.sender,self.thread))):
            raise IsolationUnavailable()

    def __enter__(self): return self
    def __exit__(self,*args): self.close()


def dispatch(child, kind, object_id, observe):
    """唯一工具分派；不接受助手提供身份、其他对象或额外参数。"""
    selected = kind+'\t'+object_id
    if child.receive() != 'observe\t'+selected: raise IsolationUnavailable()
    envelope = observe()
    serialized = json.dumps(envelope, ensure_ascii=True, separators=(',',':'))
    child.send(serialized)
    if child.receive() != 'result\t'+serialized: raise IsolationUnavailable()
    child.finish()
    return envelope


def exchange(kind, object_id, observe):
    # 工程探测未通过时关闭入口；证据绑定此二进制，不能把编译成功当作隔离通过。
    require_evidence()
    with Child() as child:
        if child.receive() != 'ready\tappcontainer': raise IsolationUnavailable()
        child.send(kind+'\t'+object_id)
        return dispatch(child,kind,object_id,observe) # 永不信任或转发外壳附加文本。


def _guide_remaining(deadline):
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise IsolationUnavailable()
    return min(IPC_TIMEOUT, remaining)


def _guide_reference(delegation_ref):
    # 仅宿主一次性委托引用；真实对象/会话和授权绑定不进入原生进程。
    if not isinstance(delegation_ref, str) or re.fullmatch('[0-9a-f]{32}', delegation_ref) is None:
        raise IsolationUnavailable()
    return delegation_ref


def _guide_candidate(candidate):
    if not isinstance(candidate, dict): raise IsolationUnavailable()
    try:
        serialized = json.dumps(candidate, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError):
        raise IsolationUnavailable() from None
    if len(serialized.encode('ascii')) > GUIDE_CANDIDATE_BYTES:
        raise IsolationUnavailable()
    return serialized


def _complete_guide(child, reference, serialized, deadline):
    _guide_remaining(deadline)
    if child.fault.is_set() or not child.lines.empty(): raise IsolationUnavailable()
    child.send('guide-candidate\t' + reference + '\t' + serialized,
               _guide_remaining(deadline))
    if child.receive(_guide_remaining(deadline)) != 'guide-result\t' + reference + '\t' + serialized:
        raise IsolationUnavailable()
    child.finish(deadline=deadline)


class GuideSession:
    """一次原生请求/候选往返；GuideCandidate语义由可信协调器另行严格校验。

    Child是实际AppContainer。此低层类仅供工程反例直接使用；产品入口必须使用
    begin_guide。网络等待阶段不读空管道，8秒只约束实际IPC，不延长整体30秒。
    阻塞方法须由协调器的专用有界执行单元执行。
    """
    def __init__(self, delegation_ref, remaining_seconds):
        self.reference = _guide_reference(delegation_ref)
        if (isinstance(remaining_seconds, bool) or not isinstance(remaining_seconds, (int, float))
                or not math.isfinite(remaining_seconds)
                or not 0 < remaining_seconds <= GUIDE_TIMEOUT):
            raise IsolationUnavailable()
        self.deadline = time.monotonic() + remaining_seconds
        self.child = None
        self._aborted = threading.Event()
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._completed = self._closed = False
        try:
            self.child = Child()
            if self.child.receive(_guide_remaining(self.deadline)) != 'ready\tappcontainer':
                raise IsolationUnavailable()
            self.child.send('start-guide\t' + self.reference, _guide_remaining(self.deadline))
            if self.child.receive(_guide_remaining(self.deadline)) != 'guide-request\t' + self.reference:
                raise IsolationUnavailable()
        except BaseException:
            self.abort()
            raise

    def complete(self, candidate):
        with self._state_lock:
            if self._completed or self._aborted.is_set(): raise IsolationUnavailable()
            self._completed = True
        try:
            serialized = _guide_candidate(candidate)
            _complete_guide(self.child, self.reference, serialized, self.deadline)
            if self._aborted.is_set(): raise IsolationUnavailable()
            return json.loads(serialized)
        finally:
            self.close()

    def abort(self):
        self._aborted.set()
        self.close()

    def close(self):
        with self._close_lock:
            if self._closed: return
            self._closed = True
            if self.child is not None: self.child.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def begin_guide(delegation_ref, remaining_seconds):
    # 任何产品模型请求前必须已取得完整、绑定当前二进制/启动器的真实证据。
    _guide_reference(delegation_ref)
    if (isinstance(remaining_seconds, bool) or not isinstance(remaining_seconds, (int, float))
            or not math.isfinite(remaining_seconds) or not 0 < remaining_seconds <= GUIDE_TIMEOUT):
        raise IsolationUnavailable()
    deadline = time.monotonic() + remaining_seconds
    require_evidence()
    credential = ROOT / '.local/guide-model.json'
    if not credential.is_file():
        raise IsolationUnavailable()
    # 针对当前新增文件实际测CreateFile拒绝；不读、散列或传递秘密内容。
    with Child(probe=True) as child:
        if child.receive(_guide_remaining(deadline)) != 'ready\tappcontainer':
            raise IsolationUnavailable()
        child.send('file\t' + str(credential), _guide_remaining(deadline))
        if child.receive(_guide_remaining(deadline)) != 'denied\t5':
            raise IsolationUnavailable()
        child.send('end', _guide_remaining(deadline))
        child.finish(deadline=deadline)
    return GuideSession(delegation_ref, deadline - time.monotonic())


def require_evidence():
    try:
        report=json.loads((ROOT/'.local/d2-01-isolation.json').read_text(encoding='utf8'))
        if (not isinstance(report,dict) or report.get('schema_version')!=PROBE_SCHEMA
                or report.get('passed') is not True or report.get('profile')!=PROFILE
                or report.get('network_capabilities')!=[]
                or report.get('loopback_exempt') is not False
                or report.get('host_pg_connected') is not True
                or report.get('host_spawn_exit')!=11
                or report.get('binary_sha256')!=hashlib.sha256(executable().read_bytes()).hexdigest()
                or report.get('launcher_sha256')!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
            raise IsolationUnavailable()
        checked=datetime.fromisoformat(report['checked_at'])
        age=datetime.now(timezone.utc)-checked
        if age<timedelta(0) or age>PROBE_MAX_AGE: raise IsolationUnavailable()
        checks=report['checks']
        expected=set(FILE_CHECKS)|set(NETWORK_CHECKS)|{'child-process'}
        if len(checks)!=len(expected) or {r['resource'] for r in checks}!=expected:
            raise IsolationUnavailable()
        for item in checks:
            resource=item['resource']; result=item['result']
            if resource in FILE_CHECKS: valid=result=='denied\t5'
            elif resource=='child-process': valid=result in ('spawn\t5','spawn\t367','spawn\t1816')
            elif resource.startswith('arbitrary-udp'):
                valid=(result in ('socket\t10013','udp-no-response') and item.get('host_control') is True)
            elif resource.startswith('arbitrary'): valid=result=='socket\t10013'
            elif resource.startswith('live-'):
                valid=(result in ('timeout','socket\t10013','udp-no-response') and item.get('host_control') is True
                       and item.get('child_received') is False)
            else: valid=result in ('timeout','socket\t10013')
            if not valid: raise IsolationUnavailable()
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        raise IsolationUnavailable() from None


# P22使用独立二进制和证据；旧shell/配置/历史probe保留，不自动prepare或扩权。
EXPLORATION_TIMEOUT = 180
EXPLORATION_OPERATIONS = 28
EXPLORATION_OBSERVATION_BYTES = 12288
EXPLORATION_REPORT = '.local/d3-22/native-isolation.json'
def exploration_report_path(report_path=None):
    """工程显式选取当轮报告；省略参数保留历史22读取路径。"""
    return (ROOT / (EXPLORATION_REPORT if report_path is None else report_path)).resolve()


EXPLORATION_FILE_CHECKS = FILE_CHECKS + ('.local/guide-model.json', 'scripts/round21_explore.py',
    '.local/d3-20/sandbox/business.json', '.local/d3-20/sandbox/postgres-admin.json',
    'scripts/round22_explore.py')


def exploration_executable():
    selected = ntfs_runtime() / 'explore-shell.exe'
    if not selected.is_file(): raise IsolationUnavailable()
    return selected


def _exploration_limit(remaining_seconds):
    if (isinstance(remaining_seconds, bool) or not isinstance(remaining_seconds, (int, float))
            or not math.isfinite(remaining_seconds) or not 0 < remaining_seconds <= EXPLORATION_TIMEOUT):
        raise IsolationUnavailable()


def _exploration_action(operation, target, seq):
    from .exploration import Operation
    try:
        Operation.model_validate({'operation': operation, 'target': target})
    except (ValueError, TypeError):
        raise IsolationUnavailable() from None
    if type(seq) is not int or not 1 <= seq <= EXPLORATION_OPERATIONS:
        raise IsolationUnavailable()
    return operation + '\t' + ('-' if target is None else target)


def _exploration_observation(observation):
    # 调用者必须先经过运行时的匿名语义观察校验；此处仍独立限制JSON和字节。
    if not isinstance(observation, dict): raise IsolationUnavailable()
    try:
        serialized = json.dumps(observation, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError):
        raise IsolationUnavailable() from None
    if len(serialized.encode('ascii')) > EXPLORATION_OBSERVATION_BYTES:
        raise IsolationUnavailable()
    return serialized


class ExplorationSession:
    """一次委托的有界原生许可；broker必须在authorize成功返回后才执行动作。

    每个候选使用独立AppContainer，使附加输出/非零退出/挂起在任何业务写之前
    被拒绝；不能仅收到匹配permit就执行写操作。该类不包含HTTP/凭证/SQL能力。
    """
    def __init__(self, delegation_ref, remaining_seconds, *, require_evidence=False, report_path=None):
        self.reference = _guide_reference(delegation_ref)
        if type(require_evidence) is not bool: raise IsolationUnavailable()
        _exploration_limit(remaining_seconds)
        self.deadline = time.monotonic() + remaining_seconds
        self._aborted = threading.Event()
        self._operation_lock = threading.Lock()
        self._child_lock = threading.Lock()
        self._child = None
        self._sequence = 0
        self._require_evidence = require_evidence
        self._report_path = exploration_report_path(report_path) if report_path is not None else None

    def _check(self):
        if self._aborted.is_set(): raise IsolationUnavailable()
        return _guide_remaining(self.deadline)

    def authorize(self, observation, operation, target, seq):
        # 无并发候选；错序/重放/取消/超额在创建子进程前拒绝。
        with self._operation_lock:
            self._check()
            selected = _exploration_action(operation, target, seq)
            serialized = _exploration_observation(observation)
            if seq != self._sequence + 1: raise IsolationUnavailable()
            if self._require_evidence:
                if self._report_path is None: require_exploration_evidence()
                else: require_exploration_evidence(report_path=self._report_path)
            key = self.reference + '\t' + str(seq)
            child = None
            try:
                with self._child_lock:
                    self._check()
                    child = Child(exploration=True)
                    self._child = child
                if child.receive(self._check()) != 'ready\tappcontainer': raise IsolationUnavailable()
                child.send('start-explore\t' + key, self._check())
                if child.receive(self._check()) != 'explore-observe-request\t' + key:
                    raise IsolationUnavailable()
                if child.fault.is_set() or not child.lines.empty(): raise IsolationUnavailable()
                child.send('explore-observation\t' + key + '\t' + serialized, self._check())
                if child.receive(self._check()) != 'explore-candidate-request\t' + key:
                    raise IsolationUnavailable()
                if child.fault.is_set() or not child.lines.empty(): raise IsolationUnavailable()
                child.send('explore-action\t' + key + '\t' + selected, self._check())
                if child.receive(self._check()) != 'explore-permit\t' + key + '\t' + selected:
                    raise IsolationUnavailable()
                child.finish(deadline=self.deadline)
                self._check()
                self._sequence = seq
            except BaseException:
                self._aborted.set()
                raise
            finally:
                # abort与finally共享同一把锁，不能并发关闭同一个原生句柄。
                with self._child_lock:
                    if self._child is not None:
                        self._child.close()
                        self._child = None

    def abort(self):
        self._aborted.set()
        with self._child_lock:
            if self._child is not None:
                self._child.close()
                self._child = None

    close = abort
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def begin_exploration(delegation_ref, remaining_seconds, *, report_path=None):
    _guide_reference(delegation_ref)
    _exploration_limit(remaining_seconds)
    deadline = time.monotonic() + remaining_seconds
    selected = exploration_report_path(report_path) if report_path is not None else None
    if selected is None: require_exploration_evidence()
    else: require_exploration_evidence(report_path=selected)
    return ExplorationSession(delegation_ref, deadline - time.monotonic(), require_evidence=True, report_path=selected)


def require_exploration_evidence(*, report_path=None):
    """新执行单元独立失败关闭；旧probe或编译成功均不能授权探索。"""
    try:
        report = json.loads(exploration_report_path(report_path).read_text(encoding='utf8'))
        if (not isinstance(report, dict) or report.get('schema_version') != 1
                or report.get('stage') != 'complete' or report.get('appcontainer_started') is not True
                or report.get('child_reaped') is not True or report.get('passed') is not True or report.get('profile') != PROFILE
                or report.get('network_capabilities') != [] or report.get('loopback_exempt') is not False
                or report.get('binary_sha256') != hashlib.sha256(exploration_executable().read_bytes()).hexdigest()
                or report.get('launcher_sha256') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                or report.get('source_sha256') != hashlib.sha256((ROOT/'services/guide/shell.c').read_bytes()).hexdigest()
                or report.get('host_spawn_exit') != 11):
            raise IsolationUnavailable()
        age = datetime.now(timezone.utc) - datetime.fromisoformat(report['checked_at'])
        if age < timedelta(0) or age > PROBE_MAX_AGE: raise IsolationUnavailable()
        checks = report['checks']
        expected = set(EXPLORATION_FILE_CHECKS) | {'child-process', 'live-tcp4', 'live-tcp6',
            'live-udp4', 'live-udp6', 'live-lan-udp4', 'postgres', 'business-http',
            'arbitrary-network', 'arbitrary-tcp6', 'arbitrary-udp4', 'arbitrary-udp6'}
        if len(checks) != len(expected) or {r['resource'] for r in checks} != expected:
            raise IsolationUnavailable()
        for item in checks:
            resource, result = item['resource'], item['result']
            if resource in EXPLORATION_FILE_CHECKS: valid = result == 'denied\t5'
            elif resource == 'child-process': valid = result in ('spawn\t5', 'spawn\t367', 'spawn\t1816')
            elif resource.startswith('live-'):
                valid = (result in ('timeout','socket\t10013','udp-no-response')
                         and item.get('host_control') is True and item.get('child_received') is False)
            elif resource.startswith('arbitrary-udp'):
                valid = (result in ('socket\t10013','udp-no-response') and item.get('host_control') is True)
            elif resource.startswith('arbitrary'): valid = result == 'socket\t10013'
            else: valid = result in ('timeout','socket\t10013') and item.get('host_control') is True
            if not valid: raise IsolationUnavailable()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise IsolationUnavailable() from None
