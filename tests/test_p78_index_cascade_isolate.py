"""F1b2 验收（统一执行链 Stage F · Memory/指令 P1 残余三项）。

覆盖 memory-optimization §5 的三条待实施项：

1. **M-P1-3 MEMORY 索引制**（``MEMORY_INDEX_ENABLED`` 默认关）：flag-off
   注入与基线逐位等价；flag-on 记忆上下文转「段落头保留 + 正文 obs://
   指针索引」，正文外置 ArtifactStore 可取回、预算封顶截索引尾部、
   外置降级 fail-open 保留原文；取回端 = ``read_artifact``（obs:// 前缀
   自动剥离）。
2. **M-P1-4 AGENTS 级联**（``AGENTS_CASCADE_ENABLED`` 默认关）：
   ``build_instructions`` flag-off 逐字节等价；flag-on 追加全局层
   （``~/.agentix/``）与子目录层（消息触达才加载、根级命中 hash 去重），
   ``read_agents_file`` 工具声明与 handler 成对、项目边界内按需取回。
3. **M-P1-5 obs:// 接线**（``CONTEXT_ISOLATE_PERSIST_ENABLED`` 默认关）：
   flag-off 隔离引用与基线逐位一致（``obs://{step_id}`` 裸引用）；
   flag-on 超长观测正文真正外置、引用落 ``obs://{artifact_id}`` 经
   ``read_artifact`` 可取回；外置失败 fail-open 回裸引用。

外加 3 新键五处同步棘轮（settings / selfcheck / .env.example /
configuration.md / p41 手写清单）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentix.agent.context import ContextEngine, build_instructions
from agentix.agent.context import instructions as instructions_mod
from agentix.agent.context.install import install_read_agents_file_tool
from agentix.application.services.artifact_store import (
    get_artifact_store,
    reset_artifact_store,
)
from agentix.application.services.chat_service import (
    _index_memory_context,
    _maybe_memory_context,
)
from agentix.application.services.task_tools_bridge import (
    install_artifact_read_tool,
)
from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING
from agentix.config.settings import settings
from agentix.domain.agent.types import Observation, TaskSpec
from agentix.tools import registry_init
from agentix.tools.adapters.agents_file_read import (
    build_read_agents_file_spec,
    read_agents_file,
    validate_read_agents_file_args,
)
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import (
    get_tool_executor,
    reset_tool_executor,
)
from agentix.tools.gateway.human_review import reset_review_gate

REPO = Path(__file__).resolve().parents[1]

#: F1b2 三个新键（默认全部 False——任何测试都必须还原）
_NEW_KEYS = (
    "MEMORY_INDEX_ENABLED",
    "AGENTS_CASCADE_ENABLED",
    "CONTEXT_ISOLATE_PERSIST_ENABLED",
)
for _key in _NEW_KEYS:
    assert getattr(settings, _key) is False, f"{_key} 基线必须为 False"

#: 本批会挂进声明面的工具（teardown 必须摘掉，防跨文件污染）
_NEW_TOOLS = ("read_artifact", "read_agents_file")

#: 样例记忆上下文（段落头 + 缩进摘要行 + 普通正文行）
_SAMPLE_CTX = (
    "用户历史：共 3 次会话，关注领域：合同,劳动\n"
    "相关历史对话摘要：\n"
    "  1. 用户要求回答时必须附上法条出处\n"
    "用户稳定偏好（长期记忆）：戒烟；不吃香菜"
)


def _reset_decls() -> None:
    """清声明缓存 + 摘掉本批工具（flag-on 时挂进全局 registry 的）。"""
    registry_init._DECLARATIONS.clear()
    for name in _NEW_TOOLS:
        tool_registry.delete_tool(name)


@pytest.fixture
def fresh_state():
    """声明缓存 / executor / artifact store / review gate 四单例复位。"""
    _reset_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()
    yield
    _reset_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()


def _declared_names() -> set:
    return {spec.name for spec, _ in registry_init.get_declarations()}


class _FakeLTM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def get_context_for_query(self, message: str, **kwargs) -> str:
        return self._text


def _patch_ltm(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    import agentix.application.services.long_term_memory as ltm_mod

    monkeypatch.setattr(ltm_mod, "get_long_term_memory", lambda: _FakeLTM(text))


def _extract_obs_ids(text: str) -> list:
    ids = []
    for line in text.splitlines():
        marker = " → obs://"
        if line.startswith("- ") and marker in line:
            ids.append(line.split(marker, 1)[1])
    return ids


# ---------------------------------------------------------------------------
# 0. flag 五处同步（3 新键）
# ---------------------------------------------------------------------------


class TestFlagSync:
    def test_all_three_defaults_false(self):
        for key in _NEW_KEYS:
            assert getattr(settings, key) is False

    def test_selfcheck_lists_keys(self):
        for key in _NEW_KEYS:
            assert key in DEFAULT_IGNORED_MISSING, f"{key} 未列入 selfcheck 合法省略"

    def test_env_example_active_false_lines(self):
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        active = {
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for key in _NEW_KEYS:
            assert f"{key}=false" in active, f".env.example 缺少 {key}=false 活跃行"

    def test_configuration_doc_rows(self):
        text = (REPO / "docs" / "architecture" / "configuration.md").read_text(
            encoding="utf-8"
        )
        for key in _NEW_KEYS:
            assert f"| `{key}` |" in text, f"configuration.md §5 缺少 {key} 行"
        assert "`MEMORY_INDEX_ENABLED`" in text  # §2 记忆分组行也收录

    def test_p41_hand_written_list_contains_keys(self):
        text = (REPO / "tests" / "test_p41_doc_consistency.py").read_text(
            encoding="utf-8"
        )
        for key in _NEW_KEYS:
            assert f'"{key}"' in text, f"p41 手写清单缺少 {key}（守卫会静默失效）"


# ---------------------------------------------------------------------------
# 1. M-P1-3 记忆索引制
# ---------------------------------------------------------------------------


class TestMemoryIndex:
    @pytest.mark.asyncio
    async def test_flag_off_returns_base_untouched(self, monkeypatch):
        """索引关（默认）= 原文注入，与基线逐位等价。"""
        monkeypatch.setattr(settings, "MEMORY_CONTEXT_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_FANOUT_ENABLED", False)
        monkeypatch.setattr(settings, "MEMORY_INDEX_ENABLED", False)
        _patch_ltm(monkeypatch, _SAMPLE_CTX)
        out = await _maybe_memory_context("q")
        assert out == _SAMPLE_CTX

    @pytest.mark.asyncio
    async def test_context_off_index_on_stays_empty(self, monkeypatch, fresh_state):
        """嵌套闸：MEMORY_CONTEXT 关时索引键单独开也不注入（零 LTM 触碰）。"""
        monkeypatch.setattr(settings, "MEMORY_CONTEXT_ENABLED", False)
        monkeypatch.setattr(settings, "MEMORY_INDEX_ENABLED", True)
        _patch_ltm(monkeypatch, "不该被读到")
        out = await _maybe_memory_context("q")
        assert out == ""

    def test_index_keeps_headers_offloads_topics(self, fresh_state):
        out = _index_memory_context(_SAMPLE_CTX)
        lines = out.splitlines()
        assert "相关历史对话摘要：" in lines  # 段落头原样保留
        topic_lines = [ln for ln in lines if ln.startswith("- ")]
        assert len(topic_lines) == 3  # 画像行 + 摘要行 + 偏好行
        ids = _extract_obs_ids(out)
        assert len(ids) == 3 and all(len(i) == 32 for i in ids)
        # 正文可取回：第一个指针取回 = 归一后的原文行
        store = get_artifact_store()
        assert store.get(ids[0]) == "用户历史：共 3 次会话，关注领域：合同,劳动"
        assert store.get(ids[1]) == "1. 用户要求回答时必须附上法条出处"
        assert store.get(ids[2]) == "用户稳定偏好（长期记忆）：戒烟；不吃香菜"

    def test_index_long_topic_preview_bounded(self, fresh_state):
        long_line = "很长的记忆正文" + "甲" * 300
        out = _index_memory_context(long_line)
        topic_line = out.splitlines()[0]
        assert topic_line.startswith("- ")
        assert "…" in topic_line
        # 索引行不倾倒全文（预览 100 字符封顶），全文仍在 store 里
        assert "甲" * 300 not in topic_line
        aid = _extract_obs_ids(out)[0]
        assert get_artifact_store().get(aid) == long_line

    def test_index_degrade_keeps_original_line(self, monkeypatch, fresh_state):
        """外置降级（fail-open）⇒ 该行保留原文，绝不丢记忆。"""
        import agentix.application.services.artifact_store as store_mod

        def _degrade(content, **kwargs):
            return store_mod.OffloadResult(is_offloaded=False, inline=content)

        monkeypatch.setattr(store_mod, "offload_content", _degrade)
        out = _index_memory_context(_SAMPLE_CTX)
        assert out.splitlines()[0] == "用户历史：共 3 次会话，关注领域：合同,劳动"
        assert "用户稳定偏好（长期记忆）：戒烟；不吃香菜" in out

    def test_index_budget_truncates_tail(self, monkeypatch, fresh_state):
        """常驻预算封顶 ⇒ 只截索引尾部 + 附提示（头部/段落头保留）。"""
        import agentix.application.services.chat_service as chat_mod

        monkeypatch.setattr(chat_mod, "MEMORY_INDEX_MAX_CHARS", 130)
        out = _index_memory_context(_SAMPLE_CTX)
        assert out.endswith("…[记忆索引已截断，可按需取回]")
        assert len(out) == 130
        assert out.startswith("- 用户历史：")  # 头部（高优先级）保留
        assert "相关历史对话摘要：" in out

    @pytest.mark.asyncio
    async def test_end_to_end_index_and_fetch(self, monkeypatch, fresh_state):
        """MEMORY_CONTEXT + MEMORY_INDEX 开：注入为索引，obs:// 指针经
        read_artifact（前缀剥离）取回原文。"""
        monkeypatch.setattr(settings, "MEMORY_CONTEXT_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_FANOUT_ENABLED", False)
        monkeypatch.setattr(settings, "MEMORY_INDEX_ENABLED", True)
        _patch_ltm(monkeypatch, _SAMPLE_CTX)
        out = await _maybe_memory_context("q")
        assert "相关历史对话摘要：" in out
        assert "- " in out and "obs://" in out
        assert _SAMPLE_CTX not in out  # 正文不再内联

        install_artifact_read_tool()
        aid = _extract_obs_ids(out)[1]
        result = await get_tool_executor().execute(
            "read_artifact", {"artifact_id": f"obs://{aid}"}
        )
        assert result.success, result.error
        assert result.data["content"] == "1. 用户要求回答时必须附上法条出处"

    @pytest.mark.asyncio
    async def test_index_failure_falls_back_to_original(self, monkeypatch, fresh_state):
        """索引变换自身炸掉 ⇒ 回原文（fail-open，不丢记忆）。"""
        monkeypatch.setattr(settings, "MEMORY_CONTEXT_ENABLED", True)
        monkeypatch.setattr(settings, "MEMORY_FANOUT_ENABLED", False)
        monkeypatch.setattr(settings, "MEMORY_INDEX_ENABLED", True)
        _patch_ltm(monkeypatch, _SAMPLE_CTX)

        import agentix.application.services.chat_service as chat_mod

        def _boom(ctx):
            raise RuntimeError("index exploded")

        monkeypatch.setattr(chat_mod, "_index_memory_context", _boom)
        out = await _maybe_memory_context("q")
        assert out == _SAMPLE_CTX

    def test_read_artifact_declared_under_index_flag(self, monkeypatch, fresh_state):
        """MEMORY_INDEX 单开（TASK_EXEC 关）也声明 read_artifact（取回端在场）。"""
        monkeypatch.setattr(settings, "MEMORY_INDEX_ENABLED", True)
        _reset_decls()
        assert "read_artifact" in _declared_names()


# ---------------------------------------------------------------------------
# 2. M-P1-4 AGENTS 级联
# ---------------------------------------------------------------------------


class TestAgentsCascade:
    def test_cascade_flag_off_ignores_layers(self, tmp_path):
        """cascade 关（默认）⇒ global_start/subdirs 参数完全无效，
        输出与不带这两个参数的调用逐字节一致。"""
        gdir = tmp_path / "g"
        gdir.mkdir()
        (gdir / "AGENTS.md").write_text("GLOBAL规则", encoding="utf-8")
        sub = tmp_path / "s"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("SUB规则", encoding="utf-8")

        with_layers = build_instructions(
            settings_rules=("R",),
            session_rules=("SESS",),
            skills_enabled=False,
            load_project=False,
            cascade_enabled=False,
            global_start=gdir,
            subdirs=(sub,),
        )
        plain = build_instructions(
            settings_rules=("R",),
            session_rules=("SESS",),
            skills_enabled=False,
            load_project=False,
        )
        assert with_layers == plain == ("R", "SESS")

    def test_cascade_layers_order_and_content(self, tmp_path):
        """flag-on 层序 = settings → 全局 → 项目根 → 子目录 → 会话。"""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "AGENTS.md").write_text("ROOT规则", encoding="utf-8")
        sub = root / "pkg"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("PKG规则", encoding="utf-8")
        gdir = tmp_path / "home"
        gdir.mkdir()
        (gdir / "AGENTS.md").write_text("GLOBAL规则", encoding="utf-8")

        out = build_instructions(
            settings_rules=("R",),
            skills_enabled=False,
            project_start=root,
            stop=root,
            global_start=gdir,
            subdirs=(sub,),
            cascade_enabled=True,
            session_rules=("SESS",),
        )
        assert out == ("R", "GLOBAL规则", "ROOT规则", "PKG规则", "SESS")

    def test_cascade_subdir_root_hit_deduped(self, tmp_path):
        """子目录没有自己的约定文件 ⇒ 向上命中根文件，hash 去重只留一份。"""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "AGENTS.md").write_text("ROOT规则", encoding="utf-8")
        sub = root / "pkg"
        sub.mkdir()

        out = build_instructions(
            skills_enabled=False,
            project_start=root,
            stop=root,
            subdirs=(sub,),
            cascade_enabled=True,
        )
        assert out == ("ROOT规则",)

    def test_cascade_reads_settings_when_param_none(self, monkeypatch, tmp_path):
        """cascade_enabled=None ⇒ 读 settings（生产路径）。"""
        gdir = tmp_path / "g"
        gdir.mkdir()
        (gdir / "AGENTS.md").write_text("GLOBAL规则", encoding="utf-8")
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", True)
        out = build_instructions(
            skills_enabled=False,
            load_project=False,
            global_start=gdir,
        )
        assert out == ("GLOBAL规则",)
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", False)
        off = build_instructions(
            skills_enabled=False,
            load_project=False,
            global_start=gdir,
        )
        assert off == ()

    def test_mentioned_dirs_flag_off_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", False)
        assert instructions_mod.mentioned_instruction_dirs(
            "看 src/foo/bar.py", root=tmp_path
        ) == ()

    def test_mentioned_dirs_parses_existing_dirs(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", True)
        (tmp_path / "src" / "foo").mkdir(parents=True)
        (tmp_path / "src" / "foo" / "bar.py").write_text("x", encoding="utf-8")
        out = instructions_mod.mentioned_instruction_dirs(
            "请按 src/foo/bar.py 的约定改，顺便看 docs 和不存在/zz.py",
            root=tmp_path,
        )
        assert out == ("src/foo",)  # 裸词/不存在目录/文件名同级噪声均被拒

    def test_mentioned_dirs_rejects_escape_and_bare(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", True)
        assert instructions_mod.mentioned_instruction_dirs(
            "../outside/x.py  README.md", root=tmp_path
        ) == ()
        assert instructions_mod.mentioned_instruction_dirs("", root=tmp_path) == ()

    def test_mentioned_dirs_respects_limit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", True)
        for i in range(5):
            (tmp_path / f"pkg{i}").mkdir()
        out = instructions_mod.mentioned_instruction_dirs(
            " ".join(f"pkg{i}/f.py" for i in range(5)),
            root=tmp_path,
            limit=2,
        )
        assert len(out) == 2

    def test_title_and_spec_contract(self):
        assert read_agents_file.model_json_schema()["title"] == "read_agents_file"
        spec = build_read_agents_file_spec()
        assert spec.name == "read_agents_file"
        assert spec.risk_level == "L0"
        assert spec.side_effect is False
        assert "何时用" in spec.description
        assert "何时不能用" in spec.description

    def test_validate_rejects_empty_path(self):
        with pytest.raises(ValueError):
            validate_read_agents_file_args({"path": ""})
        with pytest.raises(ValueError):
            validate_read_agents_file_args({"path": "x" * 513})
        assert validate_read_agents_file_args({"path": " a/b "}) == {"path": "a/b"}

    def test_declaration_gated_by_flag(self, monkeypatch, fresh_state):
        assert "read_agents_file" not in _declared_names()  # 默认关
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", True)
        _reset_decls()
        assert "read_agents_file" in _declared_names()

    @pytest.mark.asyncio
    async def test_handler_reads_nearest_within_boundary(
        self, monkeypatch, fresh_state, tmp_path
    ):
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", True)
        _reset_decls()
        install_read_agents_file_tool()
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "AGENTS.md").write_text("PKG规则", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = await get_tool_executor().execute(
            "read_agents_file", {"path": "pkg/bar.py"}
        )
        assert result.success, result.error
        assert result.data["found"] is True
        assert result.data["content"] == "PKG规则"
        assert "pkg" in result.data["path"]

    @pytest.mark.asyncio
    async def test_handler_not_found_returns_structured_miss(
        self, monkeypatch, fresh_state, tmp_path
    ):
        """项目边界内没有约定文件 ⇒ found=false 回执（不越界、不抛错）。"""
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", True)
        _reset_decls()
        install_read_agents_file_tool()
        monkeypatch.chdir(tmp_path)  # 空目录：向上到 cwd 即停

        result = await get_tool_executor().execute(
            "read_agents_file", {"path": "nowhere/x.py"}
        )
        assert result.success, result.error
        assert result.data["found"] is False
        assert "reason" in result.data

    def test_install_gated_by_flag(self, monkeypatch, fresh_state):
        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", False)
        install_read_agents_file_tool()  # flag-off = no-op，不注册
        assert "read_agents_file" not in {
            spec.name for spec, _ in registry_init.get_declarations()
        }

    def test_audit_pairs_spec_and_handler(self, monkeypatch, fresh_state):
        from agentix.tools.adapters.integrity import audit_tool_registries

        monkeypatch.setattr(settings, "AGENTS_CASCADE_ENABLED", True)
        _reset_decls()
        install_read_agents_file_tool()
        report = audit_tool_registries()
        assert "read_agents_file" not in report.declared_no_handler
        assert "read_agents_file" not in report.handler_no_spec

    def test_production_wiring_sources(self):
        adapter = (
            REPO / "agentix" / "application" / "services" / "agent_loop_adapter.py"
        ).read_text(encoding="utf-8")
        assert "mentioned_instruction_dirs(message)" in adapter
        main_py = (REPO / "agentix" / "main.py").read_text(encoding="utf-8")
        assert "install_read_agents_file_tool" in main_py
        assert "AGENTS_CASCADE_ENABLED" in main_py
        registry_src = (
            REPO / "agentix" / "tools" / "registry_init.py"
        ).read_text(encoding="utf-8")
        assert "AGENTS_CASCADE_ENABLED" in registry_src


# ---------------------------------------------------------------------------
# 3. M-P1-5 obs:// 隔离接线
# ---------------------------------------------------------------------------


class TestIsolatePersist:
    @pytest.mark.asyncio
    async def test_flag_off_bare_ref_unchanged(self, monkeypatch, fresh_state):
        """隔离开但持久化关（默认）⇒ obs://{step_id} 裸引用，与基线逐位一致。"""
        monkeypatch.setattr(settings, "CONTEXT_ISOLATE_PERSIST_ENABLED", False)
        engine = ContextEngine(isolate_over_chars=100)
        big = "X" * 500
        snap = await engine.build(
            TaskSpec(goal="g", task_id="z"),
            trajectory=[Observation(step_id="s1", content=big, ok=True)],
        )
        assert snap.meta.get("isolated_refs") == ["obs://s1"]
        assert big not in snap.prompt
        assert "[isolated obs://s1]" in snap.prompt

    @pytest.mark.asyncio
    async def test_flag_on_persists_and_is_fetchable(self, monkeypatch, fresh_state):
        monkeypatch.setattr(settings, "CONTEXT_ISOLATE_PERSIST_ENABLED", True)
        engine = ContextEngine(isolate_over_chars=100)
        big = "Z" * 500
        snap = await engine.build(
            TaskSpec(goal="g", task_id="z"),
            trajectory=[Observation(step_id="s1", content=big, ok=True)],
        )
        refs = snap.meta.get("isolated_refs")
        assert refs and refs[0].startswith("obs://")
        artifact_id = refs[0][len("obs://") :]
        assert artifact_id != "s1"  # 落到真实 artifact，而非悬空 step_id
        assert get_artifact_store().get(artifact_id) == big  # 正文可取回
        assert big not in snap.prompt  # 全文不进 prompt
        assert f"[isolated {refs[0]}]" in snap.prompt
        assert "Z" * 100 in snap.prompt  # 头部仍在（隔离不是删除）

    @pytest.mark.asyncio
    async def test_flag_on_degrade_falls_back_to_bare_ref(
        self, monkeypatch, fresh_state
    ):
        """外置失败 ⇒ fail-open 回裸引用行为（隔离绝不抛错）。"""
        monkeypatch.setattr(settings, "CONTEXT_ISOLATE_PERSIST_ENABLED", True)
        import agentix.application.services.artifact_store as store_mod

        def _degrade(content, **kwargs):
            return store_mod.OffloadResult(is_offloaded=False, inline=content)

        monkeypatch.setattr(store_mod, "offload_content", _degrade)
        engine = ContextEngine(isolate_over_chars=100)
        snap = await engine.build(
            TaskSpec(goal="g", task_id="z"),
            trajectory=[Observation(step_id="s1", content="Y" * 500, ok=True)],
        )
        assert snap.meta.get("isolated_refs") == ["obs://s1"]

    @pytest.mark.asyncio
    async def test_read_artifact_accepts_obs_prefix(self, monkeypatch, fresh_state):
        """read_artifact 认 obs:// 前缀（剥离后按 artifact_id 取回）。"""
        monkeypatch.setattr(settings, "MEMORY_INDEX_ENABLED", True)
        _reset_decls()
        install_artifact_read_tool()
        ref = get_artifact_store().put("外置正文", kind="memory_topic", summary="s")

        result = await get_tool_executor().execute(
            "read_artifact", {"artifact_id": f"obs://{ref.artifact_id}"}
        )
        assert result.success, result.error
        assert result.data["content"] == "外置正文"
        assert result.data["artifact_id"] == ref.artifact_id  # 归一后的真 ID

        miss = await get_tool_executor().execute(
            "read_artifact", {"artifact_id": "obs://no-such-id"}
        )
        assert not miss.success
        assert "not found" in (miss.error or "")

    def test_source_wiring(self):
        engine_src = (
            REPO / "agentix" / "agent" / "context" / "engine.py"
        ).read_text(encoding="utf-8")
        assert "CONTEXT_ISOLATE_PERSIST_ENABLED" in engine_src
        assert "offload_content" in engine_src
        bridge_src = (
            REPO / "agentix" / "application" / "services" / "task_tools_bridge.py"
        ).read_text(encoding="utf-8")
        assert 'startswith("obs://")' in bridge_src
