"""P29 有限任务的源/实际输入双绑定；仅检查，不授予外发或费用许可。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import json
import re

from .assistant import SHARED_PROMPT_VERSIONS, corporate_export_scope, prompt_system
from .contracts import safe_text
from .experiment import MAX_INPUT_BYTES, NAVIGATION_TOOL, PAGES, input_size_bytes
from .fair_runtime import FairTaskBinding, fair_input
from .knowledge import fingerprint
from .model import ENDPOINT, MODEL, MODEL_REVISION, ModelUnavailable
from .page_observation import Observation


# 只允许当前实验实际采集的公开标签；状态、价格和身份不能塞进页面自由文字。
TASK_OBSERVATIONS = {
    'welcome': {'title': '欢迎', 'items': ['公开功能说明']},
    'profile': {'title': '付款资料', 'items': ['公开功能说明', '绑定付款资料']},
    'trips': {'title': '我的行程', 'items': ['公开功能说明', '创建自己的行程',
        '暂估报价不会占用优惠券，最终金额以成功锁价为准。']},
    'booking': {'title': '预订与票据', 'items': ['报价', '锁价', '付款', '票据']},
}
_CONTEXT = {'goal', 'current_page', 'current_state', 'current_observation', 'allowed_pages',
            'knowledge_hash', 'history', 'question', 'published_learning'}
_STATE_DYNAMIC = {'selected_ref', 'delegated_trips', 'shared_coupon', 'observed_at', 'valid_until'}
_PRICE_FIELDS = {'ref', 'trip_revision', 'effective_status', 'valid', 'expires_at', 'price_basis',
                 'coupon_allocation', 'amount_display', 'base_amount_display', 'coupon_discount_display'}
_DATA_FIELDS = {'trip', 'quote', 'lock', 'payment', 'ticket_task', 'ticket'}
_MONEY = re.compile(r'(?:0|[1-9][0-9]{0,7})\.[0-9]{2} 模拟元\Z')


def _reject():
    raise ModelUnavailable('q19_scope_invalid')


def _keys(value, expected):
    if type(value) is not dict or set(value) != expected:
        _reject()


def _same(left, right):
    """类型及消息content均精确比较；bool不能冒充数字。"""
    return json.dumps(left, ensure_ascii=False, separators=(',', ':'), sort_keys=True) == json.dumps(
        right, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _time(value):
    if type(value) is not str or not 20 <= len(value) <= 40:
        _reject()
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        _reject()
    if result.tzinfo is None:
        _reject()
    return result


def _minor(value):
    if type(value) is not str or _MONEY.fullmatch(value) is None:
        _reject()
    return int(Decimal(value.removesuffix(' 模拟元')) * 100)


def _context(payload, prompt_version="assistant-6"):
    if type(payload) is not dict or prompt_version not in SHARED_PROMPT_VERSIONS:
        _reject()
    _keys(payload, {'messages', 'tools', 'max_tokens', 'json_output'})
    if (type(payload['messages']) is not list or len(payload['messages']) != 2
            or not _same(payload['messages'][0], {'role': 'system', 'content': prompt_system(prompt_version)})
            or not _same(payload['tools'], [NAVIGATION_TOOL])
            or type(payload['max_tokens']) is not int or payload['max_tokens'] != 512
            or payload['json_output'] is not False):
        _reject()
    _keys(payload['messages'][1], {'role', 'content'})
    if payload['messages'][1]['role'] != 'user' or type(payload['messages'][1]['content']) is not str:
        _reject()
    # 对原始源也检查限额，B0不能靠删除共享字段绕过。
    if input_size_bytes(payload) > MAX_INPUT_BYTES:
        _reject()
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    _reject()
                result[key] = value
            return result
        context = json.loads(payload['messages'][1]['content'], object_pairs_hook=unique)
    except (ValueError, TypeError):
        _reject()
    _keys(context, _CONTEXT)
    return context


@dataclass(frozen=True)
class FrozenTaskInput:
    task_ref: str
    task_sequence: int
    round_index: int
    physical_index: int
    source_hash: str
    actual_hash: str
    scope_hash: str
    reviewed_source_hash: str
    _source_json: str
    _reviewed_json: str
    _actual_json: str

    @property
    def raw_source_hash(self):
        return self.source_hash

    @property
    def source(self):
        return json.loads(self._source_json)

    @property
    def reviewed_source(self):
        return json.loads(self._reviewed_json)

    @property
    def payload(self):
        return json.loads(self._actual_json)


class DynamicTaskScope:
    """一个宿主固定任务的有限规则；review不是用户批准，也不创建许可。"""

    def __init__(self, binding, initial_source, *, model=MODEL, task_sequence=1, prompt_version="assistant-6"):
        if (prompt_version not in SHARED_PROMPT_VERSIONS or not isinstance(binding, FairTaskBinding) or binding.group not in {'B0', 'B1'}
                or len(binding.delegated_trip_ids) != 2 or model != MODEL
                or type(task_sequence) is not int or not 1 <= task_sequence <= 4):
            _reject()
        self.binding = binding
        self.prompt_version = prompt_version
        self.task_sequence = task_sequence
        self.model = model
        self._host_binding_hash = fingerprint(asdict(binding))
        self._initial_source = deepcopy(initial_source)
        initial = _context(self._initial_source, self.prompt_version)
        if (initial['goal'] != binding.goal or initial['question'] != binding.goal
                or initial['history'] != [] or initial['current_page'] not in TASK_OBSERVATIONS
                or type(initial['allowed_pages']) is not list
                or initial['allowed_pages'] != ['welcome', 'profile', 'trips', 'booking']
                or type(initial['knowledge_hash']) is not str
                or re.fullmatch(r'[a-f0-9]{64}', initial['knowledge_hash']) is None):
            _reject()
        self._initial = deepcopy(initial)
        self._state = deepcopy(initial['current_state'])
        self._validate_initial_state()
        self._validate_source(self._initial_source, ())
        self._initial_payload = fair_input(self._initial_source, binding.group, prompt_version=self.prompt_version)
        self._round = 0
        self._pending = None
        self._consumed = True
        logical_binding = asdict(binding)
        logical_binding['delegated_trip_ids'] = ['trip_1', 'trip_2']
        self.scope_hash = fingerprint({'schema': 'P29-dynamic-scope-1', 'binding': logical_binding,
            'task_sequence': task_sequence, 'model': MODEL, 'model_revision': MODEL_REVISION,
            'endpoint': ENDPOINT, 'initial_source_scope_hash': corporate_export_scope(self._initial_source),
            'rules': self._rules()})

    def _validate_initial_state(self):
        state = self._state
        expected = {'contract_version', 'product_id', 'product_version', 'rule_version', 'policy_version',
            'feature_config_version', 'source_kind', 'environment', 'simulated_identity', 'disclosure_scope',
            'selected_trip', 'selected_ref', 'delegation', 'delegated_trips', 'shared_coupon', 'state_versions',
            'business_time', 'assumptions', 'unknowns', 'documented', 'observed_at', 'valid_until',
            'presentation_contract', 'time_contract'}
        _keys(state, expected)
        fixed = {'contract_version': 'assistant-shared-1', 'product_id': 'travel-lab',
            'product_version': 'v1', 'rule_version': 'booking-v1', 'policy_version': 'subject-assignment-v1',
            'feature_config_version': 'shared-coupon-1', 'source_kind': 'observed', 'environment': 'local-demo',
            'simulated_identity': 'synthetic_personal', 'disclosure_scope': 'current-session',
            'selected_trip': True, 'delegation': 'two_explicit_owned_trips',
            'assumptions': ['synthetic_demo_account', 'simulated_business_services'],
            'unknowns': ['unexecuted_order_outcomes'], 'presentation_contract': 'assistant-money-display-2',
            'time_contract': {'business_clock': 'business_time', 'capture_clock': 'observed_at',
                'observation_expiry': 'valid_until', 'price_validity': 'each_price.valid_and_effective_status'}}
        if any(not _same(state[key], value) for key, value in fixed.items()):
            _reject()
        from services.business.assistant_projection import SHARED_DOCUMENT, SHARED_DOCUMENT_V2
        document = SHARED_DOCUMENT_V2 if self.prompt_version in {"assistant-7", "assistant-8", "assistant-9", "assistant-10", "assistant-11", "assistant-12"} else SHARED_DOCUMENT
        if state['documented'] != document:
            _reject()
        _time(state['business_time'])
        _keys(state['state_versions'], {'trips', 'payment_profile'})
        _keys(state['state_versions']['trips'], {'trip_1', 'trip_2'})
        if (type(state['state_versions']['payment_profile']) is not int
                or not 0 <= state['state_versions']['payment_profile'] <= 1000000
                or type(state['delegated_trips']) is not list or len(state['delegated_trips']) != 2):
            _reject()
        self._trip_rules = {}
        for index, row in enumerate(state['delegated_trips'], 1):
            _keys(row, {'ref', 'data', 'unknowns'})
            if row['ref'] != f'trip_{index}':
                _reject()
            _keys(row['data'], _DATA_FIELDS)
            trip = row['data']['trip']
            from services.business.guide_projection import Trip
            try:
                Trip.model_validate(trip)
            except ValueError:
                _reject()
            safe_text(trip['flight_id'])
            if (type(state['state_versions']['trips'][row['ref']]) is not int
                    or state['state_versions']['trips'][row['ref']] != trip['revision']):
                _reject()
            quote = row['data']['quote']
            _keys(quote, _PRICE_FIELDS)
            if quote['valid'] is not True:
                _reject()
            self._trip_rules[row['ref']] = {'trip': {key: value for key, value in trip.items() if key != 'status'},
                'base_minor': _minor(quote['base_amount_display']), 'expires_at': quote['expires_at']}
        coupon = state['shared_coupon']
        _keys(coupon, {'ref', 'status', 'percent', 'attribution', 'trip_ref', 'cap_display'})
        if (coupon['ref'] != 'shared_coupon' or type(coupon['percent']) is not int
                or not 1 <= coupon['percent'] <= 100 or _minor(coupon['cap_display']) <= 0):
            _reject()
        self._coupon_percent, self._coupon_cap = coupon['percent'], _minor(coupon['cap_display'])

    def _validate_price(self, row, kind, rule):
        if row is None:
            return
        _keys(row, _PRICE_FIELDS)
        statuses = {'active', 'expired', 'invalidated'} if kind == 'quote' else {
            'held', 'protected', 'consumed', 'cancelled', 'expired', 'invalidated', 'failed'}
        allocations = {'estimate', 'not_applied'} if kind == 'quote' else {
            'not_applied', 'held', 'protected', 'consumed', 'released'}
        if (row['ref'] != kind or type(row['trip_revision']) is not int
                or row['trip_revision'] != rule['trip']['revision']
                or row['effective_status'] not in statuses or type(row['valid']) is not bool
                or row['valid'] != (row['effective_status'] in {'active', 'held'})
                or row['price_basis'] != ('estimate' if kind == 'quote' else 'frozen')
                or row['coupon_allocation'] not in allocations
                or _time(row['expires_at']) != _time(rule['expires_at'])):
            _reject()
        values = [row[key] for key in ('amount_display', 'base_amount_display', 'coupon_discount_display')]
        if not row['valid']:
            if values != [None, None, None]:
                _reject()
            return
        amount, base, reduction = map(_minor, values)
        discount = min(rule['base_minor'] * self._coupon_percent // 100, self._coupon_cap)
        if (base != rule['base_minor'] or reduction not in {0, discount} or amount != base - reduction
                or (row['coupon_allocation'] == 'not_applied') != (reduction == 0)):
            _reject()

    def _validate_state(self, state):
        _keys(state, set(self._state))
        if any(not _same(value, self._state[key]) for key, value in state.items() if key not in _STATE_DYNAMIC):
            _reject()
        if (state['selected_ref'] not in {'trip_1', 'trip_2'}
                or _time(state['valid_until']) - _time(state['observed_at']) != timedelta(seconds=30)
                or type(state['delegated_trips']) is not list or len(state['delegated_trips']) != 2):
            _reject()
        for index, row in enumerate(state['delegated_trips'], 1):
            _keys(row, {'ref', 'data', 'unknowns'})
            if row['ref'] != f'trip_{index}' or row['unknowns'] not in ([], ['current_quote_amount']):
                _reject()
            rule = self._trip_rules[row['ref']]
            data = row['data']
            _keys(data, _DATA_FIELDS)
            _keys(data['trip'], {*rule['trip'], 'status'})
            if (not _same({key: value for key, value in data['trip'].items() if key != 'status'}, rule['trip'])
                    or data['trip']['status'] not in {'draft', 'payment_pending', 'paid'}):
                _reject()
            for kind in ('quote', 'lock'):
                self._validate_price(data[kind], kind, rule)
            if data['payment'] is not None:
                payment = data['payment']
                _keys(payment, {'ref', 'status', 'amount_display'})
                discount = min(rule['base_minor'] * self._coupon_percent // 100, self._coupon_cap)
                if (payment['ref'] != 'payment' or payment['status'] not in {'processing', 'unknown', 'succeeded', 'failed'}
                        or _minor(payment['amount_display']) not in {rule['base_minor'], rule['base_minor'] - discount}):
                    _reject()
            for kind, statuses in (('ticket_task', {'pending', 'succeeded', 'failed'}), ('ticket', {'valid'})):
                item = data[kind]
                if item is not None:
                    _keys(item, {'ref', 'status', 'trip_revision'} if kind == 'ticket' else {'ref', 'status'})
                    if (item['ref'] != kind or item['status'] not in statuses
                            or (kind == 'ticket' and (type(item['trip_revision']) is not int
                                or item['trip_revision'] != rule['trip']['revision']))):
                        _reject()
        coupon = state['shared_coupon']
        _keys(coupon, set(self._state['shared_coupon']))
        if (coupon['ref'] != 'shared_coupon' or type(coupon['percent']) is not int
                or coupon['percent'] != self._coupon_percent or coupon['cap_display'] != self._state['shared_coupon']['cap_display']
                or coupon['status'] not in {'available', 'held', 'protected', 'consumed'}
                or coupon['attribution'] not in {'none', 'delegated_trip', 'unknown'}
                or coupon['trip_ref'] not in {None, 'trip_1', 'trip_2'}
                or (coupon['attribution'] == 'delegated_trip') != (coupon['trip_ref'] is not None)
                or (coupon['status'] == 'available' and coupon['attribution'] != 'none')):
            _reject()

    def _validate_observation(self, observation, page, *, source=True):
        if type(observation) is not dict or page not in TASK_OBSERVATIONS:
            _reject()
        value = deepcopy(observation)
        if source and value.pop('source_kind', None) != 'client_observed':
            _reject()
        try:
            Observation.model_validate(value)
        except ValueError:
            _reject()
        public = TASK_OBSERVATIONS[page]
        if (value['page'] != page or value['title'] != public['title']
                or len(value['items']) != len(set(value['items']))
                or set(value['items']) - set(public['items'])):
            _reject()

    def _validate_source(self, source, feedback):
        context = _context(source, self.prompt_version)
        if not _same(source['messages'][0], self._initial_source['messages'][0]):
            _reject()
        if fingerprint(asdict(self.binding)) != self._host_binding_hash:
            _reject()
        for key in ('goal', 'allowed_pages', 'knowledge_hash', 'published_learning'):
            if not _same(context[key], self._initial[key]):
                _reject()
        allowed = {self.binding.goal}
        for text in feedback:
            if type(text) is not str or not 1 <= len(text) <= 400:
                _reject()
            safe_text(text)
            allowed.add(text)
        if context['question'] not in allowed:
            _reject()
        safe_text(context['question'])
        if type(context['history']) is not list or len(context['history']) > 4:
            _reject()
        # raw历史完整留hash；与fair_input逐字相同的共同规则只外发user_reported。
        history = [row for row in context['history'] if type(row) is dict and set(row) == {'source_kind', 'text'}
                   and row['source_kind'] == 'user_reported']
        for row in history:
            if row['text'] not in allowed:
                _reject()
            safe_text(row['text'])
        self._validate_state(context['current_state'])
        if context['current_page'] not in context['allowed_pages']:
            _reject()
        self._validate_observation(context['current_observation'], context['current_page'])
        reviewed = deepcopy(source)
        context['history'] = history
        reviewed['messages'][1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
        return reviewed

    def freeze_round(self, source, payload, *, round_index, physical_index, feedback=()):
        try:
            if (type(round_index) is not int or round_index != self._round + 1
                    or round_index > self.binding.max_rounds or not self._consumed
                    or type(physical_index) is not int or physical_index != 1):
                _reject()
            reviewed = self._validate_source(source, feedback)
            expected = fair_input(source, self.binding.group, prompt_version=self.prompt_version)
            if (not _same(payload, expected) or input_size_bytes(payload) > MAX_INPUT_BYTES
                    or (round_index == 1 and corporate_export_scope(reviewed) != corporate_export_scope(self._initial_source))):
                _reject()
            frozen = FrozenTaskInput(self.binding.task_ref, self.task_sequence, round_index, physical_index,
                fingerprint(source), fingerprint(payload), self.scope_hash, fingerprint(reviewed),
                json.dumps(source, ensure_ascii=False), json.dumps(reviewed, ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False))
            self._pending, self._consumed = frozen, False
            self._round = round_index
            return frozen
        except (ValueError, TypeError, KeyError, AttributeError):
            _reject()

    def check_actual(self, value):
        if (self._pending is None or self._consumed or not _same(value, self._pending.payload)
                or fingerprint(asdict(self.binding)) != self._host_binding_hash):
            _reject()
        return self._pending

    def authorize_actual(self, value):
        frozen = self.check_actual(value)
        self._consumed = True
        return frozen

    def _rules(self):
        return {'source_preprocessing': 'record raw source; retain exactly user_reported history as fair_input',
            'actual_transform': 'B0 removes current_state/current_observation; B1 equals reviewed source',
            'fixed': ['task/group/actor/ordered anonymous delegation/release', 'system/knowledge/tools/model/destination',
                'trip flight/date/passengers/revision', 'payment_profile revision/business_time', 'coupon percent/cap'],
            'dynamic': ['selected_ref in trip_1/trip_2', 'existing anonymous quote/lock/payment/ticket states',
                'original base price or the public coupon discount', 'registered public page vocabulary/semantics',
                '30 second aware capture timestamps', 'host-registered user_reported question/history'],
            'unknown_fields': 'reject', 'source_input_bytes': MAX_INPUT_BYTES,
            'actual_input_bytes': MAX_INPUT_BYTES, 'output_tokens': 512,
            'implicit_model_tool_followup': 'reject; current Assistant has one physical request per chat',
            'public_observations': deepcopy(TASK_OBSERVATIONS)}

    def review(self):
        return {**({'prompt_version': self.prompt_version} if self.prompt_version in {'assistant-7', 'assistant-8', 'assistant-10', 'assistant-11', 'assistant-12'} else {}),
            'schema': 'P29-dynamic-scope-1', 'approved': False,
            'fixed_binding': asdict(self.binding), 'host_binding_hash': self._host_binding_hash,
            'task_sequence': self.task_sequence, 'scope_hash': self.scope_hash,
            'model': MODEL, 'model_revision': MODEL_REVISION, 'endpoint': ENDPOINT,
            'initial_source': deepcopy(self._initial_source), 'initial_payload': deepcopy(self._initial_payload),
            'initial_source_hash': fingerprint(self._initial_source),
            'initial_actual_hash': fingerprint(self._initial_payload),
            'initial_source_bytes': input_size_bytes(self._initial_source),
            'initial_actual_bytes': input_size_bytes(self._initial_payload),
            'initial_scope_hash': corporate_export_scope(self._initial_source),
            'future_exact_hashes': None, 'rules': self._rules()}
