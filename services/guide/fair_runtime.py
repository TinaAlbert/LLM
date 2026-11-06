"""P28 当前 Assistant 的本地公平运输；不创建许可、不允许真实模型。

任务、组别与反馈只由可信宿主固定，生产 Chat 不增加模式参数。B0 的删减
发生在同一冻结助手版本组装输入之后；原身份重核、状态时效及交付守卫照常执行。
本模块不持有业务答案、SQL、worker 或业务写入工具。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
import time
from uuid import UUID

from fastapi import HTTPException

from .assistant import SHARED_PROMPT_VERSIONS, corporate_export_scope, prompt_system
from .contracts import safe_text
from .experiment import NAVIGATION_TOOL
from .knowledge import fingerprint
from .model import ModelUnavailable


def _reject(reason, status=409):
    raise HTTPException(status, detail={"code": reason})


@dataclass(frozen=True)
class FairTaskBinding:
    """仅宿主持有；不进入模型 payload，也不接受客户端覆盖。"""

    case_id: str
    task_ref: str
    group: str
    actor_id: str
    delegated_trip_ids: tuple[str, ...]
    release_version: str
    goal: str
    condition: str = "published-flow"
    max_rounds: int = 9
    task_deadline_seconds: int = 300

    def __post_init__(self):
        if (self.case_id not in {"DEV27-05", "DEV27-08"}
                or self.group not in {"B0", "B1", "guide"}
                or self.condition != "published-flow"
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", self.task_ref)
                or not self.actor_id or not self.release_version
                or not isinstance(self.delegated_trip_ids, tuple)
                or len(self.delegated_trip_ids) not in {0, 2}
                or len(set(self.delegated_trip_ids)) != len(self.delegated_trip_ids)
                or (self.case_id == "DEV27-05" and len(self.delegated_trip_ids) != 2)
                or type(self.max_rounds) is not int or not 1 <= self.max_rounds <= 9
                or type(self.task_deadline_seconds) is not int or not 1 <= self.task_deadline_seconds <= 300
                or not isinstance(self.goal, str) or not 1 <= len(self.goal) <= 400):
            raise ValueError("fair_binding_invalid")
        for value in self.delegated_trip_ids:
            if str(UUID(value)) != value:
                raise ValueError("fair_binding_invalid")
        safe_text(self.goal)


def fair_input(payload, group, *, prompt_version="assistant-6"):
    """只变换获授权 Assistant 输入；B1 与 guide 为同一实现标签。"""
    if group not in {"B0", "B1", "guide"} or prompt_version not in SHARED_PROMPT_VERSIONS:
        raise ValueError("fair_group_invalid")
    value = deepcopy(payload)
    if (set(value) != {"messages", "tools", "max_tokens", "json_output"}
            or len(value["messages"]) != 2
            or value["messages"][0] != {"role": "system", "content": prompt_system(prompt_version)}
            or set(value["messages"][1]) != {"role", "content"}
            or value["messages"][1]["role"] != "user"
            or value["tools"] != [NAVIGATION_TOOL]
            or type(value["max_tokens"]) is not int or value["max_tokens"] != 512
            or value["json_output"] is not False):
        raise ValueError("fair_assistant_contract_changed")
    context = json.loads(value["messages"][-1]["content"])
    expected = {"goal", "current_page", "current_state", "current_observation", "allowed_pages",
                "knowledge_hash", "history", "question", "published_learning"}
    if set(context) != expected or type(context["history"]) is not list:
        raise ValueError("fair_context_contract_changed")
    # 各组统一历史策略，避免 B0 从旧候选或浏览器语义回执绕回实时资料。
    context["history"] = [row for row in context["history"]
                          if type(row) is dict and set(row) == {"source_kind", "text"}
                          and row["source_kind"] == "user_reported"]
    if group == "B0":
        context.pop("current_state")
        context.pop("current_observation")
    value["messages"][-1]["content"] = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return value


class FairRuntime:
    """本地工程替身专用，不可当未来动态外发的批准门。"""

    def __init__(self, assistant, binding):
        if (getattr(assistant, "fair_runtime", None) is not None or assistant.sessions
                or assistant.external_model is not False
                or assistant.releases.active()["version"] != binding.release_version
                or assistant.releases.active()["prompt_version"] not in SHARED_PROMPT_VERSIONS):
            raise ValueError("fair_local_assistant_required")
        self.assistant, self.binding = assistant, binding
        self.prompt_version = assistant.releases.active()["prompt_version"]
        self._chat, self._infer, self._who = assistant.chat, assistant.infer, assistant.who
        self.feedback = set()
        self.pending_feedback = set()
        self.session_id = None
        self.active_question = None
        self.failed = False
        self.inputs = []
        self.accepted_rounds = 0
        self.task_started_at = None

    def _local_only(self):
        # B0 缺 current_state 会跳过旧 shared-export 检查，因此每轮明确禁真实适配器。
        # 未来真实批须同时授权原 scope、实际变换和动态状态，不复用旧两题许可。
        if self.assistant.external_model is not False:
            raise ModelUnavailable("fair_runtime_real_model_forbidden")
        active = self.assistant.releases.active()
        if (active["version"] != self.binding.release_version
                or active["prompt_version"] != self.prompt_version):
            _reject("fair_release_changed")

    def allow_feedback(self, text):
        """宿主在真实动作回执后登记文本；调用者负责证明其只描述可见尝试。"""
        if not isinstance(text, str) or not 1 <= len(text) <= 400:
            raise ValueError("fair_feedback_invalid")
        safe_text(text)
        if re.search(r"(?i)DEV27-|task_ref|evaluator|worker|reset|SQL|参考分支|正确答案|"
                     r"最优答案|标准答案|独立判据|隐藏状态", text):
            raise ValueError("fair_feedback_internal_data")
        # 中间件可以反复读取同一宿主文件；旧回执登记保持幂等，不补发新轮额度。
        if text not in self.feedback:
            self.feedback.add(text)
            self.pending_feedback.add(text)

    def _task_budget(self):
        if self.accepted_rounds >= self.binding.max_rounds:
            self.failed = True
            _reject("fair_round_limit")
        if (self.task_started_at is not None
                and time.monotonic() - self.task_started_at >= self.binding.task_deadline_seconds):
            self.failed = True
            _reject("fair_task_deadline")

    async def who(self, request, deadline):
        who = await self._who(request, deadline)
        if who.actor is None or str(who.actor) != self.binding.actor_id:
            _reject("not_visible", 404)
        return who

    async def chat(self, body, request):
        self._local_only()
        if self.failed:
            _reject("fair_task_stopped")
        self._task_budget()
        expected = self.binding.delegated_trip_ids
        if (tuple(str(value) for value in body.delegated_trip_ids) != expected
                or (expected and str(body.trip_id) not in expected)
                or (not expected and body.trip_id is not None)):
            _reject("not_visible", 404)
        if self.active_question is not None:
            _reject("assistant_busy")
        if self.session_id is None:
            if body.session_id is not None or body.message != self.binding.goal:
                _reject("fair_initial_question_required")
        elif body.session_id != self.session_id or body.message not in self.pending_feedback:
            _reject("fair_visible_feedback_required")
        else:
            self.pending_feedback.remove(body.message)
        self.active_question = body.message
        try:
            result = await self._chat(body, request)
            self._task_budget()
            self.session_id = result["session_id"]
            self.accepted_rounds += 1
            return result
        except BaseException:
            # 接受后失败/取消即停；不能静默新建会话重试。
            self.failed = True
            raise
        finally:
            self.active_question = None

    async def infer(self, payload, kind, version):
        self._local_only()
        self._task_budget()
        if (kind != "chat" or version != self.binding.release_version
                or self.active_question is None):
            raise ModelUnavailable("fair_chat_only")
        context = json.loads(payload["messages"][-1]["content"])
        if context.get("goal") != self.binding.goal or context.get("question") != self.active_question:
            raise ValueError("fair_question_changed")
        allowed = self.feedback | {self.binding.goal}
        for row in context.get("history", []):
            if row.get("source_kind") == "user_reported" and row.get("text") not in allowed:
                raise ValueError("fair_feedback_changed")
        transformed = fair_input(payload, self.binding.group, prompt_version=self.prompt_version)
        if self.task_started_at is None:
            # prepare/注册/授权前置停止不启动计时；首个有效输入至最后交付共用总截止。
            self.task_started_at = time.monotonic()
        self.inputs.append({"task_ref": self.binding.task_ref, "case_id": self.binding.case_id,
            "group": self.binding.group, "condition": self.binding.condition,
            "source_scope_hash": corporate_export_scope(payload),
            "source_input_hash": fingerprint(payload), "input_hash": fingerprint(transformed),
            "input": deepcopy(transformed), "real_model": False,
            "guide_equivalent_to_b1": True})
        return await self._infer(transformed, kind, version)


def install_fair_runtime(assistant, binding):
    """对 create_app 已注册的同一 Assistant 安装固定本地条件，不换 Coordinator。"""
    runtime = FairRuntime(assistant, binding)
    assistant.chat, assistant.infer, assistant.who = runtime.chat, runtime.infer, runtime.who
    assistant.fair_runtime = runtime
    return runtime
