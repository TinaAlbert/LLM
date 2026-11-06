"""P30-02新入口不能消费已关闭Q20或其旧决定。"""
import json
import pytest

from scripts import round29_task as task


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round30_recovery()
    monkeypatch.setattr(task, 'LOCAL', tmp_path)
    return tmp_path


def test_recovery_uses_distinct_q21_paths_and_version(recovery):
    from services.guide.budget import BudgetLedger
    ledger = BudgetLedger(recovery / 'usage.json')
    assert task.grant_paths(ledger) == (ledger.q21_path, ledger.q21_stop_path)
    assert task.batch_name() == 'Q21' and task.shared_prompt_version() == 'assistant-8'
    assert task.ENTRY == task.ROOT / 'scripts/round30_recovery.py'
    with pytest.raises(RuntimeError, match='outside_workspace'):
        task.checked_directory(str(task.ROOT / '.local/d3-30/review'))


@pytest.mark.parametrize('package', ['Q19-P29-continuous-task', 'Q20-P30-single-step-task'])
def test_old_explicit_approval_cannot_create_recovery_grant(recovery, monkeypatch, package):
    import services.guide.budget as budget
    def unexpected_ledger(*args, **kwargs):
        pytest.fail('old package must stop before opening ledger')
    monkeypatch.setattr(budget, 'BudgetLedger', unexpected_ledger)
    monkeypatch.setattr(task, 'checked_review', lambda directory: {})
    manifest = recovery / 'review-manifest.json'
    manifest.write_text('{}', encoding='utf-8')
    decision = {'package': package, 'approved': True, 'source_kind': 'explicit_user_reply',
        'source_reply': '批准旧审查包', 'review_manifest_sha256': task.sha(manifest)}
    path = recovery / 'decision.json'
    path.write_text(json.dumps(decision), encoding='utf-8')
    with pytest.raises(RuntimeError, match='explicit_matching_decision'):
        task.init_grant(recovery, path)
