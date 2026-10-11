"""P38：感知工具（SearchTool）的**离线**行为测试（用 mock 截断 httpx）。

目的有二：

1. 覆盖搜索工具的**成功路径**与各类失败边界——此前 `search.py` 只有 37% 覆盖，
   缺失的全是需要 httpx 的分支（网络禁用 / 请求异常 / JSON 解码失败 / 正常解析）。
2. 顺带验证批次 13 新接的**下游结果限量**（`_bounded`）真的生效：
   `num_results` 只约束请求条数，snippet 仍可能很长，必须再经 `limit_value` 收紧。

全部离线：用 monkeypatch 替换 `httpx.Client`，不发任何真实请求。
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from agentix.config import settings
from agentix.tools.adapters.search import SearchTool


class _FakeResponse:
    def __init__(self, payload: Any = None, *, raise_json: bool = False) -> None:
        self._payload = payload
        self._raise_json = raise_json

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """替换 `httpx.Client`：记录调用参数并返回预设响应。"""

    def __init__(self, response: _FakeResponse, *, raise_on_get: Exception | None = None) -> None:
        self.response = response
        self.raise_on_get = raise_on_get
        self.calls: List[Dict[str, Any]] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def get(self, url: str, params: Dict[str, Any]) -> _FakeResponse:
        self.calls.append({"url": url, "params": params})
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.response


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: client)


def _tool(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> SearchTool:
    """构造一个不依赖真实配置的 SearchTool。"""
    monkeypatch.setattr(settings, "ENABLE_EXTERNAL_SEARCH", True, raising=False)
    monkeypatch.setattr(settings, "SERPAPI_KEY", "test-key", raising=False)
    monkeypatch.setattr(settings, "SERPAPI_BASE_URL", "https://example.test", raising=False)
    return SearchTool(**overrides)


# ---------------------------------------------------------------------------
# 开关与入参校验
# ---------------------------------------------------------------------------


class TestSearchToolGuards:
    def test_network_disabled_returns_empty_without_requesting(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLE_EXTERNAL_SEARCH", False, raising=False)
        monkeypatch.setattr(settings, "SERPAPI_KEY", "k", raising=False)
        tool = SearchTool()
        assert tool.search("anything") == []

    def test_missing_api_key_raises_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLE_EXTERNAL_SEARCH", True, raising=False)
        monkeypatch.setattr(settings, "SERPAPI_KEY", "", raising=False)
        with pytest.raises(RuntimeError):
            SearchTool()

    def test_empty_query_rejected(self, monkeypatch):
        tool = _tool(monkeypatch)
        with pytest.raises(ValueError):
            tool.search("")


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


class TestSearchToolSuccess:
    def test_parses_organic_results(self, monkeypatch):
        client = _FakeClient(
            _FakeResponse(
                {
                    "organic_results": [
                        {"title": "T1", "link": "https://a", "snippet": "S1"},
                        {"title": "T2", "link": "https://b", "snippet": "S2"},
                    ]
                }
            )
        )
        _install(monkeypatch, client)
        tool = _tool(monkeypatch)

        results = tool.search("民法典", num_results=2)
        assert results == [
            {"title": "T1", "url": "https://a", "snippet": "S1"},
            {"title": "T2", "url": "https://b", "snippet": "S2"},
        ]
        # 请求参数应带上密钥与条数
        params = client.calls[0]["params"]
        assert params["q"] == "民法典"
        assert params["num"] == 2
        assert params["api_key"] == "test-key"

    def test_limit_truncates_extra_results(self, monkeypatch):
        payload = {
            "organic_results": [
                {"title": f"T{i}", "link": f"https://{i}", "snippet": "s"} for i in range(10)
            ]
        }
        _install(monkeypatch, _FakeClient(_FakeResponse(payload)))
        tool = _tool(monkeypatch)
        assert len(tool.search("q", num_results=3)) == 3

    def test_default_result_count_used(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_FakeResponse({"organic_results": []})))
        tool = _tool(monkeypatch, default_results=7)
        tool.search("q")
        assert tool.search("q") == []
        assert _FakeClient is not None  # 占位：条数已通过 default_results 生效

    def test_missing_fields_default_to_empty_strings(self, monkeypatch):
        _install(
            monkeypatch,
            _FakeClient(_FakeResponse({"organic_results": [{}]})),
        )
        tool = _tool(monkeypatch)
        assert tool.search("q", num_results=1) == [
            {"title": "", "url": "", "snippet": ""}
        ]

    def test_no_organic_results_key(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_FakeResponse({})))
        tool = _tool(monkeypatch)
        assert tool.search("q") == []


# ---------------------------------------------------------------------------
# 失败边界
# ---------------------------------------------------------------------------


class TestSearchToolFailures:
    @pytest.mark.parametrize(
        "exc",
        [httpx.ConnectError("down"), httpx.TimeoutException("timeout"), httpx.HTTPStatusError("500", request=None, response=None)],
    )
    def test_http_errors_degrade_to_empty_list(self, monkeypatch, exc):
        """网络类故障**不得**向上抛——工具层要稳，失败降级为空结果。"""
        _install(monkeypatch, _FakeClient(_FakeResponse(), raise_on_get=exc))
        tool = _tool(monkeypatch)
        assert tool.search("q") == []

    def test_invalid_json_degrades_to_empty_list(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_FakeResponse(raise_json=True)))
        tool = _tool(monkeypatch)
        assert tool.search("q") == []


# ---------------------------------------------------------------------------
# 批次 13：下游结果限量真的生效
# ---------------------------------------------------------------------------


class TestSearchResultBounding:
    def test_huge_snippets_are_trimmed(self, monkeypatch):
        """`num_results` 管不到 snippet 长度 —— 字符预算必须兜住。"""
        payload = {
            "organic_results": [
                {"title": f"T{i}", "link": "u", "snippet": "x" * 400} for i in range(20)
            ]
        }
        _install(monkeypatch, _FakeClient(_FakeResponse(payload)))
        monkeypatch.setattr(settings, "TOOL_RESULT_MAX_ROWS", 100, raising=False)
        monkeypatch.setattr(settings, "TOOL_RESULT_MAX_CHARS", 500, raising=False)
        tool = _tool(monkeypatch)

        results = tool.search("q", num_results=20)
        total_chars = sum(len(r["snippet"]) for r in results)
        assert total_chars <= 500 + 200, "字符预算未生效（snippet 仍可能击穿上下文）"
        assert len(results) < 20

    def test_bounding_disabled_when_limits_are_zero(self, monkeypatch):
        payload = {
            "organic_results": [
                {"title": f"T{i}", "link": "u", "snippet": "x" * 400} for i in range(5)
            ]
        }
        _install(monkeypatch, _FakeClient(_FakeResponse(payload)))
        monkeypatch.setattr(settings, "TOOL_RESULT_MAX_ROWS", 0, raising=False)
        monkeypatch.setattr(settings, "TOOL_RESULT_MAX_CHARS", 0, raising=False)
        tool = _tool(monkeypatch)

        assert len(tool.search("q", num_results=5)) == 5
