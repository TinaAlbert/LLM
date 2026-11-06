"""Q16追加交付只读保护与静态核查；不调用模型、不reset。"""
import argparse
import ast
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.llm_round19 import protection,sha,read
from scripts.d3_evaluate import business_snapshot
from scripts.llm_round17 import terminal
from services.business.config import load_settings
from services.guide.budget import BudgetLedger
OUT=ROOT/'.local/d3-22/q16'

def historical():
    return {p.relative_to(ROOT).as_posix():sha(p) for number in (19,20,21,22)
        for p in (ROOT/f'.local/d3-{number}').rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and 'sandbox' not in p.parts
        and not (number==22 and p.is_relative_to(OUT))}

def snapshot(phase):
    value={'captured_at':datetime.now(timezone.utc).isoformat(),'files':protection(),
         'dev':business_snapshot(load_settings()),'test':business_snapshot(load_settings(test=True)),
         'terminal':terminal(),'historical':historical(),
         'old_native':{name:sha(Path(name)) for name in read(ROOT/'.local/d3-22/snapshot-before.json')['old_native']}}
    compare=read(ROOT/'.local/d3-22/snapshot-after.json') if phase=='before' else read(OUT/'snapshot-before.json')
    original=compare['files'];current=value['files'];key='.local/guide-model.json'
    value['checks']={
        'business_unchanged':all(value[k]['sha256']==compare[k]['sha256'] for k in ('dev','test')),
        'authentication_unchanged':all(value[k]['authentication']==compare[k]['authentication'] for k in ('dev','test')),
        'terminal_unchanged':value['terminal']==compare['terminal'],
        'protected_files':all(current['protected'].get(k)==v for k,v in original['protected'].items()
                              if phase!='before' or k!=key),
        'original_22_unchanged':current['ledger_request_hashes'][:22]==original['ledger_request_hashes'][:22],
        'ledger_identity':current['ledger_identity']==original['ledger_identity'],
        'candidates':current['candidate_hashes']==original['candidate_hashes'],
        'active':current['active']==original['active'],
        'old_native':value['old_native']==compare['old_native']}
    if phase=='before':
        value['authorized_nonsecret_config_change']={'file':key,'before_sha256':original['protected'][key],
             'after_sha256':current['protected'][key],'only_recorded_change':'model_revision to DeepSeek-V4.1-Flash-1017'}
        assert current['budget']['physical_requests']==22
    else:
        value['checks']['history']=value['historical']==compare['historical']
        assert current['budget']['physical_requests'] in (22,23,24)
    assert current['budget']['unresolved_requests']==0
    assert len(value['dev']['tables'])==len(value['test']['tables'])==23
    assert value['dev']['tables']['tickets']['rows']==6
    assert all(value['checks'].values()),'Q16_preservation_failed'
    with (OUT/f'snapshot-{phase}.json').open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,default=str)
    print(json.dumps({'phase':phase,'checks':value['checks'],'budget':current['budget'],
                    'historical_files':len(value['historical'])},ensure_ascii=False))

def audit():
    before,after=(read(OUT/f'snapshot-{p}.json') for p in ('before','after'))
    assert all(after['checks'].values())
    assert all(sha(ROOT/p)==v for p,v in before['historical'].items())
    assert all(sha(ROOT/p)==v for p,v in before['files']['protected'].items())
    listing=subprocess.check_output(['git','-c',f"safe.directory={ROOT.as_posix()}",'ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT)
    paths=sorted({p.decode('utf-8') for p in listing.split(bytes([0])) if p})
    secrets=[read(ROOT/'.local/guide-model.json')['api_key'],*read(ROOT/'.local/business.json').values(),
             *read(ROOT/'.local/d3-20/sandbox/business.json').values()]
    leaks=[p for p in paths if any(v.encode() in (ROOT/p).read_bytes() for v in secrets if len(v)>8)]
    assert not leaks,leaks
    py=[p for p in paths if p.endswith('.py')]
    for p in py:ast.parse((ROOT/p).read_text(encoding='utf-8-sig'),filename=p)
    md=[p for p in paths if p.endswith('.md')];links=0
    for name in md:
        p=ROOT/name;s=p.read_text(encoding='utf-8-sig')
        assert sum(line.startswith('```') for line in s.splitlines())%2==0,name
        for match in re.finditer(r'\]\(([^)]+)\)',s):
            target=match.group(1).strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:',target) or target.startswith('#'):continue
            target=unquote(target.split('#',1)[0])
            if target:links+=1;assert (p.parent/target).exists(),(name,target)
    runtime=read(OUT/'runtime-final.json')
    assert runtime['listeners']==[] and runtime['api_exit']==0
    assert runtime['original_postgres_stop_exit']==runtime['sandbox_postgres_stop_exit']==0
    assert runtime['data_preserved'] and runtime['q16_approved']
    controller=read(OUT/'controller-result.json')
    # Q16真实首题HTTP503，必须按失败停止，不能把raw模型返回或离线修复改记交付。
    assert not controller['complete'] and len(controller['samples'])==1
    assert controller['samples'][0]['http']==503
    assert not (OUT/'attempt-2.json').exists() and not (OUT/'payload-live-2.json').exists()
    decision=read(OUT/'decision.json');assert decision['approved'] is True
    from services.guide.assistant import corporate_export_scope
    from services.guide.experiment import input_size_bytes,MAX_INPUT_BYTES
    for index,scope in enumerate(decision['scope_hashes'][:1],1):
        payload=read(OUT/f'payload-live-{index}.json')
        assert payload['scope_hash']==scope==corporate_export_scope(payload['payload'])
        assert input_size_bytes(payload['payload'])==payload['input_bytes']<=MAX_INPUT_BYTES
        assert read(OUT/f'attempt-{index}.json')['scope_hash']==scope
        sample=read(OUT/f'sample-{index}.json')
        assert sample['http']==503 and sample['business_unchanged'] and sample['real_model']
        assert sample['budget']['physical_requests']==22+index and sample['budget']['unresolved_requests']==0
        assert 'tool' not in sample['reply']
        assert read(OUT/f'sample-{index}-reviewed.json')['passed'] is False
    runs=[read(p) for p in (OUT/'live-runs').glob('*.json')]
    assert len(runs)==1 and all(r['status']=='received' and not r['candidate']['tool_calls'] for r in runs)
    ledger=BudgetLedger(ROOT/'.local/guide-usage.json');budget=ledger.summary()
    assert budget['physical_requests']==23 and budget['unresolved_requests']==0
    assert budget==after['files']['budget'] and runtime['model_calls']==1
    assert ledger.q16_stop_path.is_file()
    assert read(OUT/'stopped-check.json')['remaining_scope_rejected']
    assert read(OUT/'guard-replay-after.json')['guard_passed_after_local_fix']
    value={'deliverables':len(paths),'python_ast':len(py),'markdown':len(md),'local_links':links,
           'secret_matches':leaks,'preservation':after['checks'],'budget':budget,
           'q16_real_model_calls':1,'q16_approved':True,'runtime_stopped':True,
           'first_delivery_http':503,'second_sample_sent':False,'remaining_scope_closed':True,
           'model_calls_by_audit':0,'full_isolation_accepted':False}
    (OUT/'delivery-audit.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('before','after','audit'))
    action=p.parse_args().action
    audit() if action=='audit' else snapshot(action)
