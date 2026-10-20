"""产物落盘：把一次演示的结果写成可逐项核对的 JSON / JSONL。

产物
----
* ``citations.jsonl``        命中作答分支：答案 + 来源条目 id 引用（引用结果）
* ``handoff_tickets.jsonl``  转人工工单记录（上下文包）
* ``retrieval_trace.jsonl``  每次问询的检索候选与门控结论
* ``decisions.jsonl``        一行一个结论：分支 / 置信度 / 阈值 / 引用或工单号
* ``trace.jsonl``            完整执行轨迹
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from .model import HandoffTicket, TurnOutcome
from .trace import TraceRecorder


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=False) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def write_artifacts(
    outcomes: Sequence[TurnOutcome],
    tickets: Sequence[HandoffTicket],
    recorder: TraceRecorder,
    out_dir: Path,
) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    citation_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    retrieval_rows: list[dict[str, Any]] = []

    for item in outcomes:
        retrieval_rows.append(
            {
                "query_id": item.query.qid,
                "session_id": item.query.session_id,
                "question": item.query.text,
                "expect": item.query.expect,
                "branch": item.gate.branch,
                "confidence": round(item.gate.confidence, 6),
                "answer_threshold": item.gate.answer_threshold,
                "min_relevance": item.gate.min_relevance,
                "allowed": item.gate.allowed,
                "reason": item.gate.reason,
                "candidates": [
                    {
                        "page_id": doc.page_id,
                        "title": doc.title,
                        "url": doc.url,
                        "score": doc.score,
                        "matched_terms": list(doc.matched_terms),
                    }
                    for doc in item.retrieval.docs
                ],
            }
        )

        if item.answer is not None:
            citation_rows.append(
                {
                    "query_id": item.answer.query_id,
                    "session_id": item.answer.session_id,
                    "question": item.answer.question,
                    "answer": item.answer.text,
                    "confidence": round(item.answer.confidence, 6),
                    "citations": [
                        {
                            "page_id": c.page_id,
                            "title": c.title,
                            "url": c.url,
                            "score": c.score,
                        }
                        for c in item.answer.citations
                    ],
                }
            )

        decision_rows.append(
            {
                "query_id": item.query.qid,
                "expect": item.query.expect,
                "branch": item.gate.branch,
                "confidence": round(item.gate.confidence, 6),
                "threshold": item.gate.answer_threshold,
                "allowed": item.gate.allowed,
                "citation_ids": [c.page_id for c in item.answer.citations] if item.answer else [],
                "ticket_id": item.ticket.ticket_id if item.ticket else None,
                "queue": item.ticket.route["queue"] if item.ticket else None,
            }
        )

    paths["citations"] = _write_jsonl(out_dir / "citations.jsonl", citation_rows)
    paths["handoff_tickets"] = _write_jsonl(
        out_dir / "handoff_tickets.jsonl", [t.to_dict() for t in tickets]
    )
    paths["retrieval_trace"] = _write_jsonl(out_dir / "retrieval_trace.jsonl", retrieval_rows)
    paths["decisions"] = _write_jsonl(out_dir / "decisions.jsonl", decision_rows)
    paths["trace"] = recorder.write_jsonl(out_dir / "trace.jsonl")
    return paths
