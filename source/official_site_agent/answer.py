"""答案生成（仅在门控放行之后执行）。

本单不调用任何大模型：生成是**抽取式**的——从命中条目原文里挑出与问题重叠
最多的一句话，并附上来源条目 id。这样做的好处是：

* 完全确定性，可逐字节复跑；
* 每一句答案都能追溯到具体知识条目（满足买家「来源条目 id 可追溯」要求）；
* 生成入口处强制校验门控结果，低置信度不可能走到这里。
"""

from __future__ import annotations

import re

from .gate import assert_allowed
from .model import Answer, Citation, GateDecision, RetrievalResult
from .retrieval import tokenize

_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def _compose_body(text: str, query: str, min_term_len: int = 2, max_chars: int = 200) -> str:
    """按原文顺序拼接所有与问题有词重叠的句子，得到完整且可核对的答案。

    只挑「最像的一句」容易漏掉条件或例外（例如浏览器页的最后一句是「IE 不再支持」），
    因此这里保留全部相关句子；若一句都不相关则退回首句。
    """
    q_terms = set(tokenize(query, min_term_len))
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]
    if not sentences:
        return text.strip()

    chosen: list[str] = []
    total = 0
    for sentence in sentences:
        terms = set(tokenize(sentence, min_term_len))
        if not (terms & q_terms):
            continue
        if chosen and total + len(sentence) > max_chars:
            break
        chosen.append(sentence)
        total += len(sentence)

    return "".join(chosen) if chosen else sentences[0]


def compose_answer(
    result: RetrievalResult,
    decision: GateDecision,
    session_id: str,
    min_term_len: int = 2,
) -> Answer:
    """在门控放行后生成带引用的答案。未放行直接抛 GenerationBlocked。"""
    assert_allowed(decision)
    top = result.top_doc
    if top is None:  # pragma: no cover - 门控已保证有候选
        raise AssertionError("门控放行但没有检索候选，属于逻辑矛盾")

    body = _compose_body(top.context or top.snippet or "", result.query_text, min_term_len)
    if not body:
        body = top.snippet

    citations: list[Citation] = []
    for doc in result.docs:
        # 只把达到作答阈值（或与最高分持平）的候选列为引用，避免堆砌低相关来源。
        if doc.score >= decision.answer_threshold or doc.page_id == top.page_id:
            citations.append(Citation(page_id=doc.page_id, title=doc.title, url=doc.url, score=doc.score))

    citation_ids = "、".join(c.page_id for c in citations)
    text = f"根据《{top.title}》：{body}（来源：{citation_ids}）"

    return Answer(
        query_id=result.query_id,
        session_id=session_id,
        question=result.query_text,
        branch=decision.branch,
        text=text,
        citations=tuple(citations),
        confidence=decision.confidence,
        generated=True,
    )
