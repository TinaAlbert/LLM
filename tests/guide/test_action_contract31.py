"""P31 严格结构、说明偷带路线和工具一致性；模型/HTTP/数据库均0。"""
from copy import deepcopy
import json

import pytest

from services.guide.action_contract import (
    ACTION_CONTRACT_VERSION, ACTION_ENTRIES, ACTION_PROMPT, action_digest,
    render_action_contract, validate_action_contract,
)

STATE = {"delegated_trips": [{"ref": "trip_1", "data": {}}, {"ref": "trip_2", "data": {}}],
         "selected_ref": "trip_2"}
PAGES = ["welcome", "profile", "trips", "booking"]


def candidate(kind="lock", target=None):
    return {"v": 1, "action": {"kind": kind,
        "target": target or ("trip_1" if kind in {"lock", "pay", "cancel", "query", "requote"} else "task"),
        "entry": ACTION_ENTRIES[kind], "params": {"page": "profile"} if kind == "navigate" else {}},
        "reason": "", "conditions": "", "check": ""}


def content(value):
    return json.dumps(value, ensure_ascii=False)


def validate(value, *, state=STATE, calls=()):
    return validate_action_contract(content(value), state, PAGES, calls)


def tool(page="profile"):
    return {"id": "synthetic-navigation", "name": "navigate", "arguments": {"page": page}}


@pytest.mark.parametrize("kind", ["lock", "pay", "cancel", "query", "requote",
                                   "wait", "clarify", "unknown", "end"])
def test_one_current_action_and_exact_ref_are_not_inferred_from_selected(kind):
    value = candidate(kind)
    original = deepcopy(value)
    assert validate(value) == original and value == original
    reply = render_action_contract(value)
    assert reply.count("当前动作：") == 1
    if kind in {"lock", "pay", "cancel", "query", "requote"}:
        assert "trip_1" in reply and "trip_2" not in reply
        assert "booking." not in reply
    assert ACTION_CONTRACT_VERSION == "single-action-8"


@pytest.mark.parametrize("field,text", [
    ("conditions", "锁价前确认报价仍有效。"),
    ("conditions", "若trip_1报价仍有效时才可锁价。"),
    ("reason", "trip_1尚未付款，预报价并非锁价金额。"),
    ("check", "本步锁价后预计生成锁价快照。"),
    ("check", "本步成功后预期生成锁价快照。"),
    ("check", "之后预期能看到实际金额与券归属。"),
    ("conditions", "报价、库存、期限和资格仍满足；不要重复付款。"),
    ("check", "trip_1付款状态仍待确认。"),
])
def test_current_conditions_expected_results_and_negative_constraints_are_legal(field, text):
    value = candidate()
    value[field] = text
    assert validate(value)[field] == text
    assert text in render_action_contract(value)


@pytest.mark.parametrize("field", ["reason", "conditions", "check"])
@pytest.mark.parametrize("text", [
    "若trip_1锁价成功，再在trip_2重新报价。",
    "本步成功后为trip_2锁价并付款。",
    "然后打开付款资料。",
    "trip_2重新报价。",
    "锁价，付款。",
    "请为trip_2付款。",
    "成功后查询trip_1付款状态。",
    "先锁价；再付款；最后取票。",
    "如果报价过期，请重新报价。",
    "接着绑定付款资料。",
    "请先查询付款状态，再打开其他页面。",
    "若本步成功，trip_2重新核价后决定是否锁价并付款。",
    "若锁价成功，则为trip_1付款。",
    "在结果返回时为trip_2付款。",
])
def test_every_explanation_field_rejects_followup_instructions(field, text):
    value = candidate()
    value[field] = text
    with pytest.raises(ValueError, match="action_explanation_route"):
        validate(value)


@pytest.mark.parametrize("text", [
    "另一行程需重新报价。", "另一个行程付款状态未知。",
    "另一个行程锁价仍有效。",
    "另一单金额600模拟元。", "前者已出票。", "其余行程可以付款。",
])
def test_ambiguous_other_object_explanation_never_defaults_to_current_target(text):
    value = candidate()
    value["reason"] = text
    with pytest.raises(ValueError, match="action_fact_ambiguous"):
        validate(value)


@pytest.mark.parametrize("raw,reason", [
    ("当前动作：为trip_1锁价。", "action_format_invalid"),
    ('```json\n{}\n```', "action_format_invalid"),
    ('[]', "action_fields_invalid"),
    ('{"v":1,"v":1}', "action_duplicate_field"),
    ('{"v":NaN}', "action_format_invalid"),
    ('{"v":Infinity}', "action_format_invalid"),
])
def test_malformed_and_free_text_candidates_are_not_repaired(raw, reason):
    with pytest.raises(ValueError, match=reason):
        validate_action_contract(raw, STATE, PAGES)


@pytest.mark.parametrize("mutate,reason", [
    (lambda v: v.update(v=True), "action_version_invalid"),
    (lambda v: v.update(v=2), "action_version_invalid"),
    (lambda v: v.update(extra="next"), "action_fields_invalid"),
    (lambda v: v.pop("check"), "action_fields_invalid"),
    (lambda v: v.update(action=[v["action"], v["action"]]), "action_fields_invalid"),
    (lambda v: v["action"].update(next="pay"), "action_fields_invalid"),
    (lambda v: v["action"].update(kind="approve"), "action_kind_invalid"),
    (lambda v: v["action"].update(entry="booking.pay"), "action_entry_invalid"),
    (lambda v: v["action"].update(params={"amount": 800}), "action_params_invalid"),
    (lambda v: v["action"].update(params=[]), "action_params_invalid"),
    (lambda v: v["action"].update(target="selected"), "action_target_invalid"),
    (lambda v: v["action"].update(target="trip_1 trip_2"), "action_target_invalid"),
    (lambda v: v["action"].update(target="trip_3"), "action_target_invalid"),
    (lambda v: v.update(conditions=["still valid"]), "action_text_invalid"),
    (lambda v: v.update(check=" " + v["check"]), "action_text_invalid"),
])
def test_unknown_missing_multiple_and_wrongly_typed_fields_fail_closed(mutate, reason):
    value = candidate()
    mutate(value)
    with pytest.raises(ValueError, match=reason):
        validate(value)


@pytest.mark.parametrize("state", [
    {}, {"selected_ref": "trip_1"},
    {"delegated_trips": [{"ref": "trip_2"}], "selected_ref": "trip_1"},
    {"delegated_trips": [{"ref": "trip_1"}, {"ref": "trip_1"}]},
])
def test_ref_must_exist_uniquely_in_current_explicit_delegation(state):
    with pytest.raises(ValueError, match="action_target_invalid"):
        validate(candidate(), state=state)


def test_navigation_has_exactly_one_matching_tool_and_no_claim_of_success():
    value = candidate("navigate")
    assert validate(value, calls=[tool()]) == value
    rendered = render_action_contract(value)
    assert "付款资料" in rendered and "等待浏览器回执" in rendered and "已打开" not in rendered
    for calls in ([], [tool("booking")], [tool(), tool()], [tool() | {"extra": True}],
                  [tool() | {"name": "pay"}], [tool() | {"id": ""}]):
        with pytest.raises(ValueError, match="action_tool_mismatch"):
            validate(value, calls=calls)
    with pytest.raises(ValueError, match="action_tool_mismatch"):
        validate(candidate("lock"), calls=[tool()])


@pytest.mark.parametrize("change", [
    {"target": "profile"}, {"params": {}}, {"params": {"page": "approvals"}},
    {"params": {"page": "profile", "target": "trip_1"}}, {"params": {"page": True}},
])
def test_navigation_does_not_guess_target_page_or_parameters(change):
    value = candidate("navigate")
    value["action"].update(change)
    with pytest.raises(ValueError):
        validate(value, calls=[tool()])


def test_digest_binds_structure_explanations_and_version_without_reinterpreting():
    value = candidate()
    first = action_digest(validate(value))
    assert first == action_digest(dict(reversed(list(value.items()))))
    for change in ("reason", "conditions", "check"):
        changed = deepcopy(value)
        changed[change] = "报价仍有效。"
        assert action_digest(validate(changed)) != first
    changed = deepcopy(value)
    changed["action"]["target"] = "trip_2"
    assert action_digest(validate(changed)) != first
    assert '"v":1' in ACTION_PROMPT and "booking.reconcile" in ACTION_PROMPT


def test_duplicate_nested_field_and_control_character_cannot_disguise_operations():
    raw = content(candidate()).replace('"kind": "lock"', '"kind": "lock", "kind": "pay"')
    with pytest.raises(ValueError, match="action_duplicate_field"):
        validate_action_contract(raw, STATE, PAGES)
    value = candidate()
    value["check"] = "然后付\u200b款。"
    with pytest.raises(ValueError, match="action_text_invalid"):
        validate(value)


@pytest.mark.parametrize("text", ["需为trip_2付款。", "必须为trip_2付款。", "需要在trip_2重新报价。", "应为trip_2付款。"])
def test_explicit_requirement_in_explanation_is_still_another_instruction(text):
    value = candidate()
    value["reason"] = text
    with pytest.raises(ValueError, match="action_explanation_route"):
        validate(value)

@pytest.mark.parametrize("text", ["不要重复付款。", "不要再为trip_1付款。", "无需再次付款。"])
def test_negative_current_constraints_are_not_instructions(text):
    value = candidate()
    value["conditions"] = text
    assert validate(value)["conditions"] == text
