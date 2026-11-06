// Q15: exactly one original UI chat, real approved model, no retries or business writes.
import {chromium,expect} from '@playwright/test';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=fileURLToPath(new URL('../../..',import.meta.url)),out=path.join(root,'.local/d3-20');
const reportPath=path.join(out,'live-browser.json');
writeFileSync(reportPath,JSON.stringify({state:'started'}),{flag:'wx'});
const sample=JSON.parse(readFileSync(path.join(out,'sample.json'),'utf8'));
const report={state:'started',mode:'Q15_real_model',chat_posts:0,ack_posts:0,business_writes:0,responses:[]};
mkdirSync(path.join(out,'screenshots'),{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1100}});
let authorization='';
page.on('request',request=>{if(request.headers()['authorization'])authorization=request.headers()['authorization'];});
page.on('response',async response=>{
  const url=new URL(response.url());
  if(!['/api/assistant/chat','/api/assistant/ack'].includes(url.pathname))return;
  try{report.responses.push({path:url.pathname,status:response.status(),body:await response.json()});}catch{}
});
await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),method=request.method();
  if(url.hostname!=='127.0.0.1')return route.abort();
  if(url.pathname==='/api/assistant/chat'){
    report.chat_posts++;
    if(report.chat_posts!==1||request.postDataJSON().message!==sample.question)return route.abort();
    return route.continue();
  }
  if(url.pathname==='/api/assistant/ack'){
    report.ack_posts++;return report.ack_posts===1?route.continue():route.abort();
  }
  if(url.pathname.startsWith('/api/')&&!['GET','HEAD'].includes(method)&&
     !(url.pathname==='/api/demo/sessions'&&method==='POST')&&!(url.pathname==='/api/session'&&method==='DELETE')){
    report.business_writes++;return route.abort();
  }
  return route.continue();
});
try{
  await page.goto('http://127.0.0.1:5173');
  await page.getByLabel('演示身份',{exact:true}).selectOption('employee_chen');
  await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
  await expect(page.getByTestId('identity-id')).toHaveText('employee_chen');
  await expect(page.locator('.trip-list button')).toHaveCount(1);
  await page.locator('.trip-list button').first().click();
  await expect(page.getByTestId('trip-detail')).toBeVisible();
  await expect(page.getByTestId('trip-revision')).toHaveText('2');
  await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'预订与票据',exact:true}).click();
  await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();
  await page.getByLabel('咨询问题',{exact:true}).fill(sample.question);
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/chat',{timeout:65000});
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const response=await pending;
  report.chat_status=response.status();report.chat=await response.json();
  await expect(page.getByRole('button',{name:'停止等待',exact:true})).toHaveCount(0,{timeout:65000});
  report.user_visible=await page.getByRole('log').innerText();
  if(response.status()===200){
    await expect(page.getByRole('log')).toContainText(report.chat.reply);
    report.state='delivered';
  }else{report.state='not_delivered';process.exitCode=1;}
  expect(report.chat_posts).toBe(1);expect(report.business_writes).toBe(0);
}catch(error){report.state='failed';report.error=String(error);process.exitCode=1;}
finally{
  await page.screenshot({path:path.join(out,'screenshots','live-enterprise.png'),fullPage:true}).catch(()=>{});
  if(authorization)try{report.logout_status=(await page.request.delete('http://127.0.0.1:8000/session',
    {headers:{Authorization:authorization}})).status();}catch{}
  writeFileSync(reportPath,JSON.stringify(report,null,2));await browser.close();
}
console.log(JSON.stringify({state:report.state,chat_posts:report.chat_posts,chat_status:report.chat_status,
  ack_posts:report.ack_posts,business_writes:report.business_writes,reply:report.chat?.reply,error:report.error}));
