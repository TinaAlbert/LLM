// 无费用网页/交付协议检查；不计真实模型或完整隔离成功。
import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url));
function control(...args:string[]) {
  return JSON.parse(execFileSync(path.join(root,'.venv/Scripts/python.exe'),
    ['-X','utf8',path.join(root,'apps/web/tests/control.py'),...args],
    {cwd:root,encoding:'utf8',windowsHide:true}));
}
async function open(page:Page) {
  control('reset','single-trip');
  await page.goto('/');
  await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
  await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
  await page.getByRole('button',{name:'创建行程',exact:true}).click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  await expect(page.getByRole('button',{name:'求助 / 继续检查',exact:true})).toBeEnabled();
}
function response() {return {delegation_ref:'a'.repeat(32),valid_until:new Date(Date.now()+30000).toISOString(),
  completion_verified:false,candidate:{status:'next_step',explanation:'请核对本人付款资料。',
  preconditions:['选定本人的行程'],expected_change:'绑定后可申请报价',unknowns:[],references:['knowledge:profile.bind']},
  entry:{id:'profile.bind',path:'本人的付款资料 → 绑定付款资料'},amount:null};}

test('按需指引真实503与无任务零next、业务无副作用',async({page})=>{
  // 当前实际完整证据决定先在哪个前置停止；不接受任意503掩盖其他错误。
  const isolationReady=control('guide-runtime-ready').isolation_ready;
  const expectedError=isolationReady?'token_bound_unverified':'isolation_unavailable';
  const expectedText=isolationReady?'模型输入上界尚未核验':'指引的隔离环境尚未就绪';
  let calls=0;
  page.on('request',r=>{if(r.url().endsWith('/guide/next'))calls++;});
  await open(page);
  await page.getByRole('button',{name:'刷新当前状态',exact:true}).click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  expect(calls).toBe(0);
  const before=control('digest');
  const sent=page.waitForResponse(r=>r.url().endsWith('/guide/next'));
  await page.getByRole('button',{name:'求助 / 继续检查',exact:true}).click();
  const result=await sent;
  expect(result.status()).toBe(503);
  expect((await result.json()).error).toBe(expectedError);
  await expect(page.getByText('指引服务暂未就绪，你仍可在页面中自行预订。',{exact:true})).toBeVisible();
  await page.getByText('查看原因',{exact:true}).click();
  await expect(page.getByRole('status').getByText(expectedText,{exact:false})).toBeVisible();
  expect(calls).toBe(1);expect(control('digest')).toEqual(before);
  await expect(page.getByTestId('personal-guide')).toHaveCount(0);
});

test('受控网页响应：取消清理展示且原业务控件可用',async({page})=>{
  await open(page);
  const bodies:any[]=[];
  await page.route('**/api/guide/next',async route=>{
    const body=route.request().postDataJSON();bodies.push(body);
    await route.fulfill({json:body.action==='cancel'?{status:'cancelled'}:response()});
  });
  const before=control('digest');
  await page.getByRole('button',{name:'求助 / 继续检查',exact:true}).click();
  await expect(page.getByTestId('personal-guide')).toContainText('本人的付款资料');
  await page.getByRole('button',{name:'结束指引任务',exact:true}).click();
  await expect(page.getByTestId('personal-guide')).toHaveCount(0);
  await expect.poll(()=>bodies.filter(b=>b.action==='cancel').length).toBe(1);
  expect(bodies[1]).toEqual({action:'cancel',delegation_ref:'a'.repeat(32),request_id:expect.any(String)});
  await expect(page.getByRole('button',{name:'绑定付款资料',exact:true})).toBeEnabled();
  expect(control('digest')).toEqual(before);
});

test('受控网页响应：普通业务刷新作废且不自动求助',async({page})=>{
  await open(page);
  const bodies:any[]=[];
  await page.route('**/api/guide/next',async route=>{bodies.push(route.request().postDataJSON());await route.fulfill({json:response()});});
  await page.getByRole('button',{name:'求助 / 继续检查',exact:true}).click();
  await expect(page.getByTestId('personal-guide')).toBeVisible();
  await page.getByRole('button',{name:'刷新当前状态',exact:true}).click();
  await expect(page.getByTestId('personal-guide')).toHaveCount(0);
  expect(bodies.filter(b=>b.action==='next')).toHaveLength(1);
  await page.getByRole('button',{name:'求助 / 继续检查',exact:true}).click();
  await expect(page.getByTestId('personal-guide')).toBeVisible();
  expect(bodies[1].delegation_ref).toBe('a'.repeat(32));
});

test('受控网页迟到：切换身份后不交付旧个人指引',async({page})=>{
  await open(page);
  let release!:()=>void;
  const ready=new Promise<void>(resolve=>{release=resolve;});
  await page.route('**/api/guide/next',async route=>{
    if(route.request().postDataJSON().action==='next')await ready;
    try{await route.fulfill({json:response()});}catch{}
  });
  await page.getByRole('button',{name:'求助 / 继续检查',exact:true}).click();
  await expect(page.getByRole('button',{name:'核对中…',exact:true})).toBeVisible();
  await page.getByLabel('演示身份',{exact:true}).selectOption('personal_bob');
  await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
  await expect(page.getByTestId('identity-id')).toHaveText('personal_bob');
  release();
  await expect(page.getByTestId('personal-guide')).toHaveCount(0);
  await expect(page.getByRole('button',{name:'求助 / 继续检查',exact:true})).toHaveCount(0);
});
