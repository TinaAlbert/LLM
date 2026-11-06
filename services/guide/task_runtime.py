"""P29 专用持续任务接点；公平变换之前与实际发送输入必须分别绑定。

不解释自然语言、不选择业务动作；人工确认和原API执行由宿主入口处理。
"""
from copy import deepcopy
import json
import time

from .fair_runtime import FairRuntime, fair_input, _reject
from .knowledge import fingerprint
from .model import ModelUnavailable


class TaskRuntime(FairRuntime):
    def __init__(self, assistant, binding, *, scope=None, real_model=False):
        super().__init__(assistant, binding)
        if type(real_model) is not bool or (real_model and scope is None):
            raise ValueError("task_dynamic_scope_required")
        self.scope, self.real_model = scope, real_model
        self.pending_input = None

    def _local_only(self):
        if self.assistant.external_model is not self.real_model:
            raise ModelUnavailable("task_model_mode_changed")
        active = self.assistant.releases.active()
        if (active["version"] != self.binding.release_version
                or active["prompt_version"] != self.prompt_version):
            _reject("fair_release_changed")
        if self.real_model and self.assistant.dynamic_export_authorizer != self.authorize_export:
            raise ModelUnavailable("task_dynamic_export_gate_missing")

    def authorize_export(self, actual):
        """原 infer 的外发门；源资料已经过 freeze_round，删状态不能绕过它。"""
        if (self.pending_input is None or self.scope is None
                or actual != self.pending_input.payload):
            raise ModelUnavailable("task_export_input_changed")
        self.scope.check_actual(actual)

    async def infer(self, payload, kind, version):
        self._local_only()
        self._task_budget()
        if kind != "chat" or version != self.binding.release_version or self.active_question is None:
            raise ModelUnavailable("fair_chat_only")
        context = json.loads(payload["messages"][-1]["content"])
        if context.get("goal") != self.binding.goal or context.get("question") != self.active_question:
            raise ValueError("fair_question_changed")
        allowed = self.feedback | {self.binding.goal}
        if any(row.get("source_kind") == "user_reported" and row.get("text") not in allowed
               for row in context.get("history", [])):
            raise ValueError("fair_feedback_changed")
        actual = fair_input(payload, self.binding.group, prompt_version=self.prompt_version)
        round_index = self.accepted_rounds + 1
        physical_index = 1 + sum(row["round_index"] == round_index for row in self.inputs)
        if self.scope is not None:
            self.pending_input = self.scope.freeze_round(payload, actual, round_index=round_index,
                physical_index=physical_index, feedback=self.feedback)
        if self.task_started_at is None:
            self.task_started_at = time.monotonic()
        self.inputs.append({"task_ref": self.binding.task_ref, "case_id": self.binding.case_id,
            "group": self.binding.group, "condition": self.binding.condition,
            "round_index": round_index, "physical_index": physical_index,
            "source_input_hash": fingerprint(payload), "input_hash": fingerprint(actual),
            "reviewed_source_hash": (self.pending_input.reviewed_source_hash
                if self.pending_input is not None else fingerprint(fair_input(payload, "B1", prompt_version=self.prompt_version))),
            "source_input": deepcopy(payload), "input": deepcopy(actual),
            "real_model": self.real_model, "guide_equivalent_to_b1": True})
        try:
            return await self._infer(actual, kind, version)
        finally:
            self.pending_input = None


def install_task_runtime(assistant, binding, *, scope=None, real_model=False):
    runtime = TaskRuntime(assistant, binding, scope=scope, real_model=real_model)
    assistant.chat, assistant.infer, assistant.who = runtime.chat, runtime.infer, runtime.who
    assistant.fair_runtime = runtime
    assistant.dynamic_export_authorizer = runtime.authorize_export if real_model else None
    assistant.external_model = real_model
    return runtime
