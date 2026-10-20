# 官网咨询 Agent（ClawHunt #1504）

面向官网咨询的知识问答 Bot：**已知问题作答并给出处，未知问题与低置信度问题转人工**。
置信度阈值在答案生成之前判定；检索只命中授权的公开知识子集；转人工会带走完整上下文包并落成工单。

- 对应验收标准：**提供已知问题、未知问题和转人工演示**
- 环境：Python 3.13（实测 3.13.9），**仅标准库**（零第三方依赖）；不联网、不调用任何模型 API、不需要数据库

## 快速开始

```bash
cd 1504-official-site-agent

# 第 1 步：一键复跑，产出三分支演示日志、引用结果、工单记录、执行轨迹
python -m official_site_agent demo

# 第 2 步：运行单元测试（纯 assert，无 pytest）
python tests/test_runner.py

# 第 3 步：运行独立验收脚本（逐条断言，原始输出写入 verify/records/）
python verify/run_verification.py
```

单条试问（便于用你自己的问题验证）：

```bash
python -m official_site_agent ask --question "免费试用期有多久？" --session-id S-DEMO
```

## 三条分支

| 分支 | 触发条件 | 行为 |
|---|---|---|
| `answered` | 最高检索分 `>= answer_confidence` | 生成抽取式答案，附来源条目 id 与 url |
| `handoff_low_confidence` | `min_relevance <= 最高分 < answer_confidence` | 不生成，转专家队列（SLA 2h） |
| `handoff_no_hit` | 最高分 `< min_relevance` 或无候选 | 不生成，转一线队列（SLA 4h） |
| `rate_limited` | 同会话超滑动窗口上限 | 不检索不生成，转安全队列（SLA 1h） |

关键顺序固定为：**限流 -> 检索 -> 门控 -> 生成 / 转人工**。门控未放行时生成函数不会被调用；
即使被误调用，入口处的 `assert_allowed` 也会抛 `GenerationBlocked`。

## 目录结构

```
official_site_agent/   代码（model / config / knowledge / retrieval / gate / answer / handoff /
                       ratelimit / trace / agent / artifacts / cli）
config/                三个 JSON：授权知识源白名单、阈值与限流、转人工路由
samples/               kb_pages.jsonl（25 条合成知识，22 条授权）
                       queries.jsonl（26 条查询，覆盖已知/未知/边界/超频）
tests/test_runner.py   49 个单元测试（纯 assert）
verify/                验收脚本与原始输出记录
out/                   demo 产物（引用结果、工单、检索轨迹、执行轨迹、演示日志）
```

## 配置说明（改配置即可换知识源/阈值/路由，无需改代码）

**授权白名单** `config/knowledge_sources.json`：`allowed_hosts` 列出可引用的主机与路径前缀；
`denied_hosts` 明确拒绝内部资料；`require_authorized_flag` 要求条目自身 `authorized=true`。
只有两者同时满足的条目才会进入检索索引，其余会在演示第 1 节列出排除原因。

**阈值与限流** `config/thresholds.json`：

| 字段 | 含义 |
|---|---|
| `retrieval.top_k` | 最多返回候选数 |
| `retrieval.min_term_len` | 英文词最小长度（中文按二元组切分） |
| `gate.min_relevance` | 低于此分视为未命中，转一线人工 |
| `gate.answer_confidence` | 达到此分才允许生成答案 |
| `gate.min_margin` | 最高分与次高分之差下限，0 表示不启用歧义抑制 |
| `rate_limit.window_seconds` | 滑动窗口长度 |
| `rate_limit.max_requests` | 窗口内最大请求数 |
| `rate_limit.abuse_queue` | 超限后进入的队列 |

**转人工路由** `config/handoff_routing.json`：按分支配置 `queue`、`sla_hours`、`priority`；
未配置的分支落到 `default_route`。

## 产物

| 文件 | 内容 |
|---|---|
| `out/demo_run.log` | 三分支演示日志（含「输入 -> 输出」对照） |
| `out/citations.jsonl` | 引用结果：答案正文 + 来源条目 id/标题/url/分数 |
| `out/handoff_tickets.jsonl` | 转人工工单（问题原文、检索候选、置信度、会话 id、路由、SLA） |
| `out/retrieval_trace.jsonl` | 每条查询的检索候选与门控结论 |
| `out/decisions.jsonl` | 一行一个结论：分支、置信度、阈值、引用或工单号 |
| `out/trace.jsonl` | 完整执行轨迹（限流/检索/门控/生成/转人工逐步） |
| `out/expected_output.txt` | 与 `demo_run.log` 一致的期望输出，用于比对复跑 |

## 常见问题

**Q：会不会让模型硬答？**
A：不会。本单不含生成模型；即便接入模型，门控也在生成之前完成，`compose_answer` 对未放行的
`GateDecision` 直接抛错。演示轨迹里转人工分支只出现 `generation_skipped`，没有 `generation`。

**Q：检索会不会命中内部资料？**
A：不会。索引只建立在授权子集上；`Retriever.search` 还会二次断言每个命中条目都在白名单内。
样本里故意放了 3 条内部/未授权条目，演示第 1 节会显示它们被排除的原因。

**Q：转人工丢了上下文怎么办？**
A：每张工单都带问题原文、检索候选（page_id/标题/url/分数）、置信度、会话 id、阈值快照、
路由队列与 SLA，人工接手无需再问访客一遍。

**Q：输出可复现吗？**
A：可。时间来自确定性时钟，检索是 TF-IDF 余弦（无随机数）。连续两次 `demo` 的
`trace.jsonl`、`demo_run.log`、`citations.jsonl`、`handoff_tickets.jsonl` 逐字节一致，验证脚本会断言。

**Q：中文输出乱码？**
A：若用 `conda run`，conda 会用自己的 GBK 编码重新打印子进程输出，含中文时可能报
`UnicodeEncodeError`。**直接用 python 运行**；必须用 conda run 时先设 `PYTHONIOENCODING=utf-8`。

**Q：怎么接真实知识库？**
A：把 `samples/kb_pages.jsonl` 换成你们已授权公开的资料（字段 page_id/title/text/url/tags/authorized），
把 `config/knowledge_sources.json` 换成真实域名白名单即可，代码不用改。

## 边界

不依赖系列其它单的交付物；不接真实账号与权限；只用合成/脱敏数据；不联网、不调用模型 API；
模型费用、云资源与真实知识库运营不在本单范围内。
