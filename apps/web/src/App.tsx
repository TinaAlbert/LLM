import { useCallback, useEffect, useRef, useState } from 'react';
import { Api, ApiError, StaleResponse, explain } from './api';
import type { Operation } from './api';
import type { User, Profile, Trip, Price, Booking, Options, Budget, Coupon, Approval } from './types';
import { Observation } from './Observation';
import { Guide } from './Guide';
import { Assistant, navigatePage, pageNames, pagesForUser } from './Assistant';
import type { AssistantPage } from './Assistant';
import { money, status, paymentStatus, ticketStatus, tone, flightName, projectName, sourceName, businessTime } from './presentation';

const api = new Api();
const accounts = [['personal_alice','Alice · 个人'],['personal_bob','Bob · 个人'],['employee_chen','Chen · 员工'],['approver_lin','Lin · 审批人'],['employee_ding','Ding · 员工'],['employee_fang','Fang · 员工'],['approver_wu','Wu · 审批人'],['approver_zhao','Zhao · 审批人'],['approver_qian','Qian · 审批人']];

export function App() {
  const [account,setAccount] = useState('personal_alice');
  const [user,setUser] = useState<User|null>(null);
  const [error,setError] = useState('');
  const [logging,setLogging] = useState(false);
  const [page,setPage] = useState<AssistantPage>('welcome');
  const [assistantTrip,setAssistantTrip] = useState('');
  const [businessVersion,setBusinessVersion] = useState(0);
  const epoch = useRef(0);
  const updateTrip = useCallback((id:string)=>setAssistantTrip(id),[]);
  const updateBusiness = useCallback(()=>setBusinessVersion(value=>value+1),[]);
  const availablePages = pagesForUser(user);
  useEffect(()=>{
    function locate(event:Event) {
      const target=event.target instanceof Element?event.target:null;
      const section=target?.closest<HTMLElement>('[data-assistant-page],[data-assistant-part]');
      const next=section?.dataset.assistantPage??section?.dataset.assistantPart;
      if(next && availablePages.includes(next as AssistantPage))setPage(next as AssistantPage);
    }
    document.addEventListener('focusin',locate);document.addEventListener('click',locate);
    return ()=>{document.removeEventListener('focusin',locate);document.removeEventListener('click',locate);};
  },[user]);
  async function goTo(next:AssistantPage) {
    const version=epoch.current;
    const result=await navigatePage(next,availablePages,()=>epoch.current===version);
    if(result.success)setPage(next);
    return result;
  }
  function clearIdentity() {
    const current=++epoch.current;const session=api.switchIdentity();setUser(null);setAssistantTrip('');setPage('welcome');
    setBusinessVersion(value=>value+1);setError('');setLogging(false);
    if(session.previous)void api.request('/session','DELETE',undefined,undefined,session.previous).catch(()=>{if(epoch.current===current)setError('本机已退出，服务端撤销尚未确认。');});
  }
  async function login() {
    const current = ++epoch.current;
    const session = api.switchIdentity();
    setUser(null); setAssistantTrip(''); setPage('welcome'); setError(''); setLogging(true);
    try {
      await api.login(account,session.previous,session.generation);
      const next = await api.request<User>('/session');
      if(current === epoch.current) setUser(next);
    } catch(e) { if(!(e instanceof StaleResponse) && current === epoch.current) setError(explain(e)); }
    finally { if(current === epoch.current) setLogging(false); }
  }
  return <><header className="site-header" data-assistant-page="welcome" data-assistant-zone="welcome.header" data-assistant-observation="page-semantics-1" data-assistant-selected-trip={assistantTrip?"true":"false"} tabIndex={-1}><div className="header-inner">
    <a className="brand" href="#workspace"><span className="brand-mark" aria-hidden="true">↗</span><span><span data-assistant-public>差旅工作台</span><small>让每一步出行更清楚</small></span></a>
    <div className="account-controls"><label>演示身份<select aria-label="演示身份" value={account} onChange={e=>setAccount(e.target.value)}>{accounts.map(([id,name])=><option key={id} value={id}>{name}</option>)}</select></label>
      <button className="button-secondary" data-assistant-control="identity.login" onClick={login} disabled={logging}>{logging?'登录中…':'登录 / 切换身份'}</button>{user&&<button className="button-text" onClick={clearIdentity}>退出</button>}</div>
  </div></header>
    <div className="demo-banner"><span className="demo-dot" aria-hidden="true"/>本地演示 · 身份、行程与金额均为模拟数据，不会真实扣款。</div>
    {error && <div className="page-message"><p role="alert">{error}</p></div>}
    <nav className="section-navigation" aria-label="工作台分区">{availablePages.map(item=><button key={item} className="button-text" data-assistant-control={item==='booking'?'nav.booking':undefined} aria-current={page===item?'location':undefined} onClick={()=>void goTo(item)} disabled={item==='booking'&&!assistantTrip}>{pageNames[item]}</button>)}</nav>
    {user ? <><div className="page-intro"><div><p className="eyebrow">我的差旅</p><h1>{user.display_name}，欢迎回来</h1><p>安排出行，核对付款，随时查看出票进度。</p></div>
      <div className="identity-card" data-testid="identity"><span className="avatar" aria-hidden="true">{user.display_name.slice(0,1)}</span><div><strong>{user.display_name}</strong><p>{user.roles?.includes('employee')?'企业员工':user.roles?.includes('approver')?'部门审批人':'个人用户'}</p>
        <details data-testid="identity-details" className="technical-details"><summary>身份详情</summary><dl><dt>账号标识</dt><dd data-testid="identity-id">{user.id}</dd>{user.organization_id&&<><dt>组织标识</dt><dd>{user.organization_id}</dd></>}{user.department_id&&<><dt>部门标识</dt><dd>{user.department_id}</dd></>}</dl></details></div></div>
    </div><Workbench key={`${user.id}-${epoch.current}`} user={user} onTripChange={updateTrip} onBusinessChange={updateBusiness}/></> : <main id="workspace" className="welcome" data-assistant-part="welcome" data-assistant-zone="welcome.intro"><div className="welcome-copy"><p className="eyebrow">从这里开始下一段旅程</p><h1 data-assistant-public>安排出行，<br/>每一步心中有数。</h1><p data-assistant-public>创建行程、确认报价，再查看付款和出票结果。<br/>选择上方演示身份，即可开始体验。</p><div className="welcome-steps"><span><b>01</b> 选择行程</span><span><b>02</b> 确认预订</span><span><b>03</b> 查看票据</span></div></div><div className="welcome-visual" aria-hidden="true"><span className="visual-caption">下一站，准备出发</span><div className="route-line"><i/><span>✈</span><i/></div><div className="visual-route"><strong>出发地</strong><strong>目的地</strong></div><div className="visual-ticket"><span>出行有安排</span><strong>从一份清晰的行程开始</strong><div className="ticket-dashes"/></div></div></main>}
    <footer>付款成功后仍需等待出票。只有出现当前行程的有效票据，才表示预订完成。</footer>
    <Assistant api={api} identity={`${epoch.current}:${user?.id??'anonymous'}`} user={user} page={page} tripId={assistantTrip} businessVersion={businessVersion} onNavigate={goTo}/></>;
}

function PriceView({price,final}:{price:Price;final?:boolean}) {
  return <section className={`price-card ${final?'price-final':''}`} aria-label={final?'最终锁价':'暂估报价'}><div className="section-heading"><h3>{final?'最终锁价':'暂估报价'}</h3><span className={`badge ${tone(price.effective_status)}`}>{status(price.effective_status)}</span></div>
    <dl><dt>基础金额</dt><dd>{money(price.base_amount_minor)}</dd><dt>券抵扣</dt><dd>{money(price.coupon_discount_minor)}</dd>
    <dt className="total-label">{final?'最终应付':'暂估应付'}</dt><dd className="price-total" data-testid={final?'locked-amount':'quoted-amount'}>{money(price.amount_minor)}</dd></dl>
    <p className="hint">有效期至 {businessTime(price.expires_at)}（模拟时间 · 北京时间）</p>
    <small>{final?'付款前请核对这里的最终金额。':'暂估报价不会占用优惠券，最终金额以成功锁价为准。'}</small></section>;
}

function Workbench({user,onTripChange,onBusinessChange}:{user:User;onTripChange:(id:string)=>void;onBusinessChange:()=>void}) {
  const employee = !!user.roles?.includes('employee');
  const approver = !!user.roles?.includes('approver');
  const traveler = !user.roles || employee;
  const [options,setOptions] = useState<Options>({flights:[],sources:[]});
  const [profile,setProfile] = useState<Profile>({id:null,source_ref:null,revision:0});
  const [trips,setTrips] = useState<Trip[]>([]);
  const [budgets,setBudgets] = useState<Budget[]>([]);
  const [coupons,setCoupons] = useState<Coupon[]>([]);
  const [approvals,setApprovals] = useState<Approval[]>([]);
  const [booking,setBooking] = useState<Booking|null>(null);
  const [source,setSource] = useState('');
  const [flight,setFlight] = useState('');
  const [passengers,setPassengers] = useState('1');
  const [project,setProject] = useState('');
  const [lookup,setLookup] = useState('');
  const [error,setError] = useState('');
  const [notice,setNotice] = useState('');
  const [pending,setPending] = useState<Operation|null>(null);
  const [busy,setBusy] = useState(false);
  const [guideEpoch,setGuideEpoch] = useState(0);
  const [guideTrip,setGuideTrip] = useState('');
  const working = useRef(false);
  const selected = useRef('');
  const observation = useRef(0);
  const alive = useRef(true);
  const disabled = busy || !!pending;
  useEffect(()=>{alive.current=true; void refresh(); return ()=>{alive.current=false; observation.current++;};},[]);
  useEffect(()=>{onTripChange(guideTrip);},[guideTrip,onTripChange]);
  useEffect(()=>{onBusinessChange();},[guideEpoch,onBusinessChange]);

  async function observe(id: string) {
    selected.current=id;
    setGuideTrip(id);
    const version=++observation.current;
    setBooking(null);
    if(!id) return;
    try {
      const data=await api.request<Booking>(`/trips/${encodeURIComponent(id)}/booking`);
      if(alive.current && observation.current===version) setBooking(data);
    } catch(e) { if(alive.current && observation.current===version && !(e instanceof StaleResponse)) setError(explain(e)); }
  }
  async function refresh(id=selected.current) {
    setGuideEpoch(value=>value+1);
    try {
      if(traveler) {
        const [o,p,t,c,b] = await Promise.all([
          api.request<Options>('/booking-options'), api.request<Profile>('/payment-profile'),
          api.request<{trips:Trip[]}>('/trips'), api.request<{coupons:Coupon[]}>('/coupons'),
          employee ? api.request<{projects:Budget[]}>('/budget-projects') : Promise.resolve({projects:[]}),
        ]);
        if(!alive.current) return;
        setOptions(o); setProfile(p); setTrips(t.trips); setCoupons(c.coupons);setBudgets(b.projects);
        setSource(old=>o.sources.some(s=>s.ref===old)?old:p.source_ref??o.sources[0]?.ref??'');
        setFlight(old=>old|| (o.flights[0]?JSON.stringify(o.flights[0]):''));
        setProject(old=>old||b.projects[0]?.id||'');
        // An older refresh must not reopen a trip after another selection.
        if(id===selected.current) await observe(id);
      }
      if(approver) {
        const result=await api.request<{approvals:Approval[]}>('/approvals');
        if(alive.current) setApprovals(result.approvals);
      }
    } catch(e) { if(alive.current && !(e instanceof StaleResponse)) setError(explain(e)); }
  }
  async function execute(operation:Operation) {
    if(working.current) return;
    working.current=true; setBusy(true);setPending(operation);setError('');setNotice('');
    setGuideEpoch(value=>value+1);
    try {
      const response=await api.write<{id?:string}>(operation);
      if(!alive.current) return;
      setPending(null);setNotice(`${operation.label}已受理，请核对下方当前状态。`);
      if(operation.path==='/trips' && response.id) selected.current=response.id;
      await refresh();
    } catch(e) {
      if(!alive.current || e instanceof StaleResponse) return;
      setError(explain(e));
      if(e instanceof ApiError && e.status>=400 && e.status<500) {setPending(null); await refresh();}
    } finally {working.current=false;if(alive.current)setBusy(false);}
  }
  function write(label:string,path:string,body:unknown={},method='POST') {
    if(disabled || working.current) return;
    void execute({label,path,body,method,key:crypto.randomUUID()});
  }
  const trip=booking?.trip;
  const lock=booking?.lock;
  const quote=booking?.quote;
  const editable=trip?.status==='draft';
  const currentLock=!!lock?.valid && lock.trip_revision===trip?.revision;
  const currentQuote=!!quote?.valid && quote.trip_revision===trip?.revision;
  const validTicket=!!booking?.ticket && booking.ticket.status==='valid' && booking.ticket.trip_revision===trip?.revision;
  return <main id="workspace" className="workbench"><div className="toolbar"><div><h2>{traveler?'出行安排':'审批工作台'}</h2><span className="hint">{traveler?'从行程到票据，在这里查看最新进度。':'查看分配给你的申请，并核对金额与出行信息。'}</span></div><button className="button-secondary" disabled={busy} onClick={()=>void refresh()}>刷新当前状态</button></div>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {pending && !busy && <aside className="pending-operation" role="status"><p>“{pending.label}”的结果尚未确认。重试会继续核对同一次操作。</p><button onClick={()=>void execute(pending)}>重试原操作</button></aside>}
    <div className={traveler?'workbench-layout':'approval-layout'}><div className="workspace-content">
    {traveler && <>
      <div className="setup-grid">
      <section className="card" aria-label="付款资料" data-assistant-page="profile" data-assistant-zone="profile.form" tabIndex={-1}><div className="section-heading"><h2 data-assistant-public>本人的付款资料</h2><span className={`badge ${profile.source_ref?'success':'neutral'}`}>{profile.source_ref?'已绑定':'未绑定'}</span></div><p className="section-description" data-assistant-public>选择模拟付款方式，绑定后可获取报价。</p>
        <p className="current-value">当前资料：<strong>{sourceName(profile.source_ref,options.sources)}</strong></p>
        <label><span data-assistant-public>付款来源</span><select aria-label="付款来源" value={source} onChange={e=>setSource(e.target.value)}>{options.sources.map(s=><option key={s.ref} value={s.ref}>{sourceName(s.ref,options.sources)}</option>)}</select></label>
        <button className="button-secondary" data-assistant-public data-assistant-control="profile.bind" disabled={disabled||!source} onClick={()=>write('绑定资料','/payment-profile',{source_ref:source,expected_revision:profile.revision},'PUT')}>绑定付款资料</button>
        <p className="hint" data-assistant-public>更换资料后，尚未付款的报价、锁价和申请需重新确认。付款受理后暂不可更换。</p>
        <details className="technical-details" data-testid="profile-details"><summary>付款资料详情</summary><dl><dt>已绑定来源</dt><dd>{profile.source_ref??'尚未绑定'}</dd><dt>选定来源</dt><dd>{source||'尚未选择'}</dd><dt>资料编号</dt><dd>{profile.id??'暂无'}</dd><dt>修订</dt><dd>{profile.revision}</dd></dl></details></section>
      <section className="card create-card" data-assistant-part="trips" data-assistant-zone="trips.create"><h2 data-assistant-public>创建自己的行程</h2><p className="section-description" data-assistant-public>选好航段与日期，开始这次出行。</p><form onSubmit={e=>{e.preventDefault();const f=JSON.parse(flight);write('创建行程','/trips',{flight_id:f.id,travel_date:f.travel_date,passengers:Number(passengers)});}}>
        <label><span data-assistant-public>航段与日期</span><select aria-label="航段与日期" required value={flight} onChange={e=>setFlight(e.target.value)}>{options.flights.map(f=><option key={f.id+f.travel_date} value={JSON.stringify(f)}>{flightName(f.id)} · {f.travel_date}</option>)}</select></label>
        <div className="form-action-row"><label className="passenger-field"><span data-assistant-public>乘机人数</span><input type="number" min="1" max="9" step="1" required value={passengers} onChange={e=>setPassengers(e.target.value)}/></label><button data-assistant-public data-assistant-control="trips.create" disabled={disabled||!flight}>创建行程</button></div></form></section>
      </div>
      <section className="card" aria-label="我的行程" data-assistant-page="trips" data-assistant-zone="trips.list" tabIndex={-1}><div className="section-heading"><h2 data-assistant-public>我的行程</h2><span className="count-label">{trips.length} 个行程</span></div>{trips.length===0 && <div className="empty-state"><p>暂无行程。</p><span data-assistant-public>创建后，你可以在这里继续预订或查看进度。</span></div>}<ul className="trip-list">{trips.map((t,index)=><li key={t.id}><button className={`trip-item ${guideTrip===t.id?'selected':''}`} aria-pressed={guideTrip===t.id} data-assistant-control="trips.select" disabled={busy} onClick={()=>void observe(t.id)}><span className="trip-item-title">{flightName(t.flight_id)}<span className={`badge ${tone(t.status)}`}>{status(t.status)}</span></span><span className="trip-item-meta">{t.travel_date} · {t.passengers} 人<span>行程 {index+1}</span></span></button></li>)}</ul>
        {guideTrip&&<button className="button-text" disabled={busy} onClick={()=>void observe('')}>取消行程选择</button>}
        <details className="technical-details" data-testid="trip-lookup"><summary>通过行程编号打开</summary><form className="lookup-form" onSubmit={e=>{e.preventDefault();setError('');void observe(lookup);}}><label>本人行程 ID<input value={lookup} onChange={e=>setLookup(e.target.value)} required placeholder="输入本人的完整行程编号"/></label><button className="button-secondary" disabled={busy}>打开行程</button></form></details></section>
      {booking && trip && <section className="card trip-detail" data-testid="trip-detail" aria-label="行程详情" data-assistant-page="booking" data-assistant-zone="booking.detail" tabIndex={-1}><div className="section-heading"><h2 data-assistant-public>行程详情</h2><span className={`badge ${validTicket?'success':tone(trip.status)}`}>{validTicket?'预订完成':status(trip.status)}</span></div>
        <div className="trip-summary"><div><p className="eyebrow">已选行程</p><h3>{flightName(trip.flight_id)}</h3><p>{trip.travel_date} <span className="separator">·</span> {trip.passengers} 位乘机人</p></div><div className="trip-summary-price"><span>当前价格</span><strong>{trip.price==null?'金额待确认':money(Number(trip.price)*100)}</strong>{trip.price==null&&<small>获取有效报价后显示</small>}</div></div>
        <TripEdit key={`${trip.id}-${trip.revision}`} trip={trip} disabled={disabled||!editable} save={body=>write('修订行程',`/trips/${trip.id}`,body,'PATCH')}/>
        <div className="booking-step" data-assistant-zone="booking.pricing"><div className="step-heading"><span className="step-number" aria-hidden="true">1</span><div><h3 data-assistant-public>报价与锁价</h3><p data-assistant-public>先查看暂估报价，再确认最终应付金额。</p></div></div>
          <div className="action-row"><button className="button-secondary" data-assistant-control="booking.quote" disabled={disabled||!editable} onClick={()=>write('获取报价',`/trips/${trip.id}/quotes`,{expected_revision:trip.revision})}>获取暂估报价</button><button data-assistant-control="booking.lock" disabled={disabled||!editable||!currentQuote||currentLock} onClick={()=>write('锁价',`/quotes/${quote!.id}/lock`)}>确认最终锁价</button></div>
          {(quote||lock)&&<div className="prices-grid">{quote && <PriceView price={quote}/>} {lock && <PriceView price={lock} final/>}</div>}
          {lock && <button className="button-text" data-assistant-control="booking.cancel_lock" disabled={disabled||!editable||!currentLock} onClick={()=>write('取消锁价',`/locks/${lock.id}/cancel`)}>取消尚未付款锁价</button>}
        </div>
        {employee && <section className="booking-step budget-section" aria-label="预算与申请"><div className="step-heading"><span className="step-number" aria-hidden="true">2</span><div><h3>预算与审批</h3><p>选定预算项目，提交后等待审批。</p></div></div><p>当前项目：<strong>{projectName(trip.budget_selection?.project_id)}</strong></p>
          <div className="form-action-row"><label>预算项目<select aria-label="预算项目" value={project} onChange={e=>setProject(e.target.value)}>{budgets.map(b=><option key={b.id} value={b.id}>{projectName(b.id)} · 可用 {money(b.available_minor)}</option>)}</select></label>
          <button className="button-secondary" disabled={disabled||!editable||!project} onClick={()=>write('选择预算',`/trips/${trip.id}/budget`,{project_id:project,expected_revision:trip.revision,expected_selection_revision:trip.budget_selection?.selection_revision??0},'PUT')}>选择预算项目</button></div>
          <p className="hint">首次选择会保留锁价；更换项目后需重新锁价和申请。提交申请才占用预算。</p>
          <details className="technical-details"><summary>查看项目额度与详情</summary>{budgets.map(b=><div className="budget-detail" key={b.id}><strong>{projectName(b.id)}</strong><dl><dt>已占用</dt><dd>{money(b.reserved_minor)}</dd><dt>已使用</dt><dd>{money(b.used_minor)}</dd><dt>可用额度</dt><dd>{money(b.available_minor)}</dd><dt>项目标识</dt><dd>{b.id}</dd><dt>配置修订</dt><dd>{b.policy_revision}</dd></dl></div>)}</details>
          <button disabled={disabled||!editable||!currentLock||!trip.budget_selection||['pending','approved'].includes(booking.approval?.status??'')} onClick={()=>write('提交申请',`/locks/${lock!.id}/approvals`,{expected_revision:trip.revision})}>提交审批</button>
          {booking.approval && <p className={`state-message ${tone(booking.approval.effective_status)}`} data-testid="approval-state">审批申请：{status(booking.approval.effective_status)}</p>}</section>}
        <section className="booking-step payment-section" aria-label="付款与票据" data-assistant-zone="booking.payment"><div className="step-heading"><span className="step-number" aria-hidden="true">{employee?'3':'2'}</span><div><h3 data-assistant-public>付款与出票</h3><p data-assistant-public>付款与出票分别处理，请以有效票据确认完成。</p></div></div>
          <button className="payment-button" data-assistant-control="booking.pay" disabled={disabled||!editable||!currentLock||(employee&&!booking.approval?.valid)} onClick={()=>write('付款',`/locks/${lock!.id}/payments`,{amount_minor:lock!.amount_minor})}>确认支付冻结金额 {lock?money(lock.amount_minor):'（需锁价）'}</button>
          <div className="payment-status-grid"><div className="status-panel"><span className="status-label">付款状态</span>{booking.payment?<><strong className={`status-text ${tone(booking.payment.status)}`} data-testid="payment-state">{paymentStatus(booking.payment.status)} · {money(booking.payment.amount_minor)}</strong><button className="button-text" data-assistant-control="booking.reconcile" disabled={disabled} onClick={()=>write('原付款对账',`/payments/${booking.payment!.id}/reconcile`)}>对账原付款</button></>:<strong>尚未付款</strong>}</div>
          <div className="status-panel"><span className="status-label">出票状态</span>{booking.ticket_task?<strong className={`status-text ${tone(booking.ticket_task.status)}`} data-testid="ticket-task">{ticketStatus(booking.ticket_task.status)}</strong>:<strong>{validTicket?'已有有效票据':'尚无出票任务'}</strong>}</div></div>
          {booking.payment?.status==='processing' && <p className="state-message warning">付款正在处理中，请等待或查询原付款结果，不要再次付款。</p>}
          {booking.payment?.status==='unknown' && <p className="state-message warning">结果未知，资源继续受保护。请对账原付款，不可再次付款。</p>}
          {booking.payment?.status==='failed' && <p className="state-message danger">付款失败，本次未完成付款。请核对当前报价与锁价后再操作。</p>}
          {booking.payment?.status==='succeeded' && !validTicket && <p className="state-message warning">已付待出票，行程尚未完成。</p>}
          {booking.ticket_task?.status==='failed' && <p className="state-message danger">出票失败，仍已付款；没有自动退款或第二次付款。</p>}
          {validTicket ? <div className="ticket-success" data-testid="valid-ticket"><span className="ticket-check" aria-hidden="true">✓</span><div><strong>已出票，预订完成</strong><p>已取得当前行程的有效票据。</p><details className="technical-details"><summary>查看票据凭证</summary><p>{booking.ticket!.certificate}</p><p>对应行程修订：{booking.ticket!.trip_revision}</p></details></div></div> : <p className="hint">尚未取得有效票据。</p>}
        </section>
        <Observation key={`observation-${trip.id}-${guideEpoch}`} api={api} kind="trip" id={trip.id} disabled={disabled}/>
        <details className="technical-details" data-testid="trip-technical-details"><summary>行程与订单详情</summary><dl><dt>行程 ID</dt><dd><output data-testid="trip-id">{trip.id}</output></dd><dt>行程修订</dt><dd><output data-testid="trip-revision">{trip.revision}</output></dd><dt>航段标识</dt><dd>{trip.flight_id}</dd><dt>行程状态</dt><dd>{trip.status}</dd>{quote&&<><dt>报价 ID</dt><dd>{quote.id}</dd><dt>报价状态</dt><dd>{quote.effective_status}</dd><dt>报价有效期</dt><dd>{quote.expires_at}</dd></>}{lock&&<><dt>锁价 ID</dt><dd><output data-testid="lock-id">{lock.id}</output></dd><dt>锁价状态</dt><dd>{lock.effective_status}</dd><dt>锁价有效期</dt><dd>{lock.expires_at}</dd></>}{booking.payment&&<><dt>付款 ID</dt><dd><output data-testid="payment-id">{booking.payment.id}</output></dd><dt>付款状态</dt><dd>{booking.payment.status}</dd></>}{booking.ticket_task&&<><dt>出票任务 ID</dt><dd><output data-testid="ticket-task-id">{booking.ticket_task.id}</output></dd><dt>出票任务状态</dt><dd>{booking.ticket_task.status}</dd></>}{booking.approval&&<><dt>审批 ID</dt><dd><output data-testid="approval-id">{booking.approval.id}</output></dd><dt>审批行程修订</dt><dd>{booking.approval.trip_revision}</dd><dt>审批状态</dt><dd>{booking.approval.effective_status}</dd></>}{booking.ticket&&<><dt>票据 ID</dt><dd>{booking.ticket.id}</dd><dt>票据状态</dt><dd>{booking.ticket.status}</dd></>}</dl></details>
      </section>}
    </>}
    {approver && <section className="card" aria-label="指派审批" data-assistant-page="approvals" data-assistant-zone="approvals.list" tabIndex={-1}><h2 data-assistant-public>被指派的申请摘要</h2>{approvals.length===0 && <div className="empty-state"><p>暂无被指派的待审批申请。</p><span>新的审批申请会显示在这里。</span></div>}{approvals.map(a=><article className="approval-card" key={a.id} aria-label={`申请 ${a.id}`}><div className="section-heading"><h3>{a.applicant_name} 的差旅申请</h3><span className={`badge ${tone(a.effective_status)}`}>{status(a.effective_status)}</span></div><p className="approval-route">{flightName(a.flight_id)}</p><p>{a.travel_date} · {a.passengers} 人</p><div className="approval-amount"><span>{projectName(a.project_id)}</span><strong>{money(a.amount_minor)}</strong></div><details className="technical-details"><summary>申请详情</summary><dl><dt>申请 ID</dt><dd>{a.id}</dd><dt>项目标识</dt><dd>{a.project_id}</dd><dt>航段标识</dt><dd>{a.flight_id}</dd><dt>行程修订</dt><dd>{a.trip_revision}</dd><dt>申请状态</dt><dd>{a.effective_status}</dd></dl></details><Observation key={`${a.id}-${a.revision}-${guideEpoch}`} api={api} kind="approval" id={a.id} disabled={disabled}/><div className="action-row">{(['approved','rejected'] as const).map(decision=><button className={decision==='rejected'?'button-secondary':''} key={decision} data-assistant-control={decision==='approved'?'approvals.approve':'approvals.reject'} disabled={disabled||a.effective_status!=='pending'} onClick={()=>write('审批决定',`/approvals/${a.id}/decision`,{expected_revision:a.revision,decision})}>{decision==='approved'?'批准申请':'拒绝申请'}</button>)}</div></article>)}</section>}
    </div>{traveler&&<div className="workspace-sidebar">
      {!employee && guideTrip ? <Guide key={guideTrip} api={api} id={guideTrip} disabled={disabled} invalidation={guideEpoch}/> : !employee&&<aside className="card guide-placeholder"><span className="assistant-mark" aria-hidden="true">✦</span><h2>需要一点帮助？</h2><p>创建或选择一个行程后，可以主动求助，核对下一步。</p><p className="hint">指引只提供建议，预订操作由你完成。</p></aside>}
      <section className="card coupon-card" aria-label="本人的券"><div className="section-heading"><h2>本人的券</h2><span aria-hidden="true">◇</span></div>{coupons.length===0?<p className="hint">无可用券资料。</p>:coupons.map(c=><div className="coupon" key={c.id}><strong>{c.percent}% 折扣</strong><p>最多抵扣 {money(c.cap_minor)}</p><span className={`badge ${tone(c.status)}`} data-testid="coupon-state">{status(c.status)}</span></div>)}</section>
      <p className="sidebar-note">所有金额以模拟元显示。报价和资源可能变化，操作前请核对最新状态。</p>
    </div>}</div>
  </main>;
}

function TripEdit({trip,disabled,save}:{trip:Trip;disabled:boolean;save:(body:unknown)=>void}) {
  const [flight,setFlight]=useState(trip.flight_id),[date,setDate]=useState(trip.travel_date),[count,setCount]=useState(String(trip.passengers));
  return <details className="trip-edit" data-testid="trip-edit"><summary>修订尚未付款行程</summary><p className="hint">修改后需重新获取报价；已付款的行程不能在这里修改。</p><form onSubmit={e=>{e.preventDefault();save({expected_revision:trip.revision,flight_id:flight,travel_date:date,passengers:Number(count)});}}>
    <fieldset disabled={disabled}><legend className="sr-only">修订尚未付款行程</legend><div className="edit-fields"><label>修改航段<input required maxLength={80} value={flight} onChange={e=>setFlight(e.target.value)}/></label><label>修改日期<input type="date" required value={date} onChange={e=>setDate(e.target.value)}/></label><label>修改人数<input type="number" min="1" max="9" required value={count} onChange={e=>setCount(e.target.value)}/></label></div><button className="button-secondary">保存行程修订</button></fieldset></form></details>;
}
