"""22轮只读原数据/旧运行物保护与静态交付核查；不调用模型或reset。"""
import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.llm_round19 import protection, sha
from scripts.d3_evaluate import business_snapshot
from scripts.llm_round17 import terminal
from services.business.config import load_settings
from services.guide.windows import ntfs_runtime, exploration_executable
OUT = ROOT / '.local/d3-22'
OLD_NATIVE = [ROOT / '.local/guide-runtime.json', ROOT / '.local/d2-01-isolation.json',
              ntfs_runtime() / 'shell.exe']

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def snapshot(phase):
    value = {'captured_at': datetime.now(timezone.utc).isoformat(), 'files': protection(),
        'dev': business_snapshot(load_settings()), 'test': business_snapshot(load_settings(test=True)),
        'terminal': terminal(), 'old_native': {str(p): sha(p) for p in OLD_NATIVE}}
    value['previous_rounds'] = {p.relative_to(ROOT).as_posix(): sha(p)
        for number in (19,20,21) for p in (ROOT / f'.local/d3-{number}').rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and 'sandbox' not in p.parts}
    prior = read(ROOT / '.local/d3-21/snapshot-after.json')
    compare = prior if phase == 'before' else read(OUT / 'snapshot-before.json')
    a,b = value['files'],compare['files']
    value['checks'] = {
        'business_unchanged': all(value[k]['sha256']==compare[k]['sha256'] for k in ('dev','test')),
        'authentication_unchanged': all(value[k]['authentication']==compare[k]['authentication'] for k in ('dev','test')),
        'protected_files': all(a['protected'].get(k)==h for k,h in b['protected'].items()),
        'ledger_all_22_unchanged': a['ledger_request_hashes']==b['ledger_request_hashes'],
        'ledger_identity': a['ledger_identity']==b['ledger_identity'],
        'old_candidates': a['candidate_hashes']==b['candidate_hashes'], 'active_release': a['active']==b['active']}
    if phase=='after':
        value['checks'].update(previous_rounds=value['previous_rounds']==compare['previous_rounds'],
                               old_native=value['old_native']==compare['old_native'])
    assert a['budget']['physical_requests']==22 and a['budget']['unresolved_requests']==0
    assert len(value['dev']['tables'])==len(value['test']['tables'])==23
    assert value['dev']['tables']['tickets']['rows']==6
    assert all(value['checks'].values()), 'preservation_failed'
    OUT.mkdir(parents=True,exist_ok=True)
    with (OUT / f'snapshot-{phase}.json').open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,default=str)
    print(json.dumps({'phase':phase,'checks':value['checks'],'budget':a['budget'],
        'active':a['active'],'previous_files':len(value['previous_rounds'])},ensure_ascii=False))

def audit():
    before,after=(read(OUT / f'snapshot-{p}.json') for p in ('before','after'))
    assert all(after['checks'].values())
    for collection in (before['files']['protected'],before['previous_rounds']):
        assert all(sha(ROOT/p)==h for p,h in collection.items())
    assert all(sha(Path(p))==h for p,h in before['old_native'].items())
    listing=subprocess.check_output(['git','-c',f"safe.directory={ROOT.as_posix()}",'ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT)
    paths=sorted({p.decode('utf-8') for p in listing.split(bytes([0])) if p})
    secrets=[read(ROOT/'.local/guide-model.json')['api_key'],*read(ROOT/'.local/business.json').values(),
             *read(ROOT/'.local/d3-20/sandbox/business.json').values()]
    matches=[p for p in paths if any(v.encode() in (ROOT/p).read_bytes() for v in secrets if len(v)>8)]
    assert not matches,matches
    py=[p for p in paths if p.endswith('.py')]
    for p in py: ast.parse((ROOT/p).read_text(encoding='utf-8-sig'),filename=p)
    md=[p for p in paths if p.endswith('.md')]; links=0
    for name in md:
        p=ROOT/name; content=p.read_text(encoding='utf-8-sig')
        assert sum(line.startswith('```') for line in content.splitlines())%2==0,name
        for m in re.finditer(r'\]\(([^)]+)\)',content):
            target=m.group(1).strip().strip('<>')
            if re.match(r'^[a-zA-Z][\w+.-]*:',target) or target.startswith('#'): continue
            target=unquote(target.split('#',1)[0])
            if target: links+=1; assert (p.parent/target).exists(),(name,target)
    from services.guide.budget import BudgetLedger
    budget=BudgetLedger(ROOT/'.local/guide-usage.json').summary()
    assert budget==before['files']['budget']==after['files']['budget']
    runtime=read(OUT/'runtime-final.json'); assert runtime['listeners']==[]
    assert runtime['original_postgres_stop_exit']==runtime['sandbox_postgres_stop_exit']==0
    assert runtime['api_exit']==0
    native=read(OUT/'native-isolation.json')
    assert native['passed'] is False and native['appcontainer_started'] and native['child_reaped']
    assert native['binary_sha256']==sha(exploration_executable())
    gate=read(OUT/'default-gate-check.json'); assert gate['expected_rejection'] and gate['business_unchanged']
    run=read(OUT/'exploration/latest-summary.json')
    assert run['complete'] and run['native_process_used'] and run['native_engineering']
    assert not run['executor_isolation_accepted'] and not run['engineering_stub'] and run['model_calls']==0
    review=read(ROOT/run['run_directory']/'candidate-review.json')
    assert review['independent_truth']=='passed' and review['source_integrity']=='matched' and not review['publishable']
    q16=read(OUT/'q16-review.json'); assert q16['approved'] is False and q16['physical_requests_requested']==2
    from services.guide.experiment import input_size_bytes,MAX_INPUT_BYTES
    for number in (1,2):
        preview=read(OUT/f'consult-preview-{number}.json')
        assert not preview['approved'] and input_size_bytes(preview['payload'])==preview['input_bytes']<=MAX_INPUT_BYTES
        text=json.dumps(preview['payload'],ensure_ascii=False)
        assert not re.search(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',text,re.I)
    guard=read(OUT/'consult-transport-guard.json')
    assert guard['model_send_attempts']==guard['budget_reserve_attempts']==0

    value={'deliverables':len(paths),'python_ast':len(py),'markdown':len(md),'local_links':links,
        'secret_matches':matches,'preservation':after['checks'],'budget':budget,'model_calls_by_audit':0,
        'runtime_stopped':True,'default_gate_closed':True,'native_engineering_run':True,
        'full_isolation_accepted':False,'q16_approved':False,'model_send_attempts':0}
    (OUT/'delivery-audit.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('phase',choices=('before','after','audit'))
    phase=parser.parse_args().phase
    audit() if phase=='audit' else snapshot(phase)
