# 第十六批LLM实验：单券语义与单步任务恢复

> 公开脱敏说明：本机/服务器位置已改为仓库相对路径或私有配置占位项（`PRIVATE_*_DIR`）；本机私有记录未随仓库发布。原始材料在本机保留，历史命令不构成当前运行或费用许可。

后续进展：P30-02价格对象修复后，Q21获明确批准，首个B1第1轮因单步违约正式503而停批；新增1次/.00586184，累计29/.10683192/未决0，无动作，详[真实报告](30价格对象修复.md)与[原Q21范围](30价格对象恢复审查.md)。旧Q20的16冻结实现已先归档至.local/d3-30-recovery/frozen-q20，当前源码随后修订，下方“源码匹配”均为当时快照，不能用新源码冒充旧实验。Q20真实失败/费用和停止保持。

更新：2026-10-20，Q20已明确批准后首轮真实交付失败，整批关闭。**本轮真实尝试已收尾，业务两票任务未完成。** 用户明确回复：“批准Q20：按审查包执行四任务，最多20次，会计预留0.640元，遵守所列停止规则。”实际原文和匹配的原manifest保存decision（本机私有记录，未发布：`../.local/d3-30/decision.json`）。此前“完成了吗，那个审查批准”的含糊表述曾被自动审核拒绝写入，命令未执行；只有上述后续明确答复才建立许可。

## Q20真实结果与停止

按冻结顺序执行首个DEV27-05 / B1，第1轮模型产生候选，但正式`POST /assistant/chat`返回503 / assistant_result_unavailable；未进入忠实动作确认。内部最小收据在线记录`price_validity_conflict`，此次已能直接关联input/candidate hash，区别于29只保存generic503。控制器exit1，API及55433监督进程均exit0。Q20停止侧车已持久关闭，余19次不续用、不重试。

| 已批准顺序 | 实际结果 |
| --- | --- |
| DEV27-05 / B1，最多9轮 | 第1轮真实请求，正式503，任务失败 |
| DEV27-05 / B0，最多9轮 | 未启动；交付失败关闭全批 |
| DEV27-08 / B0，最多1轮 | 同上 |
| DEV27-08 / B1，最多1轮 | 同上 |

AI启动分母1、忠实完成0，无业务动作、工程补步、付款或票据。原汇总completion_rate=null保持，不改成成功；没有公平配对收益。29失败继续独立保留。

实际执行命令：

```powershell
.venv\Scripts\python.exe -X utf8 scripts/round30_task.py init-grant --directory ./.local/d3-30/review --decision ./.local/d3-30/decision.json
.venv\Scripts\python.exe -X utf8 scripts/round30_task.py run --review ./.local/d3-30/review --directory ./.local/d3-30/live-1 --task-sequence 1
```

init-grant exit0；run因p29_formal_delivery_failed exit1（共用运输保留旧错误名，实际mode=Q20）。**上述命令为已执行历史，不能复制重跑。** 最终16实现与原manifest未改，官方模型/价格实发前重新核对相同，无重新prepare或旧回归。

## 失败归因：价格对象守卫误拦

原候选（本机私有记录，未发布：`../.local/d3-30/live-1/model-01.json`）明确整张券归属、另一单重新核价、两份预报价不能相加，没有29“剩80元”或联合总额推断；提出对当前选中行程锁价。输出187 token，非512截断。未交付候选不能计完整任务成功，单例也不能证明新版稳定改善。

在线收据（本机私有记录，未发布：`../.local/d3-30/live-1/model-runs/rejections/5ca1245e0747b1a3612d4b561de3d3f8.json`）记录price_validity_conflict；零模型离线诊断（本机私有记录，未发布：`../.local/d3-30/live-1/content-diagnostic.json`）以实际source重建并验证display_state逐字回转，完整原候选复现同因。原句“锁价前确认报价仍有效”同时含锁价/报价标签，守卫把同一个“仍有效”同时校验两类对象：报价valid=true通过，不存在的lock失败。该句是报价核对条件，没有断言已有有效锁价；这是宿主价格对象语义误绑定，不能归为模型复犯29资源错误。

未来“报价到期2026…00:05”单独通过，不是把未来到期时间当已过期。离线只将“锁价前”换成“操作前”后完整原守卫通过；该措辞对照仅用于诊断，未修改原候选或在线记录。原shared/single_action守卫对原候选均通过。后续最小修复应将有效性断言绑定到所描述的价格对象，保留无有效锁却声称有效的拒绝；本轮不改已冻结源码、不续发Q20。

## 用量、保护与收尾

第28次输入2804/输出187，命中896/未中1908，已结算：`1908×2/1e6 + 896×0.04/1e6 + 187×8/1e6 = 0.00534784 CNY`。累计28次 / **0.10097008 CNY** / 未决0，供应商账单未独立对账。0.640是最多会计预留，本次没有全额消费。

实际messages+tools 11508字节，B1源/实际逐字相同；只更新本轮采集时间和30秒TTL，不改非时间语义、Prompt/知识/工具/判据。咨询前后23表聚合hash和行数一致，锁/付款/票据均0；不重新启动PG作重复查询。独立复核（本机私有记录，未发布：`../.local/d3-30/independent-live-review.json`）重新关联批准、输入、intent→账本usage、内部收据及正式失败，原27笔/身份/旧八侧车保持。定向保全（本机私有记录，未发布：`../.local/d3-30/live-preservation.json`）17份非账本旧原件保持；最初检查未规范化Windows分隔符，误把预期追加的账本留在“不变文件”集合，初始失败保留后仅纠正排除，不掩盖真实变化。

15:20:28（Asia/Shanghai）实际生命周期（本机私有记录，未发布：`../.local/d3-30/live-lifecycle.json`）确认controller exit1、API/PG监督exit0，8000/5173/55432/55433无监听，数据保留。原55432未启动/reset；原Q16–Q19关闭保持，无安装、全局配置、提交、推送或发布。Q20实测失败已收尾，不再待批准，不生成31同义准备入口；下一实质工作是有界局部修复价格对象守卫及必要反例，新实发仍需新的具体审查/明确批准。Q5、完整公平矩阵、跨版本、自主探索、8B/训练和首演缺项保持。

实际独立只读复核38项通过；最终定向文档检查exit0，9份修改文档/381本地链接/围栏编码通过，live-documentation-check.json保存结果。检查模型/HTTP/PG0，16实现不变，未重跑旧回归。Git仍main无提交、交付未跟踪，无暂存/提交/推送。

---

## 2026-10-20批准前本地交付（历史）

日期：2026-10-20（Asia/Shanghai）。D3-01 / P30-01，沿[30入口](../prompts/30-单券语义与单步任务恢复.md)。**本地部分已验证，新批Q20待明确批准；真实任务研究未完成。** 本轮模型0、真实许可0、费用0，累计27次 / 0.09562224 CNY / 未决0，Q16–Q19关闭保持。29首轮交付失败仍在原分母，不以本轮替身或修订版本替换。

## 实现与边界

[收据模块](../services/guide/reply_receipts.py)仅在答复/导航校验拒绝点记录白名单原因、candidate/input hash及已有推理报告名；输出在推理目录rejections下，不包含原文、状态、源码或堆栈。产品继续generic503，收据不进模型/历史。观察、身份变化、运输或OSError不冒充内容失败；无法写收据时仍保持拒绝。

[assistant-7](../services/guide/assistant.py)新增单券不可拆分/cap非余额/首个合格锁价归属/另一单重新核价，以及每轮一个“当前动作：”和必要核对点；[公开资料](../services/business/assistant_projection.py)revision2仅由新版选用，旧SYSTEM6和文档revision1保留。单步守卫只检查有限格式及显式串联，不声称完整理解自然语言。原金额、权限、总价条件、导航和终态守卫保持；未把目标路线、答案总额、STEPS或付款出票顺序写入Prompt。

新候选`kb-f9986b39a2af0d2dcfee`仅在30独立测试发布副本选用，原26 active/知识与观察不变。[公平包装](../services/guide/fair_runtime.py)、[scope](../services/guide/task_scope.py)、[runtime](../services/guide/task_runtime.py)精确冻结7，运行中不能换版本，旧默认6保持。B0/B1共同契约在新B1首发前冻结，不能与29拼配对。

[新入口](../scripts/round30_task.py)复用[29运输](../scripts/round29_task.py)，仅参数化新批/目录/候选；没有复制工作流。预算公共算法增加batch=Q20：同原账本前缀27、独立Q20侧车、绑定Q16–Q19八旧侧车，adapter/experiment和controller全链选新批。四任务9/9/1/1、20物理、300秒任务/1200秒全批、0.032/0.640会计池不变，详[具体审查](30单步任务外发审查.md)。

## 实际验证

| 检查与实际命令概要 | 结果 |
| --- | --- |
| 改前test_assistant基线；收据+宿主集中检查 | 146/9.68秒；154/11.18秒，exit0 |
| 并行语义合并后仅新收据风险复验 | 8/1.64秒，exit0 |
| 语义改前shared_prompt21/task_scope/round28_fair；新增single_action30集中检查 | 87/1.32秒；101/1.50秒，exit0 |
| 补显式串联反例后只跑single_action30 | 15/0.72秒，exit0 |
| Q19预算基线；Q19/Q20及旧批保全/adapter代表集中检查 | 13/5.94秒；31/12.82秒，exit0 |
| round30_entry+round29_confirmation；新入口测试隔离后只跑新6项 | 15/1.73秒；6/1.12秒，exit0 |
| 29原候选→当前正式ASGI chat→最小拒绝收据 | 503/amount_unverified；原输入/display状态逐字回转，模型/PG/外部HTTP0 |
| prepare：真实55433原业务API采集四初始输入/scope | exit0，模型发送0/预算预留0/许可0/账本不变 |
| rehearse：同7会话两次正式HTTP200→原GET→可见反馈 | controller/API exit0，双guard发送0/预留0；替身AI分母0 |

各组有重叠不相加。完整命令/JUnit见收据检查（本机私有记录，未发布：`../.local/d3-30/receipt-checks.json`）、预算检查（本机私有记录，未发布：`../.local/d3-30/q20-budget-validation.json`）、统一检查记录（本机私有记录，未发布：`../.local/d3-30/checks.json`）。单元检查仅临时文件/Mock，无真实模型或业务PG；prepare/rehearse才用已核55433，不重跑完整替身四任务、原55432基线、隔离probe或全历史hash。

保留两次检查失败：预算首跑23过/1失败是测试错误期待停止优先于故意错误hash，修正测试后通过；根入口首跑13过/2失败定位round_receipt多读旧最小测试ledger缺失的stop属性，局部改回只读grant路径后15通过。随后新批准测试改为构造ledger前截断，不依赖真实许可存在性，6项通过。未把失败输出删除或当通过，未因此扩跑旧全量。

29原候选复核（本机私有记录，未发布：`../.local/d3-30/receipt-replay/result.json`）实际走当前正式ASGI `/assistant/chat`、原7份来源hash不变，仍拒绝为`amount_unverified`。这是零模型重放的新内部收据，不能倒填29在线堆栈；不把所有503归给模型内容。新输出未真实验证效果。

实际消息+工具初始字节依次11508/7931/7925/11502，最大11508；完整payload多37字节。后续逐轮重新核双绑定和12KiB，不放宽。两票完成与最优性判据分别冻结，无实跑参考不声称最优。独立输入审查（本机私有记录，未发布：`../.local/d3-30/independent-input-review.json`）和保全结果（本机私有记录，未发布：`../.local/d3-30/preservation-after.json`）保存最终复核。18份相关旧原件/账本/配置均保持，Q20 grant/stop均不存在；其余未改历史复用29证据。

## 待批准与恢复

最终manifest SHA256为`d3e0962bec1e4bfa1a8983adc376e144886c128b48ff8e673154b77025da340e`，16份实现及全部四组初始源/实际/scope冻结，prepare初版另存不覆盖。新批准问题只见[Q20具体审查](30单步任务外发审查.md)。未批准时不创建decision/grant或发送；不重复prepare/rehearse来等待，不生成31。

明确批准后保存实际原文、来源、package=`Q20-P30-single-step-task`、approved=true和匹配manifest到`.local/d3-30/decision.json`，先核端口/runner锁/旧停止记录，再启动既有55433监督入口。下列为恢复操作，**本轮未执行真实init/run**：

```powershell
Set-Location -LiteralPath '.'
# 独立受监督会话；重复标签拒绝，不能删旧标记腾路径
.venv\Scripts\python.exe -X utf8 .local/d3-30/pg-supervisor.py live-1
# 另一执行会话，仅明确批准且review仍满足当前日价格和最终hash后
.venv\Scripts\python.exe -X utf8 scripts/round30_task.py init-grant --directory ./.local/d3-30/review --decision ./.local/d3-30/decision.json
.venv\Scripts\python.exe -X utf8 scripts/round30_task.py run --review ./.local/d3-30/review --directory ./.local/d3-30/live-1 --task-sequence 1
```

每次正式交付后，工程代理在300秒期限内忠实引用明确建议写confirmation-NN.json，禁止SQL或旧路线补计划；无真实导航回执不ack。首任务完成/明确失败后，仅按批准停止规则和顺序使用新live-2/3/4目录。进程失联不得迁旧会话；未知usage/交付失败停全批。全批结束创建`pg-live-1.stop`让监督进程正常停止，保存独立SQL/费用/停止与端口结果。若跨日，实发前重核官方模型/价格并保留日期刷新副本、原批准包及原决定；字段/参数/预算变化须重新明确决定，不能凭日期刷新扩范围。

本地预演API exit0、55433正常停止且数据保留，8000/5173/55432/55433均无监听；原55432未启动/reset。产物`./.local/d3-30`，复用原`.venv`/PG，无安装、全局配置、提交、推送或外部发布。Q5、完整公平真实任务、跨版本、自主探索、8B/训练及Q2四类首演保持未完成。里程碑精简检查未新增空模块/通用框架；未来可合并旧历史文档重复状态，但本轮不做大规模整理。

最终交付定向检查exit0：19份Python AST、9份修改Markdown/369本地链接/围栏编码和16实现hash通过。首次检查在写入聚合checks.json之前扫描该链接，唯一缺项在文件生成后单独补核通过，初始结果保留，不重跑其余检查。语义三组仅工具输出、未保存JUnit，具体命令已汇入checks.json。12:13:21（Asia/Shanghai）实际端口均无监听，pg_ctl stop exit0；监督进程退出码未捕获，不将其推断成实测0。Git仍main无提交、交付未跟踪，无暂存/提交/推送。
