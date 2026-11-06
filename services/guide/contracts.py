"""D3 只接收有限个人指引，不提供业务写入或模型工具。"""

import json
import re
import unicodedata
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

STEPS = ("trip.open", "profile.bind", "quote.request", "quote.lock", "payment.confirm",
         "booking.refresh", "payment.reconcile", "trip.revise")
Step = Literal["trip.open", "profile.bind", "quote.request", "quote.lock", "payment.confirm",
               "booking.refresh", "payment.reconcile", "trip.revise"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def safe_text(text):
    # 内容检测只收窄本次合成任务；身份/字段/目的地仍由程序固定。
    if re.search(r"(?i)https?://|file:|Bearer\s|sk-[a-z0-9]|password|api[_ -]?key|"
                 r"-----BEGIN|\bSELECT\b|\bINSERT\b|\bUPDATE\b|源码|测试答案|密钥|数据库凭证|"
                 r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{22,}|[\x00-\x08\x0b-\x1f]", text):
        raise ValueError("输入超出本次合成任务范围")
    return text


class Next(Strict):
    action: Literal["next"]
    trip_id: UUID
    goal: str = Field(min_length=1, max_length=600)
    feedback: str = Field(default="", max_length=600)
    detail: Literal["brief", "detailed"] = "brief"
    request_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    delegation_ref: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def text_boundary(self):
        safe_text(self.goal)
        safe_text(self.feedback)
        return self


class Cancel(Strict):
    action: Literal["cancel"]
    delegation_ref: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")


class Candidate(Strict):
    status: Literal["next_step", "need_clarification", "unknown", "unsupported", "blocked",
                    "pending", "completion_candidate"]
    step_id: Step | None
    anchor_id: str | None = Field(max_length=80)
    explanation: str = Field(min_length=1, max_length=600)
    preconditions: list[str] = Field(max_length=8)
    expected_change: str = Field(max_length=300)
    references: list[str] = Field(min_length=1, max_length=12)
    unknowns: list[str] = Field(max_length=8)
    amount_ref: Literal["quote.amount_minor", "lock.amount_minor", "payment.amount_minor"] | None

    @model_validator(mode="after")
    def bounded_text(self):
        if self.status == "next_step" and (self.step_id is None or self.anchor_id is None):
            raise ValueError("下一步缺入口")
        if self.step_id is None and self.anchor_id is not None:
            raise ValueError("入口无步骤")
        for value in [self.explanation, self.expected_change, *self.preconditions, *self.unknowns]:
            if len(value) > 600:
                raise ValueError("文本过长")
            normalized = unicodedata.normalize("NFKC", value)
            safe_text(normalized)
            # 金额必须引用可信字段；不接受模型自行生成金额数字。
            if re.search(r"[0-9零〇二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟¥￥]|"
                         r"(?:金额|应付|支付|付款|冻结)(?:为|是|：|:|共|总计|约)?\s*[一两]|[一两](?:元|块|圆|角|分)", normalized):
                raise ValueError("金额须用事实引用")
            if (any(c.isnumeric() and c not in "一两" for c in normalized) or
                    re.search(r"(?i)\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
                              r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
                              r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)\b", normalized)):
                raise ValueError("金额须用事实引用")
        return self


def parse_candidate(payload):
    if not isinstance(payload, dict):
        raise ValueError("候选须为对象")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 16384:
        raise ValueError("候选过长")
    return Candidate.model_validate(payload)


def validate_evidence(candidate, public, knowledge, anchors):
    """语义事实只作拒绝校验，不替模型选择正确下一步。"""
    refs = {node["id"] for node in knowledge["nodes"]} | {"observation:current"}
    if set(candidate.references) - refs:
        raise ValueError("伪证据引用")
    if candidate.step_id is not None:
        anchor = anchors["anchors"].get(candidate.step_id)
        if anchor is None or candidate.anchor_id != anchor["id"]:
            raise ValueError("未知入口")
    data = public["data"]
    payment, ticket = data.get("payment"), data.get("ticket")
    if candidate.step_id == "payment.confirm":
        lock = data.get("lock")
        if ((payment and payment["status"] != "failed") or
                not lock or not lock["valid"] or lock["trip_revision"] != data["trip"]["revision"] or
                candidate.amount_ref != "lock.amount_minor" or
                public["payment_profile_state"] != "usable" or public["flight_state"] != "available"):
            raise ValueError("付款前提未成立")
    if payment and payment["status"] in {"processing", "unknown", "succeeded"}:
        if candidate.step_id in {"profile.bind", "quote.request", "quote.lock", "payment.confirm", "trip.revise"}:
            raise ValueError("原付款须核对")
    if candidate.status == "completion_candidate":
        if (not ticket or ticket["status"] != "valid" or ticket["trip_revision"] != data["trip"]["revision"] or
                not payment or payment["status"] != "succeeded" or
                not data.get("ticket_task") or data["ticket_task"]["status"] != "succeeded"):
            raise ValueError("没有当前有效票据")
    if candidate.amount_ref:
        kind = candidate.amount_ref.split(".")[0]
        row = data.get(kind)
        if not row or (kind in {"quote", "lock"} and
                       (not row["valid"] or row["trip_revision"] != data["trip"]["revision"])):
            raise ValueError("金额证据无效")
    return candidate
