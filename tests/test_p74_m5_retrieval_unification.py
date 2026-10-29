"""p74——Stage M·M5 检索域收编的契约与守卫（方案 v2 §5 M5 行 + §6 守卫）。

四组契约：

1. **原生工具循环金测**：``chat_agent_runner`` 剥离 langgraph 后，事件时序
   （model_usage / tool_running / answer_delta / terminated）与旧
   ``create_react_agent`` 路径语义对齐；锁「首轮无工具 = 单 delta、一次模型
   调用」（消除旧 fallback 二次调模型 bug）、工具轮 deep/fast 语义、预算与
   轮次上限终态、流失败 fallback。
2. **单门收敛**：应用侧 ``RetrievalPort`` 装配只允许发生在
   ``knowledge_gateway.get_retrieval_port``（三处散点改指网关）。
3. **旁路清零**：``agentix`` 包外对 ``agentix.retrieval.*`` 的静态 import
   面 = 显式豁免清单（组合根 / 单门 / settings 配置面），零漂移。
4. **共享域迁移**：``legal_effectiveness`` / ``grounding_verify`` 实体迁
   ``agentix/domain/policies/``，旧路径 alias shim 同对象；实体零
   retrieval 依赖；进化保护路径覆盖新家。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.tools import tool

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="replace")


def _parse(rel: str) -> ast.AST:
    return ast.parse(_read(rel))


def _import_modules(rel: str) -> list[str]:
    mods: list[str] = []
    for node in ast.walk(_parse(rel)):
        if isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.append(node.module)
    return mods


# ---------------------------------------------------------------------------
# 假模型 / 假工具（金测桩：无网络、无真实 LLM）
# ---------------------------------------------------------------------------


@tool
def query_knowledge_base(question: str) -> str:
    """检索知识库（金测用，带调用记录）。"""
    return f"doc:{question}"


def _make_echo(record: list[str]):
    @tool
    def echo(text: str) -> str:
        """回声工具（记录调用）。"""
        record.append(text)
        return text + "!"

    return echo


class _FakeLLM:
    """按轮次回放预设 chunk；``bind_tools`` 返回自身。"""

    def __init__(self, rounds: list[list], *, fail_stream: bool = False, fail_all: bool = False):
        self.rounds = [list(r) for r in rounds]
        self.fail_stream = fail_stream
        self.fail_all = fail_all
        self.stream_calls = 0

    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        self.stream_calls += 1
        if self.fail_all or self.fail_stream:
            raise RuntimeError("stream boom")
        chunks = self.rounds.pop(0)
        for spec in chunks:
            yield spec if isinstance(spec, AIMessageChunk) else AIMessageChunk(content=spec)

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        if self.fail_all:
            raise RuntimeError("invoke boom")
        return AIMessage(
            content="fallback answer",
            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )


def _tc_chunk(name: str, args: str, call_id: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": name, "args": args, "id": call_id, "index": 0}],
    )


def _usage_chunk(tokens_in: int, tokens_out: int) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        usage_metadata={
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        },
    )


async def _native(llm, tools, *, deep: bool = True) -> list[dict]:
    from agentix.retrieval.usecases import chat_agent_runner as runner

    return [
        e
        async for e in runner._stream_native_rounds(
            llm=llm,
            tools=tools,
            system_prompt="sys",
            user_text="hi",
            emit_tool_running=deep,
        )
    ]


async def _run(llm, *, chat_mode: str = "fast", monkeypatch) -> list[dict]:
    from agentix.retrieval.usecases import chat_agent_runner as runner

    monkeypatch.setattr(runner, "build_chat_agent_tools", lambda ctx: [query_knowledge_base])
    monkeypatch.setattr(runner, "build_agent_system_prompt", lambda ctx: "sys")
    return [
        e
        async for e in runner.run_agent_stream(
            llm=llm,
            ctx=object(),
            question="q",
            history_text="",
            chat_mode=chat_mode,
        )
    ]


def _kinds(events: list[dict]) -> list[str]:
    return [e.get("kind") for e in events]


# ---------------------------------------------------------------------------
# 1) 原生工具循环金测
# ---------------------------------------------------------------------------


class TestNativeAgentLoopContract:
    @pytest.mark.asyncio
    async def test_direct_answer_is_single_delta_single_model_call(self):
        """首轮无工具：整文单 delta、模型只调一次（修旧 ainvoke 二次调模型 bug）。"""
        llm = _FakeLLM([[AIMessageChunk(content="Hello "), AIMessageChunk(content="world")]])
        events = await _native(llm, [query_knowledge_base])
        assert _kinds(events) == ["answer_delta"], events
        assert events[0]["content"] == "Hello world"
        assert llm.stream_calls == 1 and not llm.rounds

    @pytest.mark.asyncio
    async def test_usage_emitted_before_answer_delta(self):
        """无工具轮的 usage 先于缓冲 delta（对齐旧 updates → fallback 时序）。"""
        llm = _FakeLLM([[_usage_chunk(10, 5), AIMessageChunk(content="答案")]])
        events = await _native(llm, [query_knowledge_base])
        assert _kinds(events) == ["model_usage", "answer_delta"], events
        assert events[0]["tokens_in"] == 10 and events[0]["tokens_out"] == 5
        assert events[1]["tokens_in"] == 0 and events[1]["tokens_out"] == 0

    @pytest.mark.asyncio
    async def test_fast_mode_runs_tools_without_tool_running(self):
        """fast：工具照跑但不推 tool_running；答案增量实时流出。"""
        record: list[str] = []
        llm = _FakeLLM(
            [
                [_tc_chunk("echo", '{"text": "x"}', "c1")],
                [AIMessageChunk(content="Ans"), AIMessageChunk(content="wer")],
            ]
        )
        events = await _native(llm, [_make_echo(record)], deep=False)
        assert _kinds(events) == ["answer_delta", "answer_delta"], events
        assert [e["content"] for e in events] == ["Ans", "wer"]
        assert record == ["x"], "工具必须真实执行"

    @pytest.mark.asyncio
    async def test_deep_mode_tool_running_with_frozen_label_and_announced_once(self):
        """deep：tool_running 用冻结文案且每工具名只播一次（跨轮去重）。"""
        llm = _FakeLLM(
            [
                [_tc_chunk("query_knowledge_base", '{"question": "a"}', "c1")],
                [_tc_chunk("query_knowledge_base", '{"question": "b"}', "c2")],
                [AIMessageChunk(content="终答")],
            ]
        )
        events = await _native(llm, [query_knowledge_base], deep=True)
        running = [e for e in events if e["kind"] == "tool_running"]
        assert len(running) == 1, events
        assert running[0]["content"] == "正在检索知识库…"
        assert _kinds(events)[-1] == "answer_delta"

    @pytest.mark.asyncio
    async def test_tool_labels_are_frozen(self):
        """工具提示文案冻结（前端契约，防漂移）。"""
        from agentix.retrieval.usecases.chat_agent_runner import _TOOL_LABELS

        assert _TOOL_LABELS == {
            "count_scope_documents": "正在统计文档数量…",
            "list_scope_documents": "正在列出文档…",
            "lookup_scope_document": "正在查找文档…",
            "query_knowledge_base": "正在检索知识库…",
            "search_documents": "正在搜索文档片段…",
        }

    @pytest.mark.asyncio
    async def test_model_usage_precedes_tool_execution(self, monkeypatch):
        """输入预算检查点在工具执行前：超限 → 终止且工具不跑。"""
        from agentix.retrieval.settings import settings

        record: list[str] = []
        llm = _FakeLLM([[_usage_chunk(100, 1), _tc_chunk("echo", '{"text": "x"}', "c1")]])
        monkeypatch.setattr(settings, "AGENT_MAX_INPUT_TOKENS", 50)
        events = await _run(llm, monkeypatch=monkeypatch)
        assert _kinds(events) == ["terminated"], events
        assert events[0]["terminate_reason"] == "budget_exceeded"
        assert record == [], "预算终止后不得执行工具"

    @pytest.mark.asyncio
    async def test_output_budget_truncates_with_hint(self, monkeypatch):
        """输出预算：delta 已发出后追加截断提示（terminate_reason=budget_exceeded）。"""
        from agentix.retrieval.settings import settings

        monkeypatch.setattr(settings, "AGENT_MAX_OUTPUT_TOKENS", 2)
        llm = _FakeLLM([[_usage_chunk(1, 99), AIMessageChunk(content="xy")]])
        events = await _run(llm, monkeypatch=monkeypatch)
        assert _kinds(events) == ["answer_delta", "terminated"], events
        assert events[1]["terminate_reason"] == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_recursion_limit_emits_hint_then_terminated(self, monkeypatch):
        """步数（模型步+工具步）对照 AGENT_RECURSION_LIMIT：超限给提示并终止，不 fallback。"""
        from agentix.retrieval.settings import settings
        from agentix.retrieval.usecases.chat_agent_runner import _RECURSION_HINT

        monkeypatch.setattr(settings, "AGENT_RECURSION_LIMIT", 2)
        llm = _FakeLLM(
            [
                [_tc_chunk("query_knowledge_base", '{"question": "a"}', "c1")],
                [_tc_chunk("query_knowledge_base", '{"question": "b"}', "c2")],
                [AIMessageChunk(content="不应到达")],
            ]
        )
        events = await _run(llm, monkeypatch=monkeypatch)
        assert _kinds(events) == ["answer_delta", "terminated"], events
        assert events[0]["content"] == _RECURSION_HINT
        assert events[1]["terminate_reason"] == "recursion_limit"
        assert llm.rounds, "超限后不得继续消费模型轮次"

    @pytest.mark.asyncio
    async def test_stream_failure_falls_back_to_single_delta_with_usage(self, monkeypatch):
        """流失败 → 非流式 fallback：单 delta + 真实 usage（预算从此可核算）。"""
        llm = _FakeLLM([], fail_stream=True)
        events = await _run(llm, monkeypatch=monkeypatch)
        assert _kinds(events) == ["answer_delta"], events
        assert events[0]["content"] == "fallback answer"
        assert (events[0]["tokens_in"], events[0]["tokens_out"]) == (7, 3)

    @pytest.mark.asyncio
    async def test_total_failure_yields_error_delta(self, monkeypatch):
        """流与 fallback 皆失败：错误兜底文案（与旧路径同款）。"""
        llm = _FakeLLM([], fail_all=True)
        events = await _run(llm, monkeypatch=monkeypatch)
        assert _kinds(events) == ["answer_delta"], events
        assert events[0]["content"] == "处理问题时出错，请稍后重试。"

    @pytest.mark.asyncio
    async def test_tool_exception_becomes_error_tool_message_and_loop_continues(self):
        """工具异常 → error ToolMessage 回注模型，循环继续到终答（不炸流）。"""
        from langchain_core.tools import tool

        @tool
        def broken_tool() -> str:
            """必然失败的工具。"""
            raise RuntimeError("boom")

        llm = _FakeLLM(
            [
                [_tc_chunk("broken_tool", "{}", "c1")],
                [AIMessageChunk(content="已恢复")],
            ]
        )
        events = await _native(llm, [broken_tool], deep=False)
        assert _kinds(events) == ["answer_delta"], events
        assert events[0]["content"] == "已恢复"

    @pytest.mark.asyncio
    async def test_event_kind_vocabulary_frozen(self):
        """事件词汇表冻结：四 kind 外零新增（SSE 投影层契约）。"""
        record: list[str] = []
        llm = _FakeLLM(
            [
                [_usage_chunk(1, 1), _tc_chunk("echo", '{"text": "x"}', "c1")],
                [AIMessageChunk(content="答")],
            ]
        )
        events = await _native(llm, [_make_echo(record)], deep=True)
        assert set(_kinds(events)) <= {"tool_running", "answer_delta", "model_usage", "terminated"}


class TestRunnerLangGraphFree:
    def test_runner_has_no_langgraph_or_langchain_agents_import(self):
        """M5：chat_agent_runner 静态零 langgraph / langchain.agents 导入。"""
        tree = _parse("agentix/retrieval/usecases/chat_agent_runner.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for m in names:
                assert not (m == "langgraph" or m.startswith("langgraph.")), m
                assert not (m == "langchain.agents" or m.startswith("langchain.agents")), m

    def test_retrieval_package_is_langgraph_free(self):
        """retrieval 域整体不依赖 langgraph（p66 白名单 11→10 的域内断言）。"""
        offenders: list[str] = []
        for f in sorted((REPO / "agentix" / "retrieval").rglob("*.py")):
            rel = f.relative_to(REPO).as_posix()
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                if any(n == "langgraph" or n.startswith("langgraph.") for n in names):
                    offenders.append(rel)
        assert offenders == [], f"retrieval 出现 langgraph import：{offenders}"


# ---------------------------------------------------------------------------
# 2) 单门收敛
# ---------------------------------------------------------------------------


class TestRetrievalPortSingleDoor:
    def test_assembly_point_only_inside_knowledge_gateway(self):
        """应用侧直连 ``retrieval_port_adapter`` 的文件 == 网关单点。"""
        offenders: list[str] = []
        for f in sorted((REPO / "agentix" / "application").rglob("*.py")):
            rel = f.relative_to(REPO).as_posix()
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == (
                    "agentix.retrieval.usecases.retrieval_port_adapter"
                ):
                    offenders.append(rel)
                elif isinstance(node, ast.Import):
                    offenders.extend(
                        rel
                        for a in node.names
                        if a.name == "agentix.retrieval.usecases.retrieval_port_adapter"
                    )
        assert sorted(set(offenders)) == [
            "agentix/application/services/knowledge_gateway.py",
        ], f"Port 装配必须收敛到网关单文件：{sorted(set(offenders))}"

    def test_three_scattered_callers_take_port_from_gateway(self):
        """file_handler / general_handler / kb_tools.node 经网关取 Port。"""
        gateway = "agentix.application.services.knowledge_gateway"
        for rel in (
            "agentix/application/agents/file_handler.py",
            "agentix/application/agents/general_handler.py",
            "agentix/application/agents/kb_tools/node.py",
        ):
            tree = _parse(rel)
            picked = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == gateway:
                    if any(a.name == "get_retrieval_port" for a in node.names):
                        picked = True
                if isinstance(node, ast.Import):
                    picked = picked or any(
                        a.name == gateway for a in node.names
                    )
            assert picked, f"{rel} 必须从 knowledge_gateway 导入 get_retrieval_port"

    @pytest.mark.asyncio
    async def test_get_retrieval_port_returns_adapter(self):
        from agentix.application.services.knowledge_gateway import get_retrieval_port
        from agentix.retrieval.usecases.retrieval_port_adapter import RetrievalPortAdapter

        port = get_retrieval_port()
        assert isinstance(port, RetrievalPortAdapter)


# ---------------------------------------------------------------------------
# 3) 旁路清零（静态 import 面 = 豁免清单）
# ---------------------------------------------------------------------------

#: 包外对 ``agentix.retrieval.*`` 的显式豁免（M5 评审冻结；增行须走批次评审）：
#: main 组合根 / 网关单门 / settings 配置面。
RETRIEVAL_BYPASS_ALLOW: dict[str, frozenset[str]] = {
    "agentix/main.py": frozenset(
        {
            "agentix.retrieval.api.routers",
            "agentix.retrieval.api.response",
            "agentix.retrieval.errors",
            "agentix.retrieval.settings",
            "agentix.retrieval.infra.db.session",
            # E4 批次评审：embedding 缓存启动预热（flag 默认关）。导入刻意留在
            # ``_embedding_prewarm_worker`` 函数体内——依赖缺失/循环导入也要 fail-open
            # （纪律 2），所以组合根这一行豁免按"启动预热接线"登记，不是新旁路。
            "agentix.retrieval.infra.embedding.prewarm",
        }
    ),
    "agentix/application/services/knowledge_gateway.py": frozenset(
        {
            "agentix.retrieval.usecases.retrieval_port_adapter",
            "agentix.retrieval.settings",
        }
    ),
    "agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/cypher_tools/node.py": frozenset(
        {"agentix.retrieval.settings"}
    ),
    # M2-1（拆边）：原此处两条 `"agentix/observability/logging_config.py"` /
    # `"agentix/observability/trajectory.py"` → `agentix.retrieval.settings` 的豁免**已删除**。
    # 理由：这两行是把「observability→retrieval.settings」这条旧架构边硬编码进豁免清单；
    # 三键真源上提到 config.settings、observability 直读 config 之后，边消失 ⇒ 豁免应当消失
    # （`test_outside_import_surface_equals_frozen_allowlist` 用的是相等棘轮
    # `used == allowed_pairs`，留着就是僵尸豁免、断言必红）。删豁免是**收紧**，
    # 判据本身一字未动，也没有删/弱化任何既有守卫。
    # F1b·M-P1-1（Stage F 批次评审）：进程内记忆 provider 复用检索域向量栈
    # 做 embedding（embed_texts_sync，httpx 直连，不另建框架）。
    "agentix/agent/memory/service.py": frozenset(
        {"agentix.retrieval.infra.rag.dense"}
    ),
}


def _allowed(mod: str, allow: frozenset[str]) -> bool:
    return any(mod == a or mod.startswith(a + ".") for a in allow)


class TestBypassZero:
    def test_outside_import_surface_equals_frozen_allowlist(self):
        """``agentix/**``（retrieval 包外）静态 import ``agentix.retrieval.*``
        的 (file, module) 集合 == 豁免清单（equality 棘轮：旁路清零且无僵尸豁免）。"""
        offenders: list[str] = []
        used: set[tuple[str, str]] = set()
        for f in sorted((REPO / "agentix").rglob("*.py")):
            rel = f.relative_to(REPO).as_posix()
            if rel.startswith("agentix/retrieval/"):
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for m in names:
                    if m != "agentix.retrieval" and not m.startswith("agentix.retrieval."):
                        continue
                    allow = RETRIEVAL_BYPASS_ALLOW.get(rel, frozenset())
                    if _allowed(m, allow):
                        used.add((rel, m))
                    else:
                        offenders.append(f"{rel} -> {m}")
        assert offenders == [], f"retrieval 旁路未清零（新增须入 RETRIEVAL_BYPASS_ALLOW）：{offenders}"
        allowed_pairs = {
            (rel, m)
            for rel, mods in RETRIEVAL_BYPASS_ALLOW.items()
            for m in mods
        }
        assert used == allowed_pairs, (
            f"豁免清单漂移：新出现={sorted(used - allowed_pairs)} "
            f"僵尸={sorted(allowed_pairs - used)}"
        )


# ---------------------------------------------------------------------------
# 4) 共享域迁移 + alias shim + 进化保护
# ---------------------------------------------------------------------------

_MIGRATED = {
    "agentix/retrieval/domain/policies/legal_effectiveness.py": (
        "agentix.domain.policies.legal_effectiveness"
    ),
    "agentix/retrieval/domain/services/grounding_verify.py": (
        "agentix.domain.policies.grounding_verify"
    ),
}


class TestSharedDomainMigration:
    @pytest.mark.parametrize("shim_rel,target", sorted(_MIGRATED.items()))
    def test_old_path_shim_removed(self, shim_rel: str, target: str):
        """M6 清尾：旧路径 shim 文件已删除，规范位是唯一入口。"""
        import importlib

        assert not (REPO / shim_rel).exists(), f"M6 应删除的旧路径残留: {shim_rel}"
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(shim_rel[:-3].replace("/", "."))

    @pytest.mark.parametrize("shim_rel,target", sorted(_MIGRATED.items()))
    def test_shim_target_constant_gone(self, shim_rel: str, target: str):
        assert not (REPO / shim_rel).exists(), f"旧路径 shim 复活: {shim_rel}"

    @pytest.mark.parametrize(
        "rel",
        [
            "agentix/domain/policies/legal_effectiveness.py",
            "agentix/domain/policies/grounding_verify.py",
        ],
    )
    def test_moved_modules_have_zero_retrieval_dependency(self, rel: str):
        """迁出实体（纯函数）零 retrieval 依赖——包外禁 domain 旁路的对偶面。"""
        for m in _import_modules(rel):
            assert not (m == "agentix.retrieval" or m.startswith("agentix.retrieval.")), m

    def test_evolution_protected_paths_cover_real_verifier_logic(self):
        """PROTECTED_VERIFIER_PATHS 覆盖真实逻辑路径（M6 拆 shim 后收缩至 4 条实体）。"""
        from agentix.agent.evolution.loop import PROTECTED_VERIFIER_PATHS

        assert PROTECTED_VERIFIER_PATHS == frozenset(
            {
                "agentix/policies/verifier/layered.py",
                "agentix/policies/verifier/structural.py",
                "agentix/domain/policies/grounding_verify.py",
                "agentix/domain/policies/completion.py",
            }
        )
