// Prompt17 rendered-page evidence. Reads the preserved synthetic test trip; no model or business write.
import {chromium,expect} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url));
const output=path.join(root,process.argv[2]??'.local/d3-17');
mkdirSync(path.join(output,'screenshots'),{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const checks=[],observations={},sources=[],counterexamples=[];
let blockedAssistantPosts=0,blockedBusinessWrites=0,error;
await page.route('**/*',route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.hostname!=='127.0.0.1')return route.abort();
  if(url.pathname.startsWith('/api/assistant/')&&request.method()!=='GET'){
    blockedAssistantPosts++;return route.abort();
  }
  if(url.pathname.startsWith('/api/')&&!['GET','HEAD'].includes(request.method())&&
    !(url.pathname==='/api/demo/sessions'&&request.method()==='POST')&&
    !(url.pathname==='/api/session'&&request.method()==='DELETE')){
    blockedBusinessWrites++;return route.abort();
  }
  return route.continue();
});
// Exercise the production collector rather than a second copy of its filtering logic.
async function observe(name){
  return page.evaluate(async name=>(await import('/src/Assistant.tsx')).observePage(name),name);
}
function record(key,value){
  sources.push({source_index:sources.length,source_key:key,observed_at:new Date().toISOString(),source_kind:'client_observed'});
  observations[key]=value;
}
const zone=(value,id)=>value.semantics.zones.find(row=>row.id===id);
const control=(value,id)=>value.semantics.controls.find(row=>row.id===id);
function checkShape(value){
  expect(Object.keys(value).sort()).toEqual(['items','page','product_version','semantics','title']);
  expect(Object.keys(value.semantics).sort()).toEqual(['controls','schema','selected_trip','zones']);
  for(const row of value.semantics.zones)expect(Object.keys(row).sort()).toEqual(['id','present','visible']);
  for(const row of value.semantics.controls)expect(Object.keys(row).sort()).toEqual(['enabled','id','present','visible']);
  expect(JSON.stringify(value)).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
}
try{
  await page.goto('http://127.0.0.1:5173');
  const welcome=await observe('welcome');
  expect(zone(welcome,'welcome.intro')).toEqual({id:'welcome.intro',present:true,visible:true});
  checkShape(welcome);checks.push('未登录公开页面语义注册，不传身份选择值');
  await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
  await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
  await expect(page.locator('.trip-list button').first()).toBeVisible();
  await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'付款资料',exact:true}).click();
  const profile=await observe('profile');record('profile',profile);
  await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'我的行程',exact:true}).click();
  const before=await observe('trips');record('trips_before',before);
  expect(before.semantics.selected_trip).toBe(false);
  expect(zone(before,'booking.detail')).toEqual({id:'booking.detail',present:false,visible:false});
  expect(zone(before,'booking.payment')).toEqual({id:'booking.payment',present:false,visible:false});
  expect(control(before,'nav.booking')).toEqual({id:'nav.booking',present:true,visible:true,enabled:false});
  await page.screenshot({path:path.join(output,'screenshots/unselected-trips.png'),fullPage:true});
  checks.push('同一Alice身份未选行程：后置分区当前不存在，预订导航禁用');
  await page.locator('.trip-list button').first().click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  await expect(page.getByTestId('valid-ticket')).toBeVisible();
  const after=await observe('trips');record('trips_after',after);
  expect(after.semantics.selected_trip).toBe(true);
  for(const id of ['booking.detail','booking.payment'])expect(zone(after,id)).toEqual({id,present:true,visible:true});
  expect(control(after,'nav.booking')).toEqual({id:'nav.booking',present:true,visible:true,enabled:true});
  await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'预订与票据',exact:true}).click();
  const booking=await observe('booking');record('booking',booking);
  expect(control(booking,'booking.pay')).toEqual({id:'booking.pay',present:true,visible:true,enabled:false});
  await expect(page.getByRole('button',{name:/确认支付冻结金额/})).toBeDisabled();
  await page.screenshot({path:path.join(output,'screenshots/selected-booking.png'),fullPage:true});
  checks.push('选择既存有效票行程后：预订/付款票据区显示；已付款控件禁用，未产生付款');
  // Controlled DOM counterexamples are separate from the actual before/after learning sources.
  const hidden=await page.evaluate(async()=>{
    const element=document.querySelector('[data-assistant-zone="booking.payment"]');
    const previous=element.hidden;element.hidden=true;
    try{return (await import('/src/Assistant.tsx')).observePage('booking');}finally{element.hidden=previous;}
  });
  expect(zone(hidden,'booking.payment')).toEqual({id:'booking.payment',present:true,visible:false});
  expect(control(hidden,'booking.pay').enabled).toBe(false);
  counterexamples.push({kind:'controlled_hidden_zone',observation:hidden});
  const uncollected=await page.evaluate(async()=>{
    const marker=document.querySelector('[data-assistant-observation]');
    const previous=marker.getAttribute('data-assistant-observation');marker.removeAttribute('data-assistant-observation');
    try{return (await import('/src/Assistant.tsx')).observePage('booking');}finally{marker.setAttribute('data-assistant-observation',previous);}
  });
  expect(uncollected).not.toHaveProperty('semantics');
  counterexamples.push({kind:'controlled_unregistered_renderer',observation:uncollected});
  checks.push('明确区分固定区当前不存在、存在但隐藏、注册语义未采集');
  await page.getByTestId('trip-technical-details').locator('summary').click();
  await page.getByTestId('valid-ticket').locator('summary').click();
  const filtered=await page.evaluate(async()=>{
    const publicLabel=document.querySelector('[data-assistant-page="booking"] [data-assistant-public]');
    const input=document.createElement('input');input.value='CONTROL_VALUE_MUST_STAY_PRIVATE';publicLabel.append(input);
    const unknown=document.createElement('button');unknown.setAttribute('data-assistant-control','unregistered.private');unknown.textContent='PRIVATE_UNREGISTERED_TEXT';publicLabel.parentElement.append(unknown);
    try{return (await import('/src/Assistant.tsx')).observePage('booking');}finally{input.remove();unknown.remove();}
  });
  expect(JSON.stringify(filtered)).not.toContain('CONTROL_VALUE_MUST_STAY_PRIVATE');
  expect(JSON.stringify(filtered)).not.toContain('PRIVATE_UNREGISTERED_TEXT');
  expect(JSON.stringify(filtered)).not.toContain('unregistered.private');
  expect(JSON.stringify(filtered)).not.toContain('DEMO-TICKET');
  for(const value of [welcome,profile,before,after,booking,hidden,filtered])checkShape(value);
  expect(filtered.items).toEqual(booking.items);expect(filtered.semantics).toEqual(booking.semantics);
  checks.push('展开内部详情/票据凭证、控件值和未注册元素均不进入允许观察');
  expect(blockedAssistantPosts).toBe(0);expect(blockedBusinessWrites).toBe(0);
}catch(cause){error=String(cause);}
finally{await browser.close();}
const report={state:error?'failed':'passed',source:'rendered local synthetic test workspace; same Alice identity',
  observed_at:new Date().toISOString(),checks,observations,sources,counterexamples,error,
  privacy:{control_values:false,internal_ids:false,html:false,credentials:false},
  paid_requests:0,business_writes:0,blocked_assistant_posts:blockedAssistantPosts,blocked_business_writes:blockedBusinessWrites,
  limitation:'Browser semantics describe only this rendered page; they do not authorize the actor, prove business state or establish other roles/branches.'};
writeFileSync(path.join(output,'observation17.json'),JSON.stringify(report,null,2));
console.log(JSON.stringify({state:report.state,checks,error,paid_requests:0}));
if(error)process.exitCode=1;
