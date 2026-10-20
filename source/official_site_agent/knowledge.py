"""授权知识源白名单与知识加载。

检索**只能**命中授权的公开知识子集，因此这里做了两道闸门：

1. 条目自带的 ``authorized`` 必须为真；
2. 条目 url 的 host 必须命中 ``config/knowledge_sources.json`` 的 allowed_hosts，
   且不得命中 denied_hosts，且协议必须是 https（可配置）。

不满足任一条的条目会被排除，并记录排除原因，便于审计「为什么这条资料不能用于作答」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import KnowledgeSources
from .model import KbPage


class KnowledgeError(RuntimeError):
    """知识库加载或授权校验失败。"""


@dataclass
class ExcludedPage:
    page_id: str
    title: str
    url: str
    reason: str


@dataclass
class KnowledgeBase:
    """已通过授权校验的知识库。检索只允许从这里取数。"""

    pages: tuple[KbPage, ...]
    excluded: tuple[ExcludedPage, ...] = ()
    source_config: KnowledgeSources | None = None
    stats: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_id = {p.page_id: p for p in self.pages}
        if len(self._by_id) != len(self.pages):
            raise KnowledgeError("知识条目 page_id 存在重复")
        if not self.pages:
            raise KnowledgeError("没有任何已授权知识条目，检索无法工作")

    @property
    def page_ids(self) -> tuple[str, ...]:
        return tuple(p.page_id for p in self.pages)

    def get(self, page_id: str) -> KbPage:
        try:
            return self._by_id[page_id]
        except KeyError as exc:
            raise KnowledgeError(f"未知知识条目: {page_id}（只允许访问授权子集）") from exc

    def contains(self, page_id: str) -> bool:
        return page_id in self._by_id


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _scheme_of(url: str) -> str:
    try:
        return (urlparse(url).scheme or "").lower()
    except ValueError:
        return ""


def _check_page(page: KbPage, sources: KnowledgeSources) -> tuple[bool, str]:
    """返回 (是否授权, 原因)。"""
    if sources.require_authorized_flag and not page.authorized:
        return False, "条目 authorized=false（资料未获授权对外）"

    if not page.url:
        return False, "条目缺少 url，无法核验来源"

    if sources.require_https and _scheme_of(page.url) != "https":
        return False, f"url 协议不是 https: {page.url}"

    host = _host_of(page.url)
    if not host:
        return False, f"url 无法解析出 host: {page.url}"

    for denied in sources.denied_hosts:
        if host == str(denied.get("host", "")).lower():
            return False, f"host {host} 在 denied_hosts（{denied.get('reason', '未授权')}）"

    for entry in sources.allowed_hosts:
        if host != str(entry.get("host", "")).lower():
            continue
        prefix = str(entry.get("path_prefix", "") or "")
        path = urlparse(page.url).path or ""
        if prefix and not path.startswith(prefix):
            return False, f"path {path} 不在授权前缀 {prefix} 内"
        return True, f"host {host} 命中白名单{prefix or ''}"

    return False, f"host {host} 不在 allowed_hosts 白名单"


def load_kb_pages(path: Path) -> list[KbPage]:
    """从 JSONL 读取全部知识条目（尚未做授权过滤）。"""
    if not path.is_file():
        raise KnowledgeError(f"知识条目文件不存在: {path}")
    pages: list[KbPage] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            pages.append(KbPage.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError) as exc:
            raise KnowledgeError(f"{path}:{lineno} 知识条目不合法: {exc}") from exc
    if not pages:
        raise KnowledgeError(f"{path} 里没有任何知识条目")
    return pages


def build_knowledge_base(pages: list[KbPage], sources: KnowledgeSources) -> KnowledgeBase:
    """按白名单过滤，得到检索真正可用的授权子集。"""
    allowed: list[KbPage] = []
    excluded: list[ExcludedPage] = []
    for page in pages:
        ok, reason = _check_page(page, sources)
        if ok:
            allowed.append(page)
        else:
            excluded.append(ExcludedPage(page.page_id, page.title, page.url, reason))

    stats = {
        "total": len(pages),
        "authorized": len(allowed),
        "excluded": len(excluded),
    }
    return KnowledgeBase(
        pages=tuple(allowed),
        excluded=tuple(excluded),
        source_config=sources,
        stats=stats,
    )


def load_knowledge(path: Path, sources: KnowledgeSources) -> KnowledgeBase:
    """读取并过滤，一步到位。"""
    return build_knowledge_base(load_kb_pages(path), sources)
