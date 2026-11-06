"""DS-1007原技术费用账本；DS-1011复用额度与历史，工程侧显式初始化。"""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import hashlib
import re
import math
import os
from pathlib import Path
import threading
import time
import uuid


PACKAGE = 'DS-1007'
MAX_INPUT = 8000
MAX_OUTPUT = 2000
INPUT_PRICE = Decimal('2')
CACHE_PRICE = Decimal('0.04')
OUTPUT_PRICE = Decimal('8')
# DS-1011沿用会计预留；短输入不证明供应商账单绝不超出此金额。
BASE_RESERVATION = Decimal('0.032')
# Q14（2026-10-15）只将同一原账本累计次数16→22；不另建额度或改变金额停止线。
EXPERIMENT_REQUEST_CEILING = 22
EXPERIMENT_ACCOUNTED_CEILING = Decimal('0.5')
_locks = {}
_locks_guard = threading.Lock()


class BudgetUnavailable(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def money(value):
    if not isinstance(value, (str, Decimal)):
        raise BudgetUnavailable('ledger_corrupt')
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise BudgetUnavailable('ledger_corrupt') from None
    if not parsed.is_finite() or parsed < 0:
        raise BudgetUnavailable('ledger_corrupt')
    return parsed


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def usage_cost(prompt_tokens, completion_tokens, cache_hit_tokens=0, cache_miss_tokens=None):
    if (not _integer(prompt_tokens, MAX_INPUT) or not _integer(completion_tokens, MAX_OUTPUT)
            or not _integer(cache_hit_tokens, MAX_INPUT)):
        raise BudgetUnavailable('usage_out_of_bounds')
    miss = prompt_tokens - cache_hit_tokens if cache_miss_tokens is None else cache_miss_tokens
    if not _integer(miss, MAX_INPUT) or cache_hit_tokens + miss != prompt_tokens:
        raise BudgetUnavailable('usage_out_of_bounds')
    # 不假设节假日/优惠。真实账单可更低；此金额是可信usage按高峰价核算的上界。
    return (Decimal(miss) * INPUT_PRICE + Decimal(cache_hit_tokens) * CACHE_PRICE
            + Decimal(completion_tokens) * OUTPUT_PRICE) / Decimal(1000000)


_Q16_SEAL = object()
Q16_PACKAGE = 'Q16-P22-shared-two'
_Q17_SEAL = object()
Q17_PACKAGE = 'Q17-P24-shared-two'
_Q18_SEAL = object()
Q18_PACKAGE = 'Q18-P26-compact-two'


@dataclass(frozen=True)
class _Q16Permit:
    scope_hash: str
    grant_path: Path
    seal: object


def _q16_permit_for_validated_input(scope_hash, grant_path):
    # 仅可信prepare对已经input_validator校验的实际输入计算摘要后调用。
    return _Q16Permit(scope_hash, Path(grant_path), _Q16_SEAL)


@dataclass(frozen=True)
class _Q17Permit:
    scope_hash: str
    grant_path: Path
    seal: object


def _q17_permit_for_validated_input(scope_hash, grant_path):
    return _Q17Permit(scope_hash, Path(grant_path), _Q17_SEAL)


@dataclass(frozen=True)
class _Q18Permit:
    scope_hash: str
    grant_path: Path
    seal: object


def _q18_permit(scope_hash, grant_path):
    # 仅可信prepare对已经input_validator校验的实际紧凑512输入计算摘要后调用。
    return _Q18Permit(scope_hash, Path(grant_path), _Q18_SEAL)


@dataclass(frozen=True)
class Reservation:
    request_id: str
    amount_cny: Decimal


class BudgetLedger:
    """线程锁 + 本机文件锁；网络等待期间不持文件锁/PG连接。"""
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.marker = self.path.with_name(self.path.name + '.initialized')
        self.lock_path = self.path.with_name(self.path.name + '.lock')
        self.q16_path = self.path.with_name(self.path.name + '.q16.json')
        self.q16_stop_path = self.path.with_name(self.path.name + '.q16-stopped.json')
        self.q17_path = self.path.with_name(self.path.name + '.q17.json')
        self.q17_stop_path = self.path.with_name(self.path.name + '.q17-stopped.json')
        self.q18_path = self.path.with_name(self.path.name + '.q18.json')
        self.q18_stop_path = self.path.with_name(self.path.name + '.q18-stopped.json')
        self.q19_path = self.path.with_name(self.path.name + '.q19.json')
        self.q19_stop_path = self.path.with_name(self.path.name + '.q19-stopped.json')
        self.q20_path = self.path.with_name(self.path.name + '.q20.json')
        self.q20_stop_path = self.path.with_name(self.path.name + '.q20-stopped.json')
        self.q21_path = self.path.with_name(self.path.name + '.q21.json')
        self.q21_stop_path = self.path.with_name(self.path.name + '.q21-stopped.json')
        self.q22_path = self.path.with_name(self.path.name + '.q22.json')
        self.q22_stop_path = self.path.with_name(self.path.name + '.q22-stopped.json')
        self.q23_path = self.path.with_name(self.path.name + '.q23.json')
        self.q23_stop_path = self.path.with_name(self.path.name + '.q23-stopped.json')
        self.q24_path = self.path.with_name(self.path.name + '.q24.json')
        self.q24_stop_path = self.path.with_name(self.path.name + '.q24-stopped.json')
        self.q25_path = self.path.with_name(self.path.name + '.q25.json')
        self.q25_stop_path = self.path.with_name(self.path.name + '.q25-stopped.json')
        self.q26_path = self.path.with_name(self.path.name + '.q26.json')
        self.q26_stop_path = self.path.with_name(self.path.name + '.q26-stopped.json')
        self.q27_path = self.path.with_name(self.path.name + '.q27.json')
        self.q27_stop_path = self.path.with_name(self.path.name + '.q27-stopped.json')
        self.q28_path = self.path.with_name(self.path.name + '.q28.json')
        self.q28_stop_path = self.path.with_name(self.path.name + '.q28-stopped.json')
        self.q29_path = self.path.with_name(self.path.name + '.q29.json')
        self.q29_stop_path = self.path.with_name(self.path.name + '.q29-stopped.json')
        self.q30_path = self.path.with_name(self.path.name + '.q30.json')
        self.q30_stop_path = self.path.with_name(self.path.name + '.q30-stopped.json')
        self.q31_path = self.path.with_name(self.path.name + '.q31.json')
        self.q31_stop_path = self.path.with_name(self.path.name + '.q31-stopped.json')
        self.q32_path = self.path.with_name(self.path.name + '.q32.json')
        self.q32_stop_path = self.path.with_name(self.path.name + '.q32-stopped.json')
        self.q33_path = self.path.with_name(self.path.name + '.q33.json')
        self.q33_stop_path = self.path.with_name(self.path.name + '.q33-stopped.json')
        self.q34_path = self.path.with_name(self.path.name + '.q34.json')
        self.q34_stop_path = self.path.with_name(self.path.name + '.q34-stopped.json')
        self.q35_path = self.path.with_name(self.path.name + '.q35.json')
        self.q35_stop_path = self.path.with_name(self.path.name + '.q35-stopped.json')
        self.q36_path = self.path.with_name(self.path.name + '.q36.json')
        self.q36_stop_path = self.path.with_name(self.path.name + '.q36-stopped.json')
        self.clock = clock
        key = str(self.path.resolve()).casefold()
        with _locks_guard:
            self.thread_lock = _locks.setdefault(key, threading.Lock())

    @contextmanager
    def _locked(self, *, existing_only=True):
        if not self.path.parent.is_dir():
            raise BudgetUnavailable('ledger_uninitialized')
        if not self.thread_lock.acquire(blocking=False):
            raise BudgetUnavailable('budget_busy')
        handle = None
        locked = False
        try:
            if existing_only:
                if not any(path.exists() for path in (self.path, self.marker, self.lock_path)):
                    raise BudgetUnavailable('ledger_uninitialized')
            elif self.path.exists() or self.marker.exists():
                raise BudgetUnavailable('initialization_already_recorded')
            # 只有显式初始化可排他创建锁；已有空锁或残留也不修补。
            handle = self.lock_path.open('r+b' if existing_only else 'x+b')
            handle.seek(0, 2)
            if handle.tell() == 0:
                if existing_only:
                    raise BudgetUnavailable('ledger_corrupt')
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            yield handle
        except FileExistsError:
            raise BudgetUnavailable('initialization_already_recorded') from None
        except FileNotFoundError:
            raise BudgetUnavailable('ledger_missing') from None
        except OSError:
            raise BudgetUnavailable('ledger_io_or_lock') from None
        finally:
            if handle is not None:
                if locked:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            self.thread_lock.release()

    @staticmethod
    def _write(path, value):
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8', newline='\n') as handle:
                json.dump(value, handle, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def initialize(self):
        """仅首次可执行；任一账本/标记/锁残留均拒绝清零或修补。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(existing_only=False) as lock_handle:
            lock_handle.seek(1)
            anchor = lock_handle.read(512)
            if self.path.exists() or self.marker.exists() or anchor:
                raise BudgetUnavailable('initialization_already_recorded')
            ledger_id = uuid.uuid4().hex
            now = self._now()
            # .lock内另存首次锚点；账本和.initialized同时丢失也不能自动初始化。
            lock_handle.seek(1)
            lock_handle.write(json.dumps({'package': PACKAGE, 'ledger_id': ledger_id},
                                         separators=(',', ':')).encode('ascii'))
            lock_handle.flush()
            os.fsync(lock_handle.fileno())
            # 中途崩溃造成缺任一文件时，后续检查失败关闭；不自动修补/清零。
            self._write(self.marker, {'schema': 1, 'package': PACKAGE, 'ledger_id': ledger_id})
            self._write(self.path, {'schema': 1, 'package': PACKAGE, 'currency': 'CNY',
                                   'ledger_id': ledger_id, 'phase': 'first',
                                   'initialized_at': now, 'requests': []})

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise BudgetUnavailable('clock_invalid')
        return now

    @staticmethod
    def _strict_json(raw):
        def unique_pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError('duplicate key')
                value[key] = item
            return value
        try:
            return json.loads(raw, object_pairs_hook=unique_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
        except (ValueError, UnicodeError, RecursionError):
            raise BudgetUnavailable('ledger_corrupt') from None

    @staticmethod
    def _read_json(path):
        try:
            if path.stat().st_size > 131072:
                raise BudgetUnavailable('ledger_corrupt')
            return BudgetLedger._strict_json(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise BudgetUnavailable('ledger_missing') from None
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise BudgetUnavailable('ledger_corrupt') from None

    def _load(self):
        if not self.path.exists() and not self.marker.exists():
            if self.lock_path.stat().st_size > 1:
                raise BudgetUnavailable('ledger_missing')
            raise BudgetUnavailable('ledger_uninitialized')
        marker = self._read_json(self.marker)
        ledger = self._read_json(self.path)
        try:
            # Windows锁住第0字节；另一个句柄不读取锁区，以免自触发LOCK_VIOLATION。
            with self.lock_path.open('rb', buffering=0) as anchor_handle:
                anchor_handle.seek(1)
                anchor_bytes = anchor_handle.read(513)
            if len(anchor_bytes) > 512:
                raise BudgetUnavailable('ledger_corrupt')
            anchor = self._strict_json(anchor_bytes)
        except (OSError, ValueError, UnicodeError):
            raise BudgetUnavailable('ledger_corrupt') from None
        if type(marker) is not dict or set(marker) != {'schema', 'package', 'ledger_id'}:
            raise BudgetUnavailable('ledger_corrupt')
        if type(ledger) is not dict or set(ledger) != {'schema', 'package', 'currency', 'ledger_id',
                                                    'phase', 'initialized_at', 'requests'}:
            raise BudgetUnavailable('ledger_corrupt')
        if (type(marker['schema']) is not int or type(ledger['schema']) is not int
                or marker['schema'] != 1 or ledger['schema'] != 1 or marker['package'] != PACKAGE
                or ledger['package'] != PACKAGE or ledger['currency'] != 'CNY'
                or marker['ledger_id'] != ledger['ledger_id']
                or type(ledger['ledger_id']) is not str or len(ledger['ledger_id']) != 32
                or ledger['phase'] not in ('first', 'cumulative')
                or type(ledger['requests']) is not list or len(ledger['requests']) > 100):
            raise BudgetUnavailable('ledger_corrupt')
        if anchor != {'package': PACKAGE, 'ledger_id': ledger['ledger_id']}:
            raise BudgetUnavailable('ledger_corrupt')
        now = self._now()
        if type(ledger['initialized_at']) not in (int, float) or not 0 < ledger['initialized_at'] <= now:
            raise BudgetUnavailable('clock_or_ledger_invalid')
        seen = set()
        receipts = set()
        for item in ledger['requests']:
            if type(item) is not dict or set(item) != {'request_id', 'phase', 'reserved_cny',
                    'additional_fee_cny', 'state', 'accounted_cny', 'prompt_tokens', 'completion_tokens',
                    'cache_hit_tokens', 'cache_miss_tokens',
                    'provider_request_id', 'dispatched_at', 'reason'}:
                raise BudgetUnavailable('ledger_corrupt')
            if (type(item['request_id']) is not str or len(item['request_id']) != 32
                    or item['request_id'] in seen or item['phase'] not in ('first', 'cumulative')
                    or item['state'] not in ('pending', 'unresolved', 'settled')
                    or type(item['dispatched_at']) not in (int, float)
                    or not 0 < item['dispatched_at'] <= now):
                raise BudgetUnavailable('ledger_corrupt')
            seen.add(item['request_id'])
            fee = money(item['additional_fee_cny'])
            if money(item['reserved_cny']) != BASE_RESERVATION + fee:
                raise BudgetUnavailable('ledger_corrupt')
            if item['state'] == 'settled':
                calculated = usage_cost(item['prompt_tokens'], item['completion_tokens'],
                                        item['cache_hit_tokens'], item['cache_miss_tokens']) + fee
                if (money(item['accounted_cny']) != calculated or type(item['provider_request_id']) is not str
                        or not 1 <= len(item['provider_request_id']) <= 128
                        or any(ord(char) < 32 for char in item['provider_request_id'])
                        or item['provider_request_id'] in receipts):
                    raise BudgetUnavailable('ledger_corrupt')
                receipts.add(item['provider_request_id'])
            elif any(item[field] is not None for field in ('accounted_cny', 'prompt_tokens',
                    'completion_tokens', 'cache_hit_tokens', 'cache_miss_tokens', 'provider_request_id')):
                raise BudgetUnavailable('ledger_corrupt')
            if item['reason'] is not None and (type(item['reason']) is not str or len(item['reason']) > 64):
                raise BudgetUnavailable('ledger_corrupt')
        first = [item for item in ledger['requests'] if item['phase'] == 'first']
        # 超过50只接受匹配新包前缀、意图和全部历史侧车的显式特例。
        first_limit = 50
        if 50 < len(first) <= 57 and self.q36_path.exists():
            from .task_budget import _load_locked
            _load_locked(self, ledger, batch='Q36', check_stopped=False)
            first_limit = 57
        elif 50 < len(first) <= 59 and self.q35_path.exists():
            from .task_budget import _load_locked
            _load_locked(self, ledger, batch='Q35', check_stopped=False)
            first_limit = 59
        elif 50 < len(first) <= 52:
            from .task_budget import _load_locked
            _load_locked(self, ledger, batch='Q34', check_stopped=False)
            first_limit = 52
        if (len(first) > first_limit or self._amount(first) > Decimal('2')
                or self._amount(ledger['requests']) > Decimal('5')
                or (ledger['phase'] == 'first' and len(first) != len(ledger['requests']))):
            raise BudgetUnavailable('ledger_corrupt')
        return ledger

    @staticmethod
    def _amount(items):
        return sum((money(item['accounted_cny'] if item['state'] == 'settled' else item['reserved_cny'])
                    for item in items), Decimal(0))


    @staticmethod
    def _q16_prefix(rows):
        return hashlib.sha256(json.dumps(rows[:22], ensure_ascii=True, allow_nan=False,
                                        sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()

    @staticmethod
    def _q16_hash(value):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None

    def create_q16_grant(self, scope_hashes):
        """工程侧显式登记已批准的两个精确样本；不由产品/初始化调用，不重建许可。

        复用同一原账本锁，只创建作用域许可侧车，不更改原22条或三件账本结构。
        调用者必须已取得用户对实际预览的Q16批准，此方法不自动产生该批准。
        """
        if (type(scope_hashes) not in (tuple, list) or len(scope_hashes) != 2
                or not all(self._q16_hash(s) for s in scope_hashes) or len(set(scope_hashes)) != 2):
            raise BudgetUnavailable('q16_scope_invalid')
        with self._locked():
            ledger = self._load()
            if self.q16_path.exists(): raise BudgetUnavailable('q16_already_recorded')
            rows = ledger['requests']
            if len(rows) != 22: raise BudgetUnavailable('q16_prefix_mismatch')
            if any(row['state'] != 'settled' for row in rows): raise BudgetUnavailable('usage_unresolved')
            if self._amount(rows) >= EXPERIMENT_ACCOUNTED_CEILING:
                raise BudgetUnavailable('experiment_budget_stop')
            value = {'schema': 1, 'package': Q16_PACKAGE, 'approved': True,
                     'approved_on': '2026-10-17', 'source_kind': 'explicit_user_reply',
                     'ledger_id': ledger['ledger_id'], 'prefix_requests': 22,
                     'prefix_sha256': self._q16_prefix(rows), 'scope_hashes': list(scope_hashes),
                     'intents': []}
            self._write(self.q16_path, value)
            return dict(value)

    def stop_q16_batch(self, reason):
        """工程执行失败后永久关闭本次许可；结算事实保留，剩余次数不能继续使用。"""
        if type(reason) is not str or not re.fullmatch('[a-z0-9_]{1,64}', reason):
            raise BudgetUnavailable('q16_stop_reason_invalid')
        with self._locked():
            ledger = self._load()
            if not self.q16_path.is_file(): raise BudgetUnavailable('q16_grant_missing')
            if not self.q16_stop_path.exists():
                self._write(self.q16_stop_path, {'package': Q16_PACKAGE,
                    'ledger_id': ledger['ledger_id'], 'physical_requests': len(ledger['requests']),
                    'reason': reason, 'stopped_at': self._now()})

    def _q16_grant(self, ledger, scope_hash):
        """必须持同一锁；任何未完成intent或原件/序号/账本关联不符均失败关闭。"""
        if self.q16_stop_path.exists(): raise BudgetUnavailable('q16_batch_stopped')
        if not self._q16_hash(scope_hash): raise BudgetUnavailable('q16_scope_invalid')
        value = self._read_json(self.q16_path)
        rows = ledger['requests']
        fields = {'schema','package','approved','approved_on','source_kind','ledger_id',
                  'prefix_requests','prefix_sha256','scope_hashes','intents'}
        if (type(value) is not dict or set(value) != fields
                or type(value['schema']) is not int or value['schema'] != 1
                or value['package'] != Q16_PACKAGE or value['approved'] is not True
                or value['approved_on'] != '2026-10-17' or value['source_kind'] != 'explicit_user_reply'
                or value['ledger_id'] != ledger['ledger_id']
                or type(value['prefix_requests']) is not int or value['prefix_requests'] != 22
                or len(rows) < 22 or value['prefix_sha256'] != self._q16_prefix(rows)
                or type(value['scope_hashes']) is not list or len(value['scope_hashes']) != 2
                or not all(self._q16_hash(item) for item in value['scope_hashes'])
                or len(set(value['scope_hashes'])) != 2 or type(value['intents']) is not list
                or len(value['intents']) > 2):
            raise BudgetUnavailable('q16_grant_invalid')
        for index, intent in enumerate(value['intents']):
            if (type(intent) is not dict or set(intent) != {'sequence','scope_hash','request_id','state'}
                    or type(intent['sequence']) is not int or intent['sequence'] != index + 1
                    or intent['scope_hash'] != value['scope_hashes'][index]
                    or not isinstance(intent['request_id'], str)
                    or re.fullmatch('[0-9a-f]{32}', intent['request_id']) is None
                    or intent['state'] != 'reserved' or len(rows) <= 22 + index
                    or rows[22 + index]['request_id'] != intent['request_id']):
                raise BudgetUnavailable('q16_intent_unresolved')
        if len(rows) != 22 + len(value['intents']): raise BudgetUnavailable('q16_prefix_mismatch')
        index = len(value['intents'])
        if index >= 2: raise BudgetUnavailable('q16_budget_stop')
        if value['scope_hashes'][index] != scope_hash: raise BudgetUnavailable('q16_scope_or_order_mismatch')
        return value

    def check_q16_scope(self, scope_hash):
        """精确样本只读预检；实际reserve仍在锁内重新校验，不预留/不消费。"""
        with self._locked():
            ledger = self._load()
            if self.q16_stop_path.exists(): raise BudgetUnavailable('q16_batch_stopped')
            if any(row['state'] != 'settled' for row in ledger['requests']):
                raise BudgetUnavailable('usage_unresolved')
            if self._amount(ledger['requests']) >= EXPERIMENT_ACCOUNTED_CEILING:
                raise BudgetUnavailable('experiment_budget_stop')
            self._q16_grant(ledger, scope_hash)
            return True

    @staticmethod
    def _q17_prefix(rows):
        return hashlib.sha256(json.dumps(rows[:23], ensure_ascii=True, allow_nan=False,
                                        sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()

    def _q17_q16_binding(self, ledger):
        """Q17只接原23笔且Q16首题失败停止的链；旧两侧车逐字保留。"""
        try:
            raw = self.q16_path.read_bytes()
            stopped_raw = self.q16_stop_path.read_bytes()
            if len(raw) > 131072 or len(stopped_raw) > 4096:
                raise BudgetUnavailable('q17_legacy_invalid')
            value = self._strict_json(raw)
            stopped = self._strict_json(stopped_raw)
            rows = ledger['requests']
            fields = {'schema','package','approved','approved_on','source_kind','ledger_id',
                      'prefix_requests','prefix_sha256','scope_hashes','intents'}
            if (type(value) is not dict or set(value) != fields or type(value['schema']) is not int
                    or value['schema'] != 1 or value['package'] != Q16_PACKAGE or value['approved'] is not True
                    or value['approved_on'] != '2026-10-17' or value['source_kind'] != 'explicit_user_reply'
                    or value['ledger_id'] != ledger['ledger_id'] or type(value['prefix_requests']) is not int
                    or value['prefix_requests'] != 22 or len(rows) < 23
                    or value['prefix_sha256'] != self._q16_prefix(rows)
                    or type(value['scope_hashes']) is not list or len(value['scope_hashes']) != 2
                    or not all(self._q16_hash(item) for item in value['scope_hashes'])
                    or len(set(value['scope_hashes'])) != 2 or type(value['intents']) is not list
                    or value['intents'] != [{'sequence': 1, 'scope_hash': value['scope_hashes'][0],
                         'request_id': rows[22]['request_id'], 'state': 'reserved'}]
                    or type(value['intents'][0]['sequence']) is not int):
                raise BudgetUnavailable('q17_legacy_invalid')
            if (type(stopped) is not dict or set(stopped) != {
                    'package','ledger_id','physical_requests','reason','stopped_at'}
                    or stopped['package'] != Q16_PACKAGE or stopped['ledger_id'] != ledger['ledger_id']
                    or type(stopped['physical_requests']) is not int or stopped['physical_requests'] != 23
                    or type(stopped['reason']) is not str or not re.fullmatch('[a-z0-9_]{1,64}', stopped['reason'])
                    or type(stopped['stopped_at']) not in (int, float)
                    or not 0 < stopped['stopped_at'] <= self._now()):
                raise BudgetUnavailable('q17_legacy_invalid')
            return {'q16_grant_sha256': hashlib.sha256(raw).hexdigest(),
                    'q16_stop_sha256': hashlib.sha256(stopped_raw).hexdigest()}
        except OSError:
            raise BudgetUnavailable('q17_legacy_invalid') from None

    def create_q17_grant(self, scope_hashes):
        """工程侧显式登记已批准的两个精确样本；不由产品/初始化调用，不重建许可。

        复用同一原账本锁，只创建作用域许可侧车，不更改原23条或三件账本结构。
        调用者必须已取得用户对实际预览的Q17批准，此方法不自动产生该批准。
        """
        if (type(scope_hashes) not in (tuple, list) or len(scope_hashes) != 2
                or not all(self._q16_hash(s) for s in scope_hashes) or len(set(scope_hashes)) != 2):
            raise BudgetUnavailable('q17_scope_invalid')
        with self._locked():
            ledger = self._load()
            if self.q17_path.exists() or self.q17_stop_path.exists():
                raise BudgetUnavailable('q17_already_recorded')
            rows = ledger['requests']
            if len(rows) != 23: raise BudgetUnavailable('q17_prefix_mismatch')
            if any(row['state'] != 'settled' for row in rows): raise BudgetUnavailable('usage_unresolved')
            if self._amount(rows) >= EXPERIMENT_ACCOUNTED_CEILING:
                raise BudgetUnavailable('experiment_budget_stop')
            legacy = self._q17_q16_binding(ledger)
            value = {'schema': 1, 'package': Q17_PACKAGE, 'approved': True,
                     'approved_on': '2026-10-18', 'source_kind': 'explicit_user_reply',
                     'ledger_id': ledger['ledger_id'], 'prefix_requests': 23,
                     'prefix_sha256': self._q17_prefix(rows), **legacy, 'scope_hashes': list(scope_hashes),
                     'intents': []}
            self._write(self.q17_path, value)
            return dict(value)

    def stop_q17_batch(self, reason):
        """工程执行失败后永久关闭本次许可；结算事实保留，剩余次数不能继续使用。"""
        if type(reason) is not str or not re.fullmatch('[a-z0-9_]{1,64}', reason):
            raise BudgetUnavailable('q17_stop_reason_invalid')
        with self._locked():
            ledger = self._load()
            if not self.q17_path.is_file(): raise BudgetUnavailable('q17_grant_missing')
            if not self.q17_stop_path.exists():
                self._write(self.q17_stop_path, {'package': Q17_PACKAGE,
                    'ledger_id': ledger['ledger_id'], 'physical_requests': len(ledger['requests']),
                    'reason': reason, 'stopped_at': self._now()})

    def _q17_grant(self, ledger, scope_hash):
        """必须持同一锁；任何未完成intent或原件/序号/账本关联不符均失败关闭。"""
        if self.q17_stop_path.exists(): raise BudgetUnavailable('q17_batch_stopped')
        if not self._q16_hash(scope_hash): raise BudgetUnavailable('q17_scope_invalid')
        value = self._read_json(self.q17_path)
        rows = ledger['requests']
        fields = {'schema','package','approved','approved_on','source_kind','ledger_id',
                  'prefix_requests','prefix_sha256','scope_hashes','intents',
                  'q16_grant_sha256','q16_stop_sha256'}
        if (type(value) is not dict or set(value) != fields
                or type(value['schema']) is not int or value['schema'] != 1
                or value['package'] != Q17_PACKAGE or value['approved'] is not True
                or value['approved_on'] != '2026-10-18' or value['source_kind'] != 'explicit_user_reply'
                or value['ledger_id'] != ledger['ledger_id']
                or type(value['prefix_requests']) is not int or value['prefix_requests'] != 23
                or len(rows) < 23 or value['prefix_sha256'] != self._q17_prefix(rows)
                or type(value['scope_hashes']) is not list or len(value['scope_hashes']) != 2
                or not all(self._q16_hash(item) for item in value['scope_hashes'])
                or len(set(value['scope_hashes'])) != 2 or type(value['intents']) is not list
                or len(value['intents']) > 2):
            raise BudgetUnavailable('q17_grant_invalid')
        if any(value[key] != digest for key, digest in self._q17_q16_binding(ledger).items()):
            raise BudgetUnavailable('q17_legacy_changed')
        for index, intent in enumerate(value['intents']):
            if (type(intent) is not dict or set(intent) != {'sequence','scope_hash','request_id','state'}
                    or type(intent['sequence']) is not int or intent['sequence'] != index + 1
                    or intent['scope_hash'] != value['scope_hashes'][index]
                    or not isinstance(intent['request_id'], str)
                    or re.fullmatch('[0-9a-f]{32}', intent['request_id']) is None
                    or intent['state'] != 'reserved' or len(rows) <= 23 + index
                    or rows[23 + index]['request_id'] != intent['request_id']):
                raise BudgetUnavailable('q17_intent_unresolved')
        if len(rows) != 23 + len(value['intents']): raise BudgetUnavailable('q17_prefix_mismatch')
        index = len(value['intents'])
        if index >= 2: raise BudgetUnavailable('q17_budget_stop')
        if value['scope_hashes'][index] != scope_hash: raise BudgetUnavailable('q17_scope_or_order_mismatch')
        return value

    def check_q17_scope(self, scope_hash):
        """精确样本只读预检；实际reserve仍在锁内重新校验，不预留/不消费。"""
        with self._locked():
            ledger = self._load()
            if self.q17_stop_path.exists(): raise BudgetUnavailable('q17_batch_stopped')
            if any(row['state'] != 'settled' for row in ledger['requests']):
                raise BudgetUnavailable('usage_unresolved')
            if self._amount(ledger['requests']) >= EXPERIMENT_ACCOUNTED_CEILING:
                raise BudgetUnavailable('experiment_budget_stop')
            self._q17_grant(ledger, scope_hash)
            return True

    @staticmethod
    def _q18_prefix(rows):
        return hashlib.sha256(json.dumps(rows[:24], ensure_ascii=True, allow_nan=False,
                                        sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()

    def _q18_legacy_binding(self, ledger):
        """只接前24笔及Q16/Q17各一次失败停止；四侧车只读逐字绑定。"""
        try:
            legacy = self._q17_q16_binding(ledger)
            raw = self.q17_path.read_bytes()
            stopped_raw = self.q17_stop_path.read_bytes()
            if len(raw) > 131072 or len(stopped_raw) > 4096:
                raise BudgetUnavailable('q18_legacy_invalid')
            value, stopped = self._strict_json(raw), self._strict_json(stopped_raw)
            rows = ledger['requests']
            fields = {'schema','package','approved','approved_on','source_kind','ledger_id',
                      'prefix_requests','prefix_sha256','scope_hashes','intents',
                      'q16_grant_sha256','q16_stop_sha256'}
            if (type(value) is not dict or set(value) != fields or type(value['schema']) is not int
                    or value['schema'] != 1 or value['package'] != Q17_PACKAGE or value['approved'] is not True
                    or value['approved_on'] != '2026-10-18' or value['source_kind'] != 'explicit_user_reply'
                    or value['ledger_id'] != ledger['ledger_id'] or type(value['prefix_requests']) is not int
                    or value['prefix_requests'] != 23 or len(rows) < 24
                    or value['prefix_sha256'] != self._q17_prefix(rows)
                    or any(value[key] != digest for key, digest in legacy.items())
                    or type(value['scope_hashes']) is not list or len(value['scope_hashes']) != 2
                    or not all(self._q16_hash(item) for item in value['scope_hashes'])
                    or len(set(value['scope_hashes'])) != 2 or type(value['intents']) is not list
                    or value['intents'] != [{'sequence': 1, 'scope_hash': value['scope_hashes'][0],
                         'request_id': rows[23]['request_id'], 'state': 'reserved'}]
                    or type(value['intents'][0]['sequence']) is not int):
                raise BudgetUnavailable('q18_legacy_invalid')
            if (type(stopped) is not dict or set(stopped) != {
                    'package','ledger_id','physical_requests','reason','stopped_at'}
                    or stopped['package'] != Q17_PACKAGE or stopped['ledger_id'] != ledger['ledger_id']
                    or type(stopped['physical_requests']) is not int or stopped['physical_requests'] != 24
                    or type(stopped['reason']) is not str or not re.fullmatch('[a-z0-9_]{1,64}', stopped['reason'])
                    or type(stopped['stopped_at']) not in (int, float)
                    or not 0 < stopped['stopped_at'] <= self._now()):
                raise BudgetUnavailable('q18_legacy_invalid')
            return {**legacy, 'q17_grant_sha256': hashlib.sha256(raw).hexdigest(),
                    'q17_stop_sha256': hashlib.sha256(stopped_raw).hexdigest()}
        except (OSError, BudgetUnavailable):
            raise BudgetUnavailable('q18_legacy_invalid') from None

    def create_q18_grant(self, scope_hashes):
        """工程侧登记用户明确批准的紧凑512两精确scope，不自动授权或预留。"""
        if (type(scope_hashes) not in (tuple, list) or len(scope_hashes) != 2
                or not all(self._q16_hash(s) for s in scope_hashes) or len(set(scope_hashes)) != 2):
            raise BudgetUnavailable('q18_scope_invalid')
        with self._locked():
            ledger = self._load()
            if self.q18_path.exists() or self.q18_stop_path.exists():
                raise BudgetUnavailable('q18_already_recorded')
            rows = ledger['requests']
            if len(rows) != 24: raise BudgetUnavailable('q18_prefix_mismatch')
            if any(row['state'] != 'settled' for row in rows): raise BudgetUnavailable('usage_unresolved')
            if self._amount(rows) >= EXPERIMENT_ACCOUNTED_CEILING:
                raise BudgetUnavailable('experiment_budget_stop')
            legacy = self._q18_legacy_binding(ledger)
            value = {'schema': 1, 'package': Q18_PACKAGE, 'approved': True,
                     'approved_on': '2026-10-18', 'source_kind': 'explicit_user_reply',
                     'ledger_id': ledger['ledger_id'], 'prefix_requests': 24,
                     'prefix_sha256': self._q18_prefix(rows), **legacy,
                     'scope_hashes': list(scope_hashes), 'request_ceiling': 2,
                     'reserved_per_request_cny': '0.032', 'reserved_ceiling_cny': '0.064', 'intents': []}
            self._write(self.q18_path, value)
            return dict(value)

    def stop_q18_batch(self, reason):
        if type(reason) is not str or not re.fullmatch('[a-z0-9_]{1,64}', reason):
            raise BudgetUnavailable('q18_stop_reason_invalid')
        with self._locked():
            ledger = self._load()
            if not self.q18_path.is_file(): raise BudgetUnavailable('q18_grant_missing')
            if not self.q18_stop_path.exists():
                self._write(self.q18_stop_path, {'package': Q18_PACKAGE,
                    'ledger_id': ledger['ledger_id'], 'physical_requests': len(ledger['requests']),
                    'reason': reason, 'stopped_at': self._now()})

    def _q18_grant(self, ledger, scope_hash):
        if self.q18_stop_path.exists(): raise BudgetUnavailable('q18_batch_stopped')
        if not self._q16_hash(scope_hash): raise BudgetUnavailable('q18_scope_invalid')
        value = self._read_json(self.q18_path)
        rows = ledger['requests']
        fields = {'schema','package','approved','approved_on','source_kind','ledger_id',
                  'prefix_requests','prefix_sha256','scope_hashes','intents','request_ceiling',
                  'reserved_per_request_cny','reserved_ceiling_cny',
                  'q16_grant_sha256','q16_stop_sha256','q17_grant_sha256','q17_stop_sha256'}
        if (type(value) is not dict or set(value) != fields
                or type(value['schema']) is not int or value['schema'] != 1
                or value['package'] != Q18_PACKAGE or value['approved'] is not True
                or value['approved_on'] != '2026-10-18' or value['source_kind'] != 'explicit_user_reply'
                or value['ledger_id'] != ledger['ledger_id']
                or type(value['prefix_requests']) is not int or value['prefix_requests'] != 24
                or len(rows) < 24 or value['prefix_sha256'] != self._q18_prefix(rows)
                or type(value['request_ceiling']) is not int or value['request_ceiling'] != 2
                or value['reserved_per_request_cny'] != '0.032' or value['reserved_ceiling_cny'] != '0.064'
                or type(value['scope_hashes']) is not list or len(value['scope_hashes']) != 2
                or not all(self._q16_hash(item) for item in value['scope_hashes'])
                or len(set(value['scope_hashes'])) != 2 or type(value['intents']) is not list
                or len(value['intents']) > 2):
            raise BudgetUnavailable('q18_grant_invalid')
        if any(value[key] != digest for key, digest in self._q18_legacy_binding(ledger).items()):
            raise BudgetUnavailable('q18_legacy_changed')
        for index, intent in enumerate(value['intents']):
            if (type(intent) is not dict or set(intent) != {'sequence','scope_hash','request_id','state'}
                    or type(intent['sequence']) is not int or intent['sequence'] != index + 1
                    or intent['scope_hash'] != value['scope_hashes'][index]
                    or type(intent['request_id']) is not str
                    or re.fullmatch('[0-9a-f]{32}', intent['request_id']) is None
                    or intent['state'] != 'reserved' or len(rows) <= 24 + index
                    or rows[24 + index]['request_id'] != intent['request_id']
                    or money(rows[24 + index]['reserved_cny']) != Decimal('0.032')
                    or money(rows[24 + index]['additional_fee_cny']) != 0):
                raise BudgetUnavailable('q18_intent_unresolved')
        if len(rows) != 24 + len(value['intents']): raise BudgetUnavailable('q18_prefix_mismatch')
        index = len(value['intents'])
        if index >= 2: raise BudgetUnavailable('q18_budget_stop')
        if value['scope_hashes'][index] != scope_hash: raise BudgetUnavailable('q18_scope_or_order_mismatch')
        return value

    def check_q18_scope(self, scope_hash):
        with self._locked():
            ledger = self._load()
            if self.q18_stop_path.exists(): raise BudgetUnavailable('q18_batch_stopped')
            if any(row['state'] != 'settled' for row in ledger['requests']):
                raise BudgetUnavailable('usage_unresolved')
            if self._amount(ledger['requests']) >= EXPERIMENT_ACCOUNTED_CEILING:
                raise BudgetUnavailable('experiment_budget_stop')
            self._q18_grant(ledger, scope_hash)
            return True

    def reserve(self, *, additional_fee_cny=Decimal(0), experiment=False, q16_permit=None, q17_permit=None, q18_permit=None, q19_permit=None, q20_permit=None, q21_permit=None, q22_permit=None, q23_permit=None, q24_permit=None, q25_permit=None, q26_permit=None, q27_permit=None, q28_permit=None, q29_permit=None, q30_permit=None, q31_permit=None, q32_permit=None, q33_permit=None, q34_permit=None, q35_permit=None, q36_permit=None):
        fee = money(additional_fee_cny)
        amount = BASE_RESERVATION + fee
        with self._locked():
            ledger = self._load()
            rows = ledger['requests']
            task_grant = None
            task_batch = 'Q36' if q36_permit is not None else 'Q35' if q35_permit is not None else 'Q34' if q34_permit is not None else 'Q33' if q33_permit is not None else 'Q32' if q32_permit is not None else 'Q31' if q31_permit is not None else 'Q30' if q30_permit is not None else 'Q29' if q29_permit is not None else 'Q28' if q28_permit is not None else 'Q27' if q27_permit is not None else 'Q26' if q26_permit is not None else 'Q25' if q25_permit is not None else 'Q24' if q24_permit is not None else 'Q23' if q23_permit is not None else 'Q22' if q22_permit is not None else 'Q21' if q21_permit is not None else 'Q20' if q20_permit is not None else 'Q19'
            task_permit = q36_permit if q36_permit is not None else q35_permit if q35_permit is not None else q34_permit if q34_permit is not None else q33_permit if q33_permit is not None else q32_permit if q32_permit is not None else q31_permit if q31_permit is not None else q30_permit if q30_permit is not None else q29_permit if q29_permit is not None else q28_permit if q28_permit is not None else q27_permit if q27_permit is not None else q26_permit if q26_permit is not None else q25_permit if q25_permit is not None else q24_permit if q24_permit is not None else q23_permit if q23_permit is not None else q22_permit if q22_permit is not None else q21_permit if q21_permit is not None else q20_permit if q20_permit is not None else q19_permit
            task_path = getattr(self, task_batch.lower() + '_path')
            if q36_permit is None and (self.q36_path.exists() or self.q36_stop_path.exists()):
                raise BudgetUnavailable('q36_scope_required')
            if q36_permit is None and q35_permit is None and (self.q35_path.exists() or self.q35_stop_path.exists()):
                raise BudgetUnavailable('q35_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and (self.q34_path.exists() or self.q34_stop_path.exists()):
                raise BudgetUnavailable('q34_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and (self.q33_path.exists() or self.q33_stop_path.exists()):
                raise BudgetUnavailable('q33_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and (self.q32_path.exists() or self.q32_stop_path.exists()):
                raise BudgetUnavailable('q32_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and (self.q31_path.exists() or self.q31_stop_path.exists()):
                for old_permit, name in ((q30_permit, 'q30'), (q29_permit, 'q29'), (q28_permit, 'q28'), (q27_permit, 'q27'), (q26_permit, 'q26'), (q25_permit, 'q25'),
                        (q24_permit, 'q24'), (q23_permit, 'q23'), (q22_permit, 'q22'),
                        (q21_permit, 'q21'), (q20_permit, 'q20'), (q19_permit, 'q19'),
                        (q18_permit, 'q18'), (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q31_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and (self.q30_path.exists() or self.q30_stop_path.exists()):
                for old_permit, name in ((q29_permit, 'q29'), (q28_permit, 'q28'), (q27_permit, 'q27'), (q26_permit, 'q26'), (q25_permit, 'q25'),
                        (q24_permit, 'q24'), (q23_permit, 'q23'), (q22_permit, 'q22'),
                        (q21_permit, 'q21'), (q20_permit, 'q20'), (q19_permit, 'q19'),
                        (q18_permit, 'q18'), (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q30_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and (self.q29_path.exists() or self.q29_stop_path.exists()):
                for old_permit, name in ((q28_permit, 'q28'), (q27_permit, 'q27'), (q26_permit, 'q26'), (q25_permit, 'q25'),
                        (q24_permit, 'q24'), (q23_permit, 'q23'), (q22_permit, 'q22'),
                        (q21_permit, 'q21'), (q20_permit, 'q20'), (q19_permit, 'q19'),
                        (q18_permit, 'q18'), (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q29_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and (self.q28_path.exists() or self.q28_stop_path.exists()):
                for old_permit, name in ((q27_permit, 'q27'), (q26_permit, 'q26'), (q25_permit, 'q25'),
                        (q24_permit, 'q24'), (q23_permit, 'q23'), (q22_permit, 'q22'),
                        (q21_permit, 'q21'), (q20_permit, 'q20'), (q19_permit, 'q19'),
                        (q18_permit, 'q18'), (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q28_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and q27_permit is None and (self.q27_path.exists() or self.q27_stop_path.exists()):
                for old_permit, name in ((q26_permit, 'q26'), (q25_permit, 'q25'),
                        (q24_permit, 'q24'), (q23_permit, 'q23'), (q22_permit, 'q22'),
                        (q21_permit, 'q21'), (q20_permit, 'q20'), (q19_permit, 'q19'),
                        (q18_permit, 'q18'), (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q27_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and q27_permit is None and q26_permit is None and (self.q26_path.exists() or self.q26_stop_path.exists()):
                for old_permit, name in ((q25_permit, 'q25'), (q24_permit, 'q24'), (q23_permit, 'q23'),
                        (q22_permit, 'q22'), (q21_permit, 'q21'), (q20_permit, 'q20'),
                        (q19_permit, 'q19'), (q18_permit, 'q18'), (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q26_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and q27_permit is None and q26_permit is None and q25_permit is None and (self.q25_path.exists() or self.q25_stop_path.exists()):
                for old_permit, name in ((q24_permit, 'q24'), (q23_permit, 'q23'),
                        (q22_permit, 'q22'), (q21_permit, 'q21'), (q20_permit, 'q20'),
                        (q19_permit, 'q19'), (q18_permit, 'q18'), (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q25_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and q27_permit is None and q26_permit is None and q25_permit is None and q24_permit is None and (self.q24_path.exists() or self.q24_stop_path.exists()):
                for old_permit, name in ((q23_permit, 'q23'), (q22_permit, 'q22'), (q21_permit, 'q21'),
                        (q20_permit, 'q20'), (q19_permit, 'q19'), (q18_permit, 'q18'),
                        (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q24_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and q27_permit is None and q26_permit is None and q25_permit is None and q24_permit is None and q23_permit is None and (self.q23_path.exists() or self.q23_stop_path.exists()):
                for old_permit, name in ((q22_permit, 'q22'), (q21_permit, 'q21'), (q20_permit, 'q20'),
                        (q19_permit, 'q19'), (q18_permit, 'q18'), (q17_permit, 'q17'), (q16_permit, 'q16')):
                    if old_permit is not None and getattr(self, name + '_stop_path').exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q23_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and q27_permit is None and q26_permit is None and q25_permit is None and q24_permit is None and q23_permit is None and q22_permit is None and (self.q22_path.exists() or self.q22_stop_path.exists()):
                for old_permit, old_stop, name in ((q21_permit, self.q21_stop_path, 'q21'),
                        (q20_permit, self.q20_stop_path, 'q20'), (q19_permit, self.q19_stop_path, 'q19'),
                        (q18_permit, self.q18_stop_path, 'q18'), (q17_permit, self.q17_stop_path, 'q17'),
                        (q16_permit, self.q16_stop_path, 'q16')):
                    if old_permit is not None and old_stop.exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q22_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and q27_permit is None and q26_permit is None and q25_permit is None and q24_permit is None and q23_permit is None and q22_permit is None and q21_permit is None and (self.q21_path.exists() or self.q21_stop_path.exists()):
                for old_permit, old_stop, name in ((q20_permit, self.q20_stop_path, 'q20'),
                        (q19_permit, self.q19_stop_path, 'q19'), (q18_permit, self.q18_stop_path, 'q18'),
                        (q17_permit, self.q17_stop_path, 'q17'), (q16_permit, self.q16_stop_path, 'q16')):
                    if old_permit is not None and old_stop.exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q21_scope_required')
            if q36_permit is None and q35_permit is None and q34_permit is None and q33_permit is None and q32_permit is None and q31_permit is None and q30_permit is None and q29_permit is None and q28_permit is None and q27_permit is None and q26_permit is None and q25_permit is None and q24_permit is None and q23_permit is None and q22_permit is None and q21_permit is None and q20_permit is None and (self.q20_path.exists() or self.q20_stop_path.exists()):
                for old_permit, old_stop, name in ((q19_permit, self.q19_stop_path, 'q19'),
                        (q18_permit, self.q18_stop_path, 'q18'), (q17_permit, self.q17_stop_path, 'q17'),
                        (q16_permit, self.q16_stop_path, 'q16')):
                    if old_permit is not None and old_stop.exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q20_scope_required')
            if task_permit is not None:
                from .task_budget import _check_locked, _stop_locked
                try:
                    if (any(permit is not None for permit in (q16_permit, q17_permit, q18_permit))
                            or sum(permit is not None for permit in (q19_permit, q20_permit, q21_permit, q22_permit, q23_permit, q24_permit, q25_permit, q26_permit, q27_permit, q28_permit, q29_permit, q30_permit, q31_permit, q32_permit, q33_permit, q34_permit, q35_permit, q36_permit)) > 1
                            or experiment is not True or amount != Decimal('0.032')):
                        raise BudgetUnavailable(task_batch.lower() + '_scope_invalid')
                    task_grant = _check_locked(self, ledger, task_permit, batch=task_batch)
                except BudgetUnavailable as error:
                    if task_path.exists() and error.reason != task_batch.lower() + '_batch_stopped':
                        _stop_locked(self, ledger, error.reason, batch=task_batch)
                    raise
            elif self.q19_path.exists() or self.q19_stop_path.exists():
                # 新批不能被普通/旧许可入口占用；旧已关闭批次仍给出原停止原因。
                for old_permit, old_stop, name in ((q18_permit, self.q18_stop_path, 'q18'),
                        (q17_permit, self.q17_stop_path, 'q17'), (q16_permit, self.q16_stop_path, 'q16')):
                    if old_permit is not None and old_stop.exists():
                        raise BudgetUnavailable(name + '_batch_stopped')
                raise BudgetUnavailable('q19_scope_required')
            if task_grant is not None:
                pass
            elif q18_permit is not None:
                if (q16_permit is not None or q17_permit is not None or type(q18_permit) is not _Q18Permit
                        or q18_permit.seal is not _Q18_SEAL or experiment is not True
                        or q18_permit.grant_path.resolve() != self.q18_path.resolve()):
                    raise BudgetUnavailable('q18_scope_invalid')
                if self.q18_stop_path.exists(): raise BudgetUnavailable('q18_batch_stopped')
                if amount != Decimal('0.032'): raise BudgetUnavailable('q18_reservation_invalid')
            elif self.q18_path.exists() or self.q18_stop_path.exists():
                if q17_permit is not None and self.q17_stop_path.exists():
                    raise BudgetUnavailable('q17_batch_stopped')
                if q16_permit is not None and self.q16_stop_path.exists():
                    raise BudgetUnavailable('q16_batch_stopped')
                raise BudgetUnavailable('q18_scope_required')
            if any(item['state'] != 'settled' for item in rows):
                raise BudgetUnavailable('usage_unresolved')
            q16_grant = None
            q17_grant = None
            q18_grant = None
            if task_grant is not None:
                pass
            elif q18_permit is not None:
                q18_grant = self._q18_grant(ledger, q18_permit.scope_hash)
            elif q17_permit is not None:
                if (q16_permit is not None or type(q17_permit) is not _Q17Permit
                        or q17_permit.seal is not _Q17_SEAL or experiment is not True
                        or q17_permit.grant_path.resolve() != self.q17_path.resolve()):
                    raise BudgetUnavailable('q17_scope_invalid')
                q17_grant = self._q17_grant(ledger, q17_permit.scope_hash)
            elif self.q17_path.exists() or self.q17_stop_path.exists():
                raise BudgetUnavailable('q17_scope_required')
            elif q16_permit is not None:
                if (type(q16_permit) is not _Q16Permit or q16_permit.seal is not _Q16_SEAL
                        or experiment is not True
                        or q16_permit.grant_path.resolve() != self.q16_path.resolve()):
                    raise BudgetUnavailable('q16_scope_invalid')
                q16_grant = self._q16_grant(ledger, q16_permit.scope_hash)
            elif self.q16_path.exists():
                # 已登记Q16后普通实验/旧严格入口都不能取得新增的第23/24次。
                raise BudgetUnavailable('q16_scope_required')
            if experiment and ((q16_grant is None and q17_grant is None and q18_grant is None and task_grant is None and len(rows) >= EXPERIMENT_REQUEST_CEILING)
                               or self._amount(rows) >= EXPERIMENT_ACCOUNTED_CEILING):
                raise BudgetUnavailable('experiment_budget_stop')
            first = [item for item in rows if item['phase'] == 'first']
            if len(rows) >= 100 or self._amount(rows) + amount > Decimal('5'):
                if task_grant is not None:
                    _stop_locked(self, ledger, 'cumulative_budget_stop', batch=task_batch)
                raise BudgetUnavailable('cumulative_budget_stop')
            # Q36只允许B/D两次达57；Q35保留59、Q34保留52。费用线不变。
            first_request_ceiling = 57 if task_grant is not None and task_batch == 'Q36' else 59 if task_grant is not None and task_batch == 'Q35' else 52 if task_grant is not None and task_batch == 'Q34' else 50
            if ledger['phase'] == 'first' and (len(first) >= first_request_ceiling or self._amount(first) + amount > Decimal('2')):
                if task_grant is not None:
                    _stop_locked(self, ledger, 'first_round_budget_stop', batch=task_batch)
                raise BudgetUnavailable('first_round_budget_stop')
            now = self._now()
            if len([item for item in rows if item['dispatched_at'] > now - 60]) >= 6:
                if task_grant is not None:
                    _stop_locked(self, ledger, 'model_rate_stop', batch=task_batch)
                raise BudgetUnavailable('model_rate_stop')
            identifier = uuid.uuid4().hex
            if task_grant is not None:
                from .task_budget import reserve_intent_locked
                task_grant = reserve_intent_locked(self, ledger, task_permit, identifier, now, batch=task_batch)
            if q16_grant is not None:
                # write-ahead intent先fsync，任一步崩溃均不自动修复或再次发送。
                q16_grant['intents'].append({'sequence': len(q16_grant['intents']) + 1,
                    'scope_hash': q16_permit.scope_hash, 'request_id': identifier, 'state': 'intent'})
                self._write(self.q16_path, q16_grant)
            if q17_grant is not None:
                q17_grant['intents'].append({'sequence': len(q17_grant['intents']) + 1,
                    'scope_hash': q17_permit.scope_hash, 'request_id': identifier, 'state': 'intent'})
                self._write(self.q17_path, q17_grant)
            if q18_grant is not None:
                q18_grant['intents'].append({'sequence': len(q18_grant['intents']) + 1,
                    'scope_hash': q18_permit.scope_hash, 'request_id': identifier, 'state': 'intent'})
                self._write(self.q18_path, q18_grant)
            rows.append({'request_id': identifier, 'phase': ledger['phase'], 'reserved_cny': str(amount),
                         'additional_fee_cny': str(fee), 'state': 'pending', 'accounted_cny': None,
                         'prompt_tokens': None, 'completion_tokens': None, 'provider_request_id': None,
                         'cache_hit_tokens': None, 'cache_miss_tokens': None,
                         'dispatched_at': now, 'reason': None})
            self._write(self.path, ledger)
            if task_grant is not None:
                task_grant['intents'][-1]['state'] = 'reserved'
                self._write(task_path, task_grant)
            if q16_grant is not None:
                q16_grant['intents'][-1]['state'] = 'reserved'
                self._write(self.q16_path, q16_grant)
            if q17_grant is not None:
                q17_grant['intents'][-1]['state'] = 'reserved'
                self._write(self.q17_path, q17_grant)
            if q18_grant is not None:
                q18_grant['intents'][-1]['state'] = 'reserved'
                self._write(self.q18_path, q18_grant)
            return Reservation(identifier, amount)

    def settle(self, reservation, *, prompt_tokens, completion_tokens, provider_request_id,
               cache_hit_tokens=0, cache_miss_tokens=None):
        cost = usage_cost(prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens)
        if (type(provider_request_id) is not str or not 1 <= len(provider_request_id) <= 128
                or any(ord(char) < 32 for char in provider_request_id)):
            raise BudgetUnavailable('usage_unassociated')
        with self._locked():
            ledger = self._load()
            item = next((row for row in ledger['requests'] if row['request_id'] == reservation.request_id), None)
            if item is None or item['state'] not in ('pending', 'unresolved') or money(item['reserved_cny']) != reservation.amount_cny:
                raise BudgetUnavailable('reservation_mismatch')
            if any(row['provider_request_id'] == provider_request_id for row in ledger['requests']
                   if row['request_id'] != reservation.request_id):
                raise BudgetUnavailable('usage_unassociated')
            accounted = cost + money(item['additional_fee_cny'])
            if accounted > reservation.amount_cny:
                raise BudgetUnavailable('usage_out_of_bounds')
            item.update(state='settled', accounted_cny=str(accounted), prompt_tokens=prompt_tokens,
                        cache_hit_tokens=cache_hit_tokens,
                        cache_miss_tokens=prompt_tokens - cache_hit_tokens if cache_miss_tokens is None else cache_miss_tokens,
                        completion_tokens=completion_tokens, provider_request_id=provider_request_id, reason=None)
            self._write(self.path, ledger)
            return accounted

    def mark_unresolved(self, reservation, reason):
        if type(reason) is not str or not 1 <= len(reason) <= 64:
            raise BudgetUnavailable('invalid_failure_category')
        with self._locked():
            ledger = self._load()
            item = next((row for row in ledger['requests'] if row['request_id'] == reservation.request_id), None)
            if item is None or money(item['reserved_cny']) != reservation.amount_cny:
                raise BudgetUnavailable('reservation_mismatch')
            if item['state'] == 'settled':
                return
            item.update(state='unresolved', reason=reason)
            self._write(self.path, ledger)

    def finish_first_round(self):
        with self._locked():
            ledger = self._load()
            if any(row['state'] != 'settled' for row in ledger['requests']):
                raise BudgetUnavailable('usage_unresolved')
            ledger['phase'] = 'cumulative'
            self._write(self.path, ledger)

    def summary(self):
        with self._locked():
            ledger = self._load()
            rows = ledger['requests']
            return {'package': PACKAGE, 'phase': ledger['phase'], 'physical_requests': len(rows),
                    'accounted_cny': str(sum((money(row['accounted_cny']) for row in rows
                                           if row['state'] == 'settled'), Decimal(0))),
                    'unresolved_cny': str(sum((money(row['reserved_cny']) for row in rows
                                              if row['state'] != 'settled'), Decimal(0))),
                    'unresolved_requests': sum(row['state'] != 'settled' for row in rows)}

    def receipt_snapshot(self):
        """宿主工程取证；严格复用账本检查，不创建三件、不改预留或记录。

        技术关联只交给可信宿主，不能成为模型输入或产品工具。
        """
        with self._locked(existing_only=True):
            ledger = self._load()
            return [dict(row) for row in ledger['requests']]
