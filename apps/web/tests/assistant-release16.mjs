// Local reviewed publication and rollback; never sends a model request.
import {chromium,expect} from '@playwright/test';
import {readFileSync,writeFileSync} from 'node:fs';import {fileURLToPath} from 'node:url';import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url)),out=path.join(root,'.local/d3-16');
const review=JSON.parse(readFileSync(path.join(out,'knowledge-review.json'),'utf8'));if(!review.approved_for_local_demo)throw Error('Review required');
const target=path.join(out,'release.json');writeFileSync(target,'{"state":"started"}',{flag:'wx'});
const browser=await chromium.launch({channel:'msedge',headless:true});const page=await browser.newPage();let result={};
await page.route('**/api/assistant/**',route=>{const p=new URL(route.request().url()).pathname;return route.request().method()==='GET'||p.endsWith('/publish')||p.endsWith('/rollback')?route.continue():route.abort();});
async function click(name,endpoint){const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/admin/'+endpoint);await page.getByRole('button',{name,exact:true}).click();const response=await pending;expect(response.status()).toBe(200);return response.json();}
try{
 await page.goto('http://127.0.0.1:5173');await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
 await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();await page.getByText('知识管理',{exact:true}).click();
 await page.getByLabel('候选版本',{exact:true}).selectOption(review.candidate_version);
 const published=await click('上线所选版本','publish');expect(published.active_version).toBe(review.candidate_version);
 const old=await click('回滚版本','rollback');expect(old.active_version).not.toBe(review.candidate_version);
 const again=await click('上线所选版本','publish');expect(again.active_version).toBe(review.candidate_version);
 result={state:'passed',published:published.active_version,rollback:old.active_version,final_active:again.active_version,paid_requests:0};
}catch(error){result={state:'failed',error:String(error)};}
finally{writeFileSync(target,JSON.stringify(result,null,2));await browser.close();}
console.log(JSON.stringify(result));if(result.state!=='passed')process.exitCode=1;
