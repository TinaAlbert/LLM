"""P30 前瞻提示词、单步格式及版本冻结；模型/HTTP/PG均不参与。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from services.business.assistant_projection import SHARED_DOCUMENT, SHARED_DOCUMENT_V2
from services.guide.assistant import (SYSTEM_V6, SYSTEM_V7, SYSTEM_V8, display_state,
    prompt_system, validate_reply, validate_single_action_reply)
from services.guide.fair_runtime import FairTaskBinding, fair_input
from services.guide.knowledge import fingerprint
from services.guide.model import ModelUnavailable
from services.guide.task_runtime import install_task_runtime
from services.guide.task_scope import DynamicTaskScope
from test_assistant import knowledge, observation, Request
from test_shared_prompt21 import SharedHarness, body, facts
from test_task_scope import initial_source, GOAL, FEEDBACK, changed


def source7():
    value = initial_source()
    value['messages'][0]['content'] = SYSTEM_V7
    context = json.loads(value['messages'][1]['content'])
    context['current_state']['documented'] = deepcopy(SHARED_DOCUMENT_V2)
    value['messages'][1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    return value


def binding(group='B1'):
    return FairTaskBinding('DEV27-05', 'p30-test-task', group, 'synthetic-actor',
        tuple(str(uuid4()) for _ in range(2)), 'kb-fixed', GOAL)


def test_new_public_semantics_keep_old_prompt_and_document_replayable_without_answers():
    assert hashlib.sha256(SYSTEM_V6.encode()).hexdigest() == 'ed1fc7459c59107e83cd99ab80c72e244b9c31aa6d2f444580a5100f08eec02e'
    assert prompt_system('assistant-7') == SYSTEM_V7 and SYSTEM_V7.startswith(SYSTEM_V6)
    assert SHARED_DOCUMENT['revision'] == 1 and SHARED_DOCUMENT_V2['revision'] == 2
    old = facts()
    old['documented'] = deepcopy(SHARED_DOCUMENT)
    original = deepcopy(old)
    assert display_state(old, 'assistant-6')['documented'] == SHARED_DOCUMENT
    assert display_state(old, 'assistant-7')['documented'] == SHARED_DOCUMENT_V2 and old == original
    for phrase in ('不可拆分', '最高减免额不是余额', '首个合格锁价', '重新核价'):
        assert phrase in SHARED_DOCUMENT_V2['text']
    for answer in ('1400', '1480', '140000', '148000', 'STEPS', '先大后小', '先trip_1', 'DEV27-'):
        assert answer not in SYSTEM_V7 and answer not in SHARED_DOCUMENT_V2['text']


@pytest.mark.parametrize('text', [
    '当前动作：在行程详情为trip_1尝试锁价。核对：报价、库存、期限和资格仍满足；成功后查看实际金额与券归属。',
    '当前动作：请核实公开资料是否支持所问功能。当前资料未覆盖，尚不能确认。',
    '当前动作：尝试打开付款资料分区。核对：等待页面回执，导航尚未确认。',
    '当前动作：查询原付款结果。核对：\n1. 付款状态是否仍处理中。\n2. 资格和期限是否改变。',
])
def test_single_action_allows_conditions_unknown_and_navigation(text):
    assert validate_reply(text, facts(), prompt_version='assistant-7') == text


@pytest.mark.parametrize('text', [
    '请锁价，然后付款。',
    '当前动作：先尝试锁价，再付款。核对：实际金额。',
    '当前动作：锁价。当前动作：付款。',
    '当前动作：依次操作。\n1. 请锁价。\n2. 再付款。',
    '当前动作：\n核对：报价。',
])
def test_single_action_rejects_missing_empty_duplicate_and_explicit_multiple_actions(text):
    with pytest.raises(ValueError, match='single_action_required'):
        validate_single_action_reply(text)


@pytest.mark.parametrize('text,reason', [
    ('当前动作：核对报价。trip_1预报价999模拟元。', 'amount_unverified'),
    ('当前动作：核对报价。预估总价1550模拟元，报价仍有效。', 'shared_total_conditions_missing'),
    ('当前动作：核对票据。两个行程已经完成。', 'shared_completion_unverified'),
])
def test_new_format_does_not_waive_original_amount_conditions_or_completion_guard(text, reason):
    with pytest.raises(ValueError, match=reason):
        validate_reply(text, facts(), prompt_version='assistant-7')


def test_fair_transform_and_scope_require_explicit_exact_new_prompt_and_document():
    source = source7()
    fixed = deepcopy(source)
    b0 = fair_input(source, 'B0', prompt_version='assistant-7')
    assert source == fixed and 'current_state' not in json.loads(b0['messages'][1]['content'])
    assert fair_input(source, 'B1', prompt_version='assistant-7') == source
    with pytest.raises(ValueError):
        fair_input(source, 'B0')
    with pytest.raises(ModelUnavailable):
        DynamicTaskScope(binding(), source)
    gate = DynamicTaskScope(binding(), source, prompt_version='assistant-7')
    assert gate.review()['prompt_version'] == 'assistant-7'
    wrong = deepcopy(source)
    wrong['messages'][0]['content'] += '放宽权限。'
    with pytest.raises(ModelUnavailable):
        DynamicTaskScope(binding(), wrong, prompt_version='assistant-7')
    wrong = changed(source, lambda context: context['current_state'].update(documented=SHARED_DOCUMENT))
    with pytest.raises(ModelUnavailable):
        DynamicTaskScope(binding(), wrong, prompt_version='assistant-7')
    first = gate.freeze_round(source, source, round_index=1, physical_index=1)
    gate.authorize_actual(first.payload)
    later = changed(source, lambda context: context.update(question=FEEDBACK))
    later['messages'][0]['content'] = SYSTEM_V6
    with pytest.raises(ModelUnavailable):
        gate.freeze_round(later, later, round_index=2, physical_index=1, feedback={FEEDBACK})


@pytest.mark.parametrize('prompt_version,system', [('assistant-7', SYSTEM_V7), ('assistant-8', SYSTEM_V8)])
def test_new_release_task_runtime_uses_frozen_seven_and_stops_same_release_prompt_swap(tmp_path, prompt_version, system):
    async def scenario():
        h = SharedHarness(tmp_path, {'content': '当前动作：在行程详情核对当前报价。', 'tool_calls': []})
        h.facts = facts()
        sources = [observation('profile'), observation('trips')]
        old = h.releases.save_candidate(knowledge(), sources, prompt_version='assistant-6')
        h.releases.publish(old['version'])
        new = h.releases.save_candidate(knowledge(), sources, prompt_version=prompt_version)
        assert h.releases.active()['prompt_version'] == 'assistant-6'
        h.releases.publish(new['version'])
        ids = tuple(str(uuid4()) for _ in range(2))
        bound = replace(binding('B0'), delegated_trip_ids=ids,
            release_version=new['version'], goal='一起安排两个行程。')
        runtime = install_task_runtime(h, bound)
        result = await h.chat(body(ids), Request())
        assert result['reply'].startswith('当前动作：')
        assert h.calls[0]['messages'][0]['content'] == system
        assert 'current_state' not in json.loads(h.calls[0]['messages'][1]['content'])
        assert runtime.prompt_version == prompt_version and len(h.calls) == 1
        # 即使宿主错误地保留release id，运行中也不能切回旧prompt。
        h.releases.active = lambda: {**new, 'prompt_version': 'assistant-6'}
        with pytest.raises(HTTPException, match='fair_release_changed'):
            runtime._local_only()
    asyncio.run(scenario())


def test_eight_only_adds_explicit_anonymous_object_contract_and_freezes_new_scope():
    assert hashlib.sha256(SYSTEM_V7.encode()).hexdigest() == '7cae0119d05b2b01a10e05b154ee1aaae3c4c9cc85daa80d105b8b546045602e'
    assert SYSTEM_V8.startswith(SYSTEM_V6)
    addition = SYSTEM_V8[len(SYSTEM_V6):]
    # 8合并新增规则，避免后段必要状态超既有12KiB门；旧6/7不重写。
    assert len(addition.encode('utf-8')) <= 270
    for phrase in ('不可拆分', 'cap非余额', '首个合格锁价', '另单重核价',
                   '独立估价不相加', '仅一条', '入口/动作', '必要条件/核对点', '不串联', '等结果'):
        assert phrase in addition
    assert 'trip_1' in addition and 'trip_2' in addition and '不明先核实勿猜' in addition
    for answer in ('1400', '1480', 'STEPS', '先trip_1', 'DEV27-', '付款', '出票'):
        assert answer not in addition
    assert display_state(facts(), 'assistant-8')['documented'] == SHARED_DOCUMENT_V2
    value = source7()
    value['messages'][0]['content'] = SYSTEM_V8
    gate = DynamicTaskScope(binding(), value, prompt_version='assistant-8')
    assert gate.review()['prompt_version'] == 'assistant-8'
    with pytest.raises(ModelUnavailable):
        DynamicTaskScope(binding(), value, prompt_version='assistant-7')
    assert fair_input(value, 'B1', prompt_version='assistant-8') == value
    frozen = gate.freeze_round(value, value, round_index=1, physical_index=1)
    assert gate.authorize_actual(frozen.payload) is frozen
