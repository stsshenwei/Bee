from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.agent.agent_runtime_tools import HTTPJSONSearchProvider, TavilySearchProvider
from app.services.infrastructure.logging_config import truncate_text


WEB_FALLBACK_NOTICE = "知识库无答案，以下来自网络搜索"


class WebSearchProvider(Protocol):
    def search(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class WebSearchFallbackConfig:
    enabled: bool = False
    endpoint: str = ""
    tavily_api_key: str = ""
    tavily_endpoint: str = "https://api.tavily.com/search"
    top_k: int = 5
    timeout_seconds: float = 5.0
    min_score: float = 0.15

    @classmethod
    def from_env(cls) -> "WebSearchFallbackConfig":
        enabled = _env_bool("WEB_SEARCH_FALLBACK_ENABLED", default=_env_bool("AGENT_RUNTIME_WEB_SEARCH_ENABLED", default=False))
        endpoint = _env("WEB_SEARCH_FALLBACK_URL", "AGENT_RUNTIME_WEB_SEARCH_URL", default="")
        return cls(
            enabled=enabled,
            endpoint=endpoint,
            tavily_api_key=_env("TAVILY_API_KEY", default=""),
            tavily_endpoint=_env("TAVILY_SEARCH_URL", default="https://api.tavily.com/search"),
            top_k=_env_int("WEB_SEARCH_FALLBACK_TOP_K", default=5),
            timeout_seconds=_env_float("WEB_SEARCH_FALLBACK_TIMEOUT_SECONDS", "AGENT_RUNTIME_TOOL_TIMEOUT_SECONDS", default=5.0),
            min_score=_env_float("WEB_SEARCH_FALLBACK_MIN_CONFIDENCE", "MIN_RELEVANCE_SCORE", default=0.15),
        )


@dataclass(frozen=True)
class EvidenceSufficiencyConfig:
    min_score: float = 0.15


@dataclass(frozen=True)
class EvidenceSufficiency:
    sufficient: bool
    reason: str
    best_score: float = 0.0
    hit_count: int = 0
    source_count: int = 0


@dataclass(frozen=True)
class WebSearchFallbackResult:
    attempted: bool
    used: bool
    notice: str = WEB_FALLBACK_NOTICE
    answer_context: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class WebSearchFallbackService:
    def __init__(
        self,
        *,
        provider: WebSearchProvider | None = None,
        config: WebSearchFallbackConfig | None = None,
    ):
        self.config = config or WebSearchFallbackConfig.from_env()
        self.provider = provider or self._provider_from_config(self.config)

    def search(self, query: str, *, trigger_reason: str, mode: str) -> WebSearchFallbackResult:
        metadata = self._base_metadata(mode=mode, trigger_reason=trigger_reason)
        if not self.config.enabled or self.provider is None:
            metadata.update({"available": False, "error": "web_search_unavailable"})
            return WebSearchFallbackResult(
                attempted=True,
                used=False,
                answer_context="知识库未检索到足够答案，且网络搜索不可用或未配置。",
                metadata=metadata,
            )

        try:
            raw_results = self.provider.search(query, top_k=self.config.top_k)
        except Exception as exc:
            metadata.update(
                {
                    "available": True,
                    "error": "web_search_failed",
                    "error_type": exc.__class__.__name__,
                    "result_count": 0,
                }
            )
            return WebSearchFallbackResult(
                attempted=True,
                used=False,
                answer_context="知识库未检索到足够答案，且网络搜索失败，无法生成可靠答案。",
                metadata=metadata,
            )

        sources = self._normalize_sources(raw_results)
        metadata.update({"available": True, "result_count": len(sources)})
        if not sources:
            metadata.update({"used": False, "error": "no_web_results"})
            return WebSearchFallbackResult(
                attempted=True,
                used=False,
                answer_context="未找到可用的知识库或网络搜索证据，无法生成可靠答案。",
                sources=[],
                metadata=metadata,
            )

        metadata.update({"used": True, "error": ""})
        return WebSearchFallbackResult(
            attempted=True,
            used=True,
            sources=sources,
            answer_context=self._answer_context(sources),
            metadata=metadata,
        )

    @staticmethod
    def _provider_from_config(config: WebSearchFallbackConfig) -> WebSearchProvider | None:
        if not config.endpoint:
            if config.tavily_api_key:
                return TavilySearchProvider(
                    config.tavily_api_key,
                    timeout_seconds=config.timeout_seconds,
                    max_results=config.top_k,
                    endpoint=config.tavily_endpoint,
                )
            return None
        return HTTPJSONSearchProvider(config.endpoint, timeout_seconds=config.timeout_seconds, max_results=config.top_k)

    @staticmethod
    def _base_metadata(*, mode: str, trigger_reason: str) -> dict[str, Any]:
        return {
            "attempted": True,
            "used": False,
            "available": False,
            "mode": str(mode or ""),
            "trigger_reason": str(trigger_reason or ""),
            "result_count": 0,
            "error": "",
        }

    @staticmethod
    def _normalize_sources(results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in results or []:
            if not isinstance(item, dict):
                continue
            title = truncate_text(str(item.get("title") or item.get("url") or "Web result").strip(), 160)
            url = truncate_text(str(item.get("url") or item.get("link") or "").strip(), 500)
            snippet = truncate_text(str(item.get("snippet") or item.get("content") or "").strip(), 500)
            key = url or f"{title}:{snippet}"
            if not title and not url and not snippet:
                continue
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                {
                    "source": title or url or "Web result",
                    "source_type": "web",
                    "url": url,
                    "snippet": snippet,
                    "score": float(item.get("score", 0.0) or 0.0),
                    "provider": "web_search",
                }
            )
        return sources

    @staticmethod
    def _answer_context(sources: list[dict[str, Any]], *, include_notice: bool = True) -> str:
        lines = []
        if include_notice:
            lines.extend([WEB_FALLBACK_NOTICE, ""])
        lines.append("网络搜索结果:")
        for index, source in enumerate(sources, start=1):
            title = source.get("source") or "Web result"
            url = source.get("url") or ""
            snippet = source.get("snippet") or ""
            line = f"{index}. {title}"
            if url:
                line = f"{line} ({url})"
            if snippet:
                line = f"{line}\n   {snippet}"
            lines.append(line)
        return "\n".join(lines)


def assess_evidence_sufficiency(
    hits: list[dict[str, Any]] | None,
    sources: list[dict[str, Any]] | None,
    *,
    config: EvidenceSufficiencyConfig | None = None,
) -> EvidenceSufficiency:
    config = config or EvidenceSufficiencyConfig()
    hits = list(hits or [])
    sources = list(sources or [])
    if not hits:
        return EvidenceSufficiency(False, "no_internal_hits", hit_count=0, source_count=len(sources))
    if not sources:
        return EvidenceSufficiency(False, "no_internal_sources", hit_count=len(hits), source_count=0)
    best_score = max([_hit_score(hit) for hit in hits] + [_source_score(source) for source in sources] + [0.0])
    if best_score < float(config.min_score):
        return EvidenceSufficiency(False, "low_confidence", best_score=best_score, hit_count=len(hits), source_count=len(sources))
    return EvidenceSufficiency(True, "sufficient", best_score=best_score, hit_count=len(hits), source_count=len(sources))


def _hit_score(hit: dict[str, Any]) -> float:
    for key in ("hybrid_score", "reranker_score", "vector_score", "keyword_score", "score"):
        try:
            return float(hit.get(key))
        except (TypeError, ValueError):
            continue
    try:
        return max(0.0, 1.0 - float(hit.get("distance", 1.0)))
    except (TypeError, ValueError):
        return 0.0


def _source_score(source: dict[str, Any]) -> float:
    try:
        return float(source.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = _env(name, default="true" if default else "false").lower()
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int) -> int:
    try:
        return int(_env(name, default=str(default)))
    except ValueError:
        return default


def _env_float(*names: str, default: float) -> float:
    try:
        return float(_env(*names, default=str(default)))
    except ValueError:
        return default
