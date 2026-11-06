"""20 企业提示、金额/批准与外发授权的纯离线单元检查；不连接PG或模型。"""
from copy import deepcopy
import json

import pytest

from services.guide.assistant import (
    SYSTEM_V4, SYSTEM_V5, authorize_corporate_export, corporate_export_scope,
    display_state, prompt_system, validate_reply,
)
from services.guide.model import ModelUnavailable
from services.guide.releases import Releases


def public_sources():
    profile = {'page': 'profile', 'title': '付款资料', 'items': ['公开功能说明'],
               'product_version': 'v1', 'source_kind': 'client_observed',
               'disclosure_scope': 'public-procedure'}
    trips = {'page': 'trips', 'title': '行程', 'items': ['公开功能说明'],
             'product_version': 'v1', 'source_kind': 'client_observed',
             'disclosure_scope': 'public-procedure'}
    result = [profile]
    for selected in (False, True):
        row = deepcopy(trips)
        row['semantics'] = {'schema': 'page-semantics-1', 'selected_trip': selected,
                            'zones': [{'id': 'booking.detail', 'present': selected, 'visible': selected}],
                            'controls': []}
        result.append(row)
    return result


def public_knowledge():
    return {'entries': [
        {'page': 'profile', 'summary': '可在付款资料页查看公开说明。', 'source_index': 0, 'quote': '付款资料'},
        {'page': 'trips', 'summary': '选择行程后预订详情在本次观察中可见。', 'source_index': 2, 'quote': '行程',
         'condition': {'selected_trip': True},
         'visibility_change': {'zone_id': 'booking.detail', 'before_source_index': 1}}],
        'unknowns': ['本次公开页面不能证明当前用户的业务资格、金额或付款成功。']}


def corporate_facts(*, applicability='stale'):
    return {'contract_version': 'assistant-corporate-1', 'source_kind': 'observed',
            'current_roles': ['employee'], 'selected_trip': True,
            'observed_at': '2026-10-16T09:00:00Z', 'business_time': '2026-09-27T00:00:00Z',
            'state_versions': {'trip': 2, 'membership': 1},
            'approval_handoff': 'assigned_department_approver',
            'assigned_approvals': [],
            'data': {'approval': {'status': 'approved', 'valid': True, 'applicability': applicability},
                     'payment': None, 'ticket': None},
            'unknowns': ['approver_name']}


def export_payload():
    context = {'question': '现在卡在哪里，已有批准还能用于本次付款吗？',
               'current_page': 'booking', 'current_state': corporate_facts(),
               'knowledge': public_knowledge(), 'allowed_pages': ['welcome', 'profile', 'trips', 'booking'],
               'history': []}
    return {'messages': [{'role': 'system', 'content': prompt_system('assistant-5')},
                         {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}],
            'tools': [], 'max_tokens': 512, 'json_output': False}


def change_context(payload, change):
    changed = deepcopy(payload)
    context = json.loads(changed['messages'][1]['content'])
    change(context)
    changed['messages'][1]['content'] = json.dumps(context, ensure_ascii=False)
    return changed


def approval_record(payload):
    return {'package': 'P20-corporate-one', 'approved': True,
            'source_kind': 'explicit_user_reply', 'scope_hash': corporate_export_scope(payload)}


def save_approval(path, payload, **changes):
    path.write_text(json.dumps({**approval_record(payload), **changes}, ensure_ascii=False), encoding='utf-8')


def test_new_prompt_preserves_four_and_separates_corporate_permissions():
    old = prompt_system('assistant-4')
    current = prompt_system('assistant-5')
    assert old == SYSTEM_V4 and current == SYSTEM_V5 and old != current
    assert 'published_learning' in old and 'approval_handoff' not in old
    for clause in ('知识管理员不等于业务管理员', 'approval_handoff', '姓名未知',
                   'budget_selection', '不表示预算余额充足', 'approval.valid', 'applicability=current',
                   'applicability=stale/unavailable', 'payment_in_progress/used_for_payment',
                   'assigned_approvals', '空列表不证明其他申请不存在', '未取得申请人的付款或票据权限'):
        assert clause in current


def test_same_checked_knowledge_can_publish_five_and_rollback_four_without_mutation(tmp_path):
    store = Releases('test', tmp_path / 'releases.json')
    old = store.save_candidate(public_knowledge(), public_sources(), prompt_version='assistant-4')
    store.publish(old['version'])
    old_saved = deepcopy(store.read()['candidates'][old['version']])
    upgraded = store.save_candidate(old['knowledge'], old['observations'], prompt_version='assistant-5')
    assert upgraded['version'] != old['version']
    assert upgraded['knowledge'] == old['knowledge']
    assert upgraded['observations'] == old['observations']
    before_review = store.path.read_bytes()
    review = store.review(upgraded['version'])
    assert review['prompt_version'] == 'assistant-5' and review['publishable']
    assert review['legacy_contract'] is False
    assert review['validation']['conditional_structure'] == 'matched'
    assert review['validation']['truth'] == 'requires_admin_review'
    assert store.path.read_bytes() == before_review
    store.publish(upgraded['version'])
    assert store.active()['prompt_version'] == 'assistant-5'
    assert store.read()['candidates'][old['version']] == old_saved
    assert store.rollback() == old['version']
    assert store.active()['prompt_version'] == 'assistant-4'
    assert store.read()['candidates'][old['version']] == old_saved
    assert store.rollback() == upgraded['version']


def test_assigned_amount_is_formatted_as_simulated_yuan_without_changing_input():
    facts = {'assigned_approvals': [{'id': 'synthetic-approval', 'amount_minor': 100000,
                                   'currency': 'DEMO', 'status': 'pending'}], 'data': {}}
    original = deepcopy(facts)
    shown = display_state(facts, 'assistant-5')
    assert shown['assigned_approvals'] == [{'id': 'synthetic-approval', 'status': 'pending',
                                            'amount_display': '1000.00 模拟元'}]
    assert facts == original
    text = '当前指派待办金额为1000.00 模拟元。'
    assert validate_reply(text, facts, prompt_version='assistant-5') == text
    with pytest.raises(ValueError, match='amount_unverified'):
        validate_reply('当前指派待办金额为100000元。', facts, prompt_version='assistant-5')


@pytest.mark.parametrize('applicability', ['stale', 'unavailable', 'used_for_payment'])
@pytest.mark.parametrize('text', ['已有批准可用于当前付款。', '旧批准仍然有效，可以直接付款。'])
def test_old_approval_cannot_authorize_current_payment(applicability, text):
    with pytest.raises(ValueError, match='approval_applicability_unverified'):
        validate_reply(text, corporate_facts(applicability=applicability), prompt_version='assistant-5')


@pytest.mark.parametrize('applicability', ['stale', 'unavailable', 'used_for_payment'])
def test_approval_uncertainty_can_be_explained_without_claiming_current_permission(applicability):
    text = '旧批准不能用于当前付款，应先刷新原申请和付款状态。'
    assert validate_reply(text, corporate_facts(applicability=applicability), prompt_version='assistant-5') == text


def test_current_approval_allows_fact_statement_but_never_self_approval():
    facts = corporate_facts(applicability='current')
    text = '已有批准可用于当前付款，实际提交时仍应由原页面重新校验。'
    assert validate_reply(text, facts, prompt_version='assistant-5') == text
    with pytest.raises(ValueError, match='self_approval_forbidden'):
        validate_reply('你可以自行审批。', facts, prompt_version='assistant-5')


def test_corporate_export_requires_record_but_personal_flow_keeps_existing_gate(tmp_path):
    missing = tmp_path / 'missing-approval.json'
    with pytest.raises(ModelUnavailable, match='corporate_export_not_approved'):
        authorize_corporate_export(export_payload(), missing)
    personal = change_context(export_payload(), lambda context: context.update(
        current_state={'source_kind': 'observed', 'selected_trip': False}))
    assert authorize_corporate_export(personal, missing) is None
    assert not missing.exists()


def test_explicit_record_for_exact_preview_is_accepted_without_modifying_payload(tmp_path):
    payload = export_payload()
    before = deepcopy(payload)
    path = tmp_path / 'approval.json'
    save_approval(path, payload)
    assert authorize_corporate_export(payload, path) is None
    assert payload == before


@pytest.mark.parametrize('changed_field', ['question', 'roles', 'fact', 'knowledge', 'system', 'limit'])
def test_approved_scope_cannot_be_reused_for_different_semantic_input(tmp_path, changed_field):
    original = export_payload()
    path = tmp_path / 'approval.json'
    save_approval(path, original)
    if changed_field == 'question':
        modified = change_context(original, lambda context: context.update(question='我可以自己审批吗？'))
    elif changed_field == 'roles':
        modified = change_context(original, lambda context: context['current_state'].update(current_roles=['approver']))
    elif changed_field == 'fact':
        modified = change_context(original, lambda context: context['current_state']['data']['approval'].update(applicability='current'))
    elif changed_field == 'knowledge':
        modified = change_context(original, lambda context: context['knowledge']['entries'][0].update(summary='另一份公开知识。'))
    else:
        modified = deepcopy(original)
        if changed_field == 'system':
            modified['messages'][0]['content'] = prompt_system('assistant-4')
        else:
            modified['max_tokens'] = 1024
    assert corporate_export_scope(modified) != corporate_export_scope(original)
    with pytest.raises(ModelUnavailable, match='corporate_export_not_approved'):
        authorize_corporate_export(modified, path)


def test_capture_timestamp_refresh_retains_exact_semantic_scope(tmp_path):
    original = export_payload()
    path = tmp_path / 'approval.json'
    save_approval(path, original)
    refreshed = change_context(original, lambda context: context['current_state'].update(
        observed_at='2026-10-16T09:01:00Z'))
    assert refreshed != original
    assert corporate_export_scope(refreshed) == corporate_export_scope(original)
    assert authorize_corporate_export(refreshed, path) is None


@pytest.mark.parametrize('change', [
    {'approved': False}, {'package': 'other-package'}, {'source_kind': 'generated_plan'},
    {'scope_hash': '0' * 64}, {'extra': 'unknown'},
])
def test_record_without_explicit_matching_package_and_scope_is_rejected(tmp_path, change):
    payload = export_payload()
    path = tmp_path / 'approval.json'
    save_approval(path, payload, **change)
    with pytest.raises(ModelUnavailable, match='corporate_export_not_approved'):
        authorize_corporate_export(payload, path)
