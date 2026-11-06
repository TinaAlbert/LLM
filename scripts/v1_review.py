"""从既有V1零模型演练生成有限场景输入审查；不调用模型、不启动PG、不生成许可。"""
from pathlib import Path
import hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from services.guide.budget import BudgetLedger
from services.guide.experiment import input_size_bytes


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    out=ROOT/'.local/v1-41/review';out.mkdir(exist_ok=False)
    rehearsal=ROOT/'.local/v1-41/rehearsal-03'
    turns=json.loads((rehearsal/'turns.json').read_text(encoding='utf-8'))
    def save(name,value):
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    previews=[]
    for i,turn in enumerate(turns,1):
        name=f'input-{i:02d}.json';save(name,turn['input'])
        previews.append({'file':name,'bytes':input_size_bytes(turn['input']),'provenance':'actual_host_input_with_scripted_candidate_and_synthetic_page_observation','sent_to_provider':False})
    save('previews.json',previews)
    save('scenarios.json',{'package':'V1-P41-finite-scenarios','approved':False,'execution_ready':False,
        'tasks':[{'id':'V1-A-C','max_calls':8,'initial_page':'booking','goal':'请帮助我为当前本人行程取得有效票。',
                  'fixture':'single-trip','actor':'personal_alice','profile':'demo_personal_alice, bound and usable',
                  'trip':{'flight_id':'DEMO-PEK-SHA','travel_date':'2026-10-01','passengers':1,'revision':1},
                  'initial_state':'valid quote, no lock/payment/ticket','business_time':'2026-09-27T00:00:00Z'},
                 {'id':'V1-B','max_calls':1,'initial_page':'welcome','goal':'请带我去预订与票据页面查看当前行程。',
                  'initial_state':'same owned trip after successful V1-A-C; fresh session'},
                 {'id':'V1-D','max_calls':1,'initial_page':'booking','goal':'能否为我办理该产品未说明的国际签证？',
                  'initial_state':'same owned trip after successful V1-A-C; fresh session'}],
        'program_cases':['V1-E: reuse relevant local counterexamples','V1-F: existing reviewed knowledge publication and rollback, no new model build'],
        'limits':{'calls':10,'accounting_pool_cny':'0.320','per_call_reserve_cny':'0.032','max_per_minute':6,
                  'task_seconds':300,'batch_seconds':1200,'input_bytes':12288,'output_tokens':512,'thinking':'disabled','stream':False},
        'stop_rule':'Any content/transport/usage/faithful-confirmation/independent-terminal failure permanently stops entire batch; no retries or reuse of unused calls.',
        'worker_rule':'After original accepted payment, first accepted query for that object runs payment worker; second accepted query runs ticket worker. Neither rejected nor duplicate queries advance event counters.',
        'unknowns':['provider account balance not independently verified','API-specific training optout/account setting unknown'],
        'required_before_execution':['personal dynamic-scope and existing-ledger permit wiring','frozen runnable manifest and matching explicit user approval','browser/controller identity and actual DOM receipt','current endpoint/pricing/account checks only if invalidated'],
        'not_permission':True})
    save('model-review.json',{'date':'2026-10-29','method':'free official documentation reads; no API probe',
        'model':'deepseek-flash','revision':'DeepSeek-V4.1-Flash-1017','endpoint':'https://api.deepseek.com/chat/completions',
        'peak_cny_per_million':{'input_miss':'2','input_hit':'0.04','output':'8'},
        'sources':['https://api-docs.deepseek.com/zh-cn/quick_start/pricing/','https://api-docs.deepseek.com/guides/thinking_mode/',
                   'https://api-docs.deepseek.com/api/create-chat-completion/','https://api-docs.deepseek.com/zh-cn/guides/kv_cache/',
                   'https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html','https://cdn.deepseek.com/policies/zh-CN/deepseek-open-platform-terms-of-service.html'],
        'privacy_date':'2026-02-10','terms_updated':'2026-04-22','terms_effective':'2026-04-29','cache':'default disk cache',
        'training_optout':'API-specific applicability and account setting unknown','pricing_unchanged_from_q31':True})
    ledger=ROOT/'.local/guide-usage.json'
    save('ledger-snapshot.json',{'summary':BudgetLedger(ledger).summary(),'sha256':sha(ledger),
        'sidecars':{p.name:sha(p) for p in ledger.parent.glob('guide-usage.json.q*.json')},'new_grant_created':False})
    files=['services/guide/action_contract.py','services/guide/assistant.py','services/guide/releases.py','services/guide/reply_receipts.py',
           'tests/guide/test_evidence_contract41.py','scripts/v1_prepare.py','scripts/v1_review.py']
    save('manifest.json',{'kind':'local_M1_review_snapshot_not_live_permission','execution_ready':False,
        'implementation_sha256':{f:sha(ROOT/f) for f in files},'files':{p.name:sha(p) for p in sorted(out.glob('*.json'))}})
    print(json.dumps({'previews':len(previews),'max_input_bytes':max(x['bytes'] for x in previews),
                      'ledger':BudgetLedger(ledger).summary(),'manifest_sha256':sha(out/'manifest.json')},ensure_ascii=False))

if __name__=='__main__':main()
