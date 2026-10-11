"""p102 上下文工程 L2 接线验收（A/B/C/D 四阶段，默认全关）。

对应优化规划「L2 生产链路上下文工程补齐」：

1. **A（Prompt Caching 可观测与纪律）**：
   - A1 ``general`` 角色指纹改取**模板常量**（``_TEMPLATE_PREFIX_ROLES``）——
     正文含 memory/指令动态注入不再上报，模板变更仍是漂移信号；
   - A2 provider 缓存命中量采集：``_extract_cached_tokens`` 双形态字段
     （langchain ``input_token_details.cache_read`` / OpenAI
     ``prompt_tokens_details.cached_tokens``），缺字段 = 不记账（不造假 miss）；
     ``record_llm_tokens(label_type="cached")`` 落 ``llm_tokens_total{type="cached"}``。
   - A4 守护测试：``assert_static_prefix_stable`` 对 general 组装序
     （静态模板常量在前、动态 logic/指令/记忆在后）验证**公共前缀稳定**——
     只进测试，不进热路径。
2. **B（指令分层接入 L2）**：``LG_INSTRUCTIONS_ENABLED`` 默认关 = flag-off
   返回空串、config 不写键；flag-on 走 ``build_instructions`` + 进程级指纹
   缓存（文件不变不重读盘）；general_handler 拼接顺序 = 静态 system 常量 →
   指令块 → memory_context（源级棘轮钉死）。
3. **C（token 级历史窗口）**：``LG_TOKEN_HISTORY_BUDGET`` 默认 0 = 与条数
   窗口逐位等价；``>0`` 从最新向前一次性累加（每条只计一次，无 O(n²)），
   至少保留最新一条。
4. **D（状态尾注）**：``LG_STATUS_TAIL_ENABLED`` 默认关 = 空串；flag-on
   输出 ``[状态] …``（轮次/历史量/预算余量），且结构上只作用于本轮 human
   消息尾部（append-only，绝不进静态前缀）。

外加 3 新键五处同步棘轮（settings / selfcheck / .env.example /
configuration.md / p41 手写清单）。

已知边界（有意为之，记档）：流式路径（``_stream_events_impl``）无
BudgetLedger ⇒ 状态尾注的预算段仅非流式 ask 出现，轮次/历史量两段两路径
一致；flag 默认关 ⇒ 基线不受影响。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from prometheus_client import REGISTRY

from agentix.application.services import chat_service
from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING
from agentix.config.settings import settings
from agentix.domain.policies.prompt_envelope import assert_static_prefix_stable
from agentix.observability.metrics import (
    record_cache_hit,
    record_cache_miss,
    record_llm_tokens,
)
from agentix.orchestration.langgraph import lg_builder

REPO = Path(__file__).resolve().parents[1]

#: 本批三个新键（默认 False/0——任何测试都必须还原）
_NEW_KEYS = ("LG_INSTRUCTIONS_ENABLED", "LG_STATUS_TAIL_ENABLED")
_NEW_ZERO_KEYS = ("LG_TOKEN_HISTORY_BUDGET",)
for _key in _NEW_KEYS:
    assert getattr(settings, _key) is False, f"{_key} 基线必须为 False"
for _key in _NEW_ZERO_KEYS:
    assert getattr(settings, _key) == 0, f"{_key} 基线必须为 0"


@pytest.fixture(autouse=True)
def _restore_flags_and_cache():
    """每例后还原 flag 默认值与指令块进程级缓存（防跨文件污染）。"""
    yield
    for key in _NEW_KEYS:
        setattr(settings, key, False)
    for key in _NEW_ZERO_KEYS:
        setattr(settings, key, 0)
    chat_service._INSTRUCTIONS_CACHE["fingerprint"] = ""
    chat_service._INSTRUCTIONS_CACHE["text"] = ""


# ---------------------------------------------------------------------------
# 0. 五处同步棘轮（3 新键）
# ---------------------------------------------------------------------------


class TestFlagSync:
    def test_defaults_off(self):
        for key in _NEW_KEYS:
            assert getattr(settings, key) is False
        for key in _NEW_ZERO_KEYS:
            assert getattr(settings, key) == 0

    def test_selfcheck_lists_keys_as_legal_omission(self):
        for key in (*_NEW_KEYS, *_NEW_ZERO_KEYS):
            assert key in DEFAULT_IGNORED_MISSING, f"{key} 未列入 selfcheck 合法省略"

    def test_env_example_documents_keys(self):
        text = (REPO / ".env.example").read_text(encoding="utf-8-sig", errors="replace")
        for key in (*_NEW_KEYS, *_NEW_ZERO_KEYS):
            assert f"{key}=" in text, f"{key} 未写入 .env.example"

    def test_configuration_md_documents_keys(self):
        text = (REPO / "docs" / "architecture" / "configuration.md").read_text(
            encoding="utf-8", errors="replace"
        )
        for key in (*_NEW_KEYS, *_NEW_ZERO_KEYS):
            assert f"`{key}`" in text, f"{key} 未写入 configuration.md §5 表"

    def test_p41_handwritten_list_covers_keys(self):
        text = (REPO / "tests" / "test_p41_doc_consistency.py").read_text(
            encoding="utf-8", errors="replace"
        )
        for key in (*_NEW_KEYS, *_NEW_ZERO_KEYS):
            assert f'"{key}"' in text, f"{key} 未加入 p41 手写清单（守卫将对其失效）"


# ---------------------------------------------------------------------------
# B：指令分层（_maybe_instructions + 指纹缓存）
# ---------------------------------------------------------------------------


class TestInstructionsInjection:
    @pytest.mark.asyncio
    async def test_flag_off_returns_empty_string(self):
        assert settings.LG_INSTRUCTIONS_ENABLED is False
        assert await chat_service._maybe_instructions("随便聊聊") == ""

    @pytest.mark.asyncio
    async def test_flag_on_builds_and_joins_blocks(self, monkeypatch):
        monkeypatch.setattr(settings, "LG_INSTRUCTIONS_ENABLED", True)
        import agentix.agent.context.instructions as instructions_mod

        monkeypatch.setattr(
            instructions_mod,
            "build_instructions",
            lambda **kw: ("block-a", "block-b"),
        )
        monkeypatch.setattr(
            instructions_mod, "mentioned_instruction_dirs", lambda message: ()
        )
        monkeypatch.setattr(
            instructions_mod, "find_project_instructions", lambda *a, **kw: None
        )
        out = await chat_service._maybe_instructions("hello")
        assert out == "block-a\n\nblock-b\n\n"

    @pytest.mark.asyncio
    async def test_fingerprint_cache_hits_without_reread(self, monkeypatch):
        monkeypatch.setattr(settings, "LG_INSTRUCTIONS_ENABLED", True)
        import agentix.agent.context.instructions as instructions_mod

        calls = {"n": 0}

        def fake_build(**kw):
            calls["n"] += 1
            return ("block-a",)

        monkeypatch.setattr(instructions_mod, "build_instructions", fake_build)
        monkeypatch.setattr(
            instructions_mod, "mentioned_instruction_dirs", lambda message: ()
        )
        monkeypatch.setattr(
            instructions_mod, "find_project_instructions", lambda *a, **kw: None
        )
        first = await chat_service._maybe_instructions("hello")
        second = await chat_service._maybe_instructions("hello")
        assert first == second == "block-a\n\n"
        assert calls["n"] == 1, "指纹未变时应复用缓存，不得重读盘"

    @pytest.mark.asyncio
    async def test_fingerprint_change_triggers_reread(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "LG_INSTRUCTIONS_ENABLED", True)
        import agentix.agent.context.instructions as instructions_mod

        calls = {"n": 0}

        def fake_build(**kw):
            calls["n"] += 1
            return ("block-a",)

        monkeypatch.setattr(instructions_mod, "build_instructions", fake_build)
        monkeypatch.setattr(
            instructions_mod, "mentioned_instruction_dirs", lambda message: ()
        )
        project = tmp_path / "AGENTS.md"
        project.write_text("demo", encoding="utf-8")
        monkeypatch.setattr(
            instructions_mod, "find_project_instructions", lambda *a, **kw: project
        )
        await chat_service._maybe_instructions("hello")
        # 指纹变化（项目层从无到有）：换一个不存在的路径 ⇒ 键变 ⇒ 必须重读
        monkeypatch.setattr(
            instructions_mod,
            "find_project_instructions",
            lambda *a, **kw: tmp_path / "missing.md",
        )
        await chat_service._maybe_instructions("hello")
        assert calls["n"] == 2, "指纹变化后必须重读盘（宁可多读，不可复用陈旧指令）"

    @pytest.mark.asyncio
    async def test_build_failure_fails_open_to_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "LG_INSTRUCTIONS_ENABLED", True)
        import agentix.agent.context.instructions as instructions_mod

        def boom(**kw):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(instructions_mod, "build_instructions", boom)
        monkeypatch.setattr(
            instructions_mod, "mentioned_instruction_dirs", lambda message: ()
        )
        monkeypatch.setattr(
            instructions_mod, "find_project_instructions", lambda *a, **kw: None
        )
        assert await chat_service._maybe_instructions("hello") == ""

    def test_handler_injection_order_instructions_before_memory(self):
        """源级棘轮：general_handler 内指令块必须拼接在 memory_context 之前。"""
        src = (
            REPO / "agentix" / "application" / "agents" / "general_handler.py"
        ).read_text(encoding="utf-8")
        instructions_at = src.find('configurable.get("instructions")')
        memory_at = src.find('configurable.get("memory_context")')
        assert instructions_at != -1, "general_handler 未消费 configurable['instructions']"
        assert memory_at != -1
        assert instructions_at < memory_at, "注入顺序必须为 静态常量 → 指令块 → memory_context"


# ---------------------------------------------------------------------------
# C：token 级历史窗口
# ---------------------------------------------------------------------------


def _msg(i: int, content: str) -> HumanMessage:
    return HumanMessage(content=content, id=f"m{i}")


class TestTokenHistoryWindow:
    @pytest.mark.asyncio
    async def test_budget_zero_is_bit_equal_to_baseline(self):
        """budget=0：与旧条数窗口逐位等价（裁掉头部、保留尾部 N 条）。"""
        node = lg_builder.create_message_window_node(2, 0)
        state = SimpleNamespace(
            messages=[_msg(0, "a" * 400), _msg(1, "b"), _msg(2, "c"), _msg(3, "d")]
        )
        out = await node(state)
        assert [m.id for m in out["messages"]] == ["m0", "m1"]

    @pytest.mark.asyncio
    async def test_within_limits_is_noop(self):
        node = lg_builder.create_message_window_node(10, 0)
        state = SimpleNamespace(messages=[_msg(0, "a"), _msg(1, "b")])
        assert (await node(state))["messages"] == []

    @pytest.mark.asyncio
    async def test_budget_trims_older_tail_first(self):
        """从尾部向前累加：超预算的老消息被裁，最新消息一定保留。"""
        # len//4 估算（CONTEXT_REAL_TOKEN_COUNT_ENABLED 默认关）：400 字符 = 100 token
        node = lg_builder.create_message_window_node(100, 250)
        state = SimpleNamespace(
            messages=[
                _msg(0, "a" * 400),
                _msg(1, "b" * 400),
                _msg(2, "c" * 400),
            ]
        )
        out = await node(state)
        assert [m.id for m in out["messages"]] == ["m0"], "只有最老的 m0 超预算"

    @pytest.mark.asyncio
    async def test_latest_message_never_trimmed(self):
        node = lg_builder.create_message_window_node(100, 1)
        state = SimpleNamespace(messages=[_msg(0, "a" * 400), _msg(1, "b" * 400)])
        out = await node(state)
        # 预算 1 连最新一条都装不下，但最新一条（本轮输入）必须保留；更老的 m0 裁掉
        assert [m.id for m in out["messages"]] == ["m0"]

    @pytest.mark.asyncio
    async def test_counted_once_no_recompute(self, monkeypatch):
        """每条消息只计一次 token（防 O(n²) 重算）：计数器断言。"""
        import agentix.agent.context.token_count as token_count_mod

        calls = {"n": 0}
        real = token_count_mod.count_tokens

        def counting(text: str) -> int:
            calls["n"] += 1
            return real(text)

        monkeypatch.setattr(token_count_mod, "count_tokens", counting)
        node = lg_builder.create_message_window_node(100, 999999)
        state = SimpleNamespace(messages=[_msg(i, "x" * 40) for i in range(6)])
        out = await node(state)
        assert out["messages"] == []
        assert calls["n"] == 6, f"6 条消息应恰好计 6 次，实际 {calls['n']}（存在重算）"

    @pytest.mark.asyncio
    async def test_supervisor_graph_wires_same_factory(self):
        """接线棘轮：supervisor 图的 message_window 也吃 token 预算（同工厂）。"""
        src = (
            REPO / "agentix" / "orchestration" / "langgraph" / "lg_supervisor.py"
        ).read_text(encoding="utf-8")
        assert "LG_TOKEN_HISTORY_BUDGET" in src, "supervisor 未接 token 预算键"


# ---------------------------------------------------------------------------
# D：状态尾注
# ---------------------------------------------------------------------------


class TestStatusTail:
    def test_flag_off_returns_empty(self):
        from agentix.application.agents.general_handler import _status_tail

        state = SimpleNamespace(messages=[HumanMessage(content="hi")])
        assert _status_tail(state, {}) == ""

    def test_flag_on_contains_turn_and_history(self):
        from agentix.application.agents.general_handler import _status_tail

        settings.LG_STATUS_TAIL_ENABLED = True
        try:
            state = SimpleNamespace(
                messages=[HumanMessage(content="q1"), SimpleNamespace(type="ai"),
                          HumanMessage(content="q2")]
            )
            tail = _status_tail(state, {})
        finally:
            settings.LG_STATUS_TAIL_ENABLED = False
        assert tail.startswith("[状态]")
        assert "第 2 轮" in tail
        assert "3 条" in tail

    def test_budget_segment_only_with_data(self):
        from agentix.application.agents.general_handler import _status_tail

        settings.LG_STATUS_TAIL_ENABLED = True
        try:
            state = SimpleNamespace(messages=[HumanMessage(content="hi")])
            without = _status_tail(state, {})
            with_budget = _status_tail(state, {"budget_status": "剩余 1200 token"})
        finally:
            settings.LG_STATUS_TAIL_ENABLED = False
        assert "预算" not in without, "无 ledger 数据时不得伪造预算段"
        assert "剩余 1200 token" in with_budget


# ---------------------------------------------------------------------------
# A1/A4：静态前缀纪律（general 模板指纹 + 守护测试）
# ---------------------------------------------------------------------------


class TestStaticPrefixDiscipline:
    def test_general_is_template_prefix_role_not_static_role(self):
        from agentix.application.agents.llm_factory import (
            _STATIC_PROMPT_ROLES,
            _TEMPLATE_PREFIX_ROLES,
        )

        assert "general" in _TEMPLATE_PREFIX_ROLES
        assert "general" not in _STATIC_PROMPT_ROLES, (
            "general 正文含动态注入，进 _STATIC_PROMPT_ROLES 会逐请求抖动"
        )

    def test_template_extraction_returns_constant(self):
        from agentix.application.agents.lg_prompts import GENERAL_QUERY_SYSTEM_PROMPT
        from agentix.application.agents.llm_factory import _static_prompt_template

        assert _static_prompt_template("general") is GENERAL_QUERY_SYSTEM_PROMPT
        assert _static_prompt_template("router") is None

    def test_general_assembly_has_stable_prefix(self):
        """A4 守护：general 组装序（模板 → logic → 指令/记忆）公共前缀稳定。

        复刻 general_handler 的拼接形态——logic/指令/记忆逐样本变化，
        前缀必须始终落在模板常量上。只进测试，不进热路径。
        """
        from agentix.application.agents.lg_prompts import GENERAL_QUERY_SYSTEM_PROMPT

        def build(sample: dict) -> str:
            parts = [GENERAL_QUERY_SYSTEM_PROMPT.format(logic=sample["logic"])]
            if sample.get("instructions"):
                parts.append(f"以下是本项目/会话的常驻协作约定（AGENTS.md 级联）……\n{sample['instructions']}")
            if sample.get("memory"):
                parts.append(f"用户历史：{sample['memory']}")
            return "\n".join(parts)

        samples = [
            {"logic": "问候类，无检索必要", "instructions": "回答保持简短", "memory": "3 次会话"},
            {"logic": "寒暄/无关问题", "instructions": None, "memory": None},
            {"logic": "另一条完全不同的分类理由", "instructions": "遵循 X 规范", "memory": "偏好 Y"},
        ]
        prefix = assert_static_prefix_stable(
            build, samples, expected_prefix=GENERAL_QUERY_SYSTEM_PROMPT[:20],
            label="general-query",
        )
        assert len(prefix) >= 40
        # 公共前缀必须恰好停在静态/动态边界：<logic> 开标签属于模板常量，
        # 动态的是标签内的分类理由文本——前缀吃进任何 logic 内容即为污染
        assert prefix.endswith("<logic>\n"), (
            f"公共前缀应停在 <logic>\\n 边界，实际结尾：{prefix[-40:]!r}"
        )


# ---------------------------------------------------------------------------
# A2：缓存命中量采集（纯函数 + 指标落点）
# ---------------------------------------------------------------------------


class TestCachedTokensAccounting:
    def test_extracts_langchain_cache_read(self):
        from agentix.application.agents.llm_factory import _extract_cached_tokens

        usage = {"input_token_details": {"cache_read": 128}, "prompt_tokens": 1000}
        assert _extract_cached_tokens(usage) == 128

    def test_extracts_openai_cached_tokens_fallback(self):
        from agentix.application.agents.llm_factory import _extract_cached_tokens

        usage = {"prompt_tokens_details": {"cached_tokens": 64}}
        assert _extract_cached_tokens(usage) == 64

    def test_missing_field_returns_none_not_zero(self):
        from agentix.application.agents.llm_factory import _extract_cached_tokens

        assert _extract_cached_tokens({"prompt_tokens": 100}) is None
        assert _extract_cached_tokens(None) is None
        assert _extract_cached_tokens("nonsense") is None

    def test_cached_label_routes_to_llm_tokens_total(self):
        before = REGISTRY.get_sample_value(
            "llm_tokens_total", {"model": "test-model", "type": "cached"}
        ) or 0.0
        record_llm_tokens(model="test-model", direction="in", value=77, label_type="cached")
        after = REGISTRY.get_sample_value(
            "llm_tokens_total", {"model": "test-model", "type": "cached"}
        ) or 0.0
        assert after - before == 77.0

    def test_zero_or_negative_value_is_noop(self):
        before = REGISTRY.get_sample_value(
            "llm_tokens_total", {"model": "test-model", "type": "cached"}
        ) or 0.0
        record_llm_tokens(model="test-model", direction="in", value=0, label_type="cached")
        after = REGISTRY.get_sample_value(
            "llm_tokens_total", {"model": "test-model", "type": "cached"}
        ) or 0.0
        assert after == before

    def test_cache_hit_miss_recorded_on_prompt_prefix_type(self):
        before_hit = REGISTRY.get_sample_value(
            "cache_hits_total", {"cache_type": "prompt_prefix"}
        ) or 0.0
        before_miss = REGISTRY.get_sample_value(
            "cache_misses_total", {"cache_type": "prompt_prefix"}
        ) or 0.0
        record_cache_hit("prompt_prefix")
        record_cache_miss("prompt_prefix")
        after_hit = REGISTRY.get_sample_value(
            "cache_hits_total", {"cache_type": "prompt_prefix"}
        ) or 0.0
        after_miss = REGISTRY.get_sample_value(
            "cache_misses_total", {"cache_type": "prompt_prefix"}
        ) or 0.0
        assert after_hit - before_hit == 1.0
        assert after_miss - before_miss == 1.0
