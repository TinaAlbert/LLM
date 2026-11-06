// Explicit Prompt 16 browser phases. Paid phases are opt-in and never default regression.
import {chromium,expect} from '@playwright/test';
import {readFileSync,writeFileSync,existsSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url)),out=path.join(root,'.local/d3-16');
const phase=process.argv[2];if(!['prepare','enhanced','continue','learn','terminal'].includes(phase))throw Error('Explicit phase required');
const target=path.join(out,`browser-${phase}.json`);
writeFileSync(target,JSON.stringify({state:'started',phase}),{flag:'wx'});
const paid=phase!=='prepare';
const beforeFiles=new Set((await import('node:fs')).readdirSync(path.join(root,'.local/assistant-runs')));
function control(...args){return JSON.parse(execFileSync(path.join(root,'.venv/Scripts/python.exe'),['-X','utf8',path.join(root,'apps/web/tests/control.py'),...args],{cwd:root,encoding:'utf8',windowsHide:true}));}
const browser=await chromium.launch({channel:'msedge',headless:true});const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(25000);
let result={phase,state:'started'},modelPosts=0;
const limits=['learn','enhanced'].includes(phase)?2:paid?1:0;
const allowed=phase==='learn'?['/api/assistant/admin/explore','/api/assistant/admin/explore-finish']:['/api/assistant/chat'];
await page.route('**/api/assistant/**',route=>{
 const p=new URL(route.request().url()).pathname;
 if(route.request().method()==='POST'&&!p.endsWith('/ack')) {
  if(!allowed.includes(p)||++modelPosts>limits)return route.abort();
 }
 return route.continue();
});
async function nav(name){await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name,exact:true}).click();}
async function ask(question){
 await page.getByLabel('咨询问题',{exact:true}).fill(question);
 const r=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/chat');
 await page.getByRole('button',{name:'发送',exact:true}).click();
 const res=await r,b=await res.json();if(res.status()!==200)throw Error(`chat:${res.status()}:${JSON.stringify(b)}`);
 delete b.session_id;return b;
}
try {
 await page.goto('http://127.0.0.1:5173');await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
 await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
 if(phase==='prepare'){
  await page.getByRole('button',{name:'创建行程',exact:true}).click();await expect(page.getByTestId('trip-detail')).toBeVisible();
  const trip=(await page.getByTestId('trip-id').textContent()).trim();
  await page.getByRole('button',{name:'绑定付款资料',exact:true}).click();await expect(page.getByRole('status').filter({hasText:'绑定资料已受理'})).toBeVisible();
  await page.getByRole('button',{name:'获取暂估报价',exact:true}).click();await expect(page.getByTestId('quoted-amount')).toHaveText('1,100.00 模拟元');
  await page.getByRole('button',{name:'确认最终锁价',exact:true}).click();await expect(page.getByTestId('locked-amount')).toHaveText('1,100.00 模拟元');
  await page.getByRole('button',{name:/^确认支付冻结金额/}).click();await expect(page.getByTestId('payment-state')).toContainText('处理中');
  const payment=(await page.getByTestId('payment-id').textContent()).trim();control('payment',trip,'succeeded',payment);
  await page.getByRole('button',{name:'刷新当前状态',exact:true}).click();await expect(page.getByText('已付待出票，行程尚未完成。',{exact:true})).toBeVisible();
  result.trip_id=trip;result.payment_id=payment;result.business_preparation='human_test_driver_original_ui_and_worker';
  result.public_observations=await page.evaluate(async()=>{const m=await import('/src/Assistant.tsx');return ['profile','trips','welcome','booking'].map(m.observePage);});
 }else if(phase==='learn'){
  await nav('付款资料');await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();await page.getByText('知识管理',{exact:true}).click();
  const chosen=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/admin/explore');
  await page.getByRole('button',{name:'训练（构建知识）',exact:true}).click();
  const chosenResponse=await chosen,proposal=await chosenResponse.json();if(chosenResponse.status()!==200)throw Error(JSON.stringify(proposal));
  result.selected_page=proposal.tool.arguments.page;
  const finished=await page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/admin/explore-finish');
  result.finish_status=finished.status();result.candidate=await finished.json();
  if(finished.status()!==200)throw Error(JSON.stringify(result.candidate));
  await expect(page.locator(`[data-assistant-page="${result.selected_page}"]`)).toBeFocused();
  await page.getByText('查看候选知识与验证结果',{exact:true}).click();
 }else{
  const prepared=JSON.parse(readFileSync(path.join(out,'browser-prepare.json'),'utf8'));
  await page.getByText('通过行程编号打开',{exact:true}).click();await page.getByLabel('本人行程 ID',{exact:true}).fill(prepared.trip_id);
  await page.getByRole('button',{name:'打开行程',exact:true}).click();await expect(page.getByTestId('trip-id')).toHaveText(prepared.trip_id);
  await nav(phase==='enhanced'?'付款资料':'预订与票据');
  await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();
  const questions={enhanced:'请核对我当前这笔预订是否完成、是否还需要付款，并带我找到查看预订进度的页面。',continue:'请根据当前状态说明这笔预订完成了吗、还要付款吗、接下来怎么做。请留在当前页面直接回答。',terminal:'请根据刷新后的当前状态说明这笔预订是否完成、是否还需要付款。请留在当前页面回答。'};
  result.question=questions[phase];result.reply=await ask(result.question);
  if(result.reply.tool){
   await expect(page.getByRole('log')).toContainText('页面已打开');
   result.navigation_page=result.reply.tool.arguments.page;
   await expect(page.locator(`[data-assistant-page="${result.navigation_page}"]`)).toBeFocused();
  }
  if(phase==='enhanced'){
   await nav('预订与票据');
   result.continuation=await ask(questions.continue);
   await expect(page.getByRole('log')).toContainText(questions.enhanced);
   result.conversation_retained=true;
  }
 }
 result.model_posts=modelPosts;result.state='passed';await page.screenshot({path:path.join(out,`${phase}.png`),fullPage:true});
}catch(error){result.state='failed';result.error=String(error);await page.screenshot({path:path.join(out,`${phase}-failed.png`),fullPage:true});}
finally{
 result.run_files=(await import('node:fs')).readdirSync(path.join(root,'.local/assistant-runs')).filter(f=>!beforeFiles.has(f));
 writeFileSync(target,JSON.stringify(result,null,2));await browser.close();
}
console.log(JSON.stringify(result));if(result.state!=='passed')process.exitCode=1;
