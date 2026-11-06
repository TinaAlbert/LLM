"""19 条件知识发布的离线检查；夹具不代表真实模型学习或真实业务状态。"""
import pytest

from services.guide.knowledge import fingerprint
from services.guide.model import MODEL, MODEL_REVISION
from services.guide.releases import Releases, validate_generated


def source(page, selected=None):
    value = {"page": page, "title": "付款资料" if page == "profile" else "行程",
             "items": ["公开功能说明"], "product_version": "v1",
             "source_kind": "client_observed", "disclosure_scope": "public-procedure"}
    if selected is not None:
        value["semantics"] = {"schema": "page-semantics-1", "selected_trip": selected,
                              "zones": [{"id": "booking.detail", "present": selected, "visible": selected}],
                              "controls": []}
    return value


def sources():
    return [source("profile"), source("trips", False), source("trips", True)]


def knowledge(*, conditional=True):
    value = {"entries": [
        {"page": "profile", "summary": "公开说明付款资料的入口。", "source_index": 0, "quote": "付款资料"},
        {"page": "trips", "summary": "行程页展示公开功能说明。", "source_index": 2 if conditional else 1,
         "quote": "行程"}], "unknowns": ["页面可见性不能证明当前用户的付款资格或业务成功。"]}
    if conditional:
        value["entries"][1].update(summary="在已观察页面中，选择行程后预订详情变为可见。",
                                   condition={"selected_trip": True},
                                   visibility_change={"zone_id": "booking.detail", "before_source_index": 1})
    return value


def legacy_candidate(store, version):
    """保留旧契约的历史原件形状，不通过新候选入口降级契约。"""
    data = knowledge(conditional=False)
    data["entries"][1].update(source_index=2, summary="选择行程后预订详情变为可见。")
    release = {"model": MODEL, "model_revision": MODEL_REVISION,
               "prompt_version": version, "tool_version": "navigate-1", "product_version": "v1",
               "knowledge": data, "knowledge_hash": fingerprint(data), "observations": sources(),
               "source_kind": "client_observed", "disclosure_scope": "public-procedure",
               "validation": {"structure": "passed", "citations": "matched",
                              "truth": "requires_admin_review", "weights_modified": False}}
    release["version"] = "kb-" + fingerprint(release)[:20]
    release["fingerprint"] = fingerprint(release)
    state = store.read()
    state["candidates"][release["version"]] = release
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.write(state)
    return release


def test_new_candidate_has_checked_change_sources_and_review(tmp_path):
    store = Releases("test", tmp_path / "releases.json")
    release = store.save_candidate(knowledge(), sources())
    review = store.review(release["version"])
    assert release["prompt_version"] == "assistant-4"
    assert review["validation"]["conditional_structure"] == "matched"
    assert review["validation"]["truth"] == "requires_admin_review"
    assert review["publishable"] and not review["legacy_contract"]
    assert review["coverage"] == {"pages": ["profile", "trips"],
                                  "conditions": [{"page": "trips", "selected_trip": True,
                                                  "zone_id": "booking.detail"}],
                                  "missing_pages": [], "missing_conditions": []}
    assert review["sources"][1]["selected_trip"] is False
    assert review["sources"][1]["zones"][0]["visible"] is False
    assert review["sources"][2]["zones"][0]["visible"] is True
    assert set(review["sources"][0]) == {"index", "page", "title", "items", "product_version",
                                         "selected_trip", "zones", "controls"}
    assert store.active()["version"] == "builtin-v1"


def test_review_preserves_filtered_control_availability_without_changing_candidate(tmp_path):
    store = Releases("test", tmp_path / "releases.json")
    observations = sources()
    for index, enabled in ((1, False), (2, True)):
        observations[index]["semantics"]["controls"] = [
            {"id": "nav.booking", "present": True, "visible": True, "enabled": enabled}]
    release = store.save_candidate(knowledge(), observations)
    original = store.path.read_bytes()
    review = store.review(release["version"])
    assert review["sources"][0]["controls"] == []
    assert review["sources"][1]["controls"] == [
        {"id": "nav.booking", "present": True, "visible": True, "enabled": False}]
    assert review["sources"][2]["controls"] == [
        {"id": "nav.booking", "present": True, "visible": True, "enabled": True}]
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("summary", ["选择行程后会出现详情。", "选择之前区域不可见，之后变为可见。",
                                    "预订入口变为可见。", "未选择行程时详情不可见。",
                                    "After selection the detail becomes visible."])
def test_change_claim_without_structure_cannot_be_new_knowledge(summary):
    value = knowledge(conditional=False)
    value["entries"][1]["summary"] = summary
    with pytest.raises(ValueError, match="knowledge_change_structure_required"):
        validate_generated(value, [source("profile"), source("trips")], strict=True)


def test_actual_source_pair_cannot_silently_be_reduced_to_plain_summaries(tmp_path):
    store = Releases("test", tmp_path / "releases.json")
    with pytest.raises(ValueError, match="knowledge_change_structure_required"):
        store.save_candidate(knowledge(conditional=False), sources())
    assert not store.path.exists()


@pytest.mark.parametrize("case", ["condition_only", "change_only", "unknowns_empty", "source_private",
                                  "source_identity", "source_version", "other_page", "reverse_sources",
                                  "missing_zone", "already_visible", "wrong_condition", "quote_not_in_source"])
def test_new_candidate_rejects_missing_or_unmatched_evidence(case, tmp_path):
    data, observations = knowledge(), sources()
    entry = data["entries"][1]
    if case == "condition_only": entry.pop("visibility_change")
    if case == "change_only": entry.pop("condition")
    if case == "unknowns_empty": data["unknowns"] = []
    if case == "source_private": observations[2]["disclosure_scope"] = "current-session"
    if case == "source_identity": observations[2]["actor"] = "other-user"
    if case == "source_version": observations[2]["product_version"] = "v2"
    if case == "other_page": entry["visibility_change"]["before_source_index"] = 0
    if case == "reverse_sources":
        observations[1], observations[2] = observations[2], observations[1]
        entry["source_index"] = 1
        entry["visibility_change"]["before_source_index"] = 2
    if case == "missing_zone": observations[1]["semantics"]["zones"] = []
    if case == "already_visible": observations[1]["semantics"]["zones"][0].update(present=True, visible=True)
    if case == "wrong_condition": entry["condition"]["selected_trip"] = False
    if case == "quote_not_in_source": entry["quote"] = "未观察的业务结果"
    store = Releases("test", tmp_path / "releases.json")
    with pytest.raises(ValueError):
        store.save_candidate(data, observations)
    assert not store.path.exists()


@pytest.mark.parametrize("prompt_version", ["assistant-1", "assistant-2", "assistant-3"])
def test_legacy_natural_language_candidates_are_readable_and_explicitly_reversible(prompt_version, tmp_path):
    store = Releases("test", tmp_path / "releases.json")
    old = legacy_candidate(store, prompt_version)
    store.publish(old["version"])
    new = store.save_candidate(knowledge(), sources())
    store.publish(new["version"])
    before = store.path.read_bytes()
    review = store.review(old["version"])
    assert review["legacy_contract"] and not review["publishable"]
    assert review["coverage"]["missing_conditions"] == [
        {"page": "trips", "selected_trip": True, "zone_id": "booking.detail"}]
    with pytest.raises(ValueError, match="knowledge_condition_coverage_regression"):
        store.publish(old["version"])
    assert store.path.read_bytes() == before
    assert store.rollback() == old["version"]
    assert store.active()["prompt_version"] == prompt_version
    assert store.rollback() == new["version"]


def test_new_contract_is_rechecked_on_read(tmp_path):
    store = Releases("test", tmp_path / "releases.json")
    legacy_candidate(store, "assistant-4")
    with pytest.raises(ValueError, match="knowledge_change_structure_required"):
        store.read()


def test_review_missing_page_matches_publish_guard(tmp_path):
    store = Releases("test", tmp_path / "releases.json")
    old = store.save_candidate(knowledge(conditional=False), [source("profile"), source("trips")])
    store.publish(old["version"])
    data = knowledge(conditional=False)
    data["entries"][1]["page"] = "welcome"
    observation = source("welcome")
    new = store.save_candidate(data, [source("profile"), observation])
    assert store.review(new["version"])["coverage"]["missing_pages"] == ["trips"]
    with pytest.raises(ValueError, match="knowledge_coverage_regression"):
        store.publish(new["version"])


def test_review_does_not_accept_unknown_candidate(tmp_path):
    store = Releases("test", tmp_path / "releases.json")
    with pytest.raises(ValueError, match="candidate_not_found"):
        store.review("kb-" + "0" * 20)
