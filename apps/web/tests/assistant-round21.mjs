// Original webpage and real HTTP; inference is an explicitly offline substitute.
import {chromium,expect} from '@playwright/test';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url));
const out=path.join(root,'.local/d3-21');
const sample=JSON.parse(readFileSync(path.join(out,'sample.json'),'utf8'));
const report={state:'started',checks:[],chat_posts:0,ack_posts:0,business_writes:0,physical_model_requests:0,model:'offline_substitute'};
mkdirSync(path.join(out,'screenshots'),{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1100}});
let authorization='';
page.on('request',r=>{if(r.headers()['authorization'])authorization=r.headers()['authorization'];});
await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),method=request.method();
  if(url.hostname!=='127.0.0.1')return route.abort();
  if(url.pathname==='/api/assistant/chat'){report.chat_posts++;return route.continue();}
  if(url.pathname==='/api/assistant/ack'){report.ack_posts++;return route.continue();}
  if(url.pathname.startsWith('/api/')&&!['GET','HEAD'].includes(method)&&
    !(url.pathname==='/api/demo/sessions'&&method==='POST')&&!(url.pathname==='/api/session'&&method==='DELETE')){
    report.business_writes++;return route.abort();
  }
  return route.continue();
});
const click=async name=>page.getByRole('button',{name,exact:true}).click();
const idle=async()=>expect(page.getByRole('button',{name:'停止等待',exact:true})).toHaveCount(0,{timeout:40000});
async function login(account){await page.getByLabel('演示身份',{exact:true}).selectOption(account);await click('登录 / 切换身份');await expect(page.getByTestId('identity-id')).toHaveText(account);}
async function send(question){await page.getByLabel('咨询问题',{exact:true}).fill(question);const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/chat');await click('发送');const r=await pending;await idle();return r;}
try{
  await page.goto('http://127.0.0.1:5173');await login('personal_alice');
  await expect(page.locator('.trip-list button')).toHaveCount(2);
  await page.locator('.trip-list button').filter({hasText:'行程 A'}).click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();await click('✦ AI 咨询');
  await page.getByText('共同咨询两个行程',{exact:true}).click();
  const shared=page.getByRole('checkbox',{name:'同时核对当前行程与另一个本人行程'});
  await shared.check();await page.getByLabel('咨询问题',{exact:true}).fill('请共同核对两个行程。');await click('发送');
  await expect(page.getByRole('alert')).toContainText('再选择另一个本人行程');
  expect(report.chat_posts).toBe(0);report.checks.push('explicit_second_owned_trip_required_before_request');
  await page.getByLabel('共同咨询的第二个行程',{exact:true}).selectOption(sample.trip_ids[1]);
  const request=page.waitForRequest(r=>new URL(r.url()).pathname==='/api/assistant/chat');
  expect((await send('请共同核对两个行程和共享券，在当前页解释。')).status()).toBe(200);
  expect((await request).postDataJSON().delegated_trip_ids).toEqual(sample.trip_ids);
  await expect(page.getByRole('log')).toContainText('尚未确定最终归属');
  report.checks.push('two_explicit_objects_real_chat_and_shared_resource_explanation');
  await page.screenshot({path:path.join(out,'screenshots/shared.png'),fullPage:true});
  expect((await send('请解释共享券，再导航到我的行程。')).status()).toBe(200);
  await expect(page.getByRole('log')).toContainText('页面已打开，可以继续咨询。');
  expect(report.ack_posts).toBe(1);report.checks.push('explanation_then_original_navigation_and_ack');
  await page.getByLabel('咨询问题',{exact:true}).fill('晚到：请解释两个行程。');
  const started=page.waitForRequest(r=>new URL(r.url()).pathname==='/api/assistant/chat');
  await click('发送');await started;await shared.uncheck();await idle();
  await expect(page.getByRole('log')).not.toContainText('本次共同核对两个');
  await expect(page.getByRole('log')).toContainText('旧回答和导航已丢弃');
  report.checks.push('delegation_mode_change_discards_inflight_result');
  await shared.check();await page.getByLabel('共同咨询的第二个行程',{exact:true}).selectOption(sample.trip_ids[1]);
  await page.getByLabel('咨询问题',{exact:true}).fill('晚到：共同核对。');
  const started2=page.waitForRequest(r=>new URL(r.url()).pathname==='/api/assistant/chat');
  await click('发送');await started2;await login('personal_bob');await page.waitForTimeout(1600);
  await page.getByText('共同咨询两个行程',{exact:true}).click();
  await expect(shared).not.toBeChecked();
  await expect(page.getByRole('log')).not.toContainText('本次共同核对两个');
  expect(await page.getByLabel('共同咨询的第二个行程').count()).toBe(0);
  report.checks.push('identity_switch_clears_delegation_and_late_private_answer');
  expect(report.business_writes).toBe(0);report.state='passed';
}catch(error){report.state='failed';report.error=String(error);process.exitCode=1;}
finally{
  try{if(authorization)await page.request.delete('http://127.0.0.1:8000/session',{headers:{Authorization:authorization}});}catch{}
  await page.screenshot({path:path.join(out,'screenshots/final.png'),fullPage:true}).catch(()=>{});
  writeFileSync(path.join(out,'offline-browser.json'),JSON.stringify(report,null,2));await browser.close();
}
console.log(JSON.stringify(report));
