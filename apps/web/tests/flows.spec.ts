import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { mkdirSync, writeFileSync } from 'node:fs';
const root=fileURLToPath(new URL('../../..',import.meta.url));
const screenshots=path.join(root,'.local/d3-14/screenshots');
mkdirSync(screenshots,{recursive:true});
const amount=(value:string)=>Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})+' 模拟元';
async function expand(page:Page,id:string) {
  const details=page.getByTestId(id);
  if(await details.getAttribute('open')===null)await details.locator('summary').click();
}
function control(...args:string[]) {
  const output=execFileSync(path.join(root,'.venv/Scripts/python.exe'),['-X','utf8',path.join(root,'apps/web/tests/control.py'),...args],{cwd:root,encoding:'utf8',windowsHide:true});
  return JSON.parse(output);
}
async function login(page:Page, account='personal_alice') {
  await page.getByLabel('演示身份',{exact:true}).selectOption(account);
  await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
  await expect(page.getByTestId('identity-id')).toHaveText(account);
  await expect(page.getByTestId('identity')).toBeVisible();
}
async function bind(page:Page) {
  await page.getByRole('button',{name:'绑定付款资料',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'绑定资料已受理'})).toBeVisible();
  await expect(page.getByRole('button',{name:'绑定付款资料',exact:true})).toBeEnabled();
}
async function create(page:Page,flight='DEMO-PEK-SHA') {
  const options=await page.getByLabel('航段与日期',{exact:true}).locator('option').evaluateAll(nodes=>nodes.map(node=>(node as HTMLOptionElement).value));
  const value=options.find(value=>value && JSON.parse(value).id===flight);
  expect(value).toBeTruthy();
  await page.getByLabel('航段与日期',{exact:true}).selectOption(value!);
  await page.getByRole('button',{name:'创建行程',exact:true}).click();
  await expect(page.getByRole('button',{name:'创建行程',exact:true})).toBeEnabled();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  await expect(page.getByTestId('trip-id')).toHaveCount(1);
  return (await page.getByTestId('trip-id').textContent())!.trim();
}
async function quote(page:Page) {
  await page.getByRole('button',{name:'获取暂估报价',exact:true}).click();
  await expect(page.getByTestId('quoted-amount')).toBeVisible();
}
async function lock(page:Page,amount:string) {
  await page.getByRole('button',{name:'确认最终锁价',exact:true}).click();
  await expect(page.getByTestId('locked-amount')).toHaveText(Number(amount).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})+' 模拟元');
}
async function pay(page:Page) {
  await page.getByRole('button',{name:/^确认支付冻结金额/}).click();
  await expect(page.getByTestId('payment-state')).toContainText('处理中');
  await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
}
async function refresh(page:Page) {
  await page.getByRole('button',{name:'刷新当前状态',exact:true}).click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
}
async function open(page:Page,id:string) {
  await expand(page,'trip-lookup');
  await page.getByLabel('本人行程 ID',{exact:true}).fill(id);
  await page.getByRole('button',{name:'打开行程',exact:true}).click();
  await expect(page.getByTestId('trip-id')).toHaveText(id);
}
async function finish(page:Page,id:string) {
  control('payment',id);
  await refresh(page);
  await expect(page.getByText('已付待出票，行程尚未完成。',{exact:true})).toBeVisible();
  await expect(page.getByTestId('valid-ticket')).toHaveCount(0);
  await page.screenshot({path:path.join(screenshots,'paid-awaiting-ticket.png'),fullPage:true});
  control('ticket',id);
  await refresh(page);
  await expect(page.getByTestId('valid-ticket')).toBeVisible();
}

test('受控观察：真实入口、明确隔离状态、身份切换及业务无变化',async({page})=>{
  control('reset','single-trip');await page.goto('/');await login(page);
  await create(page);
  const before=control('digest');
  const response=page.waitForResponse(r=>r.url().includes('/guide/observe?'));
  await page.getByRole('button',{name:'核对当前状态',exact:true}).click();
  const result=await response;
  expect(result.status()).toBe(200);
  await expect(page.getByTestId('controlled-observation')).toContainText('当前报价金额待确认');
  const body=await result.json();
  expect(body.source_kind).toBe('observed');expect(body.environment).toBe('local-demo');
  expect(body.simulated_identity).toBe('personal_alice');
  expect(body.data.payment).toBeNull();expect(body.data.ticket).toBeNull();
  expect(control('digest')).toEqual(before);
  // 真实服务已完成观察后延迟交付，切换身份必须丢弃旧结果。
  let release!:()=>void;
  const gate=new Promise<void>(resolve=>{release=resolve;});
  let captured!:()=>void;
  const pending=new Promise<void>(resolve=>{captured=resolve;});
  await page.route('**/api/guide/observe?*',async route=>{
    const upstream=await route.fetch();captured();await gate;
    try{await route.fulfill({response:upstream});}catch{/* 身份切换已取消此旧请求。 */}
  });
  await page.getByRole('button',{name:'核对当前状态',exact:true}).click();await pending;
  await login(page,'personal_bob');
  release();await page.unrouteAll({behavior:'wait'});
  await expect(page.getByTestId('controlled-observation')).toHaveCount(0);
  await expect(page.getByLabel('受控观察',{exact:true})).toHaveCount(0);
  expect(control('digest')).toEqual(before);
  writeFileSync(path.join(root,'.local/d2-01-browser-observation.json'),JSON.stringify({
    http_status:result.status(),isolation_success:result.status()===200,identity_cleared:true,business_unchanged:true,
  }));
});

test('个人：响应丢失复用操作 key，unknown 与 299 秒受理保护，独立出票',async({page})=>{
  control('reset','single-trip');
  await page.goto('/');await login(page);await bind(page);
  let originalKey='';let createCalls=0;
  await page.route('**/api/trips',async route=>{
    if(route.request().method()!=='POST') return route.continue();
    createCalls++;
    if(createCalls===1){originalKey=route.request().headers()['idempotency-key'];await route.fetch();await route.abort('failed');}
    else {expect(route.request().headers()['idempotency-key']).toBe(originalKey);await route.continue();}
  });
  await page.getByRole('button',{name:'创建行程',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('连接中断');
  await expect(page.getByRole('button',{name:'创建行程',exact:true})).toBeDisabled();
  await page.getByRole('button',{name:'重试原操作',exact:true}).click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  const id=(await page.getByTestId('trip-id').textContent())!;
  expect(control('counts').trips).toBe(1);expect(createCalls).toBe(2);
  await expect(page.getByTestId('trip-detail')).toContainText('金额待确认');
  await quote(page);await lock(page,'1100.00');
  control('advance','299');await pay(page);control('advance','2');
  await expand(page,'trip-edit');
  await expect(page.getByRole('button',{name:'保存行程修订',exact:true})).toBeDisabled();
  await expect(page.getByRole('button',{name:'取消尚未付款锁价',exact:true})).toBeDisabled();
  control('payment',id,'unknown');await refresh(page);
  await expect(page.getByTestId('payment-state')).toContainText('付款结果未知');
  await expect(page.getByTestId('valid-ticket')).toHaveCount(0);
  await page.screenshot({path:path.join(screenshots,'payment-unknown.png'),fullPage:true});
  control('payment',id,'lost_response');
  await page.getByRole('button',{name:'对账原付款',exact:true}).click();
  await expect(page.getByText('已付待出票，行程尚未完成。',{exact:true})).toBeVisible();
  control('ticket',id);await refresh(page);await expect(page.getByTestId('valid-ticket')).toBeVisible();
  expect(control('verify','personal')).toMatchObject({tickets:1,captured_minor:110000});
  await page.screenshot({path:path.join(screenshots,'personal-valid-ticket.png'),fullPage:true});
});

test('企业：网页选择预算、未指派反例、Lin 摘要批准及原修订付款',async({page})=>{
  control('reset','single-trip');await page.goto('/');await login(page,'employee_chen');await bind(page);
  const id=await create(page);await quote(page);await lock(page,'1000.00');
  const lockId=(await page.getByTestId('lock-id').textContent())!;
  await page.getByLabel('预算项目',{exact:true}).selectOption('A-eng');
  await page.getByRole('button',{name:'选择预算项目',exact:true}).click();
  await expect(page.getByTestId('lock-id')).toHaveText(lockId);
  await expect(page.getByRole('button',{name:'提交审批',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'提交审批',exact:true}).click();
  await expect(page.getByTestId('approval-state')).toContainText('待处理');
  await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
  const before=control('digest');
  await login(page,'approver_wu');
  await expect(page.getByText('暂无被指派的待审批申请。',{exact:true})).toBeVisible();
  expect(control('digest')).toEqual(before);
  await login(page,'approver_qian');
  await expect(page.getByText('暂无被指派的待审批申请。',{exact:true})).toBeVisible();
  expect(control('digest')).toEqual(before);
  await login(page,'approver_lin');
  await expect(page.getByRole('button',{name:'批准申请',exact:true})).toBeVisible();
  await expect(page.getByLabel('付款资料',{exact:true})).toHaveCount(0);
  await expect(page.getByLabel('我的行程',{exact:true})).toHaveCount(0);
  await page.screenshot({path:path.join(screenshots,'corporate-approver.png'),fullPage:true});
  await page.getByRole('button',{name:'批准申请',exact:true}).click();
  await expect(page.getByText('暂无被指派的待审批申请。',{exact:true})).toBeVisible();
  await login(page,'employee_chen');await open(page,id);
  await expect(page.getByTestId('lock-id')).toHaveText(lockId);
  await expect(page.getByTestId('approval-state')).toContainText('已批准');
  await pay(page);await finish(page,id);
  expect(control('verify','corporate')).toMatchObject({tickets:1,captured_minor:100000});
});

for(const first of ['large','small'] as const) test(`双行程：${first} 先锁，网页实际完成两票`,async({page})=>{
  control('reset','two-trips-one-coupon');await page.goto('/');await login(page);await bind(page);
  const large=await create(page,'DEMO-LARGE');await quote(page);await expect(page.getByTestId('quoted-amount')).toHaveText(amount('800.00'));
  const small=await create(page,'DEMO-SMALL');await quote(page);await expect(page.getByTestId('quoted-amount')).toHaveText(amount('480.00'));
  await expect(page.getByTestId('coupon-state')).toHaveText('可用');
  const ids=first==='large'?[large,small]:[small,large];
  const amounts=first==='large'?['800.00','600.00']:['480.00','1000.00'];
  for(let i=0;i<2;i++){await open(page,ids[i]);await lock(page,amounts[i]);}
  for(const id of ids){await open(page,id);await pay(page);await finish(page,id);}
  await expect(page.getByTestId('coupon-state')).toHaveText('已消费');
  expect(control('verify',first)).toMatchObject({tickets:2,captured_minor:first==='large'?140000:148000});
});

test('真实错误、修改和取消释放、跨用户、身份切换丢弃迟到读取',async({page})=>{
  control('reset','single-trip');await page.goto('/');await login(page);
  await page.getByLabel('乘机人数',{exact:true}).fill('0');
  await page.getByRole('button',{name:'创建行程',exact:true}).click();
  expect(control('counts').trips).toBe(0);
  await page.getByLabel('乘机人数',{exact:true}).fill('1');
  const id=await create(page);
  await page.getByRole('button',{name:'获取暂估报价',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('请先绑定');
  await bind(page);await quote(page);await lock(page,'1100.00');
  await expand(page,'trip-edit');
  await page.getByLabel('修改日期',{exact:true}).fill('2026-10-02');
  await page.getByRole('button',{name:'保存行程修订',exact:true}).click();
  await expect(page.getByTestId('trip-revision')).toHaveText('2');
  await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
  await quote(page);await lock(page,'1100.00');
  await page.getByRole('button',{name:'取消尚未付款锁价',exact:true}).click();
  await expect(page.getByRole('region',{name:'最终锁价',exact:true})).toContainText('已取消');
  await quote(page);await lock(page,'1100.00');control('advance','300');await refresh(page);
  await expect(page.getByRole('region',{name:'最终锁价',exact:true})).toContainText('已到期');
  await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
  let release!:()=>void;let entered!:()=>void;
  const ready=new Promise<void>(r=>entered=r),gate=new Promise<void>(r=>release=r);
  await page.route(`**/api/trips/${id}/booking`,async route=>{const response=await route.fetch();entered();await gate;try{await route.fulfill({response});}catch{/* identity switch aborted the old request */}});
  await page.getByRole('button',{name:'刷新当前状态',exact:true}).click();await ready;
  await login(page,'personal_bob');release();
  await expect(page.getByText('暂无行程。',{exact:true})).toBeVisible();
  await expect(page.getByTestId('trip-id')).toHaveCount(0);
  await page.unroute(`**/api/trips/${id}/booking`);
  const before=control('digest');
  await expand(page,'trip-lookup');
  await page.getByLabel('本人行程 ID',{exact:true}).fill(id);await page.getByRole('button',{name:'打开行程',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('对象不存在或当前身份无权访问');
  expect(control('digest')).toEqual(before);
  await expect(page.getByTestId('identity-id')).toHaveText('personal_bob');
  await expect(page.getByTestId('trip-id')).toHaveCount(0);
});

test('出票失败保留已付，页面不显示完成、不允许二次付款',async({page})=>{
  control('reset','single-trip');await page.goto('/');await login(page);await bind(page);
  const id=await create(page);await quote(page);await lock(page,'1100.00');await pay(page);
  control('payment',id);control('ticket',id,'failed');await refresh(page);
  await expect(page.getByText('出票失败，仍已付款；没有自动退款或第二次付款。',{exact:true})).toBeVisible();
  await expect(page.getByTestId('valid-ticket')).toHaveCount(0);
  await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
  expect(control('counts')).toMatchObject({payments:1,provider_payments:1,ticket_tasks:1,tickets:0});
  await page.screenshot({path:path.join(screenshots,'ticket-failed.png'),fullPage:true});
});

test('双击只创建一次；另一窗口修订后陈旧操作拒绝并刷新',async({page,context})=>{
  control('reset','single-trip');await page.goto('/');await login(page);await bind(page);
  let release!:()=>void;const gate=new Promise<void>(r=>release=r);
  await page.route('**/api/trips',async route=>{
    if(route.request().method()==='POST') await gate;
    await route.continue();
  });
  await page.getByRole('button',{name:'创建行程',exact:true}).dblclick();release();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  const id=(await page.getByTestId('trip-id').textContent())!;
  expect(control('counts').trips).toBe(1);
  const other=await context.newPage();await other.goto('/');await login(other);await open(other,id);
  await expand(other,'trip-edit');
  await other.getByLabel('修改日期',{exact:true}).fill('2026-10-02');
  await other.getByRole('button',{name:'保存行程修订',exact:true}).click();
  await expect(other.getByTestId('trip-revision')).toHaveText('2');
  const before=control('digest');
  await expand(page,'trip-edit');
  await page.getByLabel('修改人数',{exact:true}).fill('2');
  await page.getByRole('button',{name:'保存行程修订',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('状态已变化');
  await expect(page.getByTestId('trip-revision')).toHaveText('2');
  expect(control('digest')).toEqual(before);
  await expand(page,'trip-edit');
  await expect(page.getByLabel('修改日期',{exact:true})).toHaveValue('2026-10-02');
  await other.close();
});

test('本地来源与文件边界：测试答案、业务源码及控制入口不开放',async({request})=>{
  const before=control('digest');
  for(const url of ['/tests/control.py','/tests/flows.spec.ts?raw','/playwright.config.ts',
    '/@fs/'+path.join(root,'services/business/app.py').replaceAll('\\','/')]) {
    const response=await request.get(url,{headers:{Origin:'https://untrusted.example'}});
    expect(response.status()).toBe(403);
    expect(response.headers()['access-control-allow-origin']).toBeUndefined();
  }
  for(const url of ['/api/reset','/api/seed','/api/worker','/api/answers']) {
    expect((await request.get(url)).status()).toBe(404);
  }
  expect(control('digest')).toEqual(before);
});
