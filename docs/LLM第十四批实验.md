# 第28轮：当前Assistant公平输入与任务记录

> 公开脱敏说明：本机/服务器位置已改为仓库相对路径或私有配置占位项（`PRIVATE_*_DIR`）；本机私有记录未随仓库发布。原始材料在本机保留，历史命令不构成当前运行或费用许可。

日期：2026-10-19。任务D3-01 / P28-01，执行[28](../prompts/28-当前助手公平输入与完整任务记录.md)，接续[27报告](LLM第十三批实验.md)和[公平任务契约](27公平任务与证据契约.md)。本轮模型0、许可0、新费用0；原连续账本26次 / 0.09024104 CNY / 未决0，Q16/Q17/Q18均关闭。

## 交付与比较范围

[公平包装](../services/guide/fair_runtime.py)安装在`create_app`注册的同一个当前`Assistant.chat/infer`上，使用assistant-6双委托；没有重命名旧Coordinator或修改原Assistant/业务/预算/模型适配器。可信宿主固定case、组别、主体、有序委托、goal、发布版本和信息条件；Chat未增加客户端可选组别。跨主体、对象顺序替换、会话迁移、目标更换、未登记反馈均拒绝，原授权重核、30秒观察和答复事实守卫保持。

B0仅移除`current_state/current_observation`，B1取得原允许投影，guide与B1逐字等价，仅做等价检查不重复跑标签。各组共用SYSTEM_V6、公开知识、问题、当前分区、allowed_pages、navigate工具、512输出和统一历史规则：只保留本任务已登记的user_reported反馈。状态和页面观察一起变化，属于这两类信息的联合增强，不声称单独状态或页面的因果收益；published-flow与partial-observation不混合。

反馈由宿主在原动作实际回执后登记，仅描述尝试及可见结果，不提供worker/SQL/正确下一步。重复登记幂等、每段反馈只消费一次；单任务最多9轮（unknown为1轮）及300秒总截止在运行时执行，失败后锁闭。记录原来源scope与实际变换后的input hash。B0删状态可能使旧共享外发门失去契约分类，所以本包装明确每轮禁止真实模型；不能仅翻external_model开关把它升级为真实入口。

[任务记录模块](../scripts/round28_records.py)冻结宿主清单、逐轮来源、原业务动作、事件和独立终态，并按group/task_ref归并。prepare/前置停止/离线脚本不进入AI分母，未知启动与usage不填0，可完成/unknown/infeasible分开。只有真实建议、忠实动作、实际事件和晚于动作/事件的独立终态齐备且无工程补步，才可能列模型忠实完成；纯模型自述不作为独立方法。

## 小批本地任务与实际证据

[运输入口](../scripts/round28_run.py)仅复用已核55433合成test，固定DEV27-05共享初态、DEV27-08资料未知，组序05 B0→B1、08 B1→B0，各组重新建立独立同条件初态。宿主清单保存实际主体/对象、23表初态、业务时钟、发布/知识/Prompt/工具/规则修订、最终源码hash、事件和限制；这些内容、SQL、标准终态与工程脚本不进入Assistant上下文。未知任务也委托同一类本人两对象，咨询不产生额外业务写入。

初态准备通过原业务ASGI接口；正式Assistant咨询和业务动作通过真实TCP `127.0.0.1:8000`，独立SQL只连55433。双侧NoModelTraffic禁止真实模型发送和费用预留。两种通路分别报告，不把TestClient准备或本地替身冒充真实模型请求。

| 实例 | 实际结果 | 归因与分母 |
| --- | --- | --- |
| DEV27-05 / B0、B1 | 每组9次脚本chat运输、9个accepted原动作；各2张当前修订/数量1有效票、2笔唯一成功付款、关联job/provider/task/consumed锁、捕获140000最小单位、券仅消费1次归trip_1、库存守恒 | 预先固定的离线工程STEPS，未依据SQL选择路线；全部操作列工程补步，不是模型规划或新最优性证明 |
| DEV27-08 / B1、B0 | 每组1次脚本chat，初末SQL及23表不变，无业务动作或事件 | 只证明资料未知回答的运输和零写；不能给真实模型未知处理判分 |

每个共享任务在accepted查询序号4/5/7/8分别推进原payment/ticket外部事件；拒绝和重复请求不占序号。处理中、已付未出票和最终出票分别留独立SQL，不提前把付款成功算完整任务。每组两次使用新幂等键重复付款实际409/payment_exists，紧邻23表before/after及原回执、HTTP关联独占保存，终态仍各2笔付款。完整票据凭证只检查存在布尔，不落盘。

最终20次脚本chat按4个task_ref归并，AI启动任务0、AI分母0、模型忠实完成0，完成率null。两条工程两票闭环与两条unknown无写分别记录，不能合并成“AI成功4/4”。46条TCP审计事件排除认证/会话请求及ASGI准备，不是全部接口调用数。真实候选栏为空，脚本候选/正式交付/原动作/终态与工程补步分列。

首批`28/tasks`先完成四任务；独立审查后补轮数/截止/反馈重放门和重复付款原件，最终源码在全新`28/tasks-final`重新验证，首批原件保留。没有失败重发模型或覆盖旧产物。当前两个共享初态/真实26答复及28脚本仍是开发材料，不作为未见留出任务或组间效果成绩。

## 验证与保全

现有基线：`python -X utf8 -m pytest tests/guide/test_round27_replay.py tests/guide/test_evaluation_entry.py -q --basetemp=.local/d3-28/pytest-baseline --junitxml=.local/d3-28/baseline.xml`，89 passed /10.86秒/exit0，仅Mock/临时文件。

最终公平包装及原受影响组：`python -X utf8 -m pytest tests/guide/test_round28_fair.py tests/guide/test_shared_prompt21.py tests/guide/test_assistant.py -q --basetemp=.local/d3-28/pytest-fair-final2 --junitxml=.local/d3-28/fair-checks-final.xml`，221 passed /15.25秒/exit0（36新），PG0/真实模型0；与此前检查重叠不累加。任务记录专项36项通过，详28的checks证据；验证未知分母/usage、多轮去重、重复/拒绝动作不触发、人工补步/过时终态/遗漏事件不能算忠实完成。

实际命令为`python -X utf8 scripts/round28_run.py run`及最终`run --output-directory ./.local/d3-28/tasks-final`，均由控制器为每实例创建独立专用API并正常关闭，不开放重置/worker/组别切换HTTP接口。PG由持续监督会话保留并正常停止。原55432只读before/after，不reset。

[保全与交付审计](../scripts/llm_round28.py)使用独占28目录，before/after复核原dev/test各23业务表/认证、dev六票与test原有效票及关联、26笔账本/身份/全部侧车、旧候选/active/native/配置及19–27共7935旧原件。仅排除实际PGdata、追加PG日志及派生__pycache__。业务/预算/原Assistant未改，不重跑原55432全量、隔离probe或与本轮无关大组。

最终审计首次错误地把每次采集的captured_at也放进全对象相等断言，实际差异仅该时间字段；第二次又将文件名排序与08实际B1→B0组序直接比较，产生列表顺序误判。失败输出保留28/audit.txt及audit-final.txt。修正为仅排除采集时间、按group/task_ref排序后比较完整任务内容，不豁免文件/账本/业务字段；最终审计输出和源码hash另存，不覆盖失败记录。

最终运行、独立复核和静态审计结果见STATE顶部与28的`runtime-final.json/independent-task-review.json/delivery-audit.json`；原件目录已占用，不原样重复运行before/after/audit，也不删除旧停止标记腾路径。全部产物在`./.local/d3-28`，复用原.venv/PG，无安装、全局环境/C盘配置/ACL/网络策略变化，无提交/推送/发布。

最终审计实际exit0：236交付文件、115 Python AST、67 Markdown/617本地链接、秘密匹配0、11保护全true、Q16/Q17/Q18预检均停止；全部127独立复核原件和5源码hash匹配。2026-10-19 12:10:28（Asia/Shanghai）4个API及两PG/监督会话正常退出、8000/5173/55432/55433无监听，数据保留。最终审计脚本修正的hash及命令另存28/audit-final-checks.json；仅文档补记之后做链接/围栏定向复核，不重跑业务或旧模型检查。

## 未运行的真实任务与下一阶段

当前FairRuntime是本地运输包装，控制器按固定STEPS执行，没有把真实模型建议转成需确认的实际动作，也没有动态外发/新预算批次门。故尚不满足28中“运行器已具备有限真实任务条件”的前提；本轮没有提出可直接实发的Q19批准问题，没有创建许可，也不把下一Prompt当新批准。任务记录的真实模式是记录契约，不等于已有真实调用路径。

下一[29](../prompts/29-动态任务许可与真实持续指引.md)整批补有限真实控制器、原来源与实际输入双绑定的动态scope/预算、初始精确预览及后续字段/状态规则、集中批准问题，再依实际答复执行和独立归并；不机械重发26两题。Q5仍待真实基线后用户决定，跨版本、LLM自主探索、8B/训练、完整公平任务及Q2四类首演仍未完成。27工程补步及26原答的长度/用词/覆盖限制全部保持。
