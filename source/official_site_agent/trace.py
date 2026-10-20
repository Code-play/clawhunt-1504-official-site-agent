"""执行轨迹：把一次问询的每一步记成结构化事件，可导出 JSONL。

轨迹既是产物也是证据：每条记录带 seq、step、时间、会话、问题 id 与可读理由。
时间来自确定性时钟（固定起点 + 固定步长），保证「一键复跑」输出逐字节一致。

典型步骤序列：

    ratelimit -> retrieval -> gate -> generation
    ratelimit -> retrieval -> gate -> handoff
    ratelimit(denied) -> rate_limited
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class SyntheticClock:
    """确定性时钟：固定起点，每次取时前进固定步长。"""

    def __init__(self, start: str = "2026-01-05T09:00:00Z", step_seconds: int = 30) -> None:
        self._t = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> str:
        value = self._t
        self._t = self._t + self._step
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class TraceRecorder:
    """按顺序记录执行轨迹。"""

    def __init__(self, clock: SyntheticClock | None = None) -> None:
        self.clock = clock or SyntheticClock()
        self.entries: list[dict[str, Any]] = []

    def record(self, step: str, at: str | None = None, **fields: Any) -> dict[str, Any]:
        """记一条轨迹。传 at 时与调用方共用同一时间戳，避免同一步出现两个时间。"""
        entry: dict[str, Any] = {"seq": len(self.entries) + 1, "step": step, "at": at or self.clock.now()}
        entry.update(fields)
        self.entries.append(entry)
        return entry

    def last_seq(self) -> int:
        return len(self.entries)

    # ---- 导出 -------------------------------------------------------------
    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(e, ensure_ascii=False, sort_keys=False) for e in self.entries) + (
            "\n" if self.entries else ""
        )

    def write_jsonl(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(), encoding="utf-8")
        return path

    def render_table(self) -> str:
        """人可读的轨迹表。"""
        head = ["seq", "step", "at", "query", "what"]
        rows: list[list[str]] = []
        for e in self.entries:
            step = e.get("step")
            if step == "ratelimit":
                what = f"限流 {'放行' if e.get('allowed') else '拒绝'}: {e.get('reason', '')}"
            elif step == "retrieval":
                what = f"检索命中 {e.get('candidates')} 条，top={e.get('top_score')}"
            elif step == "gate":
                what = f"门控 {e.get('branch')}（置信度 {e.get('confidence')}，放行={e.get('allowed')}）"
            elif step == "generation":
                what = f"生成答案，引用 {e.get('citations')}"
            elif step == "handoff":
                what = f"转人工 {e.get('ticket_id')} -> {e.get('queue')}"
            elif step == "rate_limited":
                what = f"拒绝作答，等待 {e.get('retry_after_seconds')}s"
            else:
                what = str(e.get("what", ""))
            rows.append([str(e["seq"]), str(step), str(e["at"]), str(e.get("query_id", "-")), what])

        widths = [len(h) for h in head]
        for r in rows:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], len(cell))

        def line(cells: list[str]) -> str:
            return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

        sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
        return "\n".join([line(head), sep] + [line(r) for r in rows])

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            counts[str(e["step"])] = counts.get(str(e["step"]), 0) + 1
        counts["total"] = len(self.entries)
        return counts
