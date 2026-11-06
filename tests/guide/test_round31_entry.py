"""P31 唯一结构确认、旧行为保留和公平scope；纯内存、无服务。"""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from scripts import round29_task as task
from services.guide.action_contract import action_digest, render_action_contract


@pytest.fixture
def configured(monkeypatch, tmp_path):
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round31()
    monkeypatch.setattr(task, 'LOCAL', tmp_path)
    return tmp_path


def delivered(kind='query', target='trip_1', params=None):
    entries = {'lock': 'booking.lock', 'pay': 'booking.pay', 'cancel': 'booking.cancel_lock',
        'query': 'booking.reconcile', 'requote': 'booking.quote', 'navigate': 'navigate',
        'wait': 'task', 'clarify': 'task', 'unknown': 'task', 'end': 'task'}
    value = {'v': 1, 'action': {'kind': kind, 'target': target, 'entry': entries[kind],
        'params': {} if params is None else params}, 'reason': '当前状态需要核验。',
        'conditions': '仅依据当前可见资料。', 'check': '页面返回的状态。'}
    formal = {'session_id': 'formal-session', 'action_contract': value,
        'action_contract_version': 'single-action-8', 'action_sha256': action_digest(value),
        'reply': render_action_contract(value)}
    if kind == 'navigate':
        formal['tool'] = {'id': 'n1', 'name': 'navigate', 'arguments': params, 'nonce': 'host-nonce'}
    return formal


def confirmation(formal):
    kind, ref, quote = task.structured_action(formal)
    return {'task_ref': 'p31-test', 'turn': 1, 'formal_reply_sha256': task.digest(formal),
        'action_contract_version': formal['action_contract_version'], 'action_sha256': formal['action_sha256'],
        'quote': quote, 'action': kind, 'object': ref, 'actor': 'engineering_synthetic_user',
        'faithful': True, 'ambiguous': False, 'note': 'synthetic_fixture_only'}


def test_q22_uses_new_paths_prompt_and_entry(configured):
    from services.guide.budget import BudgetLedger
    ledger = BudgetLedger(configured / 'usage.json')
    assert task.ROUND == 31 and task.PACKAGE == 'Q22-P31-structured-task'
    assert task.grant_paths(ledger) == (ledger.q22_path, ledger.q22_stop_path)
    assert task.shared_prompt_version() == 'assistant-9'
    assert task.ENTRY == task.ROOT / 'scripts/round31_task.py'
    with pytest.raises(RuntimeError, match='outside_workspace'):
        task.checked_directory(str(task.ROOT / '.local/d3-30-recovery/review'))


@pytest.mark.parametrize('kind', ['lock', 'pay', 'cancel', 'query', 'requote'])
def test_business_confirmation_maps_only_structure_and_complete_host_first_line(configured, kind):
    formal = delivered(kind)
    value = confirmation(formal)
    assert value['quote'] == formal['reply'].splitlines()[0]
    assert task.validate_confirmation(value, formal, task_ref='p31-test', turn=1) == value
    with pytest.raises(RuntimeError, match='replay'):
        task.validate_confirmation(value, formal, task_ref='p31-test', turn=1, consumed=[task.digest(value)])


@pytest.mark.parametrize('change', [
    {'quote': '当前状态需要核验。'}, {'action': 'pay'}, {'object': 'trip_2'},
    {'action_contract_version': 'single-action-0'}, {'action_sha256': '0' * 64},
    {'formal_reply_sha256': '0' * 64}, {'turn': 2}, {'faithful': False}, {'ambiguous': True}])
def test_confirmation_cannot_select_explanation_change_target_or_drop_delivery_binding(configured, change):
    formal = delivered()
    with pytest.raises(RuntimeError):
        task.validate_confirmation(confirmation(formal) | change, formal, task_ref='p31-test', turn=1)


def test_structured_delivery_cannot_downgrade_to_legacy_or_change_rendering(configured):
    formal = delivered()
    value = confirmation(formal)
    del value['action_sha256']
    with pytest.raises(RuntimeError, match='fields'):
        task.validate_confirmation(value, formal, task_ref='p31-test', turn=1)
    changed = deepcopy(formal)
    changed['reply'] += '\n请为trip_2付款。'
    with pytest.raises(RuntimeError, match='presentation_mismatch'):
        task.structured_action(changed)
    with pytest.raises(RuntimeError, match='binding_required'):
        task.structured_action({'reply': '请为trip_1付款。'})


def test_navigation_uses_structured_page_and_still_requires_actual_receipt(configured):
    formal = delivered('navigate', 'task', {'page': 'booking'})
    value = confirmation(formal)
    assert value['object'] == 'booking'
    with pytest.raises(RuntimeError, match='actual_navigation_receipt_required'):
        task.validate_confirmation(value, formal, task_ref='p31-test', turn=1)
    value['navigation_receipt'] = {'source_kind': 'human_observed_browser'}
    assert task.validate_confirmation(value, formal, task_ref='p31-test', turn=1) == value
    formal['tool']['arguments'] = {'page': 'trips'}
    with pytest.raises(RuntimeError, match='delivery_invalid'):
        task.structured_action(formal)


@pytest.mark.parametrize('kind', ['wait', 'clarify', 'unknown', 'end'])
def test_nonbusiness_action_never_calls_business_client(configured, kind):
    formal = delivered(kind, 'task')
    value = confirmation(formal)
    task.validate_confirmation(value, formal, task_ref='p31-test', turn=1)
    result, text = task.execute_confirmed(None, {}, {}, value, {}, formal)
    assert result is None and text


@pytest.mark.parametrize('package', ['Q19-P29-continuous-task', 'Q20-P30-single-step-task', 'Q21-P30-validity-recovery'])
def test_closed_package_decision_cannot_create_q22_grant(configured, monkeypatch, package):
    import services.guide.budget as budget
    monkeypatch.setattr(budget, 'BudgetLedger', lambda *a, **k: pytest.fail('no ledger access for old decision'))
    monkeypatch.setattr(task, 'checked_review', lambda path: {})
    manifest = configured / 'review-manifest.json'
    manifest.write_text('{}', encoding='utf-8')
    decision = configured / 'decision.json'
    decision.write_text(json.dumps({'package': package, 'approved': True,
        'source_kind': 'explicit_user_reply', 'source_reply': '批准旧包',
        'review_manifest_sha256': task.sha(manifest)}), encoding='utf-8')
    with pytest.raises(RuntimeError, match='explicit_matching_decision'):
        task.init_grant(configured, decision)


@pytest.mark.parametrize('group', ['B0', 'B1'])
def test_assistant9_doc2_fair_source_and_actual_scope_are_both_bound(group):
    from test_task_scope import initial_source, changed, GOAL
    from services.business.assistant_projection import SHARED_DOCUMENT_V2
    from services.guide.assistant import prompt_system
    from services.guide.fair_runtime import FairTaskBinding, fair_input
    from services.guide.task_scope import DynamicTaskScope
    from services.guide.knowledge import fingerprint
    source = changed(initial_source(), lambda context: context['current_state'].update(
        documented=deepcopy(SHARED_DOCUMENT_V2)))
    source['messages'][0]['content'] = prompt_system('assistant-9')
    binding = FairTaskBinding('DEV27-05', 'p31-test', group, 'personal_alice',
        tuple(str(uuid4()) for _ in range(2)), 'kb-fixed', GOAL)
    gate = DynamicTaskScope(binding, source, prompt_version='assistant-9')
    actual = fair_input(source, group, prompt_version='assistant-9')
    frozen = gate.freeze_round(source, actual, round_index=1, physical_index=1)
    assert frozen.source_hash == fingerprint(source) and frozen.actual_hash == fingerprint(actual)
    assert gate.authorize_actual(actual) is frozen
    assert ('current_state' in json.loads(actual['messages'][1]['content'])) == (group == 'B1')
