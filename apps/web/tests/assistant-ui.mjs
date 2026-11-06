// Offline model UI checks: all assistant chat responses are intercepted; no paid request.
import {chromium,expect} from '@playwright/test';
import {writeFileSync} from 'node:fs';import {fileURLToPath} from 'node:url';import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url));const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1200,height:900}});const checks=[];let delay=0,marker='离线咨询回答',invalidTool=false,ackCount=0;
await page.route('**/api/assistant/chat',async route=>{
 const reply={session_id:'a'.repeat(32),release_version:'builtin-v1',reply:marker,...(invalidTool?{tool:{id:'test',name:'navigate',nonce:'b'.repeat(32),arguments:{page:'https://invalid.example'}}}:{})};
 if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
 await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(reply)}).catch(()=>{});
});
await page.route('**/api/assistant/admin/**',route=>route.abort());
page.on('request',r=>{if(new URL(r.url()).pathname==='/api/assistant/ack')ackCount++;});
async function login(account){await page.getByLabel('演示身份',{exact:true}).selectOption(account);await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();await expect(page.getByTestId('identity-id')).toHaveText(account);}
async function send(text){await page.getByLabel('咨询问题',{exact:true}).fill(text);await page.getByRole('button',{name:'发送',exact:true}).click();}
let error;
try{
 await page.goto('http://127.0.0.1:5173');await login('personal_alice');await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();
 await send('离线检查');await expect(page.getByRole('log')).toContainText(marker);
 await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'我的行程',exact:true}).click();
 await expect(page.getByRole('log')).toContainText(marker);checks.push('跨分区保留对话');
 delay=700;marker='不得出现的迟到回答';await send('切身份反例');await login('personal_bob');
 await page.waitForTimeout(1000);await expect(page.getByRole('log')).not.toContainText(marker);await expect(page.getByRole('log')).not.toContainText('离线检查');
 await expect(page.getByText('知识管理',{exact:true})).toHaveCount(0);checks.push('换身份清除历史/丢弃晚到，Bob无管理权限');
 delay=0;invalidTool=true;const before=ackCount;await send('非法工具反例');await expect(page.getByRole('log')).toContainText('未执行导航');expect(ackCount).toBe(before);checks.push('非法目的地不执行且不伪造回执');
 invalidTool=false;delay=700;marker='旧页面回答不得出现';await send('页面变化反例');await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'付款资料',exact:true}).click();
 await expect(page.getByRole('log')).toContainText('旧回答和导航已丢弃');await expect(page.getByRole('log')).not.toContainText(marker);checks.push('页面改变丢弃旧回答');
 await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 await page.getByRole('button',{name:'收起 AI 咨询',exact:true}).click();await expect(page.getByRole('button',{name:'✦ AI 咨询',exact:true})).toBeVisible();checks.push('窄屏及收起入口持续存在');
}catch(e){error=String(e);}
finally{await browser.close();}
writeFileSync(path.join(root,process.argv[2]??'.local/assistant-ui-latest.json'),JSON.stringify({state:error?'failed':'passed',checks,error,paid_requests:0},null,2));
console.log(JSON.stringify({checks,error}));if(error)process.exitCode=1;
