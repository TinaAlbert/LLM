"""Q18新批双目标恢复；独立26证据、正式Assistant和同一连续账本。"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import psycopg

from scripts import round20_sandbox as sandbox
from scripts.d3_evaluate import NoModelTraffic, business_snapshot
from scripts.llm_round19 import digest
from scripts.round21_explore import configuration, ordinary_session, require, sql_evidence, write
from scripts.round23_consult import check_sql
from scripts.round25_consult import QUESTIONS
from services.business.app import create_app
from services.business.manage import reset_test_database
from services.guide.assistant import authorize_shared_export, corporate_export_scope
from services.guide.budget import BudgetLedger, BudgetUnavailable
from services.guide.experiment import MAX_INPUT_BYTES, input_size_bytes, load_approval
from services.guide.knowledge import fingerprint
from services.guide.model import DeepSeekModelAdapter, ModelResult, ModelUnavailable, MODEL_REVISION, TokenUsage
from services.guide.releases import Releases

OUT = ROOT / '.local/d3-26'
PREV = ROOT / '.local/d3-25/consult-ready'
KNOWLEDGE = ROOT / '.local/d3-22/consult-releases.json'
ENDPOINT = 'https://api.deepseek.com/chat/completions'
BASE_REQUESTS = 24
BINDING = '/__q18/binding'
EXPECTED_BYTES = (10984, 11329)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def ledger():
    return BudgetLedger(ROOT / '.local/guide-usage.json')


def decision():
    value = read(OUT / 'decision.json')
    expected = {'package': 'Q18', 'approved': True, 'source_kind': 'explicit_user_reply',
        'approved_on': '2026-10-18', 'source_reply': '批准Q18紧凑512：上述两份精确合成输入，最多新增2次，会计预留共0.064元',
        'destination': ENDPOINT, 'max_physical_requests': 2, 'original_used': BASE_REQUESTS,
        'scoped_ceiling': 26, 'max_input_bytes': 12288, 'max_output_tokens_each': 512,
        'failure_stops_remaining': True, 'automatic_retry': False,
        'accounting_reserve_each_cny': '0.032', 'accounting_reserve_total_cny': '0.064'}
    require(type(value) is dict and set(value) == set(expected) | {'scope_hashes'} and
            all(type(value[k]) is type(v) and value[k] == v for k, v in expected.items()),
            'q18_explicit_decision_mismatch')
    require(type(value['scope_hashes']) is list and len(value['scope_hashes']) == 2 and
            len(set(value['scope_hashes'])) == 2, 'q18_decision_scopes_invalid')
    return value


def approved_previews():
    values = [read(PREV / f'consult-preview-{index}.json') for index in (1, 2)]
    scopes = decision()['scope_hashes']
    review = read(PREV.parent / 'recovery-review.json')
    require(review['real_model_calls'] == 0 and review['new_grant_created'] is False and
            [item['scope_hash'] for item in review['samples']] == scopes and
            all(item['independent_saved_sql'] is True and item['business_unchanged'] is True
                for item in review['samples']), 'q18_review_changed')
    for index, preview in enumerate(values):
        checked = review['samples'][index]
        require(checked['preview_file_sha256'] == sha(PREV / f'consult-preview-{index + 1}.json') and
                checked['input_bytes'] == preview['input_bytes'] and
                checked['max_tokens'] == 512 and checked['history'] == 0, 'q18_review_preview_binding_changed')
        payload = preview['payload']
        context = json.loads(payload['messages'][-1]['content'])
        require(preview['scope_hash'] == scopes[index] == corporate_export_scope(payload) and
                input_size_bytes(payload) == preview['input_bytes'] == EXPECTED_BYTES[index] <= MAX_INPUT_BYTES,
                'q18_approved_preview_changed')
        require(payload['max_tokens'] == 512 and payload['json_output'] is False and
                context['question'] == QUESTIONS[index] and context['goal'] == QUESTIONS[0] and context['history'] == [],
                'q18_approved_preview_contract_changed')
    return values


def prefix_state():
    """只比较前24笔、账本身份和所有旧侧车；不打印原始账本/凭证。"""
    value = read(ledger().path)
    baseline = read(OUT / 'snapshot-before.json')['files']
    require(len(value['requests']) >= BASE_REQUESTS and
            [digest(row) for row in value['requests'][:BASE_REQUESTS]] == baseline['ledger_request_hashes'] and
            digest({k: v for k, v in value.items() if k != 'requests'}) == baseline['ledger_identity'],
            'q18_original_24_or_identity_changed')
    require(ledger().q16_stop_path.is_file() and ledger().q17_stop_path.is_file(),
            'q18_old_batch_stop_missing')
    for relative, expected in read(OUT / 'snapshot-before.json')['ledger_sidecars'].items():
        if relative != '.local/guide-usage.json':
            require(sha(ROOT / relative) == expected, 'q18_old_batch_sidecar_changed')
    return ledger().summary()


def copy_knowledge():
    target = OUT / 'releases.json'
    if target.exists():
        require(target.read_bytes() == KNOWLEDGE.read_bytes(), 'q18_knowledge_copy_changed')
    else:
        OUT.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(KNOWLEDGE.read_bytes())
    releases = Releases('test', target)
    require(releases.active()['prompt_version'] == 'assistant-6', 'q18_original_assistant_six_required')
    return releases


def validate_payload(payload):
    previews = approved_previews()
    scope = corporate_export_scope(payload)
    scopes = [p['scope_hash'] for p in previews]
    require(scope in scopes, 'q18_live_scope_changed')
    index = scopes.index(scope)
    context = json.loads(payload['messages'][-1]['content'])
    require(input_size_bytes(payload) == previews[index]['input_bytes'] and
            payload['max_tokens'] == 512 and payload['json_output'] is False and
            context['history'] == [] and context['question'] == QUESTIONS[index] and
            context['goal'] == QUESTIONS[0],
            'q18_live_payload_contract_changed')
    return index + 1, scope


def init():
    require(not (OUT / 'initialization.json').exists() and not (OUT / 'attempt-1.json').exists(),
            'q18_initialization_already_recorded')
    values = approved_previews()
    preparation = read(OUT / 'preflight-controller-result.json')
    require(preparation['complete'] and preparation['real_model'] is False and len(preparation['samples']) == 2,
            'q18_current_preflight_missing')
    for name in ('preflight-server-transport-guard.json', 'preflight-controller-transport-guard.json'):
        guard = read(OUT / name)
        require(guard['model_send_attempts'] == guard['budget_reserve_attempts'] == 0,
                'q18_preflight_model_or_budget_attempt')
    require((OUT / 'preflight-server.stop').exists(), 'q18_preflight_server_not_stopped')
    copy_knowledge()
    for index, preview in enumerate(values, 1):
        current = read(OUT / f'preflight-preview-{index}.json')
        require(current['scope_hash'] == preview['scope_hash'] and
                current['input_bytes'] == preview['input_bytes'] and
                corporate_export_scope(current['payload']) == preview['scope_hash'],
                'q18_current_preview_mismatch')
        sample = read(OUT / f'preflight-sample-{index}.json')
        sql_check = read(OUT / f'preflight-sample-{index}-sql-check.json')
        require(sample['http'] == 200 and sample['business_unchanged'] and sql_check['passed'] is True and
                sql_check['sample_sha256'] == sha(OUT / f'preflight-sample-{index}.json'),
                'q18_current_preflight_failed')
    budget = prefix_state()
    require(budget['physical_requests'] == BASE_REQUESTS and budget['unresolved_requests'] == 0,
            'q18_initial_ledger_mismatch')
    load_approval()
    for preview in values:
        authorize_shared_export(preview['payload'], OUT / 'shared-export-approval.json')
    ledger().create_q18_grant(tuple(p['scope_hash'] for p in values))
    save('initialization.json', {'package': 'Q18', 'source_kind': 'explicit_user_reply',
        'scope_hashes': [p['scope_hash'] for p in values], 'original_requests': BASE_REQUESTS,
        'ordinary_ceiling': 22, 'scoped_ceiling': 26, 'knowledge_sha256': sha(KNOWLEDGE),
        'current_preview_sha256': [sha(OUT / f'preflight-preview-{i}.json') for i in (1, 2)],
        'usage_ledger': 'original_continuous', 'old_q16_still_stopped': True, 'old_q17_still_stopped': True,
        'accounting_reserve_each_cny': '0.032', 'accounting_reserve_total_cny': '0.064', 'model_calls': 0})
    print('Q18新批精确许可已登记；原24笔和Q16/Q17停止保持，模型0。', flush=True)


def stop_batch(reason):
    ledger().stop_q18_batch(reason)


def verify_settled_result(result, index):
    row = read(ledger().path)['requests'][BASE_REQUESTS + index - 1]
    usage = result.usage
    require(row['request_id'] == result.reservation_id and row['state'] == 'settled' and
            row['provider_request_id'] == result.request_id and
            Decimal(row['accounted_cny']) == result.amount_cny and
            row['prompt_tokens'] == usage.prompt_tokens and row['completion_tokens'] == usage.completion_tokens and
            row['cache_hit_tokens'] == usage.cached_tokens and row['cache_miss_tokens'] == usage.cache_miss_tokens and
            usage.total_tokens == usage.prompt_tokens + usage.completion_tokens,
            'q18_result_ledger_usage_mismatch')
    return {'ledger_request_id': result.reservation_id,
            'provider_request_id_sha256': hashlib.sha256(result.request_id.encode()).hexdigest(),
            'usage': asdict(usage), 'amount_cny': str(result.amount_cny), 'ledger_row_matched': True}


def configure(*, preflight=False):
    name = 'preflight-server' if preflight else 'server'
    require(not any((OUT / p).exists() for p in (name + '.json', name + '.stop')),
            'q18_server_already_recorded_no_restart')
    require(not (OUT / ('preflight-preview-1.json' if preflight else 'attempt-1.json')).exists(),
            'q18_server_attempt_already_recorded')
    values = approved_previews()
    before = prefix_state()
    require(before['physical_requests'] == BASE_REQUESTS and before['unresolved_requests'] == 0,
            'q18_server_initial_ledger_mismatch')
    if not preflight:
        require((OUT / 'initialization.json').is_file(), 'q18_initialization_missing')
        load_approval()
        ledger().check_q18_scope(values[0]['scope_hash'])
    _, settings = configuration()
    app = create_app(settings)
    assistant = app.state.assistant
    assistant.releases = copy_knowledge()
    assistant.report_dir = OUT / ('preflight-runs' if preflight else 'live-runs')
    assistant.shared_export_approval_path = OUT / 'shared-export-approval.json'
    if preflight:
        assistant.external_model = False
        old_status = assistant.status
        assistant.status = lambda who: {**old_status(who), 'ready': True, 'reason': None}
        def factory(validator):
            async def generate(payload, deadline):
                validator(payload)
                index, scope = validate_payload(payload)
                save(f'preflight-preview-{index}.json', {'approved_by': 'Q18_explicit_user_reply',
                    'external_sent': False, 'scope_hash': scope, 'input_bytes': input_size_bytes(payload),
                    'max_tokens': 512, 'payload': payload, 'assistant_source_sha256': sha(ROOT / 'services/guide/assistant.py')})
                reply = ('两个明确委托的行程共用同一张券，预报价中的优惠只是估算，并未确定最终归属。'
                    '请比较当前基础金额和优惠额度后决定锁价顺序，再核对另一行程的最终金额。'
                    '未执行分支仍需核对报价、库存、期限和资格；当前尚未付款或取得有效票据。') if index == 1 else (
                    '共享券当前由trip_2锁价占用，预报价只是估算，另一行程需在后续锁价核对最终金额。'
                    '如要重排，只能在未支付且业务允许取消时取消原锁价，再核对券、库存、期限和资格；'
                    '已消耗券不能通过取消恢复。当前两项目标尚未完成。')
                return ModelResult({'content': reply, 'tool_calls': []}, TokenUsage(100, 40, 140, 0, 100),
                                   'offline-only', Decimal('0'), 'offline-only')
            return SimpleNamespace(generate_experiment=generate)
        assistant.adapter_factory = factory
    else:
        require(assistant.external_model is True, 'q18_formal_assistant_required')
        assistant.adapter_factory = lambda validator: DeepSeekModelAdapter(input_validator=validator,
            q18_grant_path=ledger().q18_path, protocol_report_dir=OUT / 'protocol')
        original = assistant.infer
        async def one_shot(payload, kind, version):
            try:
                index, scope = validate_payload(payload)
                require(kind == 'chat', 'q18_chat_only')
                authorize_shared_export(payload, OUT / 'shared-export-approval.json')
                budget = prefix_state()
                require(budget['physical_requests'] == BASE_REQUESTS + index - 1 and
                        budget['unresolved_requests'] == 0, 'q18_attempt_order_changed')
                if index == 2:
                    first = read(OUT / 'sample-1.json')
                    sql_check = read(OUT / 'sample-1-sql-check.json')
                    require(first['http'] == 200 and first['business_unchanged'] and
                            sql_check['passed'] is True and
                            sql_check['sample_sha256'] == sha(OUT / 'sample-1.json'),
                            'q18_first_delivery_or_sql_not_verified')
                    require_first_review(first, values[0]['scope_hash'])
                ledger().check_q18_scope(scope)
                save(f'attempt-{index}.json', {'scope_hash': scope, 'input_hash': fingerprint(payload),
                    'sample': index, 'kind': kind, 'release_version': version, 'started_at_epoch': time.time(),
                    'requests_before': budget['physical_requests'], 'automatic_retry': False})
                save(f'payload-live-{index}.json', {'scope_hash': scope,
                    'input_bytes': input_size_bytes(payload), 'payload': payload})
                result, path = await original(payload, kind, version)
                after = prefix_state()
                require(after['physical_requests'] == BASE_REQUESTS + index and after['unresolved_requests'] == 0,
                        'q18_usage_not_settled')
                settled_reference = verify_settled_result(result, index)
                save(f'raw-result-{index}.json', {'report': str(path.relative_to(ROOT)),
                    'report_sha256': sha(path), 'raw_model_received': True, 'scope_hash': scope,
                    'serving_model_revision': MODEL_REVISION,
                    'knowledge_model_revision': assistant.releases.active()['model_revision'],
                    'formal_http_delivery': 'recorded_separately_in_sample',
                    'independent_verdict': 'recorded_separately_in_review', **settled_reference})
                return result, path
            except BaseException:
                stop_batch('assistant_inference_failed')
                raise
        assistant.infer = one_shot
    marker = {'scope': 'P26-Q18-preflight-only' if preflight else 'P26-Q18-exact-consult-only',
              'instance': sandbox.check_cluster()['instance_id'], 'runtime': uuid4().hex,
              'scope_hashes': [p['scope_hash'] for p in values], 'model': 'offline' if preflight else 'formal_Q18'}
    @app.get(BINDING)
    def binding():
        return marker
    save(name + '.json', marker)
    return app


def require_first_review(sample, scope):
    checked = read(OUT / 'sample-1-reviewed.json')
    require(all(checked.get(key) is True for key in ('passed', 'known_usage', 'business_unchanged',
                'independent_sql', 'content_reviewed')) and checked['scope_hash'] == scope and
            checked['reply_hash'] == fingerprint(sample['reply']), 'q18_first_independent_review_failed')


def run(*, preflight=False):
    prefix = 'preflight-' if preflight else ''
    require(not any((OUT / p).exists() for p in (prefix + 'controller-started.json',
        prefix + 'controller-result.json', prefix + 'sample-1.json')), 'q18_controller_no_restart_or_overwrite')
    config, settings = configuration()
    values = approved_previews()
    result = {'complete': False, 'real_model': not preflight, 'automatic_retry': False,
              'models_before': prefix_state(), 'samples': [], 'full_two_ticket_task_complete': False}
    guard = NoModelTraffic() if preflight else None
    try:
        with sandbox.runner_lock(), (guard.installed() if guard else nullcontext()), httpx.Client(
                base_url='http://127.0.0.1:8000', trust_env=False, follow_redirects=False, timeout=40) as client:
            save(prefix + 'controller-started.json', {'real_model': not preflight, 'automatic_retry': False})
            marker = read(OUT / (prefix + 'server.json'))
            binding = client.get(BINDING)
            require(binding.status_code == 200 and binding.json() == marker and
                    marker['scope'] == ('P26-Q18-preflight-only' if preflight else 'P26-Q18-exact-consult-only') and
                    marker['instance'] == sandbox.check_cluster()['instance_id'], 'q18_server_binding_mismatch')
            with ordinary_session(client, settings):
                pass
            require(prefix_state()['physical_requests'] == BASE_REQUESTS, 'q18_initial_ledger_changed')
            if not preflight:
                ledger().check_q18_scope(values[0]['scope_hash'])
            reset_test_database(config['test_database_url'], confirmation='llm_business_test', fixture='two-trips-one-coupon')
            with ordinary_session(client, settings) as headers:
                write(client, headers, '/payment-profile', {'source_ref': 'demo_personal_alice', 'expected_revision': 0}, 'PUT')
                ids = [write(client, headers, '/trips', {'flight_id': 'DEMO-' + size,
                    'travel_date': '2026-10-01', 'passengers': 1})['id'] for size in ('LARGE', 'SMALL')]
                quotes = [write(client, headers, f'/trips/{ref}/quotes', {'expected_revision': 1}) for ref in ids]
                session = None
                for index, question in enumerate(QUESTIONS, 1):
                    if index == 2:
                        if not preflight:
                            until = time.monotonic() + 600
                            while not (OUT / 'sample-1-reviewed.json').exists():
                                require(time.monotonic() < until, 'q18_review_timeout_no_second_request')
                                time.sleep(.2)
                            require_first_review(result['samples'][0], values[0]['scope_hash'])
                            ledger().check_q18_scope(values[1]['scope_hash'])
                        write(client, headers, f"/quotes/{quotes[1]['id']}/lock")
                    before = business_snapshot(settings)
                    check_sql(sql_evidence(settings), ids, changed=index == 2)
                    body = {'message': question, 'page': 'trips', 'trip_id': ids[0], 'delegated_trip_ids': ids,
                        'observation': {'page': 'trips', 'title': '我的行程',
                        'items': ['暂估报价不会占用优惠券，最终金额以成功锁价为准。'], 'product_version': 'v1'}}
                    if session:
                        body['session_id'] = session
                    response = client.post('/assistant/chat', headers=headers, json=body)
                    after = business_snapshot(settings)
                    evidence = sql_evidence(settings)
                    budget = prefix_state()
                    item = {'sample': index, 'http': response.status_code, 'reply': response.json(),
                        'business_before': before, 'business_after': after, 'sql': evidence,
                        'business_unchanged': before['sha256'] == after['sha256'], 'budget': budget,
                        'real_model': not preflight, 'independent_sql': False,
                        'independent_content_verdict': 'pending', 'raw_model_result': None if preflight else f'raw-result-{index}.json'}
                    result['samples'].append(item)
                    save(prefix + f'sample-{index}.json', item)
                    require(response.status_code == 200, 'q18_chat_failed_stop')
                    require(item['business_unchanged'], 'q18_consult_wrote_business')
                    check_sql(evidence, ids, changed=index == 2)
                    require(budget['physical_requests'] == BASE_REQUESTS + (0 if preflight else index) and
                            budget['unresolved_requests'] == 0, 'q18_usage_unknown_or_count_changed')
                    require('tool' not in item['reply'] and item['reply'].get('reply'), 'q18_empty_or_unrequested_navigation')
                    # 独立SQL判据是单独证据；不回写已保存HTTP原件。
                    save(prefix + f'sample-{index}-sql-check.json', {'passed': True,
                        'sample_sha256': sha(OUT / (prefix + f'sample-{index}.json')),
                        'scope_hash': values[index - 1]['scope_hash'], 'unpaid_unticketed': True})
                    item['independent_sql'] = True
                    session = item['reply']['session_id']
                    print(f'Q18_{"PREFLIGHT" if preflight else "LIVE"}_SAMPLE_{index}_HTTP_200_SETTLED_BUSINESS_UNCHANGED', flush=True)
                    if index == 1 and not preflight:
                        print('Q18_WAIT_INDEPENDENT_CONTENT_REVIEW', flush=True)
                result['complete'] = True
    except BaseException as error:
        result['failure_type'] = type(error).__name__
        if not preflight:
            stop_batch('controller_failed')
        raise
    finally:
        result['models_after'] = prefix_state()
        if guard:
            save('preflight-controller-transport-guard.json', {'model_send_attempts': guard.send_attempts,
                'budget_reserve_attempts': guard.reserve_attempts})
        save(prefix + 'controller-result.json', result)
    print('Q18当前两样本离线重建完成，模型0。' if preflight else
          'Q18两份正式咨询HTTP交付完成；独立内容结论另存，未实际取得两张票。', flush=True)


def server(*, preflight=False):
    import uvicorn

    prefix = 'preflight-' if preflight else ''
    guard = NoModelTraffic() if preflight else None
    try:
        with guard.installed() if guard else nullcontext():
            app = configure(preflight=preflight)
            instance = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8000,
                access_log=False, proxy_headers=False, log_level='warning'))
            async def serve():
                task = asyncio.create_task(instance.serve())
                while not task.done():
                    if (OUT / (prefix + 'server.stop')).exists():
                        instance.should_exit = True
                    await asyncio.sleep(.2)
                await task
            asyncio.run(serve())
    finally:
        if guard:
            save('preflight-server-transport-guard.json', {'model_send_attempts': guard.send_attempts,
                'budget_reserve_attempts': guard.reserve_attempts})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight-server', 'prepare', 'init', 'server', 'run'))
    action = parser.parse_args().action
    try:
        if action == 'preflight-server': server(preflight=True)
        elif action == 'prepare': run(preflight=True)
        elif action == 'init': init()
        elif action == 'server': server()
        else: run()
    except (KeyboardInterrupt, AssertionError, RuntimeError, ValueError, KeyError, OSError,
            BudgetUnavailable, ModelUnavailable, psycopg.Error, httpx.HTTPError):
        raise SystemExit('Q18停止；检查26专用脱敏证据，不自动重试、重开旧Q16/Q17或输出凭证。') from None
