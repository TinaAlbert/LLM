"""P23 双目标恢复的精确离线预览；仅 server/prepare，不产生模型许可。"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import round20_sandbox as sandbox
from scripts.d3_evaluate import NoModelTraffic, business_snapshot, ledger_fingerprint
from scripts.round21_explore import configuration, ordinary_session, require, sql_evidence, write
from services.business.manage import reset_test_database
from services.guide.assistant import corporate_export_scope
from services.guide.experiment import MAX_INPUT_BYTES, input_size_bytes
from services.guide.model import ModelResult, TokenUsage
from services.guide.releases import Releases

OUT = ROOT / '.local/d3-23/consult'
STOP = OUT / 'server.stop'
SCOPE = 'P23-consult-synthetic-ordinary-role'
DESTINATION = 'https://api.deepseek.com/chat/completions'
QUESTION_INITIAL = '我想为这两个行程都拿到票，怎样安排才能少花钱？请区分预估和已执行结果，在当前页回答，不导航。'
QUESTION_CHANGED = '我先锁定了第二个行程。现在券属于谁，还能调整顺序吗？请核对占用与付款状态，在当前页回答，不导航。'
QUESTIONS = (QUESTION_INITIAL, QUESTION_CHANGED)
PREPARATION_FILES = tuple(
    f'{kind}-{index}.json'
    for index in (1, 2) for kind in ('consult-preview', 'consult-sample')
) + ('consult-preparation.json', 'prepare-transport-guard.json', 'prepare-started.json')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(name, value):
    """预览和失败证据均只创建一次，中断后也不能悄悄覆盖。"""
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def require_fresh_preparation():
    require(not any((OUT / name).exists() for name in PREPARATION_FILES),
            'round23_consult_preparation_already_exists_no_overwrite')


def configure():
    from scripts.round23_explore import configure_server

    require_fresh_preparation()
    require(not any((OUT / name).exists() for name in (
        'server.json', 'consult-releases.json', 'server-transport-guard.json', 'server.stop')),
        'round23_consult_server_already_exists_no_overwrite')
    app, settings = configure_server(marker_directory=OUT, scope=SCOPE)
    release_path = OUT / 'consult-releases.json'
    # 逐字保留22已准备的assistant-6及历史知识构建修订，不重新创建候选。
    source = ROOT / '.local/d3-22/consult-releases.json'
    with release_path.open('xb') as stream:
        stream.write(source.read_bytes())
    releases = Releases('test', release_path)
    require(releases.active()['prompt_version'] == 'assistant-6',
            'round23_consult_original_assistant_six_required')
    assistant = app.state.assistant
    assistant.releases = releases
    assistant.external_model = False
    assistant.report_dir = OUT / 'offline-runs'
    original_status = assistant.status
    assistant.status = lambda who: {**original_status(who), 'ready': True, 'reason': None}

    def factory(validator):
        async def generate(payload, deadline):
            validator(payload)
            context = json.loads(payload['messages'][-1]['content'])
            require(context['question'] in QUESTIONS, 'round23_consult_question_not_reviewed')
            index = QUESTIONS.index(context['question']) + 1
            size = input_size_bytes(payload)
            require(size <= MAX_INPUT_BYTES and payload['max_tokens'] == 512,
                    'round23_consult_input_or_output_limit')
            require(context.get('history') == [], 'round23_consult_history_not_empty')
            state = context['current_state']
            require(state['contract_version'] == 'assistant-shared-1' and
                    len(state['delegated_trips']) == 2, 'round23_consult_shared_scope_mismatch')
            rows = state['delegated_trips']
            require([row['ref'] for row in rows] == ['trip_1', 'trip_2'] and
                    all(row['data'].get('payment') is None and row['data'].get('ticket') is None
                        for row in rows), 'round23_consult_unexpected_payment_or_ticket')
            coupon = state['shared_coupon']
            require(coupon['status'] == ('available' if index == 1 else 'held'),
                    'round23_consult_coupon_state_mismatch')
            require(index == 1 or coupon['trip_ref'] == 'trip_2',
                    'round23_consult_coupon_attribution_mismatch')
            require(all(row['data'].get('quote') and row['data']['quote']['valid'] for row in rows),
                    'round23_consult_valid_quotes_required')
            save(f'consult-preview-{index}.json', {
                'approved': False, 'new_explicit_approval_required': True,
                'old_q16_not_reusable': True, 'destination': DESTINATION,
                'scope_hash': corporate_export_scope(payload), 'input_bytes': size,
                'max_tokens': 512, 'payload': payload, 'model': 'offline_substitute',
                'assistant_source_sha256': hashlib.sha256(
                    (ROOT / 'services/guide/assistant.py').read_bytes()).hexdigest(),
                'knowledge_source': '.local/d3-22/consult-releases.json',
                'knowledge_source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'knowledge_model_revision': releases.active()['model_revision'],
            })
            reply = (
                '两个明确委托的行程共用同一张券，预报价中的优惠只是估算，并未确定最终归属。'
                '请比较当前基础金额和优惠额度后决定锁价顺序，再核对另一行程的最终金额。'
                '未执行分支仍需核对报价、库存、期限和资格；当前尚未付款或取得有效票据。'
            ) if index == 1 else (
                '共享券当前由trip_2锁价占用，预报价只是估算，另一行程需在后续锁价核对最终金额。'
                '如要重排，只能在未支付且业务允许取消时取消原锁价，再核对券、库存、期限和资格；'
                '已消耗券不能通过取消恢复。当前两项目标尚未完成。'
            )
            return ModelResult({'content': reply, 'tool_calls': []}, TokenUsage(100, 40, 140, 0, 100),
                               'offline-only', Decimal('0'), 'offline-only')
        return SimpleNamespace(generate_experiment=generate)

    assistant.adapter_factory = factory
    return app, settings


def check_sql(evidence, trip_ids, *, changed):
    require(len(evidence['trips']) == 2 and
            {str(row['id']) for row in evidence['trips']} == set(trip_ids) and
            all(row['owner_id'] == 'personal_alice' for row in evidence['trips']),
            'round23_consult_independent_trip_scope_failed')
    require(all(not evidence[key] for key in (
        'payments', 'payment_jobs', 'provider_payments', 'ticket_tasks', 'tickets')),
        'round23_consult_independent_not_unpaid_or_unticketed')
    require(len(evidence['coupons']) == 1 and
            evidence['coupons'][0]['status'] == ('held' if changed else 'available'),
            'round23_consult_independent_coupon_failed')
    if changed:
        require(len(evidence['locks']) == 1 and str(evidence['locks'][0]['trip_id']) == trip_ids[1] and
                evidence['locks'][0]['status'] == 'held' and
                evidence['coupons'][0]['lock_id'] == evidence['locks'][0]['id'],
                'round23_consult_independent_second_lock_failed')
    else:
        require(not evidence['locks'], 'round23_consult_independent_initial_lock_found')


def prepare():
    require_fresh_preparation()
    config, settings = configuration()
    guard = NoModelTraffic()
    ledger_before = ledger_fingerprint()
    result = {'complete': False, 'checks': [], 'models': 0, 'external_approved': False,
              'old_q16_not_reusable': True, 'requested_physical_requests': 2,
              'new_explicit_approval_required': True, 'http': 'real_tcp_127.0.0.1_8000',
              'sql': 'independent_55433_read_only', 'automatic_retry': False}
    with sandbox.runner_lock():
        require_fresh_preparation()
        save('prepare-started.json', {'mode': 'offline_only', 'external_approved': False})
        try:
            with guard.installed(), httpx.Client(
                    base_url='http://127.0.0.1:8000', trust_env=False,
                    follow_redirects=False, timeout=35) as client:
                marker = read(OUT / 'server.json')
                require(marker.get('scope') == SCOPE and
                        marker.get('instance') == sandbox.check_cluster()['instance_id'],
                        'round23_consult_server_marker_mismatch')
                binding = client.get('/__round23/binding')
                require(binding.status_code == 200 and binding.json() == marker,
                        'not_round23_consult_synthetic_server')
                # 先证实真实HTTP会话落在55433，再允许合成test重置。
                with ordinary_session(client, settings):
                    pass
                reset_test_database(config['test_database_url'], confirmation='llm_business_test',
                                    fixture='two-trips-one-coupon')
                with ordinary_session(client, settings) as headers:
                    write(client, headers, '/payment-profile',
                          {'source_ref': 'demo_personal_alice', 'expected_revision': 0}, 'PUT')
                    trip_ids = [write(client, headers, '/trips', {
                        'flight_id': 'DEMO-' + size, 'travel_date': '2026-10-01', 'passengers': 1,
                    })['id'] for size in ('LARGE', 'SMALL')]
                    quotes = [write(client, headers, f'/trips/{ref}/quotes', {'expected_revision': 1})
                              for ref in trip_ids]
                    session = None
                    for index, question in enumerate(QUESTIONS, 1):
                        if index == 2:
                            write(client, headers, f"/quotes/{quotes[1]['id']}/lock")
                        before = business_snapshot(settings)
                        body = {'message': question, 'page': 'trips', 'trip_id': trip_ids[0],
                                'delegated_trip_ids': trip_ids, 'observation': {
                                    'page': 'trips', 'title': '我的行程',
                                    'items': ['暂估报价不会占用优惠券，最终金额以成功锁价为准。'],
                                    'product_version': 'v1',
                                }}
                        if session:
                            body['session_id'] = session
                        response = client.post('/assistant/chat', headers=headers, json=body)
                        after = business_snapshot(settings)
                        evidence = sql_evidence(settings)
                        sample = {'question': question, 'request': body, 'http': response.status_code,
                                  'reply': response.json(), 'business_before': before,
                                  'business_after': after, 'business_unchanged': before['sha256'] == after['sha256'],
                                  'sql': evidence, 'model': 'offline_substitute', 'real_model': False}
                        save(f'consult-sample-{index}.json', sample)
                        require(response.status_code == 200, 'round23_consult_offline_chat_failed')
                        require(sample['business_unchanged'], 'round23_consult_wrote_business')
                        require('tool' not in sample['reply'], 'round23_consult_unrequested_navigation')
                        check_sql(evidence, trip_ids, changed=index == 2)
                        preview = read(OUT / f'consult-preview-{index}.json')
                        require(not preview['approved'] and preview['scope_hash'] ==
                                corporate_export_scope(preview['payload']), 'round23_consult_preview_changed')
                        session = sample['reply']['session_id']
                        result['checks'].append({'sample': index, 'http': 200, 'business_unchanged': True,
                                                 'independent_sql': True, 'scope_hash': preview['scope_hash'],
                                                 'input_bytes': preview['input_bytes'], 'history': 0})
                require(ledger_before == ledger_fingerprint(), 'round23_consult_ledger_changed')
                require(guard.send_attempts == guard.reserve_attempts == 0,
                        'round23_consult_model_or_budget_attempt')
                result['complete'] = True
        except BaseException as error:
            result['failure_type'] = type(error).__name__
            raise
        finally:
            result['ledger_unchanged'] = ledger_before == ledger_fingerprint()
            save('prepare-transport-guard.json', {'model_send_attempts': guard.send_attempts,
                 'budget_reserve_attempts': guard.reserve_attempts, 'ledger_unchanged': result['ledger_unchanged']})
            save('consult-preparation.json', result)
    print('两份新离线预览完成真实HTTP/SQL核对；模型0，需新的明确费用及外发授权。')


def server():
    import uvicorn

    require_fresh_preparation()
    require(not STOP.exists() and not (OUT / 'server-transport-guard.json').exists(),
            'round23_consult_server_stop_or_guard_exists')
    guard = NoModelTraffic()
    ledger_before = ledger_fingerprint()
    try:
        with guard.installed():
            app, settings = configure()
            instance = uvicorn.Server(uvicorn.Config(
                app, host='127.0.0.1', port=8000, access_log=False,
                proxy_headers=False, log_level='warning'))

            async def serve():
                task = asyncio.create_task(instance.serve())
                while not task.done():
                    if STOP.exists():
                        instance.should_exit = True
                    await asyncio.sleep(.2)
                await task

            asyncio.run(serve())
    finally:
        save('server-transport-guard.json', {'model_send_attempts': guard.send_attempts,
             'budget_reserve_attempts': guard.reserve_attempts,
             'ledger_unchanged': ledger_before == ledger_fingerprint(), 'model': 'none'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('server', 'prepare'))
    action = parser.parse_args().action
    try:
        prepare() if action == 'prepare' else server()
    except (KeyboardInterrupt, AssertionError, RuntimeError, ValueError, KeyError, OSError,
            psycopg.Error, httpx.HTTPError):
        raise SystemExit('P23离线准备停止；保留已有证据，不自动重试或覆盖，不输出凭证。') from None
