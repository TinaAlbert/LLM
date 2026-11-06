// D3-20: original UI and real localhost HTTP/SQL; server inference is offline-only.
import {chromium,expect} from '@playwright/test';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root=fileURLToPath(new URL('../../..',import.meta.url));
const out=path.join(root,'.local/d3-20'),suffix=process.argv[2]??'';
if(!/^(?:-[a-z0-9]+)?$/.test(suffix))throw Error('invalid suffix');
const reportPath=path.join(out,`offline-browser${suffix}.json`);
mkdirSync(path.join(out,'screenshots'),{recursive:true});
writeFileSync(reportPath,JSON.stringify({state:'started'}),{flag:'wx'});
const sample=JSON.parse(readFileSync(path.join(out,'sample.json'),'utf8'));
const report={state:'started',model:'offline_substitute',physical_model_requests:0,
  checks:[],responses:[],business_writes:0,chat_posts:0,ack_posts:0};
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1100}});
let authorization='';
page.on('request',request=>{if(request.headers()['authorization'])authorization=request.headers()['authorization'];});
page.on('response',async response=>{
  const url=new URL(response.url());
  if(!url.pathname.startsWith('/api/assistant/'))return;
  const row={path:url.pathname,status:response.status()};
  if(url.pathname.endsWith('/chat')||url.pathname.endsWith('/ack')){
    try{const body=await response.json();Object.assign(row,{reply:body.reply,error:body.error,
      navigation_status:body.navigation_status,release_version:body.release_version});}catch{}
  }
  report.responses.push(row);
});
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
async function click(name){await page.getByRole('button',{name,exact:true}).click();}
async function idle(){await expect(page.getByRole('button',{name:'停止等待',exact:true})).toHaveCount(0,{timeout:40000});}
async function login(account){
  await page.getByLabel('演示身份',{exact:true}).selectOption(account);await click('登录 / 切换身份');
  await expect(page.getByTestId('identity-id')).toHaveText(account);
}
async function nav(name){await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name,exact:true}).click();}
async function send(question){
  await page.getByLabel('咨询问题',{exact:true}).fill(question);
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/chat');
  await click('发送');const response=await pending;await idle();return response;
}
try{
  await page.goto('http://127.0.0.1:5173');await login('employee_chen');
  await expect(page.locator('.trip-list button')).toHaveCount(1);
  await page.locator('.trip-list button').first().click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  await expect(page.getByTestId('trip-revision')).toHaveText('2');
  await expect(page.getByTestId('approval-state')).toContainText('失效');
  await nav('预订与票据');await click('✦ AI 咨询');
  expect((await send(sample.question)).status()).toBe(200);
  await expect(page.getByRole('log')).toContainText('原批准绑定旧修订');
  await expect(page.getByRole('log')).toContainText('不能直接用于当前付款');
  report.checks.push('employee_current_revision_and_invalidated_approval_explained');
  await page.screenshot({path:path.join(out,'screenshots',`employee${suffix}.png`),fullPage:true});
  expect((await send('请解释当前状态并导航到我的行程。')).status()).toBe(200);
  await expect(page.getByRole('log')).toContainText('页面已打开，可以继续咨询。');
  expect(report.ack_posts).toBe(1);
  report.checks.push('real_original_navigation_and_http_ack');
  await login('approver_lin');await nav('审批工作台');
  await expect(page.getByRole('log')).not.toContainText('原批准绑定旧修订');
  await expect(page.getByText('暂无被指派的待审批申请。',{exact:true})).toBeVisible();
  expect((await send('请核对我当前被指派的待办；不要推断申请人的付款。')).status()).toBe(200);
  await expect(page.getByRole('log')).toContainText('不能读取申请人的付款或票据');
  report.checks.push('assigned_approver_current_empty_list_and_private_unknowns');
  await login('approver_qian');await nav('审批工作台');
  await expect(page.getByText('暂无被指派的待审批申请。',{exact:true})).toBeVisible();
  expect((await send('同部门身份下，我现在有哪些被指派的申请？')).status()).toBe(200);
  report.checks.push('same_department_unassigned_identity_not_given_other_requests');
  await login('employee_chen');await page.locator('.trip-list button').first().click();
  await nav('预订与票据');
  await page.getByLabel('咨询问题',{exact:true}).fill('晚到：请说明当前批准。');
  const started=page.waitForRequest(r=>new URL(r.url()).pathname==='/api/assistant/chat');
  await click('发送');await started;
  await login('approver_qian');await nav('审批工作台');
  await expect(page.getByRole('log')).toContainText('可以问我功能怎么用');
  await page.waitForTimeout(1600);
  await expect(page.getByRole('log')).not.toContainText('原批准绑定旧修订');
  report.checks.push('identity_switch_discards_inflight_offline_candidate_and_history');
  report.user_visible=await page.getByRole('log').innerText();
  expect(report.business_writes).toBe(0);
  report.state='passed';
}catch(error){report.state='failed';report.error=String(error);process.exitCode=1;}
finally{
  try{if(authorization)await page.request.delete('http://127.0.0.1:8000/session',{headers:{Authorization:authorization}});}catch{}
  await page.screenshot({path:path.join(out,'screenshots',`final${suffix}.png`),fullPage:true}).catch(()=>{});
  writeFileSync(reportPath,JSON.stringify(report,null,2));await browser.close();
}
console.log(JSON.stringify({state:report.state,checks:report.checks,chat_posts:report.chat_posts,
  ack_posts:report.ack_posts,physical_model_requests:0,business_writes:report.business_writes,error:report.error}));
