"""限流与防滥用（可配置）。

实现为**滑动窗口**：每个 key（默认会话 id，也可换成 IP 或账号）在最近
``window_seconds`` 秒内最多允许 ``max_requests`` 次。相比固定窗口，滑动窗口
不会在窗口边界放过双倍流量；相比令牌桶，它对「突发刷问」的抑制更直接。

时钟通过参数注入，演示里用确定性秒表，因此限流行为也可逐字节复跑。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Hashable

from .model import RateLimitDecision


class RateLimitConfigError(ValueError):
    """限流配置非法。"""


@dataclass
class RateLimiter:
    """滑动窗口限流器。线程安全性不在本单范围（单进程演示）。"""

    window_seconds: int
    max_requests: int
    enabled: bool = True
    abuse_queue: str = "security-abuse"
    abuse_sla_hours: int = 1
    _hits: dict[Hashable, Deque[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.window_seconds < 1:
            raise RateLimitConfigError("window_seconds 必须 >= 1")
        if self.max_requests < 1:
            raise RateLimitConfigError("max_requests 必须 >= 1")
        if self.abuse_sla_hours < 1:
            raise RateLimitConfigError("abuse_sla_hours 必须 >= 1")

    @staticmethod
    def from_config(rate_limit: dict[str, Any]) -> "RateLimiter":
        return RateLimiter(
            window_seconds=int(rate_limit.get("window_seconds", 60)),
            max_requests=int(rate_limit.get("max_requests", 10)),
            enabled=bool(rate_limit.get("enabled", True)),
            abuse_queue=str(rate_limit.get("abuse_queue", "security-abuse")),
            abuse_sla_hours=int(rate_limit.get("abuse_sla_hours", 1)),
        )

    def _prune(self, key: Hashable, now: int) -> Deque[int]:
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def check(self, key: Hashable, now: int) -> RateLimitDecision:
        """登记一次请求并返回决策。now 为整数秒（单调递增）。"""
        if not self.enabled:
            return RateLimitDecision(
                allowed=True,
                key=str(key),
                limit=self.max_requests,
                used=0,
                remaining=self.max_requests,
                retry_after_seconds=0,
                window_seconds=self.window_seconds,
                reason="限流已关闭（rate_limit.enabled=false）",
            )

        bucket = self._prune(key, now)
        used = len(bucket)
        if used >= self.max_requests:
            retry_after = max(1, bucket[0] + self.window_seconds - now)
            return RateLimitDecision(
                allowed=False,
                key=str(key),
                limit=self.max_requests,
                used=used,
                remaining=0,
                retry_after_seconds=retry_after,
                window_seconds=self.window_seconds,
                reason=(
                    f"最近 {self.window_seconds}s 已请求 {used} 次，达到上限 {self.max_requests}"
                    f"，需等待 {retry_after}s"
                ),
            )

        bucket.append(now)
        used += 1
        return RateLimitDecision(
            allowed=True,
            key=str(key),
            limit=self.max_requests,
            used=used,
            remaining=self.max_requests - used,
            retry_after_seconds=0,
            window_seconds=self.window_seconds,
            reason=f"窗口内第 {used}/{self.max_requests} 次请求",
        )

    def reset(self, key: Hashable | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


class SyntheticSecondsClock:
    """确定性秒表：固定起点，每次 tick 前进固定步长。"""

    def __init__(self, start: int = 1_800_000_000, step_seconds: int = 1) -> None:
        self._now = start
        self._step = step_seconds

    def now(self) -> int:
        return self._now

    def tick(self) -> int:
        value = self._now
        self._now += self._step
        return value
