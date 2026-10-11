"""
Observability module for Agent and RAG pipeline monitoring.

Tracks:
- LLM call metrics (token consumption, latency, success rate)
- Route distribution (which query types are most common)
- RAG retrieval metrics (recall count, reranker scores)
- End-to-end request metrics (total latency, error rate)
- Tool execution metrics (per-tool latency and success rate)

All metrics are stored in Redis with TTL for time-windowed aggregation.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from redis.asyncio import Redis

from agentix.config import settings


@dataclass
class LLMCallMetrics:
    """Metrics for a single LLM API call."""
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class RequestMetrics:
    """Metrics for a single user request."""
    request_id: str
    session_id: str
    route_type: str = ""
    total_latency_ms: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tools_used: List[str] = field(default_factory=list)
    # P2-2：按配置单价估算的成本（默认单价 0.0 → 诚实值 0.0，不假装有价格）
    estimated_cost: float = 0.0
    success: bool = True
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class ObservabilityService:
    """Central observability service for tracking all system metrics."""

    def __init__(
        self,
        *,
        redis_client: Optional[Redis] = None,
        redis_url: Optional[str] = None,
        prefix: str = "metrics",
        ttl: int = 86400 * 7,
    ):
        self._redis = redis_client or Redis.from_url(
            redis_url or settings.REDIS_URL,
            decode_responses=True,
        )
        self.prefix = prefix
        self.ttl = ttl

    async def record_llm_call(self, metrics: LLMCallMetrics) -> None:
        """Record a single LLM API call."""
        key = f"{self.prefix}:llm_calls"
        entry = {
            "model": metrics.model,
            "prompt_tokens": metrics.prompt_tokens,
            "completion_tokens": metrics.completion_tokens,
            "total_tokens": metrics.total_tokens,
            "latency_ms": metrics.latency_ms,
            "success": metrics.success,
            "error": metrics.error,
            "tags": metrics.tags,
            "timestamp": metrics.timestamp,
        }
        await self._redis.rpush(key, json.dumps(entry))
        await self._redis.ltrim(key, -10000, -1)
        await self._redis.expire(key, self.ttl)

        await self._redis.hincrby(f"{self.prefix}:token_total", "total", metrics.total_tokens)
        await self._redis.hincrby(f"{self.prefix}:token_total", "prompt", metrics.prompt_tokens)
        await self._redis.hincrby(f"{self.prefix}:token_total", "completion", metrics.completion_tokens)

        if metrics.success:
            await self._redis.hincrby(f"{self.prefix}:llm_stats", "success", 1)
        else:
            await self._redis.hincrby(f"{self.prefix}:llm_stats", "failure", 1)

    async def record_request(self, metrics: RequestMetrics) -> None:
        """Record a complete user request."""
        key = f"{self.prefix}:requests"
        entry = {
            "request_id": metrics.request_id,
            "session_id": metrics.session_id,
            "route_type": metrics.route_type,
            "total_latency_ms": metrics.total_latency_ms,
            "llm_calls": metrics.llm_calls,
            "prompt_tokens": metrics.prompt_tokens,
            "completion_tokens": metrics.completion_tokens,
            "total_tokens": metrics.total_tokens,
            "tools_used": metrics.tools_used,
            "estimated_cost": metrics.estimated_cost,
            "success": metrics.success,
            "error": metrics.error,
            "timestamp": metrics.timestamp,
        }
        await self._redis.rpush(key, json.dumps(entry))
        await self._redis.ltrim(key, -5000, -1)
        await self._redis.expire(key, self.ttl)

        if metrics.route_type:
            await self._redis.hincrby(f"{self.prefix}:route_dist", metrics.route_type, 1)

        if metrics.success:
            await self._redis.hincrby(f"{self.prefix}:request_stats", "success", 1)
        else:
            await self._redis.hincrby(f"{self.prefix}:request_stats", "failure", 1)

    async def record_tool_execution(
        self,
        tool_name: str,
        latency_ms: float,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Record a tool execution."""
        key = f"{self.prefix}:tool:{tool_name}"
        entry = {
            "latency_ms": latency_ms,
            "success": success,
            "error": error,
            "timestamp": time.time(),
        }
        await self._redis.rpush(key, json.dumps(entry))
        await self._redis.ltrim(key, -1000, -1)
        await self._redis.expire(key, self.ttl)

    async def record_rag_retrieval(
        self,
        source: str,
        result_count: int,
        latency_ms: float,
        avg_score: float = 0.0,
    ) -> None:
        """Record a RAG retrieval operation."""
        key = f"{self.prefix}:rag:{source}"
        entry = {
            "result_count": result_count,
            "latency_ms": latency_ms,
            "avg_score": avg_score,
            "timestamp": time.time(),
        }
        await self._redis.rpush(key, json.dumps(entry))
        await self._redis.ltrim(key, -1000, -1)
        await self._redis.expire(key, self.ttl)

    async def get_dashboard(self) -> Dict[str, Any]:
        """Get aggregated metrics for dashboard display."""
        pipe = self._redis.pipeline()

        pipe.hgetall(f"{self.prefix}:token_total")
        pipe.hgetall(f"{self.prefix}:llm_stats")
        pipe.hgetall(f"{self.prefix}:request_stats")
        pipe.hgetall(f"{self.prefix}:route_dist")
        pipe.llen(f"{self.prefix}:llm_calls")
        pipe.llen(f"{self.prefix}:requests")

        results = await pipe.execute()

        token_total = results[0] or {}
        llm_stats = results[1] or {}
        request_stats = results[2] or {}
        route_dist = results[3] or {}
        llm_call_count = results[4] or 0
        request_count = results[5] or 0

        llm_success = int(llm_stats.get("success", 0))
        llm_failure = int(llm_stats.get("failure", 0))
        llm_total = llm_success + llm_failure

        req_success = int(request_stats.get("success", 0))
        req_failure = int(request_stats.get("failure", 0))
        req_total = req_success + req_failure

        return {
            "tokens": {
                "total": int(token_total.get("total", 0)),
                "prompt": int(token_total.get("prompt", 0)),
                "completion": int(token_total.get("completion", 0)),
            },
            "llm": {
                "total_calls": llm_total,
                "success": llm_success,
                "failure": llm_failure,
                "success_rate": round(llm_success / llm_total * 100, 2) if llm_total > 0 else 0,
                "recent_calls": llm_call_count,
            },
            "requests": {
                "total": req_total,
                "success": req_success,
                "failure": req_failure,
                "success_rate": round(req_success / req_total * 100, 2) if req_total > 0 else 0,
                "recent_requests": request_count,
            },
            "route_distribution": {k: int(v) for k, v in route_dist.items()},
        }

    async def get_recent_llm_calls(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent LLM call records."""
        raw = await self._redis.lrange(f"{self.prefix}:llm_calls", -limit, -1)
        return [json.loads(r) for r in raw]

    async def get_recent_requests(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent request records."""
        raw = await self._redis.lrange(f"{self.prefix}:requests", -limit, -1)
        return [json.loads(r) for r in raw]


_observability_instance: Optional[ObservabilityService] = None


def get_observability() -> ObservabilityService:
    """Get or create the global observability service instance."""
    global _observability_instance
    if _observability_instance is None:
        _observability_instance = ObservabilityService()
    return _observability_instance


def context_report() -> Dict[str, Any]:
    """P3（岗位对照·渐进上下文，借鉴 claw-code 诊断只读契约）。

    上下文子系统的**只读快照**——预算常量、flag 现值、状态栏信任源与阶梯
    轮次。契约（测试把守）：

    - **不发 provider/LLM 请求**、不 import 客户端；
    - **不改动任何状态**（两次调用结果逐字节一致，纯函数）；
    - 只读 ``settings`` 与只读模块常量，纯 JSON 可序列化（供 ``/ops/context``）。
    """
    from agentix.agent.context.compaction import COMPACT_LADDER_ROUNDS
    from agentix.agent.context.engine import STATUS_BAR_TRUSTED_KEYS
    from agentix.agent.context.instructions import (
        DEFAULT_MAX_INSTRUCTION_CHARS,
        PROJECT_INSTRUCTION_FILES,
        TOTAL_INSTRUCTION_CHARS,
    )

    return {
        "contract": {
            "read_only": True,
            "provider_calls": False,
            "mutates_state": False,
        },
        "instructions": {
            "project_files": list(PROJECT_INSTRUCTION_FILES),
            "max_chars_per_file": int(DEFAULT_MAX_INSTRUCTION_CHARS),
            "total_budget_chars": int(TOTAL_INSTRUCTION_CHARS),
        },
        "compaction": {
            "autocompact": bool(getattr(settings, "CONTEXT_AUTOCOMPACT", False)),
            "ladder": bool(getattr(settings, "CONTEXT_COMPACT_LADDER", False)),
            "ladder_rounds": [int(n) for n in COMPACT_LADDER_ROUNDS],
        },
        "loop": {
            "context_budget_tokens": int(
                getattr(settings, "AGENT_CONTEXT_BUDGET_TOKENS", 0) or 0
            ),
            "budget_notice": bool(getattr(settings, "BUDGET_NOTICE_ENABLED", False)),
            "token_budget_per_request": int(
                getattr(settings, "TOKEN_BUDGET_PER_REQUEST", 0) or 0
            ),
        },
        "status_bar_trusted_keys": sorted(STATUS_BAR_TRUSTED_KEYS),
    }


class MetricsTracker:
    """Context manager for tracking request-level metrics."""

    def __init__(self, request_id: str, session_id: str):
        self.request_id = request_id
        self.session_id = session_id
        self.start_time = time.time()
        self.llm_calls = 0
        self.total_tokens = 0
        self.tools_used: List[str] = []
        self.route_type = ""
        self.success = True
        self.error: Optional[str] = None
        self._obs = get_observability()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        latency_ms = (time.time() - self.start_time) * 1000

        if exc_type:
            self.success = False
            self.error = str(exc_val)

        metrics = RequestMetrics(
            request_id=self.request_id,
            session_id=self.session_id,
            route_type=self.route_type,
            total_latency_ms=latency_ms,
            llm_calls=self.llm_calls,
            total_tokens=self.total_tokens,
            tools_used=self.tools_used,
            success=self.success,
            error=self.error,
        )
        await self._obs.record_request(metrics)

    def add_llm_call(self, tokens: int = 0) -> None:
        self.llm_calls += 1
        self.total_tokens += tokens

    def add_tool(self, tool_name: str) -> None:
        if tool_name not in self.tools_used:
            self.tools_used.append(tool_name)

    def set_route(self, route_type: str) -> None:
        self.route_type = route_type
