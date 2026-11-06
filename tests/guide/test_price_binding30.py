"""P30-02 价格断言对象绑定；模型/HTTP/PG不参与。"""
from copy import deepcopy

import pytest

from services.guide.assistant import validate_reply
from test_assistant import time_facts


def state():
    value = deepcopy(time_facts())
    value['data']['lock'] = None
    return value


@pytest.mark.parametrize('text', [
    '锁价前确认报价仍有效（业务时间2026-09-27T00:00:00Z，报价到期2026-09-27T00:05:00Z）。',
    '锁价前确认“报价”仍有效。',
    '锁价前确认\"估算价\"当前有效。',
])
def test_quote_validity_does_not_claim_that_an_uncreated_lock_is_valid(text):
    assert validate_reply(text, state(), prompt_version='assistant-3') == text


@pytest.mark.parametrize('text', [
    '锁价前确认报价仍有效。',
    '报价和锁价仍有效。',
    '“报价”与“锁价”当前有效。',
])
def test_invalid_quote_cannot_be_hidden_by_timing_or_coordination(text):
    value = state()
    value['data']['quote'].update(valid=False, effective_status='expired')
    value['data']['lock'] = {'valid': True, 'effective_status': 'held'}
    with pytest.raises(ValueError, match='price_validity_conflict'):
        validate_reply(text, value, prompt_version='assistant-3')


@pytest.mark.parametrize('text', [
    '锁价当前有效。',
    '报价前确认锁价仍有效。',
    '报价未过期且锁价仍有效。',
    '报价仍有效但锁价当前有效。',
    '报价和锁价仍有效。',
    '报价和锁价均仍有效。',
    '“报价”与“锁价”仍有效。',
])
def test_valid_quote_cannot_prove_a_missing_lock_is_valid(text):
    with pytest.raises(ValueError, match='price_validity_conflict'):
        validate_reply(text, state(), prompt_version='assistant-3')


@pytest.mark.parametrize('text', [
    '锁价已经过期但报价仍有效。',
    '报价仍有效但锁价已经过期。',
    '锁价已过期并非报价过期。',
])
def test_different_price_kinds_keep_their_own_observed_validity(text):
    value = state()
    value['data']['lock'] = {'valid': False, 'effective_status': 'expired'}
    assert validate_reply(text, value, prompt_version='assistant-3') == text


@pytest.mark.parametrize('text,reason', [
    ('并非报价过期但报价已过期。', 'business_time_conflict'),
    ('锁价前确认报价已过期。', 'business_time_conflict'),
    ('锁价已过期但报价和锁价都未过期。', 'price_validity_conflict'),
    ('锁价并非已经过期。', 'price_validity_conflict'),
    ('并非报价与锁价过期。', 'price_validity_conflict'),
])
def test_negation_or_other_price_cannot_exempt_a_conflicting_assertion(text, reason):
    value = state()
    value['data']['lock'] = {'valid': False, 'effective_status': 'expired'}
    with pytest.raises(ValueError, match=reason):
        validate_reply(text, value, prompt_version='assistant-3')
