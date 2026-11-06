"""Q17控制器离线反例；仅临时文件/fake ledger，不访问本机许可、PG或模型。"""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from scripts import round24_live as module


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def case(tmp_path, monkeypatch):
    out, prev = tmp_path / 'round24', tmp_path / 'round23'
    out.mkdir(); prev.mkdir()
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'OUT', out)
    monkeypatch.setattr(module, 'PREV', prev)
    knowledge = tmp_path / 'old-knowledge.json'
    knowledge.write_text('synthetic historical knowledge', encoding='utf-8')
    monkeypatch.setattr(module, 'KNOWLEDGE', knowledge)
    values = []
    for index, question in enumerate(module.QUESTIONS, 1):
        payload = {'messages': [{'role': 'system', 'content': 'synthetic public system'},
            {'role': 'user', 'content': json.dumps({'question': question, 'history': [],
                'current_state': {'contract_version': 'assistant-shared-1', 'source_kind': 'observed'}}, ensure_ascii=False)}],
            'tools': [], 'max_tokens': 512, 'json_output': False}
        preview = {'payload': payload, 'scope_hash': module.corporate_export_scope(payload),
                   'input_bytes': module.input_size_bytes(payload)}
        values.append(preview)
        put(prev / f'consult-preview-{index}.json', preview)
    monkeypatch.setattr(module, 'EXPECTED_BYTES', tuple(v['input_bytes'] for v in values))
    scopes = [v['scope_hash'] for v in values]
    document = {'package': 'Q17', 'approved': True, 'source_kind': 'explicit_user_reply',
        'approved_on': '2026-10-18', 'source_reply': '批准Q17：最多新增2次并外发上述两份精确合成字段',
        'destination': module.ENDPOINT, 'max_physical_requests': 2, 'original_used': 23,
        'scoped_ceiling': 25, 'max_input_bytes': 12288, 'max_output_tokens_each': 512,
        'failure_stops_remaining': True, 'automatic_retry': False, 'scope_hashes': scopes}
    put(out / 'decision.json', document)
    put(prev / 'recovery-review.json', {'scope_hashes': scopes, 'current_targeted_guard_replay_passed': True})
    data = {'ledger_id': 'synthetic-ledger', 'label': '含中文的身份字段',
            'requests': [{'request_id': str(i), 'state': 'settled'} for i in range(23)]}
    ledger_path = tmp_path / '.local/guide-usage.json'
    put(ledger_path, data)
    q16_path = ledger_path.with_name(ledger_path.name + '.q16.json')
    q16_stop = ledger_path.with_name(ledger_path.name + '.q16-stopped.json')
    q16_path.write_text('synthetic old grant', encoding='utf-8')
    q16_stop.write_text('synthetic permanent old stop', encoding='utf-8')
    baseline = {'files': {'ledger_request_hashes': [module.digest(r) for r in data['requests']],
        'ledger_identity': module.digest({k:v for k,v in data.items() if k != 'requests'})},
        'ledger_sidecars': {'.local/' + p.name: module.sha(p) for p in (q16_path, q16_stop)}}
    put(out / 'snapshot-before.json', baseline)
    calls, stops, count = [], [], [23]
    fake = SimpleNamespace(path=ledger_path, q16_path=q16_path, q16_stop_path=q16_stop,
        q17_path=tmp_path / '.local/guide-usage.json.q17.json',
        summary=lambda: {'physical_requests': count[0], 'unresolved_requests': 0},
        create_q17_grant=lambda scopes:calls.append(tuple(scopes)),
        check_q17_scope=lambda scope:True, stop_q17_batch=lambda reason:stops.append(reason))
    monkeypatch.setattr(module, 'ledger', lambda:fake)
    monkeypatch.setattr(module, 'load_approval', lambda:None)
    monkeypatch.setattr(module, 'authorize_shared_export', lambda *args:None)
    releases = SimpleNamespace(active=lambda:{'prompt_version':'assistant-6', 'model_revision':'historical'})
    monkeypatch.setattr(module, 'copy_knowledge', lambda:releases)
    return SimpleNamespace(out=out, prev=prev, values=values, scopes=scopes, decision=document,
        ledger=fake, data=data, calls=calls, stops=stops, count=count, monkeypatch=monkeypatch)


def preflight(case):
    put(case.out / 'preflight-controller-result.json', {'complete':True, 'real_model':False, 'samples':[{},{}]})
    for name in ('preflight-server-transport-guard.json', 'preflight-controller-transport-guard.json'):
        put(case.out / name, {'model_send_attempts':0, 'budget_reserve_attempts':0})
    (case.out / 'preflight-server.stop').write_text('normal', encoding='utf-8')
    for index, preview in enumerate(case.values, 1):
        put(case.out / f'preflight-preview-{index}.json', preview)
        path = case.out / f'preflight-sample-{index}.json'
        put(path, {'http':200, 'business_unchanged':True, 'independent_sql':False})
        put(case.out / f'preflight-sample-{index}-sql-check.json', {'passed':True, 'sample_sha256':module.sha(path)})


@pytest.mark.parametrize('field,value', [('approved',1), ('max_physical_requests',True),
    ('source_reply','继续完成'), ('automatic_retry',True), ('scoped_ceiling',26)])
def test_exact_explicit_decision_cannot_be_inferred_or_relaxed(case, field, value):
    case.decision[field] = value
    put(case.out / 'decision.json', case.decision)
    with pytest.raises(RuntimeError, match='q17_explicit_decision_mismatch'):
        module.decision()
    assert case.calls == []


def test_current_preflight_required_before_grant(case):
    with pytest.raises(FileNotFoundError):module.init()
    assert case.calls == []


def test_valid_preflight_can_only_create_new_grant_once(case):
    preflight(case)
    old = (case.ledger.path.read_bytes(), case.ledger.q16_path.read_bytes(), case.ledger.q16_stop_path.read_bytes())
    module.init()
    assert case.calls == [tuple(case.scopes)]
    assert old == (case.ledger.path.read_bytes(), case.ledger.q16_path.read_bytes(), case.ledger.q16_stop_path.read_bytes())
    with pytest.raises(RuntimeError, match='q17_initialization_already_recorded'):module.init()
    assert len(case.calls) == 1


def test_changed_current_sample_cannot_use_saved_sql_verdict(case):
    preflight(case)
    put(case.out / 'preflight-sample-1.json', {'http':200,'business_unchanged':True,'changed':True})
    with pytest.raises(RuntimeError, match='q17_current_preflight_failed'):module.init()
    assert case.calls == []


@pytest.mark.parametrize('change', ['prefix', 'identity', 'old_stop'])
def test_prefix_and_old_q16_permanent_stop_are_bound(case, change):
    assert module.prefix_state()['physical_requests'] == 23
    if change == 'prefix':
        case.data['requests'][3]['state'] = 'changed'
        put(case.ledger.path, case.data)
    elif change == 'identity':
        case.data['ledger_id'] = 'another'
        put(case.ledger.path, case.data)
    else:case.ledger.q16_stop_path.write_text('changed', encoding='utf-8')
    with pytest.raises(RuntimeError):module.prefix_state()
    assert case.calls == []


def test_changed_live_input_never_uses_approved_scope(case):
    value = deepcopy(case.values[0]['payload'])
    context = json.loads(value['messages'][-1]['content'])
    context['history'] = [{'source_kind':'user_reported','text':'new history'}]
    value['messages'][-1]['content'] = json.dumps(context, ensure_ascii=False)
    with pytest.raises(RuntimeError, match='q17_live_scope_changed'):module.validate_payload(value)


def test_second_question_requires_review_of_exact_delivered_first_reply(case):
    sample = {'reply': {'reply':'synthetic delivered text', 'session_id':'synthetic'}}
    put(case.out / 'sample-1-reviewed.json', {'passed':True, 'scope_hash':case.scopes[0],
        'reply_hash':module.fingerprint(sample['reply'])})
    module.require_first_review(sample, case.scopes[0])
    sample['reply']['reply'] = 'modified'
    with pytest.raises(RuntimeError, match='q17_first_independent_review_failed'):
        module.require_first_review(sample, case.scopes[0])


def live_app(case, *, fail=False):
    put(case.out / 'initialization.json', {'synthetic':True})
    raw = case.out / 'synthetic-raw-report.json'; put(raw, {'status':'received'})
    async def infer(payload, kind, version):
        if fail:raise RuntimeError('synthetic send failure')
        case.count[0] += 1
        request_id = 'synthetic-provider-' + str(case.count[0])
        reservation_id = 'synthetic-reservation-' + str(case.count[0])
        data = module.read(case.ledger.path)
        # 第二题单独门测试跳过第一题推理，但账本仍有其合成占位。
        while len(data['requests']) < case.count[0] - 1:
            data['requests'].append({'request_id':'synthetic-first-placeholder','state':'settled'})
        data['requests'].append({'request_id':reservation_id,'state':'settled',
            'provider_request_id':request_id,'accounted_cny':'0.00036',
            'prompt_tokens':100,'completion_tokens':20,'cache_hit_tokens':0,'cache_miss_tokens':100})
        put(case.ledger.path, data)
        return module.ModelResult({'content':'synthetic','tool_calls':[]},
            module.TokenUsage(100,20,120,0,100),request_id,Decimal('0.00036'),reservation_id), raw
    assistant = SimpleNamespace(external_model=True, infer=infer)
    app = SimpleNamespace(state=SimpleNamespace(assistant=assistant), get=lambda path:lambda f:f)
    case.monkeypatch.setattr(module, 'configuration', lambda:({},SimpleNamespace()))
    case.monkeypatch.setattr(module, 'create_app', lambda settings:app)
    case.monkeypatch.setattr(module.sandbox, 'check_cluster', lambda:{'instance_id':'synthetic'})
    module.configure()
    return assistant


def test_formal_inference_records_raw_separately_and_refuses_replay(case):
    assistant = live_app(case)
    payload = case.values[0]['payload']
    asyncio.run(assistant.infer(payload, 'chat', 'synthetic-version'))
    assert (case.out / 'attempt-1.json').is_file() and (case.out / 'raw-result-1.json').is_file()
    assert not (case.out / 'sample-1.json').exists()
    with pytest.raises(RuntimeError, match='q17_attempt_order_changed'):
        asyncio.run(assistant.infer(payload, 'chat', 'synthetic-version'))
    assert case.stops == ['assistant_inference_failed']


def test_failed_inference_stops_remaining_new_batch(case):
    assistant = live_app(case, fail=True)
    with pytest.raises(RuntimeError, match='synthetic send failure'):
        asyncio.run(assistant.infer(case.values[0]['payload'], 'chat', 'synthetic-version'))
    assert case.stops == ['assistant_inference_failed']
    assert (case.out / 'attempt-1.json').exists()
    assert not (case.out / 'raw-result-1.json').exists()


def test_direct_second_scope_cannot_bypass_delivery_review_gate(case):
    assistant = live_app(case)
    case.count[0] = 24
    with pytest.raises(FileNotFoundError):
        asyncio.run(assistant.infer(case.values[1]['payload'], 'chat', 'synthetic-version'))
    assert case.stops == ['assistant_inference_failed']
    assert not (case.out / 'attempt-2.json').exists()


def test_second_send_gate_requires_sql_and_exact_reviewed_first_http(case):
    assistant = live_app(case)
    case.count[0] = 24
    first = {'http':200, 'business_unchanged':True, 'reply':{'reply':'synthetic first delivery'}}
    path = case.out / 'sample-1.json'; put(path, first)
    put(case.out / 'sample-1-sql-check.json', {'passed':True, 'sample_sha256':module.sha(path)})
    put(case.out / 'sample-1-reviewed.json', {'passed':True, 'scope_hash':case.scopes[0],
        'reply_hash':module.fingerprint(first['reply'])})
    asyncio.run(assistant.infer(case.values[1]['payload'], 'chat', 'synthetic-version'))
    assert (case.out / 'attempt-2.json').is_file() and (case.out / 'raw-result-2.json').is_file()
    assert case.count[0] == 25 and not case.stops


@pytest.mark.parametrize('field,value', [('provider_request_id','wrong-provider'),
    ('request_id','wrong-reservation'),('accounted_cny','0.00037'),('cache_miss_tokens',99)])
def test_raw_usage_cannot_be_associated_with_another_or_different_ledger_row(case, field, value):
    assistant = live_app(case)
    asyncio.run(assistant.infer(case.values[0]['payload'],'chat','synthetic-version'))
    saved = module.read(case.out/'raw-result-1.json')
    assert saved['ledger_row_matched'] and saved['ledger_request_id']=='synthetic-reservation-24'
    data = module.read(case.ledger.path); data['requests'][23][field]=value; put(case.ledger.path,data)
    result = module.ModelResult({'content':'synthetic','tool_calls':[]},module.TokenUsage(100,20,120,0,100),
        'synthetic-provider-24',Decimal('0.00036'),'synthetic-reservation-24')
    with pytest.raises(RuntimeError,match='q17_result_ledger_usage_mismatch'):
        module.verify_settled_result(result,1)
