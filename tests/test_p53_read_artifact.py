"""P3 ``read_artifact`` 取回工具验收测试（岗位对照·写→读闭环）。

把 P3 W4 的 DoD 逐条钉死：

1. **flag 治理**：``TASK_EXEC_ENABLED`` 默认 off ⇒ 零声明零注册；
   flag-on ⇒ 声明 + handler 成对挂载（对账无 declared_no_handler）；
2. **title 契约**：schema 类名 == 注册名（``read_artifact``），否则
   ``selection._spec_for_schema`` 的 title 回查静默 miss——延迟检索与
   tool_policy 过滤都会漏掉它；
3. **roundtrip**：write_task_artifact / append_task_note 写入后按
   artifact_id 取回原文；未知 ID fail-fast（不静默空串）；
4. **只读治理**：L0 + ``side_effect=False`` + 名称不命中风险词表 ⇒
   任何 HITL 模式免审批；读不改动 store（字节级只读）；
5. **有界取回**：``max_chars`` 截断 + 提示（防整库倾倒进上下文），
   参数校验非法输入 fail-fast；
6. **可延迟**：``TOOL_DEFERRED_TOOLS`` 能把它移出常驻面并经
   ``tool_search`` 召回（P2 机制对新工具同样成立）。
"""

from __future__ import annotations

import pytest

from agentix.application.services.artifact_store import (
    get_artifact_store,
    reset_artifact_store,
)
from agentix.application.services.task_tools_bridge import install_task_write_tools
from agentix.tools import registry_init
from agentix.tools.adapters.artifact_read import (
    MAX_READ_CHARS,
    build_read_artifact_spec,
    read_artifact,
    validate_read_artifact_args,
)
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import (
    get_tool_executor,
    reset_tool_executor,
)
from agentix.tools.gateway.human_review import (
    ReviewPolicy,
    RiskLevel,
    reset_review_gate,
)
from agentix.config.settings import settings

# flag 基线（默认关——任何测试都必须还原）
assert settings.TASK_EXEC_ENABLED is False, "TASK_EXEC_ENABLED 基线必须为 False"

_TASK_TOOLS = ("append_task_note", "write_task_artifact", "read_artifact")


def _reset_task_decls() -> None:
    """清声明缓存 + 摘掉 flag-on 时挂进全局 registry 的任务工具。"""
    registry_init._DECLARATIONS.clear()
    for name in _TASK_TOOLS:
        tool_registry.delete_tool(name)


def _declared_names() -> set:
    return {spec.name for spec, _ in registry_init.get_declarations()}


def _active_titles() -> list:
    return [
        s.model_json_schema().get("title", "")
        for s in registry_init.get_active_tools()
    ]


@pytest.fixture
def task_on(monkeypatch: pytest.MonkeyPatch):
    """flag-on：声明缓存 / executor / artifact store / review gate 四单例复位。"""
    monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()
    install_task_write_tools()
    yield
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()


@pytest.fixture
def flags_off(monkeypatch: pytest.MonkeyPatch):
    """flag-off：零声明零注册的基线态（并清理延迟配置）。"""
    monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", False)
    monkeypatch.setattr(settings, "TOOL_DEFERRED_TOOLS", "")
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()
    yield
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()


# ---------------------------------------------------------------------------
# 1. flag 治理 + title 契约
# ---------------------------------------------------------------------------


class TestFlagGovernance:
    def test_flag_off_zero_declaration(self, flags_off):
        assert "read_artifact" not in _declared_names()
        assert "read_artifact" not in _active_titles()
        assert not tool_registry.has_tool("read_artifact")

    def test_flag_on_declared_and_registered(self, task_on):
        assert "read_artifact" in _declared_names()
        assert "read_artifact" in _active_titles()
        assert tool_registry.has_tool("read_artifact")

    def test_flag_on_install_pairs_spec_and_handler(self, task_on):
        # 对账：声明在册且 handler 已装 ⇒ 无 declared_no_handler 漂移
        from agentix.tools.adapters.integrity import audit_tool_registries

        report = audit_tool_registries()
        assert "read_artifact" not in report.declared_no_handler
        assert "read_artifact" not in report.handler_no_spec

    def test_title_contract_matches_registry_name(self):
        # schema title == 注册名：selection._spec_for_schema 才能回查到 spec
        assert read_artifact.model_json_schema()["title"] == "read_artifact"
        spec = build_read_artifact_spec()
        assert spec.name == "read_artifact"
        assert spec.parameters.get("title") == "read_artifact"

    def test_spec_is_readonly_low_risk(self):
        spec = build_read_artifact_spec()
        assert spec.risk_level == "L0"
        assert spec.side_effect is False
        assert spec.control.strip() == "", "L0 无需 control"
        # 名称不命中 human_review 词表（无 write/drop/send 等词）⇒ LOW
        assert ReviewPolicy().resolve("read_artifact") is RiskLevel.LOW

    def test_spec_for_schema_resolves_when_registered(self, task_on):
        from agentix.tools.selection import _spec_for_schema

        spec = _spec_for_schema(read_artifact)
        assert spec is not None and spec.name == "read_artifact"

    def test_spec_schema_defaults(self):
        props = read_artifact.model_json_schema()["properties"]
        assert props["max_chars"]["default"] == 4000
        assert props["max_chars"]["maximum"] == MAX_READ_CHARS


# ---------------------------------------------------------------------------
# 2. roundtrip + 只读语义
# ---------------------------------------------------------------------------


class TestReadRoundtrip:
    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self, task_on):
        ex = get_tool_executor()
        written = await ex.execute(
            "write_task_artifact",
            {
                "name": "p53_report.md",
                "content": "# 报告\n正文内容",
                "kind": "report",
                "summary": "摘要",
            },
        )
        assert written.success, written.error
        artifact_id = written.data["artifact_id"]

        result = await ex.execute("read_artifact", {"artifact_id": artifact_id})
        assert result.success, result.error
        data = result.data
        assert data["status"] == "ok"
        assert data["content"] == "# 报告\n正文内容"
        assert data["truncated"] is False
        assert data["kind"] == "report"
        assert data["summary"] == "摘要"
        assert data["size_bytes"] == written.data["size_bytes"]

    @pytest.mark.asyncio
    async def test_append_note_then_read(self, task_on):
        ex = get_tool_executor()
        note = await ex.execute("append_task_note", {"note": "要点一", "key": "k"})
        assert note.success, note.error
        result = await ex.execute(
            "read_artifact", {"artifact_id": note.data["artifact_id"]}
        )
        assert result.success, result.error
        assert result.data["content"] == "要点一"
        assert result.data["kind"] == "task_note"

    @pytest.mark.asyncio
    async def test_unknown_id_fails_fast(self, task_on):
        result = await get_tool_executor().execute(
            "read_artifact", {"artifact_id": "no-such-id"}
        )
        assert not result.success
        assert "not found" in (result.error or "")

    @pytest.mark.asyncio
    async def test_read_does_not_mutate_store(self, task_on):
        store = get_artifact_store()
        ref = store.put("immutable body", kind="task_artifact", summary="s")
        before_items = dict(store._items)
        before_stats = store.stats()

        result = await get_tool_executor().execute(
            "read_artifact", {"artifact_id": ref.artifact_id}
        )
        assert result.success, result.error
        assert dict(store._items) == before_items, "读取不得改动 store 内容"
        assert store.stats() == before_stats

    @pytest.mark.asyncio
    async def test_read_never_requires_hitl(self, task_on, monkeypatch):
        # L0/LOW：即使 gate 全开也免审批（与 write 的 review_required 对照）
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
        reset_review_gate()
        store = get_artifact_store()
        ref = store.put("safe body", kind="task_artifact")
        try:
            result = await get_tool_executor().execute(
                "read_artifact", {"artifact_id": ref.artifact_id}
            )
            assert result.success, result.error
            assert result.metadata.get("reason") != "review_required"
        finally:
            reset_review_gate()


# ---------------------------------------------------------------------------
# 3. 有界取回 + 参数校验
# ---------------------------------------------------------------------------


class TestBoundedRead:
    @pytest.mark.asyncio
    async def test_max_chars_truncates_with_marker(self, task_on):
        store = get_artifact_store()
        ref = store.put("z" * 10000, kind="task_artifact")
        result = await get_tool_executor().execute(
            "read_artifact", {"artifact_id": ref.artifact_id, "max_chars": 100}
        )
        assert result.success, result.error
        assert result.data["truncated"] is True
        assert result.data["content"].startswith("z" * 100)
        assert result.data["content"].endswith("\n…[artifact truncated]")

    @pytest.mark.asyncio
    async def test_short_body_not_truncated(self, task_on):
        store = get_artifact_store()
        ref = store.put("short", kind="task_artifact")
        result = await get_tool_executor().execute(
            "read_artifact", {"artifact_id": ref.artifact_id}
        )
        assert result.success, result.error
        assert result.data["content"] == "short"
        assert result.data["truncated"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args",
        [
            {"artifact_id": ""},
            {"artifact_id": "x" * 129},
            {"artifact_id": "a", "max_chars": 0},
            {"artifact_id": "a", "max_chars": MAX_READ_CHARS + 1},
            {"artifact_id": "a", "max_chars": "abc"},
        ],
    )
    async def test_invalid_args_fail_fast(self, task_on, args):
        result = await get_tool_executor().execute("read_artifact", args)
        assert not result.success, f"非法入参必须失败：{args}"

    def test_validator_normalizes(self):
        out = validate_read_artifact_args(
            {"artifact_id": "  abc-1  ", "max_chars": "512"}
        )
        assert out == {"artifact_id": "abc-1", "max_chars": 512}

    def test_validator_rejects(self):
        with pytest.raises(ValueError):
            validate_read_artifact_args({"artifact_id": ""})
        with pytest.raises(ValueError):
            validate_read_artifact_args({"artifact_id": "a", "max_chars": "x"})


# ---------------------------------------------------------------------------
# 4. 可延迟（P2 机制对新工具成立）
# ---------------------------------------------------------------------------


class TestDeferrable:
    def test_deferred_hides_from_resident_but_keeps_declaration(
        self, task_on, monkeypatch
    ):
        monkeypatch.setattr(settings, "TOOL_DEFERRED_TOOLS", "read_artifact")
        registry_init._DECLARATIONS.clear()
        assert "read_artifact" in _declared_names(), "延迟不删除声明"
        assert "read_artifact" not in _active_titles(), "延迟退出常驻面"

    def test_tool_search_recalls_deferred_read_artifact(
        self, task_on, monkeypatch
    ):
        monkeypatch.setattr(settings, "TOOL_DEFERRED_TOOLS", "read_artifact")
        registry_init._DECLARATIONS.clear()
        from agentix.tools.adapters.tool_search import search_declarations

        payload = search_declarations("取回产物", top_k=20)
        names = [r["name"] for r in payload["results"]]
        assert "read_artifact" in names
        hit = next(r for r in payload["results"] if r["name"] == "read_artifact")
        assert hit["deferred"] is True
        assert hit["risk_level"] == "L0"
        assert hit["schema"]["title"] == "read_artifact"
