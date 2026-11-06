// Explicit one-call knowledge experiment from reviewed synthetic page labels only.
import { chromium,expect } from '@playwright/test';
import { readFileSync,writeFileSync,existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url));const out=path.join(root,'.local/d3-15');
const target=path.join(out,'browser-train.json');if(existsSync(target))throw new Error('Do not repeat paid phase');
const reviewed=JSON.parse(readFileSync(path.join(out,'outbound-page-review.json'),'utf8'));
const browser=await chromium.launch({channel:'msedge',headless:true});let result={state:'started'},posts=0;
try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 await page.route('**/api/assistant/admin/train',route=>{
  const value=route.request().postDataJSON();
  if(++posts!==1||JSON.stringify(value)!==JSON.stringify({observations:reviewed.observations}))return route.abort();
  return route.continue();
 });
 await page.goto('http://127.0.0.1:5173');
 await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
 await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
 await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();
 await page.getByText('知识管理',{exact:true}).click();
 await expect(page.getByRole('button',{name:'训练（构建知识）',exact:true})).toBeEnabled();
 const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/admin/train');
 await page.getByRole('button',{name:'训练（构建知识）',exact:true}).click();
 const response=await pending;const details=await response.json();
 result={state:response.status()===200?'received':'failed',status:response.status(),details,reviewed_synthetic_payload_only:true};
 if(response.status()===200){await page.getByText('查看候选知识与验证结果',{exact:true}).click();}
 await page.screenshot({path:path.join(out,'training-candidate.png'),fullPage:true});
}catch(error){result={...result,state:'failed',error:String(error)};}
finally{writeFileSync(target,JSON.stringify(result,null,2));await browser.close();}
console.log(JSON.stringify(result));if(result.state!=='received')process.exitCode=1;
