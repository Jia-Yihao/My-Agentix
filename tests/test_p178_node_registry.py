"""M3-1 编排层节点注册表底座的用例（`agentix/orchestration/node_registry.py`）。

## 覆盖面

1. 四入口齐（`register` / `provider` / `resolve` / `require`）的行为用例；
2. 未注册 name 的 fail-closed：`resolve` 与 `require` 都抛 `NodeNotRegistered`，
   且异常文本**同时**含缺失的 name 与短语 `application wiring not loaded`；
3. 重复注册同名的处置（默认拒绝覆盖，`override=True` 才放行）；
4. provider thunk 语义（改模块属性后 `resolve` 必须取到新对象 ⇒ 不许缓存真身）；
5. sync/async 判据（「调用后能否拿到可 await 的结果」，不是 `iscoroutinefunction` 字面）；
6. **编译图等价取证守卫**：运行时 `build_graph()` 产物的节点名集合与边集；
7. `node_registry.py` 自身的静态守卫：`langgraph` import 0 处、`agentix.application`
   import 0 处（`ast.walk` 全树，函数体内的延迟 import 同样扫到）。**口径比
   `tests/test_p177_arch_closure.py` 更窄**：本文件的 helper 只扫绝对导入（跳过
   `level > 0` 的相对导入），跨包相对导入的兜底源是 p177 守卫 #5
   `test_no_cross_package_relative_imports`，本文件不另立第二份判据。

## 验收口径（对齐 `docs/architecture/分层架构与依赖规则.md` §7.5 第 ① 步）

- 本批**只建底座、不拆边**：`agentix/orchestration/` 对 `agentix.application` 的
  import 语句数仍为 27（由复验命令核对，不在本文件断言——本文件断言的是新文件的零依赖）。
- 第 6 条守卫是 M3 后续批次唯一的**新增**等价性基准线：判据取自运行时编译图，
  **不读源码文本**。理由是 M3 之后「源码里没有那个名字」不再是证据（handler 改由注册表
  注入，源码里本来就不会再出现具体 handler 名）。**改这条守卫必须同批说明理由**，
  否则等价性证明的基准线会被悄悄挪动。

## 隔离口径

模块级函数写的是进程级单例 `DEFAULT_REGISTRY`，所以用到它的用例一律在 fixture 里
快照并还原其登记项：本文件的用例不得给后续批次留下脏注册表，也不得依赖执行顺序。
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import pathlib
from typing import Any, Callable, Dict, Iterable, List, Tuple

import pytest

from agentix.orchestration import node_registry
from agentix.orchestration.node_registry import (
    WIRING_HINT,
    DEFAULT_REGISTRY,
    NodeAlreadyRegistered,
    NodeEntry,
    NodeNotRegistered,
    NodeRegistry,
    is_awaitable_result,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE_REGISTRY_PATH = REPO_ROOT / "agentix" / "orchestration" / "node_registry.py"


# ---------------------------------------------------------------------------
# 1 — 四入口齐
# ---------------------------------------------------------------------------


class TestFourEntryPoints:
    def test_register_then_resolve_returns_provider_result(self) -> None:
        sentinel = object()
        reg = NodeRegistry()
        reg.register("n", lambda: sentinel, awaits=True, takes_config=False)
        assert reg.resolve("n") is sentinel

    def test_provider_returns_the_thunk_itself_not_the_result(self) -> None:
        """`provider` 与 `resolve` 必须可区分：建图期要的是间接引用本身。"""

        def thunk() -> str:
            return "body"

        reg = NodeRegistry()
        reg.register("n", thunk, awaits=False, takes_config=True)
        assert reg.provider("n") is thunk
        assert reg.resolve("n") == "body"

    def test_require_passes_when_all_present(self) -> None:
        reg = NodeRegistry()
        reg.register("a", lambda: 1, awaits=True, takes_config=False)
        reg.register("b", lambda: 2, awaits=True, takes_config=False)
        reg.require(["a", "b"])  # 不抛即通过

    def test_require_accepts_any_iterable(self) -> None:
        reg = NodeRegistry()
        reg.register("a", lambda: 1, awaits=False, takes_config=False)
        reg.require(iter(["a"]))

    def test_entry_exposes_the_registered_shape(self) -> None:
        """`entry` 是形态（名字 / awaits / takes_config）的唯一读取入口。"""
        reg = NodeRegistry()
        reg.register("n", lambda: 1, awaits=True, takes_config=False)
        e = reg.entry("n")
        assert isinstance(e, NodeEntry)
        assert (e.name, e.awaits, e.takes_config) == ("n", True, False)

    def test_names_and_registered_are_read_only_views(self) -> None:
        reg = NodeRegistry()
        assert reg.names() == ()
        assert reg.registered("n") is False
        reg.register("n", lambda: 1, awaits=False, takes_config=False)
        assert reg.names() == ("n",)
        assert reg.registered("n") is True

    def test_registered_names_sorted_for_deterministic_messages(self) -> None:
        reg = NodeRegistry()
        for name in ("zeta", "alpha", "mid"):
            reg.register(name, lambda: None, awaits=True, takes_config=False)
        assert reg.names() == ("alpha", "mid", "zeta")

    def test_module_level_functions_forward_to_default_registry(self) -> None:
        sentinel = object()
        node_registry.register("p178.modlevel", lambda: sentinel, awaits=True, takes_config=True)
        assert node_registry.provider("p178.modlevel")() is sentinel
        assert node_registry.resolve("p178.modlevel") is sentinel
        node_registry.require(["p178.modlevel"])
        assert node_registry.entry("p178.modlevel").awaits is True
        assert "p178.modlevel" in node_registry.names()


# ---------------------------------------------------------------------------
# 2 — 未注册即 fail-closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_resolve_unregistered_raises_and_names_the_key(self) -> None:
        reg = NodeRegistry()
        with pytest.raises(NodeNotRegistered) as excinfo:
            reg.resolve("missing_node")
        text = str(excinfo.value)
        assert "missing_node" in text
        assert WIRING_HINT in text
        assert "application wiring not loaded" in text

    def test_require_unregistered_raises_with_all_missing_listed(self) -> None:
        """一次列全缺失名：建图入口有 10 个名字，逐个抛会让回归变成十轮往返。"""
        reg = NodeRegistry()
        reg.register("present", lambda: 1, awaits=True, takes_config=False)
        with pytest.raises(NodeNotRegistered) as excinfo:
            reg.require(["present", "gone_a", "gone_b"])
        text = str(excinfo.value)
        assert "gone_a" in text
        assert "gone_b" in text
        assert WIRING_HINT in text
        assert excinfo.value.missing == ("gone_a", "gone_b")

    def test_provider_and_entry_unregistered_also_raise(self) -> None:
        reg = NodeRegistry()
        for accessor in (reg.provider, reg.entry):
            with pytest.raises(NodeNotRegistered) as excinfo:
                accessor("nope")
            assert "nope" in str(excinfo.value)
            assert WIRING_HINT in str(excinfo.value)

    def test_exception_carries_available_set_for_triage(self) -> None:
        reg = NodeRegistry()
        reg.register("real_node", lambda: 1, awaits=True, takes_config=False)
        with pytest.raises(NodeNotRegistered) as excinfo:
            reg.resolve("typo_node")
        assert excinfo.value.available == ("real_node",)
        assert "real_node" in str(excinfo.value)

    def test_no_fallback_lookup_surface(self) -> None:
        """底座不提供「取不到就返回默认实现」的口子：有的话装配缺失就会伪装成跑通。"""
        assert not hasattr(NodeRegistry, "get")
        assert not hasattr(node_registry, "get")
        assert list(inspect.signature(NodeRegistry.resolve).parameters) == ["self", "name"]
        assert list(inspect.signature(node_registry.resolve).parameters) == ["name"]

    def test_error_type_is_not_keyerror(self) -> None:
        """`KeyError` 会给消息再套一层 repr 引号，让「文本包含 name」的断言形态不稳。"""
        reg = NodeRegistry()
        with pytest.raises(NodeNotRegistered) as excinfo:
            reg.resolve("k")
        assert not isinstance(excinfo.value, KeyError)


# ---------------------------------------------------------------------------
# 3 — 重复同名注册的处置（显式定义 + 钉住）
# ---------------------------------------------------------------------------


class TestDuplicateRegistration:
    def test_second_register_without_override_is_rejected(self) -> None:
        reg = NodeRegistry()
        reg.register("n", lambda: "first", awaits=True, takes_config=False)
        with pytest.raises(NodeAlreadyRegistered) as excinfo:
            reg.register("n", lambda: "second", awaits=True, takes_config=False)
        assert "n" in str(excinfo.value)
        assert "override" in str(excinfo.value)

    def test_rejected_duplicate_leaves_the_original_in_place(self) -> None:
        reg = NodeRegistry()
        reg.register("n", lambda: "first", awaits=True, takes_config=False)
        with pytest.raises(NodeAlreadyRegistered):
            reg.register("n", lambda: "second", awaits=True, takes_config=False)
        assert reg.resolve("n") == "first"

    def test_override_true_swaps_deliberately(self) -> None:
        reg = NodeRegistry()
        reg.register("n", lambda: "first", awaits=True, takes_config=False)
        reg.register("n", lambda: "second", awaits=False, takes_config=True, override=True)
        assert reg.resolve("n") == "second"
        assert (reg.entry("n").awaits, reg.entry("n").takes_config) == (False, True)

    def test_default_registry_forwards_override_flag(self) -> None:
        node_registry.register("p178.dup", lambda: "a", awaits=True, takes_config=False)
        with pytest.raises(NodeAlreadyRegistered):
            node_registry.register("p178.dup", lambda: "b", awaits=True, takes_config=False)
        node_registry.register(
            "p178.dup", lambda: "b", awaits=True, takes_config=False, override=True
        )
        assert node_registry.resolve("p178.dup") == "b"


# ---------------------------------------------------------------------------
# 4 — provider thunk 语义（后续批次取证的依据）
# ---------------------------------------------------------------------------


class _ModuleLike:
    """模拟 application 侧的 handler 模块（`lambda: mod.execute_task` 的 mod）。"""

    def __init__(self) -> None:
        self.execute_task = "impl-v1"


class TestProviderThunkSemantics:
    def test_resolve_reads_current_module_attribute_not_snapshot(self) -> None:
        """改模块属性后 `resolve` 必须返回新对象。

        这是后续批次的取证依据：注册写成 `lambda: router_mod.execute_task` 时，测试对
        application 模块属性的 patch 仍然生效；写成 `provider=已取出的函数` 则在 import 期
        就绑死了，patch 打不中——而 patch 打不中的表现是「测试通过但没测到东西」。
        """
        mod = _ModuleLike()
        reg = NodeRegistry()
        reg.register("execute_task", lambda: mod.execute_task, awaits=True, takes_config=False)
        assert reg.resolve("execute_task") == "impl-v1"
        mod.execute_task = "impl-v2"
        assert reg.resolve("execute_task") == "impl-v2"

    def test_repeated_resolve_re_invokes_the_thunk(self) -> None:
        """同一 name 重复 resolve 每次走 thunk，不许缓存真身。"""
        calls: List[int] = []

        def thunk() -> int:
            calls.append(1)
            return len(calls)

        reg = NodeRegistry()
        reg.register("counting", thunk, awaits=False, takes_config=False)
        assert [reg.resolve("counting") for _ in range(3)] == [1, 2, 3]

    def test_entry_is_not_a_second_source_for_the_body(self) -> None:
        """`entry(name).provider` 与 `provider(name)` 必须是同一个 thunk（只有一个源）。"""
        reg = NodeRegistry()
        reg.register("n", lambda: 1, awaits=True, takes_config=False)
        assert reg.entry("n").provider is reg.provider("n")

    def test_non_callable_provider_rejected_at_register(self) -> None:
        reg = NodeRegistry()
        with pytest.raises(ValueError, match="zero-arg thunk"):
            reg.register("n", "already-resolved-body", awaits=True, takes_config=False)

    def test_empty_or_non_str_name_rejected_at_register(self) -> None:
        reg = NodeRegistry()
        for bad in ("", None, 123):  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="non-empty str"):
                reg.register(bad, lambda: 1, awaits=True, takes_config=False)

    @pytest.mark.parametrize("flag", ["awaits", "takes_config"])
    def test_shape_flags_must_be_bool(self, flag: str) -> None:
        """形态位必须显式布尔：让注册表猜就是第二个判据源（纪律 6 ④）。"""
        reg = NodeRegistry()
        kwargs: Dict[str, Any] = {"awaits": True, "takes_config": True}
        kwargs[flag] = "yes"
        with pytest.raises(ValueError, match="must be a bool"):
            reg.register("n", lambda: 1, **kwargs)


# ---------------------------------------------------------------------------
# 5 — sync/async 判据
# ---------------------------------------------------------------------------


def _criterion_of(fn: Callable[[], Any]) -> bool:
    """按注册表的判据评估一次：调用后能否拿到可 await 的结果。

    走 `asyncio.run` 而不是直接调：异步节点的结果必须被 await 掉，否则留下一串
    "coroutine was never awaited" 告警，把下一次回归的日志变成噪音现场。
    """

    async def _run() -> bool:
        result = fn()
        awaits = is_awaitable_result(result)
        if awaits:
            await result
        return awaits

    return asyncio.run(_run())


async def _async_fn() -> str:
    return "async"


def _sync_fn() -> str:
    return "sync"


class _AsyncCallNode:
    """类形式的异步节点：`iscoroutinefunction` 对它为 False，但调用结果是可 await 的。"""

    async def __call__(self) -> str:
        return "class-async"


class TestAwaitableCriterion:
    def test_async_def_is_awaitable(self) -> None:
        assert _criterion_of(_async_fn) is True

    def test_plain_def_is_not_awaitable(self) -> None:
        assert _criterion_of(_sync_fn) is False

    def test_class_form_async_call_is_awaitable(self) -> None:
        node = _AsyncCallNode()
        assert _criterion_of(node) is True
        # 纪律 6 ④的反面证据：按字面判会把每个类形式的异步节点登记成同步。
        assert inspect.iscoroutinefunction(node) is False

    def test_criterion_is_the_registry_helper_itself(self) -> None:
        """判据只有一处实现：用例不许另算一份 `iscoroutinefunction` 当基准。"""

        async def _run() -> Tuple[bool, bool]:
            a = _async_fn()
            s = _sync_fn()
            verdicts = (is_awaitable_result(a), is_awaitable_result(s))
            await a  # 不留 un-awaited coroutine 告警，免得下次回归的日志变噪音现场
            return verdicts

        assert asyncio.run(_run()) == (True, False)

    def test_registered_shape_matches_the_criterion(self) -> None:
        """`entry.awaits` 与判据对同一节点必须一致（判据源与记录同源，不得两套）。"""
        node = _AsyncCallNode()
        reg = NodeRegistry()
        reg.register("n", lambda: node, awaits=_criterion_of(node), takes_config=False)
        assert reg.entry("n").awaits is True
        assert _criterion_of(reg.resolve("n")) is True


# ---------------------------------------------------------------------------
# 6 — 编译图等价取证守卫（M3 新增的唯一等价性证据）
# ---------------------------------------------------------------------------

#: 扁平主图的 10 个节点名，取自 `lg_builder.py` 的 `builder.add_node(...)` 块（时点 `:428-467`）；
#: 与 `lg_builder.py` 上方「本块仅注册下列 10 个节点」注释（时点 `:419-422`）点名的集合一致；
#: `check_hallucinations` 有意不接线，故不在集合内）。
EXPECTED_GRAPH_NODE_NAMES: frozenset = frozenset(
    {
        "message_window",  # lg_builder.py:428-435
        "analyze_and_route_query",  # lg_builder.py:436
        "respond_to_general_query",  # lg_builder.py:437
        "get_additional_info",  # lg_builder.py:438
        "create_research_plan",  # lg_builder.py:439-444
        "create_image_query",  # lg_builder.py:445
        "create_file_query",  # lg_builder.py:446
        "create_kb_query",  # lg_builder.py:447
        "execute_task",  # lg_builder.py:453-458
        "code_agent",  # lg_builder.py:462-467
    }
)

#: LangGraph 编译图里的伪起点（`START`）。它出现在 `compiled.nodes` 与 `get_graph()`
#: 的节点集合里，但不是 `add_node` 登记的业务节点 ⇒ 取「恰为 10 个」时必须先剥掉。
#: 写成常量而不是字符串字面量多处复制，是为了让「10」这个读数只有一处口径。
_PSEUDO_START = "__start__"
_PSEUDO_END = "__end__"

#: 编译图的**完整边集**冻结基准：18 条三元组 `(source, target, conditional)`，
#: 形态与 `TestCompiledGraphEquivalence._edges()` 逐字一致，按 `sorted()` 升序排列。
#:
#: 取值来源：**运行时一次枚举**，不是手抄源码——
#:     PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "
#:       from agentix.orchestration.langgraph import lg_builder;
#:       print(sorted((e.source, e.target, bool(e.conditional))
#:                    for e in lg_builder.graph.get_graph().edges))"
#: 取值日期：2026-10-29（T-M3-1 第 2 轮；原始输出已随该轮提交审查报告登记）。
#:
#: 为什么必须是**全集相等**：本类其余断言只覆盖 ①入口两条边的成员、②条件边集合、
#: ③入 `__end__` 的非条件边**源集**，三段并集漏掉「`conditional=False` 且 target 是
#: 业务节点」这个边域 ⇒ 一条新增的非条件业务→业务边（如 `execute_task →
#: respond_to_general_query`）能让那些断言全部保持绿色，而图拓扑已经变了。本常量补的
#: 就是这个缺口，也是模块 docstring 第 6 条「节点名集合**与边集**」这句声明的兑现。
#:
#: **改它必须同批说明理由**（与本文件既有守卫同一口径）：它是 M3 后续拆边批次的等价性
#: 基准线，悄悄挪动它等于把等价性证明作废。
EXPECTED_GRAPH_EDGES: Tuple[Tuple[str, str, bool], ...] = (
    (_PSEUDO_START, "message_window", False),
    ("analyze_and_route_query", "code_agent", True),
    ("analyze_and_route_query", "create_file_query", True),
    ("analyze_and_route_query", "create_image_query", True),
    ("analyze_and_route_query", "create_kb_query", True),
    ("analyze_and_route_query", "create_research_plan", True),
    ("analyze_and_route_query", "execute_task", True),
    ("analyze_and_route_query", "get_additional_info", True),
    ("analyze_and_route_query", "respond_to_general_query", True),
    ("code_agent", _PSEUDO_END, False),
    ("create_file_query", _PSEUDO_END, False),
    ("create_image_query", _PSEUDO_END, False),
    ("create_kb_query", _PSEUDO_END, False),
    ("create_research_plan", _PSEUDO_END, False),
    ("execute_task", _PSEUDO_END, False),
    ("get_additional_info", _PSEUDO_END, False),
    ("message_window", "analyze_and_route_query", False),
    ("respond_to_general_query", _PSEUDO_END, False),
)

#: 边数单独钉一条：常量整体被替换时（例如有人重排或删项）这条断言给出的是「几条」的
#: 直接读数，比集合差集更好定位；且它必须与 `EXPECTED_GRAPH_EDGES` 的实际长度一致。
EXPECTED_GRAPH_EDGE_COUNT = 18

#: 装配期（M3-2 起，`agentix/application/wiring.py` = §7.5 第②步唯一落点）登记的节点名
#: 全集：8 个 `ROUTE_HANDLERS` 键 + `route_query` + `analyze_and_route_query`。
#: `names()` 返回排序元组，故常量按 `sorted` 冻结——判据与 `node_registry.names()` 同形，
#: 保持**全集相等**口径（新增/缺失/替换实现名都会让它红）。
EXPECTED_WIRED_NAMES: Tuple[str, ...] = tuple(
    sorted(
        {
            "respond_to_general_query",
            "get_additional_info",
            "create_research_plan",
            "create_image_query",
            "create_file_query",
            "create_kb_query",
            "execute_task",
            "code_agent",
            "route_query",
            "analyze_and_route_query",
        }
    )
)


class TestCompiledGraphEquivalence:
    """守卫「现在的编译图长什么样」，作为后续拆边的等价性基准线。

    判据全部取自**运行时编译产物**（`compiled.nodes` 的键集 + `get_graph()` 的边集），
    不读源码文本：M3 之后「源码里出现了/没出现某个名字」不再能证明任何东西。
    """

    @pytest.fixture(scope="class")
    def compiled_graph(self) -> Any:
        # 从测试侧 import（测试不受 p177 的包边约束）：底座批次里注册表还没有生产写入点，
        # 因此本守卫必须在**不依赖注册表**的前提下取基准，否则基准线会随 M3-2 一起浮动。
        from agentix.orchestration.langgraph import lg_builder

        return lg_builder.graph

    @staticmethod
    def _edges(graph: Any) -> Tuple[Tuple[str, str, bool], ...]:
        return tuple((e.source, e.target, bool(e.conditional)) for e in graph.get_graph().edges)

    def test_business_node_set_is_exactly_the_ten(self, compiled_graph: Any) -> None:
        real = set(compiled_graph.nodes) - {_PSEUDO_START}
        assert real == EXPECTED_GRAPH_NODE_NAMES
        assert len(real) == 10, f"节点数漂移：{sorted(real)}"

    def test_drawn_graph_nodes_match_compiled_nodes(self, compiled_graph: Any) -> None:
        """`get_graph()` 与 `compiled.nodes` 两个视图必须同集（防取错视图当基准）。"""
        drawn = {
            n for n in compiled_graph.get_graph().nodes if n not in (_PSEUDO_START, _PSEUDO_END)
        }
        assert drawn == EXPECTED_GRAPH_NODE_NAMES

    def test_entry_chain_start_to_message_window_to_router(self, compiled_graph: Any) -> None:
        edges = {(s, t) for s, t, cond in self._edges(compiled_graph) if not cond}
        assert (_PSEUDO_START, "message_window") in edges
        assert ("message_window", "analyze_and_route_query") in edges

    def test_router_conditional_edges_cover_the_other_eight(self, compiled_graph: Any) -> None:
        conditional = {t for s, t, cond in self._edges(compiled_graph) if cond}
        assert conditional == EXPECTED_GRAPH_NODE_NAMES - {
            "message_window",
            "analyze_and_route_query",
        }
        sources = {s for s, _t, cond in self._edges(compiled_graph) if cond}
        assert sources == {"analyze_and_route_query"}, "条件边只能由路由节点发出"

    def test_every_business_node_reaches_end(self, compiled_graph: Any) -> None:
        into_end = {
            s for s, t, cond in self._edges(compiled_graph) if t == _PSEUDO_END and not cond
        }
        assert into_end == EXPECTED_GRAPH_NODE_NAMES - {"message_window", "analyze_and_route_query"}

    def test_full_edge_set_is_frozen(self, compiled_graph: Any) -> None:
        """**全集相等**守卫：逐条钉住编译图的每一条边，不留未覆盖的边域。

        本类其余用例是**局部**断言（入口成员、条件边集合、入 `__end__` 的源集），
        三段并集不覆盖「非条件且 target 是业务节点」的边 ⇒ 只靠它们会出现
        「拓扑变了而守卫全绿」的假绿形态。本用例用 `EXPECTED_GRAPH_EDGES` 把整张边集
        冻结下来：写成集合/元组的**相等**，不写成 `in` 成员判断，也不写成子集判断。
        """
        edges = tuple(sorted(self._edges(compiled_graph)))
        assert len(edges) == EXPECTED_GRAPH_EDGE_COUNT, (
            f"边数漂移：实得 {len(edges)}，冻结基准 {EXPECTED_GRAPH_EDGE_COUNT}；"
            f"差集（实 - 冻）={set(edges) - set(EXPECTED_GRAPH_EDGES)}，"
            f"差集（冻 - 实）={set(EXPECTED_GRAPH_EDGES) - set(edges)}"
        )
        assert edges == EXPECTED_GRAPH_EDGES

    def test_registry_is_wired_by_application_assembly(self) -> None:
        """M3-2 起装配已接入：默认注册表**恰含** wiring 登记的 10 个节点名。

        本用例原口径（M3-1 底座批次）是 `DEFAULT_REGISTRY.names() == ()`——当时注册表
        还没有任何生产写入点，空集即「底座建好但没有任何节点从注册表解析」的零变化证明。
        原 docstring 明确预告：「后续批次开始写入装配时这条会红，那是预期的（届时按
        当时的批次口径改写它，并在同批说明理由）」——M3-2 即该批次：
        `agentix/application/wiring.py`（§7.5 第②步）经 `tests/conftest.py` 挂线，
        装配名集合成为新的冻结基准。**改写保持全集相等，不放宽为成员/子集判断。**
        """
        assert DEFAULT_REGISTRY.names() == EXPECTED_WIRED_NAMES


# ---------------------------------------------------------------------------
# 7 — node_registry.py 自身的静态守卫
# ---------------------------------------------------------------------------


def _imported_modules(path: pathlib.Path) -> List[Tuple[int, str]]:
    """全树 import 模块名（`ast.walk`：函数体内的延迟 import 同样计入）。

    只看顶层会漏掉「为绕开检查而挪进函数里」的那一类，而那正是本机制最可能出现的
    绕行形态——所以必须 `ast.walk`。

    **口径比 `tests/test_p177_arch_closure.py` 更窄，不是同口径**：`node.level == 0`
    的过滤意味着本 helper 只扫**绝对导入**，跨包相对导入（`from ..pkg import x`）它看不见。
    那条判据的唯一源是 p177：它在 `:187-197` 解析 `level > 1`，并由守卫 #5
    `test_no_cross_package_relative_imports`（`:1049-1059`）兜底。本文件刻意不复制这份
    解析——同一个判定不许两个源（`AGENTS.md` 纪律 6 ④）。
    本批结论不受影响：`agentix/orchestration/` 全目录当前**零相对导入**
    （grep `^\\s*from \\.` 零命中），故窄口径与全口径在本批等价。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "agentix":
                # `from agentix import application` 还原成 agentix.application 再判。
                found.extend((node.lineno, f"agentix.{alias.name}") for alias in node.names)
            else:
                found.append((node.lineno, node.module))
    return found


class TestStaticDependencyGuards:
    def test_file_exists_at_the_declared_location(self) -> None:
        assert NODE_REGISTRY_PATH.is_file(), f"落点不符：{NODE_REGISTRY_PATH}"

    @pytest.mark.parametrize("prefix", ["langgraph", "agentix.application"])
    def test_no_forbidden_import(self, prefix: str) -> None:
        """`langgraph`：p66 的文件白名单是 equality 棘轮，新文件 import 它就要加一行，
        等于扩大未经评审的例外面。`agentix.application`：本文件存在的理由就是拆掉这条
        反向边，自己绝不能先沾上。"""
        offenders = [
            (line, mod)
            for line, mod in _imported_modules(NODE_REGISTRY_PATH)
            if mod == prefix or mod.startswith(prefix + ".")
        ]
        assert offenders == [], f"{NODE_REGISTRY_PATH.name} 出现 {prefix} import: {offenders}"

    def test_package_doors_stay_import_free(self) -> None:
        """注册表不得经包门面导出：`orchestration/**/__init__.py` 必须零 import
        （与 `tests/test_p71_m2_orchestration_migration.py:115-125` 同一口径）。"""
        for rel in (
            "agentix/orchestration/__init__.py",
            "agentix/orchestration/langgraph/__init__.py",
        ):
            tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
            imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
            assert imports == [], f"{rel} 包门面出现 import"

    def test_public_api_is_the_four_entry_points(self) -> None:
        """四入口的名字是口径本身（后续批次的装配代码按这两个字面点名调用）。"""
        for fn_name in ("register", "provider", "resolve", "require"):
            assert callable(getattr(node_registry, fn_name)), fn_name
            assert fn_name in node_registry.__all__
        assert "NodeNotRegistered" in node_registry.__all__
        assert WIRING_HINT == "application wiring not loaded"

    def test_no_broad_except_in_the_module(self) -> None:
        """本批按理没有宽 except：出现了就说明有人加了静默降级。"""
        tree = ast.parse(NODE_REGISTRY_PATH.read_text(encoding="utf-8"))
        broad: Iterable[int] = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
            and (
                node.type is None
                or (
                    isinstance(node.type, ast.Name)
                    and node.type.id in ("Exception", "BaseException")
                )
            )
        ]
        assert list(broad) == [], f"node_registry.py 出现宽 except: {list(broad)}"


# ---------------------------------------------------------------------------
# 夹具：模块级函数写的是进程级单例，用例结束后必须还原
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_default_registry() -> Any:
    snapshot = dict(DEFAULT_REGISTRY._entries)  # noqa: SLF001 — 测试侧还原单例的唯一办法
    yield
    DEFAULT_REGISTRY._entries.clear()  # noqa: SLF001
    DEFAULT_REGISTRY._entries.update(snapshot)  # noqa: SLF001
