"""P23只读保护、宿主网络对照与交付审计；不调用模型或重置业务。"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.llm_round19 import protection, sha, read
from services.guide.windows import exploration_executable

OUT = ROOT / '.local/d3-23'


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def historical():
    return {p.relative_to(ROOT).as_posix(): sha(p) for number in (19, 20, 21, 22)
            for p in (ROOT / f'.local/d3-{number}').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and 'sandbox' not in p.parts}


def snapshot(phase):
    from scripts.d3_evaluate import business_snapshot
    from scripts.llm_round17 import terminal
    from services.business.config import load_settings
    prior = read(ROOT / '.local/d3-22/q16/snapshot-after.json') if phase == 'before' else read(OUT / 'snapshot-before.json')
    native_names = list(prior['old_native'])
    native_names += [str(exploration_executable())]
    sidecars = sorted((ROOT / '.local').glob('guide-usage.json*'))
    value = {'captured_at': datetime.now(timezone.utc).isoformat(), 'files': protection(),
             'dev': business_snapshot(load_settings()), 'test': business_snapshot(load_settings(test=True)),
             'terminal': terminal(), 'historical': historical(),
             'old_native': {name: sha(Path(name)) for name in native_names},
             'ledger_sidecars': {p.relative_to(ROOT).as_posix(): sha(p) for p in sidecars if p.is_file()}}
    a, b = value['files'], prior['files']
    value['checks'] = {
        'business_unchanged': all(value[k]['sha256'] == prior[k]['sha256'] for k in ('dev', 'test')),
        'authentication_unchanged': all(value[k]['authentication'] == prior[k]['authentication'] for k in ('dev', 'test')),
        'terminal_unchanged': value['terminal'] == prior['terminal'],
        'protected_files': all(a['protected'].get(k) == v for k, v in b['protected'].items()),
        'all_23_ledger_entries': a['ledger_request_hashes'] == b['ledger_request_hashes'],
        'ledger_identity': a['ledger_identity'] == b['ledger_identity'],
        'candidates': a['candidate_hashes'] == b['candidate_hashes'], 'active': a['active'] == b['active'],
        'old_native': all(value['old_native'][k] == v for k, v in prior['old_native'].items())}
    if phase == 'after':
        value['checks'].update(history=value['historical'] == prior['historical'],
                               ledger_sidecars=value['ledger_sidecars'] == prior['ledger_sidecars'])
    assert a['budget']['physical_requests'] == 23 and a['budget']['unresolved_requests'] == 0
    assert len(value['dev']['tables']) == len(value['test']['tables']) == 23
    assert value['dev']['tables']['tickets']['rows'] == 6
    assert all(value['checks'].values()), 'P23_preservation_failed'
    save(f'snapshot-{phase}.json', value)
    print(json.dumps({'phase': phase, 'checks': value['checks'], 'budget': a['budget'],
                      'historical_files': len(value['historical'])}, ensure_ascii=False))


def network_controls():
    # 使用与生产probe相同的目的地和DNS查询；不改变路由或网络策略。
    marker = bytes.fromhex('d20201000001000000000000076578616d706c6503636f6d0000010001')
    checks = []
    for host, family, label in (('8.8.8.8', socket.AF_INET, 'udp4'),
                                 ('2606:4700:4700::1111', socket.AF_INET6, 'udp6')):
        item = {'resource': label, 'host': host, 'port': 53, 'host_control': False}
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sender:
                sender.settimeout(2)
                sender.connect((host, 53))
                item['local_address'] = sender.getsockname()[0]
                sender.send(marker)
                response = sender.recv(4096)
                item['host_control'] = response[:2] == marker[:2] and len(response) >= 12 and bool(response[2] & 0x80)
                item['response_bytes'] = len(response)
        except OSError as error:
            item.update(error_type=type(error).__name__, winerror=getattr(error, 'winerror', None))
        checks.append(item)
    routes = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        'Get-NetRoute -AddressFamily IPv6 | Select-Object DestinationPrefix,InterfaceAlias,NextHop,RouteMetric | ConvertTo-Json -Depth 3'],
        capture_output=True, text=True, timeout=20)
    value = {'captured_at': datetime.now(timezone.utc).isoformat(), 'checks': checks,
             'ipv6_routes_exit': routes.returncode, 'ipv6_routes': json.loads(routes.stdout) if routes.stdout.strip() else [],
             'network_policy_changed': False, 'unreachable_is_not_denied': True}
    save('host-network-controls.json', value)
    print(json.dumps(value, ensure_ascii=False))


def audit():
    before, after = (read(OUT / f'snapshot-{p}.json') for p in ('before', 'after'))
    assert all(after['checks'].values())
    for collection in (before['historical'], before['files']['protected'], before['ledger_sidecars']):
        assert all(sha(ROOT / p) == v for p, v in collection.items())
    assert all(sha(Path(p)) == v for p, v in before['old_native'].items())
    assert protection()['budget'] == before['files']['budget'] == after['files']['budget']
    listing = subprocess.check_output(['git', '-c', f"safe.directory={ROOT.as_posix()}", 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = sorted({p.decode('utf-8') for p in listing.split(bytes([0])) if p})
    secrets = [read(ROOT / '.local/guide-model.json')['api_key'], *read(ROOT / '.local/business.json').values(),
               *read(ROOT / '.local/d3-20/sandbox/business.json').values()]
    leaks = [p for p in paths if any(v.encode() in (ROOT / p).read_bytes() for v in secrets if len(v) > 8)]
    assert not leaks, leaks
    py = [p for p in paths if p.endswith('.py')]
    for p in py:
        ast.parse((ROOT / p).read_text(encoding='utf-8-sig'), filename=p)
    md = [p for p in paths if p.endswith('.md')]
    links = 0
    for name in md:
        p = ROOT / name
        content = p.read_text(encoding='utf-8-sig')
        assert sum(line.startswith('```') for line in content.splitlines()) % 2 == 0, name
        for match in re.finditer(r'\]\(([^)]+)\)', content):
            target = match.group(1).strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            target = unquote(target.split('#', 1)[0])
            if target:
                links += 1
                assert (p.parent / target).exists(), (name, target)
    runtime = read(OUT / 'runtime-final.json')
    assert runtime['listeners'] == [] and runtime['api_exit'] == 0
    assert runtime['original_postgres_stop_exit'] == runtime['sandbox_postgres_stop_exit'] == 0
    guard = read(OUT / 'exploration/server-transport-guard.json')
    assert guard['model_send_attempts'] == guard['budget_reserve_attempts'] == 0
    consult_guard = read(OUT / 'consult/server-transport-guard.json')
    prepare_guard = read(OUT / 'consult/prepare-transport-guard.json')
    assert consult_guard['model_send_attempts'] == consult_guard['budget_reserve_attempts'] == 0
    assert prepare_guard['model_send_attempts'] == prepare_guard['budget_reserve_attempts'] == 0
    native = read(OUT / 'native-isolation.json')
    assert native['binary_sha256'] == sha(exploration_executable())
    assert native['launcher_sha256'] == sha(ROOT / 'services/guide/windows.py')
    assert native['source_sha256'] == sha(ROOT / 'services/guide/shell.c')
    if native['passed']:
        from services.guide.windows import require_exploration_evidence
        require_exploration_evidence(report_path=OUT / 'native-isolation.json')
        assert native['stage'] == 'complete' and native['appcontainer_started'] and native['child_reaped']
        assert not native['limitations']
        summary = read(OUT / 'exploration/latest-summary.json')
        run = ROOT / summary['run_directory']
        assert run.resolve().is_relative_to((OUT / 'exploration').resolve())
        assert summary == read(run / 'summary.json')
        assert summary['complete'] and summary['native_process_used'] and summary['executor_isolation_accepted']
        assert not summary['engineering_stub'] and not summary['native_engineering'] and summary['model_calls'] == 0
        controller_guard = read(run / 'controller-transport-guard.json')
        assert controller_guard['model_send_attempts'] == controller_guard['budget_reserve_attempts'] == 0
        source, final = read(run / 'observation.json'), read(run / 'final-sql.json')
        from scripts.round21_explore import evaluate
        from services.guide.exploration_runtime import digest
        trips = tuple(str(next(row['id'] for row in final['trips'] if row['flight_id'] == 'DEMO-' + size))
                      for size in ('LARGE', 'SMALL'))
        expected = evaluate('first-then-second', final, source['trace'], trips)
        verdict = summary['verdict']
        assert verdict['passed'] and all(verdict[k] == v for k, v in expected.items())
        assert verdict['source_evidence_id'] == source['evidence_id'] == summary['evidence_id']
        assert verdict['observed_final_state_hash'] == digest(source['trace'][-1]['state'])
        encoded = json.dumps(final, ensure_ascii=True, sort_keys=True, separators=(',', ':'), default=str).encode()
        assert verdict['terminal_sql_sha256'] == hashlib.sha256(encoded).hexdigest()
        candidate_review = read(run / 'candidate-review.json')
        assert candidate_review['independent_truth'] == 'passed' and candidate_review['source_integrity'] == 'matched'
        assert not candidate_review['publishable']
        assert summary['used']['operations'] <= 28 and summary['used']['http_requests'] <= 120
    else:
        gate = read(OUT / 'default-gate-check.json')
        assert gate['expected_rejection'] and gate['business_unchanged']
    review = read(OUT / 'consult/recovery-review.json')
    assert review['approved'] is False and review['model_calls'] == 0
    preparation = read(OUT / 'consult/consult-preparation.json')
    assert preparation['complete'] and preparation['models'] == 0 and preparation['ledger_unchanged']
    assert not preparation['external_approved'] and preparation['old_q16_not_reusable']
    from services.guide.assistant import corporate_export_scope
    from services.guide.experiment import input_size_bytes, MAX_INPUT_BYTES
    from scripts.round23_consult import check_sql
    for index, scope in enumerate(review['scope_hashes'], 1):
        preview = read(OUT / f'consult/consult-preview-{index}.json')
        sample = read(OUT / f'consult/consult-sample-{index}.json')
        assert preview['approved'] is False and preview['scope_hash'] == scope == corporate_export_scope(preview['payload'])
        assert input_size_bytes(preview['payload']) == preview['input_bytes'] <= MAX_INPUT_BYTES
        assert preview['max_tokens'] == 512 and preview['assistant_source_sha256'] == sha(ROOT / 'services/guide/assistant.py')
        assert sample['http'] == 200 and not sample['real_model'] and sample['business_unchanged']
        assert sample['business_before']['sha256'] == sample['business_after']['sha256']
        check_sql(sample['sql'], sample['request']['delegated_trip_ids'], changed=index == 2)
    assert sha(OUT / 'consult/consult-releases.json') == sha(ROOT / '.local/d3-22/consult-releases.json')
    replay = read(OUT / 'consult/guard-replay-current.json')
    assert replay['passed'] and replay['display_inverse_roundtrip_equal'] and replay['model_calls'] == 0
    assert replay['assistant_source_sha256'] == sha(ROOT / 'services/guide/assistant.py')
    assert replay['original_http'] == 503 and not replay['real_recovery_delivery_completed']
    value = {'deliverables': len(paths), 'python_ast': len(py), 'markdown': len(md), 'local_links': links,
             'secret_matches': leaks, 'preservation': after['checks'], 'budget': after['files']['budget'],
             'model_calls': 0, 'full_isolation_accepted': native['passed'], 'runtime_stopped': True,
             'recovery_approved': False}
    save('delivery-audit.json', value)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('before', 'after', 'network', 'audit'))
    action = parser.parse_args().action
    if action in ('before', 'after'):
        snapshot(action)
    elif action == 'network':
        network_controls()
    else:
        audit()
