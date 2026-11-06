"""P30只核新入口批身份、路径和批准门，不重复运输四任务。"""
import json

import pytest

from scripts import round29_task as task


@pytest.fixture
def configured(monkeypatch, tmp_path):
    for name in ('ROUND', 'PACKAGE', 'LOCAL', 'ENTRY'):
        monkeypatch.setattr(task, name, getattr(task, name))
    task.configure_round30()
    monkeypatch.setattr(task, 'LOCAL', tmp_path)
    return tmp_path


def test_q20_paths_do_not_select_old_q19_grant(configured):
    from services.guide.budget import BudgetLedger
    ledger = BudgetLedger(configured / 'usage.json')
    assert task.batch_name() == 'Q20'
    assert task.grant_paths(ledger) == (ledger.q20_path, ledger.q20_stop_path)
    assert task.PACKAGE == 'Q20-P30-single-step-task'
    assert task.ENTRY == task.ROOT / 'scripts/round30_task.py'
    with pytest.raises(RuntimeError, match='outside_workspace'):
        task.checked_directory(str(task.ROOT / '.local/d3-29/review'))


@pytest.mark.parametrize('change', [
    {'package': 'Q19-P29-continuous-task'}, {'approved': False},
    {'source_kind': 'generated_plan'}, {'source_reply': ''},
    {'review_manifest_sha256': '0' * 64},
])
def test_new_grant_requires_explicit_matching_q20_decision(configured, monkeypatch, change):
    import services.guide.budget as budget
    def unexpected_ledger(*args, **kwargs):
        pytest.fail('invalid decision must stop before opening a ledger')
    monkeypatch.setattr(budget, 'BudgetLedger', unexpected_ledger)
    (configured / 'review-manifest.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(task, 'checked_review', lambda directory: {})
    decision = {'package': task.PACKAGE, 'approved': True,
        'source_kind': 'explicit_user_reply', 'source_reply': '批准Q20',
        'review_manifest_sha256': task.sha(configured / 'review-manifest.json')} | change
    path = configured / 'decision.json'
    path.write_text(json.dumps(decision), encoding='utf-8')
    with pytest.raises(RuntimeError, match='explicit_matching_decision'):
        task.init_grant(configured, path)
