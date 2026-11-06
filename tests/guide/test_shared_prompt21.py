"""P21离线契约与反例；没有PG、网络、付费请求或测试答案进入运行输入。"""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

from fastapi import HTTPException
import pytest
from pydantic import ValidationError

from services.business.assistant_projection import SHARED_DOCUMENT, DOCUMENT
from services.guide.assistant import (Ack, Chat, SYSTEM_V5, SYSTEM_V6, authorize_shared_export,
    corporate_export_scope, display_state, prompt_system, validate_reply)
from services.guide.model import ModelUnavailable
from services.guide.releases import Releases
from test_assistant import Harness, Request, knowledge, observation, tool_candidate


def facts():
    def data(base, reduction):
        def price(kind):
            return {'ref': kind, 'trip_revision': 1, 'amount_minor': base - reduction,
                    'currency': 'DEMO', 'valid': True,
                    'effective_status': 'active' if kind == 'quote' else 'held',
                    'price_basis': 'estimate' if kind == 'quote' else 'frozen',
                    'base_amount_minor': base, 'coupon_discount_minor': reduction,
                    'coupon_allocation': 'estimate' if kind == 'quote' else 'held'}
        return {'trip': {'revision': 1}, 'quote': price('quote'), 'lock': None,
                'payment': None, 'ticket_task': None, 'ticket': None}
    return {'contract_version': 'assistant-shared-1', 'selected_ref': 'trip_1',
            'delegated_trips': [{'ref': 'trip_1', 'data': data(125000, 25000), 'unknowns': []},
                                {'ref': 'trip_2', 'data': data(55000, 11000), 'unknowns': []}],
            'shared_coupon': {'ref': 'shared_coupon', 'status': 'available', 'percent': 20,
                              'cap_minor': 25000, 'currency': 'DEMO', 'attribution': 'none', 'trip_ref': None},
            'source_kind': 'observed', 'unknowns': ['unexecuted_order_outcomes']}


def payload():
    return {'messages': [{'role': 'system', 'content': prompt_system('assistant-6')},
                         {'role': 'user', 'content': json.dumps({'question': '如何一起安排这两个行程？',
                            'current_state': display_state(facts(), 'assistant-6'), 'history': []}, ensure_ascii=False)}],
            'tools': [], 'max_tokens': 512, 'json_output': False}


def body(ids=None, **changes):
    ids = ids or [uuid4(), uuid4()]
    return Chat(message='一起安排两个行程。', page='trips', observation=observation('trips'),
                trip_id=ids[0], delegated_trip_ids=ids, **changes)


def upgrade(harness):
    sources = [observation('profile'), observation('trips')]
    release = harness.releases.save_candidate(knowledge(), sources, prompt_version='assistant-6')
    harness.releases.publish(release['version'])
    return release


def test_six_keeps_old_prompt_and_corporate_revised_route():
    assert prompt_system('assistant-5') == SYSTEM_V5
    assert prompt_system('assistant-6') == SYSTEM_V6 and SYSTEM_V6.startswith(SYSTEM_V5)
    for clause in ('delegated_trips', 'selected_ref', 'quote.price_basis=estimate', '不占券',
                   'shared_coupon.status', 'consumed不能通过取消恢复', '报价、库存、期限和资格',
                   '两行程都取得有效ticket', '预算与审批', '审批工作台'):
        assert clause in SYSTEM_V6 or clause in SHARED_DOCUMENT['text']
    assert DOCUMENT['revision'] == 2 and '不能在那里替员工提交申请' in DOCUMENT['text']
    for answer in ('1400', '1480', '140000', '148000', '先大后小', 'LARGE', 'SMALL'):
        assert answer not in SYSTEM_V6 and answer not in SHARED_DOCUMENT['text']


@pytest.mark.parametrize('case', ['one', 'duplicate', 'three', 'no_selected', 'other_selected'])
def test_scope_requires_two_distinct_objects_and_current_selection(case):
    identifiers = [uuid4(), uuid4()]
    parameters = {'message': '一起安排', 'page': 'trips', 'observation': observation('trips'),
                  'trip_id': identifiers[0], 'delegated_trip_ids': identifiers}
    if case == 'one': parameters['delegated_trip_ids'] = identifiers[:1]
    if case == 'duplicate': parameters['delegated_trip_ids'] = identifiers[:1] * 2
    if case == 'three': parameters['delegated_trip_ids'] = identifiers + [uuid4()]
    if case == 'no_selected': parameters['trip_id'] = None
    if case == 'other_selected': parameters['trip_id'] = uuid4()
    with pytest.raises(ValidationError):
        Chat(**parameters)


def test_single_object_and_public_entry_remain_optional():
    public = Chat(message='你好', page='trips', observation=observation('trips'))
    assert public.delegated_trip_ids == []
    assert body().delegated_trip_ids


def test_six_candidate_can_read_review_publish_and_rollback_all_old_candidates(tmp_path):
    store = Releases('test', tmp_path / 'releases.json')
    sources = [observation('profile'), observation('trips')]
    old = store.save_candidate(knowledge(), sources, prompt_version='assistant-5')
    store.publish(old['version'])
    saved = deepcopy(store.read()['candidates'][old['version']])
    new = store.save_candidate(old['knowledge'], old['observations'], prompt_version='assistant-6')
    assert new['version'] != old['version']
    assert store.review(new['version'])['legacy_contract'] is False
    store.publish(new['version'])
    assert store.active()['prompt_version'] == 'assistant-6'
    assert store.rollback() == old['version']
    assert store.read()['candidates'][old['version']] == saved
    assert store.rollback() == new['version']


def test_two_object_money_and_coupon_cap_are_formatted_without_modifying_source():
    state = facts()
    original = deepcopy(state)
    shown = display_state(state, 'assistant-6')
    assert state == original
    assert 'amount_minor' not in json.dumps(shown) and 'cap_minor' not in json.dumps(shown)
    assert shown['delegated_trips'][0]['data']['quote']['amount_display'] == '1000.00 模拟元'
    assert shown['delegated_trips'][1]['data']['quote']['coupon_discount_display'] == '110.00 模拟元'
    assert shown['shared_coupon']['cap_display'] == '250.00 模拟元'
    assert shown['time_contract']['business_clock'] == 'business_time'


def test_invalid_price_is_unknown_while_consumed_coupon_is_preserved():
    state = facts()
    row = state['delegated_trips'][0]['data']['quote']
    row.update(valid=False, effective_status='invalidated')
    state['shared_coupon'].update(status='consumed', attribution='delegated_trip', trip_ref='trip_1')
    shown = display_state(state, 'assistant-6')
    assert shown['delegated_trips'][0]['data']['quote']['amount_display'] is None
    assert shown['shared_coupon']['status'] == 'consumed'


@pytest.mark.parametrize('text', ['两个行程已经完成。', '两段行程已出票。', 'trip_2已经出票。'])
def test_one_ticket_cannot_prove_both_goals(text):
    state = facts()
    state['delegated_trips'][0]['data']['ticket'] = {'status': 'valid'}
    with pytest.raises(ValueError, match='completion_unverified'):
        validate_reply(text, state, prompt_version='assistant-6')


def test_known_tickets_and_payment_are_checked_per_object():
    state = facts()
    for row in state['delegated_trips']:
        row['data']['ticket'] = {'status': 'valid'}
    assert validate_reply('两个行程已经完成。', state, prompt_version='assistant-6')
    state['delegated_trips'][0]['data']['payment'] = {'amount_minor': 100000, 'status': 'succeeded'}
    with pytest.raises(ValueError, match='payment_unverified'):
        validate_reply('trip_2已经付款。', state, prompt_version='assistant-6')


@pytest.mark.parametrize('ref,amount', [('trip_2', '1000.00'), ('trip_1', '440.00')])
def test_other_trip_price_cannot_be_reused_for_explicit_reference(ref, amount):
    with pytest.raises(ValueError, match='shared_amount_attribution_unverified'):
        validate_reply(f'{ref}的预报价为{amount}模拟元。', facts(), prompt_version='assistant-6')


@pytest.mark.parametrize('status,text', [('consumed', '优惠券已释放，可以重新使用优惠。'),
                                      ('held', '共享券当前可用。'), ('protected', '优惠券已经释放。')])
def test_resource_protection_cannot_be_reported_as_available(status, text):
    state = facts()
    state['shared_coupon']['status'] = status
    with pytest.raises(ValueError):
        validate_reply(text, state, prompt_version='assistant-6')


def test_computed_total_is_only_estimate_with_conditions_and_never_in_payload():
    state = facts()
    text = '如果报价、库存、期限和资格条件仍满足，预估合计1550.00 模拟元；未执行顺序仍需本人核对。'
    assert validate_reply(text, state, prompt_version='assistant-6') == text
    assert '1550' not in json.dumps(display_state(state, 'assistant-6'))
    with pytest.raises(ValueError, match='shared_total_conditions_missing'):
        validate_reply('总额1550.00 模拟元。', state, prompt_version='assistant-6')
    with pytest.raises(ValueError, match='shared_optimum_unverified'):
        validate_reply('已经验证这是最优顺序。', state, prompt_version='assistant-6')
    with pytest.raises(ValueError, match='amount_unverified'):
        validate_reply('总额1.00 模拟元。', state, prompt_version='assistant-6')


@pytest.mark.parametrize('change', ['question', 'state', 'system', 'limit', 'history'])
def test_shared_export_requires_exact_explicit_preview_and_keeps_old_budget(change, tmp_path):
    original = payload()
    path = tmp_path / 'approval.json'
    with pytest.raises(ModelUnavailable, match='shared_export_not_approved'):
        authorize_shared_export(original, path)
    path.write_text(json.dumps({'package': 'P21-shared-one', 'approved': True,
        'source_kind': 'explicit_user_reply', 'scope_hash': corporate_export_scope(original)}), encoding='utf-8')
    assert authorize_shared_export(original, path) is None
    changed = deepcopy(original)
    context = json.loads(changed['messages'][1]['content'])
    if change == 'question': context['question'] = '改为另一目标'
    if change == 'state': context['current_state']['shared_coupon']['status'] = 'consumed'
    if change == 'history': context['history'] = [{'text': '旧事实'}]
    if change == 'system': changed['messages'][0]['content'] = SYSTEM_V5
    if change == 'limit': changed['max_tokens'] = 1024
    changed['messages'][1]['content'] = json.dumps(context, ensure_ascii=False)
    with pytest.raises(ModelUnavailable, match='shared_export_not_approved'):
        authorize_shared_export(changed, path)


def test_shared_external_gate_runs_before_adapter_and_no_record_is_created(tmp_path):
    async def scenario():
        harness = Harness(tmp_path, {'content': '待核对', 'tool_calls': []})
        harness.external_model = True
        with pytest.raises(ModelUnavailable, match='shared_export_not_approved'):
            await harness.infer(payload(), 'chat', 'builtin-v1')
        assert harness.calls == [] and not (tmp_path / 'runs').exists()
    asyncio.run(scenario())


class SharedHarness(Harness):
    async def state(self, request, who, trip_id, deadline, delegated_trip_ids=()):
        return self.facts, json.dumps(self.facts, sort_keys=True)


def test_shared_scope_requires_six_and_does_not_silently_upgrade(tmp_path):
    async def scenario():
        harness = SharedHarness(tmp_path, {'content': '待核对', 'tool_calls': []})
        with pytest.raises(HTTPException) as error:
            await harness.chat(body(), Request())
        assert error.value.detail == 'assistant_contract_unsupported'
        assert harness.releases.active()['version'] == 'builtin-v1' and not harness.calls
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['other_trip', 'coupon', 'identity', 'release'])
def test_late_candidate_drops_changed_shared_state(change, tmp_path):
    async def scenario():
        harness = SharedHarness(tmp_path, {'content': '请核对两行程。', 'tool_calls': []})
        harness.facts = facts()
        upgrade(harness)
        def after():
            if change == 'other_trip': harness.facts['delegated_trips'][1]['data']['trip']['revision'] = 2
            if change == 'coupon': harness.facts['shared_coupon']['status'] = 'held'
            if change == 'identity': harness.current = type(harness.current)('other', 'other')
            if change == 'release': harness.releases.rollback()
        harness.after = after
        with pytest.raises(HTTPException):
            await harness.chat(body(), Request())
        assert len(harness.calls) == 1
        assert all(not session.history for session in harness.sessions.values())
    asyncio.run(scenario())


def test_navigation_ack_keeps_two_object_scope_and_rejects_other_trip_change(tmp_path):
    async def scenario():
        harness = SharedHarness(tmp_path, tool_candidate('profile'))
        harness.facts = facts()
        upgrade(harness)
        result = await harness.chat(body(), Request())
        pending = harness.sessions[result['session_id']].pending
        assert len(pending['delegated_trip_ids']) == 2
        harness.facts['delegated_trips'][1]['data']['trip']['revision'] = 2
        ack = Ack(session_id=result['session_id'], nonce=result['tool']['nonce'], success=True,
                  observation=observation('profile'))
        with pytest.raises(HTTPException) as error:
            await harness.ack(ack, Request())
        assert error.value.detail == 'state_changed'
    asyncio.run(scenario())


# 共享咨询只缩减可选历史；限额与正式prepare使用同一计数。
def oversized_history_payload():
    value = payload()
    context = json.loads(value['messages'][-1]['content'])
    context.update(goal='为本次明确委托的两个行程继续规划。',
                   history=[{'source_kind': 'user_reported' if i % 2 == 0 else 'model_candidate',
                             'text': f'历史{i}：' + '旧咨询内容。' * 160} for i in range(4)])
    value['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    return value


def test_oversized_shared_input_drops_oldest_history_only_without_mutation():
    from services.guide.assistant import trim_shared_history
    from services.guide.experiment import MAX_INPUT_BYTES, input_size_bytes
    original = oversized_history_payload()
    saved = deepcopy(original)
    before = json.loads(original['messages'][-1]['content'])
    assert input_size_bytes(original) > MAX_INPUT_BYTES
    result = trim_shared_history(original)
    after = json.loads(result['messages'][-1]['content'])
    assert original == saved and input_size_bytes(result) <= MAX_INPUT_BYTES
    assert len(after['history']) < len(before['history'])
    assert after['history'] == before['history'][len(before['history']) - len(after['history']):]
    assert {k: v for k, v in before.items() if k != 'history'} == {k: v for k, v in after.items() if k != 'history'}
    assert result['messages'][:-1] == original['messages'][:-1]
    assert {k: v for k, v in result.items() if k != 'messages'} == {k: v for k, v in original.items() if k != 'messages'}


def test_required_shared_facts_over_cap_are_preserved_and_original_guard_rejects(monkeypatch):
    from types import SimpleNamespace
    from services.guide.assistant import trim_shared_history
    from services.guide import experiment
    value = payload()
    context = json.loads(value['messages'][-1]['content'])
    context.update(goal='必要任务约束。' * 3000, history=[])
    value['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    saved = deepcopy(value)
    result = trim_shared_history(value)
    assert result == saved and value == saved
    assert experiment.input_size_bytes(result) > experiment.MAX_INPUT_BYTES
    monkeypatch.setattr(experiment, 'load_approval', lambda path: {})
    with pytest.raises(ModelUnavailable, match='experiment_input_too_large'):
        experiment.prepare(SimpleNamespace(input_validator=lambda item: item), result, None)


@pytest.mark.parametrize('case', ['legacy_prompt', 'nonshared_six'])
def test_history_trimming_does_not_change_other_prompt_contracts(case):
    from services.guide.assistant import trim_shared_history
    from services.guide.experiment import MAX_INPUT_BYTES, input_size_bytes
    value = oversized_history_payload()
    if case == 'legacy_prompt':
        value['messages'][0]['content'] = SYSTEM_V5
    else:
        context = json.loads(value['messages'][-1]['content'])
        context['current_state']['contract_version'] = 'assistant-corporate-1'
        value['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False)
    assert input_size_bytes(value) > MAX_INPUT_BYTES
    saved = deepcopy(value)
    assert trim_shared_history(value) == saved and value == saved


@pytest.mark.parametrize('remaining', [0, 1])
def test_shared_history_at_or_below_exact_byte_boundary_is_unchanged(remaining):
    from services.guide.assistant import trim_shared_history
    from services.guide.experiment import MAX_INPUT_BYTES, input_size_bytes
    value = payload()
    context = json.loads(value['messages'][-1]['content'])
    context['history'] = [{'source_kind': 'user_reported', 'text': ''}]
    value['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    context['history'][0]['text'] = 'a' * (MAX_INPUT_BYTES - remaining - input_size_bytes(value))
    value['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    assert input_size_bytes(value) == MAX_INPUT_BYTES - remaining
    saved = deepcopy(value)
    assert trim_shared_history(value) == saved and value == saved


def test_shared_chat_applies_history_limit_before_inference(tmp_path):
    from services.guide.experiment import MAX_INPUT_BYTES, input_size_bytes
    async def scenario():
        harness = SharedHarness(tmp_path, {'content': '请重新核对本轮允许观察。', 'tool_calls': []})
        harness.facts = facts()
        upgrade(harness)
        identifiers = [uuid4(), uuid4()]
        first = await harness.chat(body(identifiers), Request())
        session = harness.sessions[first['session_id']]
        history = json.loads(oversized_history_payload()['messages'][-1]['content'])['history']
        session.history = deepcopy(history)
        await harness.chat(body(identifiers, session_id=first['session_id']), Request())
        sent = harness.calls[-1]
        context = json.loads(sent['messages'][-1]['content'])
        assert input_size_bytes(sent) <= MAX_INPUT_BYTES and len(context['history']) < 4
        assert context['goal'] == '一起安排两个行程。'
        assert context['question'] == '一起安排两个行程。'
        assert context['current_state'] == display_state(facts(), 'assistant-6')
        assert context['history'] == history[4 - len(context['history']):]
    asyncio.run(scenario())
