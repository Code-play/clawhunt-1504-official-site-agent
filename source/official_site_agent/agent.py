"""官网咨询 Agent 的编排层：把限流、检索、门控、生成、转人工串成一条流水线。

流水线顺序是硬约束（对应买家关注点 1）：

    限流 -> 检索 -> 门控 -> 生成 / 转人工

门控未放行时，``compose_answer`` 根本不会被调用；工单在门控之后立即生成。
每一步都写入 TraceRecorder，便于逐条核对「为什么答 / 为什么转」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .answer import compose_answer
from .artifacts import write_artifacts
from .config import HandoffRouting, Thresholds
from .gate import BRANCH_ANSWERED, decide
from .handoff import HandoffDesk
from .knowledge import KnowledgeBase
from .model import GateDecision, Query, RetrievalResult, TurnOutcome
from .ratelimit import RateLimiter, SyntheticSecondsClock
from .retrieval import Retriever
from .trace import TraceRecorder

BRANCH_RATE_LIMITED = "rate_limited"


class OfficialSiteAgent:
    """面向官网咨询的知识问答 Bot。"""

    def __init__(
        self,
        kb: KnowledgeBase,
        thresholds: Thresholds,
        routing: HandoffRouting,
        recorder: TraceRecorder | None = None,
        limiter: RateLimiter | None = None,
        seconds: SyntheticSecondsClock | None = None,
    ) -> None:
        self.kb = kb
        self.thresholds = thresholds
        self.routing = routing
        self.recorder = recorder or TraceRecorder()
        self.limiter = limiter or RateLimiter.from_config(thresholds.rate_limit)
        self.seconds = seconds or SyntheticSecondsClock()
        self.retriever = Retriever(
            kb,
            min_term_len=thresholds.min_term_len,
            top_k=thresholds.top_k,
        )
        self.desk = HandoffDesk(routing)
        self.outcomes: list[TurnOutcome] = []

    # ---- 主入口 -----------------------------------------------------------
    def ask(self, query: Query, rate_key: str | None = None) -> TurnOutcome:
        outcome = self._ask(query, rate_key)
        self.outcomes.append(outcome)
        return outcome

    def _ask(self, query: Query, rate_key: str | None) -> TurnOutcome:
        key = rate_key or query.session_id
        rate = self.limiter.check(key, self.seconds.now())

        if not rate.allowed:
            seq = self.recorder.record(
                "ratelimit",
                session_id=query.session_id,
                query_id=query.qid,
                key=rate.key,
                allowed=False,
                used=rate.used,
                limit=rate.limit,
                reason=rate.reason,
            )["seq"]
            # 限流分支跳过检索与生成，直接进安全队列；不消耗知识库与生成环节。
            gate = GateDecision(
                branch=BRANCH_RATE_LIMITED,
                confidence=0.0,
                answer_threshold=self.thresholds.answer_confidence,
                min_relevance=self.thresholds.min_relevance,
                allowed=False,
                reason=f"触发限流，未执行检索与生成：{rate.reason}",
            )
            empty = RetrievalResult(query.qid, query.text, (), len(self.kb.pages))
            created_at = self.recorder.entries[-1]["at"]
            ticket = self.desk.create(empty, gate, query.session_id, created_at=created_at, trace_ref=seq)
            self.recorder.record(
                "handoff",
                at=created_at,
                session_id=query.session_id,
                query_id=query.qid,
                ticket_id=ticket.ticket_id,
                branch=gate.branch,
                queue=ticket.route["queue"],
                reason=gate.reason,
            )
            self.recorder.record(
                "retrieval_skipped",
                at=created_at,
                session_id=query.session_id,
                query_id=query.qid,
                reason="触发限流，未执行检索",
            )
            return TurnOutcome(query=query, retrieval=empty, gate=gate, ticket=ticket, rate=rate)

        self.recorder.record(
            "ratelimit",
            session_id=query.session_id,
            query_id=query.qid,
            key=rate.key,
            allowed=True,
            used=rate.used,
            limit=rate.limit,
            reason=rate.reason,
        )

        # ---- 检索（只访问授权子集） ----
        result = self.retriever.search(query)
        self.recorder.record(
            "retrieval",
            session_id=query.session_id,
            query_id=query.qid,
            candidates=len(result.docs),
            top_score=result.top_score,
            top_page=result.docs[0].page_id if result.docs else None,
            indexed_pages=result.indexed_pages,
            authorized_only=True,
        )

        # ---- 门控（在生成之前） ----
        decision = decide(
            result,
            self.thresholds.min_relevance,
            self.thresholds.answer_confidence,
            self.thresholds.min_margin,
        )
        at_gate = self.recorder.entries[-1]["at"]
        gate_seq = self.recorder.record(
            "gate",
            at=at_gate,
            session_id=query.session_id,
            query_id=query.qid,
            branch=decision.branch,
            confidence=round(decision.confidence, 6),
            answer_threshold=decision.answer_threshold,
            min_relevance=decision.min_relevance,
            allowed=decision.allowed,
            reason=decision.reason,
        )["seq"]

        # ---- 分支：生成 / 转人工 ----
        if decision.allowed and decision.branch == BRANCH_ANSWERED:
            answer = compose_answer(
                result,
                decision,
                session_id=query.session_id,
                min_term_len=self.thresholds.min_term_len,
            )
            self.recorder.record(
                "generation",
                at=at_gate,
                session_id=query.session_id,
                query_id=query.qid,
                citations=[c.page_id for c in answer.citations],
                confidence=round(answer.confidence, 6),
                text=answer.text,
            )
            return TurnOutcome(query=query, retrieval=result, gate=decision, answer=answer, rate=rate)

        # 未命中 / 低置信度：一律转人工，且不调用生成。
        ticket = self.desk.create(result, decision, query.session_id, created_at=at_gate, trace_ref=gate_seq)
        self.recorder.record(
            "handoff",
            at=at_gate,
            session_id=query.session_id,
            query_id=query.qid,
            ticket_id=ticket.ticket_id,
            branch=decision.branch,
            queue=ticket.route["queue"],
            confidence=round(decision.confidence, 6),
            reason=decision.reason,
        )
        # 明确记录「生成被跳过」，这是门控前置的直接证据。
        self.recorder.record(
            "generation_skipped",
            at=at_gate,
            session_id=query.session_id,
            query_id=query.qid,
            branch=decision.branch,
            reason="门控未放行，未进入生成环节",
        )
        return TurnOutcome(query=query, retrieval=result, gate=decision, ticket=ticket, rate=rate)

    # ---- 批量 -------------------------------------------------------------
    def ask_many(self, queries: Iterable[Query]) -> list[TurnOutcome]:
        return [self.ask(q) for q in queries]

    # ---- 产物 -------------------------------------------------------------
    def write_artifacts(self, out_dir: Path) -> dict[str, Path]:
        return write_artifacts(self.outcomes, self.desk.tickets, self.recorder, out_dir)
