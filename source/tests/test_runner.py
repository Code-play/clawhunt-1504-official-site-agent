"""单元测试（纯 assert，零第三方依赖）。

运行方式：

    python tests/test_runner.py

输出最后一行固定为 ``N/N tests passed``。失败时会打印用例名、异常与回溯。
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

from official_site_agent.agent import BRANCH_RATE_LIMITED, OfficialSiteAgent
from official_site_agent.answer import compose_answer
from official_site_agent.config import (
    ConfigError,
    load_config_dir,
    load_handoff_routing,
    load_knowledge_sources,
    load_thresholds,
)
from official_site_agent.gate import (
    BRANCH_ANSWERED,
    BRANCH_LOW_CONFIDENCE,
    BRANCH_NO_HIT,
    GenerationBlocked,
    assert_allowed,
    decide,
)
from official_site_agent.handoff import HandoffDesk, route_for
from official_site_agent.knowledge import (
    KnowledgeError,
    build_knowledge_base,
    load_kb_pages,
    load_knowledge,
)
from official_site_agent.model import (
    GateDecision,
    KbPage,
    Query,
    RetrievalResult,
    RetrievedDoc,
)
from official_site_agent.ratelimit import RateLimiter, RateLimitConfigError, SyntheticSecondsClock
from official_site_agent.retrieval import Retriever, tokenize
from official_site_agent.trace import TraceRecorder

CONFIG = ROOT / "config"
KB_PATH = ROOT / "samples" / "kb_pages.jsonl"
QUERIES_PATH = ROOT / "samples" / "queries.jsonl"

TESTS: list[tuple[str, object]] = []


def test(fn):
    TESTS.append((fn.__name__, fn))
    return fn


# ---------------------------------------------------------------- helpers
def fresh():
    thresholds, sources, routing = load_config_dir(CONFIG)
    kb = load_knowledge(KB_PATH, sources)
    return thresholds, sources, routing, kb


def make_agent() -> OfficialSiteAgent:
    thresholds, _sources, routing, kb = fresh()
    return OfficialSiteAgent(kb, thresholds, routing)


def query(text: str, session: str = "S-T", qid: str = "Q-T", expect: str = "") -> Query:
    return Query(qid, session, text, expect)


def doc(page_id: str, score: float, title: str = "t") -> RetrievedDoc:
    return RetrievedDoc(page_id=page_id, title=title, url="https://help.aurora-cloud.example/docs/x", score=score, matched_terms=("a",), snippet="s", context="s")


def result_with(scores: list[float], qid: str = "Q-T") -> RetrievalResult:
    docs = tuple(doc(f"KB-{i:03d}", s) for i, s in enumerate(scores))
    return RetrievalResult(qid, "问题", docs, 22)


def expect_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"预期抛出 {exc_type.__name__}，实际没有抛出")


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 分词与检索
@test
def test_tokenize_ascii_lowercase():
    assert tokenize("API Key Webhook") == ["api", "key", "webhook"]


@test
def test_tokenize_cjk_bigrams():
    toks = tokenize("退款政策")
    assert toks == ["退款", "款政", "政策"], toks


@test
def test_tokenize_single_cjk_char():
    assert tokenize("云") == ["云"]


@test
def test_tokenize_min_term_len():
    toks = tokenize("a api", min_term_len=3)
    assert toks == ["api"], toks


@test
def test_knowledge_authorized_subset():
    _t, _s, _r, kb = fresh()
    assert kb.stats["total"] == 25, kb.stats
    assert kb.stats["authorized"] == 22, kb.stats
    assert kb.stats["excluded"] == 3, kb.stats
    assert len(kb.pages) == 22


@test
def test_knowledge_excludes_unauthorized_flag():
    _t, _s, _r, kb = fresh()
    reasons = {e.page_id: e.reason for e in kb.excluded}
    assert "KB-090" not in kb.page_ids
    assert "KB-091" not in kb.page_ids
    assert "authorized=false" in reasons["KB-090"]
    assert "authorized=false" in reasons["KB-091"]


@test
def test_knowledge_excludes_denied_host():
    _t, _s, _r, kb = fresh()
    reasons = {e.page_id: e.reason for e in kb.excluded}
    assert "KB-092" not in kb.page_ids
    assert "denied_hosts" in reasons["KB-092"]


@test
def test_knowledge_get_unknown_raises():
    _t, _s, _r, kb = fresh()
    expect_raises(KnowledgeError, kb.get, "KB-999")


@test
def test_knowledge_duplicate_ids_raise():
    _t, sources, _r, _kb = fresh()
    page = KbPage("DUP", "t", "x", "https://help.aurora-cloud.example/docs/a")
    expect_raises(KnowledgeError, build_knowledge_base, [page, page], sources)


@test
def test_retrieval_only_authorized_subset():
    _t, _s, _r, kb = fresh()
    retriever = Retriever(kb, min_term_len=2, top_k=5)
    for text in ("你们公司的股票代码是多少？", "内部路线图是什么？", "免费试用期有多久？"):
        res = retriever.search(query(text))
        for d in res.docs:
            assert kb.contains(d.page_id), d.page_id


@test
def test_retrieval_is_deterministic():
    _t, _s, _r, kb = fresh()
    q = query("如何申请退款？")
    a = Retriever(kb, 2, 5).search(q)
    b = Retriever(kb, 2, 5).search(q)
    assert [(d.page_id, d.score) for d in a.docs] == [(d.page_id, d.score) for d in b.docs]


@test
def test_retrieval_scores_in_unit_range():
    _t, _s, _r, kb = fresh()
    retriever = Retriever(kb, 2, 5)
    for text in ("免费试用期有多久？", "API Key 放在 Authorization Bearer 请求头吗？", "今天天气怎么样"):
        for d in retriever.search(query(text)).docs:
            assert 0.0 <= d.score <= 1.0, d.score


@test
def test_retrieval_known_question_top_page():
    _t, _s, _r, kb = fresh()
    res = Retriever(kb, 2, 5).search(query("免费试用期有多久？"))
    assert res.top_doc.page_id == "KB-002", res.top_doc.page_id


@test
def test_retrieval_unknown_question_zero_score():
    _t, _s, _r, kb = fresh()
    res = Retriever(kb, 2, 5).search(query("你们公司的股票代码是多少？"))
    assert res.top_score == 0.0, res.top_score


# ---------------------------------------------------------------- 门控
@test
def test_gate_answered():
    d = decide(result_with([0.5, 0.1]), 0.12, 0.32)
    assert d.branch == BRANCH_ANSWERED and d.allowed is True


@test
def test_gate_low_confidence():
    d = decide(result_with([0.2, 0.1]), 0.12, 0.32)
    assert d.branch == BRANCH_LOW_CONFIDENCE and d.allowed is False


@test
def test_gate_no_hit_when_empty():
    d = decide(result_with([]), 0.12, 0.32)
    assert d.branch == BRANCH_NO_HIT and d.allowed is False


@test
def test_gate_no_hit_below_min_relevance():
    d = decide(result_with([0.05]), 0.12, 0.32)
    assert d.branch == BRANCH_NO_HIT and d.allowed is False


@test
def test_gate_margin_can_force_handoff():
    d = decide(result_with([0.5, 0.49]), 0.12, 0.32, min_margin=0.1)
    assert d.branch == BRANCH_LOW_CONFIDENCE and d.allowed is False


@test
def test_assert_allowed_raises_when_blocked():
    d = decide(result_with([0.2]), 0.12, 0.32)
    expect_raises(GenerationBlocked, assert_allowed, d)


@test
def test_compose_answer_has_citation():
    _t, _s, _r, kb = fresh()
    res = Retriever(kb, 2, 5).search(query("如何申请退款？"))
    d = decide(res, 0.12, 0.32)
    ans = compose_answer(res, d, "S-1", 2)
    assert ans.citations, "答案必须带引用"
    assert ans.citations[0].page_id == "KB-003"
    assert "KB-003" in ans.text
    assert ans.generated is True


@test
def test_compose_blocked_when_gate_not_allowed():
    _t, _s, _r, kb = fresh()
    res = Retriever(kb, 2, 5).search(query("企业版和团队版的价格差多少？"))
    d = decide(res, 0.12, 0.32)
    assert d.allowed is False
    expect_raises(GenerationBlocked, compose_answer, res, d, "S-1", 2)


# ---------------------------------------------------------------- 三分支编排
@test
def test_agent_known_branch_answers():
    agent = make_agent()
    out = agent.ask(query("免费试用期有多久？", qid="A1"))
    assert out.gate.branch == BRANCH_ANSWERED
    assert out.answer is not None and out.ticket is None
    assert out.answer.citations[0].page_id == "KB-002"


@test
def test_agent_unknown_branch_handoff():
    agent = make_agent()
    out = agent.ask(query("你们公司的股票代码是多少？", qid="A2"))
    assert out.gate.branch == BRANCH_NO_HIT
    assert out.answer is None and out.ticket is not None
    assert out.ticket.route["queue"] == "human-l1"


@test
def test_agent_low_confidence_handoff():
    agent = make_agent()
    out = agent.ask(query("企业版和团队版的价格差多少？", qid="A3"))
    assert out.gate.branch == BRANCH_LOW_CONFIDENCE
    assert out.answer is None and out.ticket is not None
    assert out.ticket.route["queue"] == "human-l2-specialist"


@test
def test_handoff_ticket_has_context_package():
    agent = make_agent()
    out = agent.ask(query("企业版和团队版的价格差多少？", qid="A4", session="S-CTX"))
    t = out.ticket
    assert t.question == "企业版和团队版的价格差多少？"
    assert t.session_id == "S-CTX"
    assert isinstance(t.retrieval_candidates, tuple) and t.retrieval_candidates
    assert t.confidence == out.gate.confidence
    assert set(t.thresholds) == {"min_relevance", "answer_confidence"}
    first = t.retrieval_candidates[0]
    assert {"page_id", "title", "url", "score"} <= set(first)


@test
def test_handoff_route_sla_values():
    _t, _s, routing, _kb = fresh()
    no_hit = route_for(BRANCH_NO_HIT, routing)
    low = route_for(BRANCH_LOW_CONFIDENCE, routing)
    limited = route_for(BRANCH_RATE_LIMITED, routing)
    assert (no_hit["queue"], no_hit["sla_hours"]) == ("human-l1", 4)
    assert (low["queue"], low["sla_hours"]) == ("human-l2-specialist", 2)
    assert (limited["queue"], limited["sla_hours"]) == ("security-abuse", 1)


@test
def test_route_falls_back_to_default():
    _t, _s, routing, _kb = fresh()
    picked = route_for("some_unknown_branch", routing)
    assert picked["queue"] == routing.default_route["queue"]


@test
def test_gate_runs_before_generation():
    agent = make_agent()
    agent.ask(query("免费试用期有多久？", qid="A5"))
    steps = [(e["step"], e.get("query_id")) for e in agent.recorder.entries if e.get("query_id") == "A5"]
    names = [s[0] for s in steps]
    assert names.index("gate") < names.index("generation"), names


@test
def test_handoff_skips_generation():
    agent = make_agent()
    agent.ask(query("你们公司的股票代码是多少？", qid="A6"))
    steps = [e["step"] for e in agent.recorder.entries if e.get("query_id") == "A6"]
    assert "generation" not in steps, steps
    assert "generation_skipped" in steps, steps
    assert steps.index("gate") < steps.index("handoff")


@test
def test_agent_citations_are_traceable():
    agent = make_agent()
    kb_ids = set(agent.kb.page_ids)
    for text, qid in (("如何申请退款？", "C1"), ("控制台支持哪些浏览器？", "C2")):
        out = agent.ask(query(text, qid=qid))
        assert out.answer is not None
        for c in out.answer.citations:
            assert c.page_id in kb_ids
            assert c.page_id in {d.page_id for d in out.retrieval.docs}


# ---------------------------------------------------------------- 转人工工单
@test
def test_ticket_ids_are_sequential():
    agent = make_agent()
    agent.ask(query("你们公司的股票代码是多少？", qid="T1"))
    agent.ask(query("推荐一款机械键盘", qid="T2"))
    ids = [t.ticket_id for t in agent.desk.tickets]
    assert ids == ["HT-0001", "HT-0002"], ids


@test
def test_tickets_jsonl_roundtrip():
    agent = make_agent()
    agent.ask(query("你们公司的股票代码是多少？", qid="T3", session="S-JSONL"))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tickets.jsonl"
        agent.desk.write_jsonl(path)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1
    row = rows[0]
    for key in ("ticket_id", "session_id", "question", "confidence", "retrieval_candidates", "route", "status"):
        assert key in row, key
    assert row["status"] == "open"


@test
def test_handoff_desk_summary():
    agent = make_agent()
    agent.ask(query("你们公司的股票代码是多少？", qid="S1"))
    agent.ask(query("企业版和团队版的价格差多少？", qid="S2"))
    summary = agent.desk.summary()
    assert summary["handoff_no_hit"] == 1
    assert summary["handoff_low_confidence"] == 1
    assert summary["total"] == 2


# ---------------------------------------------------------------- 限流
@test
def test_rate_limiter_allows_until_limit():
    limiter = RateLimiter(window_seconds=60, max_requests=3)
    decisions = [limiter.check("k", 0) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[3].retry_after_seconds == 60


@test
def test_rate_limiter_window_resets():
    limiter = RateLimiter(window_seconds=60, max_requests=1)
    assert limiter.check("k", 0).allowed is True
    assert limiter.check("k", 10).allowed is False
    assert limiter.check("k", 61).allowed is True


@test
def test_rate_limiter_disabled():
    limiter = RateLimiter(window_seconds=60, max_requests=1, enabled=False)
    assert all(limiter.check("k", 0).allowed for _ in range(5))


@test
def test_rate_limiter_invalid_config():
    expect_raises(RateLimitConfigError, RateLimiter, 0, 3)
    expect_raises(RateLimitConfigError, RateLimiter, 60, 0)


@test
def test_agent_rate_limited_skips_retrieval():
    agent = make_agent()
    outcomes = [agent.ask(query("退款要多久", session="S-RL", qid=f"R{i}")) for i in range(1, 8)]
    assert outcomes[0].gate.branch != BRANCH_RATE_LIMITED
    assert outcomes[5].gate.branch == BRANCH_RATE_LIMITED
    assert outcomes[6].gate.branch == BRANCH_RATE_LIMITED
    assert outcomes[5].retrieval.docs == ()
    assert outcomes[5].ticket.route["queue"] == "security-abuse"
    steps = [e["step"] for e in agent.recorder.entries if e.get("query_id") == "R6"]
    assert "retrieval_skipped" in steps and "retrieval" not in steps, steps


@test
def test_rate_limit_threshold_is_configurable():
    thresholds, sources, routing, kb = fresh()
    thresholds.rate_limit["max_requests"] = 2
    agent = OfficialSiteAgent(kb, thresholds, routing)
    outcomes = [agent.ask(query("退款要多久", session="S-RL2", qid=f"X{i}")) for i in range(1, 5)]
    assert [o.gate.branch == BRANCH_RATE_LIMITED for o in outcomes] == [False, False, True, True]


# ---------------------------------------------------------------- 配置校验
@test
def test_config_load_returns_three_parts():
    thresholds, sources, routing = load_config_dir(CONFIG)
    assert thresholds.top_k >= 1 and sources.allowed_hosts and routing.routes


@test
def test_config_rejects_threshold_order():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "thresholds.json"
        write_json(path, {
            "retrieval": {"top_k": 5, "min_term_len": 2},
            "gate": {"min_relevance": 0.5, "answer_confidence": 0.2, "min_margin": 0.0},
            "rate_limit": {"enabled": True, "window_seconds": 60, "max_requests": 5},
        })
        expect_raises(ConfigError, load_thresholds, path)


@test
def test_config_rejects_empty_allowed_hosts():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "knowledge_sources.json"
        write_json(path, {"allowed_hosts": [], "denied_hosts": []})
        expect_raises(ConfigError, load_knowledge_sources, path)


@test
def test_config_rejects_duplicate_host():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "knowledge_sources.json"
        entry = {"host": "help.example", "path_prefix": "/docs/", "authorized": True}
        write_json(path, {"allowed_hosts": [entry, dict(entry)]})
        expect_raises(ConfigError, load_knowledge_sources, path)


@test
def test_config_rejects_duplicate_route_branch():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "handoff_routing.json"
        route = {"id": "R1", "match": {"branch": "b"}, "queue": "q", "sla_hours": 1}
        write_json(path, {
            "routes": [route, dict(route)],
            "default_route": {"id": "D", "queue": "q", "sla_hours": 1},
        })
        expect_raises(ConfigError, load_handoff_routing, path)


# ---------------------------------------------------------------- 轨迹与确定性
@test
def test_trace_seq_and_clock_monotonic():
    rec = TraceRecorder()
    rec.record("a")
    rec.record("b")
    rec.record("c")
    assert [e["seq"] for e in rec.entries] == [1, 2, 3]
    stamps = [e["at"] for e in rec.entries]
    assert stamps == sorted(stamps)
    assert rec.summary()["total"] == 3


@test
def test_trace_jsonl_is_parseable():
    rec = TraceRecorder()
    rec.record("a", query_id="Q1")
    rec.record("b", query_id="Q1")
    rows = [json.loads(line) for line in rec.to_jsonl().splitlines() if line]
    assert len(rows) == 2 and rows[0]["step"] == "a"


@test
def test_agent_is_deterministic():
    def run():
        agent = make_agent()
        for q in (
            query("免费试用期有多久？", session="D1", qid="D1"),
            query("你们公司的股票代码是多少？", session="D2", qid="D2"),
            query("企业版和团队版的价格差多少？", session="D3", qid="D3"),
        ):
            agent.ask(q)
        return agent.recorder.to_jsonl()

    assert run() == run()


@test
def test_source_uses_only_stdlib_and_local_package():
    package = ROOT / "official_site_agent"
    allowed = set(sys.stdlib_module_names) | {"official_site_agent"}
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
                top = module.split(".")[0]
                assert top in allowed, f"{path.name} 引入了非标准库模块 {module}"


def main() -> int:
    passed = 0
    failures: list[tuple[str, str]] = []
    for name, fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"ok   {name}")
        except Exception:
            failures.append((name, traceback.format_exc()))
            print(f"FAIL {name}")

    print("")
    for name, tb in failures:
        print(f"--- FAILURE: {name} ---")
        print(tb)
    print(f"{passed}/{len(TESTS)} tests passed")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
