"""置信度门 + 三分支决策。

这是本单最关键的一块：**阈值判断必须发生在答案生成之前**。

流程固定为：

    检索 -> 门控(decide) -> [allowed?] -> 生成(compose) / 转人工(handoff)

``decide`` 只读取检索结果与阈值，不调用任何生成逻辑；生成模块 ``compose``
在入口处再次校验 ``GateDecision.allowed``，为假直接抛错。因此「低置信度硬答」
在代码层面无法发生，而不是靠提示词约束。

三条分支
--------
* ``answered``               命中且置信度 >= answer_confidence -> 生成带引用的答案
* ``handoff_low_confidence`` 有相关候选但置信度介于两阈值之间 -> 转人工（专家队列）
* ``handoff_no_hit``         最高分低于 min_relevance（含零候选）-> 转人工（一线队列）
"""

from __future__ import annotations

from .model import GateDecision, RetrievalResult

BRANCH_ANSWERED = "answered"
BRANCH_NO_HIT = "handoff_no_hit"
BRANCH_LOW_CONFIDENCE = "handoff_low_confidence"


class GenerationBlocked(RuntimeError):
    """门控未放行却试图生成答案。"""


def decide(
    result: RetrievalResult,
    min_relevance: float,
    answer_confidence: float,
    min_margin: float = 0.0,
) -> GateDecision:
    """纯函数门控：只看检索结果与阈值，绝不提前生成。"""
    confidence = result.top_score
    top = result.top_doc

    if top is None or confidence < min_relevance:
        return GateDecision(
            branch=BRANCH_NO_HIT,
            confidence=confidence,
            answer_threshold=answer_confidence,
            min_relevance=min_relevance,
            allowed=False,
            reason=(
                f"最高检索分 {confidence:.4f} < min_relevance {min_relevance:.4f}"
                f"（候选 {len(result.docs)} 条）→ 判定为未知问题，转人工"
            ),
        )

    if confidence < answer_confidence:
        return GateDecision(
            branch=BRANCH_LOW_CONFIDENCE,
            confidence=confidence,
            answer_threshold=answer_confidence,
            min_relevance=min_relevance,
            allowed=False,
            reason=(
                f"min_relevance {min_relevance:.4f} <= 最高检索分 {confidence:.4f}"
                f" < answer_confidence {answer_confidence:.4f} → 证据不足，转人工"
            ),
        )

    if min_margin > 0.0 and len(result.docs) >= 2:
        margin = result.docs[0].score - result.docs[1].score
        if margin < min_margin:
            return GateDecision(
                branch=BRANCH_LOW_CONFIDENCE,
                confidence=confidence,
                answer_threshold=answer_confidence,
                min_relevance=min_relevance,
                allowed=False,
                reason=(
                    f"最高分 {result.docs[0].score:.4f} 与次高分 {result.docs[1].score:.4f}"
                    f" 差距 {margin:.4f} < min_margin {min_margin:.4f} → 存在歧义，转人工"
                ),
            )

    return GateDecision(
        branch=BRANCH_ANSWERED,
        confidence=confidence,
        answer_threshold=answer_confidence,
        min_relevance=min_relevance,
        allowed=True,
        reason=(
            f"最高检索分 {confidence:.4f} >= answer_confidence {answer_confidence:.4f}"
            f" → 允许生成（来源 {top.page_id}）"
        ),
    )


def assert_allowed(decision: GateDecision) -> None:
    """生成前的硬校验。未放行就生成 = 逻辑错误，直接抛。"""
    if not decision.allowed:
        raise GenerationBlocked(
            f"门控未放行（branch={decision.branch}, confidence={decision.confidence:.4f}），禁止生成答案"
        )
