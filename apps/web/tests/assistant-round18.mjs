// Prompt18 release/terminal evidence through the original React controls.
// Services and the four-request policy must already be prepared by the operator.
import {chromium,expect} from '@playwright/test';
import {existsSync,mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root=fileURLToPath(new URL('../../..',import.meta.url));
const mode=process.argv[2];
if(!['release','terminal'].includes(mode))throw new Error('Usage: node tests/assistant-round18.mjs release|terminal');
const output=path.join(root,'.local/d3-18');
const reportPath=path.join(output,`${mode}.json`);
const startedPath=path.join(output,`${mode}.started.json`);
if(existsSync(reportPath)||existsSync(startedPath))throw new Error(`${mode} already has evidence; no automatic rerun`);
const prepared=JSON.parse(readFileSync(path.join(root,'.local/d3-16/browser-prepare.json'),'utf8'));
if(typeof prepared.trip_id!=='string'||!prepared.trip_id)throw new Error('Missing preserved trip reference');
let review,reviewHash;
if(mode==='release'){
  const bytes=readFileSync(path.join(output,'knowledge-review.json'));
  review=JSON.parse(bytes.toString('utf8'));
  if(review.approved_for_local_demo!==true||!/^kb-[a-z0-9]+$/.test(review.candidate_version??'')||
    !/^kb-[a-z0-9]+$/.test(review.previous_version??'')||review.candidate_version===review.previous_version)
    throw new Error('A reviewed candidate and distinct previous version are required');
  reviewHash=createHash('sha256').update(bytes).digest('hex');
}
mkdirSync(output,{recursive:true});
writeFileSync(startedPath,JSON.stringify({mode,started_at:new Date().toISOString()},null,2),{flag:'wx'});

const question='请留在当前页说明这笔行程现在是否已完成、是否还需要付款，以及选择行程前后哪些相关区域会出现；没有证据的部分请说明未知。';
const checks=[],trace=[],pendingResponses=[],navigationReceipts=[];
const counts={chat:0,ack:0,publish:0,rollback:0,blocked_assistant_posts:0,blocked_business_writes:0};
let browser,page,authorization='',error,sessionCleanup=null;
let reply=null,initialObservation=null,finalObservation=null,finalContext=null;
const approvedMutations=mode==='release'?['/api/assistant/admin/publish','/api/assistant/admin/rollback']:
  ['/api/assistant/chat','/api/assistant/ack'];
function safeMessage(cause){
  // The full request/response and browser authentication never enter this report.
  return ['Error','TimeoutError','AssertionError'].includes(cause?.name)?cause.name:'driver_error';
}
async function observe(name){
  return page.evaluate(async name=>(await import('/src/Assistant.tsx')).observePage(name),name);
}
async function releaseAction(action,expectedVersion){
  const responsePromise=page.waitForResponse(response=>new URL(response.url()).pathname===`/api/assistant/admin/${action}`&&response.request().method()==='POST');
  await page.getByRole('button',{name:action==='publish'?'上线所选版本':'回滚版本',exact:true}).click();
  const response=await responsePromise;
  expect(response.status()).toBe(200);
  const body=await response.json();
  expect(body.active_version).toBe(expectedVersion);
  await expect(page.locator('.assistant-admin')).toContainText(`当前版本：${expectedVersion}`);
  trace.push({action,status:response.status(),active_version:body.active_version});
}

try{
  browser=await chromium.launch({channel:'msedge',headless:true});
  page=await browser.newPage({viewport:{width:1440,height:1000}});
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.hostname!=='127.0.0.1')return route.abort();
    if(url.pathname.startsWith('/api/')&&request.headers().authorization)authorization=request.headers().authorization;
    if(url.pathname.startsWith('/api/assistant/')&&!['GET','HEAD'].includes(request.method())){
      if(request.method()!=='POST'||!approvedMutations.includes(url.pathname)){
        counts.blocked_assistant_posts++;return route.abort();
      }
      if(url.pathname==='/api/assistant/chat'){
        counts.chat++;
        if(counts.chat!==1){counts.blocked_assistant_posts++;return route.abort();}
      }
      if(url.pathname==='/api/assistant/ack'){
        counts.ack++;
        if(counts.chat!==1||counts.ack>1){counts.blocked_assistant_posts++;return route.abort();}
        const body=request.postDataJSON();
        navigationReceipts.push({success:body.success,observation:body.observation});
      }
      if(url.pathname==='/api/assistant/admin/publish'){
        counts.publish++;
        if(counts.publish>2||request.postDataJSON()?.candidate_version!==review.candidate_version){counts.blocked_assistant_posts++;return route.abort();}
      }
      if(url.pathname==='/api/assistant/admin/rollback'){
        counts.rollback++;
        if(counts.rollback>1){counts.blocked_assistant_posts++;return route.abort();}
      }
      return route.continue();
    }
    if(url.pathname.startsWith('/api/')&&!['GET','HEAD'].includes(request.method())&&
      !(url.pathname==='/api/demo/sessions'&&request.method()==='POST')&&
      !(url.pathname==='/api/session'&&request.method()==='DELETE')){
      counts.blocked_business_writes++;return route.abort();
    }
    return route.continue();
  });
  page.on('response',response=>{
    const pathname=new URL(response.url()).pathname;
    if(mode!=='terminal'||!['/api/assistant/chat','/api/assistant/ack'].includes(pathname))return;
    pendingResponses.push((async()=>{
      const body=await response.json();
      trace.push({action:pathname.endsWith('/ack')?'ack':'chat',status:response.status(),
        reply:typeof body.reply==='string'?body.reply:null,release_version:body.release_version??null,
        reason:body.reason??body.error?.code??null,
        tool:body.tool?{name:body.tool.name,page:body.tool.arguments?.page}:null});
    })());
  });
  await page.goto('http://127.0.0.1:5173');
  await page.getByRole('button',{name:'登录 / 切换身份',exact:true}).click();
  await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
  await page.getByText('通过行程编号打开',{exact:true}).click();
  await page.getByLabel('本人行程 ID',{exact:true}).fill(prepared.trip_id);
  await page.getByRole('button',{name:'打开行程',exact:true}).click();
  await expect(page.getByTestId('valid-ticket')).toBeVisible();
  await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
  checks.push('原保留行程已显示有效票，付款控件禁用');
  await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name:'预订与票据',exact:true}).click();
  initialObservation=await observe('booking');
  await page.getByRole('button',{name:'✦ AI 咨询',exact:true}).click();

  if(mode==='release'){
    await page.getByText('知识管理',{exact:true}).click();
    await expect(page.locator('.assistant-admin')).toContainText(`当前版本：${review.previous_version}`);
    await page.getByLabel('候选版本',{exact:true}).selectOption(review.candidate_version);
    await releaseAction('publish',review.candidate_version);
    await releaseAction('rollback',review.previous_version);
    await page.getByLabel('候选版本',{exact:true}).selectOption(review.candidate_version);
    await releaseAction('publish',review.candidate_version);
    expect(counts.publish).toBe(2);expect(counts.rollback).toBe(1);expect(counts.chat).toBe(0);
    checks.push('已审查候选经原管理员控件上线、回滚到原版本、恢复候选');
  }else{
    const responsePromise=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/assistant/chat'&&response.request().method()==='POST',{timeout:75000});
    await page.getByLabel('咨询问题',{exact:true}).fill(question);
    await page.getByRole('button',{name:'发送',exact:true}).click();
    const response=await responsePromise;
    expect(response.status()).toBe(200);
    await expect(page.getByRole('button',{name:'正在思考…',exact:true})).toBeHidden({timeout:75000});
    await Promise.all(pendingResponses);
    await expect(page.locator('.assistant-problem')).toHaveCount(0);
    await expect(page.locator('.assistant-assistant')).toHaveCount(1);
    reply=await page.locator('.assistant-assistant').innerText();
    expect(counts.chat).toBe(1);
    if(trace.some(item=>item.action==='chat'&&item.tool)){
      expect(counts.ack).toBe(1);
      expect(trace.find(item=>item.action==='ack')?.status).toBe(200);
      checks.push('模型导航提案由原UI执行并送原宿主回执，无模型续问');
    }
    checks.push('原UI仅发起一次终态咨询；语义正确性须由独立报告审查');
  }
  finalContext=await page.locator('.assistant-context').innerText();
  const pageNames={welcome:'欢迎与身份',profile:'付款资料',trips:'我的行程',booking:'预订与票据',approvals:'审批工作台'};
  const observedPage=Object.entries(pageNames).find(([,name])=>finalContext.startsWith(`当前：${name} ·`))?.[0];
  if(observedPage)finalObservation=await observe(observedPage);
  await expect(page.getByTestId('valid-ticket')).toBeVisible();
  await expect(page.getByRole('button',{name:/^确认支付冻结金额/})).toBeDisabled();
  expect(counts.blocked_assistant_posts).toBe(0);expect(counts.blocked_business_writes).toBe(0);
}catch(cause){error=safeMessage(cause);}
finally{
  if(page&&authorization){
    try{sessionCleanup=(await page.request.delete('http://127.0.0.1:5173/api/session',{headers:{Authorization:authorization}})).status();}
    catch{sessionCleanup='unconfirmed';}
  }
  authorization='';
  if(browser)await browser.close();
  const settled=await Promise.allSettled(pendingResponses);
  if(settled.some(result=>result.status==='rejected'))error??='Assistant response evidence unavailable';
  const report={state:error?'failed':'passed',mode,completed_at:new Date().toISOString(),checks,trace,counts,
    ...(mode==='release'?{candidate_version:review.candidate_version,previous_version:review.previous_version,review_sha256:reviewHash}:{question,reply,navigation_receipts:navigationReceipts}),
    initial_observation:initialObservation,final_observation:finalObservation,final_context:finalContext,session_cleanup_status:sessionCleanup,
    business_writes:0,error:error??null,
    limitation:'Browser evidence verifies rendered controls and original assistant calls; SQL and semantic answer review remain independent.'};
  writeFileSync(reportPath,JSON.stringify(report,null,2),{flag:'wx'});
  console.log(JSON.stringify({mode,state:report.state,checks,counts,error:report.error}));
}
if(error)process.exitCode=1;
