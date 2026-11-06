"""已核查同Flash升级保留历史知识原件；未知修订仍关闭。"""
import json
import pytest
from services.guide.model import MODEL_REVISION
from services.guide.releases import Releases,builtin

@pytest.mark.parametrize('revision',[MODEL_REVISION,'DeepSeek-V4.1-Flash'])
def test_known_build_revision_readable_without_rewriting(revision):
    value=builtin();value['model_revision']=revision
    before=json.dumps(value,sort_keys=True)
    Releases.compatible(value)
    assert json.dumps(value,sort_keys=True)==before

@pytest.mark.parametrize('field,value', [('model_revision','unknown-future-model'),('model','other-service'),
    ('tool_version','shell-1'),('product_version','v2')])
def test_unknown_model_or_contract_still_refused(field,value):
    release=builtin();release[field]=value
    with pytest.raises(ValueError,match='release_incompatible'):Releases.compatible(release)
