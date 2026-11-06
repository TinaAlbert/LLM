"""冻结/核验 V1 最终包；init-grant 只在用户对同一manifest明确批准后运行。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from services.guide.budget import BudgetLedger
from services.guide.experiment import approval_values
from services.guide.knowledge import fingerprint
from services.guide.model import _today,MODEL,MODEL_REVISION,ENDPOINT
from services.guide.releases import Releases
from services.guide.task_budget import create_q19_grant,_load_locked
from services.guide.v1_scope import BATCH, GOALS, scope_spec
from scripts.v1_task import save,read


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def task_specs(package):
    return [{k:s[k] for k in ('task_ref','case_id','group','max_rounds')}|{'scope_hash':fingerprint(s)} for s in package['scopes']]


def checked_package(directory):
    directory=Path(directory).resolve()
    if directory.parent!=(ROOT/'.local/v1-42').resolve():raise ValueError('package_location_invalid')
    manifest=read(directory/'manifest.json')
    if manifest['schema']!='v1-executable-package-1':raise ValueError('manifest_invalid')
    for name,digest in manifest['files'].items():
        path=(ROOT/name).resolve()
        if not path.is_relative_to(ROOT.resolve()) or sha(path)!=digest:raise ValueError('frozen_file_changed:'+name)
    package=read(directory/'package.json')
    if package['batch'] != BATCH or package['approved'] is not False or package['execution_ready'] is not True:raise ValueError('package_marker_invalid')
    # 当前日期价格前置失效不能自行更新价格文件或借旧收费许可继续。
    if read(directory/'experiment.json')!=approval_values(_today()):raise ValueError('price_review_expired')
    package['experiment_path']=directory/'experiment.json'
    return package


def checked_decision(directory):
    decision=read(directory/'decision.json')
    if (set(decision)!={'manifest_sha256','source_kind','question_id','reply','approved','approved_on'}
        or decision['manifest_sha256']!=sha(directory/'manifest.json')
        or decision['source_kind']!='explicit_user_reply' or decision['approved'] is not True
        or not isinstance(decision['question_id'],str) or not decision['question_id'].strip()
        or not isinstance(decision['reply'],str) or not decision['reply'].strip()
        or decision['approved_on']!=_today()):raise ValueError('matching_explicit_decision_required')
    return decision


def checked_grant(directory):
    directory=Path(directory).resolve();package=checked_package(directory);checked_decision(directory)
    ledger=BudgetLedger(ROOT/'.local/guide-usage.json')
    with ledger._locked():
        value=ledger._load();grant=_load_locked(ledger,value,batch=BATCH)
        if (grant['decision_sha256']!=sha(directory/'decision.json') or grant['task_specs']!=task_specs(package)
                or grant['intents'] or grant['batch_started_at'] is not None):raise ValueError('grant_used_or_changed')
    return grant


def freeze(directory,lab):
    directory=Path(directory).resolve();lab=Path(lab).resolve()
    if directory.exists():raise ValueError('exclusive_package_exists')
    browser=read(lab/'browser-result.json')
    if (browser['state']!='passed' or browser.get('mode')!='rehearsal' or browser.get('scripted_turns')!=2
            or read(lab/'lifecycle.json')['running_after'] is not False):raise ValueError('completed_local_evidence_required')
    ac=ROOT/'.local/v1-42/run-04'
    accounting=read(ac/'accounting-close.json')
    previous_sql=read(ac/'independent-sql.json')
    current_sql=read(lab/'initial-independent-sql.json')
    if (accounting.get('AC_passed') is not True or previous_sql.get('valid_ticket') is not True
            or fingerprint(current_sql)!=fingerprint(previous_sql)
            or read(lab/'binding.json')['trip_id']!=previous_sql['sql']['trips'][0]['id']):
        raise ValueError('actual_q35_ticket_evidence_required')
    ledger=BudgetLedger(ROOT/'.local/guide-usage.json');summary=ledger.summary()
    if summary['physical_requests']!=55 or summary['accounted_cny']!='0.20531680' or summary['unresolved_requests']!=0:raise ValueError('ledger_prefix_changed')
    if ledger.q36_path.exists() or ledger.q36_stop_path.exists():raise ValueError('q36_already_exists')
    directory.mkdir(parents=True)
    shutil.copy2(lab/'task-releases.json',directory/'releases.json')
    release=Releases('test',directory/'releases.json').active()
    observations=read(lab/'observations.json')
    package={'schema':'v1-executable-scope-1','batch':BATCH,'approved':False,'execution_ready':True,
        'scopes':[scope_spec(release,observations,i) for i in range(1,len(GOALS)+1)],'observations':observations,
        'model':MODEL,'model_revision':MODEL_REVISION,'endpoint':ENDPOINT,
        'budget':{'prefix':summary,'requests':2,'reserved_per_call_cny':'0.032','reserved_pool_cny':'0.064',
            'accounted_plus_reserved_ceiling_cny':'0.26931680','rate_per_minute':6,'first_requests':57,'first_cny':'2','cumulative_requests':100,'cumulative_cny':'5','experiment_cny':'0.5'},
        'deadlines':{'task_seconds':300,'batch_seconds':1200,'observation_seconds':30,'model_seconds':25},
        'input_limit_bytes':12288,'max_output_tokens':512,'thinking':'disabled','stream':False,
        'confirmation':'engineering_user_reads_exact_formal_action_within_300s_task; fresh_identity_object_state_reobservation_required_before_confirm_and_original_UI_action; fresh_observation_expires_in_30s; no auto confirmation in live mode',
        'failure':'any_content_transport_usage_confirmation_terminal_failure_permanently_stops_entire_batch; no_retry_no_carryover',
        'worker':{'policy':'disabled-valid-ticket-readonly-continuation',
            'business':'reuse actual Q35 paid valid ticket without reset, new business writes, payment or ticket events'},
        'reuse':{'AC_source':'.local/v1-42/run-04','AC_passed':True,'B_D_only':True,
            'ticket_initial_sql_sha256':sha(lab/'initial-independent-sql.json')},
        'independent_verdict':'unique owned revision-1 valid ticket; unique captured payment 110000; job/provider/task associations; inventory 2/0/1',
        'limitations':['2 calls do not guarantee completion','synthetic single trip only','account balance and provider bill not independently verified','API-specific training optout applicability and account setting unknown'],
        'outbound':['fixed synthetic goal and bounded feedback','reviewed published knowledge','registered public labels and semantic booleans','current-session anonymized personal state','exact action evidence and hashes','at most four trusted history records'],
        'excluded':['business source','SQL/hidden state/expected answers','real account or credentials','raw HTTP','persistent UUIDs']}
    save(directory/'package.json',package)
    shutil.copy2(lab/'initial-independent-sql.json',directory/'initial-independent-sql.json')
    shutil.copy2(ac/'accounting-close.json',directory/'q35-accounting-close.json')
    save(directory/'experiment.json',approval_values(_today()))
    shutil.copy2(ROOT/'.local/v1-42/model-review-1106.json',directory/'model-review.json')
    for p in sorted((lab/'turns').glob('*-input.json')):shutil.copy2(p,directory/p.name)
    save(directory/'ledger-snapshot.json',{'summary':summary,'files':{p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'.local').glob('guide-usage.json*') if p.is_file() and not p.name.endswith('.lock')}})
    sources=[*ROOT.glob('services/**/*.py'),*ROOT.glob('services/**/*.sql'),*ROOT.glob('apps/web/src/*'),ROOT/'apps/web/package-lock.json',ROOT/'apps/web/vite.config.mjs',ROOT/'apps/web/tests/v1-execution42.mjs',ROOT/'scripts/v1_task.py',ROOT/'scripts/v1_package.py',ROOT/'scripts/round20_sandbox.py',ROOT/'tests/guide/test_v1_execution42.py',ROOT/'tests/guide/test_v1_q35_budget.py',ROOT/'tests/guide/test_v1_q36_budget.py',ROOT/'tests/business/test_v1_worker_events.py',ROOT/'AGENTS.md']
    artifacts=[*directory.glob('*'),lab/'browser-result.json',lab/'knowledge-ui.json',lab/'independent-sql.json',lab/'paid-before-ticket.json',lab/'lifecycle.json',ROOT/'.local/v1-42/verification-01.xml',ROOT/'.local/v1-42/package-gates.xml',ROOT/'.local/v1-42/q34-budget.xml',ROOT/'.local/v1-42/q34-budget-targeted.xml',ROOT/'tests/guide/test_v1_completion42.py',ROOT/'.local/v1-42/stop-fix.xml',ROOT/'.local/v1-42/stop-fix-targeted.xml',ROOT/'.local/v1-42/confirmation-fix.xml',ROOT/'tests/guide/test_v1_confirmation42.py',ROOT/'.local/v1-42/frontend-build.txt',ROOT/'.local/v1-42/q35-budget.xml',ROOT/'.local/v1-42/worker-events.xml']
    sources += [ROOT/'pytest.ini',ROOT/'tests/guide/test_v1_complete_authorization.py',ROOT/'tests/guide/test_navigation_generation14.py']
    artifacts += [ROOT/'.local/v1-42/complete-authorization.xml',ROOT/'.local/v1-42/q36-budget.xml',
        ac/'binding.json',ac/'independent-sql.json',ac/'accounting-close.json',ac/'paid-before-ticket.json',
        ac/'turns/1-terminal.json',ac/'turns/1-04-formal.json',lab/'initial-independent-sql.json',
        ROOT/'.local/v1-42/q36-integration.xml',ROOT/'.local/v1-42/navigation-generation14.xml',
        ROOT/'.local/v1-42/overall-1106-fixed.xml',ROOT/'.local/v1-42/overall-1106-remaining.xml',
        ROOT/'.local/v1-42/readonly-authorization-fixed.xml',ROOT/'.local/v1-42/current-runtime-fixture.xml']
    save(directory/'manifest.json',{'schema':'v1-executable-package-1','approved':False,'files':{p.relative_to(ROOT).as_posix():sha(p) for p in sorted(set(sources+artifacts)) if p.is_file()}})
    checked_package(directory)
    print(json.dumps({'manifest_sha256':sha(directory/'manifest.json'),'files':len(read(directory/'manifest.json')['files']),'requests':2,'reserved_cny':'0.064'},ensure_ascii=False))


def init_grant(directory):
    directory=Path(directory).resolve();package=checked_package(directory);checked_decision(directory)
    snapshot=read(directory/'ledger-snapshot.json')
    if any(sha(ROOT/name)!=digest for name,digest in snapshot['files'].items()):raise ValueError('original_ledger_changed')
    ledger=BudgetLedger(ROOT/'.local/guide-usage.json')
    result=create_q19_grant(ledger,task_specs(package),sha(directory/'decision.json'),batch=BATCH)
    checked_grant(directory);print(json.dumps({'package':result['package'],'requests':result['request_ceiling']}))


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','check','init-grant']);p.add_argument('directory',type=Path);p.add_argument('--lab',type=Path)
    args=p.parse_args()
    if args.action=='freeze':freeze(args.directory,args.lab)
    elif args.action=='check':checked_package(args.directory);print('frozen package verified; not an approval')
    else:init_grant(args.directory)

if __name__=='__main__':main()
