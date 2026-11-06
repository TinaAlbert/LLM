# 第23轮：完整公网隔离与默认原生闭环、双目标恢复预览

> 公开脱敏说明：本机/服务器位置已改为仓库相对路径或私有配置占位项（`PRIVATE_*_DIR`）；本机私有记录未随仓库发布。原始材料在本机保留，历史命令不构成当前运行或费用许可。

日期：2026-10-18。任务D3-01 / P23-01，执行[23](../prompts/23-公网隔离复核与Q16双目标实测.md)。**本机独立探索单元的24项完整探测通过，默认完整门下真实AppContainer→HTTP→独立SQL闭环通过。双目标真实交付恢复已准备两份精确新预览，待Q17明确批准；本轮模型0、费用新增0。** 原Q16首题503及停止许可保持，未重发。

## 新证据与旧原件保全

沿用原`PRIVATE_RUNTIME_DIR/explore-shell.exe`、profile与零capability，没有重建二进制、改变ACL、loopback豁免、网络能力或系统策略。二进制SHA256仍`6bc47ca034618ced5f834b0e20ee263be66d4563451890c7fe8c9330f6c935d7`。旧shell4551和旧D2/22失败原件没有改写，本次成功只属于该独立探索单元。

[manage](../services/guide/manage.py)新增`--exploration-report`，报告先以独占x模式认领，已有文件立即拒绝；旧调用默认路径兼容读取，但后续不得写22原件。[完整门](../services/guide/windows.py)、每动作原生session重核、[runtime](../services/guide/exploration_runtime.py)和[23 runner](../scripts/round23_explore.py)贯穿同一个明确报告路径，无22回退。完整门补齐stage complete、AppContainer已启动及子进程已回收要求。22工程runner只作局部参数化复用，没有再运行22固定入口。

新报告及运行物均在`.local/d3-23`。旧19–22所有非sandbox、非__pycache__文件在before/after共2806份内容hash保持，其中包含21/22全部历史原件及Q16追加原件。

## 实际公网复核与默认完整门闭环

宿主网络当前已有IPv6默认路由。对原固定目的地8.8.8.8:53与2606:4700:4700::1111:53分别发送原DNS查询，UDP4/6均收到有效61字节响应，证据为host-network-controls.json；没有改路由或将不可达当拒绝。

API、原55432和合成55433在线后运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m services.guide.manage probe-exploration --authorized-profile-space --exploration-report .local/d3-23/native-isolation.json
.\.venv\Scripts\python.exe -X utf8 scripts/round23_explore.py run
```

两命令实际exit0。独立native-isolation.json：24项、passed=true、stage complete、limitations空；12个敏感文件拒读5，本机TCP/UDP IPv4/6及LAN、在线PG/API、公网TCP/UDP IPv4/6通过相应门槛，派生拒绝367、子进程回收通过。源码、启动器及实际二进制hash绑定；报告24小时有效，后续运行仍须核时效，不永久放开默认门。

默认run未用engineering或native-engineering旁路。唯一新分支目录`.local/d3-23/exploration/run-ddf233219780430fbfb56214a5562837`，native_process_used=true、executor_isolation_accepted=true、engineering_stub=false、native_engineering=false。观察驱动的确定性工程策略只看允许语义，8动作；候选形成时70次HTTP，复审后最终78次HTTP（不是把70当最终计数），均受28动作/120HTTP/180秒上限约束。

独立只读SQL确认140000最小单位、2张有效票、券消耗1次；审计从保存SQL和轨迹重新计算判据，并核SQL/最终观察hash及source evidence ID。另有只读独立代理在内存重算得到同样结果。规划器没有使用LLM，**不计自主发现、最优性或公平评测成功**；该完整隔离范围也不外推其他程序、部署或所有未知网络协议。

新候选`sandbox-candidate-7ff17052d7902ad07681`来源匹配、独立判据通过，仍publishable=false，未写原知识库。原active保持`kb-0446f653bf128f969499` / assistant-4。

## 双目标恢复本地交付与Q17

[23咨询脚本](../scripts/round23_consult.py)仅有离线server/prepare，独立输出、拒绝覆盖，server与controller双侧NoModelTraffic。历史assistant-6知识文件逐字复制，不重新生成历史模型修订或发布原active。两题经过正式Assistant、真实HTTP/SQL，各HTTP200、咨询前后23业务表不变，输入10894/11227字节、history0、输出512。具体问题、字段、scope和新请求次数集中见[Q17审查](23双目标恢复外发审查.md)。

独立SQL分别确认初始两份有效预报价/券available/无锁价，以及trip_2唯一held锁价/占券；两份均无付款/任务/票。两scope与Q16完全相同，但原停止许可不可复用。旧Q16修复后回放文件没有源码hash，不能声称它证明当前源码相同；本次将原第23次回答对新sample1允许状态作一次定向离线回放，显示金额逆转换往返逐值相等，当前守卫通过，记录当前assistant源码hash。原输入、回答和HTTP503不改，不把回放记成新HTTP200或付费交付。

本轮没有新许可、模型发送、预算预留或收费请求。当前仍23次、0.07839736 CNY、未决0。Q17拟新有序两scope各至多1次、同一账本专用累计至多25，普通22上限/Q16关闭状态保持；没有新明确答复则停在恢复准备完成，不自动重试。

## 集中验证与收尾

最终受影响检查：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/guide/test_exploration_paths23.py tests/guide/test_exploration_runtime22.py --basetemp=.local/d3-23/pytest-final --maxfail=2
```

**37 passed /3.21秒，exit0**。覆盖新路径贯穿、旧报告拒覆盖、生命周期失败关闭及受影响runtime。早先定向20通过与新增marker测试1通过属阶段检查，与最终组有重叠，不相加。另有新咨询脚本AST/导入、两份payload、防覆盖、SQL UUID判据及历史知识逐字保全检查。原132/200/32/82中无相关改变的证据复用，未重跑大组或完整PG回归。

[保护与审计脚本](../scripts/llm_round23.py)before/after **11项全true**：原dev/test各23业务表与认证、dev六票/test原110000修订1数量1唯一付款/任务/有效票及关联保持；前23笔/账本身份和全部侧车、旧候选/active、旧native及2806份旧原件均保持。原55432从未reset，合成写仅55433；两API及controller guard均零模型发送/费用预留。

2026-10-18 **11:51:40（Asia/Shanghai）**，探索API与咨询API正常exit0，两PG stop均exit0；8000/5173/55432/55433无监听、数据保留。项目仍`.`，原.venv/工具/PG复用，没有安装、全局环境变化、C盘配置变化、提交、推送或发布。

`python -X utf8 scripts/llm_round23.py audit`实际exit0：201份交付文件、92份Python AST、55份Markdown/467处本地链接、秘密匹配0、11项保护全true；同时重核完整探测hash、默认run的独立SQL与来源、两精确预览及当前回放和零调用/服务已停。结果在`.local/d3-23/delivery-audit.json`，独占保存不覆盖。Git仍main无提交，交付均未跟踪，空diff不表示没有新文件或修改。下一入口为[24：双目标恢复与独立交付](../prompts/24-双目标恢复与独立交付.md)。跨版本、完整公平任务、Q5、8B/训练及Q2首演保持未完成；实际双目标paid交付/持续跟进尚待新决定。
