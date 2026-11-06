// 工程侧只读重验已有开发记录；不创建行程、占资源或推进任务。
import { chromium, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url));
const workspace=process.argv[2];
if(!['demo','demo-coupon-large','demo-coupon-small'].includes(workspace)) throw new Error('需要明确开发空间');
const record=name=>JSON.parse(readFileSync(path.join(root,'.local',name),'utf8').replace(/^\uFEFF/,''));
const cases=workspace==='demo' ? [
  {account:'personal_alice',id:record('personal-http-result.json').trip_id,amount:'1,100.00 模拟元'},
  {account:'employee_chen',id:record('corporate-http-result.json').trip_id,amount:'1,000.00 模拟元'},
] : record(workspace+'-http-result.json').trip_ids.map((id,index)=>({account:'personal_alice',id,
  amount:(workspace==='demo-coupon-large'?['800.00 模拟元','600.00 模拟元']:['1,000.00 模拟元','480.00 模拟元'])[index]}));
const browser=await chromium.launch({channel:'msedge',headless:true});
try {
  const page=await browser.newPage();page.setDefaultTimeout(20000);
  await page.goto('http://127.0.0.1:5173');
  for(const item of cases) {
    await page.getByLabel('演示身份',{exact:true}).selectOption(item.account);
    await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
    await expect(page.getByTestId('identity-id')).toHaveText(item.account);
    await page.getByTestId('trip-lookup').locator('summary').click();
    await page.getByLabel('本人行程 ID',{exact:true}).fill(item.id);
    await page.getByRole('button',{name:'打开行程',exact:true}).click();
    await expect(page.getByTestId('trip-id')).toHaveText(item.id);
    await expect(page.getByTestId('locked-amount')).toHaveText(item.amount);
    await expect(page.getByTestId('valid-ticket')).toBeVisible();
    await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
  }
  await page.screenshot({path:path.join(root,'.local','d1-05-preserved-'+workspace+'.png'),fullPage:true});
  console.log(JSON.stringify({workspace,existingTicketsRead:cases.length,browser:'passed'}));
} finally {await browser.close();}
