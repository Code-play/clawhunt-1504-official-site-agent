# 官网咨询 Agent 交付说明（ClawHunt #1504）

【交付物获取方式】
本单为自包含可复跑交付：全部源码、配置、合成数据、单元测试与验收脚本都在下方目录树内，
评审无需下载附件即可按「运行方式」逐条复跑并比对验证记录。
- 交付物仓库（含可浏览源码树 source/ 与打包文件）：https://github.com/Code-play/clawhunt-1504-official-site-agent
- 打包下载（zip）：https://github.com/Code-play/clawhunt-1504-official-site-agent/raw/main/1504-official-site-agent.zip

【订单与范围】
订单 #1504「企业 Agent 系统 17/50｜官网 Agent」。
本单范围：配置面向官网咨询的知识问答 Bot，未知问题转人工。
交付要求：提交与本单相关的代码或配置、使用说明和验证记录；只能使用获授权或脱敏的资料。
验收标准（原文）：提供已知问题、未知问题和转人工演示。
预算：USD 20，包含一轮修改。

【需求理解】
这个 Bot 的核心不是「能不能答」，而是「不确定时敢不敢不答」。因此本单把工程重心放在
三个地方：检索边界、置信度门控、转人工上下文。具体拆成买家点名的六件事：

1. 置信度阈值必须在生成之前。低于阈值一律转人工，绝不让模型硬答。
2. 检索只能命中授权的公开知识子集，答案必须带来源条目 id 的可追溯引用。
3. 三类分支要有明确行为：命中作答 / 未命中转人工 / 低置信度转人工。
4. 转人工要带走上下文包（问题原文、检索候选、置信度、会话 id），并落成工单记录。
5. 限流与防滥用要可配置。
6. 交付物清单要能被逐项对照验收。

本交付物对以上六条的实现位置分别是：gate.py 与 answer.py 的调用顺序；knowledge.py 的
双闸门白名单与 citations；gate.py 的三个分支常量与 agent.py 的编排；handoff.py 的
HandoffDesk；ratelimit.py 的滑动窗口与 thresholds.json；DELIVERY.md 的编号清单与
verify 脚本的文件齐全断言。

【交付边界】
- 不依赖系列任何其它单，独立可运行；不接真实账号、不接真实官网、不接数据库。
- 不联网、不调用任何大模型 API；生成是抽取式的，答案句子直接来自授权知识原文。
- 全部知识条目为合成/脱敏内容，域名统一使用 example 顶级域，不含任何真实站点与个人信息。
- 真实知识库接入只需替换 samples/kb_pages.jsonl 与 config/knowledge_sources.json，代码不动。
- 模型费用、云资源、真实知识库运营与多轮对话能力不在本单范围。
- 限流为单进程内存实现；多实例部署需替换为共享存储，这属于后续单的范围。

【整体架构】
代码按职责分层，依赖方向自上而下，禁止下层反向依赖上层：

1. 模型层 model.py
   定义 KbPage、Query、RetrievedDoc、RetrievalResult、GateDecision、Answer、Citation、
   HandoffTicket、RateLimitDecision、TurnOutcome。全部是 dataclass，字段可解释、可序列化。

2. 配置层 config.py
   读取并校验三个 JSON：thresholds.json、knowledge_sources.json、handoff_routing.json。
   校验不是摆设：answer_confidence 必须严格大于 min_relevance；白名单主机不得重复且必须
   显式 authorized=true；路由不得重复覆盖同一分支。配置非法时抛 ConfigError，不静默降级。

3. 知识层 knowledge.py
   两道闸门：条目自身 authorized=true，且 url 的 host 命中 allowed_hosts 且不在 denied_hosts，
   协议必须是 https。未通过的条目进入 excluded 列表并携带可读原因，供审计与演示展示。
   KnowledgeBase 只暴露授权子集，get() 对未知 id 直接抛 KnowledgeError。

4. 检索层 retrieval.py
   纯标准库实现。中文按连续汉字串切二元组，英文按正则取词并小写；TF 用 1+log(tf)，
   IDF 用 log((N+1)/(df+1))+1，向量做 L2 归一化，得分即余弦相似度，落在 [0,1]。
   没有随机数、没有网络请求、没有第三方库，因此同一输入必然得到同一分数。

5. 门控层 gate.py
   decide() 是纯函数，只读取检索结果与阈值，不调用任何生成逻辑。三个出口：
   answered、handoff_no_hit、handoff_low_confidence。assert_allowed() 是生成前的硬闸门。

6. 生成层 answer.py
   compose_answer() 入口第一件事就是 assert_allowed()；未放行直接抛 GenerationBlocked。
   放行后按原文顺序拼接与问题有词重叠的句子，并在正文写入来源条目 id 与 url。

7. 转人工层 handoff.py
   HandoffDesk.create() 组装上下文包，并做字段完整性不变量检查；缺问题、缺候选、缺会话 id
   或缺置信度都会抛 HandoffError。工单 id 为 HT-0001 起顺序编号，确定性可复跑。

8. 限流层 ratelimit.py
   滑动窗口：每个会话在 window_seconds 内最多 max_requests 次。窗口边界不会放过双倍流量。
   时钟注入，演示使用确定性秒表；配置非法时抛 RateLimitConfigError。

9. 轨迹层 trace.py
   确定性时钟（固定起点 + 30 秒步长）与结构化事件记录，可导出 JSONL 并渲染人可读表格。

10. 编排层 agent.py
    固定顺序：限流 -> 检索 -> 门控 -> 生成 / 转人工。每一步写一条轨迹。限流拒绝时跳过检索与
    生成，转安全队列；转人工分支只记 generation_skipped，不记 generation。

11. 产物层 artifacts.py 与入口层 cli.py
    产物层把引用结果、工单、检索轨迹、逐条结论、执行轨迹落盘；入口层提供 demo 与 ask。

【目录树】
1504-official-site-agent/
├─ SOLUTION.md                     本文件
├─ README.md                       快速上手（安装/运行/测试三步）
├─ DELIVERY.md                     交付清单 + 验收点映射
├─ .gitignore
├─ official_site_agent/
│  ├─ __init__.py                  包导出与版本
│  ├─ __main__.py                  python -m 入口
│  ├─ model.py                     数据结构
│  ├─ config.py                    配置加载与校验
│  ├─ knowledge.py                 授权白名单与知识加载
│  ├─ retrieval.py                 分词 + TF-IDF + 余弦（纯标准库）
│  ├─ gate.py                      置信度门 + 三分支决策
│  ├─ answer.py                    门控放行后的抽取式答案生成
│  ├─ handoff.py                   转人工上下文包与工单
│  ├─ ratelimit.py                 滑动窗口限流
│  ├─ trace.py                     确定性时钟与执行轨迹
│  ├─ agent.py                     编排：限流 -> 检索 -> 门控 -> 生成/转人工
│  ├─ artifacts.py                 产物落盘
│  └─ cli.py                       demo / ask 命令
├─ config/
│  ├─ knowledge_sources.json       allowed_hosts / denied_hosts / rules
│  ├─ thresholds.json              检索 top_k、门控阈值、限流参数
│  └─ handoff_routing.json         分支 -> 队列 / SLA / 优先级
├─ samples/
│  ├─ kb_pages.jsonl               25 条合成知识（22 条授权，3 条用于证明过滤）
│  └─ queries.jsonl                26 条查询（已知/未知/低置信度/超频）
├─ tests/
│  └─ test_runner.py               49 个单元测试（纯 assert）
├─ verify/
│  ├─ run_verification.py          64 条验收断言
│  ├─ verification_record.md       人可读验证记录
│  ├─ verification_result.json     机器可读结果
│  └─ records/                     原始输出（demo、测试、哈希、验证报告）
└─ out/
   ├─ demo_run.log                 三分支演示日志
   ├─ citations.jsonl              引用结果（答案 + 来源条目 id）
   ├─ handoff_tickets.jsonl        转人工工单记录
   ├─ retrieval_trace.jsonl        检索候选与门控结论
   ├─ decisions.jsonl              一行一个结论
   ├─ trace.jsonl                  完整执行轨迹
   └─ expected_output.txt          期望输出（用于比对复跑）

【运行方式】
环境：Python 3.13（实测 3.13.9），仅标准库，零第三方依赖，不联网。
在项目根目录依次执行：

  python -m official_site_agent demo
  python tests/test_runner.py
  python verify/run_verification.py

第一条产出 out/ 下的全部演示产物；第二条输出 N/N tests passed；第三条输出
N/N assertions passed，并把 demo、单元测试与验证报告的原始输出写入 verify/records/。
单条试问：

  python -m official_site_agent ask --question "免费试用期有多久？" --session-id S-DEMO

【核心机制说明】
一、检索为什么只命中授权子集
索引不是建立在全部 JSONL 上，而是建立在 build_knowledge_base 过滤后的 pages 上。
过滤条件同时检查条目标志与 url 主机：authorized=false 的 KB-090、KB-091 被拒；主机在
denied_hosts 的 KB-092 被拒。演示第 1 节会打印每条被排除条目的原因。此外 Retriever.search
在返回前再次断言每个命中 page_id 都在 KnowledgeBase 内，形成第二道防线。

二、阈值为什么一定在生成之前
编排层先调用 decide() 得到 GateDecision，再根据 allowed 决定是否调用 compose_answer()。
compose_answer() 的第一行是 assert_allowed(decision)，未放行直接抛 GenerationBlocked。
因此「低置信度硬答」不是靠提示词约束，而是代码路径上不存在。轨迹层面也有证据：所有
answered 的 gate 序号都小于 generation；所有转人工只有 generation_skipped，没有 generation。

三、三类分支的判定与行为
设最高检索分为 s，min_relevance 为 r，answer_confidence 为 a，则：
- s < r 或无候选：handoff_no_hit，未知问题，转 human-l1，SLA 4h，不生成。
- r <= s < a：handoff_low_confidence，证据不足，转 human-l2-specialist，SLA 2h，不生成。
- s >= a：answered，生成抽取式答案，附 Citation（page_id/title/url/score）。
可选 min_margin 用于抑制「两个候选分数过于接近」的歧义，默认 0 表示关闭。

四、引用如何做到可追溯
Citation 直接携带 page_id、title、url 与分数，答案正文以「（来源：KB-00X）」结尾。
out/citations.jsonl 一行一条答案，citations 数组与正文互相印证；verify 断言每个 citation
既在授权集合内，也出现在答案正文中。

五、转人工上下文包与工单
工单字段包含：ticket_id、created_at、session_id、query_id、question、branch、reason、
confidence、thresholds 快照、retrieval_candidates（page_id/title/url/score/matched_terms）、
route（queue/sla_hours/priority）、status=open、trace_ref。人工接手时不需要再问访客一遍。

六、限流与防滥用
thresholds.json 的 rate_limit 段配置 enabled、window_seconds、max_requests、abuse_queue、
abuse_sla_hours。演示中同一会话第 6 次起被拒，retry_after 由窗口内最早请求时间计算，
并转 security-abuse 队列。改 max_requests 即可改变行为，verify 用两组参数做了对照断言。

【逐条验收对照表】
| 验收标准/关注点 | 实现与产物 | 复跑核验 |
|---|---|---|
| 已知问题演示 | Q-001..Q-012；answered 分支 | demo：Q-001 -> answered，引用 KB-002 |
| 未知问题演示 | Q-101..Q-104；handoff_no_hit | demo：Q-101 -> 工单 HT-0001，human-l1，SLA 4h |
| 转人工演示 | handoff.py + handoff_tickets.jsonl | demo 第 3 节输入输出对照；工单含四要素上下文 |
| 关注点 1 阈值前置 | gate.decide 先于 answer.compose_answer | 轨迹 gate 序号 < generation；未放行抛 GenerationBlocked |
| 关注点 2 授权与引用 | knowledge 双闸门 + Citation | 索引 22/25；内部条目 0 次命中；引用写入正文 |
| 关注点 3 三分支 | gate.py 三常量 + agent 编排 | decisions.jsonl 每行一个分支，互斥 |
| 关注点 4 上下文包 | HandoffDesk.create | 工单含问题/候选/置信度/会话 id，落 JSONL |
| 关注点 5 限流可配置 | RateLimiter + thresholds.json | demo 触发 2 次；改 max_requests 行为改变 |
| 关注点 6 清单可对照 | DELIVERY.md 27 项 + verify 断言 | 必需文件齐全断言通过 |

【验证结果（真实原始输出）】
命令与原始输出均保存在 verify/records/，以下为节选，未做修饰。

1) 单元测试 `python tests/test_runner.py` 末尾：
   ok   test_agent_is_deterministic
   ok   test_source_uses_only_stdlib_and_local_package

   49/49 tests passed

2) 验收脚本 `python verify/run_verification.py`：
   64/64 assertions passed

3) demo 分支统计（原始行）：
   分支统计: {"answered": 15, "handoff_no_hit": 4, "handoff_low_confidence": 5, "rate_limited": 2}

4) demo 三分支输入输出对照（原始节选）：
   场景: 已知问题
     输入: 免费试用期有多久？
     输出分支: answered
     输出答案: 根据《免费试用说明》：新注册用户可以免费试用企业版 14 天，试用期内包含全部
     功能与 100 万次 API 调用额度。试用到期后如未订阅，账号自动降级为免费版，数据保留 30 天。
     （来源：KB-002）
     来源条目 id: KB-002

   场景: 未知问题
     输入: 你们公司的股票代码是多少？
     输出分支: handoff_no_hit
     输出工单: HT-0001
     转人工队列: human-l1｜SLA 4h
     置信度: 0.0000（阈值 0.32）
     上下文包检索候选: 0 条

   场景: 低置信度问题
     输入: 企业版和团队版的价格差多少？
     输出分支: handoff_low_confidence
     输出工单: HT-0005
     转人工队列: human-l2-specialist｜SLA 2h
     置信度: 0.2667（阈值 0.32）
     上下文包检索候选: 5 条

5) 授权过滤（demo 第 1 节原始行）：
   授权知识子集: 22/25 条进入索引；排除 3 条
   KB-090 内部定价毛利测算 -> 条目 authorized=false（资料未获授权对外）
   KB-091 内部产品路线图 -> 条目 authorized=false（资料未获授权对外）
   KB-092 合作方渠道返点草案 -> host partners.aurora-cloud.example 在 denied_hosts
   （合作方草稿区，未经法务审核）

6) 确定性：连续两次 demo 后，trace.jsonl、demo_run.log、citations.jsonl、
   handoff_tickets.jsonl 四个产物的 SHA-256 完全一致，记录见
   verify/records/determinism_hashes.txt。

【已知限制与使用前提】
1. 生成是抽取式的：从命中条目原文拼接相关句子，不做改写、不做多轮推理。若客户需要自然语言
   改写，可在后续单接入模型，但门控必须仍然保持在生成之前。
2. 中文检索使用字符二元组，没有词典与向量模型。同义改写（如「退款」与「退钱」）可能被判为
   低置信度并转人工。这是有意的保守取舍：宁可多转人工，也不硬答。
3. 阈值 0.12 / 0.32 是针对本样本集标定的，接入真实知识库后需要按业务语料重新标定。
4. min_margin 默认关闭（0.0），需要歧义抑制时可开启，但会提高转人工率。
5. 限流为单进程内存实现，重启即清空；多实例或多进程部署需改为共享存储。
6. tag_routes 标签路由已配置示例，但默认未在编排层启用。
7. 使用前提：知识条目必须已经获得对外授权；未授权的内部资料不要放进 kb_pages.jsonl，
   即便放进来也会被白名单过滤并记录排除原因。
8. 若在 conda 环境中运行，请先设置 PYTHONIOENCODING=utf-8；conda run 会用自己的编码
   重新打印子进程输出，可能产生 UnicodeEncodeError，这不是本工具的问题。

【样本清单摘要】
知识条目 25 条：计费、试用、退款、鉴权、限流、Webhook、SLA、数据驻留、导出、注销、SSO、
浏览器、新手引导、状态页、加密、合规、集成、移动端、发票、支持渠道、私有化部署、登录安全，
以及 3 条故意未授权的内部/合作方资料。查询 26 条：12 条已知、4 条未知、3 条低置信度、
5 条正常请求加 2 条触发限流的超频请求。所有条目与查询均为合成内容，可安全公开。
