"""独立的、经过字段审查的 V1 个人使用资料；从不读取工程文档或评测答案。"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parent / "knowledge"
STEPS = ("trip.open", "profile.bind", "quote.request", "quote.lock", "payment.confirm",
         "booking.refresh", "payment.reconcile", "trip.revise")
Step = Literal["trip.open", "profile.bind", "quote.request", "quote.lock", "payment.confirm",
               "booking.refresh", "payment.reconcile", "trip.revise"]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def published_text(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 600 or
            re.search(r"(?i)https?://|file:|Bearer\s|sk-[a-z0-9]|password|api[_ -]?key|"
                      r"-----BEGIN|\bSELECT\b|\bINSERT\b|\bUPDATE\b|源码|测试答案|密钥|数据库凭证|"
                      r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{22,}|[\x00-\x08\x0b-\x1f]", value)):
        raise ValueError("发布资料含本次范围外内容")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Node(Strict):
    id: str
    revision: int = Field(ge=1)
    step_id: Step
    anchor_id: Step
    purpose: str = Field(min_length=1, max_length=600)
    preconditions: list[str] = Field(max_length=8)
    expected_change: str = Field(min_length=1, max_length=600)
    recovery: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def review(self):
        if self.id != "knowledge:" + self.step_id or self.anchor_id != self.step_id:
            raise ValueError("未知资料/入口关系")
        for text in (self.purpose, *self.preconditions, self.expected_change, self.recovery):
            published_text(text)
        return self


class Knowledge(Strict):
    product_id: Literal["travel-lab"]
    product_version: Literal["v1"]
    knowledge_version: Literal["personal-guide-1"]
    revision: int = Field(ge=1)
    source_kind: Literal["documented"]
    source: Literal["product-published:travel-lab/v1/personal-guide"]
    disclosure_scope: Literal["public-personal-procedure"]
    supported_scope: str = Field(min_length=1, max_length=600)
    nodes: list[Node] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def review(self):
        published_text(self.supported_scope)
        if {node.step_id for node in self.nodes} != set(STEPS):
            raise ValueError("资料节点重复或缺失")
        return self


class Anchor(Strict):
    id: Step
    path: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def review(self):
        published_text(self.path)
        return self


class Anchors(Strict):
    product_id: Literal["travel-lab"]
    product_version: Literal["v1"]
    mapping_version: Literal["personal-anchors-1"]
    revision: int = Field(ge=1)
    source_kind: Literal["documented"]
    source: Literal["product-published:travel-lab/v1/personal-anchors"]
    anchors: dict[Step, Anchor]

    @model_validator(mode="after")
    def review(self):
        if set(self.anchors) != set(STEPS) or any(key != val.id for key, val in self.anchors.items()):
            raise ValueError("未知入口或入口缺失")
        return self


@dataclass(frozen=True)
class KnowledgeBundle:
    knowledge: dict
    anchors: dict
    fingerprint: str


def validate_knowledge(knowledge, anchors):
    """所有叶字段严格验证；修订未变而内容变化仍会改变私有内容指纹。"""
    reviewed = Knowledge.model_validate(knowledge).model_dump(mode="json")
    mapping = Anchors.model_validate(anchors).model_dump(mode="json")
    return KnowledgeBundle(reviewed, mapping, fingerprint({"knowledge": reviewed, "anchors": mapping}))


def load_knowledge():
    """每轮重新读固定文件，不接受用户/模型路径，不缓存跨资料更新的旧内容。"""
    def read(name):
        payload = (ROOT / name).read_bytes()
        if len(payload) > 16384:
            raise ValueError("发布资料超长")
        # 重复JSON键也失败关闭，不能用最后一个键悄悄取代已审查字段。
        def distinct(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("发布资料重复字段")
                result[key] = value
            return result
        return json.loads(payload.decode("utf-8"), object_pairs_hook=distinct)
    return validate_knowledge(read("personal-v1.json"), read("personal-anchors-v1.json"))
