// Original UI driver. Offline mode intercepts every model request; live modes require Q14.
import {chromium,expect} from '@playwright/test';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root=fileURLToPath(new URL('../../..',import.meta.url));
const out=path.join(root,'.local/d3-19'),mode=process.argv[2],suffix=process.argv[3]??'';
if(!['offline','learn','consult','consult-recovery','release'].includes(mode))throw Error('invalid mode');
const reportPath=path.join(out,`${mode}${mode==='offline'?suffix:''}-browser.json`);
mkdirSync(path.join(out,'screenshots'),{recursive:true});
writeFileSync(reportPath,JSON.stringify({state:'started',mode}),{flag:'wx'});
const report={mode,state:'started',started_at:new Date().toISOString(),checks:[],responses:[],model_posts:0,business_writes:0};
let authorization='',failNavigation=false,delayChat=false,invalidTool=false,blockedReview=false,ackRequests=0;
const saved=JSON.parse(readFileSync(path.join(root,'.local/d3-18/pair-runs/ab639f7c6d107ee7920b8284d1123ceb.json'),'utf8'));
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1100}});
page.on('request',request=>{const token=request.headers()['authorization'];if(token)authorization=token;if(new URL(request.url()).pathname==='/api/assistant/ack')ackRequests++;});
page.on('response',async response=>{
  const url=new URL(response.url());
  if(!url.pathname.startsWith('/api/assistant/'))return;
  const row={path:url.pathname,status:response.status()};
  if(url.pathname.endsWith('/chat')||url.pathname.endsWith('/ack')){
    try{const body=await response.json();Object.assign(row,{reply:body.reply,error:body.error,tool:body.tool?.arguments,release_version:body.release_version,navigation_status:body.navigation_status});}catch{}
  }
  report.responses.push(row);
});
await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),method=request.method();
  if(url.hostname!=='127.0.0.1')return route.abort();
  const modelPath=['/api/assistant/chat','/api/assistant/admin/learning-build','/api/assistant/admin/train','/api/assistant/admin/explore','/api/assistant/admin/explore-finish'].includes(url.pathname);
  if(mode==='offline'&&modelPath){
    if(url.pathname.endsWith('/chat')){
      if(delayChat)await new Promise(resolve=>setTimeout(resolve,700));
      return route.fulfill({json:{session_id:'a'.repeat(32),release_version:'offline-replay',reply:saved.candidate.content,
        tool:{id:'offline-tool',name:'navigate',nonce:'b'.repeat(32),arguments:{page:invalidTool?'https://invalid.example':'trips'}}}}).catch(()=>{});
    }
    if(url.pathname.endsWith('/learning-build')){
      report.checks.push('构建请求由原UI发起；离线拦截，模型0次');
      const status=await page.request.get('http://127.0.0.1:8000/assistant/status',{headers:{Authorization:authorization}});
      const body=await status.json();
      return route.fulfill({json:{candidate_version:body.active_version,knowledge:{},validation:{offline:true}}});
    }
    return route.abort();
  }
  if(mode==='offline'&&url.pathname==='/api/assistant/ack'){
    const body=request.postDataJSON();
    if(!failNavigation)expect(body.success).toBe(true);
    return route.fulfill({json:{session_id:body.session_id,release_version:'offline-replay',reply:failNavigation?'未能确认导航完成，请手动打开页面。':'页面已打开，可以继续咨询。',navigation_status:failNavigation?'failed':'succeeded'}});
  }
  if(mode==='offline'&&blockedReview&&url.pathname==='/api/assistant/admin/review'){
    const response=await route.fetch(),body=await response.json();
    body.publishable=false;body.coverage.missing_conditions=[{page:'trips',selected_trip:true,zone_id:'booking.payment'}];
    return route.fulfill({json:body});
  }
  if(modelPath){
    const permitted=(mode==='learn'&&url.pathname.endsWith('/learning-build'))||(['consult','consult-recovery'].includes(mode)&&url.pathname.endsWith('/chat'));
    if(!permitted)return route.abort();
    report.model_posts++;
    if(report.model_posts>(mode==='consult'?3:1))return route.abort();
  }
  if(url.pathname.startsWith('/api/assistant/')&&method!=='GET'){
    const allowed=['/learning-start','/learning-capture','/learning-cancel'].some(p=>url.pathname.endsWith(p))||
      (['consult','consult-recovery'].includes(mode)&&url.pathname.endsWith('/ack'))||modelPath||
      (mode==='release'&&['/publish','/rollback'].some(p=>url.pathname.endsWith(p)));
    if(!allowed)return route.abort();
  }else if(url.pathname.startsWith('/api/')&&!['GET','HEAD'].includes(method)&&
    !(url.pathname==='/api/demo/sessions'&&method==='POST')&&!(url.pathname==='/api/session'&&method==='DELETE')){
    report.business_writes++;return route.abort();
  }
  return route.continue();
});
async function nav(name){await page.getByRole('navigation',{name:'工作台分区'}).getByRole('button',{name,exact:true}).click();}
async function click(name){await page.getByRole('button',{name,exact:true}).click();}
async function idle(){await expect(page.getByRole('button',{name:'停止等待',exact:true})).toHaveCount(0,{timeout:70000});}
async function sourceStep(label){const pending=page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/assistant/admin/learning-'));await click(label);const response=await pending;expect(response.status()).toBe(200);await idle();}
async function selected(){await page.locator('.trip-list button').first().click();await expect(page.getByTestId('valid-ticket')).toBeVisible();}
async function learningSources(){
  await nav('付款资料');await sourceStep('采集公开资料');
  await nav('我的行程');await sourceStep('采集选择前页面');
  await selected();await sourceStep('采集选择后页面');
  await expect(page.getByRole('button',{name:'构建条件知识',exact:true})).toBeEnabled();
  report.checks.push('同身份原UI采集公开页、未选及选择原行程后三份来源');
}
async function send(question){
  await page.getByLabel('咨询问题',{exact:true}).fill(question);
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/chat');
  await click('发送');const response=await pending;await idle();
  return response.status();
}
try{
  await page.goto('http://127.0.0.1:5173');await click('登录 / 切换身份');
  await expect(page.getByTestId('identity-id')).toHaveText('personal_alice');
  await expect(page.locator('.trip-list button')).toHaveCount(1);
  await click('✦ AI 咨询');await page.getByText('知识管理',{exact:true}).click();
  if(mode==='offline'||mode==='learn'){
    await learningSources();
    const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/assistant/admin/learning-build');
    await click('构建条件知识');const response=await pending;report.training_status=response.status();report.training=await response.json();await idle();
    if(mode==='learn'){
      report.user_visible=await page.getByRole('log').innerText();
      if(response.status()!==200){report.state='model_or_validation_failed';process.exitCode=1;}
      else await expect(page.locator('.assistant-review')).toContainText('尚未确认');
    }else{
      await expect(page.locator('.assistant-review')).toContainText('尚未确认');
      await expect(page.locator('.assistant-review')).toContainText('旧版候选');
      report.checks.push('原候选审查显示来源、未知、覆盖和旧契约限制');
      await nav('预订与票据');expect(await send('离线回放：完成情况并导航')).toBe(200);
      await expect(page.getByRole('log')).toContainText(saved.candidate.content);
      await expect(page.getByRole('log')).toContainText('正在尝试导航');
      await expect(page.getByRole('log')).toContainText('页面已打开');
      await expect(page.locator('[data-assistant-page="trips"]')).toBeFocused();
      report.checks.push('保存真实解释完整显示，提案/执行中及导航回执分开');
      failNavigation=true;await nav('预订与票据');await send('离线失败回执');
      await expect(page.getByRole('log')).toContainText('未能确认导航完成');failNavigation=false;
      report.checks.push('失败回执不覆盖先前解释、不宣称成功');
      invalidTool=true;await send('离线非法工具');await expect(page.getByRole('log')).toContainText('未执行导航');invalidTool=false;
      const priorMessages=await page.locator('.assistant-message.assistant-assistant').count(),priorAcks=ackRequests;
      const priorContext=await page.locator('.assistant-context').innerText();
      delayChat=true;await page.getByLabel('咨询问题',{exact:true}).fill('取消后迟到');await click('发送');await click('停止等待');
      await page.waitForTimeout(1000);await expect(page.getByRole('log')).toContainText('已停止等待');delayChat=false;
      expect(await page.locator('.assistant-message.assistant-assistant').count()).toBe(priorMessages);
      expect(ackRequests).toBe(priorAcks);expect(await page.locator('.assistant-context').innerText()).toBe(priorContext);
      report.checks.push('非法工具拦截与取消晚到结果');
      report.step='refresh-invalidates-capture';
      await click('取消行程选择');await nav('付款资料');await sourceStep('采集公开资料');
      await click('收起 AI 咨询');await click('刷新当前状态');await click('✦ AI 咨询');
      await page.getByText('知识管理',{exact:true}).click();
      await expect(page.getByRole('button',{name:'采集公开资料',exact:true})).toBeVisible();
      report.checks.push('刷新业务状态后旧采集作废');
      report.step='coverage-regression-review';
      const choices=await page.getByLabel('候选版本',{exact:true}).locator('option').evaluateAll(options=>options.map(o=>o.value).filter(Boolean));
      blockedReview=true;await page.getByLabel('候选版本',{exact:true}).selectOption(choices.find(v=>v!==report.training.candidate_version)??'');
      await expect(page.getByRole('button',{name:'上线所选版本',exact:true})).toBeDisabled();
      await expect(page.locator('.assistant-review')).toContainText('缺少当前版本条件');
      report.checks.push('覆盖回退在审查中展示且发布按钮禁用');
      await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    }
  }else if(mode==='release'){
    const version=suffix;if(!/^kb-[a-f0-9]{20}$/.test(version))throw Error('candidate required');
    await page.getByLabel('候选版本',{exact:true}).selectOption(version);
    await expect(page.locator('.assistant-review')).toContainText('尚未确认');
    for(const action of ['上线所选版本','回滚版本','上线所选版本']){
      const pending=page.waitForResponse(r=>['/api/assistant/admin/publish','/api/assistant/admin/rollback'].includes(new URL(r.url()).pathname));
      await click(action);const response=await pending;expect(response.status()).toBe(200);report.checks.push(action);await idle();
      if(action==='回滚版本')await page.getByLabel('候选版本',{exact:true}).selectOption(version);
    }
  }else{
    await selected();
    const allQuestions=[
      '请留在当前页，核对我这笔预订是否已经完成，还需要再次付款吗？',
      '请留在当前页。根据已发布知识说明“我的行程”选择行程前后有哪些区域变化，再区分这些历史规律与我本轮实际观察；未知请明确说。',
      '请解释我当前预订完成情况和是否需要再次付款，并带我打开“我的行程”查看进度。',
    ];
    const questions=mode==='consult-recovery'?[allQuestions[2]]:allQuestions;
    report.consultations=[];
    for(const [index,question] of questions.entries()){
      await nav('预订与票据');const start=report.responses.length,status=await send(question);
      const rows=report.responses.slice(start),chat=rows.find(r=>r.path.endsWith('/chat')),ack=rows.find(r=>r.path.endsWith('/ack'));
      const navigation=chat?.tool?(ack?.status===200&&ack.navigation_status==='succeeded'?'succeeded':'failed_or_unconfirmed'):'not_proposed';
      report.consultations.push({question,http_status:status,navigation,user_visible:await page.getByRole('log').innerText(),finished_at:new Date().toISOString()});
      if(status!==200){report.state='model_or_validation_failed';process.exitCode=1;break;}
      if((index===2||mode==='consult-recovery')&&navigation!=='succeeded'){report.state='navigation_incomplete';process.exitCode=1;}
    }
  }
  await page.screenshot({path:path.join(out,`screenshots/${mode}${mode==='offline'?suffix:''}.png`),fullPage:true});
  expect(report.business_writes).toBe(0);if(report.state==='started')report.state='finished';
}catch(error){report.state='failed';report.error=['Error','TimeoutError','AssertionError'].includes(error?.name)?error.name:'driver_error';
  await page.screenshot({path:path.join(out,`screenshots/${mode}${mode==='offline'?suffix:''}-failure.png`),fullPage:true}).catch(()=>{});process.exitCode=1;}
finally{
  if(authorization)await page.request.delete('http://127.0.0.1:8000/session',{headers:{Authorization:authorization}}).catch(()=>{});
  authorization='';await browser.close();report.finished_at=new Date().toISOString();writeFileSync(reportPath,JSON.stringify(report,null,2));
}
console.log(JSON.stringify({state:report.state,mode,checks:report.checks,model_posts:report.model_posts,training_status:report.training_status,error:report.error}));
