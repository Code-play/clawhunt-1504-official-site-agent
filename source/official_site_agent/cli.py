"""命令行入口。

一键复跑（验收要求的三分支演示都在这里产出）：

    python -m official_site_agent demo

单条问询（便于用你自己的问题试）：

    python -m official_site_agent ask --question "免费试用期有多久？" --session-id S-DEMO

只依赖 Python 标准库，不需要装任何第三方包。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import BRANCH_RATE_LIMITED, OfficialSiteAgent
from .config import load_config_dir
from .gate import BRANCH_ANSWERED, BRANCH_LOW_CONFIDENCE, BRANCH_NO_HIT
from .knowledge import load_knowledge
from .model import Query
from .trace import TraceRecorder

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config"
DEFAULT_KB = ROOT / "samples" / "kb_pages.jsonl"
DEFAULT_QUERIES = ROOT / "samples" / "queries.jsonl"
DEFAULT_OUT = ROOT / "out"


class Printer:
    """同时打印与落盘，保证 demo_run.log 与屏幕输出逐字一致。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)
        print(text)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def load_queries(path: Path) -> list[Query]:
    queries: list[Query] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            queries.append(Query.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError) as exc:
            raise SystemExit(f"{path}:{lineno} 查询不合法: {exc}") from exc
    if not queries:
        raise SystemExit(f"{path} 里没有任何查询")
    return queries


def _section(out: Printer, title: str) -> None:
    out("")
    out("=" * 78)
    out(title)
    out("=" * 78)


def _branch_label(branch: str) -> str:
    return {
        BRANCH_ANSWERED: "命中作答（answered）",
        BRANCH_NO_HIT: "未命中转人工（handoff_no_hit）",
        BRANCH_LOW_CONFIDENCE: "低置信度转人工（handoff_low_confidence）",
        BRANCH_RATE_LIMITED: "限流拒绝（rate_limited）",
    }.get(branch, branch)


def cmd_demo(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir)
    out_dir = Path(args.out_dir)
    out = Printer()

    thresholds, sources, routing = load_config_dir(config_dir)
    kb = load_knowledge(Path(args.kb), sources)
    queries = load_queries(Path(args.queries))

    recorder = TraceRecorder()
    agent = OfficialSiteAgent(kb, thresholds, routing, recorder=recorder)

    out("# 官网咨询 Agent · 一键复跑演示")
    out(f"配置目录: {config_dir}")
    out(f"知识条目: {Path(args.kb)}（合成/脱敏，共 {kb.stats['total']} 条）")
    out(f"查询输入: {Path(args.queries)}（{len(queries)} 条）")
    out(
        f"授权知识子集: {kb.stats['authorized']}/{kb.stats['total']} 条进入索引；"
        f"排除 {kb.stats['excluded']} 条"
    )
    out(
        f"门控阈值: min_relevance={thresholds.min_relevance}，"
        f"answer_confidence={thresholds.answer_confidence}，min_margin={thresholds.min_margin}"
    )
    out(
        f"限流: enabled={thresholds.rate_limit.get('enabled')}，"
        f"window={thresholds.rate_limit.get('window_seconds')}s，"
        f"max_requests={thresholds.rate_limit.get('max_requests')}"
    )

    # ---------------- 1) 授权知识子集 ----------------
    _section(out, "1. 授权知识子集（检索只允许命中这里）")
    out("")
    out(f"{'page_id':<10} {'title':<28} url")
    out("-" * 78)
    for page in kb.pages:
        out(f"{page.page_id:<10} {page.title:<28} {page.url}")
    out("")
    out(f"被排除的条目（{len(kb.excluded)} 条，附原因）：")
    for ex in kb.excluded:
        out(f"  {ex.page_id} {ex.title} -> {ex.reason}")

    # ---------------- 2) 逐条问询 ----------------
    _section(out, "2. 逐条问询（检索 -> 门控 -> 生成/转人工）")
    outcomes = []
    for query in queries:
        outcome = agent.ask(query)
        outcomes.append(outcome)
        out("")
        out(f"[{query.qid}] {query.text}")
        out(f"  会话: {query.session_id}｜期望分支: {_branch_label(query.expect) if query.expect else '-'}")
        out(f"  实际分支: {_branch_label(outcome.gate.branch)}")
        out(f"  检索候选: {len(outcome.retrieval.docs)} 条（授权子集内）")
        for i, doc in enumerate(outcome.retrieval.docs[:3], 1):
            terms = ",".join(doc.matched_terms[:6])
            out(f"    {i}. {doc.page_id} score={doc.score:.4f} 命中[{terms}] {doc.title}")
        if not outcome.retrieval.docs:
            out("    （无候选）")
        out(
            f"  门控: confidence={outcome.gate.confidence:.4f} "
            f"answer_threshold={outcome.gate.answer_threshold} "
            f"allowed={outcome.gate.allowed}"
        )
        out(f"  门控理由: {outcome.gate.reason}")
        if outcome.answer is not None:
            out(f"  输出答案: {outcome.answer.text}")
            out(f"  引用条目: {', '.join(c.page_id for c in outcome.answer.citations)}")
        elif outcome.ticket is not None:
            route = outcome.ticket.route
            out(f"  输出工单: {outcome.ticket.ticket_id} -> 队列 {route['queue']}（SLA {route['sla_hours']}h）")
            out(f"  上下文包: 问题/候选{len(outcome.ticket.retrieval_candidates)}条/置信度/会话 {outcome.ticket.session_id}")

    # ---------------- 3) 三分支输入输出对照 ----------------
    _section(out, "3. 三分支「输入 -> 输出」对照（验收标准）")
    scenarios = [
        ("已知问题", BRANCH_ANSWERED),
        ("未知问题", BRANCH_NO_HIT),
        ("低置信度问题", BRANCH_LOW_CONFIDENCE),
    ]
    for label, branch in scenarios:
        picked = next((o for o in outcomes if o.gate.branch == branch), None)
        out("")
        out(f"场景: {label}")
        if picked is None:
            out("  未在样本中覆盖该分支")
            continue
        out(f"  输入: {picked.query.text}")
        out(f"  输出分支: {picked.gate.branch}")
        if picked.answer is not None:
            out(f"  输出答案: {picked.answer.text}")
            out(f"  来源条目 id: {', '.join(c.page_id for c in picked.answer.citations)}")
        else:
            out(f"  输出工单: {picked.ticket.ticket_id}")
            out(f"  转人工队列: {picked.ticket.route['queue']}｜SLA {picked.ticket.route['sla_hours']}h")
            out(f"  置信度: {picked.gate.confidence:.4f}（阈值 {picked.gate.answer_threshold}）")
            out(f"  上下文包检索候选: {len(picked.ticket.retrieval_candidates)} 条")

    # ---------------- 4) 限流与防滥用 ----------------
    _section(out, "4. 限流与防滥用（可配置滑动窗口）")
    limited = [o for o in outcomes if o.gate.branch == BRANCH_RATE_LIMITED]
    out("")
    out(f"限流配置: {json.dumps(thresholds.rate_limit, ensure_ascii=False)}")
    out(f"本轮触发限流: {len(limited)} 次")
    for o in limited:
        rate = o.rate
        out(
            f"  [{o.query.qid}] key={rate.key} used={rate.used}/{rate.limit} "
            f"retry_after={rate.retry_after_seconds}s -> 工单 {o.ticket.ticket_id}"
        )
    if not limited:
        out("  （本轮未触发；可调低 max_requests 或增加同会话请求以复现）")

    # ---------------- 5) 执行轨迹 ----------------
    _section(out, "5. 执行轨迹（完整一次）")
    out("")
    out(recorder.render_table())
    out("")
    out(f"轨迹条目统计: {json.dumps(recorder.summary(), ensure_ascii=False)}")

    # ---------------- 6) 产物 ----------------
    paths = agent.write_artifacts(out_dir)
    _section(out, "6. 产物")
    out(f"  {paths['citations']}         （引用结果：答案 + 来源条目 id）")
    out(f"  {paths['handoff_tickets']}   （转人工工单记录 {len(agent.desk.tickets)} 条）")
    out(f"  {paths['retrieval_trace']}   （检索候选与门控结论）")
    out(f"  {paths['decisions']}         （逐条结论一行一条）")
    out(f"  {paths['trace']}             （完整执行轨迹 {len(recorder.entries)} 条）")
    out(f"  {out_dir / 'demo_run.log'}   （本文件，用于一键复跑比对）")

    log_text = out.text()
    (out_dir / "demo_run.log").write_text(log_text, encoding="utf-8")
    (out_dir / "expected_output.txt").write_text(log_text, encoding="utf-8")

    # 汇总（不写入日志主体，避免影响复跑比对；仅供人看）
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.gate.branch] = counts.get(o.gate.branch, 0) + 1
    print("")
    print(f"分支统计: {json.dumps(counts, ensure_ascii=False)}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir)
    thresholds, sources, routing = load_config_dir(config_dir)
    kb = load_knowledge(Path(args.kb), sources)
    agent = OfficialSiteAgent(kb, thresholds, routing)
    outcome = agent.ask(Query("AD-HOC", args.session_id, args.question, "ad_hoc"))
    payload = {
        "question": args.question,
        "session_id": args.session_id,
        "branch": outcome.gate.branch,
        "confidence": round(outcome.gate.confidence, 6),
        "answer_threshold": outcome.gate.answer_threshold,
        "allowed": outcome.gate.allowed,
        "reason": outcome.gate.reason,
        "candidates": [
            {"page_id": d.page_id, "title": d.title, "score": d.score} for d in outcome.retrieval.docs
        ],
        "answer": outcome.answer.text if outcome.answer else None,
        "citations": [c.page_id for c in outcome.answer.citations] if outcome.answer else [],
        "ticket": outcome.ticket.to_dict() if outcome.ticket else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 某些宿主不支持
        pass

    parser = argparse.ArgumentParser(prog="official_site_agent", description="官网咨询知识问答 Bot（未知问题转人工）")
    sub = parser.add_subparsers(dest="cmd")

    p_demo = sub.add_parser("demo", help="一键复跑：三分支演示 + 工单 + 引用结果")
    p_demo.add_argument("--config-dir", default=str(DEFAULT_CONFIG))
    p_demo.add_argument("--kb", default=str(DEFAULT_KB))
    p_demo.add_argument("--queries", default=str(DEFAULT_QUERIES))
    p_demo.add_argument("--out-dir", default=str(DEFAULT_OUT))
    p_demo.set_defaults(func=cmd_demo)

    p_ask = sub.add_parser("ask", help="只问一条，打印结构化结果")
    p_ask.add_argument("--config-dir", default=str(DEFAULT_CONFIG))
    p_ask.add_argument("--kb", default=str(DEFAULT_KB))
    p_ask.add_argument("--question", required=True)
    p_ask.add_argument("--session-id", default="S-ADHOC")
    p_ask.set_defaults(func=cmd_ask)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
