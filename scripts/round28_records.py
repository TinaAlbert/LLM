"""P28宿主任务记录：纯数据入口，不发模型请求、不操作业务或费用账本。

冻结答案和SQL只在host_only；本模块的对象不能当作Assistant输入。
offline_scripted仅证明运输/工程链路，不能进入AI任务分母。
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _text(value):
    return type(value) is str and bool(value.strip())


def _mapping(value, code):
    require(type(value) is dict and bool(value), code)
    # 不把未知对象偷偷stringify；宿主必须明确选择实际保存的字段。
    json.dumps(value, allow_nan=False)
    return deepcopy(value)


def _checked(record):
    require(type(record) is dict and record.get('kind') == 'assistant_task_record',
            'p28_task_record_required')
    require(digest(record['freeze']) == record['freeze_sha256'], 'p28_frozen_task_changed')
    return record


def freeze_task(*, group, task_ref, case_id, task_class, mode, host_only, events=()):
    """宿主固定组别/初态/版本/规程；不提供客户端请求参数或送模投影。"""
    require(group in {'B0', 'B1', 'guide'}, 'p28_unknown_group')
    require(_text(task_ref) and _text(case_id), 'p28_task_identity_required')
    require(task_class in {'completable', 'unknown', 'infeasible'}, 'p28_task_class')
    require(mode in {'offline_scripted', 'real_model'}, 'p28_task_mode')
    require(type(events) in {list, tuple}, 'p28_event_list')
    frozen_events, identifiers = [], set()
    for event in events:
        require(type(event) is dict and set(event) ==
                {'event_id', 'after_accepted_action', 'action'}, 'p28_event_fields')
        require(_text(event['event_id']) and event['event_id'] not in identifiers,
                'p28_event_identity')
        require(type(event['after_accepted_action']) is int and
                event['after_accepted_action'] > 0 and _text(event['action']), 'p28_event_trigger')
        identifiers.add(event['event_id'])
        frozen_events.append(deepcopy(event))
    frozen = {'group': group, 'task_ref': task_ref, 'case_id': case_id,
        'task_class': task_class, 'mode': mode,
        'host_only': _mapping(host_only, 'p28_host_freeze_required'), 'events': frozen_events}
    return {'schema': 1, 'kind': 'assistant_task_record', 'freeze': frozen,
        'freeze_sha256': digest(frozen), 'rounds': [], 'transitions': [], 'events': [],
        'terminals': [], 'accepted_action_sequence': 0, 'pending_events': []}


def record_model_attempt(record, *, round_ref, started, suggestion=None, usage=None):
    """started=None保留无法确认的启动；False用于prepare/前置停止/脚本替身。"""
    _checked(record)
    require(_text(round_ref) and all(row['round_ref'] != round_ref for row in record['rounds']),
            'p28_round_identity')
    require(started is None or type(started) is bool, 'p28_started_tristate')
    real = record['freeze']['mode'] == 'real_model'
    require(real or (started is False and usage is None), 'p28_offline_cannot_start_model')
    require(suggestion is None or _text(suggestion) or
            (type(suggestion) is dict and bool(suggestion)), 'p28_suggestion_type')
    require(not real or suggestion is None or started is True,
            'p28_real_suggestion_requires_started_model')
    normalized_usage = None
    if usage is not None:
        require(started is True and type(usage) is dict and
                set(usage) == {'reservation_ref', 'state', 'accounted_cny'}, 'p28_usage_fields')
        require(_text(usage['reservation_ref']) and
                usage['state'] in {'settled', 'pending', 'unresolved'}, 'p28_usage_receipt')
        amount = usage['accounted_cny']
        if usage['state'] == 'settled':
            require(type(amount) is str, 'p28_usage_amount')
            try:
                parsed = Decimal(amount)
            except InvalidOperation as error:
                raise ValueError('p28_usage_amount') from error
            require(parsed.is_finite() and parsed >= 0, 'p28_usage_amount')
        else:
            require(amount is None, 'p28_unresolved_cost_unknown')
        normalized_usage = deepcopy(usage)
    row = {'round_ref': round_ref, 'model_started': started,
        'source_kind': 'real_model' if real else 'offline_scripted',
        'real_model_suggestion': deepcopy(suggestion) if real else None,
        'scripted_suggestion': deepcopy(suggestion) if not real else None,
        'usage': normalized_usage}
    json.dumps(row, allow_nan=False)
    record['rounds'].append(row)
    return deepcopy(row)


def record_transition(record, *, action_ref, action, actor, http_status, accepted,
                      duplicate=False, suggestion_ref=None, engineering_added_step=False,
                      reason=None, evidence=None):
    """只记录已发生原API/网页动作；返回待宿主推进的冻结事件ID，不执行事件。"""
    _checked(record)
    require(_text(action_ref) and all(row['action_ref'] != action_ref
            for row in record['transitions']), 'p28_action_identity')
    require(_text(action) and _text(actor), 'p28_action_actor')
    require(type(http_status) is int and 100 <= http_status <= 599, 'p28_http_status')
    require(type(accepted) is bool and type(duplicate) is bool and
            type(engineering_added_step) is bool, 'p28_transition_flags')
    require(not accepted or 200 <= http_status < 300, 'p28_rejected_http_cannot_be_accepted')
    require(not engineering_added_step or _text(reason), 'p28_engineering_reason_required')
    require(reason is None or _text(reason), 'p28_reason_type')
    model_round = None
    if suggestion_ref is not None:
        matches = [row for row in record['rounds'] if row['round_ref'] == suggestion_ref]
        require(len(matches) == 1, 'p28_suggestion_reference')
        model_round = matches[0]
        require(model_round['real_model_suggestion'] is not None or
                model_round['scripted_suggestion'] is not None, 'p28_suggestion_missing')
    evidence = _mapping(evidence, 'p28_original_action_evidence_required')
    counts = accepted and not duplicate
    require(not counts or not record['pending_events'], 'p28_event_must_precede_next_action')
    sequence = record['accepted_action_sequence'] + int(counts)
    due = [event['event_id'] for event in record['freeze']['events']
        if counts and event['after_accepted_action'] == sequence and event['action'] == action]
    attributed = (model_round is not None and model_round['source_kind'] == 'real_model' and
                  model_round['model_started'] is True and not engineering_added_step)
    row = {'action_ref': action_ref, 'action': action, 'actor': actor,
        'actual_original_api_or_web': True, 'http_status': http_status,
        'accepted': accepted, 'duplicate': duplicate,
        'accepted_action_sequence': sequence if counts else None,
        'suggestion_ref': suggestion_ref, 'real_model_attributed': attributed,
        'engineering_added_step': engineering_added_step, 'reason': reason,
        'original_action_evidence': evidence, 'triggered_event_ids': due}
    record['transitions'].append(row)
    record['accepted_action_sequence'] = sequence
    record['pending_events'].extend(due)
    return list(due)


def record_event(record, event_id, *, evidence):
    """只能记录刚刚实际接受动作触发的事件；拒绝、重放和人工猜测不能触发。"""
    _checked(record)
    require(event_id in record['pending_events'] and all(row['event_id'] != event_id
            for row in record['events']), 'p28_event_not_due')
    event = next(row for row in record['freeze']['events'] if row['event_id'] == event_id)
    require(event['after_accepted_action'] == record['accepted_action_sequence'],
            'p28_event_sequence_changed')
    row = {'event_id': event_id, 'after_accepted_action': record['accepted_action_sequence'],
        'evidence': _mapping(evidence, 'p28_event_evidence_required')}
    record['events'].append(row)
    record['pending_events'].remove(event_id)
    return deepcopy(row)


def record_terminal(record, *, passed, evidence, method='independent_read_only_sql'):
    """独立检查结果与建议/业务动作分列；不得用模型自述作为终态依据。"""
    _checked(record)
    require(passed is None or type(passed) is bool, 'p28_terminal_tristate')
    require(method in {'independent_read_only_sql', 'independent_semantic_review'},
            'p28_independent_terminal_method')
    row = {'check_ref': len(record['terminals']) + 1, 'passed': passed, 'method': method,
        'after_accepted_action': record['accepted_action_sequence'],
        'after_event_count': len(record['events']),
        'evidence': _mapping(evidence, 'p28_independent_evidence_required')}
    record['terminals'].append(row)
    return deepcopy(row)


def _merge_rows(target, rows, key, code):
    for row in rows:
        identity = row[key]
        if identity in target:
            require(target[identity] == row, code)
        else:
            target[identity] = deepcopy(row)


def summarize(records):
    """同group/task_ref多轮/快照去重；离线、未知启动和任务类别保持分列。"""
    require(type(records) in {list, tuple} and bool(records), 'p28_summary_records_required')
    tasks = {}
    for record in records:
        _checked(record)
        frozen = record['freeze']
        key = (frozen['group'], frozen['task_ref'])
        task = tasks.setdefault(key, {'freeze': deepcopy(frozen),
            'freeze_sha256': record['freeze_sha256'], 'rounds': {}, 'transitions': {},
            'events': {}, 'terminals': {}})
        require(task['freeze_sha256'] == record['freeze_sha256'], 'p28_summary_freeze_conflict')
        for field, identity in (('rounds', 'round_ref'), ('transitions', 'action_ref'),
                                 ('events', 'event_id'), ('terminals', 'check_ref')):
            _merge_rows(task[field], record[field], identity, 'p28_summary_evidence_conflict')
    result, receipts = [], {}
    usage_unknown = False
    for task in tasks.values():
        frozen = task['freeze']
        rounds, actions = list(task['rounds'].values()), list(task['transitions'].values())
        real_rounds = [row for row in rounds if row['source_kind'] == 'real_model']
        start_states = [row['model_started'] for row in real_rounds]
        started = True if True in start_states else None if None in start_states else False
        for row in real_rounds:
            if row['model_started'] is None or (row['model_started'] is True and row['usage'] is None):
                usage_unknown = True
            if row['usage'] is not None:
                receipt = row['usage']
                ref = receipt['reservation_ref']
                require(ref not in receipts or receipts[ref] == receipt, 'p28_summary_usage_conflict')
                receipts[ref] = receipt
        terminals = list(task['terminals'].values())
        terminal = max(terminals, key=lambda row: row['check_ref']) if terminals else None
        passed = terminal['passed'] if terminal else None
        accepted = [row for row in actions if row['accepted'] and not row['duplicate']]
        engineering = [row for row in actions if row['engineering_added_step']]
        fired = set(task['events'])
        due = {event for row in actions for event in row['triggered_event_ids']}
        # 终态本身不能证明模型忠实：实际动作均须关联真实建议且没有工程补步。
        all_events = {event['event_id'] for event in frozen['events']}
        terminal_current = bool(terminal and terminal['after_accepted_action'] ==
            max((row['accepted_action_sequence'] for row in accepted), default=0) and
            terminal['after_event_count'] == len(fired))
        faithful = bool(started is True and passed is True and not engineering and
            any(row['real_model_suggestion'] is not None for row in real_rounds) and
            (accepted or frozen['task_class'] != 'completable') and terminal_current and
            all(row['real_model_attributed'] for row in accepted) and all_events <= fired)
        result.append({'group': frozen['group'], 'task_ref': frozen['task_ref'],
            'case_id': frozen['case_id'], 'task_class': frozen['task_class'],
            'mode': frozen['mode'], 'freeze_sha256': task['freeze_sha256'],
            'ai_task_started': started, 'real_model_suggestions':
                [row for row in real_rounds if row['real_model_suggestion'] is not None],
            'scripted_suggestions': [row for row in rounds if row['scripted_suggestion'] is not None],
            'actual_actions': actions, 'independent_terminals': terminals,
            'independent_terminal_passed': passed, 'engineering_added_steps': engineering,
            'model_faithful_complete': faithful, 'pending_event_ids': sorted(due - fired)})
    unknown_start = [row for row in result if row['ai_task_started'] is None]
    started = [row for row in result if row['ai_task_started'] is True]
    by_class = {}
    for category in ('completable', 'unknown', 'infeasible'):
        known = [row for row in started if row['task_class'] == category]
        uncertain = [row for row in unknown_start if row['task_class'] == category]
        by_class[category] = {'confirmed_started': len(known),
            'unknown_start': len(uncertain), 'denominator': None if uncertain else len(known),
            'independent_terminal_passed': sum(row['independent_terminal_passed'] is True for row in known),
            'model_faithful_complete': sum(row['model_faithful_complete'] for row in known)}
    unresolved = [row for row in receipts.values() if row['state'] != 'settled']
    settled = sum((Decimal(row['accounted_cny']) for row in receipts.values()
                   if row['state'] == 'settled'), Decimal(0))
    return {'schema': 1, 'kind': 'assistant_task_summary', 'tasks': result,
        'confirmed_started_ai_tasks': len(started), 'unknown_start_tasks': len(unknown_start),
        'ai_task_denominator': None if unknown_start else len(started),
        'task_classes': by_class, 'offline_scripted_tasks':
            sum(row['mode'] == 'offline_scripted' for row in result),
        'physical_reservations_from_records': None if usage_unknown else len(receipts),
        'usage_association_unknown': usage_unknown, 'unresolved_receipts': len(unresolved),
        'known_settled_accounted_cny': str(settled),
        'accounted_cny': None if usage_unknown or unresolved else str(settled),
        'actual_bill_cny': None, 'completion_rate': None,
        'limits': ['offline_scripted_is_not_ai_task_success',
            'unknown_start_and_usage_are_not_zero',
            'supplier_bill_not_reconciled', 'task_classes_must_not_be_combined']}
