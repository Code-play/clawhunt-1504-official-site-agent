"""转人工：构造上下文包并落成工单记录。

上下文包必须包含买家点名的四项：问题原文、检索候选、置信度、会话 id。
此外还带上：命中分支、门控阈值快照、路由队列、SLA、优先级、轨迹引用，
方便人工接手时无需再问访客一遍。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import HandoffRouting
from .model import GateDecision, HandoffTicket, RetrievalResult


class HandoffError(RuntimeError):
    """路由配置无法匹配当前分支。"""


def route_for(branch: str, routing: HandoffRouting) -> dict[str, Any]:
    """按分支选路；分支未单独配置时落到 default_route。"""
    for route in routing.routes:
        if str(route["match"].get("branch", "")) == branch:
            return {
                "route_id": route["id"],
                "queue": route["queue"],
                "sla_hours": int(route["sla_hours"]),
                "priority": route.get("priority", "normal"),
                "note": route.get("note", ""),
            }
    default = routing.default_route
    return {
        "route_id": default["id"],
        "queue": default["queue"],
        "sla_hours": int(default["sla_hours"]),
        "priority": default.get("priority", "normal"),
        "note": default.get("note", ""),
    }


class HandoffDesk:
    """工单台：生成确定性 ticket_id，累积工单并可导出 JSONL。"""

    def __init__(self, routing: HandoffRouting, id_prefix: str = "HT") -> None:
        self.routing = routing
        self.id_prefix = id_prefix
        self.tickets: list[HandoffTicket] = []

    def _next_id(self) -> str:
        return f"{self.id_prefix}-{len(self.tickets) + 1:04d}"

    def create(
        self,
        result: RetrievalResult,
        decision: GateDecision,
        session_id: str,
        created_at: str,
        trace_ref: int = 0,
    ) -> HandoffTicket:
        route = route_for(decision.branch, self.routing)
        candidates = tuple(
            {
                "page_id": doc.page_id,
                "title": doc.title,
                "url": doc.url,
                "score": doc.score,
                "matched_terms": list(doc.matched_terms),
            }
            for doc in result.docs
        )
        thresholds = {
            "min_relevance": decision.min_relevance,
            "answer_confidence": decision.answer_threshold,
        }
        ticket = HandoffTicket(
            ticket_id=self._next_id(),
            created_at=created_at,
            session_id=session_id,
            query_id=result.query_id,
            question=result.query_text,
            branch=decision.branch,
            reason=decision.reason,
            confidence=decision.confidence,
            thresholds=thresholds,
            retrieval_candidates=candidates,
            route=route,
            status="open",
            trace_ref=trace_ref,
        )
        # 不变量：上下文包四项必须齐全，缺一即视为实现缺陷。
        missing = [
            name
            for name, value in (
                ("question", ticket.question),
                ("retrieval_candidates", ticket.retrieval_candidates),
                ("session_id", ticket.session_id),
                ("confidence", ticket.confidence),
            )
            if value is None or value == ""
        ]
        if missing:
            raise HandoffError(f"转人工上下文包缺少字段: {missing}")
        self.tickets.append(ticket)
        return ticket

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(t.to_dict(), ensure_ascii=False, sort_keys=False) for t in self.tickets) + (
            "\n" if self.tickets else ""
        )

    def write_jsonl(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(), encoding="utf-8")
        return path

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for ticket in self.tickets:
            counts[ticket.branch] = counts.get(ticket.branch, 0) + 1
        counts["total"] = len(self.tickets)
        return counts
