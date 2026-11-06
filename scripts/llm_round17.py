"""第17轮工程侧只读快照；标准终态与SQL不会送入模型。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.d3_evaluate import business_snapshot
from services.business.config import load_settings
from services.business.db import connect
from services.business.manage import check_marker, database_purpose
from services.guide.budget import BudgetLedger

OUT = ROOT / '.local/d3-17'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)


def preserved_sources():
    """保留旧模型记录与16证据；仅记录哈希，不复制原始业务/秘密资料。"""
    paths = sorted((ROOT / '.local/assistant-runs').glob('*.json'))
    paths += sorted((ROOT / '.local/d3-16').rglob('*'))
    paths += [ROOT / ('.local/guide-usage.json' + suffix)
              for suffix in ('', '.initialized', '.lock')]
    paths += [ROOT / '.local/guide-experiment.json', ROOT / '.local/assistant-releases-test.json']
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def terminal():
    """复用16原有效票，只读独立核对付款/任务/票据关联。"""
    prepared = read(ROOT / '.local/d3-16/browser-prepare.json')
    settings = load_settings(test=True)
    with connect(settings.database_url) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        check_marker(conn, 'test')
        assert database_purpose(conn) == 'test'
        rows = conn.execute('''SELECT t.revision,t.passengers,t.status AS trip_status,
          p.status AS payment_status,p.amount_minor,l.amount_minor AS locked_minor,
          l.trip_revision,l.status AS lock_status,pp.outcome,pp.captured_minor,
          tt.status AS task_status,k.trip_revision AS ticket_revision,k.quantity,
          k.payment_id=p.id AS ticket_payment_matches,k.trip_id=t.id AS ticket_trip_matches
          FROM trips t JOIN payments p ON (p.workspace_id,p.trip_id)=(t.workspace_id,t.id)
          JOIN booking_locks l ON (l.workspace_id,l.id)=(p.workspace_id,p.lock_id)
          JOIN provider_payments pp ON (pp.workspace_id,pp.operation_id)=(p.workspace_id,p.id)
          JOIN ticket_tasks tt ON (tt.workspace_id,tt.payment_id)=(p.workspace_id,p.id)
          JOIN tickets k ON (k.workspace_id,k.task_id)=(tt.workspace_id,tt.id)
          WHERE t.workspace_id=%s AND t.id=%s AND p.id=%s''',
          ('test', prepared['trip_id'], prepared['payment_id'])).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row['trip_status'] == 'paid'
        assert row['payment_status'] == row['outcome'] == row['task_status'] == 'succeeded'
        assert row['amount_minor'] == row['locked_minor'] == row['captured_minor'] == 110000
        assert row['revision'] == row['trip_revision'] == row['ticket_revision'] == 1
        assert row['quantity'] == row['passengers'] == 1
        assert row['lock_status'] == 'consumed'
        assert row['ticket_payment_matches'] and row['ticket_trip_matches']
        counts = {table: conn.execute(f'SELECT count(*) AS n FROM {table} WHERE workspace_id=%s',
                                     ('test',)).fetchone()['n']
                  for table in ('payments', 'ticket_tasks', 'tickets')}
        assert counts == {'payments': 1, 'ticket_tasks': 1, 'tickets': 1}
    return {'independent_sql': 'passed', 'state': row, 'counts': counts}


def snapshot(phase):
    result = {'dev': business_snapshot(load_settings()),
              'test': business_snapshot(load_settings(test=True)),
              'terminal': terminal(), 'budget': BudgetLedger(ROOT / '.local/guide-usage.json').summary(),
              'preserved_sources': preserved_sources()}
    if phase == 'after':
        before = read(OUT / 'snapshot-before.json')
        result['business_unchanged'] = all(result[key]['sha256'] == before[key]['sha256']
                                          for key in ('dev', 'test'))
        result['history_budget_release_unchanged'] = result['preserved_sources'] == before['preserved_sources']
        assert result['business_unchanged'] and result['history_budget_release_unchanged']
        assert result['budget'] == before['budget']
    save(f'snapshot-{phase}.json', result)
    print(json.dumps({'phase': phase, 'terminal': result['terminal']['counts'],
                      'tables_per_database': len(result['test']['tables']),
                      'business_unchanged': result.get('business_unchanged'),
                      'history_budget_release_unchanged': result.get('history_budget_release_unchanged'),
                      'budget': result['budget']}, ensure_ascii=False, default=str))


def replay():
    """仅重放保存输出到当前校验，不请求模型；每种已知错误独立判断。"""
    from services.guide.assistant import validate_reply, display_state
    sources = {name: read(ROOT / '.local/assistant-runs' / filename) for name, filename in {
        'original7': '708084aa178567b6cc7e51f361b89663.json',
        'original8': '6e2aaae0fe8df58fb44d9d0b5e82b29b.json',
        'corrected12': '2d10644cfcd4479de2e6b01c9b0ff7a2.json'}.items()}
    state = json.loads(sources['original7']['input']['messages'][-1]['content'])['current_state']
    assert state['data']['quote']['valid'] is True
    assert state['business_time'] < state['data']['quote']['expires_at'] < state['observed_at']
    original7, original8 = (sources[key]['candidate']['content'] for key in ('original7', 'original8'))
    fragments = [
        ('original7_full', original7, 'amount_unverified'),
        ('original7_amount', '金额 110000（币种 DEMO）', 'amount_unverified'),
        ('original7_time', '报价仍为 active 但属估算价（expires_at 已过）', 'business_time_conflict'),
        ('original8_amount', '金额 110000 DEMO', 'amount_unverified'),
        ('original8_page', '此页只说明流程，不能直接查进度', 'page_capability_unverified')]
    assert fragments[1][1] in original7 and fragments[2][1] in original7
    assert fragments[3][1] in original8 and fragments[4][1] in original8
    results = []
    for name, text, expected in fragments:
        try:
            validate_reply(text, state, prompt_version='assistant-3')
            actual = 'accepted'
        except ValueError as exc:
            actual = str(exc)
        results.append({'case': name, 'actual': actual, 'expected': expected})
    # 12号输入已是展示金额结构；守卫继续用同一笔可信原始金额事实。
    corrected = sources['corrected12']['candidate']['content']
    validate_reply(corrected, state, prompt_version='assistant-3')
    results.append({'case': 'corrected12_full', 'actual': 'accepted', 'expected': 'accepted'})
    shown = display_state(state, 'assistant-3')
    assert shown['data']['payment']['amount_display'] == '1100.00 模拟元'
    assert shown['data']['quote']['valid'] is True and shown['business_time'] == state['business_time']
    result = {'kind': 'offline_saved_response_replay', 'paid_requests': 0,
              'original_sources_unchanged': True, 'business_time': state['business_time'],
              'observed_at': state['observed_at'], 'results': results,
              'passed': all(row['actual'] == row['expected'] for row in results),
              'limitation': '有限格式守卫回放；没有生成新模型回答，也不证明任意自然语言正确。'}
    save('replay.json', result)
    print(json.dumps(result, ensure_ascii=False))
    assert result['passed']


def prepare():
    """冻结配对和条件知识输入；无模型网络入口，也不读取模型Key。"""
    import time
    from uuid import UUID
    import httpx
    from starlette.requests import Request
    from services.guide.assistant import (identity, current_state, display_state, prompt_system,
                                          TRAIN_SYSTEM)
    from services.guide.page_observation import Observation, observation_payload
    from services.guide.experiment import NAVIGATION_TOOL, validate_messages
    from services.guide.knowledge import fingerprint
    from services.guide.releases import Releases
    observations = read(OUT / 'observation17.json')['observations']
    filtered = {key: observation_payload(Observation.model_validate(value))
                for key, value in observations.items()}
    release = Releases('test').active()
    trip_id = UUID(read(ROOT / '.local/d3-16/browser-prepare.json')['trip_id'])
    settings = load_settings(test=True)
    # 仅登录/退出修改认证会话，业务读取由原授权投影完成。秘密令牌仅在内存。
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=10) as client:
        response = client.post('/demo/sessions', json={'account': 'personal_alice'})
        response.raise_for_status()
        token = response.json()['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        try:
            request = Request({'type': 'http', 'headers': [(b'authorization', headers['Authorization'].encode())],
                               'client': ('127.0.0.1', 1), 'method': 'GET', 'path': '/'})
            deadline = time.monotonic() + 20
            who = identity(settings, request, deadline)
            state, _ = current_state(settings, request, who, trip_id, deadline)
        finally:
            client.delete('/session', headers=headers).raise_for_status()
    question = read(ROOT / '.local/d3-16/browser-enhanced.json')['question']
    def chat_payload(observation):
        context = {'goal': question, 'current_page': 'booking', 'page_observation': {
                    **observation, 'source_kind': 'client_observed'},
                   'current_state': display_state(state, 'assistant-3'),
                   'allowed_pages': who.allowed_pages, 'knowledge': release['knowledge'],
                   'knowledge_hash': release['knowledge_hash'], 'history': [], 'question': question}
        return {'messages': [{'role': 'system', 'content': prompt_system('assistant-3')},
                             {'role': 'user', 'content': json.dumps(context, ensure_ascii=False, separators=(',', ':'))}],
                'tools': [NAVIGATION_TOOL], 'max_tokens': 512, 'json_output': False}
    enhanced = chat_payload(filtered['booking'])
    labels = dict(filtered['booking']); labels.pop('semantics', None)
    original = chat_payload(labels)
    old_context = json.loads(original['messages'][-1]['content'])
    new_context = json.loads(enhanced['messages'][-1]['content'])
    assert old_context == {**new_context, 'page_observation': old_context['page_observation']}
    assert original['messages'][0] == enhanced['messages'][0] and original['tools'] == enhanced['tools']
    learning_sources = [filtered[key] for key in ('profile', 'trips_before', 'trips_after')]
    learning = {'messages': [{'role': 'system', 'content': TRAIN_SYSTEM},
                            {'role': 'user', 'content': json.dumps(learning_sources, ensure_ascii=False)}],
                'tools': [], 'max_tokens': 1024, 'json_output': True}
    payloads = {'labels_only': original, 'semantics_added': enhanced, 'conditional_learning': learning}
    sizes = {}
    for key, payload in payloads.items():
        validate_messages(payload['messages'])
        sizes[key] = len(json.dumps({'messages': payload['messages'], 'tools': payload['tools']},
                                   ensure_ascii=False, separators=(',', ':')).encode())
        assert sizes[key] <= 12 * 1024
    result = {'status': 'prepared_not_called', 'paid_requests': 0, 'proposed_prompt': 'assistant-3',
              'published_release': release['version'], 'published_prompt': release['prompt_version'],
              'state_source': 'original_current_user_authorized_projection',
              'state_hash': fingerprint(state), 'observation_source': 'observation17.json',
              'only_pair_difference': 'page_observation.semantics', 'payload_bytes': sizes,
              'payloads': payloads, 'input_hashes': {key: fingerprint(value) for key, value in payloads.items()},
              'new_candidate_generated': False, 'new_release_published': False,
              'paid_blocker': 'Q12追加4次尚未明确批准；调用前须重采状态、观察并核对当日价格。'}
    save('prepared-inputs.json', result)
    print(json.dumps({key: result[key] for key in ('status','paid_requests','published_release',
                     'published_prompt','only_pair_difference','payload_bytes')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('before', 'after', 'replay', 'prepare'))
    phase = parser.parse_args().phase
    if phase in ('before', 'after'):
        snapshot(phase)
    elif phase == 'replay':
        replay()
    else:
        prepare()
