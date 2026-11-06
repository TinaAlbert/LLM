// New two-request learning UI contract; API inference is fully intercepted.
import {chromium,expect} from '@playwright/test';
import {writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url));
const browser=await chromium.launch({channel:'msedge',headless:true});const page=await browser.newPage({viewport:{width:1440,height:1000}});
let finish=0;const checks=[];
await page.route('**/api/assistant/**',async route=>{
 const url=new URL(route.request().url()).pathname;
 if(url.endsWith('/status'))return route.fulfill({json:{admin:true,ready:true,active_version:'builtin-v1',candidate_versions:[],allowed_pages:['welcome','profile','trips','booking']}});
 if(url.endsWith('/explore')){
  expect(route.request().postDataJSON().observation.page).toBe('profile');
  return route.fulfill({json:{session_id:'a'.repeat(32),release_version:'builtin-v1',tool:{id:'call-test',name:'navigate',arguments:{page:'trips'},nonce:'b'.repeat(32)}}});
 }
 if(url.endsWith('/explore-finish')){
  const value=route.request().postDataJSON();expect(value.success).toBe(true);expect(value.observation.page).toBe('trips');expect(value.observation.items.length).toBeGreaterThan(0);finish++;
  return route.fulfill({json:{candidate_version:'kb-'+'c'.repeat(20),knowledge:{entries:[],unknowns:['离线控件检查']},validation:{structure:'fake'},coverage_missing:[]}});
 }
 return route.abort();
});
try{
 await page.goto('http://127.0.0.1:5173');await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
 await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'付款资料',exact:true}).click();
 await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();await page.getByText('知识管理',{exact:true}).click();
 await page.getByRole('button',{name:'训练（构建知识）',exact:true}).click();await expect(page.getByRole('log')).toContainText('已生成候选知识');expect(finish).toBe(1);
 await expect(page.locator('[data-assistant-page="trips"]')).toBeFocused();checks.push('模型目标驱动实际导航与观察回执；仅一次归纳');
 await page.locator('.trip-list button').first().click();await expect(page.getByTestId('trip-detail')).toBeVisible();
 await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'预订与票据',exact:true}).click();
 await expect(page.getByRole('button',{name:'训练（构建知识）',exact:true})).toBeDisabled();checks.push('非公开学习分区禁用训练');
 writeFileSync(path.join(root,'.local/d3-16/learning-ui.json'),JSON.stringify({passed:checks,paid_requests:0},null,2));console.log(JSON.stringify(checks));
}finally{await browser.close();}
