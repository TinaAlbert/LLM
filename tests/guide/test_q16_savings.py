"""仅从当前有效报价计算带预估措辞的省钱差额；不外发答案。"""
from copy import deepcopy
import pytest
from services.guide.assistant import validate_reply
from test_shared_prompt21 import facts

def test_estimated_savings_is_derived_from_observed_discounts_only():
    state=facts()
    text='比较两种顺序，按当前报价预估约省140模拟元；仍要核对库存、期限和资格。'
    assert validate_reply(text,state,prompt_version='assistant-6')==text

@pytest.mark.parametrize('text', ['预计约省141模拟元。','已经省140模拟元。',
    '当前行程价格为140模拟元。','trip_1的报价是140模拟元。'])
def test_unknown_or_factual_amounts_not_exempted(text):
    with pytest.raises(ValueError,match='amount_unverified'):
        validate_reply(text,facts(),prompt_version='assistant-6')

@pytest.mark.parametrize('change',['expired','held','single'])
def test_difference_requires_available_coupon_and_two_valid_quotes(change):
    state=facts()
    if change=='expired':state['delegated_trips'][0]['data']['quote']['valid']=False
    elif change=='held':state['shared_coupon']['status']='held'
    else:state['delegated_trips']=state['delegated_trips'][:1]
    with pytest.raises(ValueError,match='amount_unverified'):
        validate_reply('预计约省140模拟元。',state,prompt_version='assistant-6')
