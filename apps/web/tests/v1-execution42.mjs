// Engineering browser: original controls only. Live mode waits for a human/agent's exact confirmation file.
import {chromium,expect} from '@playwright/test';
import {readFileSync,writeFileSync,existsSync,mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {createHash} from 'node:crypto';
const root=fileURLToPath(new URL('../../..',import.meta.url));
const label=process.argv[2];if(!label||!/^[a-zA-Z0-9-]+$/.test(label))throw Error('exclusive label required');
const out=path.join(root,'.local/v1-42',label),read=p=>JSON.parse(readFileSync(p,'utf8').replace(/^\uFEFF/,''));
const binding=read(path.join(out,'binding.json')),live=binding.mode==='live',key=read(path.join(out,'host-secret.json')).key;
const save=(name,value)=>writeFileSync(path.join(out,name),JSON.stringify(value,null,2),{flag:'wx'});
const canonical=v=>v===null||typeof v!=='object'?JSON.stringify(v):Array.isArray(v)?'['+v.map(canonical).join(',')+']':'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}';
// action_digest uses ensure_ascii=True; fixed keys/actions are ASCII, reply digest comes from Python receipt below.
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1080}});
let authorization='',requests=0,chatBodies=[],ackRows=[],result={state:'failed'};
page.on('request',r=>{if(r.url().startsWith('http://127.0.0.1:5173/api/'))authorization=r.headers()['authorization']||authorization;});
await page.route('**/api/assistant/**',route=>{
 const r=route.request(),p=new URL(r.url()).pathname;
 if(r.method()==='GET'||['/chat','/ack','/publish','/rollback'].some(s=>p.endsWith(s)))return route.continue();
 return route.abort();
});
page.on('response',async r=>{if(new URL(r.url()).pathname==='/api/assistant/ack')ackRows.push({status:r.status(),body:await r.json()});});
const host=async(name,body)=>{const r=await page.request.post('http://127.0.0.1:5173/api/__v1/'+name,{headers:{'X-V1-Host':key,Authorization:authorization},data:body});if(!r.ok())throw Error(name+':'+r.status()+':'+await r.text());return r.json();};
const button=name=>page.getByRole('button',{name,exact:true});
async function shot(name){await page.locator('.assistant-messages').evaluateAll(elements=>elements.forEach(e=>{e.scrollTop=e.scrollHeight;}));await page.screenshot({path:path.join(out,name+'.png'),fullPage:false});}
async function adminClick(name,suffix){const future=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/admin/'+suffix);await button(name).click();const r=await future;expect(r.status()).toBe(200);return r.json();}
async function switchPage(name){await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name,exact:true}).click();}
try{
 await page.goto('http://127.0.0.1:5173');await button('登录 / 切换身份').click();await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
 await page.locator('[data-assistant-control="trips.select"]').click();await expect(page.getByTestId('trip-id')).toHaveText(binding.trip_id);
 await button('✦ AI 咨询').click();
 let publication=null;
 if(!live){
  await page.getByText('知识管理',{exact:true}).click();
  const rollback=await adminClick('回滚版本','rollback');expect(rollback.active_version).not.toBe(binding.release);
  await page.getByLabel('候选版本',{exact:true}).selectOption(binding.release);
  await expect(page.getByText('候选知识与来源审查',{exact:true})).toBeVisible();await shot('01-knowledge-review');
  const published=await adminClick('上线所选版本','publish');expect(published.active_version).toBe(binding.release);
  publication={review_visible:true,published:published.active_version,previous:rollback.active_version,new_session:false};
  await page.getByText('知识管理',{exact:true}).click();
 }
 // Real DOM collection via the original exported semantic observer, never HTML/control values.
 const observations=await page.evaluate(async()=>{const m=await import('/src/Assistant.tsx');return Object.fromEntries(['welcome','profile','trips','booking'].map(p=>[p,m.observePage(p)]));});
 const install=await host('install',observations);await button('刷新助手状态').click();
 if(!live)await page.evaluate(()=>{const banner=document.querySelector('.demo-banner');banner.textContent='工程零模型演练 · 脚本候选，AI 验收待批准 · 真实业务与浏览器回执';});
 const pageNames={welcome:'欢迎与身份',booking:'预订与票据'};
 await switchPage(pageNames[install.initial_pages[0]]);await shot('02-initial-state');
 let sequence=1,turn=0,done=false,lastChat=0,sessionIds=[];
 while(!done){
  if(live&&lastChat&&Date.now()-lastChat<11000)await new Promise(r=>setTimeout(r,11000-(Date.now()-lastChat)));
  const question=turn===0?install.goals[sequence-1]:install.feedback;
  const future=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/chat',{timeout:40000});
  await page.getByLabel('咨询问题',{exact:true}).fill(question);lastChat=Date.now();await button('发送').click();
  const response=await future;requests++;chatBodies.push(response.request().postDataJSON());
  if(response.status()!==200)throw Error('formal delivery failed '+response.status()+':'+await response.text());
  const formal=await response.json();turn++;sessionIds.push(formal.session_id);
  expect(formal.release_version).toBe(binding.release);
  if(publication)publication.new_session=true;
  const action=formal.action_contract.action,kind=action.kind;
  if(kind==='navigate'){
   await expect.poll(()=>ackRows.length).toBeGreaterThan(0);
   expect(ackRows.at(-1).body.navigation_status).toBe('succeeded');
   const focused=await page.evaluate(p=>document.activeElement?.getAttribute('data-assistant-page')===p,action.params.page);
   expect(focused).toBe(true);
  }
  // Python precomputed reply hash is diagnostic only; live operator still must read the entire formal reply and explicitly confirm its exact action.
  const pending=await host('pending',{});
  save(`${sequence}-${turn}-browser-formal.json`,{formal,confirmation_template:pending.confirmation,source_kind:live?'real_model':'scripted_not_ai'});
  let confirmation=pending.confirmation;
  if(live){
   const file=path.join(out,`${sequence}-${turn}-operator-confirmation.json`);
   console.log(JSON.stringify({awaiting:file,action,reply:formal.reply}));
   const until=Date.now()+Math.max(0,pending.remaining_seconds-2)*1000;
   while(!existsSync(file)){if(Date.now()>until)throw Error('operator confirmation timeout');await new Promise(r=>setTimeout(r,100));}
   confirmation=read(file);
  }
  const confirmed=await host('confirm',confirmation);expect(confirmed.action).toEqual(action);
  const controls={lock:'booking.lock',pay:'booking.pay',query:'booking.reconcile',cancel:'booking.cancel_lock',requote:'booking.quote'};
  if(controls[kind]){
   const received=page.waitForResponse(r=>r.request().method()==='POST'&&!new URL(r.url()).pathname.includes('/assistant/')&&!new URL(r.url()).pathname.includes('/__v1/'));
   await page.locator(`[data-assistant-control="${controls[kind]}"]`).click();const r=await received;expect(r.ok()).toBe(true);
   await expect(button('刷新当前状态')).toBeEnabled();
  }
  const completed=await host('complete',{kind,...(kind==='navigate'?{page:action.params.page,dom_verified:true}:{})});
  // Simulation events run after faithful completion. Read back through the original UI;
  // refreshing is read-only and is never an extra reconcile/payment business action.
  if(['pay','query','wait'].includes(kind)){
   const refreshed=page.waitForResponse(r=>r.request().method()==='GET'&&new URL(r.url()).pathname.startsWith('/api/trips/')&&new URL(r.url()).pathname.endsWith('/booking'));
   await button('刷新当前状态').click();await refreshed;
   await expect(button('刷新当前状态')).toBeEnabled();
  }
  await shot(`${sequence}-${turn}-${kind}`);
  if(kind==='pay')await expect(page.getByTestId('ticket-task')).toContainText('等待');
  if(kind==='end')await expect(page.getByTestId('valid-ticket')).toBeVisible();
  done=completed.completed;
  if(!done&&completed.sequence!==sequence){
   sequence=completed.sequence;turn=0;await button('新对话').click();await expect(page.getByLabel('咨询记录',{exact:true})).toContainText('可以问我');
   await switchPage(pageNames[install.initial_pages[sequence-1]]);
  }
 }
 if(publication){
  await page.getByText('知识管理',{exact:true}).click();const rolled=await adminClick('回滚版本','rollback');expect(rolled.active_version).toBe(publication.previous);
  publication.rollback=rolled.active_version;await shot('09-knowledge-rollback');save('knowledge-ui.json',publication);
 }
 expect(new Set(sessionIds).size).toBe(install.goals.length);
 result={state:'passed',mode:binding.mode,model_calls:live?requests:0,ai_task_denominator:live?install.goals.length:0,scripted_turns:live?0:requests,real_browser_ack:ackRows,session_count:install.goals.length,original_ui:true,not_ai_acceptance:!live};
}catch(error){if(live)await host('abort',{}).catch(()=>{});result={state:'failed',mode:binding.mode,error:String(error),requests,not_ai_acceptance:!live};await shot('failure').catch(()=>{});}
finally{save('browser-result.json',result);save('browser-chat-bodies.json',chatBodies);await page.close();await browser.close();}
console.log(JSON.stringify(result));if(result.state!=='passed')process.exitCode=1;
