"""A6 分层守卫定稿 p66——架构重构统一执行链 §6 十二条守卫的测试化收敛。

口径：守卫层十二条，守卫基线只减不增，豁免必须显式登记。

12 条守卫 → 承载测试：

==== ==========================================================
 #1   单一运行平面 / 单一出口 → 本文件 ``TestCrossPathSinglePlane``
      （chat + retrieval 双流同平面、distinct run_id、跨路径
      重复 spawn 拒绝）+ test_p61（spawn 单 owner）
 #2   Gateway 唯一对外出口   → test_p62_unified_gateway
 #3   能力层不反依赖上层      → ``TestCapabilityLayering``
 #4   适配层无业务 DB、runtime 非 checkpoint 源 → ``TestAdapterLayering``
 #5   流式统一词汇 + run 登记 → test_p65_unified_sse（R5a-R5d）
 #6   应用核心不直连客户端    → ``TestAppCoreNoDirectClients``
 #7   promote 只在离线进化    → ``TestOnlinePromoteForbidden``
 #8   断路器/降级/超时全覆盖  → test_p31* + test_p61（idle 超时）
 #9   replan 有界 + 预算兜底  → ``TestReplanBoundedByBudget``
 #10  事实表 / 门禁回灌       → test_p32* + test_p33*
 #11  LangGraph 白名单棘轮    → ``TestLangGraphWhitelistRatchet``
 #12  strangler shim 集合棘轮 → ``TestShimDirRatchet``
==== ==========================================================

纪律（§7）：两把 **equality 棘轮**（#11 / #12）与显式豁免登记（#6）是可
grep 审计的常量——改动基线必须走批次评审，禁止"顺手加白名单"。#11 的
白名单随 M 批次图迁移收缩（应用侧 langgraph 文件迁往 orchestration 时同步
评审更新）；#12 终态为**空集**（shim 全部拆除）。
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from agentix.agent import AgentLoop, LoopResult
from agentix.agent.context import SimpleContextBuilder
from agentix.application.services.chat_service import ChatService
from agentix.application.services.trajectory import reset_trajectory_recorder
from agentix.domain.agent.types import TaskSpec
from agentix.orchestration.executor import MappingStepExecutor
from agentix.policies.planner import DeclaredStepsPlanner, RetryReplanner
from agentix.runtime.control_plane import (
    get_control_plane,
    reset_control_plane,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_plane():
    """每个用例独立的 ControlPlane / 轨迹记录器单例。"""
    reset_control_plane()
    reset_trajectory_recorder()
    yield
    reset_control_plane()
    reset_trajectory_recorder()


# ---------------------------------------------------------------------------
# AST 扫描工具
# ---------------------------------------------------------------------------


def _iter_py(sub: str) -> List[Path]:
    return sorted((REPO / sub).rglob("*.py"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse(path: Path) -> ast.AST | None:
    try:
        return ast.parse(_read(path))
    except SyntaxError:
        return None


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _import_modules(tree: ast.AST) -> List[str]:
    """顶层（level==0）import 模块名；相对导入不计。"""
    mods: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.append(node.module)
    return mods


def _is_forbidden(mod: str, forbidden: tuple) -> bool:
    return any(mod == f or mod.startswith(f + ".") for f in forbidden)


# ---------------------------------------------------------------------------
# #11 — LangGraph 白名单棘轮（equality：基线只减不增）
# ---------------------------------------------------------------------------

#: 扫描根：生产代码 + 根入口 *.py（tests / web / .venv 不在被守护面）。
LANGGRAPH_SCAN_ROOTS = ("agentix", "scripts", "evals", "kb_ingest", "archive")

#: 冻结白名单（A6 死代码批后 = 11 文件）。**equality 断言**——M2（2026-10-02）
#: 已把应用侧 lg_{builder,states,supervisor} 迁往 orchestration/langgraph/ 并随批
#: 刷新本集合（lg_prompts 无 langgraph 导入，业务 prompt 留 application）；
#: M5（2026-10-03）剥离 retrieval 执行链 → chat_agent_runner 去 langgraph，11 → 10；
#: 禁止在未评审的改动里直接增行（§7 只减不增）。
#: LG-0-3（2026-10-27）例外增行 1 条：`orchestration/langgraph/policies.py` 是指导文档
#: §4.1 LG-0-3 点名的**策略集中点**（LangGraph 的 RetryPolicy/TimeoutPolicy 只许在这里
#: 构造，见 tests/test_p147_policy_centralization.py）。它天然落在 langgraph 适配层目录内，
#: 与 lg_builder / lg_states / lg_supervisor 同族 ⇒ 属该守卫注释里「随批刷新本集合」的
#: 已评审路径；equality 棘轮本身**未放宽**（多一个文件就要多一行，仍需署名理由）。
LANGGRAPH_ALLOWED: frozenset = frozenset(
    {
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/tool_selection/node.py",
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/edges.py",
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/kb_workflow.py",
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/multi_tool.py",
        "agentix/application/agents/main.py",
        "agentix/application/agents/text2sql/workflow.py",
        "agentix/infrastructure/persistence/checkpointer.py",
        "agentix/orchestration/langgraph/lg_builder.py",
        "agentix/orchestration/langgraph/lg_states.py",
        "agentix/orchestration/langgraph/lg_supervisor.py",
        "agentix/orchestration/langgraph/policies.py",
    }
)


def _langgraph_importers() -> Set[str]:
    hits: Set[str] = set()
    files: List[Path] = []
    for root in LANGGRAPH_SCAN_ROOTS:
        p = REPO / root
        if p.is_dir():
            files.extend(_iter_py(root))
    files.extend(sorted(REPO.glob("*.py")))
    for f in files:
        tree = _parse(f)
        if tree is None:
            continue
        if any(_is_forbidden(m, ("langgraph",)) for m in _import_modules(tree)):
            hits.add(_rel(f))
    return hits


class TestLangGraphWhitelistRatchet:
    def test_langgraph_imports_equal_frozen_whitelist(self):
        """守卫 #11：全仓 langgraph import 文件集合 == 冻结白名单（equality 棘轮）。"""
        actual = _langgraph_importers()
        added = sorted(actual - LANGGRAPH_ALLOWED)
        removed = sorted(LANGGRAPH_ALLOWED - actual)
        assert not added and not removed, (
            f"langgraph 白名单漂移：新增={added} 消失={removed}；"
            "基线改动必须随批次评审更新 LANGGRAPH_ALLOWED（§7 只减不增）"
        )


# ---------------------------------------------------------------------------
# #12 — strangler shim 目录集合棘轮（equality，终态空集）
# ---------------------------------------------------------------------------

#: 当前 shim 目录（p36 `_TARGET` 机制；application/runtime 的 A4 shim 已拆）。
#: M1–M5 逐批降级转发至规范位（capabilities/tools、agent/{core,executor,
#: multiagent,planner,replanner,verifier,hitl}、application/agents/lg_*、
#: application/services/{hitl_bridge,control_plane}、retrieval/domain/{policies,
#: services} 等，详见 p69 manifest）。
#: **M6（2026-10-03）三簇 shim 全数删除——终态 `== set()` 已达成**；
#: 新增任何 `_TARGET` 转发层都必须走 §7 批次评审（棘轮只减不增）。
SHIM_DIRS_ALLOWED: frozenset = frozenset()


def _shim_dirs() -> Set[str]:
    """模块级 ``_TARGET = "..."`` 字符串赋值所在的目录集合（posix 相对路径）。

    覆盖面只到此形态为止，这是**裁决而不是遗漏**（2026-11-01 编排方裁决）：本判据认的仍是「模块级 ``_TARGET`` 字符串
    赋值」这一种形态；模块级**再导出**形态
    （``from x import (...)  # noqa: F401``，例如
    ``agentix/application/agents/kg_sub_graph/kg_tools_list.py``
    把 4 个参数 schema 再导出到规范位 ``agentix.tools.kg_arg_schemas``）
    **不纳入**。
    扩面要把仓里现存的合法再导出逐条进允许集并各写理由，成本高于收益；
    而守卫 #12 设计目的是盯 ``_TARGET`` 转发层的删除进度（M6 终态空集），
    再导出 shim 不是它要消灭的对象。
    缺口靠这段文字自身可见，不靠台账兜底。
    """
    hits: Set[str] = set()
    for f in _iter_py("agentix"):
        tree = _parse(f)
        if tree is None:
            continue
        for node in tree.body:  # 模块级（不递归函数体）
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "_TARGET" for t in node.targets
            ):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                hits.add(f.parent.relative_to(REPO).as_posix())
    return hits


class TestShimDirRatchet:
    def test_shim_dirs_equal_frozen_set(self):
        """守卫 #12：`_TARGET` shim 目录集合 == 冻结集合（equality 棘轮）。"""
        actual = _shim_dirs()
        added = sorted(actual - SHIM_DIRS_ALLOWED)
        removed = sorted(SHIM_DIRS_ALLOWED - actual)
        assert not added and not removed, (
            f"shim 目录集合漂移：新增={added} 消失={removed}；"
            "M6 终态为空集，任何 `_TARGET` 转发层新增须走 §7 批次评审"
        )


# ---------------------------------------------------------------------------
# #3 — 能力层不反依赖上层
# ---------------------------------------------------------------------------

CAPABILITY_FORBIDDEN: tuple = (
    "agentix.agent",
    "agentix.orchestration",
    "agentix.policies",
)

#: 唯一登记豁免（§7 批次评审口径）：``agentix/tools/gateway/executor.py`` 对
#: ``agentix.policies.guardrails`` 的边——蓝图 §5 Tool Gateway 链本就含
#: Guardrails 步（「经 Tool Gateway（唯一对外出口，Guardrails 打 Risk 级 …）」），
#: 项目实现手册 §9.5 M3 注记「M6 清尾时同步调整守卫与 import」即指此边：
#: 迁移期该 import 走 shim 转发以满足守卫，M6 删 shim 后改为**显式窄豁免**。
#: 豁免精确到模块前缀；新增豁免一律走 §7 批次评审。
TOOLS_GUARDRAILS_EDGE_ALLOWED: tuple = ("agentix.policies.guardrails",)


class TestCapabilityLayering:
    def test_capabilities_never_import_upper_layers(self):
        """守卫 #3：``capabilities/**`` 与 ``tools/**`` 对 agent / orchestration / policies 零 import。

        M1 起能力层实体住 ``agentix/tools``（旧路径 capabilities/tools 迁移期为
        shim，M6 已删），守卫覆盖两处目录；仅 ``tools/gateway/executor.py`` →
        ``policies.guardrails`` 一条边按 ``TOOLS_GUARDRAILS_EDGE_ALLOWED`` 豁免
        （蓝图 §5 Tool Gateway 链的 Guardrails 步）。
        """
        offenders: List[str] = []
        for root in ("agentix/capabilities", "agentix/tools"):
            for f in _iter_py(root):
                tree = _parse(f)
                if tree is None:
                    continue
                for m in _import_modules(tree):
                    if not _is_forbidden(m, CAPABILITY_FORBIDDEN):
                        continue
                    if root == "agentix/tools" and _is_forbidden(
                        m, TOOLS_GUARDRAILS_EDGE_ALLOWED
                    ):
                        continue
                    offenders.append(f"{_rel(f)} -> {m}")
        assert offenders == [], f"能力层出现反依赖上层 import：{offenders}"


# ---------------------------------------------------------------------------
# #4 — 适配层无业务 DB / runtime 非 checkpoint 源
# ---------------------------------------------------------------------------

ADAPTER_DB_FORBIDDEN: tuple = (
    "agentix.infrastructure.persistence",
    "agentix.infrastructure.core.database",
)


class TestAdapterLayering:
    def test_graph_adapters_never_touch_business_db(self):
        """守卫 #4a：``application/agents/**``（图编排适配层）零业务 DB 接触。"""
        offenders: List[str] = []
        for f in _iter_py("agentix/application/agents"):
            tree = _parse(f)
            if tree is None:
                continue
            for m in _import_modules(tree):
                if _is_forbidden(m, ADAPTER_DB_FORBIDDEN):
                    offenders.append(f"{_rel(f)} -> import {m}")
            for node in ast.walk(tree):  # SessionLocal 全局句柄也禁止
                if isinstance(node, ast.Name) and node.id == "SessionLocal":
                    offenders.append(f"{_rel(f)} -> Name SessionLocal")
        assert offenders == [], f"适配层出现业务 DB 接触：{offenders}"

    def test_runtime_layer_never_imports_langgraph(self):
        """守卫 #4b：``runtime/**`` 不以 langgraph 为 checkpoint 运行时源。"""
        offenders: List[str] = []
        for f in _iter_py("agentix/runtime"):
            tree = _parse(f)
            if tree is None:
                continue
            for m in _import_modules(tree):
                if _is_forbidden(m, ("langgraph",)):
                    offenders.append(f"{_rel(f)} -> {m}")
        assert offenders == [], f"runtime 出现 langgraph import：{offenders}"


# ---------------------------------------------------------------------------
# #6 — 应用核心不直连 Neo4j / Milvus / MCP 客户端
# ---------------------------------------------------------------------------

APP_CLIENT_FORBIDDEN: frozenset = frozenset(
    {
        "pymilvus",
        "neo4j",
        "langchain_neo4j",
        "langchain_community",
        "langchain_mcp",
        "mcp",
    }
)

#: 显式豁免登记（§7）：``application/agents/`` = 图编排子树（lg_* / 图节点），
#: 是 M2 的迁移对象——登记可见、随迁收缩。**新豁免必须在批次评审里加行。**
APP_CLIENT_EXEMPT_PREFIXES: tuple = ("agentix/application/agents/",)


class TestAppCoreNoDirectClients:
    def test_usecase_services_never_touch_clients(self):
        """守卫 #6：应用核心（services/usecases）零 Neo4j/Milvus/MCP 直连。"""
        offenders: List[str] = []
        for f in _iter_py("agentix/application"):
            rel = _rel(f)
            if rel.startswith(APP_CLIENT_EXEMPT_PREFIXES):
                continue
            tree = _parse(f)
            if tree is None:
                continue
            base_names = {m.split(".")[0] for m in _import_modules(tree)}
            for name in sorted(base_names & APP_CLIENT_FORBIDDEN):
                offenders.append(f"{rel} -> {name}")
        assert offenders == [], (
            f"应用核心出现客户端直连：{offenders}"
            f"（豁免前缀={APP_CLIENT_EXEMPT_PREFIXES}，M2 迁出后收缩）"
        )


# ---------------------------------------------------------------------------
# #7 — promote / run_regression 只在离线进化
# ---------------------------------------------------------------------------

ONLINE_PROMOTE_RE = re.compile(r"\.(?:promote|run_regression)\s*\(")
PROMOTE_ALLOWED_PREFIX: tuple = ("agentix/agent/evolution/",)


class TestOnlinePromoteForbidden:
    def test_promote_only_inside_offline_evolution(self):
        """守卫 #7：``.promote`` / ``.run_regression`` 调用点只允许离线进化包内。"""
        offenders: List[str] = []
        for f in _iter_py("agentix"):
            rel = _rel(f)
            if rel.startswith(PROMOTE_ALLOWED_PREFIX):
                continue
            for i, line in enumerate(_read(f).splitlines(), 1):
                if ONLINE_PROMOTE_RE.search(line):
                    offenders.append(f"{rel}:{i}")
        assert offenders == [], (
            f"在线路径出现 promote/run_regression 调用：{offenders}"
            "（staged-only propose 可在线，promote 只在离线）"
        )


# ---------------------------------------------------------------------------
# #9 — replan 有界 + 步预算兜底（不挂起）
# ---------------------------------------------------------------------------


class _AlwaysPassVerifier:
    async def verify(self, task, plan, observations):
        from agentix.domain.agent.types import VerificationResult

        return VerificationResult(passed=True, reason="always")


def _boom_tool(**kwargs):
    raise RuntimeError("boom")


class TestReplanBoundedByBudget:
    @pytest.mark.asyncio
    async def test_replan_loop_bounded_by_step_budget_no_hang(self):
        """守卫 #9：replan 可反复触发，但步预算先触顶——``budget_exceeded`` 终态、不挂起。"""
        task = TaskSpec(
            goal="loop",
            task_id="p66-replan",
            constraints={
                "steps": [
                    {"id": "a", "action": "act", "tool": "act"},
                    {"id": "b", "action": "act", "tool": "act"},
                    {"id": "c", "action": "act", "tool": "act"},
                ]
            },
        )
        loop = AgentLoop(
            planner=DeclaredStepsPlanner(),
            context=SimpleContextBuilder(),
            verifier=_AlwaysPassVerifier(),
            executor=MappingStepExecutor({"act": _boom_tool}),
            max_steps=2,
            max_replans=5,
            replanner=RetryReplanner(max_replans=5, same_failure_threshold=99),
        )
        # 挂起即超时失败——重规划循环必须被步预算截断
        result: LoopResult = await asyncio.wait_for(loop.run(task), timeout=3.0)
        assert result.terminal == "budget_exceeded"
        assert result.passed is False
        assert result.steps_used == 2
        assert result.replans < 5, "预算必须先于重规划上限触顶"


# ---------------------------------------------------------------------------
# #1 — 跨路径单一运行平面（chat + retrieval 双流）
# ---------------------------------------------------------------------------


class _FakeChunk:
    def __init__(self, content: Any) -> None:
        self.content = content


class _StubStreamGraph:
    """按顺序回放预设 LangGraph 事件（chat 侧零 LLM/Docker 桩）。"""

    def __init__(self, events: List[Dict[str, Any]]):
        self._events = events

    def astream_events(self, input_state, *, config=None, version=None):
        async def _gen():
            for ev in self._events:
                yield ev

        return _gen()


def _ev(
    kind: str,
    name: str | None = None,
    data: Dict | None = None,
    metadata: Dict | None = None,
) -> Dict[str, Any]:
    return {"event": kind, "name": name, "data": data or {}, "metadata": metadata or {}}


def _success_events() -> List[Dict[str, Any]]:
    """路由 → 工具开始/结束 → ≥6 字 token → 最外层图结束（与 p65 同款编排）。"""
    answer = "这是流式测试回答内容"
    return [
        _ev(
            "on_chain_end",
            name="analyze_and_route_query",
            data={"output": {"router": {"type": "legal", "logic": "kb-query"}}},
            metadata={"langgraph_node": "analyze_and_route_query"},
        ),
        _ev("on_tool_start", name="milvus_search"),
        _ev(
            "on_chat_model_stream",
            name="ChatModel",
            data={"chunk": _FakeChunk(answer)},
            metadata={"langgraph_node": "create_kb_query"},
        ),
        _ev("on_tool_end", name="milvus_search"),
        {
            "event": "on_chain_end",
            "name": "LangGraph",
            "data": {"output": {"sources": [{"document_id": "d1", "source": "d1"}]}},
            "metadata": {},
        },
    ]


async def _collect_chat(session_id: str = "s-p66") -> List[Dict[str, Any]]:
    """经 ``stream_events`` 投影层（run-scoped emitter 戳记）收集全部帧。"""
    graph = _StubStreamGraph(_success_events())
    service = ChatService(graph_getter=lambda: graph)
    return [ev async for ev in service.stream_events("问题", session_id)]


class _FakeMsg:
    id = 42


def _patch_retrieval_stream(monkeypatch) -> None:
    """把 retrieval 流内部依赖替换为离线桩（与 p65 同款）。"""
    from agentix.retrieval.settings import settings as retrieval_settings
    from agentix.retrieval.usecases import chat_service as cs
    from agentix.retrieval.usecases import chat_service_helpers as helpers

    monkeypatch.setattr(retrieval_settings, "TRAJECTORY_ENABLED", False)
    monkeypatch.setattr(
        cs,
        "_prepare_conversation_and_scope",
        lambda *a, **kw: ("cid-1", _FakeMsg(), "kb", "kb-1"),
    )
    monkeypatch.setattr(
        cs, "_ensure_kb_ready_or_reject", lambda *a, **kw: (object(), None, None, None)
    )
    monkeypatch.setattr(cs, "count_documents_in_scope", lambda *a, **kw: 3)

    async def fake_prepare_context(*a, **kw):
        return {"history_text": "", "scope_label": ""}

    async def fake_preflight(*a, **kw):
        return None

    final_frames = [
        {
            "type": "answer_chunk",
            "content": "这是流式测试回答内容",
            "request_id": "req-p66",
            "finished": False,
        },
        {
            "type": "answer_chunk",
            "content": "",
            "request_id": "req-p66",
            "finished": True,
            "conversation_id": "cid-1",
            "message_id": 1,
            "answer_id": 2,
            "citations": [],
        },
    ]

    async def fake_final(*a, **kw):
        for frame in final_frames:
            yield frame

    monkeypatch.setattr(helpers, "_prepare_context", fake_prepare_context)
    monkeypatch.setattr(helpers, "_stream_retrieval_preflight", fake_preflight)
    monkeypatch.setattr(helpers, "_stream_final_answer", fake_final)


async def _collect_retrieval() -> List[Dict[str, Any]]:
    from agentix.retrieval.usecases import chat_service as cs

    return [
        p
        async for p in cs._stream_answer_impl(
            object(),
            kb_id="kb-1",
            question="q",
            conversation_id=None,
            request_id="req-p66",
            user_id="u-1",
            cache_redis=None,
        )
    ]


class TestCrossPathSinglePlane:
    @pytest.mark.asyncio
    async def test_two_stream_paths_share_one_plane_distinct_runs(self, monkeypatch):
        """守卫 #1（跨路径补全）：chat 与 retrieval 双流同平面、run_id 互异、重复 spawn 拒绝。"""
        chat_frames = await _collect_chat(session_id="s-p66-chat")
        assert chat_frames, "chat 流必须产出帧"

        _patch_retrieval_stream(monkeypatch)
        ret_frames = await _collect_retrieval()
        assert ret_frames, "retrieval 流必须产出帧"

        plane = get_control_plane()
        runs = [r for r in plane.list_runs() if r.meta.get("channel") == "stream"]
        assert len(runs) == 2, f"两条流路径必须都登记到同一平面：{runs}"

        run_ids = {r.run_id for r in runs}
        assert len(run_ids) == 2 and "" not in run_ids, f"run_id 必须互异非空：{run_ids}"
        names = {r.agent_name for r in runs}
        assert names == {"kg_graph", "retrieval_chat"}, (
            f"双流各自独立登记（agent_name 应为两条路径）：{names}"
        )

        # 跨路径单 owner：以任一已登记 run_id 再 spawn → 拒绝（p61 同语义）
        with pytest.raises(ValueError, match="already registered"):
            plane.spawn("retrieval_chat", run_id=next(iter(run_ids)))
