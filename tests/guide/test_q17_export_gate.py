"""Q17精确外发许可只对显式路径生效；不打开默认入口或授予模型次数。"""
from copy import deepcopy
import pytest
from services.guide import assistant as module
from services.guide.assistant import authorize_shared_export, corporate_export_scope
from services.guide.model import ModelUnavailable
from test_q16_export import samples, write


def approval(values):
    return {'package': 'P24-Q17-two', 'approved': True, 'source_kind': 'explicit_user_reply',
            'scope_hashes': [corporate_export_scope(value) for value in values]}


def test_q17_explicit_two_scopes_do_not_enable_default_path(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    values = samples()
    default = tmp_path / '.local/d3-21/shared-export-approval.json'
    explicit = tmp_path / 'q17.json'
    write(default, approval(values))
    write(explicit, approval(values))
    for value in values:
        assert authorize_shared_export(value, explicit) is None
        with pytest.raises(ModelUnavailable, match='shared_export_not_approved'):
            authorize_shared_export(value)
    changed = deepcopy(values[0])
    changed['messages'][0]['content'] += '另一个未批准指令'
    with pytest.raises(ModelUnavailable, match='shared_export_not_approved'):
        authorize_shared_export(changed, explicit)


@pytest.mark.parametrize('change', ['source', 'package', 'approval', 'extra', 'duplicate'])
def test_q17_bad_approval_rejected(tmp_path, change):
    values = samples()
    document = approval(values)
    if change == 'source':
        document['source_kind'] = 'model_claim'
    elif change == 'package':
        document['package'] = 'P24-Q17-other'
    elif change == 'approval':
        document['approved'] = 1
    elif change == 'extra':
        document['extra'] = True
    else:
        document['scope_hashes'][1] = document['scope_hashes'][0]
    target = tmp_path / 'q17.json'
    write(target, document)
    with pytest.raises(ModelUnavailable, match='shared_export_not_approved'):
        authorize_shared_export(values[0], target)
