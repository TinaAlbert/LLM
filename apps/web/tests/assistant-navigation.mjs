// Explicit two-call navigation experiment. Existing synthetic state, no business mutations.
import { chromium,expect } from '@playwright/test';
import { readFileSync,writeFileSync,existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url)),out=path.join(root,'.local/d3-15');
const target=path.join(out,'browser-navigation-v3.json');if(existsSync(target))throw new Error('Do not repeat paid phase');
const review=JSON.parse(readFileSync(path.join(out,'knowledge-review.json'),'utf8'));if(!review.approved_for_local_demo)throw new Error('Review required');
const browser=await chromium.launch({channel:'msedge',headless:true});const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(20000);
let result={state:'started'},calls=0;
const allowedQuestions=['请带我找到付款资料的页面。','请根据当前状态说明我接下来该做什么，不需要再次切换页面。'];
await page.route('**/api/assistant/chat',route=>{const body=route.request().postDataJSON();if(calls>=2||body.message!==allowedQuestions[calls++])return route.abort();return route.continue();});
async function responseFor(endpoint,action){const pending=page.waitForResponse(r=>new URL(r.url()).pathname===`/api/assistant/${endpoint}`);await action();const r=await pending,b=await r.json();if(r.status()!==200)throw new Error(`${endpoint}: ${r.status()} ${JSON.stringify(b)}`);return b;}
async function ask(text){await page.getByLabel('咨询问题',{exact:true}).fill(text);return responseFor('chat',()=>page.getByRole('button',{name:'发送',exact:true}).click());}
try{
 await page.goto('http://127.0.0.1:5173');await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
 await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();await page.getByText('知识管理',{exact:true}).click();
 await page.getByLabel('候选版本',{exact:true}).selectOption(review.candidate_version);
 const published=await responseFor('admin/publish',()=>page.getByRole('button',{name:'上线所选版本',exact:true}).click());
 result.published_version=published.active_version;
 await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'我的行程',exact:true}).click();
 const ack=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/ack').catch(()=>null);
 const proposed=await ask(allowedQuestions[0]);
 expect(proposed.tool?.name).toBe('navigate');expect(proposed.tool?.arguments).toEqual({page:'profile'});
 const ackResponse=await ack;expect(ackResponse).not.toBeNull();expect(ackResponse.status()).toBe(200);result.ack=await ackResponse.json();delete result.ack.session_id;
 await expect(page.getByRole('log')).toContainText('页面已打开');
 await expect(page.locator('[data-assistant-page="profile"]')).toBeFocused();
 expect(await page.locator('[data-assistant-page="profile"]').evaluate(el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.top<innerHeight;})).toBe(true);
 result.actual_navigation='profile';
 const continued=await ask(allowedQuestions[1]);expect(continued.session_id).toBe(proposed.session_id);expect(continued.tool).toBeFalsy();
 await expect(page.getByRole('log')).toContainText(allowedQuestions[0]);
 result.continuation=continued.reply;result.conversation_retained=true;
 await page.screenshot({path:path.join(out,'real-navigation.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 await expect(page.getByRole('button',{name:'✦ 收起咨询',exact:true})).toBeVisible();await page.screenshot({path:path.join(out,'narrow-assistant.png'),fullPage:true});
 await page.setViewportSize({width:1440,height:1000});
 const rolled=await responseFor('admin/rollback',()=>page.getByRole('button',{name:'回滚版本',exact:true}).click());expect(rolled.active_version).toBe('builtin-v1');result.rollback_version=rolled.active_version;
 await responseFor('admin/publish',()=>page.getByRole('button',{name:'上线所选版本',exact:true}).click());result.final_active=published.active_version;result.state='passed';
}catch(error){result.state='failed';result.error=String(error);await page.screenshot({path:path.join(out,'navigation-failure-v3.png'),fullPage:true});}
finally{writeFileSync(target,JSON.stringify(result,null,2));await browser.close();}
console.log(JSON.stringify(result));if(result.state!=='passed')process.exitCode=1;
