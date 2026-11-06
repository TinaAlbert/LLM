# 第十五批LLM实验：首个真实持续任务在交付处失败

> 公开脱敏说明：本机/服务器位置已改为仓库相对路径或私有配置占位项（`PRIVATE_*_DIR`）；本机私有记录未随仓库发布。原始材料在本机保留，历史命令不构成当前运行或费用许可。

更新日期：2026-10-20（Asia/Shanghai）。D3-01 / P29-01，对应[29执行入口](../prompts/29-动态任务许可与真实持续指引.md)。

**当前为29规定的②：首个真实尝试失败，整批停止。** 用户已明确批准Q19，已执行DEV27-05 / B1首轮真实调用；模型产生候选，正式`POST /assistant/chat`返回503 / `assistant_result_unavailable`，候选没有交付给业务用户，未进入人工执行接点。控制器exit1；没有锁价、付款、查询或事件补步。已启动AI任务1个、忠实完成0个，失败保留在分母1；业务任务未完成，配对及未知反例未执行，不能比较B0/B1收益。

## 批准与实际矩阵

用户2026-10-20对完整具体问题明确回复：“批准Q19：上述四任务、动态合成字段和工程忠实执行范围，最多20次，会计预留0.640元，按所列停止规则执行”。决定原件（本机私有记录，未发布：`../.local/d3-29/decision.json`）及[正式决定](决策记录.md)已保存。原批准包manifest `06179fd8e5b44784afadbf38bff9f0a9bb986ae1cfb9865de9bccdd13fc77463`保留；20日官方模型/价格核对相同，只在独立live-review-20261020副本刷新实验日期，manifest `14e590a6f3cc3c231f757ef3f59725863034c73c46679f7b437b5f4862ffebbc`。未改Prompt、知识、工具、参数、判据或初始事实，没有重复请求批准。

| 固定顺序 | 实际结果 |
| --- | --- |
| DEV27-05 / B1，最多9轮 | 第1轮真实调用，正式交付503，任务失败 |
| DEV27-05 / B0，最多9轮 | 未启动；交付失败按批准规则关闭全批 |
| DEV27-08 / B0，最多1轮 | 同上 |
| DEV27-08 / B1，最多1轮 | 同上 |

实际命令：

```powershell
.venv\Scripts\python.exe -X utf8 scripts/round29_task.py init-grant --directory ./.local/d3-29/live-review-20261020 --decision ./.local/d3-29/decision.json
.venv\Scripts\python.exe -X utf8 scripts/round29_task.py run --review ./.local/d3-29/live-review-20261020 --directory ./.local/d3-29/live-1 --task-sequence 1
```

init-grant exit0；run因`p29_formal_delivery_failed` exit1，Q19停止侧车已持久写入。上述为实际执行记录，**不能复制重跑或用剩余19次续发**。先前审批服务额度失败未执行写入或调用；本次常规审批恢复后才创建许可。

## 真实候选与失败知识

原候选及用量（本机私有记录，未发布：`../.local/d3-29/live-1/model-01.json`）完整保存：2637输入/327输出（非512截断），命中1280/未命中1357，无工具调用。模型先指出两行程仅预报价、券未定、锁价后核最终金额，但随后把一张共享券解释成可将“剩80元”分配给另一行程，并把两份各自假设券可用的估价相加为1280元；两者不能作为共同可实现总价。此内容未被工程执行，也不能因为方向上建议先trip_1就算规划成功。

正式HTTP原件（本机私有记录，未发布：`../.local/d3-29/live-1/turn-01.json`）仅保存503与通用错误；不把离线推断冒充原服务已保存的内部异常。离线守卫复算（本机私有记录，未发布：`../.local/d3-29/live-1/offline-delivery-diagnostic.json`）通过保存来源与原回执重建状态，display_state逐字回转相同，完整validate_reply首异常为`amount_unverified`（首个不允许金额为“剩80元”，随后1280也无据）。单独运行共享守卫得到`shared_total_conditions_missing`，缺库存/资格条件，它不是完整校验首异常。复算没有模型/HTTP/数据库调用；原在线内部堆栈未保存，不能冒称运行日志直接记录该异常。工程侧只需补充宿主内部拒绝原因及candidate/input hash的最小记录，对用户仍返回通用错误；不要为此重建审计平台。最小后续方向是让模型围绕已证实当前状态只给一个明确下一步，在锁价及反馈后重算另一行程；共享券须明确独占占用，禁止把独立估价直接相加或虚构剩余额度。保留现有总价条件/权限/导航守卫，不为放行本答案削弱它们。本地局部改进可继续自主完成；新的真实外发输入/批次需具体审查与批准，不能复活Q19。

## 用量、保护与独立结果

实际首轮messages+tools为10748 UTF-8字节；task-binding.actual_bytes=10785计入max_tokens/json_output，差37字节属于统计口径，均小于12KiB。来源与实际B1输入逐字相同；当前采集时间/30秒TTL刷新，非采集时间语义与批准源相同。

第27次保守高峰记账：`1357×2/1e6 + 1280×0.04/1e6 + 327×8/1e6 = 0.0053812 CNY`；累计27次 / **0.09562224 CNY** / 未决0。0.640是获准最大会计预留，本次没有全额消费；供应商账单未独立对账。

独立审查（本机私有记录，未发布：`../.local/d3-29/independent-live-review.json`）重算27项：前26笔原顺序字节前缀、旧六侧车与配置保全，Q19 intent→实际输入→request→usage→正式失败关联成立，源与实际B1双绑定通过。首轮咨询前后23业务表hash相同；失败后独立SQL（本机私有记录，未发布：`../.local/d3-29/live-1/post-failure-independent.json`）再次确认无付款、无票据，未读SQL选动作。原55432未启动/reset；只在授权55433重建本任务独立同参数初态。无工程补步、无成功终态/新最优性证明。

任务记录（本机私有记录，未发布：`../.local/d3-29/live-1/task-record.json`）、原汇总（本机私有记录，未发布：`../.local/d3-29/live-1/task-summary.json`）、HTTP证据（本机私有记录，未发布：`../.local/d3-29/live-1/http-evidence.json`）、停止侧车（本机私有记录，未发布：`../.local/guide-usage.json.q19-stopped.json`）均保留。原汇总completion_rate为null且没有成功终态，不能改成成功；启动分母1、忠实完成0的实际计数明确报告。

本次不修改冻结源码，不重复准备、替身四任务或旧回归；下方已通过161/253/54/9组结果继续有效（重叠不累加）。仅增加本次原件/账本、必要独立终态、离线失败定位及修改文档检查。保存独立SQL时一次临时序列化命令因datetime类型失败，未写结果、未改业务；随后使用项目原save方法成功，非模型重试。

2026-10-20T11:42:47+08:00收尾：controller exit1（预期失败）、API exit0、55433监督exit0，8000/5173/55432/55433均无监听，数据保留，见live-lifecycle.json（本机私有记录，未发布：`../.local/d3-29/live-lifecycle.json`）。产物仍在`./.local/d3-29`；复用既有环境，无安装、全局配置变更、提交/推送/发布。Q5、完整公平任务、跨版本、自主探索、8B/训练及Q2四类首演仍未完成。

最终定向检查实际exit0：8份本次修改文档、333个本地链接、围栏/UTF-8检查均通过；8个冻结实现hash保持。命令为项目Python执行只读检查脚本（`.venv\Scripts\python.exe -X utf8 -`），结果`.local/d3-29/live-documentation-check.json`。模型/HTTP/数据库调用0，未重跑旧测试。Git仍main无提交、交付未跟踪，无暂存/提交/推送。

下一入口为[30单券语义与单步任务恢复](../prompts/30-单券语义与单步任务恢复.md)，由本次实际失败导出，尚未执行、不批准新批；它限定最小拒绝收据及单券/单步输出，不重开Q19。

补充30入口后，仅对5份新增/改动文档及7个新本地链接定向核对，exit0；原333链接与8实现核对复用，记录`.local/d3-29/next-entry-check.json`。未执行30、未产生新请求。

以下19日批准前报告保留为历史，旧init/run说明不再是当前恢复指令。

---

# 2026-10-19历史：动态任务入口与Q19待批准

日期：2026-10-19。任务D3-01 / P29-01。对应[29执行入口](../prompts/29-动态任务许可与真实持续指引.md)及[Q19具体审查](29动态任务外发审查.md)。

**结果为29规定的③：未获新批准，真实研究未完成。** 本轮已完成可审查的动态门、人工确认接点、当前精确输入与最小零模型预演；尚未运行首个真实双目标任务或公平配对，不能报告模型完成率/收益/失败结论。新增模型0、真实许可0、费用0；原账本26次 / 0.09024104 CNY / 未决0，Q16/Q17/Q18关闭保持。

## 已完成的接点与范围

[task_runtime](../services/guide/task_runtime.py)沿当前assistant-6/FairRuntime，把源投影→审核后共同历史→实际B0/B1输入分列hash；[task_scope](../services/guide/task_scope.py)校验完整源与严格有限动态schema，B0删状态仍须审批源共享字段。固定知识/Prompt/工具/主体/有序委托/版本/模型及本批业务参数，未来逐轮冻结，不伪造未来hash。

[task_budget](../services/guide/task_budget.py)及原budget/model/experiment最小接入沿同一账本/锁；26笔前缀、全部旧停止侧车、任务顺序、intent、防重放、正式交付确认、20物理上限及300/1200秒截止均受控。所有真实许可创建只在明确Q19后；普通22上限及原金额/速率/未知用量停止不改。

[round29_task](../scripts/round29_task.py)提供prepare、人工确认run、只读终态、两轮rehearse及最终manifest冻结。确认须引用同一正式交付和明确单动作/对象；条件/否定/歧义不能猜补。产品用户仍执行真实付款/审批，提案中的工程身份只在55433合成空间忠实操作。导航需要原页面证据，不伪造成功ack。API动作、浏览器动作及工程补步分别记。

异步规则改为批准前固定的同对象相对accepted序号：原付款后第1/2次已接受查询回执之后，分别推进原payment/ticket，拒绝/重复不计。它适配取消或反序路线，不沿28固定STEPS替模型决策。独立两票判据保留归属/当前修订/唯一付款-job-provider-task-ticket/锁金额/券一次/库存；不固定140000或券归trip_1。最优性仍为null，不能拿旧路线证明新规划。

## 实际验证与耗时

| 检查 | 实际结果 |
| --- | --- |
| 相关公平包装、Q18及预算适配器基线 | 161 passed /50.09秒 /exit0 |
| Q19新门及直接受影响的旧预算/模型路径集中回归 | 253 passed /125.10秒 /exit0 |
| 动态scope、确认及原公平包装 | 54 passed /7.99秒 /exit0 |
| 最后失败分母归因/确认定向复验 | 9 passed /2.26秒 /exit0 |
| 当前prepare | exit0；4精确初始输入；双guard发送0/预留0；许可0/账本未变 |
| 最小两轮rehearse | exit0；同会话2次正式HTTP200→2个原GET200→可见反馈；服务器及控制器双guard发送0/预留0；API正常exit0 |
| 28复用定向核查 | 5源码/23关键任务、SQL和guard原件hash匹配，复用原独立审查，不重跑四任务 |
| 定向保全 | 31关键原件hash均不变，26笔/旧侧车及配置保持；原55432未启动/reset |

命令、JUnit及证据见checks.json（本机私有记录，未发布：`../.local/d3-29/checks.json`），各组有重叠不累加。单元检查只用临时账本/MockTransport，不连接业务PG或模型；prepare/rehearse使用真实55433原业务API，但fixture不是模型输出，AI分母0。没有重跑未改企业业务、原网页、隔离probe或数千历史文件重哈希。

独立源码复核发现并修复跨轮physical_index冲突、导航词表不一致、连续等待反馈去重、超时/失联被宽泛语义前缀误分类；另用intent→账本request_id归因已发但解析/交付失败，避免已结算失败从分母消失。未知启动/usage保留未知，不猜0。最后归因路径改动仅做9项定向检查，成功两轮已有证据复用。

原Starlette/httpx弃用警告保持，未更新依赖。执行途中自动审批服务一度报告usage limit（非unsafe判定），部分代理被中断；正常审批随后恢复，已完成上述余项。早期中断记录（本机私有记录，未发布：`../.local/d3-29/INTERRUPTED.md`）保留为历史，不能替代本报告当前状态。

## Q19待决与恢复

[审查文件](29动态任务外发审查.md)已列固定DeepSeek目的地/模型、全部初始精确输入、后续允许变化、动作工程身份、9/9/1/1轮及20物理/.640会计池/停止规则。最终manifest SHA256：`06179fd8e5b44784afadbf38bff9f0a9bb986ae1cfb9865de9bccdd13fc77463`，原prepare manifest另存保留。源码/Prompt/知识/工具/任务判据已在首个B1发送前一并冻结；看完B1不能再改B0。

只在本次明确答复批准后，把实际原文和最终manifest hash写入本地decision.json，再执行：

```powershell
Set-Location -LiteralPath '.'
.venv\Scripts\python.exe -X utf8 scripts/round29_task.py init-grant --directory ./.local/d3-29/review --decision ./.local/d3-29/decision.json
.venv\Scripts\python.exe -X utf8 scripts/round29_task.py run --review ./.local/d3-29/review --directory ./.local/d3-29/live-1 --task-sequence 1
```

run持续等待本任务confirmation-NN.json，忠实抄录正式答中明确建议；不从SQL/固定路线补步。任务结束后按2/3/4序，使用新live-2/3/4独占目录；同一任务目录不重放。已启动后丢失进程/截止/用量未知关闭整批，不能重建会话自动恢复。成功或明确失败均留轨迹，不能获批后仍只交准备。

PG仅复用既有55433；启动前先确认端口/runner锁及pg-resume-stopped记录，沿本轮正常监督入口启动，不删除旧stop标记腾路径。跨日先重新核对官方模型/价格与实验日期，不把过期文件当新事实；若改变已审包按实际差异重新审查。未批准时不执行上述两条命令，不重复prepare/rehearse来填充等待。

产物在`./.local/d3-29`，项目及原.venv位置保留；无安装、全局环境/C盘配置/ACL/网络策略变更，无提交/推送/发布。Q5、完整公平真实任务、跨版本/自主探索、8B/训练和Q2四类首演仍缺。沿29继续，不生成30同义准备入口。

实际收尾：2026-10-19T23:49:16.305011+08:00（Asia/Shanghai），本轮API与55433/监督会话均正常exit0，8000/5173/55432/55433无监听；数据保留，见lifecycle.json（本机私有记录，未发布：`../.local/d3-29/lifecycle.json`）。最终11 Python AST、7份Markdown/310本地链接、围栏/编码定向检查通过。
