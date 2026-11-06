// Read-only outbound review: blocks every assistant action and all non-loopback requests.
import { chromium,expect } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url));
const browser=await chromium.launch({channel:'msedge',headless:true});
try{
 const page=await browser.newPage();
 await page.route('**/*',route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.hostname!=='127.0.0.1'||(url.pathname.startsWith('/api/assistant/')&&request.method()!=='GET'))return route.abort();
  return route.continue();
 });
 await page.goto('http://127.0.0.1:5173');
 await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
 await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
 const observations=[];
 for(const section of ['profile','trips']){
  const observation=await page.evaluate(page=>{
   const roots=document.querySelectorAll(`[data-assistant-page="${page}"],[data-assistant-part="${page}"]`);
   const items=[];
   for(const root of roots)for(const el of root.querySelectorAll('[data-assistant-public]')){
    if(!el.getClientRects().length||el.closest('.technical-details,details:not([open]),[hidden],[aria-hidden="true"]'))continue;
    const clone=el.cloneNode(true);clone.querySelectorAll('input,select,textarea,output,script,style,[hidden],[aria-hidden="true"]').forEach(x=>x.remove());
    const value=clone.textContent.replace(/\s+/g,' ').trim().slice(0,160);if(value&&!items.includes(value))items.push(value);
   }
   return {page,title:page==='profile'?'付款资料':'我的行程',items:items.slice(0,8),product_version:'v1'};
  },section);observations.push(observation);
 }
 const review={source:'local synthetic travel-lab test workspace; rendered DOM',no_business_values:true,no_hidden_details:true,no_html_source:true,no_credentials:true,no_external_requests:true,observations};
 writeFileSync(path.join(root,'.local/d3-15/outbound-page-review.json'),JSON.stringify(review,null,2));
 console.log(JSON.stringify(review));
}finally{await browser.close();}
