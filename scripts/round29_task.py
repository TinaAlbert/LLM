"""P29–31人工忠实确认接点。prepare/rehearse均零模型；run只消费对应新批批准。

人工将正式答中明确的一条建议抄入 confirmation-NN.json。进程保留会话并等待
该文件，暂停计入300秒总截止；进程失联关闭批次，不重放请求或重建旧会话。
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from scripts import round20_sandbox as sandbox
from scripts.d3_evaluate import NoModelTraffic, business_snapshot
from scripts.round21_explore import configuration, ordinary_session, require, write
from scripts.round27_replay import HTTPJournal, terminal_sql, sha, ledger_fingerprint
from scripts.round28_run import GOALS, event_worker, read, save
from scripts.round28_records import (digest, freeze_task, record_model_attempt,
    record_transition, record_event, record_terminal, summarize)
from services.business.app import create_app
from services.business.manage import reset_test_database
from services.guide.fair_runtime import FairTaskBinding, fair_input
from services.guide.knowledge import fingerprint
from services.guide.model import ModelResult, TokenUsage, ModelUnavailable
from services.guide.releases import Releases
from services.guide.task_runtime import install_task_runtime
from services.guide.action_contract import ACTION_CONTRACT_VERSION

MATRIX = (('DEV27-05', 'B1', 9), ('DEV27-05', 'B0', 9),
          ('DEV27-08', 'B0', 1), ('DEV27-08', 'B1', 1))
EVENT_RULE = {'version': 'per-object-accepted-pay-query-v1',
    'payment': 'first accepted query for the same object after its original accepted payment',
    'ticket': 'second accepted query for the same object after its original accepted payment',
    'rejected_or_duplicate': 'does not advance counters or trigger events',
    'ordering': 'event executes after query receipt and before next action; no SQL action selection'}
OBSERVATION = {'page': 'booking', 'title': '预订与票据',
    'items': ['报价', '锁价', '付款', '票据'], 'product_version': 'v1'}
ACTION_WORDS = {'lock': r'锁价|锁定', 'pay': r'付款|支付', 'cancel': r'取消|释放',
    'query': r'查询|查看|核对|刷新|确认', 'requote': r'重新报价|重报价|获取报价',
    'navigate': r'打开|导航|切换|前往', 'wait': r'等待|稍后|等一',
    'clarify': r'说明|告诉|提供|确认|请问|是否|未知|资料不足|未覆盖',
    'end': r'完成|结束|未知|资料不足|未覆盖|无法确认'}

# 30只替换新批身份/独立文件和候选；运输、动作确认及事件规程复用。
ROUND = 29
PACKAGE = 'Q19-P29-continuous-task'
LOCAL = ROOT / '.local/d3-29'
ENTRY = Path(__file__).resolve()


def configure_round30():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 30, 'Q20-P30-single-step-task'
    LOCAL = ROOT / '.local/d3-30'
    ENTRY = ROOT / 'scripts/round30_task.py'


def configure_round30_recovery():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 30, 'Q21-P30-validity-recovery'
    LOCAL = ROOT / '.local/d3-30-recovery'
    ENTRY = ROOT / 'scripts/round30_recovery.py'



def configure_round31():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 31, 'Q22-P31-structured-task'
    LOCAL = ROOT / '.local/d3-31'
    ENTRY = ROOT / 'scripts/round31_task.py'


def configure_round32():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 32, 'Q23-P32-current-action'
    LOCAL = ROOT / '.local/d3-32'
    ENTRY = ROOT / 'scripts/round32_task.py'


def configure_round33():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 33, 'Q24-P33-query-state'
    LOCAL = ROOT / '.local/d3-33'
    ENTRY = ROOT / 'scripts/round33_task.py'


def configure_round34():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 34, 'Q25-P34-step-result'
    LOCAL = ROOT / '.local/d3-34'
    ENTRY = ROOT / 'scripts/round34_task.py'


def configure_round35():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 35, 'Q26-P35-explanation-following'
    LOCAL = ROOT / '.local/d3-35'
    ENTRY = ROOT / 'scripts/round35_task.py'


def configure_round36():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 36, 'Q27-P36-current-result'
    LOCAL = ROOT / '.local/d3-36'
    ENTRY = ROOT / 'scripts/round36_task.py'


def configure_round37():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 37, 'Q28-P37-return-relation'
    LOCAL = ROOT / '.local/d3-37'
    ENTRY = ROOT / 'scripts/round37_task.py'


def configure_round38():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 38, 'Q29-P38-clause-following'
    LOCAL = ROOT / '.local/d3-38'
    ENTRY = ROOT / 'scripts/round38_task.py'


def configure_round39():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 39, 'Q30-P39-lock-amount'
    LOCAL = ROOT / '.local/d3-39'
    ENTRY = ROOT / 'scripts/round39_task.py'


def configure_round40():
    global ROUND, PACKAGE, LOCAL, ENTRY
    ROUND, PACKAGE = 40, 'Q31-P40-navigation-following'
    LOCAL = ROOT / '.local/d3-40'
    ENTRY = ROOT / 'scripts/round40_task.py'


def batch_name():
    return PACKAGE.split('-', 1)[0]


def shared_prompt_version():
    return 'assistant-12' if batch_name() == 'Q31' else 'assistant-11' if batch_name() in {'Q29', 'Q30'} else 'assistant-10' if batch_name() in {'Q26', 'Q27', 'Q28'} else 'assistant-9' if batch_name() in {'Q22', 'Q23', 'Q24', 'Q25'} else 'assistant-8' if batch_name() == 'Q21' else 'assistant-7'


def grant_paths(ledger):
    prefix = batch_name().lower()
    return getattr(ledger, prefix + '_path'), getattr(ledger, prefix + '_stop_path')


def binding_from(frozen):
    return FairTaskBinding(case_id=frozen['case_id'], task_ref=frozen['task_ref'],
        group=frozen['group'], actor_id='personal_alice',
        delegated_trip_ids=tuple(frozen['trip_ids']), release_version=frozen['release_version'],
        goal=GOALS[frozen['case_id']], max_rounds=frozen['max_rounds'], task_deadline_seconds=300)


def body_for(frozen, message, *, session_id=None, page='booking'):
    from services.guide.task_scope import TASK_OBSERVATIONS
    body = {'message': message, 'page': page, 'trip_id': frozen['trip_ids'][0],
        'delegated_trip_ids': frozen['trip_ids'], 'observation': {
            'page': page, **TASK_OBSERVATIONS[page], 'product_version': 'v1'}}
    if session_id:
        body['session_id'] = session_id
    return body


def validate_confirmation(confirmation, formal, *, task_ref, turn, consumed=()):
    """有限显式引用核对。人工负责语义忠实；条件/否定/多对象歧义不能执行。

    不推测模型未提出的下一步，不按业务状态/SQL选动作。每份确认只能有一个动作。
    """
    if ROUND >= 31 or 'action_contract' in formal:
        return validate_structured_confirmation(confirmation, formal, task_ref=task_ref, turn=turn, consumed=consumed)
    required = {
        'task_ref', 'turn', 'formal_reply_sha256', 'quote', 'action', 'object',
        'actor', 'faithful', 'ambiguous', 'note'}
    require(type(confirmation) is dict and set(confirmation) in (required, required | {'navigation_receipt'}),
        'p29_confirmation_fields')
    require(confirmation['task_ref'] == task_ref and type(confirmation['turn']) is int
        and confirmation['turn'] == turn and confirmation['formal_reply_sha256'] == digest(formal),
        'p29_confirmation_delivery_mismatch')
    require(digest(confirmation) not in consumed, 'p29_confirmation_replay')
    require(confirmation['actor'] == 'engineering_synthetic_user'
        and confirmation['faithful'] is True and confirmation['ambiguous'] is False,
        'p29_human_faithful_confirmation_required')
    quote, action, ref = (confirmation[key] for key in ('quote', 'action', 'object'))
    require(type(quote) is str and len(quote.strip()) >= 2 and quote in formal['reply']
        and action in ACTION_WORDS and type(ref) is str and type(confirmation['note']) is str,
        'p29_confirmation_explicit_quote_required')
    require(re.search(ACTION_WORDS[action], quote) is not None, 'p29_action_not_in_quote')
    if action in {'lock', 'pay', 'cancel', 'query', 'requote'}:
        refs = set(re.findall(r'(?<![A-Za-z0-9_])trip_[12](?![A-Za-z0-9_])', quote))
        require(ref in {'trip_1', 'trip_2'} and refs == {ref}, 'p29_object_ambiguous')
        require(not re.search(r'如果|假如|若|是否|可能|不要|不能|不可|切勿|无需|不必|不建议|尚未确认', quote),
            'p29_conditional_or_negative_action')
        require(re.search(r'请|建议|先|再|继续|接着|可以', quote) is not None,
            'p29_action_not_explicit_instruction')
        if action == 'pay':
            require(not re.search(r'已(?:经)?(?:完成|成功)?(?:付款|支付)', quote), 'p29_past_fact_not_instruction')
    elif action == 'navigate':
        require(formal.get('tool', {}).get('name') == 'navigate'
            and formal['tool']['arguments'] == {'page': ref}, 'p29_navigation_tool_mismatch')
        require(type(confirmation.get('navigation_receipt')) is dict,
            'p29_actual_navigation_receipt_required')
    else:
        require(ref == 'task', 'p29_nonbusiness_object')
    return dict(confirmation)



def structured_action(formal):
    """只读取宿主正式交付的唯一动作结构，不从说明或业务状态选动作。"""
    from services.guide.action_contract import action_digest, render_action_contract, validate_action_contract
    from services.guide.task_scope import TASK_OBSERVATIONS
    contract = formal.get('action_contract')
    require(formal.get('action_contract_version') == ACTION_CONTRACT_VERSION
        and type(contract) is dict and formal.get('action_sha256') == action_digest(contract),
        'p31_action_delivery_binding_required')
    tool = formal.get('tool')
    calls = [] if tool is None else [{key: tool.get(key) for key in ('id', 'name', 'arguments')}]
    try:
        checked = validate_action_contract(json.dumps(contract, ensure_ascii=False),
            {'delegated_trips': [{'ref': 'trip_1'}, {'ref': 'trip_2'}]}, list(TASK_OBSERVATIONS), calls)
        rendered = render_action_contract(checked)
    except (ValueError, TypeError, KeyError):
        raise RuntimeError('p31_action_delivery_invalid') from None
    require(formal.get('reply') == rendered, 'p31_action_presentation_mismatch')
    action = checked['action']
    # 导航的唯一对象是结构参数page；保留原执行器的人工object接口。
    ref = action['params']['page'] if action['kind'] == 'navigate' else action['target']
    return action['kind'], ref, rendered.splitlines()[0]


def validate_structured_confirmation(confirmation, formal, *, task_ref, turn, consumed=()):
    required = {'task_ref', 'turn', 'formal_reply_sha256', 'action_sha256',
        'action_contract_version', 'quote', 'action', 'object', 'actor', 'faithful', 'ambiguous', 'note'}
    require(type(confirmation) is dict and set(confirmation) in (required, required | {'navigation_receipt'}),
        'p31_confirmation_fields')
    require(confirmation['task_ref'] == task_ref and type(confirmation['turn']) is int
        and confirmation['turn'] == turn and confirmation['formal_reply_sha256'] == digest(formal)
        and confirmation['action_contract_version'] == formal.get('action_contract_version')
        and confirmation['action_sha256'] == formal.get('action_sha256'),
        'p31_confirmation_delivery_mismatch')
    require(digest(confirmation) not in consumed, 'p29_confirmation_replay')
    require(confirmation['actor'] == 'engineering_synthetic_user'
        and confirmation['faithful'] is True and confirmation['ambiguous'] is False,
        'p29_human_faithful_confirmation_required')
    kind, ref, first_line = structured_action(formal)
    require(confirmation['action'] == kind and confirmation['object'] == ref
        and confirmation['quote'] == first_line and type(confirmation['note']) is str,
        'p31_confirmation_unique_action_required')
    if kind == 'navigate':
        require(type(confirmation.get('navigation_receipt')) is dict, 'p29_actual_navigation_receipt_required')
    else:
        require('navigation_receipt' not in confirmation, 'p31_confirmation_fields')
    return dict(confirmation)


def terminal_verdict(sql, trip_ids):
    """独立两票/关联/守恒判据；不把固定140000或券归trip_1当任务完成条件。"""
    try:
        trips = {str(row['id']): row for row in sql['trips']}
        require(set(trips) == set(trip_ids) and all(row['owner_id'] == 'personal_alice'
            for row in trips.values()), 'scope')
        require(all(len(sql[key]) == 2 for key in
            ('payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets')), 'unique')
        payments = {str(row['id']): row for row in sql['payments']}
        jobs = {str(row['payment_id']): row for row in sql['payment_jobs']}
        providers = {str(row['operation_id']): row for row in sql['provider_payments']}
        tasks = {str(row['id']): row for row in sql['ticket_tasks']}
        locks = {str(row['id']): row for row in sql['locks']}
        require(len(payments) == len(jobs) == len(providers) == len(tasks) == 2
            and set(jobs) == set(providers) == set(payments), 'associations')
        require({str(t['trip_id']) for t in sql['tickets']} == set(trip_ids)
            and len({str(t['id']) for t in sql['tickets']}) == 2, 'two_tickets')
        for ticket in sql['tickets']:
            trip, pay, task = trips[str(ticket['trip_id'])], payments[str(ticket['payment_id'])], tasks[str(ticket['task_id'])]
            lock = locks[str(pay['lock_id'])]
            require(str(pay['trip_id']) == str(task['trip_id']) == str(ticket['trip_id'])
                and str(task['payment_id']) == str(pay['id']) and ticket['trip_revision'] == trip['revision']
                and ticket['quantity'] == 1 and ticket['certificate_present'] is True and trip['status'] == 'paid'
                and pay['status'] == task['status'] == 'succeeded' and jobs[str(pay['id'])]['status'] == 'done'
                and providers[str(pay['id'])]['outcome'] == 'succeeded'
                and providers[str(pay['id'])]['captured_minor'] == pay['amount_minor']
                and str(lock['trip_id']) == str(trip['id']) and lock['trip_revision'] == trip['revision']
                and lock['status'] == 'consumed' and lock['amount_minor'] == pay['amount_minor'], 'ticket_payment')
        coupon = sql['coupons']
        require(len(coupon) == 1 and coupon[0]['status'] == 'consumed', 'coupon')
        owners = [row for row in sql['locks'] if row['coupon_id'] == coupon[0]['id'] and row['status'] == 'consumed']
        require(len(owners) == 1 and owners[0]['id'] == coupon[0]['lock_id']
            and str(owners[0]['trip_id']) in trip_ids and
            [row['count'] for row in sql['coupon_events'] if row['action'] == 'coupon.consumed'] == [1], 'coupon_once')
        require(len(sql['flights']) == 2 and {row['id'] for row in sql['flights']} ==
            {row['flight_id'] for row in trips.values()} and all(row['capacity'] == 2
            and row['reserved'] == 0 and row['sold'] == 1 for row in sql['flights']), 'inventory')
        return {'passed': True, 'tickets': 2, 'captured_minor': sum(row['captured_minor'] for row in providers.values()),
            'coupon_consumed': 1, 'inventory_conserved': True, 'optimality': None,
            'optimality_reason': 'no_same_parameter_legal_reference_branches'}
    except (RuntimeError, KeyError, TypeError) as error:
        return {'passed': False, 'reason': str(error), 'optimality': None}


def prepare(directory):
    """同一个合成初态冻结四项共同契约及两种精确初始输入；不创建许可。"""
    from fastapi.testclient import TestClient
    require(not directory.exists(), 'p29_output_exists_no_replay')
    if ROUND >= 30:
        candidate_path = LOCAL / 'candidate-releases.json'
        if candidate_path.exists():
            provenance = read(LOCAL / 'candidate-provenance.json')
            require(provenance['source_sha256'] == sha(ROOT / '.local/d3-26/releases.json')
                and provenance['candidate_file_sha256'] == sha(candidate_path), 'p30_candidate_changed')
        else:
            candidate_path.write_bytes((ROOT / '.local/d3-26/releases.json').read_bytes())
            candidate_store = Releases('test', candidate_path)
            previous = candidate_store.active()
            candidate = candidate_store.save_candidate(previous['knowledge'], previous['observations'],
                prompt_version=shared_prompt_version())
            candidate_store.publish(candidate['version'])
            save(LOCAL / 'candidate-provenance.json', {'source': '.local/d3-26/releases.json',
                'source_sha256': sha(ROOT / '.local/d3-26/releases.json'),
                'candidate_file_sha256': sha(candidate_path),
                'previous': previous['version'], 'candidate': candidate['version'],
                'prompt_version': shared_prompt_version(), 'knowledge_preserved': True,
                'local_synthetic_only': True, 'product_active_modified': False,
                'model_calls': 0})
    directory.mkdir(parents=True)
    config, settings = configuration()
    guard = NoModelTraffic()
    ledger_before = ledger_fingerprint()
    with sandbox.runner_lock(), guard.installed():
        reset_test_database(config['test_database_url'], confirmation='llm_business_test', fixture='two-trips-one-coupon')
        app = create_app(settings)
        assistant = app.state.assistant
        release = read((LOCAL / 'candidate-releases.json') if ROUND >= 30 else ROOT / '.local/d3-26/releases.json')
        save(directory / 'releases.json', release)
        assistant.releases = Releases('test', directory / 'releases.json')
        assistant.external_model = False
        captures = []
        async def capture(payload, kind, version):
            captures.append(payload)
            raise ModelUnavailable('p29_prepare_only')
        assistant.infer = capture
        with TestClient(app) as client:
            with ordinary_session(client, settings) as headers:
                write(client, headers, '/payment-profile', {'source_ref': 'demo_personal_alice', 'expected_revision': 0}, 'PUT')
                ids = [write(client, headers, '/trips', {'flight_id': 'DEMO-'+size,
                    'travel_date': '2026-10-01', 'passengers': 1})['id'] for size in ('LARGE', 'SMALL')]
                quotes = [write(client, headers, f'/trips/{trip}/quotes', {'expected_revision': 1}) for trip in ids]
                tasks = []
                for sequence, (case, group, rounds) in enumerate(MATRIX, 1):
                    frozen = {'task_sequence': sequence, 'task_ref': f'p{ROUND}{"r" if batch_name() == "Q21" else ""}-{case.lower()}-{group.lower()}',
                        'case_id': case, 'group': group, 'max_rounds': rounds, 'trip_ids': ids,
                        'release_version': release['active'], 'goal': GOALS[case], 'quotes': quotes,
                        'condition': 'published-flow', 'model': release['candidates'][release['active']]['model'],
                        'input_bytes': 12288, 'output_tokens': 512, 'task_deadline_seconds': 300,
                        'physical_batch_limit': 11 if ROUND == 40 else 12 if ROUND == 39 else 13 if ROUND == 38 else 14 if ROUND == 37 else 15 if ROUND == 36 else 16 if ROUND == 35 else 17 if ROUND == 34 else 20, 'events': [],
                        'event_rule': EVENT_RULE if case == 'DEV27-05' else None}
                    if ROUND >= 30:
                        frozen['prompt_version'] = shared_prompt_version()
                    before = len(captures)
                    response = client.post('/assistant/chat', headers=headers, json=body_for(frozen, GOALS[case]))
                    require(response.status_code == 503 and len(captures) == before + 1, 'p29_prepare_capture_failed')
                    source = captures[-1]
                    actual = fair_input(source, group, **({'prompt_version': shared_prompt_version()} if ROUND >= 30 else {}))
                    save(directory / f'initial-{sequence}-source.json', source)
                    save(directory / f'initial-{sequence}-actual.json', actual)
                    frozen.update(source_sha256=fingerprint(source), actual_sha256=fingerprint(actual),
                        actual_bytes=len(json.dumps(actual, ensure_ascii=False, separators=(',', ':')).encode('utf-8')))
                    require(frozen['actual_bytes'] <= 12288, 'p29_initial_input_too_large')
                    tasks.append(frozen)
        save(directory / 'review-tasks.json', tasks)
        save(directory / 'initial-sql.json', terminal_sql(settings))
        save(directory / 'initial-business.json', business_snapshot(settings))
    save(directory / 'prepare-result.json', {'real_model_calls': 0, 'permits_created': 0,
        'send_attempts': guard.send_attempts, 'reserve_attempts': guard.reserve_attempts,
        'ledger_unchanged': ledger_fingerprint() == ledger_before,
        'matrix': [list(row) for row in MATRIX], 'review_tasks_sha256': sha(directory / 'review-tasks.json')})
    require(ledger_fingerprint() == ledger_before, 'p29_prepare_ledger_changed')


def execute_confirmed(client, headers, frozen, confirmation, visible, formal, directory=None):
    """只按人工选择调用一个原动作。这里没有SQL、STEPS或下一动作选择。"""
    action, ref = confirmation['action'], confirmation['object']
    idempotency = {**headers, 'Idempotency-Key': uuid4().hex}
    if action in {'wait', 'clarify', 'unknown', 'end'}:
        text = ('我按建议等待，尚未进行新的业务操作。' if action == 'wait' else
            '当前可见资料仍不足以确认，我没有进行额外业务操作。')
        return None, text
    if action == 'navigate':
        from services.guide.task_scope import TASK_OBSERVATIONS
        receipt = confirmation['navigation_receipt']
        require(set(receipt) == {'source_kind', 'page', 'success', 'evidence_file', 'evidence_sha256'}
            and receipt['source_kind'] == 'human_observed_browser' and receipt['page'] == ref
            and receipt['success'] is True and directory is not None, 'p29_actual_navigation_receipt_required')
        evidence = Path(receipt['evidence_file']).resolve()
        require(evidence.is_relative_to(directory) and evidence.is_file()
            and sha(evidence) == receipt['evidence_sha256'], 'p29_actual_navigation_evidence_required')
        result = client.post('/assistant/ack', headers=headers, json={'session_id': formal['session_id'],
            'nonce': formal['tool']['nonce'], 'success': True, 'observation': {
                'page': ref, **TASK_OBSERVATIONS[ref], 'product_version': 'v1'}})
        return result, f'我尝试打开{ref}页面，页面切换回执为{"成功" if result.status_code == 200 else "失败"}。'
    index = int(ref[-1]) - 1
    trip = frozen['trip_ids'][index]
    if action == 'lock':
        result = client.post(f"/quotes/{visible['quotes'][index]['id']}/lock", headers=idempotency, json={})
        if result.status_code in (200, 201):
            visible['locks'][ref] = result.json()
    elif action == 'requote':
        result = client.post(f'/trips/{trip}/quotes', headers=idempotency, json={'expected_revision': 1})
        if result.status_code in (200, 201):
            visible['quotes'][index] = result.json()
    elif action in {'pay', 'cancel'}:
        require(ref in visible['locks'], 'p29_no_visible_original_lock')
        lock = visible['locks'][ref]
        suffix = 'payments' if action == 'pay' else 'cancel'
        result = client.post(f"/locks/{lock['id']}/{suffix}", headers=idempotency,
            json={'amount_minor': lock['amount_minor']} if action == 'pay' else {})
    else:
        result = client.get(f'/trips/{trip}/booking', headers=headers)
    response = result.json()
    if action == 'query' and result.status_code == 200:
        pay = (response.get('payment') or {}).get('status') or '未知'
        ticket = (response.get('ticket') or {}).get('status') or '未取得'
        feedback = f'我查询了{ref}，页面显示付款状态{pay}，票据状态{ticket}。'
    else:
        label = {'lock': '锁价', 'requote': '重报价', 'pay': '付款', 'cancel': '取消', 'query': '查询'}[action]
        feedback = f'我尝试了{ref}的{label}，页面返回' + ('请求已受理。' if result.status_code < 300 else f'请求被拒绝，错误为{response.get("error", "未知")}。')
    return result, feedback


def replace_json(path, value):
    staging = path.with_suffix('.next.tmp')
    staging.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    staging.replace(path)


def live_scope(frozen, source):
    from services.guide.task_scope import DynamicTaskScope
    from services.guide.model import MODEL
    return DynamicTaskScope(binding_from(frozen), source, model=MODEL,
        task_sequence=frozen['task_sequence'],
        **({'prompt_version': frozen['prompt_version']} if ROUND >= 30 else {}))


def relative_events(record, state, action, *, accepted, duplicate=False):
    """冻结的对象内相对序号，只消费原HTTP接受事实；不查询业务或答案。"""
    if not accepted or duplicate:
        return []
    kind, ref = action.split('.')[1:]
    if kind == 'pay':
        require(ref not in state, 'p29_second_accepted_payment')
        state[ref] = {'payment_action_sequence': record['accepted_action_sequence'], 'queries': 0}
        return []
    if kind != 'query' or ref not in state or state[ref]['queries'] >= 2:
        return []
    state[ref]['queries'] += 1
    event_id = ('payment-' if state[ref]['queries'] == 1 else 'ticket-') + ref
    record['transitions'][-1]['triggered_event_ids'].append(event_id)
    record['pending_events'].append(event_id)
    return [event_id]


def recorded_relative_event(record, state, event_id, evidence):
    require(event_id in record['pending_events'] and all(row['event_id'] != event_id
        for row in record['events']), 'p29_relative_event_not_due')
    ref = event_id.split('-', 1)[1]
    record['events'].append({'event_id': event_id,
        'after_accepted_action': record['accepted_action_sequence'],
        'payment_action_sequence': state[ref]['payment_action_sequence'],
        'accepted_query_after_payment': state[ref]['queries'], 'event_rule': EVENT_RULE['version'],
        'evidence': evidence})
    record['pending_events'].remove(event_id)


def add_scope_reviews(directory):
    from services.guide.experiment import approval_values
    from services.guide.model import _today
    pricing = read(LOCAL / 'model-review.json')
    require(pricing.get('checked_on') == _today() and pricing.get('result') == 'same_as_current_model_constants'
        and pricing.get('model') == 'deepseek-flash' and pricing.get('model_revision') == 'DeepSeek-V4.1-Flash-1017'
        and pricing.get('input_per_million') == '2' and pricing.get('cache_hit_per_million') == '0.04'
        and pricing.get('output_per_million') == '8', 'p29_current_price_review_required')
    save(directory / 'experiment.json', approval_values(pricing['checked_on']))
    tasks = read(directory / 'review-tasks.json')
    specs = []
    for frozen in tasks:
        seq = frozen['task_sequence']
        scope = live_scope(frozen, read(directory / f'initial-{seq}-source.json'))
        review = scope.review()
        save(directory / f'scope-{seq}-review.json', review)
        specs.append({key: frozen[key] for key in ('task_ref', 'case_id', 'group', 'max_rounds')} |
            {'scope_hash': review['scope_hash']})
    save(directory / 'task-specs.json', specs)
    review_files = ['review-tasks.json', 'releases.json', 'task-specs.json', 'experiment.json'] + [
        f'{prefix}-{seq}-{suffix}.json' for seq in range(1, 5) for prefix, suffix in
        (('initial', 'source'), ('initial', 'actual'), ('scope', 'review'))]
    implementations = ['services/guide/task_runtime.py', 'services/guide/task_scope.py',
        'services/guide/task_budget.py', 'services/guide/assistant.py', 'services/guide/model.py',
        'services/guide/experiment.py', 'services/guide/budget.py', 'scripts/round29_task.py']
    if ROUND >= 30:
        implementations += ['services/guide/fair_runtime.py', 'services/guide/releases.py',
            'services/guide/knowledge.py', 'scripts/round30_task.py', 'scripts/round28_records.py',
            'scripts/round28_run.py', 'services/business/assistant_projection.py']
        receipt_module = ROOT / 'services/guide/reply_receipts.py'
        if receipt_module.is_file():
            implementations.append(str(receipt_module.relative_to(ROOT)).replace('\\', '/'))
        if batch_name() == 'Q21':
            implementations.append('scripts/round30_recovery.py')
        if ROUND >= 31:
            implementations += ['services/guide/action_contract.py', 'scripts/round31_task.py']
        if ROUND == 32:
            implementations.append('scripts/round32_task.py')
        if ROUND == 33:
            implementations.append('scripts/round33_task.py')
        if ROUND == 34:
            implementations.append('scripts/round34_task.py')
        if ROUND == 35:
            implementations.append('scripts/round35_task.py')
        if ROUND == 36:
            implementations.append('scripts/round36_task.py')
        if ROUND == 37:
            implementations.append('scripts/round37_task.py')
        if ROUND == 38:
            implementations.append('scripts/round38_task.py')
        if ROUND == 39:
            implementations.append('scripts/round39_task.py')
        if ROUND == 40:
            implementations.append('scripts/round40_task.py')
    save(directory / 'review-manifest.json', {'files': {name: sha(directory / name) for name in review_files},
        'implementation_sha256': {name: sha(ROOT / name) for name in implementations},
        'event_rule': EVENT_RULE, 'matrix': [list(row) for row in MATRIX], 'approved': False,
        'package': PACKAGE, 'price_checked_on': pricing['checked_on'],
        'model_review_sha256': sha(LOCAL / 'model-review.json'),
        **({'request_ceiling': 17, 'reserved_per_request_cny': '0.032',
            'reserved_ceiling_cny': '0.544', 'prefix_requests': 33, 'legacy_sidecars': 18}
            if ROUND == 34 else {}),
        **({'request_ceiling': 16, 'reserved_per_request_cny': '0.032',
            'reserved_ceiling_cny': '0.512', 'prefix_requests': 34, 'legacy_sidecars': 20}
            if ROUND == 35 else {}),
        **({'request_ceiling': 15, 'reserved_per_request_cny': '0.032',
            'reserved_ceiling_cny': '0.480', 'prefix_requests': 35, 'legacy_sidecars': 22}
            if ROUND == 36 else {}),
        **({'request_ceiling': 14, 'reserved_per_request_cny': '0.032',
            'reserved_ceiling_cny': '0.448', 'prefix_requests': 36, 'legacy_sidecars': 24}
            if ROUND == 37 else {}),
        **({'request_ceiling': 13, 'reserved_per_request_cny': '0.032',
            'reserved_ceiling_cny': '0.416', 'prefix_requests': 37, 'legacy_sidecars': 26}
            if ROUND == 38 else {}),
        **({'request_ceiling': 12, 'reserved_per_request_cny': '0.032',
            'reserved_ceiling_cny': '0.384', 'prefix_requests': 38, 'legacy_sidecars': 28}
            if ROUND == 39 else {}),
        **({'request_ceiling': 11, 'reserved_per_request_cny': '0.032',
            'reserved_ceiling_cny': '0.352', 'prefix_requests': 39, 'legacy_sidecars': 30}
            if ROUND == 40 else {}),
        **({'action_contract_version': ACTION_CONTRACT_VERSION,
            'confirmation_rule': 'formal-digest-contract-digest-version-complete-host-first-line',
            'stop_rule': 'first-failure-stops-entire-batch-no-retry-no-reuse'} if ROUND >= 31 else {})})


def checked_review(directory):
    from services.guide.model import _today
    review = read(directory / 'review-manifest.json')
    require(review.get('package', 'Q19-P29-continuous-task') == PACKAGE, 'task_review_package_mismatch')
    if ROUND >= 30:
        require(sha(LOCAL / 'model-review.json') == review['model_review_sha256'], 'task_price_review_changed')
    require(review['price_checked_on'] == _today(), 'p29_review_expired_recheck_and_new_decision_required')
    require(all(sha(directory / name) == expected for name, expected in review['files'].items())
        and all(sha(ROOT / name) == expected for name, expected in review['implementation_sha256'].items()),
        'p29_frozen_review_changed')
    return review


def finalize_review(directory):
    """只重冻最终源码hash；初始payload/SQL/旧manifest原件均保留。"""
    manifest = read(directory / 'review-manifest.json')
    require(not (directory / 'review-manifest.initial.json').exists(), 'p29_review_already_finalized')
    require(all(sha(directory / name) == expected for name, expected in manifest['files'].items()),
        'p29_initial_review_artifact_changed')
    save(directory / 'review-manifest.initial.json', manifest)
    if ROUND >= 30:
        manifest['implementation_sha256'].setdefault('services/business/assistant_projection.py', '')
    manifest['implementation_sha256'] = {name: sha(ROOT / name)
        for name in manifest['implementation_sha256']}
    manifest['initial_manifest_sha256'] = sha(directory / 'review-manifest.initial.json')
    replace_json(directory / 'review-manifest.json', manifest)


def init_grant(directory, decision_path):
    """只能在收到新的明确批准后调用；prepare/run从不自行创建许可。"""
    from services.guide.budget import BudgetLedger
    from services.guide.task_budget import create_q19_grant
    checked_review(directory)
    decision = read(decision_path)
    require(decision.get('package') == PACKAGE and decision.get('approved') is True
        and decision.get('source_kind') == 'explicit_user_reply' and type(decision.get('source_reply')) is str
        and bool(decision['source_reply'].strip()) and decision.get('review_manifest_sha256') ==
            sha(directory / 'review-manifest.json'), 'p29_explicit_matching_decision_required')
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    create_q19_grant(ledger, read(directory / 'task-specs.json'), sha(decision_path), batch=batch_name())



def finish_task_from_terminal(ledger, task_ref, verdict):
    """31独立终态失败即停全批；正常业务拒绝仍由下一轮新提案处理。"""
    from services.guide.task_budget import finish_q19_task, stop_q19_batch
    if ROUND >= 31 and verdict.get('passed') is False:
        stop_q19_batch(ledger, 'terminal_task_failed', batch=batch_name())
        raise RuntimeError('p31_independent_terminal_failed')
    finish_q19_task(ledger, task_ref, batch=batch_name())


def round_receipt(ledger, task_ref, turn):
    """按本task/round的intent→request_id关联，不能凭候选文件猜启动/零费用。"""
    try:
        grant = read(getattr(ledger, batch_name().lower() + '_path'))
        intents = [row for row in grant['intents'] if row['binding']['task_ref'] == task_ref
            and row['binding']['round_index'] == turn]
        if not intents:
            return False, None
        require(len(intents) == 1, 'p29_unexpected_multiple_physical_requests')
        receipts = {row['request_id']: row for row in ledger.receipt_snapshot()}
        receipt = receipts.get(intents[0]['request_id'])
        # intent/pending只证明发送可能发生；已结算且实际usage确定才确认真实启动。
        if receipt is None or receipt['state'] != 'settled':
            return None, None
        return True, {'reservation_ref': receipt['request_id'], 'state': 'settled',
            'accounted_cny': receipt['accounted_cny']}
    except Exception:
        return None, None


def server(directory, rehearsal=False):
    import uvicorn
    from services.guide.budget import BudgetLedger
    from services.guide.model import DeepSeekModelAdapter
    from services.guide.task_budget import TaskPermitBinding, validated_q19_permit
    frozen = read(directory / 'task-binding.json')
    _, settings = configuration()
    app = create_app(settings)
    assistant = app.state.assistant
    assistant.releases = Releases('test', directory / 'releases.json')
    assistant.external_model = False
    assistant.report_dir = directory / 'model-runs'
    scope = live_scope(frozen, read(directory / 'approved-source.json'))
    runtime = install_task_runtime(assistant, binding_from(frozen), scope=scope, real_model=not rehearsal)
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    def authorize(value):
        bound = scope.authorize_actual(value)
        permit_binding = TaskPermitBinding(**{key: getattr(bound, key) for key in
            ('task_ref', 'task_sequence', 'round_index', 'physical_index', 'source_hash', 'actual_hash', 'scope_hash')})
        return validated_q19_permit(permit_binding, grant_paths(ledger)[0], batch=batch_name())
    def factory(validator):
        async def generate(value, deadline):
            if rehearsal:
                validator(value)
                scope.authorize_actual(value)
                if ROUND >= 31:
                    candidate = {'content': json.dumps({'v': 1, 'action': {'kind': 'query',
                        'target': 'trip_1', 'entry': 'booking.reconcile', 'params': {}},
                        'reason': ('需核对trip_1的锁价与券归属的实际状态。' if ROUND == 33
                                   else '当前状态需要核验。'), 'conditions': '仅依据当前可见资料。',
                        'check': '页面返回的付款与票据状态。'}, ensure_ascii=False), 'tool_calls': []}
                else:
                    candidate = {'content': ('当前动作：请在预订与票据查询trip_1的原预订状态。'
                        if ROUND >= 30 else '请查询trip_1的原预订状态。'), 'tool_calls': []}
                if ROUND in {34, 35, 36}:
                    first = runtime.accepted_rounds == 0
                    candidate = {'content': json.dumps({'v': 1,
                        'action': {'kind': 'lock' if first else 'query', 'target': 'trip_1',
                            'entry': 'booking.lock' if first else 'booking.reconcile', 'params': {}},
                        'reason': ('需先锁价以确定券归属与trip_1最终应付额。' if ROUND == 35 and first else '本轮合成运输夹具。'), 'conditions': '仅依据当前可见资料。',
                        'check': '核对trip_1锁价结果、券归trip_1、冻结金额及有效性。' if first
                            else '需核对trip_1的锁价与券归属的实际状态。'}, ensure_ascii=False), 'tool_calls': []}
                result = ModelResult(candidate, TokenUsage(0, 0, 0), 'offline-rehearsal', Decimal(0), 'offline-rehearsal')
            else:
                adapter = DeepSeekModelAdapter(ledger, input_validator=validator,
                    **{batch_name().lower() + '_grant_path': grant_paths(ledger)[0],
                       batch_name().lower() + '_authorizer': authorize},
                    protocol_report_dir=directory / 'protocol')
                result = await adapter.generate_experiment(value, deadline,
                    approval_path=directory / 'experiment.json')
            save(directory / f'model-{runtime.accepted_rounds+1:02d}.json', {
                'mode': 'offline_rehearsal' if rehearsal else 'real_model',
                'candidate': result.candidate, 'usage': asdict(result.usage),
                'reservation_ref': result.reservation_id, 'accounted_cny': str(result.amount_cny)})
            return result
        return SimpleNamespace(generate_experiment=generate)
    assistant.adapter_factory = factory
    @app.middleware('http')
    async def host_feedback(request, call_next):
        if request.url.path.startswith('/assistant'):
            for text in read(directory / 'host-feedback.json'):
                runtime.allow_feedback(text)
        try:
            return await call_next(request)
        finally:
            replace_json(directory / 'runtime-inputs.json', runtime.inputs)
    @app.get('/__round29/binding')
    def marker():
        return {'task_binding_sha256': sha(directory / 'task-binding.json'),
            'instance': sandbox.check_cluster()['instance_id'], 'mode': 'rehearsal' if rehearsal else batch_name()}
    instance = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8000,
        access_log=False, proxy_headers=False))
    async def serve():
        task = asyncio.create_task(instance.serve())
        while not task.done():
            if (directory / 'server.stop').exists():
                instance.should_exit = True
            await asyncio.sleep(.1)
        await task
    guard = NoModelTraffic()
    try:
        if rehearsal:
            with guard.installed():
                asyncio.run(serve())
        else:
            asyncio.run(serve())
    finally:
        replace_json(directory / 'runtime-inputs.json', runtime.inputs)
        save(directory / 'server-result.json', {'mode': 'rehearsal' if rehearsal else batch_name(),
            'send_attempts_blocked': guard.send_attempts, 'reserve_attempts_blocked': guard.reserve_attempts})


def create_instance(review, directory, sequence):
    """各任务独立同参数初态；重新采集身份/时间，不借上一组业务或会话。"""
    from fastapi.testclient import TestClient
    require(not directory.exists(), 'p29_task_directory_exists_no_replay')
    directory.mkdir(parents=True)
    frozen = read(review / 'review-tasks.json')[sequence-1]
    config, settings = configuration()
    reset_test_database(config['test_database_url'], confirmation='llm_business_test', fixture='two-trips-one-coupon')
    with TestClient(create_app(settings)) as client:
        with ordinary_session(client, settings) as headers:
            write(client, headers, '/payment-profile', {'source_ref': 'demo_personal_alice', 'expected_revision': 0}, 'PUT')
            frozen['trip_ids'] = [write(client, headers, '/trips', {'flight_id': 'DEMO-'+size,
                'travel_date': '2026-10-01', 'passengers': 1})['id'] for size in ('LARGE', 'SMALL')]
            frozen['quotes'] = [write(client, headers, f'/trips/{trip}/quotes', {'expected_revision': 1})
                for trip in frozen['trip_ids']]
    save(directory / 'task-binding.json', frozen)
    save(directory / 'releases.json', read(review / 'releases.json'))
    save(directory / 'approved-source.json', read(review / f'initial-{sequence}-source.json'))
    if (review / 'experiment.json').is_file():
        save(directory / 'experiment.json', read(review / 'experiment.json'))
    save(directory / 'host-feedback.json', [])
    save(directory / 'initial-sql.json', terminal_sql(settings))
    save(directory / 'initial-business.json', business_snapshot(settings))
    return frozen, settings


def run(review, directory, sequence, rehearsal=False):
    from services.guide.budget import BudgetLedger
    from services.guide.task_budget import confirm_q19_round, finish_q19_task, stop_q19_batch
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    # 许可存在性检查在任何合成reset之前；不从执行29推定批准。
    if not rehearsal:
        checked_review(review)
        grant_path, stop_path = grant_paths(ledger)
        require(grant_path.is_file() and not stop_path.exists(), f'p{ROUND}_approved_{batch_name().lower()}_required')
        require((review / 'experiment.json').is_file(), 'p29_current_price_review_required')
        grant = read(grant_path)
        expected = read(review / 'task-specs.json')
        require(grant.get('task_specs') == expected, 'p29_review_grant_mismatch')
        require(all(row['finished'] for row in grant['tasks'][:sequence-1])
            and not grant['tasks'][sequence-1]['finished'], 'p29_task_order')
    guard, journal = NoModelTraffic(), HTTPJournal()
    journal.branch = f'p{ROUND}-task-{sequence}'
    ledger_before = ledger_fingerprint()
    child = None
    record = None
    failed = False
    turn = 0
    with sandbox.runner_lock(), guard.installed() if rehearsal else nullcontext():
        frozen, settings = create_instance(review, directory, sequence)
        record = freeze_task(group=frozen['group'], task_ref=frozen['task_ref'], case_id=frozen['case_id'],
            task_class='completable' if frozen['case_id'] == 'DEV27-05' else 'unknown',
            mode='offline_scripted' if rehearsal else 'real_model', host_only=frozen,
            events=frozen['events'])
        log = (directory / 'server.log').open('xb')
        command = [sys.executable, '-X', 'utf8', str(ENTRY), 'server', '--directory', str(directory)]
        if rehearsal:
            command.append('--rehearsal')
        child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with httpx.Client(base_url='http://127.0.0.1:8000', trust_env=False, follow_redirects=False,
                timeout=35, event_hooks={'response': [journal.response]}) as client:
                deadline = time.monotonic() + 30
                while True:
                    require(child.poll() is None and time.monotonic() < deadline, 'p29_server_not_ready')
                    try:
                        marker = client.get('/__round29/binding')
                        if marker.status_code == 200:
                            require(marker.json()['task_binding_sha256'] == sha(directory / 'task-binding.json')
                                and marker.json()['instance'] == sandbox.check_cluster()['instance_id'], 'p29_wrong_server')
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.1)
                with ordinary_session(client, settings) as headers:
                    session, feedback, consumed, page = None, [], [], 'booking'
                    visible, event_state = {'quotes': frozen['quotes'], 'locks': {}}, {}
                    began = time.monotonic()
                    for turn in range(1, (2 if rehearsal else frozen['max_rounds']) + 1):
                        if not rehearsal:
                            # 主动等待共享滚动窗口；不是失败后自动重试。
                            rows = ledger.receipt_snapshot()
                            recent = sorted(row['dispatched_at'] for row in rows if row['dispatched_at'] > time.time() - 60)
                            while len(recent) >= 6:
                                delay = max(.05, recent[-6] + 60.05 - time.time())
                                require(time.monotonic() - began + delay < 275, 'p29_rate_window_exceeds_task_deadline')
                                time.sleep(min(.2, delay))
                                rows = ledger.receipt_snapshot()
                                recent = sorted(row['dispatched_at'] for row in rows if row['dispatched_at'] > time.time() - 60)
                        before = business_snapshot(settings)
                        response = client.post('/assistant/chat', headers=headers,
                            json=body_for(frozen, frozen['goal'] if turn == 1 else feedback[-1], session_id=session, page=page))
                        after = business_snapshot(settings)
                        save(directory / f'turn-{turn:02d}.json', {'formal_reply': response.json(), 'http': response.status_code,
                            'before_business': before, 'after_business': after, 'real_model': not rehearsal})
                        require(before['sha256'] == after['sha256'], 'p29_consult_business_write')
                        model_path = directory / f'model-{turn:02d}.json'
                        model = read(model_path) if model_path.exists() else None
                        started, usage = (False, None) if rehearsal else round_receipt(ledger, frozen['task_ref'], turn)
                        record_model_attempt(record, round_ref=f'turn-{turn}', started=started,
                            suggestion=model and (rehearsal or started is True) and {
                                'candidate': model['candidate'], 'formal_reply': response.json()} or None,
                            usage=usage)
                        require(response.status_code == 200, 'p29_formal_delivery_failed')
                        formal = response.json()
                        session = formal['session_id']
                        if not rehearsal:
                            confirm_q19_round(ledger, frozen['task_ref'], turn, batch=batch_name())
                        awaiting = {'task_ref': frozen['task_ref'], 'turn': turn, 'formal_reply_sha256': digest(formal),
                            'formal_reply': formal, 'confirmation_file': str(directory / f'confirmation-{turn:02d}.json'),
                            'required_fields': ['quote', 'action', 'object', 'actor', 'faithful', 'ambiguous', 'note']}
                        if ROUND >= 31:
                            awaiting.update(action_sha256=formal.get('action_sha256'),
                                action_contract_version=formal.get('action_contract_version'))
                            awaiting['required_fields'] += ['action_sha256', 'action_contract_version']
                            awaiting['unique_action'] = dict(zip(('action', 'object', 'quote'), structured_action(formal)))
                        replace_json(directory / 'awaiting-confirmation.json', awaiting)
                        path = directory / f'confirmation-{turn:02d}.json'
                        if rehearsal:
                            example = {'task_ref': frozen['task_ref'], 'turn': turn,
                                'formal_reply_sha256': digest(formal), 'quote': formal['reply'], 'action': 'query',
                                'object': 'trip_1', 'actor': 'engineering_synthetic_user', 'faithful': True,
                                'ambiguous': False, 'note': 'offline_fixture_only_not_model'}
                            if ROUND >= 31:
                                kind, ref, quote = structured_action(formal)
                                example.update(action=kind, object=ref, quote=quote,
                                    action_sha256=formal['action_sha256'],
                                    action_contract_version=formal['action_contract_version'])
                            save(path, example)
                        print(f'正式交付已保存：{directory / f"turn-{turn:02d}.json"}；等待 {path}', flush=True)
                        while not path.exists():
                            require(child.poll() is None, 'p29_process_loss')
                            require(time.monotonic() - began < 300, 'p29_confirmation_deadline')
                            time.sleep(.2)
                        confirmation = validate_confirmation(read(path), formal, task_ref=frozen['task_ref'],
                            turn=turn, consumed=consumed)
                        consumed.append(digest(confirmation))
                        result, text = execute_confirmed(client, headers, frozen, confirmation, visible, formal, directory)
                        # 各次真实尝试都有可见序号；相同状态文本不复活旧回执，也不阻断正常等待。
                        text = f'第{turn}次可见尝试：{text}'
                        save(directory / f'execution-{turn:02d}.json', {'confirmation': confirmation,
                            'original_http': None if result is None else {'status': result.status_code,
                            'path': result.request.url.path, 'visible_response': result.json()}, 'feedback': text})
                        if result is not None:
                            action = f"booking.{confirmation['action']}.{confirmation['object']}"
                            due = record_transition(record, action_ref=f'action-{turn}', action=action,
                                actor=confirmation['actor'], http_status=result.status_code, accepted=result.status_code < 300,
                                suggestion_ref=f'turn-{turn}', engineering_added_step=rehearsal,
                                reason='offline_fixture_only' if rehearsal else None,
                                evidence={'formal_reply_sha256': digest(formal), 'confirmation_sha256': digest(confirmation),
                                    'http_sequence': len(journal.events), 'response_sha256': digest(result.json())})
                            require(not due, 'p29_absolute_event_not_allowed')
                            for event_id in relative_events(record, event_state, action, accepted=result.status_code < 300):
                                recorded_relative_event(record, event_state, event_id,
                                    event_worker(settings, event_id, frozen['trip_ids']))
                        feedback.append(text)
                        replace_json(directory / 'host-feedback.json', feedback)
                        if confirmation['action'] == 'navigate' and result.status_code == 200:
                            page = confirmation['object']
                        # SQL只用于动作后的独立判定和保留中间态，永不选择动作或反馈。
                        sql = terminal_sql(settings)
                        save(directory / f'after-action-{turn:02d}-sql.json', sql)
                        if confirmation['action'] in {'unknown', 'end'}:
                            break
                    final = terminal_sql(settings)
                    save(directory / 'final-sql.json', final)
                    verdict = terminal_verdict(final, frozen['trip_ids']) if frozen['case_id'] == 'DEV27-05' else {
                        'passed': None, 'business_unchanged': business_snapshot(settings)['sha256'] ==
                            read(directory / 'initial-business.json')['sha256'], 'semantic_review_required': True}
                    record_terminal(record, passed=verdict['passed'], evidence=verdict)
                    if not rehearsal:
                        finish_task_from_terminal(ledger, frozen['task_ref'], verdict)
        except BaseException as error:
            failed = True
            if turn > 0 and not any(row['round_ref'] == f'turn-{turn}' for row in record['rounds']):
                started, usage = (False, None) if rehearsal else round_receipt(ledger, frozen['task_ref'], turn)
                record_model_attempt(record, round_ref=f'turn-{turn}', started=started, usage=usage)
            save(directory / 'interrupted.json', {'kind': type(error).__name__, 'reason': str(error),
                'automatic_retry': False, 'real_model': not rehearsal})
            if not rehearsal:
                semantic_errors = {'p29_confirmation_explicit_quote_required',
                    'p29_human_faithful_confirmation_required', 'p29_action_not_in_quote',
                    'p29_object_ambiguous', 'p29_conditional_or_negative_action',
                    'p29_action_not_explicit_instruction', 'p29_past_fact_not_instruction',
                    'p29_actual_navigation_receipt_required', 'p29_actual_navigation_evidence_required',
                    'p29_navigation_tool_mismatch', 'p29_nonbusiness_object', 'p29_no_visible_original_lock'}
                if ROUND >= 31:
                    # 新包沿首异常整批停止；内容/人工确认失败不擅自续跑其他任务。
                    stop_q19_batch(ledger, 'controller_interrupted_or_failed', batch=batch_name())
                elif isinstance(error, RuntimeError) and str(error) in semantic_errors and turn > 0:
                    record_terminal(record, passed=False, method='independent_semantic_review',
                        evidence={'kind': 'host_cannot_faithfully_execute_formal_suggestion', 'reason': str(error)})
                    finish_q19_task(ledger, frozen['task_ref'], batch=batch_name())
                else:
                    stop_q19_batch(ledger, 'controller_interrupted_or_failed', batch=batch_name())
            raise
        finally:
            if child is not None and child.poll() is None:
                (directory / 'server.stop').touch(exist_ok=False)
                child.wait(timeout=30)
            log.close()
            if not rehearsal:
                # 失联后等发送侧退出再核一次；真实已结算失败不能掉出任务分母。
                for row in record['rounds']:
                    if row['model_started'] is not True:
                        index = int(row['round_ref'].split('-')[-1])
                        row['model_started'], row['usage'] = round_receipt(ledger, frozen['task_ref'], index)
            save(directory / 'http-evidence.json', {'authentication_requests_omitted': True, 'events': journal.events})
            save(directory / 'task-record.json', record)
            save(directory / 'task-summary.json', summarize([record]))
            save(directory / 'controller-result.json', {'real_model': not rehearsal, 'failed': failed,
                'server_exit': child.returncode if child else None,
                'model_send_attempts_blocked': guard.send_attempts,
                'budget_reserve_attempts_blocked': guard.reserve_attempts,
                'rehearsal_ledger_unchanged': ledger_fingerprint() == ledger_before if rehearsal else None})


def checked_directory(value):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path and path.is_relative_to(LOCAL),
        'p29_directory_outside_workspace')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'scope-reviews', 'finalize-review', 'rehearse', 'run', 'server', 'init-grant'))
    parser.add_argument('--directory', required=True, type=checked_directory)
    parser.add_argument('--review', type=checked_directory)
    parser.add_argument('--task-sequence', type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument('--rehearsal', action='store_true')
    parser.add_argument('--decision', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.directory)
        add_scope_reviews(args.directory)
    elif args.action == 'scope-reviews':
        add_scope_reviews(args.directory)
    elif args.action == 'finalize-review':
        finalize_review(args.directory)
    elif args.action == 'server':
        server(args.directory, args.rehearsal)
    elif args.action == 'init-grant':
        require(args.decision is not None and args.decision.resolve().is_relative_to(LOCAL),
            'p29_decision_path_required')
        init_grant(args.directory, args.decision)
    else:
        require(args.review is not None, 'p29_review_required')
        run(args.review, args.directory, args.task_sequence, args.action == 'rehearse')


if __name__ == '__main__':
    main()
