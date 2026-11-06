"""Q19/Q20/Q21/Q22 最小任务许可；沿原 BudgetLedger 的锁、账本和会计停止线。

本模块只供可信宿主使用。初始化函数不构成批准；必须先取得用户对冻结
审查包的明确答复。模型、产品客户端均不能选择任务、轮次或确认正式交付。
"""
from dataclasses import dataclass, asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from .budget import BudgetUnavailable, EXPERIMENT_ACCOUNTED_CEILING


Q19_PACKAGE = 'Q19-P29-continuous-task'
Q20_PACKAGE = 'Q20-P30-single-step-task'
Q21_PACKAGE = 'Q21-P30-validity-recovery'
Q22_PACKAGE = 'Q22-P31-structured-task'
Q23_PACKAGE = 'Q23-P32-current-action'
Q24_PACKAGE = 'Q24-P33-query-state'
Q25_PACKAGE = 'Q25-P34-step-result'
Q26_PACKAGE = 'Q26-P35-explanation-following'
Q27_PACKAGE = 'Q27-P36-current-result'
Q28_PACKAGE = 'Q28-P37-return-relation'
Q29_PACKAGE = 'Q29-P38-clause-following'
Q30_PACKAGE = 'Q30-P39-lock-amount'
Q31_PACKAGE = 'Q31-P40-navigation-following'
Q32_PACKAGE = 'Q32-P42-v1-personal'
Q33_PACKAGE = 'Q33-P42-v1-fresh-confirmation'
Q34_PACKAGE = 'Q34-P42-v1-completion'
Q35_PACKAGE = 'Q35-P42-v1-wait-progress'
Q36_PACKAGE = 'Q36-P42-v1-navigation-unknown'
_BATCHES = {'Q19': (Q19_PACKAGE, 26), 'Q20': (Q20_PACKAGE, 27), 'Q21': (Q21_PACKAGE, 28),
            'Q22': (Q22_PACKAGE, 29), 'Q23': (Q23_PACKAGE, 30), 'Q24': (Q24_PACKAGE, 32),
            'Q25': (Q25_PACKAGE, 33), 'Q26': (Q26_PACKAGE, 34), 'Q27': (Q27_PACKAGE, 35), 'Q28': (Q28_PACKAGE, 36), 'Q29': (Q29_PACKAGE, 37), 'Q30': (Q30_PACKAGE, 38), 'Q31': (Q31_PACKAGE, 39), 'Q32': (Q32_PACKAGE, 40), 'Q33': (Q33_PACKAGE, 43), 'Q34': (Q34_PACKAGE, 45), 'Q35': (Q35_PACKAGE, 50), 'Q36': (Q36_PACKAGE, 55)}
Q19_REQUEST_CEILING = 20
Q19_RESERVED_CEILING = Decimal('0.640')
Q19_BATCH_SECONDS = 1200
Q19_TASK_SECONDS = 300
_SEAL = object()
_HASH = re.compile(r'[0-9a-f]{64}')
_REF = re.compile(r'[A-Za-z0-9._-]{1,100}')


def request_ceiling(batch):
    return 2 if batch == "Q36" else 9 if batch == "Q35" else 7 if batch in {"Q33","Q34"} else 10 if batch == "Q32" else 11 if batch == "Q31" else 12 if batch == "Q30" else 13 if batch == "Q29" else 14 if batch == "Q28" else 15 if batch == "Q27" else 16 if batch == "Q26" else 17 if batch == "Q25" else Q19_REQUEST_CEILING


def reserved_ceiling(batch):
    return "0.064" if batch == "Q36" else "0.288" if batch == "Q35" else "0.224" if batch in {"Q33","Q34"} else "0.320" if batch == "Q32" else "0.352" if batch == "Q31" else "0.384" if batch == "Q30" else "0.416" if batch == "Q29" else "0.448" if batch == "Q28" else "0.480" if batch == "Q27" else "0.512" if batch == "Q26" else "0.544" if batch == "Q25" else "0.640"


def _profile(ledger, batch):
    if type(batch) is not str or batch not in _BATCHES:
        raise BudgetUnavailable('task_batch_invalid')
    package, prefix = _BATCHES[batch]
    return package, prefix, getattr(ledger, batch.lower() + '_path'), getattr(ledger, batch.lower() + '_stop_path')


def _error(batch, suffix):
    return BudgetUnavailable(batch.lower() + '_' + suffix)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


@dataclass(frozen=True)
class TaskPermitBinding:
    task_ref: str
    task_sequence: int
    round_index: int
    physical_index: int
    source_hash: str
    actual_hash: str
    scope_hash: str


@dataclass(frozen=True)
class _Q19Permit:
    binding: TaskPermitBinding
    grant_path: Path
    seal: object
    batch: str = 'Q19'


def validated_q19_permit(binding, grant_path, *, batch='Q19'):
    """仅动态 scope 已同时验证原来源和实际输入后调用，不自行验证字段权限。"""
    if type(batch) is not str or batch not in _BATCHES:
        raise BudgetUnavailable('task_batch_invalid')
    _validate_binding(binding, batch=batch)
    return _Q19Permit(binding, Path(grant_path), _SEAL, batch)


def _validate_binding(binding, *, batch='Q19'):
    if (type(binding) is not TaskPermitBinding or not isinstance(binding.task_ref, str)
            or _REF.fullmatch(binding.task_ref) is None
            or any(type(value) is not int or value < 1 for value in
                   (binding.task_sequence, binding.round_index, binding.physical_index))
            or binding.task_sequence > (2 if batch == 'Q36' else 3 if batch in {'Q32','Q33','Q34','Q35'} else 4) or binding.round_index > 9
            or binding.physical_index > request_ceiling(batch)
            or any(type(value) is not str or _HASH.fullmatch(value) is None
                   for value in (binding.source_hash, binding.actual_hash, binding.scope_hash))):
        raise _error(batch, 'scope_invalid')


def _legacy(ledger, value, *, batch='Q19'):
    """绑定全部旧侧车原字节，并复用既有 Q16/Q17 结构校验。"""
    if batch == 'Q20':
        return _q20_legacy(ledger, value)
    if batch == 'Q21':
        return _closed_task_legacy(ledger, value, previous_batch='Q20', prefix=28, batch=batch)
    if batch == 'Q22':
        return _closed_task_legacy(ledger, value, previous_batch='Q21', prefix=29, batch=batch)
    if batch == 'Q23':
        return _closed_task_legacy(ledger, value, previous_batch='Q22', prefix=30, batch=batch)
    if batch == 'Q36':
        return _closed_task_legacy(ledger, value, previous_batch='Q35', prefix=55, batch=batch)
    if batch == 'Q35':
        return _closed_task_legacy(ledger, value, previous_batch='Q34', prefix=50, batch=batch)
    if batch == 'Q34':
        return _closed_task_legacy(ledger, value, previous_batch='Q33', prefix=45, batch=batch)
    if batch == 'Q33':
        return _closed_task_legacy(ledger, value, previous_batch='Q32', prefix=43, batch=batch)
    if batch == 'Q32':
        return _closed_task_legacy(ledger, value, previous_batch='Q31', prefix=40, batch=batch)
    if batch == 'Q31':
        return _closed_task_legacy(ledger, value, previous_batch='Q30', prefix=39, batch=batch)
    if batch == 'Q30':
        return _closed_task_legacy(ledger, value, previous_batch='Q29', prefix=38, batch=batch)
    if batch == 'Q29':
        return _closed_task_legacy(ledger, value, previous_batch='Q28', prefix=37, batch=batch)
    if batch == 'Q28':
        return _closed_task_legacy(ledger, value, previous_batch='Q27', prefix=36, batch=batch)
    if batch == 'Q27':
        return _closed_task_legacy(ledger, value, previous_batch='Q26', prefix=35, batch=batch)
    if batch == 'Q26':
        return _closed_task_legacy(ledger, value, previous_batch='Q25', prefix=34, batch=batch)
    if batch == 'Q25':
        return _closed_task_legacy(ledger, value, previous_batch='Q24', prefix=33, batch=batch)
    if batch == 'Q24':
        return _closed_task_legacy(ledger, value, previous_batch='Q23', prefix=32, batch=batch)
    inherited = ledger._q18_legacy_binding(value)
    result = {}
    for name in ('q16_path', 'q16_stop_path', 'q17_path', 'q17_stop_path',
                 'q18_path', 'q18_stop_path'):
        path = getattr(ledger, name)
        try:
            raw = path.read_bytes()
        except OSError:
            raise _error(batch, 'legacy_invalid') from None
        result[name] = hashlib.sha256(raw).hexdigest()
    old = ledger._read_json(ledger.q18_path)
    stopped = ledger._read_json(ledger.q18_stop_path)
    rows = value['requests']
    old_fields = {'schema', 'package', 'approved', 'approved_on', 'source_kind', 'ledger_id',
        'prefix_requests', 'prefix_sha256', 'scope_hashes', 'intents', 'request_ceiling',
        'reserved_per_request_cny', 'reserved_ceiling_cny', *inherited}
    if (len(rows) < 26 or type(old) is not dict or set(old) != old_fields
            or type(stopped) is not dict
            or set(stopped) != {'package', 'ledger_id', 'physical_requests', 'reason', 'stopped_at'}
            or type(old['schema']) is not int or old['schema'] != 1
            or old['package'] != 'Q18-P26-compact-two' or old['approved'] is not True
            or old['approved_on'] != '2026-10-18' or old['source_kind'] != 'explicit_user_reply'
            or type(old['prefix_requests']) is not int or old['prefix_requests'] != 24
            or any(old[key] != digest for key, digest in inherited.items())
            or type(old['scope_hashes']) is not list or len(old['scope_hashes']) != 2
            or any(type(scope) is not str or _HASH.fullmatch(scope) is None for scope in old['scope_hashes'])
            or len(set(old['scope_hashes'])) != 2 or type(old['intents']) is not list
            or old['reserved_per_request_cny'] != '0.032' or old['reserved_ceiling_cny'] != '0.064'
            or old.get('ledger_id') != value['ledger_id']
            or old.get('prefix_sha256') != ledger._q18_prefix(rows)
            or old.get('request_ceiling') != 2 or len(old.get('intents', [])) != 2
            or stopped.get('ledger_id') != value['ledger_id']
            or stopped.get('package') != 'Q18-P26-compact-two'
            or stopped.get('reason') != 'completed'
            or stopped.get('physical_requests') != 26):
        raise _error(batch, 'legacy_invalid')
    for index, intent in enumerate(old['intents']):
        if intent != {'sequence': index + 1, 'state': 'reserved',
                      'scope_hash': old['scope_hashes'][index],
                      'request_id': rows[24 + index]['request_id']}:
            raise _error(batch, 'legacy_invalid')
    return result


def _q20_legacy(ledger, value):
    return _closed_task_legacy(ledger, value, previous_batch='Q19', prefix=27, batch='Q20')


def _closed_task_legacy(ledger, value, *, previous_batch, prefix, batch):
    """只绑定已停止前批的固定前缀和全部侧车；不重开旧许可。"""
    try:
        if len(value['requests']) < prefix:
            raise ValueError()
        historical = dict(value, requests=value['requests'][:prefix])
        previous = _load_locked(ledger, historical, batch=previous_batch, check_stopped=False)
        result = dict(previous['legacy_sha256'])
        package, _, grant_path, stop_path = _profile(ledger, previous_batch)
        stopped = ledger._read_json(stop_path)
        if (type(stopped) is not dict
                or set(stopped) != {'package', 'ledger_id', 'physical_requests', 'reason', 'stopped_at'}
                or stopped['package'] != package or stopped['ledger_id'] != value['ledger_id']
                or type(stopped['physical_requests']) is not int or stopped['physical_requests'] != prefix
                or type(stopped['reason']) is not str or re.fullmatch(r'[a-z0-9_]{1,64}', stopped['reason']) is None
                or type(stopped['stopped_at']) not in (int, float)
                or not 0 < stopped['stopped_at'] <= ledger._now()
                or any(row['state'] != 'settled' for row in historical['requests'])):
            raise ValueError()
        for name, path in ((previous_batch.lower() + '_path', grant_path),
                           (previous_batch.lower() + '_stop_path', stop_path)):
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result
    except (OSError, ValueError, BudgetUnavailable):
        raise _error(batch, 'legacy_invalid') from None


def _specs(task_specs, *, batch='Q19'):
    expected = [('DEV27-05', 'B1', 9), ('DEV27-05', 'B0', 9),
                ('DEV27-08', 'B0', 1), ('DEV27-08', 'B1', 1)]
    if batch == 'Q32':
        expected = [('V1-AC', 'personal', 8), ('V1-B', 'personal', 1), ('V1-D', 'personal', 1)]
    if batch in {'Q33','Q34'}:
        expected = [('V1-AC', 'personal', 5), ('V1-B', 'personal', 1), ('V1-D', 'personal', 1)]
    if batch == 'Q35':
        expected = [('V1-AC', 'personal', 7), ('V1-B', 'personal', 1), ('V1-D', 'personal', 1)]
    if batch == 'Q36':
        expected = [('V1-B', 'personal', 1), ('V1-D', 'personal', 1)]
    fields = {'task_ref', 'case_id', 'group', 'max_rounds', 'scope_hash'}
    if type(task_specs) is not list or len(task_specs) != len(expected):
        raise _error(batch, 'task_specs_invalid')
    refs = set()
    for spec, (case, group, rounds) in zip(task_specs, expected):
        if (type(spec) is not dict or set(spec) != fields or spec['case_id'] != case
                or spec['group'] != group or type(spec['max_rounds']) is not int
                or spec['max_rounds'] != rounds or type(spec['task_ref']) is not str
                or _REF.fullmatch(spec['task_ref']) is None or spec['task_ref'] in refs
                or type(spec['scope_hash']) is not str or _HASH.fullmatch(spec['scope_hash']) is None):
            raise _error(batch, 'task_specs_invalid')
        refs.add(spec['task_ref'])
    return json.loads(json.dumps(task_specs))


def create_q19_grant(ledger, task_specs, decision_sha256, *, batch='Q19'):
    """获明确新批准后由工程入口显式调用；不更改旧许可、不预留模型费用。"""
    package, prefix, grant_path, stop_path = _profile(ledger, batch)
    specs = _specs(task_specs, batch=batch)
    if type(decision_sha256) is not str or _HASH.fullmatch(decision_sha256) is None:
        raise _error(batch, 'decision_invalid')
    with ledger._locked():
        value = ledger._load()
        if grant_path.exists() or stop_path.exists():
            raise _error(batch, 'already_recorded')
        if len(value['requests']) != prefix:
            raise _error(batch, 'prefix_mismatch')
        if any(row['state'] != 'settled' for row in value['requests']):
            raise BudgetUnavailable('usage_unresolved')
        if ledger._amount(value['requests']) >= EXPERIMENT_ACCOUNTED_CEILING:
            raise BudgetUnavailable('experiment_budget_stop')
        grant = {'schema': 1, 'package': package, 'approved': True,
            'source_kind': 'explicit_user_reply', 'decision_sha256': decision_sha256,
            'ledger_id': value['ledger_id'], 'prefix_requests': prefix,
            'prefix_sha256': _digest(value['requests']), 'legacy_sha256': _legacy(ledger, value, batch=batch),
            'task_specs': specs, 'task_specs_sha256': _digest(specs),
            'request_ceiling': request_ceiling(batch), 'reserved_per_request_cny': '0.032',
            'reserved_ceiling_cny': reserved_ceiling(batch), 'batch_deadline_seconds': 1200,
            'task_deadline_seconds': 300, 'batch_started_at': None,
            'tasks': [{'started_at': None, 'confirmed_rounds': 0, 'finished': False}
                      for _ in specs], 'intents': []}
        ledger._write(grant_path, grant)
        return json.loads(json.dumps(grant))


def _stop_locked(ledger, value, reason, *, batch='Q19'):
    package, prefix, grant_path, stop_path = _profile(ledger, batch)
    if type(reason) is not str or re.fullmatch(r'[a-z0-9_]{1,64}', reason) is None:
        raise _error(batch, 'stop_reason_invalid')
    if not stop_path.exists():
        ledger._write(stop_path, {'package': package,
            'ledger_id': value['ledger_id'], 'physical_requests': len(value['requests']),
            'reason': reason, 'stopped_at': ledger._now()})


def stop_q19_batch(ledger, reason, *, batch='Q19'):
    package, prefix, grant_path, stop_path = _profile(ledger, batch)
    with ledger._locked():
        value = ledger._load()
        if not grant_path.is_file():
            raise _error(batch, 'grant_missing')
        _stop_locked(ledger, value, reason, batch=batch)


def _load_locked(ledger, value, *, batch='Q19', check_stopped=True):
    package, prefix, grant_path, stop_path = _profile(ledger, batch)
    if check_stopped and stop_path.exists():
        raise _error(batch, 'batch_stopped')
    grant = ledger._read_json(grant_path)
    fields = {'schema', 'package', 'approved', 'source_kind', 'decision_sha256',
        'ledger_id', 'prefix_requests', 'prefix_sha256', 'legacy_sha256', 'task_specs',
        'task_specs_sha256', 'request_ceiling', 'reserved_per_request_cny',
        'reserved_ceiling_cny', 'batch_deadline_seconds', 'task_deadline_seconds',
        'batch_started_at', 'tasks', 'intents'}
    if (type(grant) is not dict or set(grant) != fields or type(grant['schema']) is not int
            or grant['schema'] != 1 or grant['package'] != package
            or grant['approved'] is not True or grant['source_kind'] != 'explicit_user_reply'
            or not isinstance(grant['decision_sha256'], str)
            or _HASH.fullmatch(grant['decision_sha256']) is None
            or grant['ledger_id'] != value['ledger_id'] or type(grant['prefix_requests']) is not int
            or grant['prefix_requests'] != prefix or len(value['requests']) < prefix
            or grant['prefix_sha256'] != _digest(value['requests'][:prefix])
            or grant['legacy_sha256'] != _legacy(ledger, value, batch=batch)
            or type(grant['request_ceiling']) is not int or grant['request_ceiling'] != request_ceiling(batch)
            or grant['reserved_per_request_cny'] != '0.032'
            or grant['reserved_ceiling_cny'] != reserved_ceiling(batch)
            or type(grant['batch_deadline_seconds']) is not int or grant['batch_deadline_seconds'] != 1200
            or type(grant['task_deadline_seconds']) is not int or grant['task_deadline_seconds'] != 300
            or type(grant['tasks']) is not list or len(grant['tasks']) != (2 if batch == 'Q36' else 3 if batch in {'Q32','Q33','Q34','Q35'} else 4)
            or type(grant['intents']) is not list or len(grant['intents']) > request_ceiling(batch)):
        raise _error(batch, 'grant_invalid')
    specs = _specs(grant['task_specs'], batch=batch)
    if grant['task_specs_sha256'] != _digest(specs):
        raise _error(batch, 'grant_invalid')
    now = ledger._now()
    def timestamp(stamp):
        return stamp is None or (type(stamp) in (int, float) and 0 < stamp <= now)
    if not timestamp(grant['batch_started_at']):
        raise _error(batch, 'grant_invalid')
    seen_unfinished = False
    for spec, state in zip(specs, grant['tasks']):
        if (type(state) is not dict or set(state) != {'started_at', 'confirmed_rounds', 'finished'}
                or not timestamp(state['started_at']) or type(state['confirmed_rounds']) is not int
                or not 0 <= state['confirmed_rounds'] <= spec['max_rounds']
                or type(state['finished']) is not bool
                or (state['finished'] and (seen_unfinished or state['confirmed_rounds'] < 1))
                or (state['started_at'] is None and state['confirmed_rounds'] != 0)):
            raise _error(batch, 'grant_invalid')
        seen_unfinished |= not state['finished']
    counters = {}
    for index, intent in enumerate(grant['intents']):
        fields = {'sequence', 'binding', 'request_id', 'state'}
        if (type(intent) is not dict or set(intent) != fields or type(intent['sequence']) is not int
                or intent['sequence'] != index + 1 or type(intent['binding']) is not dict):
            raise _error(batch, 'intent_unresolved')
        try:
            binding = TaskPermitBinding(**intent['binding'])
        except TypeError:
            raise _error(batch, 'intent_unresolved') from None
        _validate_binding(binding, batch=batch)
        spec = specs[binding.task_sequence - 1]
        key = (binding.task_sequence, binding.round_index)
        counters[key] = counters.get(key, 0) + 1
        if (spec['task_ref'] != binding.task_ref or spec['scope_hash'] != binding.scope_hash
                or binding.round_index > spec['max_rounds'] or binding.physical_index != counters[key]
                or intent['state'] != 'reserved' or len(value['requests']) <= prefix + index
                or value['requests'][prefix + index]['request_id'] != intent['request_id']
                or value['requests'][prefix + index]['reserved_cny'] != '0.032'
                or value['requests'][prefix + index]['additional_fee_cny'] != '0'):
            raise _error(batch, 'intent_unresolved')
    if len(value['requests']) != prefix + len(grant['intents']):
        raise _error(batch, 'prefix_mismatch')
    if bool(grant['intents']) != (grant['batch_started_at'] is not None):
        raise _error(batch, 'grant_invalid')
    return grant


def _check_locked(ledger, value, permit, *, batch='Q19'):
    package, prefix, grant_path, stop_path = _profile(ledger, batch)
    if (type(permit) is not _Q19Permit or permit.seal is not _SEAL
            or permit.batch != batch
            or permit.grant_path.resolve() != grant_path.resolve()):
        raise _error(batch, 'scope_invalid')
    _validate_binding(permit.binding, batch=batch)
    grant = _load_locked(ledger, value, batch=batch)
    binding = permit.binding
    if any(row['state'] != 'settled' for row in value['requests']):
        raise BudgetUnavailable('usage_unresolved')
    if ledger._amount(value['requests']) >= EXPERIMENT_ACCOUNTED_CEILING:
        raise BudgetUnavailable('experiment_budget_stop')
    if len(grant['intents']) >= request_ceiling(batch):
        raise _error(batch, 'request_limit')
    unfinished = next((i for i, state in enumerate(grant['tasks']) if not state['finished']), len(grant['tasks']))
    if unfinished == len(grant['tasks']) or binding.task_sequence != unfinished + 1:
        raise _error(batch, 'task_order_mismatch')
    spec, state = grant['task_specs'][unfinished], grant['tasks'][unfinished]
    if binding.task_ref != spec['task_ref'] or binding.scope_hash != spec['scope_hash']:
        raise _error(batch, 'scope_invalid')
    if binding.round_index != state['confirmed_rounds'] + 1:
        raise _error(batch, 'delivery_unconfirmed')
    if binding.round_index > spec['max_rounds']:
        raise _error(batch, 'round_limit')
    same_round = [intent for intent in grant['intents'] if
        intent['binding']['task_sequence'] == binding.task_sequence
        and intent['binding']['round_index'] == binding.round_index]
    if batch in {'Q32','Q33','Q34','Q35','Q36'} and same_round:
        raise _error(batch, 'retry_forbidden')
    if binding.physical_index != len(same_round) + 1:
        raise _error(batch, 'replay_or_order_mismatch')
    if any(intent['binding']['source_hash'] == binding.source_hash
           and intent['binding']['actual_hash'] == binding.actual_hash for intent in same_round):
        raise _error(batch, 'replay_or_order_mismatch')
    now = ledger._now()
    if grant['batch_started_at'] is not None and now - grant['batch_started_at'] >= 1200:
        raise _error(batch, 'batch_deadline')
    if state['started_at'] is not None and now - state['started_at'] >= 300:
        raise _error(batch, 'task_deadline')
    return grant


def check_q19_permit(ledger, permit, *, batch='Q19'):
    """零模型预检，不消费；reserve 仍在同一原锁内重新校验。"""
    with ledger._locked():
        value = ledger._load()
        _check_locked(ledger, value, permit, batch=batch)
        return True


def reserve_intent_locked(ledger, value, permit, identifier, now, *, batch='Q19'):
    """仅由 BudgetLedger.reserve 持锁调用；先 intent，再原账本，再 reserved。"""
    package, prefix, grant_path, stop_path = _profile(ledger, batch)
    try:
        grant = _check_locked(ledger, value, permit, batch=batch)
    except BudgetUnavailable as error:
        if grant_path.exists() and error.reason != batch.lower() + '_batch_stopped':
            _stop_locked(ledger, value, error.reason, batch=batch)
        raise
    if grant['batch_started_at'] is None:
        grant['batch_started_at'] = now
    state = grant['tasks'][permit.binding.task_sequence - 1]
    if state['started_at'] is None:
        state['started_at'] = now
    grant['intents'].append({'sequence': len(grant['intents']) + 1,
        'binding': asdict(permit.binding), 'request_id': identifier, 'state': 'intent'})
    ledger._write(grant_path, grant)
    return grant


def confirm_q19_round(ledger, task_ref, round_index, *, batch='Q19'):
    """宿主取得并保存正式 HTTP 交付后确认；失联或遗漏此步不能续下一轮。"""
    package, prefix, grant_path, stop_path = _profile(ledger, batch)
    with ledger._locked():
        value = ledger._load()
        grant = _load_locked(ledger, value, batch=batch)
        index = next((i for i, spec in enumerate(grant['task_specs'])
                      if spec['task_ref'] == task_ref), -1)
        if index < 0 or type(round_index) is not int:
            raise _error(batch, 'scope_invalid')
        state = grant['tasks'][index]
        now = ledger._now()
        if grant['batch_started_at'] is not None and now - grant['batch_started_at'] >= 1200:
            _stop_locked(ledger, value, batch.lower() + '_batch_deadline', batch=batch)
            raise _error(batch, 'batch_deadline')
        if state['started_at'] is not None and now - state['started_at'] >= 300:
            _stop_locked(ledger, value, batch.lower() + '_task_deadline', batch=batch)
            raise _error(batch, 'task_deadline')
        intents = [intent for intent in grant['intents'] if
            intent['binding']['task_sequence'] == index + 1
            and intent['binding']['round_index'] == round_index]
        if (state['finished'] or round_index != state['confirmed_rounds'] + 1 or not intents
                or any(row['state'] != 'settled' for row in value['requests'])):
            raise _error(batch, 'delivery_unconfirmed')
        state['confirmed_rounds'] = round_index
        ledger._write(grant_path, grant)


def finish_q19_task(ledger, task_ref, *, batch='Q19'):
    """正常等待/澄清/未知不自动失败；可信宿主根据任务证据明确结束。"""
    package, prefix, grant_path, stop_path = _profile(ledger, batch)
    with ledger._locked():
        value = ledger._load()
        grant = _load_locked(ledger, value, batch=batch)
        index = next((i for i, state in enumerate(grant['tasks']) if not state['finished']), len(grant['tasks']))
        if (index == len(grant['tasks']) or grant['task_specs'][index]['task_ref'] != task_ref
                or grant['tasks'][index]['confirmed_rounds'] < 1
                or any(row['state'] != 'settled' for row in value['requests'])
                or any(intent['binding']['task_sequence'] == index + 1
                       and intent['binding']['round_index'] > grant['tasks'][index]['confirmed_rounds']
                       for intent in grant['intents'])):
            raise _error(batch, 'delivery_unconfirmed')
        grant['tasks'][index]['finished'] = True
        ledger._write(grant_path, grant)
        if index == len(grant['tasks']) - 1:
            _stop_locked(ledger, value, 'completed', batch=batch)
