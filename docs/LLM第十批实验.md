# 第24轮：Q17精确恢复批次与首题截断停止

> 公开脱敏说明：本机/服务器位置已改为仓库相对路径或私有配置占位项（`PRIVATE_*_DIR`）；本机私有记录未随仓库发布。原始材料在本机保留，历史命令不构成当前运行或费用许可。

日期：2026-10-18。任务D3-01 / P24-01，执行[24](../prompts/24-双目标恢复与独立交付.md)，接续[23报告](LLM第九批实验.md)。用户随后明确批准Q17。**本轮新增唯一第24次模型请求，首题正式chat HTTP503，原因为output_truncated；按约立即停止，第二题未发送，Q17剩余永久关闭。双目标真实交付恢复没有成功。** 本地许可、当前样本验证和保护收尾完成，不能以工程通过替代真实回答交付。

## 批准、前置复用与独立新许可

用户对完整Q17问题明确答复“批准Q17：最多新增2次并外发上述两份精确合成字段”。正式来源、字段和限制见[决定](决策记录.md#q17-双目标交付恢复的新两次精确请求--用户已明确批准)及[审查](23双目标恢复外发审查.md)，原件`.local/d3-24/decision.json`。不是把“执行24”当批准，也没有沿Q16失败批次重发。

23的24项完整探测在核查时仍有效，源码/启动器/二进制摘要一致；原37项检查、默认原生HTTP/SQL闭环、两精确预览及历史知识/回放证据复用。未重建二进制或重跑探索。该隔离结论只属于原独立explore-shell，不解释旧shell4551，也不计LLM自主探索。

[预算模块](../services/guide/budget.py)在同一原账本与跨进程锁内新增Q17 grant/stop侧车，绑定前23条完整摘要、ledger_id和旧Q16 grant/stop原始字节摘要。每个有序scope最多一次；先落盘intent再记pending，重放/错序/中断intent/未知usage/其他入口占用均失败关闭。普通22次上限、2/5元和0.5元提前停止保持，旧Q16停止文件未删改，没有第二本usage账本。

适配器/experiment只从刚校验的实际输入生成Q17许可；512、history0、非思考/非流式、原navigate schema和精确scope保持。新增protocol_report_dir使本轮解析失败不写旧15目录。[共享外发门](../services/guide/assistant.py)仅在显式路径接受P24-Q17-two，不自动打开默认入口。[新控制器](../scripts/round24_live.py)独立24目录、防覆盖/重启；第二题在controller和发送侧均要求首题HTTP200、SQL侧证及精确reply hash的独立内容复核通过。

## 当前样本与官方核查

先运行新的preflight-server/prepare，正式Assistant使用明确离线替身、双侧NoModelTraffic，真实HTTP/55433 SQL重建两个状态。两份HTTP200、每次咨询前后23业务表相同，输入仍10894/11227字节，history0/输出512、语义hash与批准预览逐值一致。分别确认券available/无锁价，以及trip_2唯一held锁价并占券；两者均未付款、无票。预演API正常退出、双guard发送0/预留0后，才实际init新grant。历史assistant-6知识组合逐字复制，保留原DeepSeek-V4.1-Flash构建修订，没有重学或发布。

即时重新读取[官方人民币价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)和[更新日志](https://api-docs.deepseek.com/zh-cn/updates/)：当前仍deepseek-flash→DeepSeek-V4.1-Flash-1017，人民币高峰输入未命中2/命中0.04/输出8元每百万token，低峰一半。最初中英文带斜杠价格读取超时，随后无尾斜杠中文地址重定向到官方当前页成功，失败尝试如实记在model-review.json。没有启用Search/Fetch。

原guide-model.json和单价/上限保持，仅将非秘密`.local/guide-experiment.json`的price_checked_on由10月17日刷新为18日；旧内容在本轮experiment-review-before.json，前后核查只允许该日期变化。服务修订和历史知识修订分开记录，没有改历史知识原件。

## 唯一第24次实测与信息保留限制

实际执行`python -X utf8 scripts/round24_live.py run`。唯一首题scope为`a95dba938b0a9b4bd854d3177685aa38a854d9a918b3bec2366253002f63d4bb`，10894字节、history0、输出512，与Q17批准输入一致。供应商响应的身份和usage通过适配器校验，先结算；候选解析发现finish_reason=length，报output_truncated，正式chat HTTP503，向用户交付的是错误，**没有可用回答或导航建议**。live report elapsed_seconds=3.700；这是本地报告时长，不是完整用户任务时延。

| 账本用量/结果 | 实际值 |
| --- | --- |
| 输入/输出 | 2671 / 512 token |
| 缓存命中/未命中 | 2048 / 623 token，合计2671 |
| 新增保守高峰核算 | 0.00542392 CNY |
| 累计 | 24次 / 0.08382128 CNY / 未决0 |
| 正式交付 | HTTP503，output_truncated |
| 第二题 | 没有attempt/payload/sample，未发送 |

金额为623×2/百万 + 2048×0.04/百万 + 512×8/百万；保守核算不是供应商发票对账。grant唯一intent、原账本第24行、protocol文件的内部请求ID逐项关联，前23笔保持。Q17停止原因为output_truncated，两scope实际check均拒绝q17_batch_stopped；旧Q16第二scope仍拒绝q16_batch_stopped。不能因本批只花1次而沿剩余次数重试，也未提高512上限或自动换问题。

**原始截断文字和原响应usage对象没有被保存。** 现有parser在返回ModelResult之前拒绝候选；live report只有input、unavailable/reason/时长，没有candidate或usage，raw-result-1未生成；protocol只保存有限协议形状。用量依据是校验后已结算的原账本，不能假称已回看完整响应、审查语义、验证推荐顺序、回放原回答或交付成功。本轮没有新的UI建议可核验，也没有原UI新实证。未来需要补最小失败收据时，必须记录为新实现，不能补造本次原始文字。

保存样本前后业务23表摘要相同。控制器在HTTP503后立即退出，原sample中的independent_sql=false/content pending是当时未进入后续成功分支的快照；本轮另用保存SQL独立重算，确认初始无锁/券available/无付款/无票。新failure-review.json记录该事实及失败状态，不回写原样本、不给passed=true内容复核。没有两张票或完整任务成功率。

## 本地检查与审查

| 检查 | 实际结果及范围 |
| --- | --- |
| Q17显式外发门及旧Q16反例 | 30 passed /2.75秒；临时文件、模型0 |
| 受影响预算/模型/experiment/Q16/Q17集中 | 179 passed /66.00秒；原账本0、真实模型0 |
| Q17最终工具兼容修正后新组 | 41 passed /25.68秒；与179重叠，不相加 |
| 新控制器最终组 | 21 passed /4.37秒；包含早期15/17，不相加；临时fake ledger，真实HTTP/PG/模型0 |
| 实际两份预览走最终prepare | 保留原navigate schema及两精确scope，零发送/预留/实际grant |
| 新当前HTTP/SQL预演 | 两份HTTP200、独立SQL及业务不变、双guard0 |

早期基线有2个setup error，原因是独立basetemp父目录尚未建立，建立后原2项通过；不是业务失败。独立审查在真实发送前发现并修正：额外拒绝获批navigate schema，以及仅controller等待首题review而发送侧未复核。修正后实际两预览经最终prepare通过、首题review门在attempt2及infer之前。后加逐笔reservation/provider/usage/金额匹配的控制器反例纳入最终21项。原37及无相关变更的大组未重跑，未跑默认全量PG测试。

独立代理在只读内存重新核对真实失败的输入、grant/账本关联、费用、SQL、停止预检和原件保全；[本轮审计脚本](../scripts/llm_round24.py)的review-failure另作相同可复核断言，模型/HTTP/PG调用0。结果failure-review.json，原失败不重写。

## 保护、停止与接续

before/after **12项全true**：原dev/test各23业务表与认证、dev六票/test原110000修订1数量1唯一付款/任务/有效票及关联、前23笔/身份/旧侧车、旧候选/active/native、19–23共2948份历史原件保持；只允许记录过的非秘密审核日期变化。原active仍kb-0446f653bf128f969499 / assistant-4，旧Q16/23失败与成功证据均未改写，原55432从未reset，合成业务写仅55433。

**2026-10-18 12:15:43（Asia/Shanghai）**，预演及正式API正常exit0，两PG stop均exit0，8000/5173/55432/55433无监听，数据保留。项目仍.，原环境/工具复用，无安装、全局PATH/ACL/网络策略/C盘配置变化，无提交/推送/发布。运行物只在`.local/d3-24`及同一usage的Q17两侧车；原usage仅追加该一笔。

最终`python -X utf8 scripts/llm_round24.py audit`实际exit0：209份交付文件、98份Python AST、57份Markdown/490处本地链接、秘密匹配0、12项保护全true；独占保存delivery-audit.json，同时核失败关联/逐笔usage/费用/停止和服务收尾。Git仍main无提交且交付未跟踪，空diff不表示没有新文件或修改。下一入口为[25：截断失败分析与恢复方案](../prompts/25-截断失败分析与恢复方案.md)。Q17失败批次不可续用；后续改变答复长度约束或输出上限均须新精确预览和相应费用/字段批准，不自动追加调用。跨版本、完整公平任务、Q5、8B/训练、LLM自主探索和Q2首演仍未完成。
