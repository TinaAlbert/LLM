"""P26 Q18独立批原件保全与交付审计；不发送模型或重置业务。"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.llm_round19 import protection, sha, read
OUT = ROOT / '.local/d3-26'


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def historical():
    # 仅实际运行中的PG数据和追加日志可变；旧运行报告、测试原件及sandbox配置均保全。
    return {p.relative_to(ROOT).as_posix(): sha(p) for n in range(19, 26)
            for p in (ROOT / f'.local/d3-{n}').rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and not p.is_relative_to(ROOT / '.local/d3-20/sandbox/postgres-data')
            and p != ROOT / '.local/d3-20/sandbox/postgres.log'}


def snapshot(phase):
    from scripts.d3_evaluate import business_snapshot
    from scripts.llm_round17 import terminal
    from services.business.config import load_settings
    prior = read(ROOT / '.local/d3-25/snapshot-after.json') if phase == 'before' else read(OUT / 'snapshot-before.json')
    value = {'captured_at': datetime.now(timezone.utc).isoformat(), 'files': protection(),
        'dev': business_snapshot(load_settings()), 'test': business_snapshot(load_settings(test=True)),
        'terminal': terminal(), 'historical': historical(),
        'old_native': {p: sha(Path(p)) for p in prior['old_native']},
        'ledger_sidecars': {p.relative_to(ROOT).as_posix(): sha(p)
                           for p in sorted((ROOT / '.local').glob('guide-usage.json*')) if p.is_file()}}
    a, b = value['files'], prior['files']
    checks = {
        'original_business': all(value[k]['sha256'] == prior[k]['sha256'] for k in ('dev', 'test')),
        'original_authentication': all(value[k]['authentication'] == prior[k]['authentication'] for k in ('dev', 'test')),
        'original_terminal': value['terminal'] == prior['terminal'],
        'protected_files': a['protected'] == b['protected'],
        'all_24_entries': a['ledger_request_hashes'][:24] == b['ledger_request_hashes'][:24],
        'ledger_identity': a['ledger_identity'] == b['ledger_identity'],
        'old_ledger_sidecars': all(value['ledger_sidecars'].get(k) == h for k,h in prior['ledger_sidecars'].items() if k != '.local/guide-usage.json'),
        'candidate_and_active': all(a[k] == b[k] for k in ('candidate_hashes', 'active')),
        'old_native': value['old_native'] == prior['old_native'],
        'configuration': all(a['mutable_original_hashes'][k] == b['mutable_original_hashes'][k]
            for k in ('.local/guide-experiment.json', '.local/assistant-releases-test.json',
                      ))}
    if phase == 'after':
        checks['all_19_to_25_artifacts'] = value['historical'] == prior['historical']
    else:
        checks['previous_history_still_valid'] = all(value['historical'].get(k) == v for k, v in prior['historical'].items())
    assert len(value['dev']['tables']) == len(value['test']['tables']) == 23
    assert value['dev']['tables']['tickets']['rows'] == 6
    assert 24 <= a['budget']['physical_requests'] <= 26
    if phase == 'before':
        assert a['budget']['physical_requests'] == 24 and a['budget']['unresolved_requests'] == 0
        assert a['budget']['accounted_cny'] == '0.08382128'
        assert value['ledger_sidecars'] == prior['ledger_sidecars']
    else:
        assert Decimal(a['budget']['accounted_cny']) <= Decimal('0.08382128') + Decimal('0.064')
    value['checks'] = checks
    assert all(checks.values()), 'P26_preservation_failed'
    save(f'snapshot-{phase}.json', value)
    print(json.dumps({'phase': phase, 'checks': checks, 'budget': a['budget'],
                      'historical_files': len(value['historical'])}, ensure_ascii=False))


def stopped_checks():
    from services.guide.budget import BudgetLedger, BudgetUnavailable
    ledger = BudgetLedger(ROOT / '.local/guide-usage.json')
    scopes = read(ROOT / '.local/d3-24/decision.json')['scope_hashes']
    result = {}
    for label, fn, scope in (
        ('q16', ledger.check_q16_scope, scopes[1]),
        ('q17_first', ledger.check_q17_scope, scopes[0]),
        ('q17_second', ledger.check_q17_scope, scopes[1])):
        try:
            fn(scope)
        except BudgetUnavailable as error:
            result[label] = error.reason
        else:
            raise AssertionError('old_batch_reopened')
    assert result == {'q16': 'q16_batch_stopped',
                      'q17_first': 'q17_batch_stopped', 'q17_second': 'q17_batch_stopped'}
    return result



def audit():
    import hashlib
    from services.guide.budget import BudgetLedger
    from services.guide.assistant import corporate_export_scope
    from services.guide.experiment import input_size_bytes
    from services.guide.knowledge import fingerprint
    before, after = [read(OUT / f'snapshot-{p}.json') for p in ('before', 'after')]
    assert all(after['checks'].values())
    for collection in (before['historical'], before['files']['protected']):
        assert all(sha(ROOT / p) == h for p,h in collection.items())
    assert all(sha(ROOT / p) == h for p,h in before['ledger_sidecars'].items()
               if p != '.local/guide-usage.json')
    assert all(sha(Path(p)) == h for p,h in before['old_native'].items())
    listing = subprocess.check_output(['git','-c',f"safe.directory={ROOT.as_posix()}",
        'ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT)
    paths=sorted({p.decode('utf-8') for p in listing.split(bytes([0])) if p})
    secrets=[read(ROOT/'.local/guide-model.json')['api_key'],
        *read(ROOT/'.local/business.json').values(),
        *read(ROOT/'.local/d3-20/sandbox/business.json').values()]
    leaks=[p for p in paths if any(v.encode() in (ROOT/p).read_bytes() for v in secrets if len(v)>8)]
    assert not leaks
    py=[p for p in paths if p.endswith('.py')]
    md=[p for p in paths if p.endswith('.md')]
    for p in py: ast.parse((ROOT/p).read_text(encoding='utf-8-sig'),filename=p)
    links=0
    for name in md:
        p=ROOT/name;content=p.read_text(encoding='utf-8-sig')
        assert sum(line.startswith('```') for line in content.splitlines())%2==0,name
        for m in re.finditer(r'\]\(([^)]+)\)',content):
            target=m[1].strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:',target) or target.startswith('#'):continue
            target=unquote(target.split('#',1)[0])
            if target:
                links+=1
                assert (p.parent/target).exists(),(name,target)
    budget=BudgetLedger(ROOT/'.local/guide-usage.json').summary()
    assert budget==after['files']['budget']
    current=protection()
    assert current['ledger_request_hashes'][:24]==before['files']['ledger_request_hashes']
    assert current['ledger_identity']==before['files']['ledger_identity']
    assert all(current[k]==before['files'][k] for k in ('candidate_hashes','active'))
    runtime=read(OUT/'runtime-final.json')
    assert runtime['listeners']==[] and runtime['api_exit']==0
    assert runtime['original_postgres_stop_exit']==runtime['sandbox_postgres_stop_exit']==0
    decision=read(OUT/'decision.json')
    assert decision['package']=='Q18' and decision['approved'] is True
    assert decision['accounting_reserve_total_cny']=='0.064'
    controller=read(OUT/'controller-result.json')
    ledger_source=read(ROOT/'.local/guide-usage.json')
    rows=ledger_source['requests']
    added=len(rows)-24
    assert 0<=added<=2 and len(controller['samples'])<=2
    grant=read(ROOT/'.local/guide-usage.json.q18.json')
    stopped=(ROOT/'.local/guide-usage.json.q18-stopped.json').is_file()
    assert grant['package']=='Q18-P26-compact-two' and grant['approved'] is True and grant['request_ceiling']==2
    assert grant['ledger_id']==ledger_source['ledger_id'] and grant['prefix_requests']==24
    prefix=hashlib.sha256(json.dumps(rows[:24],ensure_ascii=True,allow_nan=False,
        sort_keys=True,separators=(',',':')).encode('ascii')).hexdigest()
    assert grant['prefix_sha256']==prefix and grant['scope_hashes']==decision['scope_hashes']
    assert grant['reserved_per_request_cny']=='0.032' and grant['reserved_ceiling_cny']=='0.064'
    assert added<=len(grant['intents'])<=added+1 and len(grant['intents'])<=2
    if stopped:
        stop=read(ROOT/'.local/guide-usage.json.q18-stopped.json')
        assert stop['ledger_id']==ledger_source['ledger_id'] and stop['physical_requests']==len(rows)
    for i,intent in enumerate(grant['intents'],1):
        assert type(intent['sequence']) is int and intent['sequence']==i
        assert intent['scope_hash']==decision['scope_hashes'][i-1]
        if i<=added:
            assert intent['state']=='reserved' and intent['request_id']==rows[23+i]['request_id']
        else:
            # 中断于先intent、尚未落账本的状态不伪算为已经发送或已知费用。
            assert stopped and intent['state']=='intent' and i==len(grant['intents'])
    per_request=[]
    known_accounted=Decimal(0)
    unresolved_reserve=Decimal(0)
    for i,row in enumerate(rows[24:],1):
        assert Decimal(row['reserved_cny'])==Decimal('0.032') and Decimal(row['additional_fee_cny'])==0
        assert row['state'] in ('settled','pending','unresolved')
        known=row['state']=='settled'
        usage=None;amount=None
        if known:
            names=('prompt_tokens','completion_tokens','cache_hit_tokens','cache_miss_tokens')
            assert all(type(row[key]) is int and row[key]>=0 for key in names)
            assert row['cache_hit_tokens']+row['cache_miss_tokens']==row['prompt_tokens']
            assert row['completion_tokens']<=512
            amount=(Decimal(row['cache_miss_tokens'])*Decimal('2')+
                Decimal(row['cache_hit_tokens'])*Decimal('0.04')+
                Decimal(row['completion_tokens'])*Decimal('8'))/Decimal('1000000')
            assert amount==Decimal(row['accounted_cny'])<=Decimal(row['reserved_cny'])
            known_accounted+=amount
            usage={'prompt_tokens':row['prompt_tokens'],'completion_tokens':row['completion_tokens'],
                'total_tokens':row['prompt_tokens']+row['completion_tokens'],
                'cached_tokens':row['cache_hit_tokens'],'cache_miss_tokens':row['cache_miss_tokens']}
        else:
            assert stopped and all(row[key] is None for key in ('accounted_cny','prompt_tokens',
                'completion_tokens','cache_hit_tokens','cache_miss_tokens','provider_request_id'))
            unresolved_reserve+=Decimal(row['reserved_cny'])
        raw_path=OUT/f'raw-result-{i}.json'
        receipt_path=OUT/'protocol'/('failure-receipt-'+row['request_id']+'.json')
        receipt=None
        if raw_path.exists():
            raw=read(raw_path)
            assert known and raw['raw_model_received'] is True and raw['ledger_row_matched'] is True
            assert raw['scope_hash']==decision['scope_hashes'][i-1]
            assert raw['ledger_request_id']==row['request_id']
            assert raw['provider_request_id_sha256']==hashlib.sha256(row['provider_request_id'].encode()).hexdigest()
            assert raw['usage']==usage and all(type(raw['usage'][key]) is int for key in usage)
            assert Decimal(raw['amount_cny'])==amount
            report_path=(ROOT/raw['report']).resolve()
            assert report_path.is_relative_to(OUT.resolve()) and sha(report_path)==raw['report_sha256']
            report=read(report_path)
            assert report['status']=='received' and report['usage']==usage and Decimal(report['amount_cny'])==amount
            assert report['input_hash']==fingerprint(read(OUT/f'payload-live-{i}.json')['payload'])
        elif known:
            # 已结算的候选解析失败没有ModelResult/raw-result，最小收据是其现存证据。
            assert stopped and receipt_path.is_file()
        if receipt_path.exists():
            receipt=read(receipt_path)
            assert receipt['request_id']==row['request_id'] and receipt['usage_verified'] is known
            if known:
                expected={**usage,'cache_hit_tokens':usage['cached_tokens']}
                expected.pop('cached_tokens')
                assert receipt['usage']==expected and Decimal(receipt['accounted_cny'])==amount
            else:
                assert receipt['usage'] is None and receipt['accounted_cny'] is None
        per_request.append({'sample':i,'ledger_request_id':row['request_id'],'state':row['state'],
            'usage':usage,'accounted_cny':str(amount) if known else None,
            'unresolved_reserve_cny':None if known else row['reserved_cny'],
            'raw_result_present':raw_path.exists(),'minimal_receipt_present':receipt_path.exists(),
            'failure_category':receipt['failure_category'] if receipt else None,
            'finish_reason':receipt['finish_reason'] if receipt else None})
    assert sum((Decimal(row['reserved_cny']) for row in rows[24:]),Decimal(0))<=Decimal('0.064')
    assert known_accounted+unresolved_reserve<=Decimal('0.064')
    assert Decimal(budget['accounted_cny'])==Decimal(before['files']['budget']['accounted_cny'])+known_accounted
    assert Decimal(budget['unresolved_cny'])==unresolved_reserve
    assert budget['unresolved_requests']==sum(row['state']!='settled' for row in rows[24:])
    deliveries=[]
    first_failure=None
    for i,sample in enumerate(controller['samples'],1):
        payload=read(OUT/f'payload-live-{i}.json')
        assert corporate_export_scope(payload['payload'])==payload['scope_hash']==decision['scope_hashes'][i-1]
        assert input_size_bytes(payload['payload'])==payload['input_bytes']==(10984,11329)[i-1]
        assert payload['payload']['max_tokens']==512
        assert sample['business_before']['sha256']==sample['business_after']['sha256']
        original=read(OUT/f'sample-{i}.json')
        assert original['http']==sample['http'] and original['reply']==sample['reply']
        passed=False
        if sample['http']==200:
            assert i<=added and rows[23+i]['state']=='settled' and (OUT/f'raw-result-{i}.json').is_file()
            sql=read(OUT/f'sample-{i}-sql-check.json')
            assert sql['passed'] is True and sql['sample_sha256']==sha(OUT/f'sample-{i}.json')
            review_path=OUT/f'sample-{i}-reviewed.json'
            if review_path.exists():
                review=read(review_path)
                flags=('passed','known_usage','business_unchanged','independent_sql','content_reviewed')
                assert all(type(review.get(key)) is bool for key in flags)
                assert review['reply_hash']==fingerprint(sample['reply']) and review['scope_hash']==payload['scope_hash']
                assert review['known_usage'] is True and review['business_unchanged'] is True and review['independent_sql'] is True
                passed=review['passed']
                if passed: assert all(review[key] is True for key in flags)
                else: assert stopped
            else:
                assert stopped
        else:
            assert stopped
        deliveries.append({'sample':i,'http':sample['http'],'independent_delivery_passed':passed})
        if not passed and first_failure is None:
            category='independent_content_not_passed' if sample['http']==200 else 'formal_http_failed'
            first_failure={'sample':i,'category':category,
                'adapter_failure_category':per_request[i-1]['failure_category'] if i<=added else None}
    if first_failure is None and (added>len(controller['samples']) or budget['unresolved_requests']):
        first_failure={'sample':len(controller['samples'])+1,'category':'usage_unknown_or_delivery_missing'}
    independent_passed=added==2 and len(deliveries)==2 and all(item['independent_delivery_passed'] for item in deliveries)
    if not independent_passed and first_failure is None:
        first_failure={'sample':len(deliveries)+1,'category':'controller_incomplete_or_no_delivery'}
    if controller['complete']:
        assert added==2 and budget['unresolved_requests']==0
    else:
        assert stopped
        assert len(controller['samples'])<=1 or added<=2
    if not independent_passed: assert stopped
    value={'deliverables':len(paths),'python_ast':len(py),'markdown':len(md),'local_links':links,
        'secret_matches':leaks,'preservation':after['checks'],'budget':budget,'q18_calls':added,
        'old_stops':stopped_checks(),'controller_complete':controller['complete'],
        'independent_delivery_passed':independent_passed,'deliveries':deliveries,'first_failure':first_failure,
        'new_batch_stopped':stopped,'per_request_usage_review':per_request,
        'new_known_accounted_cny':str(known_accounted),'new_unresolved_reserve_cny':str(unresolved_reserve),
        'runtime_stopped':True,'full_two_ticket_task_complete':False,'model_calls_by_audit':0}
    save('delivery-audit.json',value)
    print(json.dumps(value,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('before','after','audit'))
    action=parser.parse_args().action
    audit() if action=='audit' else snapshot(action)
