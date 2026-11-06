# 第25轮：截断诊断、最小失败收据与恢复选择

> 公开脱敏说明：本机/服务器位置已改为仓库相对路径或私有配置占位项（`PRIVATE_*_DIR`）；本机私有记录未随仓库发布。原始材料在本机保留，历史命令不构成当前运行或费用许可。

日期：2026-10-18。任务D3-01 / P25-01，执行[25](../prompts/25-截断失败分析与恢复方案.md)，接续[24报告](LLM第十批实验.md)。本轮只做本地诊断、必要实现及新精确预览；**新增模型0次、费用0，原连续账本24次 / 0.08382128 CNY / 未决0。Q16/Q17剩余均永久关闭，没有创建新许可。**

## 失败所能证明的范围

第24次的已校验结算账本记录输入2671、输出512、命中2048/未命中623，保守高峰费用0.00542392 CNY。正式chat HTTP503 / output_truncated，第二题未发送。当前解析器把finish_reason=length映射为该错误；旧协议原件只有reason/choice_count/tool_count/tool_shapes，没有保存原始finish_reason或usage对象。

原截断正文和原响应usage对象均未保存。本轮不补写24原件，不能据此判断回答内容、顺序正确性或声称守卫回放成功。独立代理在内存重算账本、前22/23摘要、唯一intent与请求关联、旧侧车及2948份19–23历史文件摘要，结果一致；这不替代新的真实回答交付或当前SQL观察。

## 本地改进

[模型适配器](../services/guide/model.py)仅在显式protocol_report_dir保存独占failure-receipt：内部request_id、白名单finish_reason和失败类别；usage_verified只在usage校验及账本settle成功后为真，保存标准化token计数及费用，未知则null，不伪补0。写入flush/fsync，已有文件不覆盖，收据I/O失败不覆盖原异常；Q17先关闭再记录。严格与实验调用均可使用显式目录，默认入口不自动向15/24历史目录补新收据。未改512、预算、许可或解析拒绝，未回补24。新正式入口必须显式绑定新目录。独立复核另发现既有协议形状会保存任意供应商字段名、协议I/O可能掩盖原解析错误；已局部改为固定键名/未知数量，协议I/O失败保留原错误及已结算停止，四个顶层协议字段保持。

[新零模型预览入口](../scripts/round25_consult.py)复用23的真实HTTP/55433独立SQL准备，仅进程内绑定25/consult-ready及两份紧凑问题。原SYSTEM_V6、公开知识、完整允许状态、navigate schema、history0、max_tokens512均保持，原产品默认提示词未改。没有重跑探索或23默认原生闭环，离线替身不作LLM成绩。

两份新输入为10984/11329字节，正式本地HTTP均200（离线替身）、每次咨询前后23业务表相同；独立SQL确认样本1无锁且券available，样本2唯一trip_2 held锁占券，两对象均无付款/任务/票。据完整结构及逐叶比较，只改变goal/question与两处采集时间；business_time/报价期限/其它事实及全部payload其它字段保持。双侧NoModelTraffic发送0/预留0。独立代理另在内存重算hash及SQL判据，结果一致。

具体精确问题、语义hash、完整字段边界及512/1024比较见[Q18审查](25紧凑答复恢复外发审查.md)。建议紧凑512并新有序scope各1次、合计最多2次、会计预留合计0.064 CNY；目前未批准、未创建许可，不继承旧Q17。若选择1024须另作对应精确预览/契约审查；本轮未启用或放行。

## 集中验证与限制

- 修改前定向基线15 passed / 129 deselected / 5.00秒；临时/MockTransport，真实模型和PG0。
- 最终受影响组：`python -X utf8 -m pytest -q tests/guide/test_failure_receipt25.py tests/guide/test_experiment.py tests/guide/test_q17_export.py tests/guide/test_model_budget.py -k "failure_receipt25 or experiment or q17_export or adapter_fixed_payload or uncertain_failures or candidate_failure or cancellation_and_global_single_model_slot" --basetemp=.local/d3-25/pytest-receipt-final --maxfail=2`，**70 passed / 70 deselected / 20.74秒，exit0**。包含20项新反例，覆盖截断/解析、未知或不可信usage不记已验证、结算后停止、其他入口零占用、防覆盖和收据I/O失败；与基线有重叠不累加，记录receipt-tests.json。
- 上述独立复核后的必要局部修复另运行 `python -X utf8 -m pytest -q tests/guide/test_failure_receipt25.py tests/guide/test_q17_export.py -k "failure_receipt25 or q17_failed_attempt_permanently_closes_remaining_scope" --basetemp=.local/d3-25/pytest-receipt-protocol-final --maxfail=2`，**27 passed /13 deselected /15.88秒，exit0**（23项收据反例+4项旧Q17协议回归）。新增秘密作为未知键名、协议mkdir/write失败三个反例，原70结果保留，新结果receipt-protocol-checks.json；分组重叠不相加，未重跑70全组。
- [25专用审查](../scripts/llm_round25.py)review实际exit0，保存recovery-review.json；独立代理保存independent-history-review.json及independent-preview-review.json。两预览的SQL只用保存结果独立重算，不让离线答复自证成功。
- 无相关变更的23完整隔离/default闭环及旧37/179/41/30/21组复用，没有重跑探索或全量PG测试，没有新增依赖。
- 最初短执行会话结束后PG不再监听，新预览server/prepare在绑定/采样前失败；25/consult仅保存零发送/零预留guard。失败原件及preview-first-failure.json保留。随后用保持运行的PG监督会话和全新consult-ready恢复，未覆盖原目录。不是模型重试或付费恢复。
- 本轮读到官方更新日志仍Flash-1017；价格页直读timeout，官方搜索快照为3周前，比较使用原24高峰价，不能记作即时价格核验。model-review.json保存限制，未改配置，实际发送前再核对。

## 保全、收尾与下一入口

before/after **11项保护全true**：原dev/test各23业务表和认证、dev六票/test原有效票及关联、全部24笔及账本身份、原usage所有侧车/旧Q16和Q17停止、旧候选/active/native、19–24共4816份历史原件逐字节保持。仅实际PGdata和追加postgres.log不列入历史产物hash，配置/结果均保全；原55432未reset，合成状态变化仅55433。原guide-model/experiment与预算/单价/普通22保持，无新许可。

**2026-10-18 17:06:07（Asia/Shanghai）**，本轮API及PG监督会话正常exit0，两PG stop exit0；8000/5173/55432/55433无监听，runtime-final.json。数据保留，产物位于./.local/d3-25；原.venv/工具复用，无安装、全局环境/C盘配置/ACL/网络策略变化、提交、推送或发布。

最终 `python -X utf8 scripts/llm_round25.py audit`完成，独占保存25/delivery-audit.json：215份交付文件、101份Python AST、60份Markdown/518处本地链接，秘密匹配0，11项保护全true；同时重核全部旧原件、24笔/侧车、旧停止及服务已停。独立最终代码复核independent-code-review.json无未解决发现，当前源码/测试hash与27项定向记录一致。Git仍main无提交且交付文件未跟踪，空diff不代表没有修改。本次只补入审计实际数值，未改程序、链接或测试范围。下一入口[26](../prompts/26-紧凑答复恢复与独立交付.md)先读Q18实际答复，未明确批准即维持模型0；不反复生成同义准备轮、不重开Q16/Q17。跨版本、完整公平任务、Q5、8B/训练、LLM自主探索和Q2首演仍未完成。里程碑复核未发现本轮范围扩张；既有逐轮脚本有重复的审计/预览绑定，后续可按实际复用压力局部收敛，不在本轮大规模重构。
