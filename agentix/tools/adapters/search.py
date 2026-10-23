from typing import Dict, List, Optional
import re

import httpx
from agentix.config import settings
from agentix.infrastructure.core import get_logger
from agentix.tools.gateway.result_limiter import limit_value

logger = get_logger(service="tool.search")

#: httpx 的异常文本会原样带出请求 URL（含 query string），SerpAPI 的密钥就在里面。
#: \b 是必要的：少了它，``monkey=1`` 会被当成 ``key=1`` 一起打掉。
_SECRET_QUERY_RE = re.compile(
    r"(?i)(\b(?:api_key|apikey|access_token|secret_key|secret|token|password|key)=)"
    r"[^&\s'\"]+"
)
_SECRET_HEADER_RE = re.compile(
    r"(?i)((?:api[_-]?key|secret|token|password)\s*[:=]\s*)([^\s,;'\"]{3,})"
)
_SECRET_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;'\"]{3,}")


def redact_secrets(text: str) -> str:
    """把日志文本里的凭据值替换成 ``***``，保留键名以便定位是哪个参数出问题。"""
    masked = _SECRET_BEARER_RE.sub(lambda m: m.group(1) + "***", text)
    masked = _SECRET_QUERY_RE.sub(lambda m: m.group(1) + "***", masked)
    masked = _SECRET_HEADER_RE.sub(lambda m: m.group(1) + "***", masked)
    own_key = getattr(settings, "SERPAPI_KEY", "")
    if isinstance(own_key, str) and len(own_key) >= 4:
        masked = masked.replace(own_key, "***")
    return masked


class SearchTool:
    """Wrapper around SerpAPI Google search results."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: Optional[float] = None,
        default_results: Optional[int] = None,
    ) -> None:
        self.allow_network = settings.ENABLE_EXTERNAL_SEARCH
        self.api_key = api_key or settings.SERPAPI_KEY
        if self.allow_network and not self.api_key:
            raise RuntimeError("SERPAPI_KEY is not configured.")
        if not self.allow_network:
            logger.info("External search disabled; SearchTool will operate in no-op mode.")

        self.endpoint = (endpoint or settings.SERPAPI_BASE_URL).rstrip("/")
        self.timeout = timeout or settings.SERPAPI_TIMEOUT
        self.default_results = default_results or settings.SEARCH_RESULT_COUNT

    def search(self, query: str, *, num_results: Optional[int] = None) -> List[Dict]:
        """执行搜索并返回结构化结果"""
        if not query:
            raise ValueError("Search query must not be empty.")

        if not self.allow_network:
            logger.info("External search disabled via configuration; returning empty result set.")
            return []

        result_count = num_results or self.default_results

        params = {
            "engine": "google",
            "q": query,
            "api_key": self.api_key,
            "num": result_count,
            "hl": "zh-CN",
            "gl": "cn",
        }

        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.get(self.endpoint, params=params)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Search request failed: {}", redact_secrets(str(exc)))
            return []

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("Failed to decode search response: {}", redact_secrets(str(exc)))
            return []

        return self._bounded(self._parse_results(payload, limit=result_count))

    def _bounded(self, results: List[Dict]) -> List[Dict]:
        """B 组：下游结果限量（行数 + 字符数）。

        ``num_results`` 只约束**请求**条数，SerpAPI 返回的 snippet 仍可能很长；
        统一经 :func:`limit_value` 施加 ``TOOL_RESULT_MAX_ROWS`` /
        ``TOOL_RESULT_MAX_CHARS`` 上限，避免大文本直接进 state 击穿上下文窗口。

        截断时记 warning（**不静默**）——静默截断是幻觉的温床。
        """
        limited = limit_value(
            results,
            max_rows=int(getattr(settings, "TOOL_RESULT_MAX_ROWS", 100) or 0),
            max_chars=int(getattr(settings, "TOOL_RESULT_MAX_CHARS", 8000) or 0),
        )
        if limited.meta.truncated or limited.meta.has_more:
            logger.warning(
                "Search results limited: total={} returned={} truncated_chars={}",
                limited.meta.total_rows,
                limited.meta.returned_rows,
                limited.meta.truncated_chars,
            )
        page = limited.value
        return page if isinstance(page, list) else results

    def _parse_results(self, data: Dict, *, limit: int) -> List[Dict]:
        """Normalize SerpAPI payload."""
        organic_results = data.get("organic_results") or []
        results: List[Dict] = []

        for item in organic_results:
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
            )
            if len(results) >= limit:
                break

        return results
