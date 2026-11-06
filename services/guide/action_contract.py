"""唯一动作契约：历史v1保留词法/事实守卫；V1作品v2用精确证据可信呈现。

仅验证模型提案，不执行业务或补计划；v2不接受自由说明，不重判旧运行。
"""
import hashlib
from decimal import Decimal
import json
import re
import unicodedata

from .contracts import safe_text


ACTION_CONTRACT_VERSION = "single-action-8"
ACTION_ENTRIES = {
    "lock": "booking.lock", "pay": "booking.pay", "cancel": "booking.cancel_lock",
    "query": "booking.reconcile", "requote": "booking.quote", "navigate": "navigate",
    "wait": "task", "clarify": "task", "unknown": "task", "end": "task",
}
BUSINESS_KINDS = frozenset({"lock", "pay", "cancel", "query", "requote"})
ACTION_PROMPT = """仅输出一个JSON对象，无围栏或额外文字：
{"v":1,"action":{"kind":"...","target":"...","entry":"...","params":{}},"reason":"当前原因","conditions":"本步前提","check":"本步预期/核对"}。
字段全必填且不得增加；后三个是短字符串，可为空，仅说明本步，不能夹带后续指令或顺序。
kind/entry固定配对：lock/booking.lock,pay/booking.pay,cancel/booking.cancel_lock,query/booking.reconcile,requote/booking.quote；这些target必须是本轮明确匿名trip_1或trip_2，params={}，对象不明勿猜。
navigate的target=task,entry=navigate,params={"page":"允许页面"}，同时提出唯一同页navigate工具；其他动作不能带工具。
wait/clarify/unknown/end的target=task,entry=task,params={}。缺资料用clarify或unknown，不从历史猜事实。说明可写当前必要条件和本步预期；所有行程事实写明确本轮ref，跨对象事实分句，不用另一单等代称，不安排后续操作。"""

_TOP_FIELDS = {"v", "action", "reason", "conditions", "check"}
_ACTION_FIELDS = {"kind", "target", "entry", "params"}
_TEXT_FIELDS = ("reason", "conditions", "check")
_REF = re.compile(r"(?<![A-Za-z0-9_])trip_[0-9]+(?![A-Za-z0-9_])")
_OPERATIONS = {
    "requote": r"重新(?:报|核)价|重报价|获取报价|申请报价",
    "lock": r"锁价|锁定(?:报价|价格)",
    "pay": r"付款|支付",
    "cancel": r"取消(?:锁价)?|释放(?:锁|优惠券|共享券|券|占用)",
    "query": r"查询|刷新|(?:查看|核对|确认).{0,6}(?:行程|付款状态|支付状态|票据)",
    "navigate": r"打开|导航|切换|前往",
    "repeat": r"重试|重做|再试|重复执行|再次执行",
    "other": r"绑定|提交|批准|审批|新建|创建|修改|选择",
}
_OP = re.compile("|".join(f"(?P<{key}>{value})" for key, value in _OPERATIONS.items()))
_COMMAND = re.compile(r"必须|需要|需|应当|应该|应|要|请|建议|务必|先|接着|随后|然后|再(?:在|为|对|去)?|继续|还可|可以|即可|就能|应当|执行|操作|并(?:在|为|对)?|同时")
_SEQUENCE = re.compile(r"然后|接着|随后|再(?:在|为|对|去)?|成功后|完成后|之后|以后|后(?:请|可|就|再|对|为|在)?|下一步|则|那么|时")
_NEGATIVE = re.compile(r"(?:不要|不得|不能|不可|无需|不必|不建议|禁止|避免|切勿|勿)(?:\s|再|重复|重新|直接|擅自|再次|为\s*trip_[12]\s*)*$")
_FACT_PREFIX = re.compile(r"(?:已经|已|尚未|还未|并未|没有|正在|未|并非|不是|不等于)\s*$")
_AMBIGUOUS = re.compile(r"另(?:一|一个|外一)(?:个)?(?:行程|单|订单|航段)|其他(?:行程|单|订单)|其余(?:行程|单|订单)|前者|后者")
_NOUN_SUFFIX = re.compile(r"^(?:状态|结果|资格|条件|前提|入口|金额|记录|快照|占用)")
_FACT_SUFFIX = re.compile(r"^(?:中|状态|结果|资格|条件|前提|入口|金额|失败|成功|(?:当前)?有效|请求(?:已|尚未)受理|是否|不等于|并不|不是|不代表|记录|快照|占用)")
# 仅限当前动作的复述语法，包括“需先”等当前要求语气。
# “所选”只修饰明示ref，“合格”只在动作词前；不引入任意前缀。
# 对象仍须在本分句明示，不能由same或selected补齐；后续上下文另行拒绝。
_CURRENT_PREFIX = re.compile(r"(?:\s|请|建议|需要|需|必须|应当|应该|应|先|本步|当前动作|当前|本次|为|给|对|将|把|尝试|执行|进行|(?:所选\s*)?trip_[12])*(?:合格\s*)?")
_LATER_CONTEXT = re.compile(r"然后|接着|随后|再次|再|重试|重复|继续|成功后|完成后|失败后|之后|以后|下一步|否则|若.{0,20}(?:成功|失败)|如果.{0,20}(?:成功|失败)")
# 有界的查询宾语语法，不能夹入执行动词、并列指令或未来条件。整段原文仍呈现
# 并经过事实守卫；这里只识别“核对锁价/付款等状态”，不证明这些状态为真。
_READ_VERB = r"(?:查询|查看|核对|确认|刷新)"
_READ_PREFIX = r"(?:\s|本步|当前|本次|请|建议|需要|需|必须|应当|应该|应|先|对|为|trip_[12])*"
_STATE_MODIFIER = r"(?:\s|trip_[12]|的|服务端|当前|实际|原有|原|本次)*"
_STATE_NAME = r"(?:锁定报价|锁定价格|锁价|付款|支付|出票|票据|(?:共享|优惠)?券(?:归属)?|最终应付额)"
_STATE_DETAIL = r"(?:状态|结果|记录|快照|是否(?:已)?(?:生效|成功|完成))"
_STATE_ITEM = _STATE_MODIFIER + _STATE_NAME + _STATE_MODIFIER + "(?:" + _STATE_DETAIL + ")?"
_STATE_OBJECT = _STATE_ITEM + r"(?:(?:与|和|及)" + _STATE_ITEM + ")*"
_READ_STATE = re.compile(
    _READ_PREFIX + _READ_VERB + _STATE_OBJECT + r"\s*|"
    + _STATE_OBJECT + _READ_PREFIX + _READ_VERB + r"\s*")
_EXPECTED_NOUN = re.compile(r"(?:预计|预期|将|会|应)(?:生成|产生|返回|显示|得到)(?:的)?$")
_POSTPOSED_COMMAND = re.compile(
    r"^(?:状态|结果|入口|操作)?(?:需(?:要)?|必须|应(?:当|该)?|请|建议|随后|然后|再)"
    r"(?:立即|马上|先)?(?:执行|操作|办理)")



# 本步结果核对使用字段语义和有界结果名词；查询/刷新仍是独立API动作。
# reason/conditions必须明说本步返回/回执，check已有“本步预期/核对”语义。
# lock中的“锁定额”仅指本步冻结金额；不扩展到估价、余额或其他动作。
_RESULT_NAMES = {
    "lock": r"锁(?:定报价|定价格|价)(?:结果|状态|快照|记录|是否生效|成功)|锁状态|(?:共享|优惠)?券(?:归属|归)|归属券ref|冻结金额|frozen金额|锁定额|最终应付(?:额|金额)|(?:锁)?有效性(?:状态)?|有效期",
    "pay": r"(?:付款|支付)(?:请求)?(?:受理结果|结果|状态|记录|编号)|(?:付款|支付)尝试(?:编号|状态)|(?:付款|支付)金额",
    "cancel": r"取消(?:锁价)?(?:结果|状态)|(?:共享|优惠)?券(?:释放状态|归属)|(?:库存|资源)(?:释放结果|占用状态)",
    "requote": r"(?:重新报价|重报价|报价)(?:结果|状态|金额|修订|有效性|有效期)|有效性|有效期",
}
_RESULT_MODIFIER = r"(?:\s|trip_[12]|的|本步|本次|返回|回执|中|服务端|当前|实际|生效)*"
# 当前锁价的返回来源只与有限结果名词组合，不是任意前后缀。
_LOCK_RETURN_SOURCE = r"锁(?:价|定报价|定价格)返回的"
_LOCK_RETURNED = re.compile(_LOCK_RETURN_SOURCE)
_RESULT_OBJECTS = {}
for _kind, _names in _RESULT_NAMES.items():
    _source = "(?:" + _LOCK_RETURN_SOURCE + ")?" if _kind == "lock" else ""
    _item = _RESULT_MODIFIER + _source + "(?:" + _names + ")" + _RESULT_MODIFIER
    _RESULT_OBJECTS[_kind] = re.compile(_item + r"(?:(?:与|和|及)" + _item + ")*")
_CHECK_PREFIX = r"(?:\s|本步|本次|请|需要|需|应当|应该|应|对|为|trip_[12])*"
_CHECK_VERB = r"(?:核对|确认|检查|查看)"
_CHECK_LEADING = re.compile(_CHECK_PREFIX + _CHECK_VERB + r"(?P<object>.+)")
_CHECK_TRAILING = re.compile(r"(?P<object>.+?)" + _CHECK_PREFIX + _CHECK_VERB + r"\s*")
_RETURNED_RESULT = re.compile(r"本(?:步|次)(?:返回|回执)")


def _step_result_check(clause, preceding, action, field, continued):
    lock_return = _LOCK_RETURNED.search(clause) is not None
    lock_amount = "锁定额" in clause
    if (lock_return or lock_amount) and action["kind"] != "lock":
        _fail("action_explanation_route")
    pattern = _RESULT_OBJECTS.get(action["kind"])
    if pattern is None:
        return False
    match = _CHECK_LEADING.fullmatch(clause) or _CHECK_TRAILING.fullmatch(clause)
    if match and not lock_amount and re.search(r"(?:仍待|待)$", match["object"]):
        return False  # 待确认是状态描述，交回原事实/动作检查。
    explicit_return = _RETURNED_RESULT.search(clause) is not None or lock_return
    # 没有新的核对词时，仅同一个核对列表可继承目标与字段语义。
    if match is None and not continued:
        if lock_amount:
            _fail("action_explanation_route")
        return False
    obj = match["object"] if match else clause
    if pattern.fullmatch(obj) is None:
        if match or lock_amount:
            _fail("action_explanation_route")
        return False
    refs = set(_REF.findall(clause))
    if not refs and continued:
        refs = {action["target"]}  # continued只来自已核验的明示对象，不来自结构猜测。
    if (refs != {action["target"]} or _LATER_CONTEXT.search(preceding + clause)
            or (field != "check" and not explicit_return and not continued)
            or ("查看" in clause and not explicit_return)):
        _fail("action_explanation_route")
    return True


def _current_state_read(clause, preceding, action):
    if _READ_STATE.fullmatch(clause) is None or re.search(_STATE_DETAIL, clause) is None:
        return False
    # 只继承同一说明字段前文的唯一明示ref，不从selected、action或别的字段补对象。
    refs = set(_REF.findall(clause)) or set(_REF.findall(preceding))
    if (action["kind"] != "query" or refs != {action["target"]}
            or _LATER_CONTEXT.search(preceding + clause)):
        _fail("action_explanation_route")
    return True


def _fail(reason):
    raise ValueError(reason)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("action_duplicate_field")
        value[key] = item
    return value


def _constant(_):
    _fail("action_format_invalid")


def _explanation(text, action, field=None):
    """逐字段检查，不因若/之后本身拒绝，也不让条件句豁免后续操作。"""
    normalized = unicodedata.normalize("NFKC", text)
    safe_text(normalized)
    if any(unicodedata.category(char) == "Cf" for char in normalized):
        _fail("action_text_invalid")
    if _AMBIGUOUS.search(normalized):
        _fail("action_fact_ambiguous")
    # 跨标点保留执行顺序：例如“失败后，给trip_1锁价”仍是重试。
    preceding = ""
    continued_result = False
    parts = re.split(r"([。！？\n；;，,、])", normalized)
    for index in range(0, len(parts), 2):
        clause = parts[index]
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if _step_result_check(clause, preceding, action, field, continued_result):
            continued_result = separator in {"，", ",", "、"}
            preceding += clause + separator
            continue
        continued_result = False
        if _current_state_read(clause, preceding, action):
            preceding += clause + "，"
            continue
        for match in _OP.finditer(clause):
            prefix, suffix = clause[:match.start()], clause[match.end():]
            kind = match.lastgroup
            if _POSTPOSED_COMMAND.search(suffix):
                _fail("action_explanation_route")
            # “之后查询状态”中状态是查询的宾语，不是让指令免检的名词后缀。
            query_instruction = kind == "query" and (
                _COMMAND.search(prefix) or _SEQUENCE.search(prefix)
                or _LATER_CONTEXT.search(preceding + prefix)) and not re.search(
                    r"(?:预计|预期|将|会|应)(?:生成|产生|返回|显示|得到)(?:的)?$", prefix)
            if _NEGATIVE.search(prefix) or _FACT_PREFIX.search(prefix):
                continue
            if kind == "repeat":
                _fail("action_explanation_route")
            imperative = _COMMAND.search(prefix) is not None
            sequential = _SEQUENCE.search(prefix) is not None
            later = _LATER_CONTEXT.search(preceding + prefix) is not None
            if _NOUN_SUFFIX.search(suffix) and not query_instruction:
                directed = (_CURRENT_PREFIX.fullmatch(prefix) is not None
                            and re.search(r"为|给|对|将|把|执行|进行", prefix))
                if (not imperative and not sequential and not later and not directed
                        or _EXPECTED_NOUN.search(prefix)):
                    continue
                _fail("action_explanation_route")
            if _FACT_SUFFIX.search(suffix) and not imperative and not sequential and not later:
                continue
            refs = set(_REF.findall(clause))
            same = kind == action["kind"] and (not refs or refs == {action["target"]})
            current_condition = (
                same and re.search(r"若|如果|只在|仅在|前提|条件|仍有效|满足|才能|才可|时", prefix)
                and not later
                and not re.search(r"先|然后|接着|随后|再|成功后|完成后|下一步", prefix))
            if current_condition:
                continue
            expected = same and not imperative and not sequential and not later and re.match(
                r"^(?:后|之后)?(?:预计|预期|将|会|应)(?:生成|产生|返回|显示|得到)", suffix)
            if expected:
                continue
            current_restatement = (
                same and refs == {action["target"]} and not later and not sequential
                and _CURRENT_PREFIX.fullmatch(prefix) is not None)
            if current_restatement:
                continue
            if imperative or sequential or later:
                _fail("action_explanation_route")
            subject = re.sub(r"\s|trip_[0-9]+|本步|当前动作|当前|本次|为|给|对|在|将|把|尝试|执行|进行", "", prefix)
            noun_or_fact = _FACT_SUFFIX.search(suffix) or re.match(
                r"^(?:前|需要|要求|须|需|应|尚|仍|未|已|可否|不)", suffix)
            if not subject and not noun_or_fact:
                _fail("action_explanation_route")
        preceding += clause + "，"


def validate_action_contract(content, state, allowed_pages, tool_calls=()):
    """严格解析原content；state仅提供ref白名单，不用来选择/补齐动作。"""
    if type(content) is not str or not 1 <= len(content) <= 5000:
        _fail("action_format_invalid")
    try:
        value = json.loads(content, object_pairs_hook=_pairs, parse_constant=_constant)
    except (json.JSONDecodeError, RecursionError):
        _fail("action_format_invalid")
    if type(value) is not dict or set(value) != _TOP_FIELDS:
        _fail("action_fields_invalid")
    if type(value["v"]) is not int or value["v"] != 1:
        _fail("action_version_invalid")
    action = value["action"]
    if type(action) is not dict or set(action) != _ACTION_FIELDS:
        _fail("action_fields_invalid")
    kind, target, entry, params = (action[key] for key in ("kind", "target", "entry", "params"))
    if type(kind) is not str or kind not in ACTION_ENTRIES:
        _fail("action_kind_invalid")
    if type(target) is not str or type(entry) is not str or entry != ACTION_ENTRIES[kind]:
        _fail("action_entry_invalid")
    if type(params) is not dict:
        _fail("action_params_invalid")
    if kind in BUSINESS_KINDS:
        refs = [row.get("ref") for row in state.get("delegated_trips", []) if type(row) is dict]
        if target not in {"trip_1", "trip_2"} or refs.count(target) != 1:
            _fail("action_target_invalid")
        if params:
            _fail("action_params_invalid")
    elif target != "task":
        _fail("action_target_invalid")
    if kind == "navigate":
        if (set(params) != {"page"} or type(params["page"]) is not str
                or params["page"] not in allowed_pages):
            _fail("action_params_invalid")
    elif params:
        _fail("action_params_invalid")
    if type(tool_calls) not in {list, tuple}:
        _fail("action_tool_mismatch")
    if kind == "navigate":
        if len(tool_calls) != 1 or type(tool_calls[0]) is not dict:
            _fail("action_tool_mismatch")
        call = tool_calls[0]
        if (set(call) != {"id", "name", "arguments"} or type(call["id"]) is not str
                or not call["id"] or call["name"] != "navigate" or call["arguments"] != params):
            _fail("action_tool_mismatch")
    elif tool_calls:
        _fail("action_tool_mismatch")
    for field in _TEXT_FIELDS:
        text = value[field]
        if type(text) is not str or len(text) > 400 or text != text.strip():
            _fail("action_text_invalid")
        _explanation(text, action, field)
    return value


def render_action_contract(value):
    """仅呈现已验证结构；首行是唯一当前动作，说明不成为动作来源。"""
    action = value["action"]
    kind, target = action["kind"], action["target"]
    entries = {"lock": "预订与票据的锁价入口", "pay": "预订与票据的付款入口",
               "cancel": "预订与票据的取消锁价入口", "query": "行程详情的刷新状态入口",
               "requote": "预订与票据的报价入口"}
    labels = {"lock": "锁价", "pay": "付款", "cancel": "取消锁价",
              "query": "查询原付款与票据状态", "requote": "重新报价"}
    if kind in BUSINESS_KINDS:
        first = f"当前动作：请在{entries[kind]}为{target}尝试{labels[kind]}。"
    elif kind == "navigate":
        pages = {"welcome": "欢迎", "profile": "付款资料", "trips": "我的行程", "booking": "预订与票据", "approvals": "审批工作台"}
        first = f"当前动作：尝试打开{pages[action['params']['page']]}分区，等待浏览器回执。"
    else:
        first = {"wait": "当前动作：请等待本步结果。", "clarify": "当前动作：请澄清本步缺失信息。",
                 "unknown": "当前动作：保留未知，当前资料不足以确定下一步。",
                 "end": "当前动作：结束本轮指引。"}[kind]
    rows = [first]
    rows.extend(f"{label}：{value[field]}" for field, label in
                (("reason", "原因"), ("conditions", "必要条件"), ("check", "本步核对")) if value[field])
    return "\n".join(rows)


def action_digest(value):
    """绑定整个原结构，包括说明，不从渲染/自由文本重新推测动作。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()

# V1 / P41：新候选使用证据值断言；旧v1解析/词法守卫保留历史语义。
EVIDENCE_CONTRACT_VERSION = "single-action-evidence-1"
EVIDENCE_PROMPT = '''仅输出JSON：
{"v":2,"action":{"kind":"...","target":"...","entry":"...","params":{}},"basis":{"state":"本轮action_evidence.basis.state","knowledge":"本轮action_evidence.basis.knowledge"},"evidence":[{"id":"本轮facts的键","value":"原值，保留类型"}],"gap":null}。
字段全必填，不增加reason/conditions/check或其他自由文字。选择唯一动作与支持该动作的证据，不输出后续路线。证据只从本轮action_evidence.facts精确引用，不猜测或改写金额/状态。basis原样引用本轮值，不能沿用历史。
kind/entry：lock/booking.lock,pay/booking.pay,cancel/booking.cancel_lock,query/booking.reconcile,requote/booking.quote；这些target=trip（本轮唯一选中的本人行程），params={}。未选对象不能补猜。
navigate：target=task,entry=navigate,params={"page":"允许页面"}，同响应必须真正调用唯一同页navigate工具。其他动作无工具。
wait/clarify/unknown/end：target=task,entry=task,params={}。clarify/unknown须选择gap=goal/object/evidence/unsupported之一；其他gap=null。至少引用一条本轮证据，不能用历史或按钮推断资格。
业务动作至少引用：lock需trip.quote.valid和trip.quote.amount_display；pay需trip.lock.valid和trip.lock.amount_display；cancel需trip.lock.valid；query需trip.payment.status；requote需trip.quote.valid。wait需trip.payment.status或trip.ticket_task.status；end需trip.ticket.status=valid。缺必要事实则clarify/unknown；结束只表示本轮选中单行程已取得有效票，不能宣称更大目标完成。
用户业务操作后重新观察，不代付款/审批；付款成功不等于出票。导航仅提案，等待真实浏览器回执。'''

_GAPS = {"goal": "请说明本次希望完成的单行程目标。", "object": "请在原页面选择要咨询的本人行程。",
         "evidence": "当前允许资料不足，请补充问题或更新页面状态后继续咨询。",
         "unsupported": "此问题超出当前已验证的单行程范围，暂不能确定操作。"}
_FACT_LABELS = {
    "selected_trip": "是否已选择本人行程", "payment_profile_state": "付款资料资格", "flight_state": "航班可用性",
    "trip.quote.valid": "报价有效性", "trip.quote.amount_display": "预报价金额",
    "trip.quote.effective_status": "报价状态", "trip.lock.valid": "锁价有效性",
    "trip.lock.amount_display": "锁定金额", "trip.lock.effective_status": "锁价状态",
    "trip.payment.status": "原付款状态", "trip.payment.amount_display": "原付款金额",
    "trip.ticket_task.status": "出票任务状态", "trip.ticket.status": "票据状态",
}
_STATUS_LABELS = {"usable": "可用", "unusable": "不可用", "bound": "已绑定（资格仍待核验）", "unbound": "未绑定",
                  "unknown": "未知", "available": "可用", "unavailable": "不可用", "active": "有效",
                  "held": "已占用", "protected": "受原付款保护", "consumed": "已消耗", "cancelled": "已取消",
                  "expired": "已过期", "invalidated": "已失效", "failed": "失败", "processing": "处理中",
                  "succeeded": "成功", "pending": "等待处理", "valid": "有效"}
_ACTION_NOTES = {
    "lock": ("本步采用当前报价尝试锁价，实际库存和资格由服务端重核。", "核对本步返回的锁价状态、金额与有效期。"),
    "pay": ("本步采用有效锁价尝试付款，实际权限与资格由服务端重核。", "核对原付款受理结果；付款成功不等于出票。"),
    "cancel": ("本步仅尝试取消当前锁价，受保护或已消耗资源不能据此恢复。", "核对本步取消结果和锁价状态。"),
    "query": ("核对原付款与票据状态，避免把未决结果当作失败。", "核对返回的原付款状态和有效票据。"),
    "requote": ("本步请求当前行程的新报价，不保证库存、价格或资格。", "核对本步报价的金额、有效性与期限。"),
    "navigate": ("打开模型选定的允许页面；提案尚未执行。", "等待浏览器实际页面回执。"),
    "wait": ("原付款或出票任务仍在处理中。", "当前尚无有效票据，结果仍待核对。"),
    "end": ("本轮选中行程已观察到有效票据。", "仅结束本次单行程指引，其他目标未据此验证。"),
}


def evidence_context(state, knowledge_hash):
    """仅选择已有授权投影叶字段；不计算正确路线，不发送隐藏状态。"""
    data = state.get("data", {})
    facts = {"selected_trip": bool(data.get("trip"))}
    for field in ("payment_profile_state", "flight_state"):
        facts[field] = state.get(field)
    for kind, fields in {"quote": ("valid", "amount_display", "effective_status"),
                         "lock": ("valid", "amount_display", "effective_status"),
                         "payment": ("status", "amount_display"),
                         "ticket_task": ("status",), "ticket": ("status",)}.items():
        row = data.get(kind) or {}
        for field in fields:
            if field == "amount_display":
                minor = row.get("amount_minor")
                facts[f"trip.{kind}.{field}"] = (f"{Decimal(minor) / 100:.2f} 模拟元"
                    if minor is not None and (kind == "payment" or row.get("valid") is True) else None)
            else:
                facts[f"trip.{kind}.{field}"] = row.get(field)
    return {"basis": {"state": action_digest(state), "knowledge": knowledge_hash}, "facts": facts}


def validate_evidence_contract(content, state, allowed_pages, tool_calls, context):
    """v2拒绝错误断言，不以宿主正确值替换候选；权限由既有投影与新鲜度检查提供。"""
    if type(content) is not str or not 1 <= len(content) <= 5000:
        _fail("action_format_invalid")
    try:
        value = json.loads(content, object_pairs_hook=_pairs, parse_constant=_constant)
    except (json.JSONDecodeError, RecursionError):
        _fail("action_format_invalid")
    if type(value) is not dict or set(value) != {"v", "action", "basis", "evidence", "gap"}:
        _fail("action_fields_invalid")
    if type(value["v"]) is not int or value["v"] != 2:
        _fail("action_version_invalid")
    if context != evidence_context(state, context["basis"]["knowledge"]) or value["basis"] != context["basis"]:
        _fail("action_evidence_stale")
    action = value["action"]
    if type(action) is not dict or set(action) != _ACTION_FIELDS:
        _fail("action_fields_invalid")
    # 复用唯一动作/参数/真实工具校验；仅临时规范化已明示的trip引用，不替模型补对象。
    normalized = json.loads(json.dumps(action))
    if type(normalized.get("kind")) is not str:
        _fail("action_kind_invalid")
    if normalized.get("kind") in BUSINESS_KINDS:
        if normalized.get("target") != "trip" or not state.get("data", {}).get("trip"):
            _fail("action_target_invalid")
        if state.get("contract_version") != "d3-personal-1":
            _fail("action_scope_unsupported")
        normalized["target"] = "trip_1"
    validate_action_contract(json.dumps({"v": 1, "action": normalized, "reason": "", "conditions": "", "check": ""}),
                             {"delegated_trips": [{"ref": "trip_1"}]}, allowed_pages, tool_calls)
    evidence = value["evidence"]
    if type(evidence) is not list or not 1 <= len(evidence) <= 8:
        _fail("action_evidence_required")
    seen = set()
    for claim in evidence:
        if type(claim) is not dict or set(claim) != {"id", "value"} or type(claim["id"]) is not str:
            _fail("action_evidence_invalid")
        key = claim["id"]
        if key in seen or key not in context["facts"]:
            _fail("action_evidence_invalid")
        expected = context["facts"][key]
        if type(claim["value"]) is not type(expected) or claim["value"] != expected:
            _fail("action_fact_conflict")
        seen.add(key)
    kind, gap = action["kind"], value["gap"]
    if kind in {"clarify", "unknown"}:
        if type(gap) is not str or gap not in _GAPS:
            _fail("action_gap_required")
    elif gap is not None:
        _fail("action_gap_invalid")
    required = {"lock": {"trip.quote.valid", "trip.quote.amount_display"},
                "pay": {"trip.lock.valid", "trip.lock.amount_display"}, "cancel": {"trip.lock.valid"},
                "query": {"trip.payment.status"}, "requote": {"trip.quote.valid"}, "end": {"trip.ticket.status"}}
    if not required.get(kind, set()) <= seen:
        _fail("action_evidence_required")
    facts = context["facts"]
    if kind in {"lock", "pay", "cancel"}:
        price = "quote" if kind == "lock" else "lock"
        if facts[f"trip.{price}.valid"] is not True:
            _fail("action_precondition_unverified")
        if kind in {"lock", "pay"} and (facts[f"trip.{price}.amount_display"] is None or
                facts["payment_profile_state"] != "usable" or facts["flight_state"] != "available"):
            _fail("action_precondition_unverified")
    if kind in {"lock", "pay", "cancel", "requote"} and (
            facts["trip.payment.status"] in {"processing", "unknown", "succeeded"} or facts["trip.ticket.status"] == "valid"):
        _fail("action_payment_or_ticket_exists")
    if kind == "wait" and (not seen & {"trip.payment.status", "trip.ticket_task.status"} or
            not (facts["trip.payment.status"] in {"processing", "unknown"} or facts["trip.ticket_task.status"] == "pending") or
            facts["trip.ticket.status"] == "valid"):
        _fail("action_wait_unverified")
    if kind == "end" and (state.get("contract_version") != "d3-personal-1" or facts["trip.ticket.status"] != "valid"):
        _fail("completion_unverified")
    return value


def render_evidence_contract(value):
    """唯一动作原样呈现；说明为当前动作语义+精确证据，无未经验证自由文本。"""
    action = value["action"]
    first = render_action_contract({"action": action, "reason": "", "conditions": "", "check": ""}).splitlines()[0]
    if action["kind"] == "query":
        first = "当前动作：请在预订与票据的对账原付款入口为本轮选中行程尝试查询原付款与票据状态。"
    if action["target"] == "trip":
        first = first.replace("为trip", "为本轮选中行程")
    kind = action["kind"]
    if kind in {"clarify", "unknown"}:
        notes = (_GAPS[value["gap"]], "资料不足保持未知，不据此判断业务成功。")
    else:
        notes = _ACTION_NOTES[kind]
    rows = [first, "本步说明：" + notes[0]]
    for claim in value["evidence"]:
        v = claim["value"]
        shown = "未知或未观察到" if v is None else "是" if v is True else "否" if v is False else _STATUS_LABELS.get(v, v)
        rows.append(f"已观察 · {_FACT_LABELS[claim['id']]}：{shown}。")
    rows.append("依据：本轮服务端观察与已发布资料；资料本身不证明本人资格。")
    rows.append("本步核对：" + notes[1])
    return "\n".join(rows)


def confirm_evidence_delivery(formal, confirmation, *, state, context, allowed_pages):
    """工程/人工确认绑定正式交付、当前对象与证据；不选动作、不执行业务。"""
    if formal.get("action_contract_version") != EVIDENCE_CONTRACT_VERSION:
        _fail("action_version_invalid")
    value = formal.get("action_contract")
    tool = formal.get("tool")
    calls = [] if tool is None else [{k: tool.get(k) for k in ("id", "name", "arguments")}]
    checked = validate_evidence_contract(json.dumps(value), state, allowed_pages, calls, context)
    reply = render_evidence_contract(checked)
    expected = {"action_sha256": action_digest(checked), "reply_sha256": action_digest(reply), "action": checked["action"]}
    if formal.get("action_sha256") != expected["action_sha256"] or formal.get("reply") != reply or confirmation != expected:
        _fail("action_confirmation_mismatch")
    return checked["action"]
