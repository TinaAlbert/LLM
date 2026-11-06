// Final browser readback only; all model POST routes are blocked.
import {chromium,expect} from '@playwright/test';
import {readFileSync,writeFileSync} from 'node:fs';import {fileURLToPath} from 'node:url';import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url)),out=path.join(root,'.local/d3-16');
const target=path.join(out,'terminal-readback.json');writeFileSync(target,'{"state":"started"}',{flag:'wx'});
const prepared=JSON.parse(readFileSync(path.join(out,'browser-prepare.json'),'utf8'));
const browser=await chromium.launch({channel:'msedge',headless:true}),page=await browser.newPage({viewport:{width:1440,height:1000}});let result={};
await page.route('**/api/assistant/**',route=>route.request().method()==='GET'?route.continue():route.abort());
try{
 await page.goto('http://127.0.0.1:5173');await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
 await page.getByText('通过行程编号打开',{exact:true}).click();await page.getByLabel('本人行程 ID',{exact:true}).fill(prepared.trip_id);await page.getByRole('button',{name:'打开行程',exact:true}).click();
 await expect(page.getByTestId('valid-ticket')).toBeVisible();await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
 await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'预订与票据',exact:true}).click();
 await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();await expect(page.getByRole('button',{name:'发送',exact:true})).toBeDisabled();
 await page.screenshot({path:path.join(out,'terminal-readback.png'),fullPage:true});
 result={state:'passed',valid_ticket_visible:true,payment_disabled:true,assistant_sender_disabled:true,model_terminal_answer_tested:false,paid_requests:0};
}catch(error){result={state:'failed',error:String(error)};}
finally{writeFileSync(target,JSON.stringify(result,null,2));await browser.close();}
console.log(JSON.stringify(result));if(result.state!=='passed')process.exitCode=1;
