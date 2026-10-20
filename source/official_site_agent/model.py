"""领域模型：知识条目、查询、检索候选、门控决策、答案、转人工工单、限流决策。

只依赖 Python 标准库。所有字段都是可解释的，便于在演示与轨迹里逐条打印。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class KbPage:
    """一条官网知识条目（合成/脱敏）。

    ``authorized`` 表示资料本身是否获授权对外；此外 url 的 host 还必须命中
    授权白名单（config/knowledge_sources.json），二者同时满足才会进入检索索引。
    """

    page_id: str
    title: str
    text: str
    url: str
    tags: tuple[str, ...] = ()
    authorized: bool = True

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "KbPage":
        return KbPage(
            page_id=str(raw["page_id"]),
            title=str(raw.get("title", "")),
            text=str(raw.get("text", "")),
            url=str(raw.get("url", "")),
            tags=tuple(raw.get("tags", ())),
            authorized=bool(raw.get("authorized", True)),
        )


@dataclass(frozen=True)
class Query:
    """一条访客问题（合成/脱敏）。``expect`` 是期望分支，供验收对照。"""

    qid: str
    session_id: str
    text: str
    expect: str = ""

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Query":
        return Query(
            qid=str(raw["qid"]),
            session_id=str(raw.get("session_id", "S-1")),
            text=str(raw["text"]),
            expect=str(raw.get("expect", "")),
        )


@dataclass(frozen=True)
class RetrievedDoc:
    """一个检索候选：命中的知识条目 + 分数 + 命中词。"""

    page_id: str
    title: str
    url: str
    score: float
    matched_terms: tuple[str, ...]
    snippet: str
    context: str = ""


@dataclass(frozen=True)
class RetrievalResult:
    """一次检索的输出：结构化候选列表（只来自授权子集）。"""

    query_id: str
    query_text: str
    docs: tuple[RetrievedDoc, ...]
    indexed_pages: int

    @property
    def top_score(self) -> float:
        return self.docs[0].score if self.docs else 0.0

    @property
    def top_doc(self) -> RetrievedDoc | None:
        return self.docs[0] if self.docs else None


@dataclass(frozen=True)
class GateDecision:
    """置信度门控决策。

    关键：这个决策在**生成之前**完成。只有 ``allowed`` 为真，才会进入答案生成；
    否则一律转人工，绝不硬答。
    """

    branch: str
    confidence: float
    answer_threshold: float
    min_relevance: float
    allowed: bool
    reason: str


@dataclass(frozen=True)
class Citation:
    """答案里可追溯的来源条目引用。"""

    page_id: str
    title: str
    url: str
    score: float


@dataclass(frozen=True)
class Answer:
    """命中作答分支的答案（抽取式，带来源条目 id 引用）。"""

    query_id: str
    session_id: str
    question: str
    branch: str
    text: str
    citations: tuple[Citation, ...]
    confidence: float
    generated: bool = True


@dataclass(frozen=True)
class RateLimitDecision:
    """限流决策：允许 / 拒绝，以及剩余额度与建议重试等待。"""

    allowed: bool
    key: str
    limit: int
    used: int
    remaining: int
    retry_after_seconds: int
    window_seconds: int
    reason: str = ""


@dataclass(frozen=True)
class HandoffTicket:
    """转人工工单：携带完整上下文包（问题原文、检索候选、置信度、会话 id）。"""

    ticket_id: str
    created_at: str
    session_id: str
    query_id: str
    question: str
    branch: str
    reason: str
    confidence: float
    thresholds: dict[str, Any]
    retrieval_candidates: tuple[dict[str, Any], ...]
    route: dict[str, Any]
    status: str = "open"
    trace_ref: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "query_id": self.query_id,
            "question": self.question,
            "branch": self.branch,
            "reason": self.reason,
            "confidence": round(self.confidence, 6),
            "thresholds": self.thresholds,
            "retrieval_candidates": list(self.retrieval_candidates),
            "route": self.route,
            "status": self.status,
            "trace_ref": self.trace_ref,
        }


@dataclass
class TurnOutcome:
    """一次「问 -> 答」的完整结果，供演示与验收逐条核对。"""

    query: Query
    retrieval: RetrievalResult
    gate: GateDecision
    answer: Answer | None = None
    ticket: HandoffTicket | None = None
    rate: RateLimitDecision | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)


def thresholds_snapshot(min_relevance: float, answer_confidence: float, min_margin: float) -> dict[str, Any]:
    """把门控阈值打包进工单，便于人工复核「当时为什么转人工」。"""
    return {
        "min_relevance": min_relevance,
        "answer_confidence": answer_confidence,
        "min_margin": min_margin,
    }
