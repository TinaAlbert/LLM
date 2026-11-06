"""P31定向只读旧件/候选/必要输入检查；不启动服务、不发送模型。"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / ".local/d3-31"

def read(p): return json.loads(p.read_text(encoding="utf-8"))
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p, v): p.write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")

def snapshot():
    selected = []
    for name in ("d3-29", "d3-30", "d3-30-recovery"):
        base = ROOT / ".local" / name
        for rel in ("decision.json", "live-1/model-01.json", "live-1/runtime-inputs.json",
                    "live-1/turn-01.json", "live-1/content-diagnostic.json",
                    "live-1/offline-delivery-diagnostic.json", "review-final/review-manifest.json",
                    "live-review-20261020/review-manifest.json"):
            if (base / rel).is_file(): selected.append(base / rel)
        selected.extend((base / "live-1/model-runs/rejections").glob("*.json"))
    selected.extend((ROOT / ".local").glob("guide-usage.json*"))
    assert not (OUT / "old-evidence-before.json").exists(), "snapshot_exists"
    save(OUT / "old-evidence-before.json", {p.relative_to(ROOT).as_posix(): sha(p) for p in selected})
    print(json.dumps({"snapshotted_files": len(selected), "model_calls": 0}))

def restore(shown):
    value = deepcopy(shown)
    for trip in value["delegated_trips"]:
        for name in ("quote", "lock", "payment"):
            price = trip["data"].get(name)
            if not price: continue
            for display, minor in (("amount_display", "amount_minor"),
                ("base_amount_display", "base_amount_minor"), ("coupon_discount_display", "coupon_discount_minor")):
                if display in price:
                    text = price.pop(display)
                    assert text is not None, "diagnostic_initial_price_required"
                    price[minor] = int(Decimal(text.split()[0]) * 100)
            price["currency"] = "DEMO"
    coupon = value["shared_coupon"]
    coupon["cap_minor"] = int(Decimal(coupon.pop("cap_display").split()[0]) * 100)
    coupon["currency"] = "DEMO"
    return value

def check():
    from services.guide.assistant import SYSTEM_V9, display_state, validate_reply
    from services.guide.action_contract import validate_action_contract
    from services.guide.experiment import input_size_bytes
    from services.guide.fair_runtime import fair_input
    from services.business.assistant_projection import SHARED_DOCUMENT_V2
    before = read(OUT / "old-evidence-before.json")
    assert all(sha(ROOT / p) == expected for p, expected in before.items()), "old_evidence_changed"
    archive = read(OUT / "preservation-before.json")
    assert all(sha(OUT / "frozen-q21" / p) == expected for p, expected in archive["implementation_sha256"].items())
    assert sha(OUT / "frozen-q21/review-manifest.json") == archive["source_manifest_sha256"]
    assert all(not p.exists() for p in (OUT/"decision.json", ROOT/".local/guide-usage.json.q22.json",
               ROOT/".local/guide-usage.json.q22-stopped.json")), "unapproved_permission_exists"
    diagnostics = []
    for name, version, expected in (("d3-29", "assistant-6", "amount_unverified"),
        ("d3-30", "assistant-7", None), ("d3-30-recovery", "assistant-8", "single_action_required")):
        base = ROOT / ".local" / name / "live-1"
        row = read(base/"runtime-inputs.json")[0]
        source = row["source_input"]
        context = json.loads(source["messages"][-1]["content"])
        state = restore(context["current_state"])
        assert display_state(state, version) == context["current_state"], "roundtrip_failed"
        candidate = read(base/"model-01.json")["candidate"]
        reason = None
        try: validate_reply(candidate["content"], state, context.get("current_observation"), prompt_version=version)
        except ValueError as exc: reason = str(exc)
        assert reason == expected, (name, reason)
        try: validate_action_contract(candidate["content"], state, context["allowed_pages"], candidate["tool_calls"])
        except ValueError as exc: format_reason = str(exc)
        else: raise AssertionError("old_freetext_accepted_as_structure")
        assert format_reason == "action_format_invalid"
        diagnostics.append({"source": name, "old_semantic_reason": reason, "new_format_reason": format_reason,
                            "candidate_modified": False, "original_formal_result_unchanged": True})
    rows = read(ROOT/".local/d3-28/tasks-final/DEV27-05-B1/fair-inputs.json")
    stored = rows[-1].get("source_input", rows[-1]["input"])
    seed = read(OUT/"review/initial-1-source.json") if (OUT/"review/initial-1-source.json").exists() else deepcopy(stored)
    context = json.loads(seed["messages"][-1]["content"])
    old_context = json.loads(stored["messages"][-1]["content"])
    for field in ("current_state", "current_observation", "current_page"):
        context[field] = deepcopy(old_context[field])
    context["current_state"]["documented"] = deepcopy(SHARED_DOCUMENT_V2)
    context["history"] = []
    questions = {
        "accepted_lock": "我尝试了trip_1的锁价，页面返回请求已受理。",
        "query_succeeded": "我查询了trip_1，页面显示付款状态succeeded，票据状态succeeded。",
        "error_32ascii_estimate": "我尝试了trip_1的锁价，页面返回请求被拒绝，错误为" + "x"*32 + "。"}
    sizes = {}
    for label, question in questions.items():
        source = deepcopy(seed); source["messages"][0]["content"] = SYSTEM_V9
        context["question"] = question
        source["messages"][-1]["content"] = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        sizes[label] = {group: input_size_bytes(fair_input(source, group, prompt_version="assistant-9"))
                        for group in ("B0", "B1")}
        assert all(size <= 12288 for size in sizes[label].values()), (label, sizes[label])
    result = {"kind": "p31_zero_model_targeted_review", "old_files_preserved": len(before),
        "q21_archive_verified": len(archive["implementation_sha256"]), "old_candidates": diagnostics,
        "required_final_state_fixed_feedback_messages_tools_bytes": sizes,
        "limit": 12288, "model_calls": 0, "http_calls": 0, "database_calls": 0,
        "limitation": "Stored final state with empty optional history and fixed feedback estimates is not proof for all future paths; dynamic source/actual byte gates remain mandatory. Error length 32 is an estimate.",
        "real_task_completion": False, "new_permission_created": False}
    save(OUT / "independent-check.json", result)
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"snapshot", "check"}: raise SystemExit("snapshot|check")
    snapshot() if sys.argv[1] == "snapshot" else check()
