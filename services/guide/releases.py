"""本机知识构建产物和助手组合发布；不修改模型权重或业务数据库。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import threading

from .knowledge import fingerprint, load_knowledge, published_text
from .model import MODEL, MODEL_REVISION, ROOT
from .page_observation import Observation, PAGE_ZONES

PROMPT_VERSION = "assistant-4"
TOOL_VERSION = "navigate-1"
PRODUCT_VERSION = "v1"
_locks = {}
_guard = threading.Lock()


def builtin():
    bundle = load_knowledge()
    return {"version": "builtin-v1", "model": MODEL, "model_revision": MODEL_REVISION,
            "prompt_version": "assistant-3", "tool_version": TOOL_VERSION,
            "product_version": PRODUCT_VERSION, "knowledge": bundle.knowledge,
            "knowledge_hash": bundle.fingerprint, "source_kind": "documented"}


def validate_generated(value, observations, *, strict=False):
    """校验引用与披露结构；不把结构校验当模型事实正确性证明。"""
    if type(value) is not dict or set(value) != {"entries", "unknowns"}:
        raise ValueError("knowledge_fields_invalid")
    entries, unknowns = value["entries"], value["unknowns"]
    if (type(entries) is not list or not 2 <= len(entries) <= 6 or
            type(unknowns) is not list or not 1 <= len(unknowns) <= 4):
        raise ValueError("knowledge_coverage_invalid")
    for text in unknowns:
        published_text(text)
    if strict:
        validate_sources(observations)
    covered = set()
    for entry in entries:
        required = {"page", "summary", "source_index", "quote"}
        if (type(entry) is not dict or not required <= set(entry) or
                set(entry) - required - {"condition", "visibility_change"} or
                type(entry["source_index"]) is not int or
                not 0 <= entry["source_index"] < len(observations)):
            raise ValueError("knowledge_source_invalid")
        source = observations[entry["source_index"]]
        if entry["page"] != source["page"]:
            raise ValueError("knowledge_source_invalid")
        published_text(entry["summary"])
        published_text(entry["quote"])
        if not any(entry["quote"] in text for text in [source["title"], *source["items"]]):
            raise ValueError("knowledge_quote_unverified")
        if strict and (claims_change(entry["summary"]) or "condition" in entry or "visibility_change" in entry):
            if not {"condition", "visibility_change"} <= set(entry):
                raise ValueError("knowledge_change_structure_required")
        if "condition" in entry or "visibility_change" in entry:
            validate_condition(entry, observations)
            if strict and entry["visibility_change"]["before_source_index"] >= entry["source_index"]:
                raise ValueError("knowledge_visibility_source_invalid")
        covered.add(entry["page"])
    if len(covered) < 2:
        raise ValueError("knowledge_coverage_invalid")
    if strict and observed_changes(observations) and not any("visibility_change" in e for e in entries):
        raise ValueError("knowledge_change_structure_required")
    return value


def claims_change(summary):
    """有限的文本防漏检查；结构配对仍是依据，不能证明自由文本语义正确。"""
    return bool(re.search(
        r"选择.{0,12}(?:之前|之后|前后|后)|选中.{0,12}(?:前|后)|未选|选前|选后|"
        r"(?:分区|区域|入口|按钮|详情|付款).{0,16}(?:变为|变得|出现|显露|解锁)|"
        r"(?:不可见|隐藏|不存在).{0,16}(?:可见|显示|出现)|"
        r"(?:before|after)\s+(?:selecting|selection)|becomes?\s+visible|false\s*(?:→|->)\s*true",
        summary, re.IGNORECASE))


def validate_sources(observations):
    """新构建仅保存公开观察；身份、时效和同一行程由可信采集会话核查。"""
    if type(observations) is not list or not 2 <= len(observations) <= 3:
        raise ValueError("knowledge_source_invalid")
    for source in observations:
        observed = public_observation(source)
        if observed.page not in {"welcome", "profile", "trips"}:
            raise ValueError("knowledge_source_invalid")
    if len({source["page"] for source in observations}) < 2:
        raise ValueError("knowledge_coverage_invalid")


def observed_changes(observations):
    """已采集三源确含可见变化时，不能把缺结构候选称为条件学习成功。"""
    for before in observations:
        old = before.get("semantics")
        if not old or old["selected_trip"]:
            continue
        for after in observations:
            new = after.get("semantics")
            if (not new or not new["selected_trip"] or before["page"] != after["page"] or
                    before["product_version"] != after["product_version"]):
                continue
            prior = {zone["id"]: zone for zone in old["zones"]}
            if any(zone["id"] in prior and not prior[zone["id"]]["visible"] and
                   zone["present"] and zone["visible"] for zone in new["zones"]):
                return True
    return False


def public_observation(source):
    if type(source) is not dict or set(source) - {
            "page", "title", "items", "product_version", "semantics", "source_kind", "disclosure_scope"}:
        raise ValueError("knowledge_condition_source_invalid")
    if (source.get("source_kind", "client_observed") != "client_observed" or
            source.get("disclosure_scope", "public-procedure") != "public-procedure"):
        raise ValueError("knowledge_condition_source_invalid")
    return Observation.model_validate({k: v for k, v in source.items()
                                       if k not in {"source_kind", "disclosure_scope"}})


def condition_observation(source):
    # 已知宿主来源标记不参与客户端观察模型，其余字段必须严格匹配。
    value = public_observation(source)
    if value.semantics is None:
        raise ValueError("knowledge_condition_unobserved")
    return value


def validate_condition(entry, observations):
    condition = entry.get("condition")
    if type(condition) is not dict or set(condition) != {"selected_trip"} or type(condition["selected_trip"]) is not bool:
        raise ValueError("knowledge_condition_invalid")
    current = condition_observation(observations[entry["source_index"]])
    if current.semantics.selected_trip != condition["selected_trip"]:
        raise ValueError("knowledge_condition_unobserved")
    if "visibility_change" not in entry:
        return
    change = entry["visibility_change"]
    if (type(change) is not dict or set(change) != {"zone_id", "before_source_index"} or
            type(change["before_source_index"]) is not int or
            not 0 <= change["before_source_index"] < len(observations) or
            change["before_source_index"] == entry["source_index"] or
            change["zone_id"] not in PAGE_ZONES[current.page]):
        raise ValueError("knowledge_visibility_source_invalid")
    before = condition_observation(observations[change["before_source_index"]])
    if (before.page != current.page or before.product_version != current.product_version or
            before.semantics.selected_trip or not current.semantics.selected_trip):
        raise ValueError("knowledge_visibility_condition_invalid")
    def zone(value):
        return next((z for z in value.semantics.zones if z.id == change["zone_id"]), None)
    old, new = zone(before), zone(current)
    if old is None or new is None or old.visible or not new.present or not new.visible:
        raise ValueError("knowledge_visibility_change_unobserved")


def condition_coverage(knowledge):
    """保护已发布的条件/分区证据覆盖；不比较自然语言含义。"""
    return {(entry["page"], entry["condition"]["selected_trip"],
             entry.get("visibility_change", {}).get("zone_id"))
            for entry in knowledge.get("entries", []) if "condition" in entry}


def coverage_difference(old, new):
    old_pages = {entry["page"] for entry in old.get("entries", [])}
    new_pages = {entry["page"] for entry in new.get("entries", [])}
    old_conditions, new_conditions = condition_coverage(old), condition_coverage(new)

    def conditions(rows):
        return [{"page": page, "selected_trip": selected, "zone_id": zone}
                for page, selected, zone in sorted(rows, key=lambda row: (row[0], row[1], row[2] or ""))]

    return {"pages": sorted(new_pages), "conditions": conditions(new_conditions),
            "missing_pages": sorted(old_pages - new_pages),
            "missing_conditions": conditions(old_conditions - new_conditions)}


class Releases:
    def __init__(self, workspace, path=None):
        self.path = Path(path) if path else ROOT / ".local" / f"assistant-releases-{workspace}.json"
        self.lock_path = self.path.with_suffix(".lock")
        with _guard:
            self.lock = _locks.setdefault(str(self.path.resolve()).casefold(), threading.Lock())

    @contextmanager
    def locked(self):
        # 线程及多宿主进程争用均不丢失发布；单个文件原子切换整个组合。
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as handle:
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self):
        if not self.path.exists():
            return {"schema": 1, "active": "builtin-v1", "previous": None, "candidates": {}}
        if self.path.stat().st_size > 262144:
            raise ValueError("release_invalid")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (type(value) is not dict or set(value) != {"schema", "active", "previous", "candidates"} or
                value["schema"] != 1 or type(value["candidates"]) is not dict or
                len(value["candidates"]) > 12):
            raise ValueError("release_invalid")
        for key, release in value["candidates"].items():
            if release.get("version") != key or release.get("fingerprint") != fingerprint(
                    {k: v for k, v in release.items() if k != "fingerprint"}):
                raise ValueError("release_invalid")
            self.compatible(release)
            validate_generated(release["knowledge"], release["observations"],
                               strict=release["prompt_version"] in {"assistant-4", "assistant-5", "assistant-6", "assistant-7", "assistant-8", "assistant-9", "assistant-10", "assistant-11", "assistant-12", "assistant-13", "assistant-14"})
        for key in (value["active"], value["previous"]):
            if key not in (None, "builtin-v1") and key not in value["candidates"]:
                raise ValueError("release_invalid")
        return value

    @staticmethod
    def compatible(release):
        # 历史知识记录构建时修订；同一Flash已核查升级不能使原件失去可读/回滚性。
        # 只接受当前及该次明确已知前版，未知版本仍拒绝；真实推理修订另行记录。
        expected = {"model": MODEL, "tool_version": TOOL_VERSION,
                    "product_version": PRODUCT_VERSION}
        if release.get("model_revision") not in {MODEL_REVISION, "DeepSeek-V4.1-Flash"}:
            raise ValueError("release_incompatible")
        if release.get("prompt_version") not in {"assistant-1", "assistant-2", "assistant-3", "assistant-4", "assistant-5", "assistant-6", "assistant-7", "assistant-8", "assistant-9", "assistant-10", "assistant-11", "assistant-12", "assistant-13", "assistant-14"}:
            raise ValueError("release_incompatible")
        if any(release.get(key) != val for key, val in expected.items()):
            raise ValueError("release_incompatible")

    def write(self, value):
        target = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with target.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(target, self.path)
        finally:
            if target.exists():
                target.unlink()

    def active(self):
        value = self.read()
        return builtin() if value["active"] == "builtin-v1" else value["candidates"][value["active"]]

    def save_candidate(self, knowledge, observations, *, prompt_version=PROMPT_VERSION):
        if prompt_version not in {'assistant-4', 'assistant-5', 'assistant-6', 'assistant-7', 'assistant-8', 'assistant-9', 'assistant-10', 'assistant-11', 'assistant-12', 'assistant-13', 'assistant-14'}:
            raise ValueError('release_incompatible')
        validate_generated(knowledge, observations, strict=True)
        release = {"model": MODEL, "model_revision": MODEL_REVISION,
                   "prompt_version": prompt_version, "tool_version": TOOL_VERSION,
                   "product_version": PRODUCT_VERSION, "knowledge": knowledge,
                   "knowledge_hash": fingerprint(knowledge), "observations": observations,
                   "source_kind": "client_observed", "disclosure_scope": "public-procedure",
                   "validation": {"structure": "passed", "citations": "matched",
                                  "conditional_structure": "matched" if condition_coverage(knowledge) else "not_present",
                                  "truth": "requires_admin_review", "weights_modified": False}}
        release["version"] = "kb-" + fingerprint(release)[:20]
        release["fingerprint"] = fingerprint(release)
        with self.locked():
            state = self.read()
            if len(state["candidates"]) >= 12 and release["version"] not in state["candidates"]:
                raise ValueError("candidate_limit")
            state["candidates"][release["version"]] = release
            self.write(state)
        return release

    def review(self, version):
        """管理员审查所需的来源和覆盖差异；不增加线上用户的私人观察。"""
        state = self.read()
        if version not in state["candidates"]:
            raise ValueError("candidate_not_found")
        release = state["candidates"][version]
        active = builtin() if state["active"] == "builtin-v1" else state["candidates"][state["active"]]
        coverage = coverage_difference(active["knowledge"], release["knowledge"])
        sources = []
        for index, source in enumerate(release["observations"]):
            semantics = source.get("semantics") or {}
            sources.append({"index": index, "page": source["page"], "title": source["title"],
                            "items": source["items"], "product_version": source["product_version"],
                            "selected_trip": semantics.get("selected_trip"),
                            "zones": semantics.get("zones", []),
                            "controls": semantics.get("controls", [])})
        return {"candidate_version": version, "active_version": state["active"],
                "prompt_version": release["prompt_version"],
                "legacy_contract": release["prompt_version"] not in {"assistant-4", "assistant-5", "assistant-6", "assistant-7", "assistant-8", "assistant-9", "assistant-10", "assistant-11", "assistant-12", "assistant-13", "assistant-14"},
                "knowledge": release["knowledge"], "validation": release["validation"],
                "sources": sources, "coverage": coverage,
                "publishable": not coverage["missing_pages"] and not coverage["missing_conditions"]}

    def publish(self, version):
        with self.locked():
            state = self.read()
            if version not in state["candidates"]:
                raise ValueError("candidate_not_found")
            if state["active"] != "builtin-v1":
                old = state["candidates"][state["active"]]["knowledge"]
                new = state["candidates"][version]["knowledge"]
                coverage = coverage_difference(old, new)
                if coverage["missing_pages"]:
                    raise ValueError("knowledge_coverage_regression")
                if coverage["missing_conditions"]:
                    raise ValueError("knowledge_condition_coverage_regression")
            if state["active"] != version:
                state["previous"], state["active"] = state["active"], version
                self.write(state)
        return version

    def rollback(self):
        with self.locked():
            state = self.read()
            if state["previous"] is None:
                raise ValueError("rollback_unavailable")
            state["active"], state["previous"] = state["previous"], state["active"]
            self.write(state)
        return state["active"]
