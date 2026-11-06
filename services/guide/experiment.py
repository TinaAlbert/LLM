"""DS-1011已批准小额实验参数与工具解析；不执行工具、不另建费用账本。"""
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import re

from .budget import EXPERIMENT_REQUEST_CEILING, EXPERIMENT_ACCOUNTED_CEILING
from .model import ROOT, MODEL, MODEL_REVISION, ModelConfig, ModelUnavailable, _json_object, _today

MAX_INPUT_BYTES = 12 * 1024
PAGES = ('welcome', 'profile', 'trips', 'booking', 'approvals')
NAVIGATION_TOOL = {'type': 'function', 'function': {
    'name': 'navigate', 'description': '打开当前用户有权查看的差旅页面分区；不付款、不审批。',
    'parameters': {'type': 'object', 'properties': {
        'page': {'type': 'string', 'enum': list(PAGES)}},
        'required': ['page'], 'additionalProperties': False}}}


def approval_values(checked_on):
    return {'package': 'DS-1011', 'approved': True, 'approved_on': '2026-10-15',
        'source_kind': 'explicit_user_reply', 'accepted_billing_uncertainty': True,
        'account_source_kind': 'user_reported', 'additional_fee_cny_reported': '0',
        'request_ceiling': EXPERIMENT_REQUEST_CEILING,
        'accounted_ceiling_cny': str(EXPERIMENT_ACCOUNTED_CEILING),
        'price_checked_on': checked_on, 'input_per_million': '2',
        'cache_hit_per_million': '0.04', 'output_per_million': '8',
        'price_source': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'}


def load_approval(path=None):
    target = Path(path) if path is not None else ROOT / '.local/guide-experiment.json'
    try:
        if target.stat().st_size > 4096:
            raise ModelUnavailable('experiment_not_approved')
        value = _json_object(target.read_text(encoding='utf-8'))
    except (OSError, UnicodeError):
        raise ModelUnavailable('experiment_not_approved') from None
    expected = approval_values(_today())
    if value != expected or any(type(value.get(k)) is not type(v) for k, v in expected.items()):
        raise ModelUnavailable('experiment_not_approved')
    return value


def credentials(path, approval):
    # 只复用原凭证及固定服务身份，不把旧账户字段伪写为供应商已核验。
    target = Path(path) if path is not None else ROOT / '.local/guide-model.json'
    try:
        if target.stat().st_size > 8192:
            raise ModelUnavailable('config_invalid')
        source = _json_object(target.read_text(encoding='utf-8'))
    except (OSError, UnicodeError):
        raise ModelUnavailable('credentials_missing') from None
    if (source.get('package') != 'DS-1007' or source.get('model') != MODEL
            or source.get('model_revision') != MODEL_REVISION or source.get('service') != 'deepseek-official'
            or type(source.get('account_ref')) is not str
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', source['account_ref'])
            or type(source.get('api_key')) is not str or not 16 <= len(source['api_key']) <= 512
            or any(char.isspace() for char in source['api_key'])):
        raise ModelUnavailable('config_outside_package')
    return ModelConfig(source['api_key'], source['account_ref'], approval['price_checked_on'],
                       'user_reported', approval['price_source'], Decimal(0))


def validate_messages(messages):
    if type(messages) is not list or not 1 <= len(messages) <= 12:
        raise ModelUnavailable('input_rejected')
    pending = set()
    for m in messages:
        if type(m) is not dict:
            raise ModelUnavailable('input_rejected')
        role = m.get('role')
        if role == 'tool':
            if (set(m) != {'role', 'tool_call_id', 'content'} or m['tool_call_id'] not in pending
                    or type(m['content']) is not str):
                raise ModelUnavailable('input_rejected')
            pending.remove(m['tool_call_id'])
        elif role == 'assistant' and m.get('tool_calls'):
            if pending or set(m) != {'role', 'content', 'tool_calls'}:
                raise ModelUnavailable('input_rejected')
            parsed = parse_response({'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': m}]}, True, False)
            pending = {c['id'] for c in parsed['tool_calls']}
        else:
            if (pending or set(m) != {'role', 'content'} or role not in ('system', 'user', 'assistant')
                    or type(m['content']) is not str or not m['content']):
                raise ModelUnavailable('input_rejected')
    if pending:
        raise ModelUnavailable('input_rejected')


def input_size_bytes(value):
    """统一既定限额的messages/tools UTF-8计数，不含宿主及provider附加字段。"""
    return len(json.dumps({'messages': value['messages'], 'tools': value['tools']},
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def _q18_delivery_gate(adapter, scope_hash, grant_path):
    """第二次精确请求发送前重核正式首答、独立SQL/内容复核与已结算用量。"""
    from .budget import BudgetUnavailable
    from .knowledge import fingerprint
    try:
        if Path(grant_path).resolve() != adapter.ledger.q18_path.resolve():
            raise ModelUnavailable('q18_scope_invalid')
        adapter.ledger.check_q18_scope(scope_hash)
    except BudgetUnavailable as error:
        raise ModelUnavailable(error.reason) from None
    try:
        grant = _json_object(Path(grant_path).read_bytes())
        if scope_hash != grant['scope_hashes'][1]:
            return
        directory = Path(grant_path).parent / 'd3-26'
        def read(name):
            target = directory / name
            if target.stat().st_size > 1024 * 1024:
                raise ValueError()
            return _json_object(target.read_bytes())
        sample_path = directory / 'sample-1.json'
        sample = read('sample-1.json')
        sql = read('sample-1-sql-check.json')
        checked = read('sample-1-reviewed.json')
        raw = read('raw-result-1.json')
        row = adapter.ledger.receipt_snapshot()[24]
        expected_usage = {'prompt_tokens': row['prompt_tokens'],
            'completion_tokens': row['completion_tokens'],
            'total_tokens': row['prompt_tokens'] + row['completion_tokens'],
            'cached_tokens': row['cache_hit_tokens'], 'cache_miss_tokens': row['cache_miss_tokens']}
        if (sample['http'] != 200 or sample['business_unchanged'] is not True
                or sql['passed'] is not True or sql['sample_sha256'] != hashlib.sha256(sample_path.read_bytes()).hexdigest()
                or any(checked[key] is not True for key in ('passed', 'known_usage',
                    'business_unchanged', 'independent_sql', 'content_reviewed'))
                or checked['scope_hash'] != grant['scope_hashes'][0]
                or checked['reply_hash'] != fingerprint(sample['reply'])
                or row['state'] != 'settled' or raw['ledger_row_matched'] is not True
                or raw['scope_hash'] != grant['scope_hashes'][0]
                or raw['ledger_request_id'] != row['request_id']
                or raw['provider_request_id_sha256'] != hashlib.sha256(row['provider_request_id'].encode()).hexdigest()
                or type(raw['usage']) is not dict or raw['usage'] != expected_usage
                or any(type(raw['usage'].get(key)) is not int for key in expected_usage)
                or raw['amount_cny'] != row['accounted_cny']):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, IndexError, ModelUnavailable):
        raise ModelUnavailable('q18_first_delivery_not_verified') from None


def prepare(adapter, experiment_input, approval_path, *, with_authorization=False):
    approval = load_approval(approval_path)
    if not callable(adapter.input_validator):
        raise ModelUnavailable('input_validator_unavailable')
    try:
        # 独立宿主先筛选；复制以免校验后输入被修改。字节限额不是token上界。
        value = json.loads(json.dumps(adapter.input_validator(experiment_input),
                                     ensure_ascii=False, allow_nan=False))
    except Exception:
        raise ModelUnavailable('input_rejected') from None
    if type(value) is not dict or set(value) != {'messages', 'tools', 'max_tokens', 'json_output'}:
        raise ModelUnavailable('input_rejected')
    messages, tools = value['messages'], value['tools']
    if (type(value['max_tokens']) is not int or value['max_tokens'] not in (512, 1024)
            or type(value['json_output']) is not bool
            or (value['max_tokens'] == 1024 and not value['json_output'])
            or (tools != [] and tools != [NAVIGATION_TOOL]) or (tools and value['json_output'])):
        raise ModelUnavailable('input_rejected')
    validate_messages(messages)
    if input_size_bytes(value) > MAX_INPUT_BYTES:
        raise ModelUnavailable('experiment_input_too_large')
    config = adapter.config if adapter.config is not None else credentials(adapter.config_path, approval)
    if config.additional_fee_cny != Decimal(0):
        raise ModelUnavailable('experiment_fee_changed')
    payload = {'model': MODEL, 'messages': messages, 'thinking': {'type': 'disabled'},
               'stream': False, 'max_tokens': value['max_tokens']}
    if value['json_output']:
        payload['response_format'] = {'type': 'json_object'}
    if tools:
        payload.update(tools=tools, tool_choice='auto')
    if with_authorization:
        permit = None
        q16_path = getattr(adapter, 'q16_grant_path', None)
        q17_path = getattr(adapter, 'q17_grant_path', None)
        q18_path = getattr(adapter, 'q18_grant_path', None)
        q19_path = getattr(adapter, 'q19_grant_path', None)
        q20_path = getattr(adapter, 'q20_grant_path', None)
        q21_path = getattr(adapter, 'q21_grant_path', None)
        q22_path = getattr(adapter, 'q22_grant_path', None)
        q23_path = getattr(adapter, 'q23_grant_path', None)
        q24_path = getattr(adapter, 'q24_grant_path', None)
        q25_path = getattr(adapter, 'q25_grant_path', None)
        q26_path = getattr(adapter, 'q26_grant_path', None)
        q27_path = getattr(adapter, 'q27_grant_path', None)
        q28_path = getattr(adapter, 'q28_grant_path', None)
        q29_path = getattr(adapter, 'q29_grant_path', None)
        q30_path = getattr(adapter, 'q30_grant_path', None)
        q31_path = getattr(adapter, 'q31_grant_path', None)
        q32_path = getattr(adapter, 'q32_grant_path', None)
        q33_path = getattr(adapter, 'q33_grant_path', None)
        q34_path = getattr(adapter, 'q34_grant_path', None)
        q35_path = getattr(adapter, 'q35_grant_path', None)
        q36_path = getattr(adapter, 'q36_grant_path', None)
        if sum(path is not None for path in (q16_path, q17_path, q18_path, q19_path, q20_path, q21_path, q22_path, q23_path, q24_path, q25_path, q26_path, q27_path, q28_path, q29_path, q30_path, q31_path, q32_path, q33_path, q34_path, q35_path, q36_path)) > 1:
            raise ModelUnavailable('batch_grant_conflict')
        if any(path is not None for path in (q19_path, q20_path, q21_path, q22_path, q23_path, q24_path, q25_path, q26_path, q27_path, q28_path, q29_path, q30_path, q31_path, q32_path, q33_path, q34_path, q35_path, q36_path)):
            from .budget import BudgetUnavailable
            from .task_budget import _Q19Permit, check_q19_permit
            batch = 'Q36' if q36_path is not None else 'Q35' if q35_path is not None else 'Q34' if q34_path is not None else 'Q33' if q33_path is not None else 'Q32' if q32_path is not None else 'Q31' if q31_path is not None else 'Q30' if q30_path is not None else 'Q29' if q29_path is not None else 'Q28' if q28_path is not None else 'Q27' if q27_path is not None else 'Q26' if q26_path is not None else 'Q25' if q25_path is not None else 'Q24' if q24_path is not None else 'Q23' if q23_path is not None else 'Q22' if q22_path is not None else 'Q21' if q21_path is not None else 'Q20' if q20_path is not None else 'Q19'
            grant_path = q36_path if q36_path is not None else q35_path if q35_path is not None else q34_path if q34_path is not None else q33_path if q33_path is not None else q32_path if q32_path is not None else q31_path if q31_path is not None else q30_path if q30_path is not None else q29_path if q29_path is not None else q28_path if q28_path is not None else q27_path if q27_path is not None else q26_path if q26_path is not None else q25_path if q25_path is not None else q24_path if q24_path is not None else q23_path if q23_path is not None else q22_path if q22_path is not None else q21_path if q21_path is not None else q20_path if q20_path is not None else q19_path
            authorizer = getattr(adapter, batch.lower() + '_authorizer')
            try:
                if value['max_tokens'] != 512 or value['json_output'] or not callable(authorizer):
                    raise ModelUnavailable(batch.lower() + '_scope_invalid')
                # scope.authorize_actual 必须重新逐字核对已经常规校验的实际输入。
                permit = authorizer(value)
                if (type(permit) is not _Q19Permit or permit.batch != batch
                        or permit.grant_path.resolve() != Path(grant_path).resolve()):
                    raise ModelUnavailable(batch.lower() + '_scope_invalid')
                from .knowledge import fingerprint
                if permit.binding.actual_hash != fingerprint(value):
                    raise ModelUnavailable(batch.lower() + '_actual_input_changed')
                check_q19_permit(adapter.ledger, permit, batch=batch)
            except BudgetUnavailable as error:
                raise ModelUnavailable(error.reason) from None
            return config, payload, permit
        grant_path = q18_path if q18_path is not None else q17_path if q17_path is not None else q16_path
        if grant_path is not None:
            from .assistant import corporate_export_scope
            from .budget import _q16_permit_for_validated_input, _q17_permit_for_validated_input, _q18_permit
            batch = 'q18' if q18_path is not None else 'q17' if q17_path is not None else 'q16'
            if value['max_tokens'] != 512 or value['json_output']:
                raise ModelUnavailable(batch + '_scope_invalid')
            try:
                if q17_path is not None or q18_path is not None:
                    context = json.loads(messages[-1]['content'])
                    if (len(messages) != 2 or messages[0]['role'] != 'system'
                            or messages[1]['role'] != 'user' or context.get('history') != []):
                        raise ValueError()
                scope_hash = corporate_export_scope(value)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ModelUnavailable(batch + '_scope_invalid') from None
            if q18_path is not None:
                _q18_delivery_gate(adapter, scope_hash, grant_path)
            factory = (_q18_permit if q18_path is not None else _q17_permit_for_validated_input
                       if q17_path is not None else _q16_permit_for_validated_input)
            permit = factory(scope_hash, grant_path)
        return config, payload, permit
    return config, payload


def parse_response(body, allow_tools, json_output):
    choices = body.get('choices')
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise ModelUnavailable('candidate_invalid')
    choice = choices[0]
    message = choice.get('message')
    if (type(choice.get('index')) is not int or choice['index'] != 0
            or type(message) is not dict or message.get('role') != 'assistant'
            or message.get('reasoning_content') or message.get('refusal') or message.get('function_call')):
        raise ModelUnavailable('candidate_invalid')
    finish, calls, content = choice.get('finish_reason'), message.get('tool_calls'), message.get('content')
    if finish == 'length':
        raise ModelUnavailable('output_truncated')
    if finish == 'tool_calls' and allow_tools:
        if type(calls) is not list or len(calls) != 1 or type(calls[0]) is not dict:
            raise ModelUnavailable('tool_call_invalid')
        call = calls[0]
        function = call.get('function')
        # 部分兼容实现给非流式tool call附带index；仅接受唯一调用的整数0。
        if set(call) not in ({'id','type','function'}, {'id','type','function','index'}):
            raise ModelUnavailable('tool_fields_invalid')
        if 'index' in call and (type(call['index']) is not int or call['index'] != 0):
            raise ModelUnavailable('tool_index_invalid')
        if (call['type'] != 'function'
                or type(call['id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', call['id'])
                or type(function) is not dict or set(function) != {'name', 'arguments'}
                or function['name'] != 'navigate' or type(function['arguments']) is not str):
            raise ModelUnavailable('tool_call_invalid')
        args = _json_object(function['arguments'])
        if set(args) != {'page'} or type(args['page']) is not str or args['page'] not in PAGES:
            raise ModelUnavailable('tool_arguments_invalid')
        if content is not None and type(content) is not str:
            raise ModelUnavailable('candidate_invalid')
        return {'content': content or '', 'tool_calls': [{'id': call['id'], 'name': 'navigate', 'arguments': args}]}
    if finish != 'stop' or calls or type(content) is not str or not content:
        raise ModelUnavailable('candidate_invalid')
    if len(content.encode('utf-8')) > 16384:
        raise ModelUnavailable('candidate_oversized_or_empty')
    if json_output:
        _json_object(content)
    return {'content': content, 'tool_calls': []}
