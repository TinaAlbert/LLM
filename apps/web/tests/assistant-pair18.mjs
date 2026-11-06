// Engineering browser driver: real rendered observations, production UI navigation, no business writes.
import {chromium, expect} from '@playwright/test';
import {mkdirSync, readFileSync, writeFileSync, openSync} from 'node:fs';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root=fileURLToPath(new URL('../../..',import.meta.url));
const out=path.join(root,'.local/d3-18'),mode=process.argv[2];
if(!['pair','learn','capture'].includes(mode))throw Error('mode must be pair, learn or capture');
mkdirSync(path.join(out,'screenshots'),{recursive:true});
const target=path.join(out,mode==='pair'?'pair-browser.json':mode==='learn'?'learning-browser.json':'capture-browser.json');
writeFileSync(target,JSON.stringify({state:'started',started_at:new Date().toISOString()}),{flag:'wx'});
const report={state:'started',mode,observations:{},responses:[],business_writes:0,model_post_attempts:0};
let authorization='',bridge,waiters=[],bridgeDone;
if(mode==='pair'){
  bridge=spawn(path.join(root,'.venv/Scripts/python.exe'),['-X','utf8','scripts/llm_round18.py','bridge'],
    {cwd:root,windowsHide:true,stdio:['pipe','pipe',openSync(path.join(out,'pair-host-error.txt'),'wx')]});
  createInterface({input:bridge.stdout}).on('line',line=>{
    const next=waiters.shift();if(!next)return;
    try{next.resolve(JSON.parse(line));}catch{next.reject(Error('host_protocol_invalid'));}
  });
  bridgeDone=new Promise(resolve=>bridge.on('exit',code=>{
    for(const next of waiters)next.reject(Error('host_bridge_exited'));waiters=[];resolve(code);
  }));
}
function host(op,body,token){
  return new Promise((resolve,reject)=>{
    waiters.push({resolve,reject});bridge.stdin.write(JSON.stringify({op,body,authorization:token})+'\n');
  });
}
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
page.on('request',request=>{const value=request.headers()['authorization'];if(value)authorization=value;});
await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),method=request.method();
  if(url.hostname!=='127.0.0.1')return route.abort();
  if(url.pathname==='/api/assistant/chat'&&method==='POST'&&mode==='pair'){
    report.model_post_attempts++;
    if(report.model_post_attempts>2)return route.abort();
    const result=await host('chat',request.postDataJSON(),request.headers()['authorization']);
    report.responses.push({status:result.status,reply:result.body.reply,reason:result.body.detail,
      tool_page:result.body.tool?.arguments.page,release_version:result.body.release_version});
    return route.fulfill({status:result.status,contentType:'application/json',body:JSON.stringify(result.body)});
  }
  if(url.pathname==='/api/assistant/ack'&&method==='POST'&&mode==='pair'){
    const receipt=request.postDataJSON();
    const result=await host('ack',receipt,request.headers()['authorization']);
    report.responses.push({operation:'ack',status:result.status,success:receipt.success,observation:receipt.observation,reply:result.body.reply});
    return route.fulfill({status:result.status,contentType:'application/json',body:JSON.stringify(result.body)});
  }
  if(url.pathname.startsWith('/api/assistant/')&&method!=='GET')return route.abort();
  if(url.pathname.startsWith('/api/')&&!['GET','HEAD'].includes(method)&&
     !(url.pathname==='/api/demo/sessions'&&method==='POST')&&!(url.pathname==='/api/session'&&method==='DELETE')){
    report.business_writes++;return route.abort();
  }
  return route.continue();
});
async function nav(name){await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name,exact:true}).click();}
async function observe(name){return page.evaluate(async name=>(await import('/src/Assistant.tsx')).observePage(name),name);}
try{
  await page.goto('http://127.0.0.1:5173');
  await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
  await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
  await expect(page.locator('.trip-list button')).toHaveCount(1);
  await nav('付款资料');report.observations.profile=await observe('profile');
  await nav('我的行程');report.observations.trips_before=await observe('trips');
  expect(report.observations.trips_before.semantics.selected_trip).toBe(false);
  await page.locator('.trip-list button').click();
  await expect(page.getByTestId('valid-ticket')).toBeVisible();
  report.observations.trips_after=await observe('trips');
  expect(report.observations.trips_after.semantics.selected_trip).toBe(true);
  for(const id of ['booking.detail','booking.payment']){
    expect(report.observations.trips_before.semantics.zones.find(z=>z.id===id).visible).toBe(false);
    expect(report.observations.trips_after.semantics.zones.find(z=>z.id===id).visible).toBe(true);
  }
  report.captured_at=new Date().toISOString();
  if(mode==='learn'){
    // Original trusted admin endpoint; observations just collected in this same Alice session.
    const sources=['profile','trips_before','trips_after'].map(k=>report.observations[k]);
    report.model_post_attempts=1;
    const response=await page.request.post('http://127.0.0.1:8000/assistant/admin/train',{
      headers:{Authorization:authorization},data:{observations:sources},timeout:65000});
    report.training_status=response.status();report.training=await response.json();
    expect(response.status()).toBe(200);
  }else if(mode==='pair'){
    const question=JSON.parse(readFileSync(path.join(root,'.local/d3-16/browser-enhanced.json'),'utf8')).question;
    report.question=question;
    await nav('预订与票据');report.observations.booking=await observe('booking');
    await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();
    for(const label of ['labels_only','semantics_added']){
      await nav('预订与票据');
      await page.getByLabel('咨询问题',{exact:true}).fill(question);
      const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/chat');
      await page.getByRole('button',{name:'发送',exact:true}).click();
      const response=await pending;
      await expect(page.getByRole('button',{name:'停止等待',exact:true})).toHaveCount(0,{timeout:65000});
      report.responses.push({condition:label,http_status:response.status(),finished_at:new Date().toISOString()});
      if(response.status()!==200)break;
    }
    await page.screenshot({path:path.join(out,'screenshots/pair-result.png'),fullPage:true});
  }else{
    await nav('预订与票据');report.observations.booking=await observe('booking');
    await page.screenshot({path:path.join(out,'screenshots/captured-booking.png'),fullPage:true});
  }
  expect(report.business_writes).toBe(0);report.state='finished';
  if(mode==='pair'){
    report.complete_pair=report.responses.filter(row=>row.condition&&row.http_status===200).length===2&&
      report.responses.every(row=>row.status===undefined||row.status===200);
    if(!report.complete_pair){report.state='incomplete';process.exitCode=1;}
  }
}catch(error){
  // Playwright request errors can include Authorization in their call log.
  report.state='failed';report.error=['Error','TimeoutError','AssertionError'].includes(error?.name)?error.name:'driver_error';
  process.exitCode=1;
}
finally{
  if(authorization)await page.request.delete('http://127.0.0.1:8000/session',{headers:{Authorization:authorization}}).catch(()=>{});
  authorization='';await browser.close();
  if(bridge){bridge.stdin.end(JSON.stringify({op:'close'})+'\n');report.host_exit=await bridgeDone;}
  writeFileSync(target,JSON.stringify(report,null,2));
}
console.log(JSON.stringify({state:report.state,mode,model_post_attempts:report.model_post_attempts,
  training_status:report.training_status,error:report.error}));
