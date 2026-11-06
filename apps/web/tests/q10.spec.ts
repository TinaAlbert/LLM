// 原网页真实业务；工程控制只推进原任务和独立查询，不调用模型。
import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { mkdirSync } from 'node:fs';
import path from 'node:path';

const root=fileURLToPath(new URL('../../..',import.meta.url));
const screenshots=path.join(root,'.local/d3-14/screenshots');
function control(...args:string[]) {
  return JSON.parse(execFileSync(path.join(root,'.venv/Scripts/python.exe'),
    ['-X','utf8',path.join(root,'apps/web/tests/control.py'),...args],
    {cwd:root,encoding:'utf8',windowsHide:true}));
}
async function readable(page:Page) {
  const visible=await page.locator('body').innerText();
  expect(visible).not.toMatch(/personal_alice|demo[_-]|DEMO-|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{22,}/i);
  await expect(page.getByTestId('identity-id')).toBeHidden();
  await expect(page.getByTestId('trip-id')).toBeHidden();
  await expect(page.getByTestId('trip-revision')).toBeHidden();
  const layout=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,
    outside:[...document.querySelectorAll('button,input,select,textarea')].filter(element=>{
      const box=element.getBoundingClientRect();
      return box.width>0 && box.height>0 && (box.left < -1 || box.right > innerWidth+1);
    }).map(element=>element.tagName)}));
  expect(layout.scroll).toBeLessThanOrEqual(layout.width+1);
  expect(layout.outside).toEqual([]);
}
async function capture(page:Page,name:string) {
  await readable(page);
  await page.screenshot({path:path.join(screenshots,name+'.png'),fullPage:true});
}
async function refresh(page:Page) {
  await page.getByRole('button',{name:'刷新当前状态',exact:true}).click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
}

test('Q10：桌面与窄屏隐藏内部值，明确付款失败后真实恢复并独立出票',async({page})=>{
  mkdirSync(screenshots,{recursive:true});
  control('reset','single-trip');
  await page.setViewportSize({width:1440,height:1000});
  await page.goto('/');
  await page.screenshot({path:path.join(screenshots,'desktop-welcome.png'),fullPage:true});
  await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
  await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
  await expect(page.getByTestId('identity')).toContainText('Alice');
  await expect(page.getByLabel('航段与日期',{exact:true}).locator('option:checked')).toHaveText('北京 → 上海 · 2026-10-01');
  await page.getByRole('button',{name:'创建行程',exact:true}).click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  const tripId=(await page.getByTestId('trip-id').textContent())!;
  await expect(page.getByTestId('trip-detail')).toContainText('金额待确认');
  await expect(page.getByText(/不会真实扣款/)).toBeVisible();
  await capture(page,'desktop-draft-unknown-amount');
  await page.setViewportSize({width:390,height:844});
  await capture(page,'narrow-draft-unknown-amount');

  await page.getByRole('button',{name:'绑定付款资料',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'绑定资料已受理'})).toBeVisible();
  await page.getByRole('button',{name:'获取暂估报价',exact:true}).click();
  await expect(page.getByTestId('quoted-amount')).toHaveText('1,100.00 模拟元');
  await page.getByRole('button',{name:'确认最终锁价',exact:true}).click();
  await expect(page.getByTestId('locked-amount')).toHaveText('1,100.00 模拟元');
  const failedLock=(await page.getByTestId('lock-id').textContent())!;
  const pay=page.getByRole('button',{name:/^确认支付冻结金额/});
  await pay.click();
  await expect(page.getByTestId('payment-state')).toContainText('处理中');
  await expect(pay).toBeDisabled();
  const failedPayment=(await page.getByTestId('payment-id').textContent())!;
  control('payment',tripId,'failed',failedPayment);
  await refresh(page);
  await expect(page.getByTestId('payment-state')).toContainText('失败');
  await expect(page.getByTestId('valid-ticket')).toHaveCount(0);
  await expect(pay).toBeDisabled();
  expect(control('verify-recovery','failed',tripId)).toMatchObject({payments:1,failed_payments:1,tickets:0,captured_minor:0});
  const failedSnapshot=control('digest');
  expect(failedSnapshot.table_count).toBe(23);
  await refresh(page);
  expect(control('digest')).toEqual(failedSnapshot);
  await capture(page,'narrow-payment-failed');

  await page.getByRole('button',{name:'获取暂估报价',exact:true}).click();
  await page.getByRole('button',{name:'确认最终锁价',exact:true}).click();
  await expect(page.getByTestId('lock-id')).not.toHaveText(failedLock);
  await expect(page.getByTestId('locked-amount')).toHaveText('1,100.00 模拟元');
  await pay.click();
  await expect(page.getByTestId('payment-state')).toContainText('处理中');
  await expect(page.getByTestId('payment-id')).not.toHaveText(failedPayment);
  const recoveredPayment=(await page.getByTestId('payment-id').textContent())!;
  control('payment',tripId,'succeeded',recoveredPayment);
  await refresh(page);
  await expect(page.getByText('已付待出票，行程尚未完成。',{exact:true})).toBeVisible();
  await expect(page.getByTestId('valid-ticket')).toHaveCount(0);
  await expect(pay).toBeDisabled();
  await capture(page,'narrow-paid-awaiting-ticket');
  control('ticket',tripId,'succeeded',recoveredPayment);
  await refresh(page);
  await expect(page.getByTestId('valid-ticket')).toBeVisible();
  await expect(pay).toBeDisabled();
  expect(control('verify-recovery','succeeded',tripId)).toMatchObject({payments:2,failed_payments:1,tickets:1,captured_minor:110000});
  await capture(page,'narrow-recovered-valid-ticket');
  await page.setViewportSize({width:1440,height:1000});
  await capture(page,'desktop-recovered-valid-ticket');
});
