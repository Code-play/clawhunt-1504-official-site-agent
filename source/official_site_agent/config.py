"""配置加载与校验。

配置全部是 JSON（不是 YAML），刻意如此：本交付物只用 Python 标准库，
「一键复跑」不需要装任何第三方包。改配置即可换知识源/阈值/路由，无需改代码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class Thresholds:
    """检索与门控阈值（来自 config/thresholds.json）。"""

    top_k: int
    min_term_len: int
    min_relevance: float
    answer_confidence: float
    min_margin: float
    require_citation: bool
    rate_limit: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeSources:
    """授权知识源白名单（来自 config/knowledge_sources.json）。"""

    allowed_hosts: tuple[dict[str, Any], ...]
    denied_hosts: tuple[dict[str, Any], ...]
    require_authorized_flag: bool
    require_https: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HandoffRouting:
    """转人工路由（来自 config/handoff_routing.json）。"""

    routes: tuple[dict[str, Any], ...]
    tag_routes: tuple[dict[str, Any], ...]
    default_route: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置顶层必须是对象: {path}")
    return data


def _require_number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{name} 必须是数字，实际为 {value!r}")
    return float(value)


def load_thresholds(path: Path) -> Thresholds:
    """读阈值配置并做一致性校验。"""
    data = _read_json(path)
    retrieval = data.get("retrieval")
    gate = data.get("gate")
    limit = data.get("rate_limit")
    if not isinstance(retrieval, dict):
        raise ConfigError(f"缺少 retrieval 段: {path}")
    if not isinstance(gate, dict):
        raise ConfigError(f"缺少 gate 段: {path}")
    if not isinstance(limit, dict):
        raise ConfigError(f"缺少 rate_limit 段: {path}")

    top_k = int(retrieval.get("top_k", 5))
    if top_k < 1:
        raise ConfigError("retrieval.top_k 必须 >= 1")
    min_term_len = int(retrieval.get("min_term_len", 2))
    if min_term_len < 1:
        raise ConfigError("retrieval.min_term_len 必须 >= 1")

    min_relevance = _require_number(gate.get("min_relevance"), "gate.min_relevance")
    answer_confidence = _require_number(gate.get("answer_confidence"), "gate.answer_confidence")
    min_margin = _require_number(gate.get("min_margin", 0.0), "gate.min_margin")
    if not (0.0 <= min_relevance < 1.0):
        raise ConfigError("gate.min_relevance 必须落在 [0, 1)")
    if not (0.0 < answer_confidence <= 1.0):
        raise ConfigError("gate.answer_confidence 必须落在 (0, 1]")
    if answer_confidence <= min_relevance:
        raise ConfigError("gate.answer_confidence 必须严格大于 gate.min_relevance")
    if min_margin < 0.0:
        raise ConfigError("gate.min_margin 不能为负")

    if "enabled" not in limit:
        raise ConfigError("rate_limit.enabled 缺失")
    window = int(limit.get("window_seconds", 60))
    max_requests = int(limit.get("max_requests", 10))
    if window < 1:
        raise ConfigError("rate_limit.window_seconds 必须 >= 1")
    if max_requests < 1:
        raise ConfigError("rate_limit.max_requests 必须 >= 1")

    return Thresholds(
        top_k=top_k,
        min_term_len=min_term_len,
        min_relevance=min_relevance,
        answer_confidence=answer_confidence,
        min_margin=min_margin,
        require_citation=bool(gate.get("require_citation", True)),
        rate_limit=dict(limit),
        raw=dict(data),
    )


def load_knowledge_sources(path: Path) -> KnowledgeSources:
    """读授权知识源白名单并校验。"""
    data = _read_json(path)
    allowed = data.get("allowed_hosts")
    if not isinstance(allowed, list) or not allowed:
        raise ConfigError(f"allowed_hosts 必须是非空数组: {path}")
    seen: set[str] = set()
    for entry in allowed:
        host = str(entry.get("host", "")).strip().lower()
        if not host:
            raise ConfigError(f"allowed_hosts 条目缺少 host: {entry}")
        if host in seen:
            raise ConfigError(f"allowed_hosts 主机重复: {host}")
        seen.add(host)
        if entry.get("authorized") is not True:
            raise ConfigError(f"白名单主机必须显式 authorized=true: {host}")

    denied = data.get("denied_hosts", [])
    if not isinstance(denied, list):
        raise ConfigError("denied_hosts 必须是数组")

    rules = data.get("rules", {}) or {}
    return KnowledgeSources(
        allowed_hosts=tuple(dict(x) for x in allowed),
        denied_hosts=tuple(dict(x) for x in denied),
        require_authorized_flag=bool(rules.get("require_authorized_flag", True)),
        require_https=bool(rules.get("require_https", True)),
        raw=dict(data),
    )


def load_handoff_routing(path: Path) -> HandoffRouting:
    """读转人工路由并校验。"""
    data = _read_json(path)
    routes = data.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ConfigError(f"routes 必须是非空数组: {path}")
    branches_covered: set[str] = set()
    for route in routes:
        for key in ("id", "match", "queue", "sla_hours"):
            if key not in route:
                raise ConfigError(f"路由缺少字段 {key}: {route}")
        branch = str(route["match"].get("branch", ""))
        if not branch:
            raise ConfigError(f"路由 match.branch 不能为空: {route}")
        if branch in branches_covered:
            raise ConfigError(f"分支 {branch} 被多条路由覆盖")
        branches_covered.add(branch)
        if int(route["sla_hours"]) < 1:
            raise ConfigError(f"路由 sla_hours 必须 >= 1: {route['id']}")

    default = data.get("default_route")
    if not isinstance(default, dict):
        raise ConfigError(f"缺少 default_route: {path}")
    for key in ("id", "queue", "sla_hours"):
        if key not in default:
            raise ConfigError(f"default_route 缺少字段 {key}")

    tag_routes = data.get("tag_routes", [])
    if not isinstance(tag_routes, list):
        raise ConfigError("tag_routes 必须是数组")

    return HandoffRouting(
        routes=tuple(dict(x) for x in routes),
        tag_routes=tuple(dict(x) for x in tag_routes),
        default_route=dict(default),
        raw=dict(data),
    )


def load_config_dir(config_dir: Path) -> tuple[Thresholds, KnowledgeSources, HandoffRouting]:
    """一次读入整个 config/ 目录。"""
    config_dir = Path(config_dir)
    thresholds = load_thresholds(config_dir / "thresholds.json")
    sources = load_knowledge_sources(config_dir / "knowledge_sources.json")
    routing = load_handoff_routing(config_dir / "handoff_routing.json")
    return thresholds, sources, routing
