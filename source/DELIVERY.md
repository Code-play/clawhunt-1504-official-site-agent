# 交付清单与验收对照（订单 #1504 · 官网 Agent）

订单范围：配置面向官网咨询的知识问答 Bot，未知问题转人工。
验收标准（原文）：`提供已知问题、未知问题和转人工演示。`

## 一、交付清单（逐项可对照）

| 序号 | 路径 | 说明 |
|---|---|---|
| 1 | `SOLUTION.md` | 中文交付说明：需求理解、边界、架构、目录树、运行方式、逐条验收对照、验证结果、已知限制 |
| 2 | `README.md` | 快速上手：安装/运行/测试三步，配置说明与常见问题 |
| 3 | `DELIVERY.md` | 本文件：交付清单 + 验收点映射 |
| 4 | `.gitignore` | 忽略 Python 缓存；out/ 与 verify/records/ 的产物故意入库 |
| 5 | `official_site_agent/model.py` | 数据结构：知识条目、查询、候选、门控决策、答案、工单、限流决策 |
| 6 | `official_site_agent/config.py` | 配置加载与校验（阈值、授权白名单、转人工路由） |
| 7 | `official_site_agent/knowledge.py` | 授权知识源白名单与加载，排除未授权条目并记录原因 |
| 8 | `official_site_agent/retrieval.py` | 纯标准库检索：CJK 二元组 + 英文词 + TF-IDF + 余弦，确定性 |
| 9 | `official_site_agent/gate.py` | 置信度门 + 三分支决策，阈值判断在生成之前 |
| 10 | `official_site_agent/answer.py` | 门控放行后的抽取式答案生成，附来源条目 id |
| 11 | `official_site_agent/handoff.py` | 转人工上下文包与工单落盘 |
| 12 | `official_site_agent/ratelimit.py` | 滑动窗口限流与防滥用，参数可配置 |
| 13 | `official_site_agent/trace.py` | 确定性时钟与执行轨迹记录 |
| 14 | `official_site_agent/agent.py` | 编排层：限流 -> 检索 -> 门控 -> 生成 / 转人工 |
| 15 | `official_site_agent/artifacts.py` | 产物落盘（引用结果、工单、检索轨迹、结论、执行轨迹） |
| 16 | `official_site_agent/cli.py` | 命令行入口：demo / ask |
| 17 | `official_site_agent/__init__.py` | 包导出与版本 |
| 18 | `official_site_agent/__main__.py` | `python -m official_site_agent` 入口 |
| 19 | `config/knowledge_sources.json` | 授权来源白名单（allowed_hosts / denied_hosts / rules） |
| 20 | `config/thresholds.json` | 门控阈值与限流参数 |
| 21 | `config/handoff_routing.json` | 转人工路由（队列、SLA、优先级、兜底） |
| 22 | `samples/kb_pages.jsonl` | 25 条合成/脱敏知识（22 条授权，3 条用于证明过滤） |
| 23 | `samples/queries.jsonl` | 26 条查询（已知/未知/低置信度/超频） |
| 24 | `tests/test_runner.py` | 49 个单元测试（纯 assert，输出 `N/N tests passed`） |
| 25 | `verify/run_verification.py` | 验收脚本，输出 `N/N assertions passed` |
| 26 | `verify/records/` | 原始输出：demo、单元测试、确定性哈希、验证报告 |
| 27 | `out/` | 演示产物：三分支日志、引用结果、工单记录、检索轨迹、执行轨迹 |

## 二、验收标准逐条对照

| 验收要求 | 交付物 | 复跑与核验方式 |
|---|---|---|
| **已知问题演示** | `samples/queries.jsonl` 的 Q-001..Q-012；`agent.py` 的 answered 分支 | `python -m official_site_agent demo`：Q-001「免费试用期有多久？」-> answered，引用 KB-002；`out/citations.jsonl` 逐条含来源 id |
| **未知问题演示** | Q-101..Q-104；`handoff_no_hit` 分支 | demo：Q-101「你们公司的股票代码是多少？」-> handoff_no_hit，工单进入 human-l1（SLA 4h） |
| **转人工演示** | `handoff.py`、`config/handoff_routing.json`、`out/handoff_tickets.jsonl` | demo 第 3 节「输入 -> 输出」对照；未知与低置信度两条路径各给工单；工单含问题原文、候选、置信度、会话 id |
| 交付要求：代码或配置 | `official_site_agent/`、`config/`、`samples/`、`tests/`、`verify/` | 见上表第 5-25 项 |
| 交付要求：使用说明 | `README.md` | 安装/运行/测试三步 + 配置说明 + FAQ |
| 交付要求：验证记录 | `verify/run_verification.py`、`verify/records/`、`verify/verification_record.md` | `python verify/run_verification.py`，原始输出落盘 |
| 只用获授权或脱敏资料 | `samples/kb_pages.jsonl`（全部合成，域名为 example）、`config/knowledge_sources.json` | 内部条目 KB-090/091/092 被排除并给出原因 |

## 三、买家关注点逐条对照

| 关注点 | 实现位置 | 证据 |
|---|---|---|
| 1. 置信度阈值在生成之前 | `gate.decide` -> `answer.compose_answer`；`assert_allowed` | 轨迹里 answered 的 gate seq 早于 generation；转人工分支只有 generation_skipped；未放行调用生成抛 `GenerationBlocked` |
| 2. 只命中授权公开子集，答案带来源条目 id | `knowledge.build_knowledge_base`、`retrieval.Retriever.search`、`answer.compose_answer` | 索引仅含 22/25 条；`out/citations.jsonl` 每条都有 page_id 且写入正文；内部条目 0 次命中 |
| 3. 三类分支行为明确 | `gate.py` 三常量 + `agent.py` 分支 | `out/decisions.jsonl` 每行一个分支；answered 无工单、两条 handoff 无引用 |
| 4. 转人工带上下文包并落成工单 | `handoff.HandoffDesk.create` | 工单含 question / retrieval_candidates / confidence / session_id / route / status，写入 `out/handoff_tickets.jsonl` |
| 5. 限流与防滥用可配置 | `ratelimit.RateLimiter` + `config/thresholds.json` | demo 第 4 节触发 2 次限流并转 security-abuse；改 `max_requests` 行为随之改变 |
| 6. 交付物清单可逐项对照 | 本文件第一节、`SOLUTION.md` 目录树 | 27 项一一列出，验收脚本断言必需文件齐全 |

## 四、验证结果

| 项目 | 结果 |
|---|---|
| 单元测试 | **49 / 49 通过**（`python tests/test_runner.py`） |
| 验收断言 | **64 / 64 通过**（`python verify/run_verification.py`） |
| 一键复跑确定性 | 连续两次 demo 的 trace/日志/引用/工单四个产物逐字节一致 |
| 分支覆盖 | answered / handoff_no_hit / handoff_low_confidence / rate_limited 均被真实触发 |
| 未授权泄露 | 内部条目 KB-090/091/092 在检索与引用中 0 次出现 |

原始输出见 `verify/records/demo_run_raw.txt`、`verify/records/test_runner_raw.txt`、
`verify/records/determinism_hashes.txt`、`verify/records/verify_output.txt`。

## 五、已知限制

- 生成是抽取式（从命中条目原文拼接相关句子），不做改写与多轮推理；本单不调用大模型。
- 中文检索用字符二元组，未引入词典或向量模型；同义改写（如「退款」与「退钱」）可能判为低置信度并转人工。
- 限流为单进程内存实现，多实例部署需换成共享存储（如 Redis）。
- 标签路由 `tag_routes` 已配置但默认未启用，需在编排层按业务开启。
