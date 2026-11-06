import { useEffect, useRef, useState } from 'react';
import { Api, ApiError, StaleResponse, explain } from './api';
import { money } from './presentation';

type Result = {
  delegation_ref: string; valid_until: string; completion_verified: boolean;
  candidate: {status: string; explanation: string; preconditions: string[];
    expected_change: string; unknowns: string[]; references: string[]};
  entry: {id: string; path: string}|null;
  amount: {amount_minor: number; currency: string}|null;
};
const failures: Record<string,string> = {
  isolation_unavailable:'指引的隔离环境尚未就绪，尚未获得有效指引。',
  credentials_missing:'模型本地凭证尚未就绪，尚未获得有效指引。',
  config_invalid:'模型本地配置无效，请在本地核对。',
  config_unreadable:'模型本地配置无法读取，请在本地核对。',
  config_outside_package:'模型配置超出已批准范围，已停止调用。',
  prerequisites_unverified:'模型账号、访问能力或计费资料尚未核验，已停止调用。',
  price_unverified:'模型计费上界尚未核验，已停止调用。',
  price_changed:'模型价格已变化，已停止调用。',
  token_bound_unverified:'模型输入上界尚未核验，尚未获得有效指引。',
  ledger_uninitialized:'费用账本尚未初始化，尚未获得有效指引。',
  ledger_missing:'费用记录缺失，模型调用已停止。',
  ledger_corrupt:'费用记录损坏，模型调用已停止。',
  usage_unresolved:'已有未核实费用，模型调用已停止。',
  first_round_budget_stop:'模型用量达到首轮上限，已停止调用。',
  cumulative_budget_stop:'模型用量达到累计上限，已停止调用。',
  model_rate_stop:'本分钟的模型请求已达上限，请稍后主动继续。',
  model_rate_limited:'模型服务限流，本次未获得有效指引。',
  model_timeout:'模型响应超时，费用核实前已停止后续调用。',
  model_network_unavailable:'模型连接失败，费用核实前已停止后续调用。',
  model_identity_changed:'模型标识已变化，已停止调用。',
  guide_busy:'指引正在处理其他请求，请稍后主动继续。',
  guide_rate_limited:'本分钟的指引请求已达上限，请稍后主动继续。',
  guide_in_progress:'当前任务仍在处理，请等待或取消。',
  guide_scope_unsupported:'此对象超出当前个人指引覆盖范围。',
  candidate_rejected:'模型未返回可核验的指引。请保留原操作进度。',
  observation_expired:'指引依据已过期，请主动重新检查。',
  state_changed:'指引依据已变化，请主动重新检查。',
  delegation_changed:'本次任务已取消或变化，旧结果已丢弃。',
};
const serviceReasons = new Set([
  'isolation_unavailable','credentials_missing','config_invalid','config_unreadable','config_outside_package',
  'prerequisites_unverified','price_unverified','price_changed','token_bound_unverified','ledger_uninitialized',
  'ledger_missing','ledger_corrupt','usage_unresolved','first_round_budget_stop','cumulative_budget_stop',
  'model_timeout','model_network_unavailable','model_identity_changed',
].map(code=>failures[code]));

export function Guide({api,id,disabled,invalidation}:{api:Api;id:string;disabled:boolean;invalidation:number}) {
  const [goal,setGoal] = useState('帮我完成这次模拟预订，并确认是否已出票');
  const [feedback,setFeedback] = useState('');
  const [detail,setDetail] = useState('brief');
  const [value,setValue] = useState<Result|null>(null);
  const [message,setMessage] = useState('');
  const [busy,setBusy] = useState(false);
  const ref = useRef<string|null>(null);
  const epoch = useRef(0);
  const controller = useRef<AbortController|null>(null);
  const alive = useRef(true);

  function clear() {epoch.current++;controller.current?.abort();controller.current=null;setValue(null);setBusy(false);}
  function cancel() {
    clear();setFeedback('');setMessage('任务已结束；原业务进度保留。');
    const current=ref.current;ref.current=null;
    if(current)void api.guide({action:'cancel',delegation_ref:current,request_id:crypto.randomUUID()}).catch(()=>{
      if(alive.current)setMessage('旧展示已清理。取消通知未确认；继续前需重新核对任务。');
    });
  }
  useEffect(()=>{alive.current=true;return()=>{
    alive.current=false;epoch.current++;controller.current?.abort();
    const current=ref.current;ref.current=null;
    if(current)void api.guide({action:'cancel',delegation_ref:current,request_id:crypto.randomUUID()}).catch(()=>{});
  };},[id]);
  useEffect(()=>{
    if(controller.current)ref.current=null;
    clear();setMessage('状态更新后请主动继续，重新核对指引依据。');
  },[invalidation]);
  useEffect(()=>{
    if(!value)return;
    const expire=()=>{setValue(null);setMessage('指引依据已失效，请主动重新检查。');};
    const timer=setTimeout(expire,Math.max(0,Date.parse(value.valid_until)-Date.now()));
    window.addEventListener('blur',expire);
    return()=>{clearTimeout(timer);window.removeEventListener('blur',expire);};
  },[value]);
  async function next() {
    if(busy||disabled)return;
    const generation=++epoch.current;
    const abort=new AbortController();controller.current=abort;
    setBusy(true);setValue(null);setMessage('');
    try {
      const result=await api.guide<Result>({action:'next',trip_id:id,goal,feedback,detail,
        request_id:crypto.randomUUID(),...(ref.current?{delegation_ref:ref.current}:{})},abort.signal);
      if(!alive.current||generation!==epoch.current)return;
      if(Date.parse(result.valid_until)<=Date.now()) {setMessage('指引依据已过期，请主动重新检查。');return;}
      ref.current=result.completion_verified?null:result.delegation_ref;
      setValue(result);
      if(result.completion_verified){setFeedback('');setMessage('已观察到此行程当前修订的有效票据。');}
    } catch(error) {
      if(alive.current&&generation===epoch.current&&!(error instanceof StaleResponse)) {
        if(error instanceof ApiError&&['not_visible','delegation_changed'].includes(error.code))ref.current=null;
        setMessage(error instanceof ApiError?failures[error.code]??explain(error):explain(error));
      }
    } finally {if(alive.current&&generation===epoch.current){setBusy(false);controller.current=null;}}
  }
  return <aside className="card guide-card" aria-label="个人按需指引"><div className="guide-heading"><span className="assistant-mark" aria-hidden="true">✦</span><div><p className="eyebrow">陪你完成下一步</p><h2>个人按需指引</h2></div></div>
    <p className="section-description">说说你想完成什么。指引只针对当前行程，由你执行预订操作。</p>
    <label>模拟预订目标<textarea rows={3} maxLength={600} value={goal} onChange={e=>{cancel();setGoal(e.target.value);}}/></label>
    <label>本次操作反馈 <span className="optional">选填</span><textarea rows={2} maxLength={600} placeholder="例如：已绑定付款资料，接下来怎么做？" value={feedback} onChange={e=>setFeedback(e.target.value)}/></label>
    <label>指引说明<select value={detail} onChange={e=>{clear();setDetail(e.target.value);}}><option value="brief">简短</option><option value="detailed">详细</option></select></label>
    <p className="hint input-note">仅填写模拟出行信息，请勿填写密钥、真实账户或银行卡资料。</p>
    <button className="guide-next" disabled={disabled||busy||!goal.trim()} onClick={()=>void next()}>{busy?'核对中…':'求助 / 继续检查'}</button>
    <button className="button-text guide-cancel" onClick={cancel}>结束指引任务</button>
    {message&&<div className="guide-message" role="status">{serviceReasons.has(message)?<><p>指引服务暂未就绪，你仍可在页面中自行预订。</p><details className="technical-details"><summary>查看原因</summary><p>{message}</p></details></>:<p>{message}</p>}</div>}
    {value&&<div className="guide-result" data-testid="personal-guide">
      <span className={`badge ${value.completion_verified?'success':'neutral'}`}>{value.completion_verified?'已核对出票结果':'本次指引'}</span>
      <p>{value.candidate.explanation}</p>
      {value.entry&&<p className="guide-entry">操作入口：{value.entry.path}</p>}
      {value.amount&&<p>当前金额：{value.amount.currency==='DEMO'?money(value.amount.amount_minor):'币种待确认，请在行程中核对'}</p>}
      {value.candidate.preconditions.length>0&&<p>需要满足：{value.candidate.preconditions.join('；')}</p>}
      {value.candidate.expected_change&&<p>预期变化：{value.candidate.expected_change}</p>}
      {value.candidate.unknowns.length>0&&<p>仍需确认：{value.candidate.unknowns.join('；')}</p>}
      <small>完成操作后，点击“求助 / 继续检查”核对下一步。</small>
      <details className="technical-details"><summary>查看指引依据</summary><p>依据个人使用说明与当前模拟身份允许查看的行程状态。状态变化或离开页面后需重新核对。</p><p>有效期：{value.valid_until}</p>{value.amount&&<p>金额单位：{value.amount.currency}</p>}</details>
    </div>}
  </aside>;
}
