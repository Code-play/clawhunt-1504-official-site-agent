"""纯标准库检索：中文分词（CJK 二元组）+ 英文词 + TF-IDF + 余弦相似度。

设计约束（来自订单）：
* 只用 Python 标准库，不联网、不依赖 numpy/requests；
* 结果必须确定性：同样的知识库 + 同样的问题，分数逐位相同；
* 只允许在**授权子集**上建索引（由 knowledge.KnowledgeBase 保证）。

分词策略
--------
* 英文/数字：按 ``[a-z0-9][a-z0-9._-]*`` 取词并小写；
* 中文：连续汉字串取**二元组**（单字串保留单字），不依赖任何词典。

打分策略
--------
TF 采用 ``1 + log(tf)``，IDF 采用 ``log((N+1)/(df+1)) + 1``，向量做 L2 归一化，
得分即余弦相似度，落在 [0, 1]，便于直接与置信度阈值比较。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from .knowledge import KnowledgeBase
from .model import KbPage, Query, RetrievalResult, RetrievedDoc

_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def tokenize(text: str, min_term_len: int = 2) -> list[str]:
    """把文本切成检索词。确定性、无状态。"""
    lowered = text.lower()
    tokens: list[str] = []

    for match in _ASCII_RE.finditer(lowered):
        word = match.group(0).strip("._-")
        if len(word) >= min_term_len:
            tokens.append(word)

    for match in _CJK_RE.finditer(lowered):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
            continue
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])

    return tokens


def _l2_normalize(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm <= 0.0:
        return {}
    return {k: v / norm for k, v in vec.items()}


@dataclass
class _DocVector:
    page: KbPage
    vector: dict[str, float]
    tokens: Counter = field(default_factory=Counter)


class TfidfIndex:
    """基于 TF-IDF 的确定性倒排索引（实际上用正排向量，规模小足够）。"""

    def __init__(self, pages: Iterable[KbPage], min_term_len: int = 2) -> None:
        self.min_term_len = min_term_len
        self.pages: list[KbPage] = list(pages)
        if not self.pages:
            raise ValueError("索引不能建立在空知识库上")

        tokenized: list[tuple[KbPage, list[str]]] = [
            (p, tokenize(f"{p.title} {p.text} {' '.join(p.tags)}", min_term_len)) for p in self.pages
        ]

        df: Counter = Counter()
        for _page, toks in tokenized:
            for term in set(toks):
                df[term] += 1
        total = len(tokenized)
        self.idf: dict[str, float] = {
            term: math.log((total + 1) / (count + 1)) + 1.0 for term, count in df.items()
        }

        self._docs: list[_DocVector] = []
        for page, toks in tokenized:
            tf = Counter(toks)
            raw = {term: (1.0 + math.log(c)) * self.idf.get(term, 0.0) for term, c in tf.items()}
            self._docs.append(_DocVector(page=page, vector=_l2_normalize(raw), tokens=tf))

    # ---- 查询 -------------------------------------------------------------
    def _query_vector(self, text: str) -> tuple[dict[str, float], Counter]:
        toks = tokenize(text, self.min_term_len)
        tf = Counter(toks)
        raw = {
            term: (1.0 + math.log(c)) * self.idf[term]
            for term, c in tf.items()
            if term in self.idf
        }
        return _l2_normalize(raw), tf

    @staticmethod
    def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(term, 0.0) for term, v in a.items())

    def search(self, query: Query, top_k: int = 5) -> RetrievalResult:
        qvec, qtf = self._query_vector(query.text)
        scored: list[tuple[float, _DocVector, tuple[str, ...]]] = []

        for doc in self._docs:
            score = self._cosine(qvec, doc.vector)
            if score <= 0.0:
                continue
            matched = tuple(term for term in qvec if term in doc.vector)
            matched = tuple(sorted(matched, key=lambda t: (-qvec[t], t)))
            scored.append((score, doc, matched))

        scored.sort(key=lambda item: (-item[0], item[1].page.page_id))

        docs: list[RetrievedDoc] = []
        for score, doc, matched in scored[: max(1, top_k)]:
            docs.append(
                RetrievedDoc(
                    page_id=doc.page.page_id,
                    title=doc.page.title,
                    url=doc.page.url,
                    score=round(score, 6),
                    matched_terms=matched[:8],
                    snippet=_best_snippet(doc.page.text, set(matched)),
                    context=doc.page.text,
                )
            )

        return RetrievalResult(
            query_id=query.qid,
            query_text=query.text,
            docs=tuple(docs),
            indexed_pages=len(self.pages),
        )


def _best_snippet(text: str, matched_terms: set[str]) -> str:
    """挑一句与命中词重叠最多的原文句子，作为可核对的证据片段。"""
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]
    if not sentences:
        return text[:120]

    best = sentences[0]
    best_hits = -1
    for sentence in sentences:
        toks = set(tokenize(sentence))
        hits = len(toks & matched_terms)
        if hits > best_hits:
            best_hits = hits
            best = sentence
    return best


class Retriever:
    """对外检索入口：绑定一个授权知识库，保证不会触及未授权条目。"""

    def __init__(self, kb: KnowledgeBase, min_term_len: int = 2, top_k: int = 5) -> None:
        self.kb = kb
        self.top_k = top_k
        self.index = TfidfIndex(kb.pages, min_term_len=min_term_len)

    def search(self, query: Query) -> RetrievalResult:
        result = self.index.search(query, top_k=self.top_k)
        # 防御性断言：检索结果必须全部落在授权子集内。
        for doc in result.docs:
            if not self.kb.contains(doc.page_id):
                raise AssertionError(f"检索命中了未授权条目 {doc.page_id}")
        return result
