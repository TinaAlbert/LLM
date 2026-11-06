"""P29 动态外发新增风险；离线，不创建许可或访问PG/模型。"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from uuid import uuid4

import pytest

from services.business.assistant_projection import SHARED_DOCUMENT
from services.guide.assistant import SYSTEM_V6, display_state
from services.guide.experiment import NAVIGATION_TOOL, input_size_bytes
from services.guide.fair_runtime import FairTaskBinding, fair_input
from services.guide.knowledge import fingerprint
from services.guide.model import ModelUnavailable
from services.guide.task_scope import DynamicTaskScope


GOAL = '请帮我为这两个行程都拿到票，并在当前条件允许下少花钱'
FEEDBACK = '我尝试为trip_1锁价，页面显示请求已受理。'


def initial_source():
    def trip(ref, flight, base, discount):
        return {'ref': ref, 'unknowns': [], 'data': {
            'trip': {'ref': 'trip', 'revision': 1, 'flight_id': flight, 'travel_date': '2026-10-20',
                     'passengers': 1, 'status': 'draft'},
            'quote': {'ref': 'quote', 'trip_revision': 1, 'amount_minor': base - discount,
                'currency': 'DEMO', 'base_amount_minor': base, 'coupon_discount_minor': discount,
                'valid': True, 'effective_status': 'active', 'expires_at': '2026-09-27T00:05:00+00:00',
                'price_basis': 'estimate', 'coupon_allocation': 'estimate'},
            'lock': None, 'payment': None, 'ticket_task': None, 'ticket': None}}
    state = {'contract_version': 'assistant-shared-1', 'product_id': 'travel-lab', 'product_version': 'v1',
        'rule_version': 'booking-v1', 'policy_version': 'subject-assignment-v1',
        'feature_config_version': 'shared-coupon-1', 'source_kind': 'observed', 'environment': 'local-demo',
        'simulated_identity': 'synthetic_personal', 'disclosure_scope': 'current-session',
        'selected_trip': True, 'selected_ref': 'trip_1', 'delegation': 'two_explicit_owned_trips',
        'delegated_trips': [trip('trip_1', 'DEMO-A', 100000, 20000), trip('trip_2', 'DEMO-B', 60000, 12000)],
        'shared_coupon': {'ref': 'shared_coupon', 'status': 'available', 'percent': 20,
            'cap_minor': 20000, 'currency': 'DEMO', 'attribution': 'none', 'trip_ref': None},
        'state_versions': {'trips': {'trip_1': 1, 'trip_2': 1}, 'payment_profile': 1},
        'business_time': '2026-09-27T00:00:00+00:00',
        'assumptions': ['synthetic_demo_account', 'simulated_business_services'],
        'unknowns': ['unexecuted_order_outcomes'], 'documented': deepcopy(SHARED_DOCUMENT),
        'observed_at': '2026-10-19T00:00:00+00:00', 'valid_until': '2026-10-19T00:00:30+00:00'}
    context = {'goal': GOAL, 'current_page': 'booking', 'current_state': display_state(state, 'assistant-6'),
        'allowed_pages': ['welcome', 'profile', 'trips', 'booking'], 'knowledge_hash': 'a' * 64,
        'history': [], 'question': GOAL,
        'published_learning': {'source_kind': 'published_knowledge', 'product_version': 'v1',
            'legacy_contract': False, 'conditional_structure': 'matched',
            'knowledge': {'entries': [], 'unknowns': ['当前资料未覆盖其他能力。']}},
        'current_observation': {'page': 'booking', 'title': '预订与票据',
            'items': ['报价', '锁价', '付款', '票据'], 'product_version': 'v1', 'source_kind': 'client_observed'}}
    return {'messages': [{'role': 'system', 'content': SYSTEM_V6}, {'role': 'user', 'content': json.dumps(
        context, ensure_ascii=False, separators=(',', ':'))}], 'tools': [deepcopy(NAVIGATION_TOOL)],
        'max_tokens': 512, 'json_output': False}


def changed(source, change):
    value = deepcopy(source)
    context = json.loads(value['messages'][1]['content'])
    change(context)
    value['messages'][1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    return value


def scope(group='B1'):
    source = initial_source()
    binding = FairTaskBinding('DEV27-05', 'p29-test-task', group, 'personal_alice',
        tuple(str(uuid4()) for _ in range(2)), 'kb-fixed', GOAL)
    return DynamicTaskScope(binding, source), source


def test_review_preserves_precise_initial_and_runtime_uuid_mapping_without_future_hashes():
    gate, source = scope('B0')
    other, _ = scope('B0')
    review = gate.review()
    assert review['scope_hash'] == other.scope_hash
    assert review['host_binding_hash'] != other.review()['host_binding_hash']
    assert review['initial_actual_hash'] == fingerprint(fair_input(source, 'B0'))
    assert review['initial_source_bytes'] == input_size_bytes(source)
    assert review['future_exact_hashes'] is None and review['approved'] is False
    review['initial_source']['max_tokens'] = 1024
    assert gate.review()['initial_source']['max_tokens'] == 512


def test_round_freeze_binds_raw_reviewed_actual_and_consumes_once():
    gate, source = scope('B0')
    frozen = gate.freeze_round(source, fair_input(source, 'B0'), round_index=1, physical_index=1)
    detached = frozen.payload
    detached['max_tokens'] = 999
    assert frozen.payload['max_tokens'] == 512
    assert gate.authorize_actual(frozen.payload) is frozen
    with pytest.raises(ModelUnavailable):
        gate.authorize_actual(frozen.payload)
    def next_round(context):
        context['question'] = FEEDBACK
        context['history'] = [{'source_kind': 'user_reported', 'text': GOAL},
                              {'source_kind': 'model_candidate', 'text': '请先核对原网页。'}]
        state = context['current_state']
        lock = deepcopy(state['delegated_trips'][0]['data']['quote'])
        lock.update(ref='lock', effective_status='held', price_basis='frozen', coupon_allocation='held')
        state['delegated_trips'][0]['data']['lock'] = lock
        state['shared_coupon'].update(status='held', attribution='delegated_trip', trip_ref='trip_1')
        observed = datetime.fromisoformat(state['observed_at']) + timedelta(seconds=2)
        state.update(observed_at=observed.isoformat(), valid_until=(observed + timedelta(seconds=30)).isoformat())
    source2 = changed(source, next_round)
    second = gate.freeze_round(source2, fair_input(source2, 'B0'), round_index=2, physical_index=1,
                               feedback={FEEDBACK})
    assert second.source_hash == fingerprint(source2)
    assert second.reviewed_source_hash != second.source_hash
    assert len(json.loads(second.reviewed_source['messages'][1]['content'])['history']) == 1
    assert 'current_state' not in json.loads(second.payload['messages'][1]['content'])
    with pytest.raises(ModelUnavailable):
        gate.freeze_round(source2, second.payload, round_index=3, physical_index=1, feedback={FEEDBACK})


@pytest.mark.parametrize('attack', ['new_private_field', 'identity', 'version', 'delegation', 'free_page_text',
                                  'unregistered_feedback', 'knowledge', 'price_text'])
def test_b0_full_source_rejects_hidden_expansion_even_after_transformation(attack):
    gate, source = scope('B0')
    frozen = gate.freeze_round(source, fair_input(source, 'B0'), round_index=1, physical_index=1)
    gate.authorize_actual(frozen.payload)
    def poison(context):
        context['question'] = FEEDBACK
        state = context['current_state']
        if attack == 'new_private_field': state['sql'] = 'SELECT secret FROM users'
        elif attack == 'identity': state['simulated_identity'] = 'administrator'
        elif attack == 'version': state['feature_config_version'] = 'shared-coupon-2'
        elif attack == 'delegation': state['delegated_trips'].reverse()
        elif attack == 'free_page_text': context['current_observation']['items'].append('password=opaque')
        elif attack == 'unregistered_feedback': context['history'] = [{'source_kind': 'user_reported', 'text': '换个目标'}]
        elif attack == 'knowledge': context['published_learning']['knowledge']['unknowns'].append('新知识')
        elif attack == 'price_text': state['delegated_trips'][0]['data']['quote']['amount_display'] = str(uuid4())
    value = changed(source, poison)
    with pytest.raises(ModelUnavailable):
        gate.freeze_round(value, fair_input(value, 'B0'), round_index=2, physical_index=1, feedback={FEEDBACK})


def test_first_round_matches_review_and_send_side_requires_exact_approved_transform():
    gate, source = scope()
    moved = changed(source, lambda context: context['current_state'].update(selected_ref='trip_2'))
    with pytest.raises(ModelUnavailable):
        gate.freeze_round(moved, fair_input(moved, 'B1'), round_index=1, physical_index=1)
    other = deepcopy(fair_input(source, 'B1'))
    other['max_tokens'] = True
    with pytest.raises(ModelUnavailable):
        gate.freeze_round(source, other, round_index=1, physical_index=1)
    with pytest.raises(ModelUnavailable):
        gate.freeze_round(source, fair_input(source, 'B1'), round_index=1, physical_index=2)
    frozen = gate.freeze_round(source, fair_input(source, 'B1'), round_index=1, physical_index=1)
    with pytest.raises(ModelUnavailable):
        gate.check_actual(changed(frozen.payload, lambda context: context.update(question='新问题')))


def test_raw_source_ceiling_is_enforced_before_b0_removal():
    gate, source = scope('B0')
    oversized = changed(source, lambda context: context.update(history=[{
        'source_kind': 'model_candidate', 'text': '历史' * 7000}]))
    assert input_size_bytes(fair_input(oversized, 'B0')) < 12288 < input_size_bytes(oversized)
    with pytest.raises(ModelUnavailable):
        gate.freeze_round(oversized, fair_input(oversized, 'B0'), round_index=1, physical_index=1)
