import { useEffect, useRef, useState } from 'react';
import { Api, ApiError, StaleResponse, explain } from './api';
import { money, status, paymentStatus, ticketStatus } from './presentation';

type Item = { status?: string; effective_status?: string; amount_minor?: number; valid?: boolean };
type Evidence = {
  observed_at: string; valid_until: string;
  documented: { text: string };
  unknowns: string[];
  data: { trip?: Item; quote?: Item|null; lock?: Item|null; payment?: Item|null;
          ticket_task?: Item|null; ticket?: Item|null; approval?: Item|null };
};

export function Observation({api,kind,id,disabled}:{api:Api;kind:'trip'|'approval';id:string;disabled:boolean}) {
  const [value,setValue] = useState<Evidence|null>(null);
  const [message,setMessage] = useState('');
  const [busy,setBusy] = useState(false);
  const live = useRef(true);
  useEffect(()=>{live.current=true;return()=>{live.current=false;};},[]);
  useEffect(()=>{
    if(!value)return;
    const clear=()=>{setValue(null);setMessage('观察已失效，请重新读取。');};
    const timer=setTimeout(clear,Math.max(0,Date.parse(value.valid_until)-Date.now()));
    window.addEventListener('blur',clear);
    return()=>{clearTimeout(timer);window.removeEventListener('blur',clear);};
  },[value]);
  async function read() {
    setBusy(true);setValue(null);setMessage('');
    try {
      const result=await api.request<Evidence>(`/guide/observe?kind=${kind}&object_id=${encodeURIComponent(id)}`);
      if(live.current) {
        if(Date.parse(result.valid_until)<=Date.now())setMessage('观察已失效，请重新读取。');
        else setValue(result);
      }
    } catch(error) {
      if(live.current && !(error instanceof StaleResponse))setMessage(
        error instanceof ApiError && error.code==='isolation_unavailable'
          ? '受控观察的隔离环境尚未就绪。'
          : error instanceof ApiError && ['state_changed','observation_expired'].includes(error.code)
            ? '观察已变化或过期，请重新读取。' : explain(error));
    } finally {if(live.current)setBusy(false);}
  }
  const state=(item:Item|null|undefined)=>item?status(item.effective_status??item.status):'尚无记录';
  return <aside className="observation-panel" aria-label="受控观察"><div className="section-heading"><div><h3>核对当前状态</h3><p className="hint">只读取你可查看的状态，不调用模型或执行预订。</p></div><button className="button-secondary" disabled={disabled||busy} onClick={()=>void read()}>{busy?'读取中…':'核对当前状态'}</button></div>
    {message && <p role="status">{message}</p>}
    {value && <div className="observation-result" data-testid="controlled-observation">
      <p className="hint">本次核对 · {new Date(value.observed_at).toLocaleTimeString('zh-CN')}</p>
      {kind==='trip'?<><dl><dt>行程</dt><dd>{state(value.data.trip)}</dd><dt>付款</dt><dd>{value.data.payment?paymentStatus(value.data.payment.status??''):'尚无付款记录'}</dd><dt>出票</dt><dd>{value.data.ticket_task?ticketStatus(value.data.ticket_task.status??''):'尚无出票任务'}</dd><dt>票据</dt><dd>{value.data.ticket?.status==='valid'?'有效票据':value.data.ticket?'票据状态待确认':'尚无有效票据'}</dd></dl>
        <p>{value.unknowns.includes('current_quote_amount')?'当前报价金额待确认。':
          value.data.lock?.valid?`冻结金额：${money(value.data.lock.amount_minor)}`:
          value.data.quote?.valid?`暂估金额：${money(value.data.quote.amount_minor)}`:'请在行程中核对金额。'}</p></>:
        <p>指派申请：{state(value.data.approval)}。</p>}
      <small>这是当前模拟身份的状态记录；付款成功不代表已出票。</small>
      <details className="technical-details"><summary>查看使用说明与核对时间</summary><p>V1 使用说明：{value.documented.text}</p><p>读取时间：{value.observed_at}</p><p>有效期：{value.valid_until}</p><p>这里只核对当前获准读取的内容，没有 AI 规划或模拟探索结果。</p></details>
    </div>}
  </aside>;
}
