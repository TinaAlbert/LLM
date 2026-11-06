import { useEffect, useRef, useState } from 'react';
import { Api, ApiError, StaleResponse } from './api';
import type { Trip, User } from './types';
import { flightName } from './presentation';

export const pageNames = {welcome:'欢迎与身份',profile:'付款资料',trips:'我的行程',booking:'预订与票据',approvals:'审批工作台'} as const;
export type AssistantPage = keyof typeof pageNames;
export interface PageSemantics {
  schema:'page-semantics-1';selected_trip:boolean;
  zones:{id:string;present:boolean;visible:boolean}[];
  controls:{id:string;present:boolean;visible:boolean;enabled:boolean|null}[];
}
export interface PageObservation {page:AssistantPage;title:string;items:string[];product_version:'v1';semantics?:PageSemantics}
// Fixed public semantic IDs. Missing records mean uncollected, never unsupported.
const observationRegistry:Record<AssistantPage,{zones:string[];controls:string[]}>= {
  welcome:{zones:['welcome.header','welcome.intro'],controls:['identity.login']},
  profile:{zones:['profile.form'],controls:['profile.bind']},
  trips:{zones:['trips.create','trips.list','booking.detail','booking.payment'],controls:['trips.create','trips.select','nav.booking']},
  booking:{zones:['booking.detail','booking.pricing','booking.payment'],controls:['nav.booking','booking.quote','booking.lock','booking.cancel_lock','booking.pay','booking.reconcile']},
  approvals:{zones:['approvals.list'],controls:['approvals.approve','approvals.reject']},
};
interface NavigationResult {success:boolean;observation:PageObservation}
interface AssistantStatus {admin:boolean;active_version:string|null;candidate_versions:string[];allowed_pages:AssistantPage[];ready:boolean;reason?:string|null}
interface NavigationTool {id:string;name:'navigate';arguments:{page:AssistantPage};nonce:string}
interface ChatResult {session_id:string;reply:string;release_version:string;tool?:NavigationTool;reason?:string}
interface TrainingResult {candidate_version:string;knowledge:unknown;validation:unknown;coverage_missing?:string[]}
interface LearningSession {session_id:string;nonce:string;stage:'before_selection'|'after_selection'|'ready';expires_in_seconds:number;release_version:string}
interface LearningDraft extends LearningSession {expiresAt:number;businessVersion:number;tripId:string}
interface ConditionCoverage {page:AssistantPage;selected_trip:boolean;zone_id:string|null}
interface KnowledgeEntry {page:AssistantPage;summary:string;source_index:number;quote:string;condition?:{selected_trip:boolean};visibility_change?:{zone_id:string;before_source_index:number}}
interface CandidateReview {
  candidate_version:string;active_version:string;prompt_version:string;legacy_contract:boolean;publishable:boolean;
  knowledge:{entries:KnowledgeEntry[];unknowns:string[]};
  sources:{index:number;page:AssistantPage;title:string;items:string[];product_version:string;selected_trip:boolean|null;zones:{id:string;present:boolean;visible:boolean}[];controls?:{id:string;present:boolean;visible:boolean;enabled:boolean|null}[]}[];
  coverage:{pages:AssistantPage[];conditions:ConditionCoverage[];missing_pages:AssistantPage[];missing_conditions:ConditionCoverage[]};
}
interface Message {role:'user'|'assistant'|'notice';text:string}

export function pagesForUser(user:User|null):AssistantPage[] {
  if(!user)return ['welcome'];
  const pages:AssistantPage[]=['welcome'];
  if(!user.roles||user.roles.includes('employee'))pages.push('profile','trips','booking');
  if(user.roles?.includes('approver'))pages.push('approvals');
  return pages;
}
function isPage(value:unknown):value is AssistantPage {
  return typeof value==='string'&&Object.hasOwn(pageNames,value);
}
function visible(element:HTMLElement) {
  return element.isConnected&&element.getClientRects().length>0&&getComputedStyle(element).visibility!=='hidden'
    &&!element.closest('[hidden],[aria-hidden="true"]')
    &&!element.closest('details:not([open])');
}

function observeSemantics(page:AssistantPage):PageSemantics|undefined {
  const registration=document.querySelector<HTMLElement>('[data-assistant-observation="page-semantics-1"]');
  if(!registration)return undefined;
  const registry=observationRegistry[page];
  return {schema:'page-semantics-1',selected_trip:registration.dataset.assistantSelectedTrip==='true',
    zones:registry.zones.map(id=>{
      const elements=[...document.querySelectorAll<HTMLElement>(`[data-assistant-zone="${id}"]`)];
      return {id,present:elements.length>0,visible:elements.some(visible)};
    }),
    controls:registry.controls.map(id=>{
      const elements=[...document.querySelectorAll<HTMLElement>(`[data-assistant-control="${id}"]`)];
      return {id,present:elements.length>0,visible:elements.some(visible),
        enabled:elements.length?elements.some(element=>visible(element)&&!element.matches(':disabled')&&element.getAttribute('aria-disabled')!=='true'):null};
    }),
  };
}

// Only explicitly public labels/help and registered presence/availability enter the observation.
// Never HTML, control values, object IDs, credentials or inferred business eligibility.
export function observePage(page:AssistantPage):PageObservation {
  const roots=document.querySelectorAll<HTMLElement>(`[data-assistant-page="${page}"],[data-assistant-part="${page}"]`);
  const items:string[]=[];
  for(const root of roots)for(const element of root.querySelectorAll<HTMLElement>('[data-assistant-public]')) {
    if(!visible(element)||element.closest('.technical-details'))continue;
    const clone=element.cloneNode(true) as HTMLElement;
    clone.querySelectorAll('input,select,textarea,output,script,style,[hidden],[aria-hidden="true"]').forEach(node=>node.remove());
    const value=clone.textContent?.replace(/\s+/g,' ').trim().slice(0,160);
    if(value&&!items.includes(value))items.push(value);
  }
  const semantics=observeSemantics(page);
  return {page,title:pageNames[page],items:items.slice(0,8),product_version:'v1',...(semantics?{semantics}:{})};
}

// This adapter accepts only semantic destinations. It cannot submit a business action.
export async function navigatePage(page:AssistantPage,allowed:AssistantPage[],isCurrent:()=>boolean):Promise<NavigationResult> {
  const fallback=()=>({success:false,observation:observePage('welcome')});
  if(!isPage(page)||!allowed.includes(page)||!isCurrent())return fallback();
  const target=document.querySelector<HTMLElement>(`[data-assistant-page="${page}"]`);
  if(!target)return fallback();
  for(let parent=target.parentElement;parent;parent=parent.parentElement)if(parent instanceof HTMLDetailsElement)parent.open=true;
  target.scrollIntoView({behavior:'instant',block:'start'});
  target.focus({preventScroll:true});
  await new Promise<void>(resolve=>requestAnimationFrame(()=>resolve()));
  if(!isCurrent()||!visible(target))return fallback();
  const rect=target.getBoundingClientRect();
  const success=document.activeElement===target&&rect.bottom>0&&rect.top<window.innerHeight;
  return {success,observation:observePage(page)};
}

const reasons:Record<string,string>={
  authentication_required:'请先登录，再查询本人的业务状态。',not_visible:'当前身份不能访问这项资料。',
  admin_required:'当前身份没有知识管理权限。',assistant_admin_required:'当前身份没有知识管理权限。',
  model_config_unavailable:'模型服务尚未配置完成。',experiment_not_approved:'小额实验配置尚未就绪。',
  corporate_export_not_approved:'企业咨询的数据范围尚未获准发送给模型；请先审查本轮合成样本。',
  shared_export_not_approved:'双行程咨询的数据范围尚未获准发送给模型；请先审查具体样本。',
  budget_unavailable:'本批调用额度或费用核算需要处理，暂不能继续。',unresolved_usage:'上一次调用的费用尚未确认，已停止后续调用。',usage_unresolved:'上一次调用的费用尚未确认，已停止后续调用。',experiment_limit:'本批实验次数或额度已用完。',experiment_budget_stop:'本批实验次数或额度已用完。',
  network_unknown:'连接中断，结果尚未确认。请先查看助手状态，不要重复发起训练。',
  state_changed:'页面或业务状态已变化，请按当前页面重新咨询。',session_changed:'会话已变化，请重新咨询。',
  learning_navigation_failed:'未能完成学习导航，已停止知识构建。',learning_no_navigation:'模型未选择新的学习页面，本次未生成知识。',
  learning_expired:'采集已过期，请重新采集公开页与选择前后来源。',learning_stage_invalid:'采集顺序不符，请重新开始。',
  learning_before_selection_required:'请先取消行程选择，再采集选择前页面。',learning_after_selection_required:'请在原行程列表选择行程，等详情显示后再采集。',
};
function errorText(error:unknown) {
  return error instanceof ApiError?(reasons[error.code]??`助手暂未完成请求（${error.code}）。`):'助手暂未完成请求，请稍后核对状态。';
}
function validTool(tool:NavigationTool,allowed:AssistantPage[]) {
  return tool&&tool.name==='navigate'&&typeof tool.nonce==='string'&&tool.nonce.length>0&&tool.nonce.length<=128
    &&tool.arguments&&Object.keys(tool.arguments).length===1&&isPage(tool.arguments.page)&&allowed.includes(tool.arguments.page);
}

export function Assistant({api,identity,user,page,tripId,businessVersion,onNavigate}:{
  api:Api;identity:string;user:User|null;page:AssistantPage;tripId:string;businessVersion:number;
  onNavigate:(page:AssistantPage)=>Promise<NavigationResult>;
}) {
  const [open,setOpen]=useState(false),[question,setQuestion]=useState('');
  const [messages,setMessages]=useState<Message[]>([]);
  const [status,setStatus]=useState<AssistantStatus|null>(null),[problem,setProblem]=useState('');
  const [busy,setBusy]=useState<'chat'|'train'|'capture'|'publish'|'rollback'|null>(null);
  const [training,setTraining]=useState<TrainingResult|null>(null),[selectedVersion,setSelectedVersion]=useState('');
  const [learning,setLearning]=useState<LearningDraft|null>(null);
  const [shared,setShared]=useState(false),[secondTrip,setSecondTrip]=useState('');
  const [ownTrips,setOwnTrips]=useState<Trip[]>([]);
  const [review,setReview]=useState<CandidateReview|null>(null),[reviewProblem,setReviewProblem]=useState('');
  const session=useRef(''),run=useRef(0),controller=useRef<AbortController|null>(null),running=useRef(false);
  const delegatedKey=shared&&tripId&&secondTrip&&tripId!==secondTrip?`${tripId}:${secondTrip}`:'';
  const context=useRef({identity,page,tripId,businessVersion,delegatedKey,shared,secondTrip});
  context.current={identity,page,tripId,businessVersion,delegatedKey,shared,secondTrip};
  const localPages=pagesForUser(user);
  const allowed=localPages.filter(value=>status?.allowed_pages.includes(value));
  function append(role:Message['role'],text:string) {setMessages(old=>[...old,{role,text}].slice(-30));}
  async function refreshStatus(signal?:AbortSignal) {
    const owner=context.current.identity;
    const result=await api.request<AssistantStatus>('/assistant/status','GET',undefined,undefined,undefined,{signal});
    if(owner!==context.current.identity)return;
    setStatus(result);setSelectedVersion(old=>result.candidate_versions.includes(old)?old:result.candidate_versions.at(-1)??'');
  }
  useEffect(()=>{
    run.current++;controller.current?.abort();running.current=false;session.current='';
    setMessages([]);setQuestion('');setProblem('');setBusy(null);setTraining(null);setSelectedVersion('');setStatus(null);setLearning(null);
    setShared(false);setSecondTrip('');setOwnTrips([]);
    const abort=new AbortController();
    void refreshStatus(abort.signal).catch(error=>{if(!abort.signal.aborted&&!(error instanceof StaleResponse))setProblem(errorText(error));});
    return ()=>{abort.abort();controller.current?.abort();};
  },[identity]);

  useEffect(()=>{
    if(!open||!user||!pagesForUser(user).includes('trips'))return;
    const abort=new AbortController();
    void api.request<{trips:Trip[]}>('/trips','GET',undefined,undefined,undefined,{signal:abort.signal})
      .then(result=>{if(!abort.signal.aborted){setOwnTrips(result.trips);setSecondTrip(old=>result.trips.some(t=>t.id===old)?old:'');}})
      .catch(error=>{if(!abort.signal.aborted&&!(error instanceof StaleResponse))setProblem(errorText(error));});
    return ()=>abort.abort();
  },[open,identity,businessVersion]);
  useEffect(()=>{if(secondTrip===tripId)setSecondTrip('');},[tripId,secondTrip]);

  useEffect(()=>{
    setReview(null);setReviewProblem('');
    if(!status?.admin||!selectedVersion)return;
    const abort=new AbortController();
    void api.request<CandidateReview>(`/assistant/admin/review?candidate_version=${encodeURIComponent(selectedVersion)}`,'GET',undefined,undefined,undefined,{signal:abort.signal})
      .then(value=>{if(!abort.signal.aborted)setReview(value);})
      .catch(error=>{if(!abort.signal.aborted&&!(error instanceof StaleResponse))setReviewProblem(errorText(error));});
    return ()=>abort.abort();
  },[identity,selectedVersion,status?.admin,status?.active_version]);

  useEffect(()=>{
    if(learning&&(learning.businessVersion!==businessVersion||
      (learning.stage==='ready'&&learning.tripId!==tripId)||
      (status&&learning.release_version!==status.active_version))) {
      setLearning(null);setProblem('业务状态、行程或知识版本已变化，请重新采集学习来源。');
    }
  },[businessVersion,tripId,status?.active_version,learning]);
  useEffect(()=>{
    if(!learning)return;
    const timer=setTimeout(()=>{setLearning(null);setProblem('采集已过期，请重新采集学习来源。');},Math.max(0,learning.expiresAt-Date.now()));
    return ()=>clearTimeout(timer);
  },[learning]);

  function begin(kind:NonNullable<typeof busy>) {
    if(running.current)return null;
    running.current=true;const serial=++run.current;const abort=new AbortController();controller.current=abort;
    setBusy(kind);setProblem('');
    return {serial,abort,owner:context.current.identity};
  }
  function current(job:{serial:number;owner:string}) {return run.current===job.serial&&context.current.identity===job.owner;}
  function finish(job:{serial:number;owner:string}) {if(current(job)){running.current=false;setBusy(null);controller.current=null;}}
  function cancel() {
    const draft=learning;
    run.current++;controller.current?.abort();controller.current=null;running.current=false;setBusy(null);
    session.current='';setLearning(null);
    if(draft)void api.request('/assistant/admin/learning-cancel','POST',{session_id:draft.session_id,nonce:draft.nonce}).catch(()=>{});
    append('notice','已停止等待，本次晚到结果不会显示。费用状态由服务端核对，不会自动重试。');
  }
  function sameContext(initial:typeof context.current) {
    const now=context.current;
    return initial.identity===now.identity&&initial.page===now.page&&initial.tripId===now.tripId&&initial.businessVersion===now.businessVersion&&initial.delegatedKey===now.delegatedKey&&initial.shared===now.shared&&initial.secondTrip===now.secondTrip;
  }
  async function chat() {
    const text=question.trim();if(!text||!status?.ready)return;
    if(shared&&!delegatedKey){setProblem('请先打开一个行程，再选择另一个本人行程作为共同咨询对象。');return;}
    const job=begin('chat');if(!job)return;
    const initial={...context.current};setQuestion('');append('user',text);
    try {
      let result=await api.request<ChatResult>('/assistant/chat','POST',{
        ...(session.current?{session_id:session.current}:{}),message:text,page:initial.page,
        ...(initial.tripId?{trip_id:initial.tripId}:{}),observation:observePage(initial.page),
        ...(initial.delegatedKey?{delegated_trip_ids:[initial.tripId,initial.secondTrip]}:{}),
      },undefined,undefined,{signal:job.abort.signal,timeoutMs:65000});
      if(!current(job))return;
      if(!sameContext(initial)){append('notice','页面或行程已变化，旧回答和导航已丢弃；请按当前状态继续咨询。');return;}
      session.current=result.session_id;
      if(result.tool) {
        if(!validTool(result.tool,allowed)){append('notice','助手提出的入口不在允许范围内，未执行导航。');return;}
        if(result.reply)append('assistant',result.reply);
        const tool=result.tool;
        append('notice',`助手提议打开“${pageNames[tool.arguments.page]}”，正在尝试导航。`);
        const navigation=await onNavigate(tool.arguments.page);
        await new Promise<void>(resolve=>requestAnimationFrame(()=>resolve()));
        if(!current(job))return;
        const now={...context.current};
        const success=navigation.success&&now.page===tool.arguments.page&&now.tripId===initial.tripId&&now.businessVersion===initial.businessVersion&&now.delegatedKey===initial.delegatedKey&&now.shared===initial.shared&&now.secondTrip===initial.secondTrip;
        result=await api.request<ChatResult>('/assistant/ack','POST',{
          session_id:session.current,nonce:tool.nonce,success,
          observation:observePage(now.page),
        },undefined,undefined,{signal:job.abort.signal,timeoutMs:20000});
        if(!current(job))return;
        if(!sameContext(now)){append('notice','页面已再次变化，导航回执已记录；请按当前状态继续咨询。');return;}
        if(result.tool){append('notice','本次导航已结束，后续入口需再次咨询。');return;}
      }
      append('assistant',result.reply);
    } catch(error) {if(current(job)&&!(error instanceof StaleResponse))setProblem(errorText(error));}
    finally {finish(job);}
  }
  async function train() {
    if(!status?.admin||!status.ready)return;
    const job=begin('train');if(!job)return;
    const initial={...context.current};
    try {
      // Start from the current public zone; the model, not this loop, selects the next one.
      if(!['welcome','profile','trips'].includes(initial.page))throw new ApiError('training_public_page_required',400);
      const proposed=await api.request<ChatResult>('/assistant/admin/explore','POST',{
        observation:observePage(initial.page),
      },undefined,undefined,{signal:job.abort.signal,timeoutMs:65000});
      if(!current(job))return;
      if(!sameContext(initial))throw new ApiError('state_changed',409);
      if(!proposed.tool||!validTool(proposed.tool,allowed)||!['welcome','profile','trips'].includes(proposed.tool.arguments.page))throw new ApiError('navigation_not_allowed',403);
      const navigation=await onNavigate(proposed.tool.arguments.page);
      await new Promise<void>(resolve=>requestAnimationFrame(()=>resolve()));
      if(!current(job))return;
      const now={...context.current};
      const success=navigation.success&&now.page===proposed.tool.arguments.page&&now.tripId===initial.tripId&&now.businessVersion===initial.businessVersion;
      const result=await api.request<TrainingResult>('/assistant/admin/explore-finish','POST',{
        session_id:proposed.session_id,nonce:proposed.tool.nonce,success,observation:observePage(now.page),
      },undefined,undefined,{signal:job.abort.signal,timeoutMs:65000});
      if(!current(job))return;
      if(!sameContext(now))throw new ApiError('state_changed',409);
      setTraining(result);setSelectedVersion(result.candidate_version);
      await refreshStatus(job.abort.signal);
      if(current(job))append('notice',result.coverage_missing?.length?'已生成候选，但缺少当前版本的部分页面知识，暂不能替换上线。':'已生成候选知识。请查看来源与未知项，再决定上线。');
    } catch(error) {if(current(job)&&!(error instanceof StaleResponse))setProblem(errorText(error));}
    finally {finish(job);}
  }
  async function collectLearning() {
    if(!status?.admin)return;
    const job=begin('capture');if(!job)return;
    const initial={...context.current};
    try {
      const result=await api.request<LearningSession>(`/assistant/admin/${learning?'learning-capture':'learning-start'}`,'POST',{
        ...(learning?{session_id:learning.session_id,nonce:learning.nonce,...(initial.tripId?{trip_id:initial.tripId}:{})}:{}),
        observation:observePage(initial.page),
      },undefined,undefined,{signal:job.abort.signal});
      if(!current(job))return;
      if(!sameContext(initial))throw new ApiError('state_changed',409);
      setLearning({...result,expiresAt:learning?.expiresAt??Date.now()+result.expires_in_seconds*1000,
        businessVersion:initial.businessVersion,tripId:initial.tripId});
    } catch(error) {if(current(job)&&!(error instanceof StaleResponse)){setLearning(null);setProblem(errorText(error));}}
    finally {finish(job);}
  }
  async function discardLearning() {
    const draft=learning;setLearning(null);
    if(draft)try {await api.request('/assistant/admin/learning-cancel','POST',{session_id:draft.session_id,nonce:draft.nonce});}
    catch {/* The local draft is discarded; expired server sources cannot be reused. */}
  }
  async function buildLearning() {
    if(!learning||learning.stage!=='ready'||!status?.ready)return;
    const job=begin('train');if(!job)return;
    const initial={...context.current},draft=learning;
    try {
      const result=await api.request<TrainingResult>('/assistant/admin/learning-build','POST',{
        session_id:draft.session_id,nonce:draft.nonce,
      },undefined,undefined,{signal:job.abort.signal,timeoutMs:65000});
      if(!current(job))return;
      if(!sameContext(initial))throw new ApiError('state_changed',409);
      setTraining(result);setSelectedVersion(result.candidate_version);
      await refreshStatus(job.abort.signal);
      if(current(job))append('notice','已生成候选知识。请审查前后来源、条件变化与未知，再决定上线。');
    } catch(error) {if(current(job)&&!(error instanceof StaleResponse))setProblem(errorText(error));}
    finally {if(current(job))setLearning(null);finish(job);}
  }
  async function publish(action:'publish'|'rollback') {
    if(!status?.admin||(action==='publish'&&(!selectedVersion||review?.candidate_version!==selectedVersion||!review.publishable)))return;
    const job=begin(action);if(!job)return;
    try {
      const result=await api.request<AssistantStatus>(`/assistant/admin/${action}`,'POST',action==='publish'?{candidate_version:selectedVersion}:{},undefined,undefined,{signal:job.abort.signal});
      if(!current(job))return;
      setStatus(result);session.current='';setLearning(null);
      setMessages([{role:'notice',text:action==='publish'?'助手版本已上线。新对话将使用这份知识。':'已回滚助手版本。新对话将重新读取知识和当前状态。'}]);
    } catch(error) {if(current(job)&&!(error instanceof StaleResponse))setProblem(errorText(error));}
    finally {finish(job);}
  }

  return <aside className="global-assistant" aria-label="全局 AI 助手">
    {open&&<section className="assistant-panel" id="assistant-panel" aria-label="AI 咨询">
      <div className="assistant-heading"><h2>AI 咨询</h2><button className="button-text" onClick={()=>setOpen(false)} aria-label="收起 AI 咨询">收起</button></div>
      <p className="assistant-context">当前：{pageNames[page]} · {user?'已登录':'未登录'}{tripId?' · 已选行程':''}</p>
      {user&&localPages.includes('trips')&&<details><summary>共同咨询两个行程</summary>
        <label><input type="checkbox" checked={shared} onChange={event=>{setShared(event.target.checked);session.current='';setMessages([]);}}/>同时核对当前行程与另一个本人行程</label>
        {shared&&<><p className="hint">请先在工作台打开第一个行程，再选择第二个。本次只读取这两个对象及本人的共享券状态；付款和锁价仍由你确认。</p>
          <label>第二个行程<select aria-label="共同咨询的第二个行程" value={secondTrip} onChange={event=>{setSecondTrip(event.target.value);session.current='';setMessages([]);}}>
            <option value="">请选择本人行程</option>{ownTrips.filter(trip=>trip.id!==tripId).map(trip=><option key={trip.id} value={trip.id}>{flightName(trip.flight_id)} · {trip.travel_date} · {trip.passengers}人</option>)}
          </select></label></>}
      </details>}
      <div className="assistant-messages" role="log" aria-live="polite" aria-label="咨询记录">
        {messages.length===0&&<p className="hint">可以问我功能怎么用，或让我带你找到页面。付款和审批由你确认。</p>}
        {messages.map((message,index)=><div key={index} className={`assistant-message assistant-${message.role}`}><span className="sr-only">{message.role==='user'?'你：':message.role==='assistant'?'助手：':'提示：'}</span>{message.text}</div>)}
      </div>
      {problem&&<p className="assistant-problem" role="alert">{problem}</p>}
      {status&&!status.ready&&<p className="hint">{reasons[status.reason??'']??'助手当前暂不可用；你仍可使用工作台。'}</p>}
      <form onSubmit={event=>{event.preventDefault();void chat();}}>
        <label className="sr-only" htmlFor="assistant-question">咨询问题</label>
        <textarea id="assistant-question" maxLength={400} rows={2} placeholder="例如：带我找到付款资料" value={question} onChange={event=>setQuestion(event.target.value)}/>
        <div className="assistant-actions"><button disabled={!!busy||!status?.ready||!question.trim()}>{busy==='chat'?'正在思考…':'发送'}</button>{busy&&<button type="button" className="button-text" onClick={cancel}>停止等待</button>}<button type="button" className="button-text" disabled={!!busy||!!learning} onClick={()=>{session.current='';setMessages([]);setProblem('');}}>新对话</button><button type="button" className="button-text" disabled={!!busy} onClick={()=>{setProblem('');void refreshStatus().catch(error=>setProblem(errorText(error)));}}>刷新助手状态</button></div>
      </form>
      {status?.admin&&<details className="assistant-admin"><summary>知识管理</summary>
        <p className="hint">条件学习：先取消行程选择，在付款资料或欢迎页采集公开资料；再到“我的行程”采集选择前页面，点击原行程并等详情出现后采集选择后页面。来源在三分钟内有效；采集不调用模型，构建候选才调用一次。</p>
        {!learning?<button className="button-secondary" disabled={!!busy||!!tripId||!['welcome','profile'].includes(page)} onClick={()=>void collectLearning()}>采集公开资料</button>:<div>
          <p role="status">{learning.stage==='before_selection'?'公开资料已采集。请到“我的行程”，保持未选择行程。':learning.stage==='after_selection'?'选择前页面已采集。请点击原行程，等待详情出现。':'三份来源已采集，可以构建条件知识。'}</p>
          {learning.stage!=='ready'&&<button className="button-secondary" disabled={!!busy||page!=='trips'||(learning.stage==='before_selection'?!!tripId:!tripId)} onClick={()=>void collectLearning()}>{learning.stage==='before_selection'?'采集选择前页面':'采集选择后页面'}</button>}
          {learning.stage==='ready'&&<button disabled={!!busy||!status.ready} onClick={()=>void buildLearning()}>构建条件知识</button>}
          <button className="button-text" disabled={!!busy} onClick={()=>void discardLearning()}>放弃本次采集</button>
        </div>}
        <p className="hint">从当前公开分区出发，由模型选择另一页，实际观察后构建候选知识；不修改模型权重。请先进入欢迎、付款资料或行程分区。</p>
        <button className="button-secondary" disabled={!!busy||!!learning||!status.ready||!localPages.includes('profile')||!['welcome','profile','trips'].includes(page)} onClick={()=>void train()}>{busy==='train'?'正在构建知识…':'训练（构建知识）'}</button>
        <p className="hint">当前版本：{status.active_version??'暂无'}</p>
        {training&&<p className="hint">本轮生成：{training.candidate_version}</p>}
        <label>候选版本<select aria-label="候选版本" value={selectedVersion} onChange={event=>setSelectedVersion(event.target.value)}><option value="">请选择已验证候选</option>{status.candidate_versions.map(version=><option key={version} value={version}>{version}</option>)}</select></label>
        {reviewProblem&&<p role="alert">{reviewProblem}</p>}
        {selectedVersion&&!review&&!reviewProblem&&<p className="hint">正在读取候选来源与覆盖…</p>}
        {review&&<details className="assistant-review" open><summary>候选知识与来源审查</summary>
          <p className="hint">{review.legacy_contract?'旧版候选按原契约保留；未提供的结构化条件不受保护。':'条件变化已按前后来源核对；文字含义仍需人工审查。'}历史观察不证明当前用户权限、资格或业务成功。</p>
          <ol>{review.knowledge.entries.map((entry,index)=>{
            const source=review.sources.find(item=>item.index===entry.source_index);
            const before=review.sources.find(item=>item.index===entry.visibility_change?.before_source_index);
            const zone=entry.visibility_change?.zone_id;
            const oldZone=before?.zones.find(item=>item.id===zone),newZone=source?.zones.find(item=>item.id===zone);
            return <li key={index}><p>{entry.summary}</p><p className="hint">来源{entry.source_index+1}：{source?.title??pageNames[entry.page]}，引用“{entry.quote}”{entry.condition?`；条件：${entry.condition.selected_trip?'已选择行程':'未选择行程'}`:''}</p>
              {zone&&<p className="hint">{zone}：来源{(entry.visibility_change?.before_source_index??0)+1} {oldZone?.visible?'可见':'未见'} → 来源{entry.source_index+1} {newZone?.visible?'可见':'未见'}</p>}</li>;
          })}</ol>
          <p>尚未确认：{review.knowledge.unknowns.join('；')}</p>
          <p className="hint">覆盖页面：{review.coverage.pages.map(item=>pageNames[item]).join('、')}；结构条件 {review.coverage.conditions.length} 项。</p>
          {!!review.coverage.missing_pages.length&&<p role="alert">缺少当前版本页面：{review.coverage.missing_pages.map(item=>pageNames[item]).join('、')}</p>}
          {!!review.coverage.missing_conditions.length&&<p role="alert">缺少当前版本条件：{review.coverage.missing_conditions.map(item=>`${pageNames[item.page]} / ${item.selected_trip?'已选':'未选'} / ${item.zone_id??'条件'}`).join('、')}</p>}
          <details><summary>展开采集来源</summary>{review.sources.map(source=><div key={source.index}><p>来源{source.index+1}：{source.title}（{source.product_version}，{source.selected_trip===null?'未采集选择状态':source.selected_trip?'已选择行程':'未选择行程'}）</p><p className="hint">{source.items.join('；')}</p>
            <p className="hint">区域：{source.zones.map(zone=>`${zone.id} ${zone.visible?'可见':zone.present?'当前不可见':'未呈现'}`).join('；')||'未采集'}</p>
            <p className="hint">控件：{source.controls?.map(control=>`${control.id} ${!control.present?'未呈现':!control.visible?'当前不可见':control.enabled===null?'可用性未知':control.enabled?'可见且启用':'可见但禁用'}`).join('；')||'未采集'}</p>
          </div>)}</details>
        </details>}
        <div className="assistant-actions"><button disabled={!!busy||!selectedVersion||review?.candidate_version!==selectedVersion||!review.publishable} onClick={()=>void publish('publish')}>上线所选版本</button><button className="button-secondary" disabled={!!busy} onClick={()=>void publish('rollback')}>回滚版本</button></div>
      </details>}
    </section>}
    <button className="assistant-launcher" aria-expanded={open} aria-controls="assistant-panel" onClick={()=>setOpen(value=>!value)}>✦ {open?'收起咨询':'AI 咨询'}</button>
  </aside>;
}
