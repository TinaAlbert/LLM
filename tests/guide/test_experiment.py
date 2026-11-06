import asyncio,copy,json,time
from decimal import Decimal
import httpx,pytest
from services.guide.budget import BudgetLedger,BudgetUnavailable
from services.guide.model import DeepSeekModelAdapter,ModelConfig,ModelUnavailable,MODEL,_today
from services.guide.experiment import NAVIGATION_TOOL,approval_values,parse_response
from scripts.llm_probe import CASE

@pytest.fixture
def setup(tmp_path):
    ledger=BudgetLedger(tmp_path/'ledger.json');ledger.initialize()
    approval=tmp_path/'approval.json';approval.write_text(json.dumps(approval_values(_today())))
    config=ModelConfig('synthetic-test-key-not-an-account','synthetic',_today(),'user_reported','test')
    return ledger,approval,config

def response():
    return {'id':'synthetic-receipt','model':MODEL,'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100},'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"answers":[]}'}}]}

def invoke(setup,case,handler):
    ledger,approval,config=setup
    return asyncio.run(DeepSeekModelAdapter(ledger,config=config,input_validator=lambda x:x,transport=httpx.MockTransport(handler)).generate_experiment(case,time.monotonic()+5,approval_path=approval))

def test_fixed_transport_and_settlement(setup):
    def handler(request):
        data=json.loads(request.content)
        assert str(request.url)=='https://api.deepseek.com/chat/completions'
        assert data['thinking']=={'type':'disabled'} and data['max_tokens']==512
        return httpx.Response(200,json=response())
    result=invoke(setup,CASE,handler)
    assert result.amount_cny==Decimal('0.0006')
    assert setup[0].summary()['unresolved_requests']==0

@pytest.mark.parametrize('change', ['large','tool','output','stale_approval','old_approval','excess_approval'])
def test_reject_before_dispatch(setup,change):
    case=copy.deepcopy(CASE)
    if change=='large':case['messages'][1]['content']='中'*13000
    if change=='tool':case['tools']=[{'name':'shell'}]
    if change=='output':case['max_tokens']=2000
    if change=='stale_approval':
        setup[1].write_text(json.dumps(approval_values('2000-01-01')))
    if change in ('old_approval','excess_approval'):
        approval=approval_values(_today())
        approval['request_ceiling']=16 if change=='old_approval' else 23
        if change=='old_approval':approval['approved_on']='2026-10-14'
        setup[1].write_text(json.dumps(approval))
    with pytest.raises(ModelUnavailable):invoke(setup,case,lambda r:pytest.fail('must not dispatch'))
    assert setup[0].summary()['physical_requests']==0

@pytest.mark.parametrize('change', ['missing_usage','timeout','redirect','output_cap'])
def test_unknown_fee_stops_followups(setup,change):
    calls=[]
    def handler(request):
        calls.append(1)
        if change=='timeout':raise httpx.ReadTimeout('synthetic')
        if change=='redirect':return httpx.Response(302,headers={'location':'https://example.com/'})
        data=response()
        if change=='output_cap':data['usage'].update(completion_tokens=513,total_tokens=613)
        else:del data['usage']
        return httpx.Response(200,json=data)
    with pytest.raises(ModelUnavailable):invoke(setup,CASE,handler)
    with pytest.raises(ModelUnavailable,match='usage_unresolved'):invoke(setup,CASE,handler)
    assert len(calls)==1 and setup[0].summary()['unresolved_requests']==1

@pytest.mark.parametrize('page',['profile','https://example.com','__proto__'])
def test_tool_parameters_are_strict_and_proposal_only(page):
    body={'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'navigate','arguments':json.dumps({'page':page})}}]}}]}
    if page=='profile':
        result=parse_response(body,True,False)
        assert result['tool_calls'][0]['arguments']=={'page':'profile'}
        assert 'executed' not in result
    else:
        with pytest.raises(ModelUnavailable):parse_response(body,True,False)

def test_q14_continues_sixteen_original_rows_and_stops_at_twenty_two(setup,monkeypatch):
    ledger=setup[0]
    now=time.time();monkeypatch.setattr(BudgetLedger,'_now',staticmethod(lambda:now))
    approval=json.loads(setup[1].read_text())
    assert approval['request_ceiling']==22 and approval['approved_on']=='2026-10-15'
    assert approval['accounted_ceiling_cny']=='0.5'
    for n in range(16):
        now+=61
        reservation=ledger.reserve(experiment=True)
        ledger.settle(reservation,prompt_tokens=100,completion_tokens=50,provider_request_id=f'fake-{n}')
    original=json.loads(ledger.path.read_text())
    anchors=(ledger.marker.read_bytes(),ledger.lock_path.read_bytes())
    recovered=BudgetLedger(ledger.path)
    for n in range(16,22):
        now+=61
        reservation=recovered.reserve(experiment=True)
        recovered.settle(reservation,prompt_tokens=100,completion_tokens=50,provider_request_id=f'fake-{n}')
    final=json.loads(ledger.path.read_text())
    assert final['requests'][:16]==original['requests']
    assert {k:v for k,v in final.items() if k!='requests'}=={k:v for k,v in original.items() if k!='requests'}
    assert (ledger.marker.read_bytes(),ledger.lock_path.read_bytes())==anchors
    before_rejected=ledger.path.read_bytes()
    with pytest.raises(BudgetUnavailable,match='experiment_budget_stop'):
        BudgetLedger(ledger.path).reserve(experiment=True)
    assert ledger.path.read_bytes()==before_rejected
    assert ledger.summary()['physical_requests']==22


def test_optional_provider_tool_index_is_not_an_executable_field():
    body={'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[{'index':0,'id':'call_1','type':'function','function':{'name':'navigate','arguments':'{"page":"profile"}'}}]}}]}
    assert parse_response(body,True,False)['tool_calls'][0]['arguments']=={'page':'profile'}
    body['choices'][0]['message']['tool_calls'][0]['index']=1
    with pytest.raises(ModelUnavailable,match='tool_index_invalid'):parse_response(body,True,False)
