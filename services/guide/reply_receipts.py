"""仅可信宿主保存答复拒绝分类；收据不进入产品答复、历史或模型输入。"""
import json
from pathlib import Path
import re

from .knowledge import fingerprint


# 固定分类防止任意异常文本（例如内部路径或凭证）进入新增诊断。
_REPLY_REASONS = frozenset({
    "reply_invalid", "completion_unverified", "payment_unverified", "amount_unverified",
    "self_approval_forbidden", "approval_applicability_unverified",
    "shared_total_conditions_missing", "shared_optimum_unverified", "shared_completion_unverified",
    "consumed_coupon_not_reusable", "coupon_availability_unverified",
    "shared_amount_attribution_unverified", "business_time_conflict", "price_validity_unverified",
    "price_validity_conflict", "page_capability_unverified", "page_visibility_unverified",
    "single_action_required", "action_fact_ambiguous",
    "action_format_invalid", "action_duplicate_field", "action_fields_invalid",
    "action_version_invalid", "action_kind_invalid", "action_entry_invalid",
    "action_params_invalid", "action_target_invalid", "action_tool_mismatch",
    "action_text_invalid", "action_explanation_route",
    "action_evidence_stale", "action_evidence_required", "action_evidence_invalid",
    "action_fact_conflict", "action_scope_unsupported", "action_gap_required", "action_gap_invalid",
    "action_precondition_unverified", "action_payment_or_ticket_exists", "action_wait_unverified",
})


def record_reply_rejection(report_path, candidate, error, *, stage):
    """仅在已执行的答复守卫拒绝点调用；不把观察/运输等其他503归为内容失败。

    关联实际 infer 报告，因此 B0 收据使用删状态后的实际 input_hash。写失败仍保持
    原拒绝，不提供降级答复；没有 inference 报告的离线替身不伪造已发送关联。
    """
    if report_path is None:
        return None
    reason = str(error)
    if stage == "navigation_validation":
        reason = "navigation_unconfirmed" if reason == "navigation_unconfirmed" else "guard_unclassified"
    elif stage == "reply_validation":
        if reason == "输入超出本次合成任务范围":
            reason = "reply_content_out_of_scope"
        elif reason not in _REPLY_REASONS:
            reason = "guard_unclassified"
    else:
        raise ValueError("reply_rejection_stage_invalid")
    path = Path(report_path)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        input_hash = report["input_hash"]
        if (type(input_hash) is not str or not re.fullmatch(r"[a-f0-9]{64}", input_hash)
                or fingerprint(report["input"]) != input_hash
                or report.get("candidate") != candidate):
            return None
        receipt = {"kind": "assistant_reply_rejection", "schema_version": 1,
                   "stage": stage, "reason": reason, "input_hash": input_hash,
                   "candidate_hash": fingerprint(candidate), "inference_report": path.name}
        # 子目录不干扰既有模型报告 *.json 枚举；不修改或倒填原推理报告。
        directory = path.parent / "rejections"
        directory.mkdir(exist_ok=True)
        target = directory / path.name
        with target.open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2)
        return target
    except (OSError, ValueError, TypeError, KeyError):
        return None
