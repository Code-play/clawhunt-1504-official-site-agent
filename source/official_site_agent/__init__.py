"""official_site_agent —— 面向官网咨询的知识问答 Bot（ClawHunt #1504）。

模块划分
--------
- ``model``       领域模型：知识条目 / 查询 / 检索候选 / 门控决策 / 答案 / 工单 / 限流
- ``config``      JSON 配置加载与校验（阈值 / 授权知识源白名单 / 转人工路由）
- ``knowledge``   授权知识源白名单与知识加载（只保留授权子集）
- ``retrieval``   纯标准库检索：分词 + TF-IDF + 余弦相似度（确定性）
- ``gate``        置信度门 + 三分支决策（阈值判断发生在生成之前）
- ``answer``      门控放行后的抽取式答案生成（带来源条目 id 引用）
- ``handoff``     转人工上下文包与工单落盘
- ``ratelimit``   滑动窗口限流与防滥用
- ``trace``       执行轨迹记录与渲染
- ``agent``       编排层：限流 -> 检索 -> 门控 -> 生成 / 转人工
- ``cli``         命令行入口，一键复跑 demo

只依赖 Python 标准库。
"""

from __future__ import annotations

from .agent import BRANCH_RATE_LIMITED, OfficialSiteAgent
from .answer import compose_answer
from .config import (
    ConfigError,
    HandoffRouting,
    KnowledgeSources,
    Thresholds,
    load_config_dir,
)
from .gate import (
    BRANCH_ANSWERED,
    BRANCH_LOW_CONFIDENCE,
    BRANCH_NO_HIT,
    GenerationBlocked,
    assert_allowed,
    decide,
)
from .handoff import HandoffDesk, HandoffError, route_for
from .knowledge import (
    ExcludedPage,
    KnowledgeBase,
    KnowledgeError,
    build_knowledge_base,
    load_kb_pages,
    load_knowledge,
)
from .model import (
    Answer,
    Citation,
    GateDecision,
    HandoffTicket,
    KbPage,
    Query,
    RateLimitDecision,
    RetrievalResult,
    RetrievedDoc,
    TurnOutcome,
)
from .ratelimit import RateLimiter, RateLimitConfigError, SyntheticSecondsClock
from .retrieval import Retriever, TfidfIndex, tokenize
from .trace import SyntheticClock, TraceRecorder

__all__ = [
    "BRANCH_ANSWERED",
    "BRANCH_LOW_CONFIDENCE",
    "BRANCH_NO_HIT",
    "BRANCH_RATE_LIMITED",
    "OfficialSiteAgent",
    "compose_answer",
    "ConfigError",
    "HandoffRouting",
    "KnowledgeSources",
    "Thresholds",
    "load_config_dir",
    "GenerationBlocked",
    "assert_allowed",
    "decide",
    "HandoffDesk",
    "HandoffError",
    "route_for",
    "ExcludedPage",
    "KnowledgeBase",
    "KnowledgeError",
    "build_knowledge_base",
    "load_kb_pages",
    "load_knowledge",
    "Answer",
    "Citation",
    "GateDecision",
    "HandoffTicket",
    "KbPage",
    "Query",
    "RateLimitDecision",
    "RetrievalResult",
    "RetrievedDoc",
    "TurnOutcome",
    "RateLimiter",
    "RateLimitConfigError",
    "SyntheticSecondsClock",
    "Retriever",
    "TfidfIndex",
    "tokenize",
    "SyntheticClock",
    "TraceRecorder",
]

__version__ = "1.0.0"
