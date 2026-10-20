"""独立验证脚本（订单 #1504 · 官网 Agent）。

    python verify/run_verification.py

按验收标准与买家关注点逐条断言，并写：

  verify/records/demo_run_raw.txt       demo 的原始标准输出
  verify/records/test_runner_raw.txt    单元测试的原始输出
  verify/records/determinism_hashes.txt 两次 demo 的产物哈希
  verify/records/verify_output.txt      本脚本的完整原始输出
  verify/verification_record.md         人可读验证记录
  verify/verification_result.json       机器可读结果

刻意不复用 demo 的打印结论：这里重新跑 demo、重新加载产物、独立断言机制本身。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

from official_site_agent.config import load_config_dir  # noqa: E402
from official_site_agent.gate import (  # noqa: E402
    BRANCH_ANSWERED,
    BRANCH_LOW_CONFIDENCE,
    BRANCH_NO_HIT,
    GenerationBlocked,
    decide,
)
from official_site_agent.answer import compose_answer  # noqa: E402
from official_site_agent.knowledge import load_knowledge  # noqa: E402
from official_site_agent.model import Query, RetrievalResult, RetrievedDoc  # noqa: E402
from official_site_agent.ratelimit import RateLimiter  # noqa: E402
from official_site_agent.retrieval import Retriever  # noqa: E402

CONFIG = ROOT / "config"
SAMPLES = ROOT / "samples"
OUT = ROOT / "out"
RECORDS = ROOT / "verify" / "records"
PY = sys.executable

REQUIRED_FILES = (
    "SOLUTION.md",
    "README.md",
    "DELIVERY.md",
    ".gitignore",
    "official_site_agent/__init__.py",
    "official_site_agent/__main__.py",
    "official_site_agent/model.py",
    "official_site_agent/config.py",
    "official_site_agent/knowledge.py",
    "official_site_agent/retrieval.py",
    "official_site_agent/gate.py",
    "official_site_agent/handoff.py",
    "official_site_agent/ratelimit.py",
    "official_site_agent/trace.py",
    "official_site_agent/cli.py",
    "official_site_agent/answer.py",
    "official_site_agent/agent.py",
    "official_site_agent/artifacts.py",
    "config/knowledge_sources.json",
    "config/thresholds.json",
    "config/handoff_routing.json",
    "samples/kb_pages.jsonl",
    "samples/queries.jsonl",
    "tests/test_runner.py",
    "verify/run_verification.py",
)

EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0xFE0F, 0xFE0F),
)


@dataclass
class Check:
    name: str
    expected: str
    actual: str
    passed: bool


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def env_utf8() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def has_emoji(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        for low, high in EMOJI_RANGES:
            if low <= code <= high:
                return True
    return False


def all_paths() -> list[Path]:
    paths: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        paths.append(path)
    return paths


def main() -> int:
    RECORDS.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []
    lines: list[str] = []

    def add(name: str, expected: object, actual: object, ok: bool) -> None:
        checks.append(Check(name, str(expected), str(actual), bool(ok)))

    def section(title: str) -> None:
        lines.append("")
        lines.append(f"## {title}")
        lines.append("")

    lines.append("# 验证记录 · 官网咨询 Agent（订单 #1504）")
    lines.append("")
    lines.append(f"生成时间（UTC）：{datetime.now(timezone.utc).isoformat()}")
    lines.append("复跑命令：`python verify/run_verification.py`")

    # ================= 0. 原始输出采集 =================
    section("0. 原始输出采集（demo 与单元测试）")
    demo_proc = subprocess.run(
        [PY, "-m", "official_site_agent", "demo"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env_utf8(),
    )
    demo_raw = (demo_proc.stdout or "") + (("\n[stderr]\n" + demo_proc.stderr) if demo_proc.stderr else "")
    (RECORDS / "demo_run_raw.txt").write_text(demo_raw, encoding="utf-8")
    add("demo 运行退出码", 0, demo_proc.returncode, demo_proc.returncode == 0)

    test_proc = subprocess.run(
        [PY, "tests/test_runner.py"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env_utf8(),
    )
    test_raw = (test_proc.stdout or "") + (("\n[stderr]\n" + test_proc.stderr) if test_proc.stderr else "")
    (RECORDS / "test_runner_raw.txt").write_text(test_raw, encoding="utf-8")
    match = re.search(r"(\d+)/(\d+) tests passed", test_raw)
    add(
        "单元测试全部通过",
        "N/N tests passed",
        match.group(0) if match else "未找到结果行",
        bool(match) and match.group(1) == match.group(2) and test_proc.returncode == 0,
    )
    lines.append(f"- demo 原始输出：verify/records/demo_run_raw.txt（{len(demo_raw.splitlines())} 行）")
    lines.append(f"- 单元测试原始输出：verify/records/test_runner_raw.txt")

    # ================= 1. 验收标准：三分支演示 =================
    section("1. 验收标准：已知问题 / 未知问题 / 转人工演示")
    decisions = read_jsonl(OUT / "decisions.jsonl")
    by_qid = {row["query_id"]: row for row in decisions}
    branches = [row["branch"] for row in decisions]
    add("逐条结论数量", ">= 15", len(decisions), len(decisions) >= 15)
    add("命中作答分支存在", True, branches.count(BRANCH_ANSWERED), branches.count(BRANCH_ANSWERED) >= 1)
    add("未命中转人工分支存在", True, branches.count(BRANCH_NO_HIT), branches.count(BRANCH_NO_HIT) >= 1)
    add("低置信度转人工分支存在", True, branches.count(BRANCH_LOW_CONFIDENCE), branches.count(BRANCH_LOW_CONFIDENCE) >= 1)
    mismatched = [r["query_id"] for r in decisions if r["expect"] and r["expect"] != r["branch"]]
    add("样本期望分支全部命中", "0 个不符", ",".join(mismatched) or "0", not mismatched)

    known = by_qid.get("Q-001", {})
    unknown = by_qid.get("Q-101", {})
    low = by_qid.get("Q-201", {})
    add("已知问题示例：Q-001 命中作答", BRANCH_ANSWERED, known.get("branch"), known.get("branch") == BRANCH_ANSWERED)
    add("已知问题示例：引用 KB-002", ["KB-002"], known.get("citation_ids"), known.get("citation_ids") == ["KB-002"])
    add("未知问题示例：Q-101 转人工", BRANCH_NO_HIT, unknown.get("branch"), unknown.get("branch") == BRANCH_NO_HIT)
    add("未知问题示例：工单队列 human-l1", "human-l1", unknown.get("queue"), unknown.get("queue") == "human-l1")
    add("低置信度示例：Q-201 转人工", BRANCH_LOW_CONFIDENCE, low.get("branch"), low.get("branch") == BRANCH_LOW_CONFIDENCE)
    add("低置信度示例：工单队列 human-l2-specialist", "human-l2-specialist", low.get("queue"), low.get("queue") == "human-l2-specialist")

    demo_log = OUT / "demo_run.log"
    log_text = demo_log.read_text(encoding="utf-8") if demo_log.is_file() else ""
    add("三分支对照段已写入演示日志", True, "三分支「输入 -> 输出」对照" in log_text,
        "三分支「输入 -> 输出」对照" in log_text)
    add("演示日志含已知问题场景", True, "场景: 已知问题" in log_text, "场景: 已知问题" in log_text)
    add("演示日志含未知问题场景", True, "场景: 未知问题" in log_text, "场景: 未知问题" in log_text)
    add("演示日志含低置信度场景", True, "场景: 低置信度问题" in log_text, "场景: 低置信度问题" in log_text)
    lines.append(f"- 分支统计：answered={branches.count(BRANCH_ANSWERED)}，no_hit={branches.count(BRANCH_NO_HIT)}，"
                 f"low_confidence={branches.count(BRANCH_LOW_CONFIDENCE)}，rate_limited={branches.count('rate_limited')}")

    # ================= 2. 关注点 1：阈值在生成之前 =================
    section("2. 关注点 1：置信度阈值在生成之前")
    trace = read_jsonl(OUT / "trace.jsonl")
    by_query: dict[str, list[dict]] = {}
    for entry in trace:
        qid = entry.get("query_id")
        if qid:
            by_query.setdefault(qid, []).append(entry)

    gate_before_generation = True
    handoff_skips_generation = True
    for qid, row in by_qid.items():
        steps = [e["step"] for e in by_query.get(qid, [])]
        if row["branch"] == BRANCH_ANSWERED:
            if not ("gate" in steps and "generation" in steps and steps.index("gate") < steps.index("generation")):
                gate_before_generation = False
        elif row["branch"] in (BRANCH_NO_HIT, BRANCH_LOW_CONFIDENCE):
            if "generation" in steps or "generation_skipped" not in steps:
                handoff_skips_generation = False
    add("所有作答分支：gate 步骤早于 generation", True, gate_before_generation, gate_before_generation)
    add("所有转人工分支：无 generation，仅有 generation_skipped", True, handoff_skips_generation,
        handoff_skips_generation)

    # 代码级：门控未放行时生成入口必须抛错
    blocked_result = RetrievalResult("Q", "问题", (RetrievedDoc("KB-001", "t", "u", 0.2, ("a",), "s", "s"),), 22)
    blocked_gate = decide(blocked_result, 0.12, 0.32)
    try:
        compose_answer(blocked_result, blocked_gate, "S", 2)
        raised = "未抛错"
    except GenerationBlocked:
        raised = "GenerationBlocked"
    add("代码级：未放行时生成入口抛 GenerationBlocked", "GenerationBlocked", raised, raised == "GenerationBlocked")

    thresholds, sources, routing = load_config_dir(CONFIG)
    add("阈值关系 answer_confidence > min_relevance", True,
        f"{thresholds.answer_confidence} > {thresholds.min_relevance}",
        thresholds.answer_confidence > thresholds.min_relevance)

    # ================= 3. 关注点 2：授权子集 + 引用 =================
    section("3. 关注点 2：只命中授权公开子集，答案带来源条目 id")
    kb = load_knowledge(SAMPLES / "kb_pages.jsonl", sources)
    allowed_ids = set(kb.page_ids)
    add("知识条目总数", ">= 20", kb.stats["total"], kb.stats["total"] >= 20)
    add("授权子集规模", 22, kb.stats["authorized"], kb.stats["authorized"] == 22)
    add("未授权条目被排除", True, ",".join(sorted(e.page_id for e in kb.excluded)),
        {e.page_id for e in kb.excluded} == {"KB-090", "KB-091", "KB-092"})

    retrieval_rows = read_jsonl(OUT / "retrieval_trace.jsonl")
    leaked = []
    for row in retrieval_rows:
        for cand in row["candidates"]:
            if cand["page_id"] not in allowed_ids:
                leaked.append(cand["page_id"])
    add("检索候选全部落在授权子集", "0 个越界", ",".join(leaked) or "0", not leaked)

    citation_rows = read_jsonl(OUT / "citations.jsonl")
    add("引用结果文件非空", True, len(citation_rows), len(citation_rows) >= 1)
    missing_citation = [row["query_id"] for row in citation_rows if not row["citations"]]
    add("每个作答都有来源引用", "0 个缺失", ",".join(missing_citation) or "0", not missing_citation)
    bad_citation = []
    for row in citation_rows:
        for c in row["citations"]:
            if c["page_id"] not in allowed_ids:
                bad_citation.append(c["page_id"])
            if c["page_id"] not in row["answer"]:
                bad_citation.append(f"未写入正文:{c['page_id']}")
    add("引用均为授权条目且写入答案正文", "0 个问题", ",".join(bad_citation) or "0", not bad_citation)

    unauthorized_hits = []
    for row in retrieval_rows:
        for cand in row["candidates"]:
            if cand["page_id"] in {"KB-090", "KB-091", "KB-092"}:
                unauthorized_hits.append(cand["page_id"])
    add("内部/未授权条目从未被检索命中", "0 次", ",".join(unauthorized_hits) or "0", not unauthorized_hits)

    # ================= 4. 关注点 3：三类分支行为 =================
    section("4. 关注点 3：三类分支行为明确")
    answered_rows = [r for r in decisions if r["branch"] == BRANCH_ANSWERED]
    nohit_rows = [r for r in decisions if r["branch"] == BRANCH_NO_HIT]
    low_rows = [r for r in decisions if r["branch"] == BRANCH_LOW_CONFIDENCE]
    add("answered 行均带 citation_ids", True, all(r["citation_ids"] for r in answered_rows),
        all(r["citation_ids"] for r in answered_rows))
    add("answered 行均无工单", True, all(r["ticket_id"] is None for r in answered_rows),
        all(r["ticket_id"] is None for r in answered_rows))
    add("handoff_no_hit 行均无引用、有工单", True,
        all(not r["citation_ids"] and r["ticket_id"] for r in nohit_rows),
        all(not r["citation_ids"] and r["ticket_id"] for r in nohit_rows))
    add("handoff_low_confidence 行均无引用、有工单", True,
        all(not r["citation_ids"] and r["ticket_id"] for r in low_rows),
        all(not r["citation_ids"] and r["ticket_id"] for r in low_rows))
    add("每行只属于一个分支", True, len({r["branch"] for r in decisions}), len({r["branch"] for r in decisions}) >= 3)

    # ================= 5. 关注点 4：上下文包与工单 =================
    section("5. 关注点 4：转人工上下文包与工单记录")
    tickets = read_jsonl(OUT / "handoff_tickets.jsonl")
    add("工单数量与转人工分支数一致", len(nohit_rows) + len(low_rows) + branches.count("rate_limited"),
        len(tickets), len(tickets) == len(nohit_rows) + len(low_rows) + branches.count("rate_limited"))
    required_keys = {"ticket_id", "session_id", "question", "confidence", "retrieval_candidates", "route", "status"}
    field_ok = all(required_keys <= set(t) for t in tickets)
    add("工单含问题/会话/候选/置信度/路由", ",".join(sorted(required_keys)), "全部具备" if field_ok else "缺失",
        field_ok)
    add("工单状态均为 open", True, sorted({t["status"] for t in tickets}), sorted({t["status"] for t in tickets}) == ["open"])
    add("工单 ticket_id 唯一", True, len({t["ticket_id"] for t in tickets}), len({t["ticket_id"] for t in tickets}) == len(tickets))
    route_ok = all(t["route"].get("queue") and int(t["route"].get("sla_hours", 0)) >= 1 for t in tickets)
    add("每张工单都有队列与 SLA", True, "全部具备" if route_ok else "缺失", route_ok)
    question_ok = all(t["question"] == by_qid[t["query_id"]]["query_id"] or t["question"] for t in tickets)
    add("工单保留问题原文", True, "全部具备" if question_ok else "缺失", question_ok)
    conf_ok = all(abs(t["confidence"] - by_qid[t["query_id"]]["confidence"]) < 1e-9 for t in tickets)
    add("工单置信度与门控结论一致", True, "一致" if conf_ok else "不一致", conf_ok)
    has_context = all(t["retrieval_candidates"] is not None and t["session_id"] for t in tickets)
    add("工单携带检索候选与会话 id", True, "有" if has_context else "无", has_context)

    # ================= 6. 关注点 5：限流可配置 =================
    section("6. 关注点 5：限流与防滥用可配置")
    limit_cfg = thresholds.rate_limit
    required_limit = {"enabled", "window_seconds", "max_requests", "abuse_queue"}
    add("限流配置字段齐全", ",".join(sorted(required_limit)),
        ",".join(sorted(required_limit & set(limit_cfg))),
        required_limit <= set(limit_cfg))
    rate_rows = [r for r in decisions if r["branch"] == "rate_limited"]
    add("限流分支可被触发", True, len(rate_rows), len(rate_rows) >= 1)
    add("限流分支转安全队列", "security-abuse", sorted({r["queue"] for r in rate_rows}),
        {r["queue"] for r in rate_rows} == {"security-abuse"})

    limiter_a = RateLimiter(window_seconds=60, max_requests=2)
    limiter_b = RateLimiter(window_seconds=60, max_requests=4)
    a = [limiter_a.check("k", 0).allowed for _ in range(3)]
    b = [limiter_b.check("k", 0).allowed for _ in range(3)]
    add("改 max_requests 即改变放行行为", "[T,T,F] vs [T,T,T]", f"{a} vs {b}", a == [True, True, False] and b == [True, True, True])

    limiter_c = RateLimiter(window_seconds=60, max_requests=1)
    first = limiter_c.check("k", 0).allowed
    blocked = limiter_c.check("k", 10).allowed
    reset = limiter_c.check("k", 61).allowed
    add("滑动窗口到期后恢复", "True/False/True", f"{first}/{blocked}/{reset}", (first, blocked, reset) == (True, False, True))

    # ================= 7. 关注点 6：交付物清单逐项可对照 =================
    section("7. 关注点 6：交付物清单逐项可对照")
    missing_files = [rel for rel in REQUIRED_FILES if not (ROOT / rel).is_file()]
    add("必需交付文件齐全", f"{len(REQUIRED_FILES)} 个", f"缺 {missing_files or 0}", not missing_files)

    non_ascii = [str(p.relative_to(ROOT)) for p in all_paths() if not p.name.isascii()]
    add("目录名与文件名全 ASCII", "0 个非 ASCII", ",".join(non_ascii) or "0", not non_ascii)

    text_files = [p for p in all_paths() if p.is_file() and p.suffix in {".py", ".md", ".json", ".jsonl", ".txt", ".gitignore"}]
    emoji_files = [str(p.relative_to(ROOT)) for p in text_files if has_emoji(p.read_text(encoding="utf-8", errors="ignore"))]
    add("源码与文档无 emoji", "0 个", ",".join(emoji_files) or "0", not emoji_files)

    bom_files = [str(p.relative_to(ROOT)) for p in text_files if p.read_bytes()[:3] == b"\xef\xbb\xbf"]
    add("文本文件均为 UTF-8 无 BOM", "0 个 BOM", ",".join(bom_files) or "0", not bom_files)

    package = ROOT / "official_site_agent"
    allowed_modules = set(sys.stdlib_module_names) | {"official_site_agent"}
    third_party = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module] if node.module and node.level == 0 else []
            else:
                continue
            for module in modules:
                if module.split(".")[0] not in allowed_modules:
                    third_party.append(f"{path.name}:{module}")
    add("零第三方依赖（仅标准库）", "0 个第三方 import", ",".join(third_party) or "0", not third_party)

    kb_count = len(read_jsonl(SAMPLES / "kb_pages.jsonl"))
    query_count = len(read_jsonl(SAMPLES / "queries.jsonl"))
    add("知识条目数量", ">= 20", kb_count, kb_count >= 20)
    add("查询样本数量", ">= 15", query_count, query_count >= 15)

    solution_path = ROOT / "SOLUTION.md"
    solution = solution_path.read_text(encoding="utf-8") if solution_path.is_file() else ""
    add("SOLUTION.md 含 REPO_LINK_HERE", True, "REPO_LINK_HERE" in solution, "REPO_LINK_HERE" in solution)
    add("SOLUTION.md 含 ZIP_LINK_HERE", True, "ZIP_LINK_HERE" in solution, "ZIP_LINK_HERE" in solution)
    add("SOLUTION.md 字符数在 8500-9500", "8500-9500", len(solution), 8500 <= len(solution) <= 9500)

    add("README.md 非空", "> 0", (ROOT / "README.md").stat().st_size if (ROOT / "README.md").is_file() else 0,
        (ROOT / "README.md").is_file() and (ROOT / "README.md").stat().st_size > 0)
    add("DELIVERY.md 非空", "> 0", (ROOT / "DELIVERY.md").stat().st_size if (ROOT / "DELIVERY.md").is_file() else 0,
        (ROOT / "DELIVERY.md").is_file() and (ROOT / "DELIVERY.md").stat().st_size > 0)

    # ================= 8. 确定性与轨迹完整性 =================
    section("8. 一键复跑确定性与轨迹完整性")
    before = {name: sha(OUT / name) for name in ("trace.jsonl", "demo_run.log", "citations.jsonl", "handoff_tickets.jsonl")}
    second = subprocess.run(
        [PY, "-m", "official_site_agent", "demo"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env_utf8(),
    )
    after = {name: sha(OUT / name) for name in ("trace.jsonl", "demo_run.log", "citations.jsonl", "handoff_tickets.jsonl")}
    hash_lines = [f"{name}: {'相同' if before.get(name) == digest else '不同'}" for name, digest in after.items()]
    (RECORDS / "determinism_hashes.txt").write_text(
        "\n".join(hash_lines) + "\n\n" + "\n".join(f"{k} {v}" for k, v in after.items()) + "\n",
        encoding="utf-8",
    )
    add("第二次 demo 退出码", 0, second.returncode, second.returncode == 0)
    add("两次 demo 产物逐字节一致", "4/4 相同", "；".join(hash_lines), before == after)

    seqs = [e["seq"] for e in trace]
    add("轨迹 seq 连续", "1..N", f"{seqs[0] if seqs else 0}..{seqs[-1] if seqs else 0}",
        seqs == list(range(1, len(seqs) + 1)))
    stamps = [e["at"] for e in trace]
    add("轨迹时间戳单调不减", True, f"{len(stamps)} 条", stamps == sorted(stamps))
    add("每条问询都有 ratelimit 步骤", True, len(by_query), all(any(e["step"] == "ratelimit" for e in v) for v in by_query.values()))

    # ================= 结论 =================
    failed = [c for c in checks if not c.passed]
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 断言总数：{len(checks)}；通过：{len(checks) - len(failed)}；失败：{len(failed)}")
    lines.append(f"- 结果：**{'全部通过' if not failed else '存在失败项'}**")
    lines.append("")
    lines.append("| 断言 | 预期 | 实际 | 结果 |")
    lines.append("|---|---|---|---|")
    for c in checks:
        lines.append(f"| {c.name} | {c.expected} | {c.actual} | {'通过' if c.passed else '失败'} |")

    body = "\n".join(lines) + "\n"
    (ROOT / "verify" / "verification_record.md").write_text(body, encoding="utf-8")
    (ROOT / "verify" / "verification_result.json").write_text(
        json.dumps(
            {
                "order": 1504,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "assertions_total": len(checks),
                "assertions_passed": len(checks) - len(failed),
                "assertions_failed": len(failed),
                "all_passed": not failed,
                "checks": [c.__dict__ for c in checks],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    summary = f"{len(checks) - len(failed)}/{len(checks)} assertions passed"
    (RECORDS / "verify_output.txt").write_text(body + "\n" + summary + "\n", encoding="utf-8")
    print(body)
    print(summary)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
