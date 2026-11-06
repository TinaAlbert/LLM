# 第22轮：受限原生许可、观察驱动探索及双目标样本

> 公开脱敏说明：本机/服务器位置已改为仓库相对路径或私有配置占位项（`PRIVATE_*_DIR`）；本机私有记录未随仓库发布。原始材料在本机保留，历史命令不构成当前运行或费用许可。

日期：2026-10-17。任务D3-01 / P22-01，执行[22](../prompts/22-探索执行器隔离与双目标实测.md)，接续[21报告](LLM第七批实验.md)。本轮完成独立AppContainer探索许可协议、真实原生反例、显式工程native→HTTP→独立SQL闭环和待审候选。**完整公网隔离未通过，默认在线探索继续关闭。本地阶段模型0；用户随后明确批准Q16，实际新增1次、首题chat503，按约停止，第二题未发送。** 本地交付不是D4整体或Q2首演完成。

## 独立外壳和实际隔离范围

保留旧`shell.exe`、`.local/guide-runtime.json`和旧完整probe原件，新增同一已授权NTFS目录的`PRIVATE_RUNTIME_DIR/explore-shell.exe`。编译复用项目迁移MinGW8.1；未安装依赖、注册新profile、扩大ACL/网络capability或改变系统安全策略。维护记录为`PRIVATE_SETUP_DIR/llm-exploration-20261017.json`。新二进制SHA256为`6bc47ca034618ced5f834b0e20ee263be66d4563451890c7fe8c9330f6c935d7`，旧二进制哈希保持。

修改前原生基线命令`python -X utf8 -m pytest -q tests/guide/test_native_protocol.py tests/guide/test_exploration21.py tests/guide/test_coordinator_lifecycle.py --basetemp=.local/d3-22/pytest-baseline --maxfail=2`在实际旧Child创建处18 passed/2 failed，错误4551；本机消息及CodeIntegrity记录表明应用控制阻止旧文件，原RX权限已存在。没有关闭策略或把旧成功历史当当前通过。独立新文件的宿主同二进制exit11、实际AppContainer ready成功；只陈述观察，不臆测安全策略为何区别对待两个文件。

[shell.c](../services/guide/shell.c)新增编译分支，沿[Windows启动器](../services/guide/windows.py)复用零capability、专用SID、无loopback豁免、创建时Job active_processes=1和关闭句柄回收、仅两管道、干净环境及禁止派生。旧默认执行路径保留。每个动作以单独AppContainer走观察请求→有界匿名JSON→候选请求→白名单动作许可；宿主还要等唯一输出、EOF和exit0，之后才可调用可信HTTP broker。32hex委托、顺序1–28、trip_1/2及五种语义动作严格校验，不接模型URL、路径、SQL、UUID、凭证或管理命令。

新[工程探测](../services/guide/manage.py)为`build-exploration`/`probe-exploration`，新证据只写`.local/d3-22`，不覆盖旧D2报告。首轮缺8000监听的失败保留为native-isolation-initial.json；API就绪后才重做一次，当前24项结果在native-isolation.json：12个源码/凭证/控制答案文件均拒读5；本机TCP/UDP IPv4/6及LAN UDP有宿主成功对照、子进程无送达；PG和API在线对照通过；派生拒绝367、进程实际回收。**公网UDP4/6宿主对照不可达、公网IPv6返回10051，未证明相应拒绝，完整passed=false。** 没有改验收定义把不可达当成功。

默认`begin_exploration`绑定新二进制/源码/launcher、24小时时效和完整检查名单，每次动作重核，不捕捉失败后改走普通进程。真实`round22_explore.py run`已验证在reset及业务动作前拒绝，独立SQL业务摘要相同；该检查只新增合成登录/撤销各1，未称整个数据库零写。

## 观察驱动的工程闭环和候选

[exploration_runtime.py](../services/guide/exploration_runtime.py)只把匿名允许观察交给策略，动作经过native许可后再使用原BoundedExploration的固定127.0.0.1:8000桥。原API仍核对普通模拟Alice；55433实例/运行标记和实际登录会话落点在任何合成reset之前核实。初始化、reset、时钟、worker、UUID和SQL只在工程控制面，没有在线助手代付款入口。

完整probe失败时，产品run保持关闭。本轮显式运行`python -X utf8 scripts/round22_explore.py native-engineering-run`，独立工程门严格复核新报告的新鲜性/哈希、全部文件、本机网络/PG/API/派生/回收通过，同时保留公网缺项。这是真实AppContainer许可加可信HTTP桥，**不是普通stub**；元数据分别为`native_process_used=true`、`executor_isolation.accepted=false`、`engineering_stub=false`，不能拿局部验证解锁产品完整隔离。普通`engineering-run`另有明确名称，本轮未以其冒充native。

实际唯一工程闭环保存在`.local/d3-22/exploration/run-d5dede46f4d64df399ba7f1739a9eda5`：确定性策略只比较当前可见预报价优惠，不带固定金额/正确顺序/SQL；操作后的新状态驱动后续动作，worker仍由工程模拟事件推进。8个语义动作，候选采集时70次HTTP；候选复审之后仍有只读调用，本次原摘要的used是采集时计数，不是整个流程最终计数，不能据此低报实际用量。实际运行始终受120次内部计数限制。未来脚本已分`used_at_candidate`与最终used，旧原件不改、没有为日志字段重跑闭环。

独立SQL核对总扣款140000最小单位、2张有效票、券仅消耗一次，并绑定来源ID、最后允许观察hash及终态SQL摘要。它是合成单分支执行证据，不是模型规划、完整公平对照或最优性证明。原21两种顺序证据复用，不重跑无变更的原券业务测试。

候选`sandbox-candidate-5c44eb6cc6218d422d8c`仅包含有来源的状态变化、版本、模拟身份/假设和未知。候选形成后重读当前匿名状态，与最后轨迹比较；接收会话、披露、来源ID及独立判据再核。审查source integrity matched、独立终态passed，仍`publishable=false`，保留待审；模拟价格/身份轨迹不适合作为原公开页面来源，没有伪造来源调用旧ReleaseStore发布，也未改active或旧候选。候选是宿主从已过滤观察形成，没有调用LLM归纳。

## 验证与失败留存

| 层次 | 实际结果 |
| --- | --- |
| 不依赖旧被阻断exe的本地基线 | 44 passed /0.64秒，模型0/PG0 |
| 新实际原生专项 | 最终63 passed /1.88秒，覆盖5动作、28配额、错序/重放/SQL/URL/UUID/额外参数、UTF8/超长/洪泛/静默/异常退出、额外permit、取消/晚到及Job回收；旧/新C编译分支语法检查通过 |
| runtime/候选离线检查 | 初次46 passed/1 failed为复审多读HTTP使资源计数变化造成假失效；改为语义来源hash与精确ID、补新反例，后续结果保留，不反改首次 |
| 最终集中检查 | `python -X utf8 -m pytest -q tests/guide/test_exploration_native22.py tests/guide/test_exploration_runtime22.py tests/guide/test_exploration21.py tests/guide/test_coordinator_lifecycle.py --basetemp=.local/d3-22/pytest-stage --maxfail=2`：**132 passed /3.45秒**，与前述分组有重叠，不相加；PG0/模型0，原生测试实际创建AppContainer |
| 默认完整门 | 真实TCP绑定后按完整failed报告拒绝，合成业务不变；default-gate-check.json |
| 工程真native+HTTP/SQL | 唯一native-engineering-run exit0，8动作、两票/140000、待审候选；完整隔离仍false、模型0 |

独立审查促成当前快照比较、SQL判据绑定、来源ID篡改反例、清理异常仍dispose并保存失败证据等局部修复。没有用输出自评证明终态；异常/拒绝反例不因普通回答流畅而抵消。

## Q16批准前的具体预览

按[具体外发审查](22双目标样本外发审查.md)，已用正式Assistant、真实TCP HTTP及独立SQL准备两份预览：初始双目标省钱规划10894字节；先锁trip_2后询问券归属/能否调整11227字节。每份咨询前后23业务表摘要不变，历史0、输出上限512、实际输入≤12288；字段过滤未含UUID/凭证/源码/SQL/答案。所有模型运输都是明确离线替身，输入原件单独保留。

原22次已用尽，因此已集中询问最多新增2次及这两份精确字段的外发；**该本地阶段尚无明确批准，故当时未建许可、未改22次上限、模型0。** 后续明确批准与实测结果见下节。 拟固定同一DeepSeek官方目的地和原费用/停止条件，第一失败或usage未知就停、不自动重试；其他上下文不得使用拟新增次数。完整公网探测不足不由这两份纯咨询样本来证明或修复。模型效果仍待决，不报告任务成功率。

## 本地阶段的保护、路径和接续

[本轮保护脚本](../scripts/llm_round22.py)最终9项checks全true：原dev/test各23表、原认证、dev六票及test原110000/修订1/数量1唯一付款/任务/有效票关联保持；169份原保护、19–21共1450份非sandbox历史文件、旧shell/config/probe、全22笔账本身份和旧候选/active保持。实际账本仍22次、0.07094176 CNY、未决0。原55432从未reset，全部合成业务写仅55433。

本地阶段API正常exit0、两PG stop均exit0，于2026-10-17 **18:52:02（Asia/Shanghai）**确认8000/5173/55432/55433无监听，数据保留；runtime-final.json和consult-transport-guard.json记录零模型发送/费用预留。项目仍`.`、原环境复用，产物`.local/d3-22`、新exe在原C盘固定目录，未安装、提交、推送或公网发布。源码、测试、报告、STATE、任务表、I-13工程说明/Q16当时待决和下一入口已交接。本地阶段当时`python -X utf8 scripts/llm_round22.py audit`通过：188交付文件、82份Python AST、52份Markdown/431处本地链接、实际密钥/连接串匹配0、9项保护全true，详delivery-audit.json；Git仍无提交且交付均未跟踪，不以空diff声称没有修改。

下一入口为[23：公网隔离复核与Q16双目标实测](../prompts/23-公网隔离复核与Q16双目标实测.md)。只接剩余公网证据/默认门与明确批准后的真实样本，不重复已过原生协议或21准备。跨业务版本、完整公平任务、Q5及8B/训练仍保留。


## Q16正式批准后的实际请求与失败停止

用户对完整Q16审查明确答复“批准 Q16：最多新增2次并外发上述合成字段”，已登记决定及`.local/d3-22/q16/decision.json`。仅两精确样本按序各最多1次，普通22上限保持；同一原账本的专用许可绑定前22条摘要/ledger_id、有序scope与跨进程锁，先落intent再记pending，任一中断失败关闭。原22笔不改，不另建usage账本，其他上下文拿不到新增次数。

即时核查[官方中文价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)与[英文价格](https://api-docs.deepseek.com/quick_start/pricing/)均实际读取成功：同`deepseek-flash`当日现映射DeepSeek-V4.1-Flash-1017，人民币高峰未命中2/命中0.04/输出8元每百万保持；[非思考参数](https://api-docs.deepseek.com/guides/thinking_mode/)仍显式disabled。发现变化先暂停发送并记录核查，再按最新同一Flash批准范围更新非秘密服务修订；不引入Search/Fetch。新闻单独页直接访问失败保留，不把它记成读取成功。

最初初始化因历史知识严格绑定旧修订报release_incompatible，模型0/未创建grant；原release副本逐字保留。局部修改只允许当前及明确已知前版知识读取，未知修订/工具/产品版本仍拒绝；原知识构建版本不重写，真实报告新增serving_model_revision与knowledge_model_revision，分别为新1017和原V4.1-Flash。32项离线回归通过后才初始化专用许可。

唯一真实第23次通过正式Assistant/原适配器/固定TLS端点；初态输入10894字节、history0、语义hash与原样本1完全相同，输出限额512。实际2671输入/421输出、命中640/未命中2031、3.843秒，保守高峰核算新增0.0074556 CNY；累计23次/0.07839736 CNY、未决0，非发票对账。

原始模型正确区分预报价/券未占用/未付款无票，按可见1000与600基价、200与120减免比较两种条件总额1400/1480，建议优先较大优惠并重核库存、期限和资格，80为条件节省估算。无navigate/tool后续、无模型业务写。**正式chat返回503/assistant_result_unavailable，回答没有交付，不能记咨询成功或完整双目标任务通过。** 新网页入口建议未独立实证，不给完整语义正确分。

保存输入逆转换显示金额后可复现amount_unverified：旧守卫识别条件总额但漏了两份可见减免的条件差额。只做局部输出校验修复，要求两有效预报价/可竞争券、差额精确匹配、紧邻预估省钱措辞；不把差额加入普通价格集合。未知/已实现节省/单对象/报价失效/已占券仍拒绝。保存原回答在新守卫本地回放通过，原paid输入/输出/503不改，没有第二次模型验证或把回放记成HTTP200。

按首失败即停，控制器退出1、第二样本/先锁trip_2状态变化未执行；没有自动重试或凑满次数。专用停止侧车`.local/guide-usage.json.q16-stopped.json`使剩余scope实际预检拒绝q16_batch_stopped，原“最多2次”不能用于继续此失败批次。若后续要真实恢复，先给具体新样本/次数决定，不沿本次许可重发。

新增阶段检查：预算/外发/共享提示200 passed /157.43秒；知识修订与发布32 passed /2.31秒；差额/许可关闭/共享及预算82 passed /27.32秒。分组重叠不相加，全部本地检查模型0。实际HTTP前后合成23表摘要相同；独立原dev/test业务/认证/旧票、前22条与账本身份/旧候选/active、旧native以及19–22共2169份非sandbox/非q16历史原件均保持，Q16 before/after十项全true。相对原22快照只有已授权同服务非秘密revision变化，Q16前后该文件hash也保持。

追加证据只在`.local/d3-22/q16`，原本地零模型guard/快照不覆盖；旧阶段审计不能再用于当前“全22账本保持”的检查。2026-10-17 **23:23:19（Asia/Shanghai）**API exit0、两PG stop exit0，8000/5173/55432/55433无监听，数据保留。维护记录`PRIVATE_SETUP_DIR/llm-q16-20261017.json`，没有安装/全局配置变更/提交/推送/发布。新只读审计`python -X utf8 scripts/llm_q16.py audit`写q16/delivery-audit.json，精确核对1次paid/原503/第二题未发/剩余关闭，不能把计划2次当实际结果。

已修订[23入口](../prompts/23-公网隔离复核与Q16双目标实测.md)只接缺失公网/默认门和真实交付恢复的剩余工作，不重复当前本地修复与测试、不自动重发本批。完整公平任务、跨业务版本、Q5和8B/训练仍保留。

Q16最终静态审计通过：194交付文件、88份Python AST、52份Markdown/434处本地链接，实际密钥/连接串匹配0、十项保护全true；同时核验唯一实发/原503/第二题未发/剩余关闭及服务已停，见q16/delivery-audit.json。
