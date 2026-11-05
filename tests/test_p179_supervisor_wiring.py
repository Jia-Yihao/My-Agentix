"""M3-2「lg_supervisor 去具体化」的用例（application 装配 + 惰性映射 + 静态守卫）。

## 覆盖面

1. **装配面**：`agentix/application/wiring.py`（§7.5 第②步唯一落点）登记恰 10 个
   节点名（8 个 `ROUTE_HANDLERS` 键 + `route_query` + `analyze_and_route_query`），
   `awaits/takes_config` 声明与调用点形态一致；重复装配（reload wiring）如实断言
   为抛 `NodeAlreadyRegistered`（本批刻意不加幂等保护，让重复装配暴露）。
2. **惰性映射五件**（`lg_supervisor.ROUTE_HANDLERS`）：键集恰 8 且顺序与改造前一致；
   `in` / `iter` / `len` **不触注册表**（真实构造装配缺失态证明）；`__setitem__` 只写
   覆盖表不进注册表；`monkeypatch.setitem` 撤销后回落到注册表解析（等价回写即撤销，
   不留快照——这是 17 处既有 setitem 兼容面成立的前提）。
3. **fail-closed 面**：静态键存在但未注册 ⇒ `__getitem__` 与 `get` 都抛
   `NodeNotRegistered`（文本含 `application wiring not loaded`）且**绝不返回 default**；
   不在键集 ⇒ `get` 返回 default、`__getitem__` 抛 `KeyError`（保住
   `supervisor_dispatch` 原 `ValueError("unknown route")` 可达语义）。
4. **对象同一性 / thunk 语义**：`ROUTE_HANDLERS["execute_task"] is` application 当前
   模块属性；patch application 模块属性后映射**立刻**可见（证每次现取、无快照）。
5. **调用点形态同源**：AST 断言 `supervisor_dispatch` 对 handler 的 await 调用
   只有一处且形态 `await handler(state, config=cfg)`、对 `route_fn` 的调用是同步
   单参——注册表项的 `awaits/takes_config` 声明以这两处为唯一事实来源，
   改调用点必须同批改声明（wiring.py 的登记注释同口径互指）。
6. **运行时编译图等值**：与 M3-1（`tests/test_p178_node_registry.py` 第 6 组）同法，
   判据取**运行时编译图**（`compiled.nodes` 键集 + `get_graph()` 边集），不读源码文本；
   基准由改造后一次真实编译取值并**逐条三元组冻结**（全集相等）。本批 supervisor 的
   图结构与改造前逐位一致（`add_node` 只是把解析点从 import 期挪到建图期，
   交给 langgraph 的是同一个真身）。
7. **静态守卫**：`lg_supervisor.py` **顶层** application import 语句 AST 计数 == 0
   （`ast.walk` 全树口径下余 2 条 = 函数体内 `llm_factory.get_llm_client` 延迟 import，
   属分派面之外的 LLM 工厂依赖，已登记归 M3-4——**时点限定：以上是 2026-10-26 核验时点的读数，
   2026-10-30 M3-4b 卡2 拆掉这两条后全树为 0 条**，判据本体见
   `test_lg_supervisor_deferred_application_imports_are_gone`）；`wiring.py` 不 import `langgraph`；
   `agentix/orchestration/**` 全树不 import `agentix.application.wiring`
   （防止把反向边换个名字绕回来）；`lg_supervisor.py` 模块体顶层指向
   `lg_builder` 的 import **恰 0 条**（2026-10-29 M3-5b 返工追加：`lg_builder`
   import 期要读注册表，顶层 import 会把装配依赖转嫁给 supervisor 的所有
   importer——p71 `TestLoadOrderSafety` 被砸红的直接教训，判据与理由见用例）。

## 验收口径

- 对齐 `.qoder/rules/code-exec.md` 之外的项目级口径见
  `docs/architecture/分层架构与依赖规则.md` §7.5 第②③步与
  `agentix/orchestration/node_registry.py` 模块 docstring 的三条设计口径
  （fail-closed / 零参 thunk 每次现取 / 形态声明唯一判据源）。
- 本文件对 `DEFAULT_REGISTRY` 的用例一律快照并还原（同 p178 隔离口径），
  不得给后续批次留脏注册表，也不得依赖执行顺序；对 `ROUTE_HANDLERS` 覆盖表同样隔离。
"""

from __future__ import annotations

import ast
import importlib
import pathlib
from typing import Any, List, Tuple

import pytest

import agentix.application.agents.general_handler as general_handler
import agentix.application.agents.task_handler as task_handler
from agentix.application import wiring
from agentix.orchestration import node_registry
from agentix.orchestration.langgraph.lg_supervisor import (
    _HANDLER_KEYS,
    ROUTE_HANDLERS,
)
from agentix.orchestration.node_registry import (
    WIRING_HINT,
    DEFAULT_REGISTRY,
    NodeAlreadyRegistered,
    NodeNotRegistered,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LG_SUPERVISOR_PATH = REPO_ROOT / "agentix" / "orchestration" / "langgraph" / "lg_supervisor.py"
WIRING_PATH = REPO_ROOT / "agentix" / "application" / "wiring.py"
ORCHESTRATION_DIR = REPO_ROOT / "agentix" / "orchestration"

#: 装配面全集（与 p178 的 EXPECTED_WIRED_NAMES 同一集合、两处冻结的理由不同：
#: p178 钉「注册表底座被接通」，本文件钉「wiring 落点的内容」；后续批次改名字
#: 必须同批改两处，这两条守卫对同一事实各背一半证据）。
WIRED_NAMES = frozenset(
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


# ---------------------------------------------------------------------------
# 夹具：注册表单例 + supervisor 覆盖表的隔离（快照/还原，同 p178 口径）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_default_registry() -> Any:
    snapshot = dict(DEFAULT_REGISTRY._entries)  # noqa: SLF001 — 测试侧还原单例的唯一办法
    overrides = dict(ROUTE_HANDLERS._overrides)  # noqa: SLF001 — 覆盖表同构隔离
    yield
    DEFAULT_REGISTRY._entries.clear()  # noqa: SLF001
    DEFAULT_REGISTRY._entries.update(snapshot)  # noqa: SLF001
    ROUTE_HANDLERS._overrides.clear()  # noqa: SLF001
    ROUTE_HANDLERS._overrides.update(overrides)  # noqa: SLF001


def _unwire() -> None:
    """构造「装配缺失」态：清空默认注册表（autouse 夹具负责还原）。"""
    DEFAULT_REGISTRY._entries.clear()  # noqa: SLF001 — 与 p178 同法的测试侧唯一手段


# ---------------------------------------------------------------------------
# 1 — 装配面
# ---------------------------------------------------------------------------


class TestAssemblySurface:
    def test_names_exactly_the_wired_ten(self) -> None:
        """`names()` 含且仅含 10 个名字（集合相等；顺序由 names() 自身排序，不作判据）。"""
        assert set(DEFAULT_REGISTRY.names()) == set(WIRED_NAMES)
        assert len(DEFAULT_REGISTRY.names()) == 10

    def test_registered_shapes_match_the_call_site_declarations(self) -> None:
        """8 键 awaits/takes_config 全 True（唯一调用点 `await handler(state, config=cfg)`）；
        `route_query` 全 False（同步 `route_fn(state)`）；`analyze_and_route_query`
        True/True（router.py:434 真实签名 `async def (state, *, config)`）。"""
        for key in _HANDLER_KEYS:
            e = DEFAULT_REGISTRY.entry(key)
            assert (e.awaits, e.takes_config) == (True, True), key
        e_route = DEFAULT_REGISTRY.entry("route_query")
        assert (e_route.awaits, e_route.takes_config) == (False, False)
        e_analyze = DEFAULT_REGISTRY.entry("analyze_and_route_query")
        assert (e_analyze.awaits, e_analyze.takes_config) == (True, True)

    def test_reload_wiring_raises_duplicate_and_leaves_registry_intact(self) -> None:
        """重复装配如实断言：override 默认关 ⇒ reload 抛 `NodeAlreadyRegistered`。

        登记实测行为（不是「应当幂等」）：`register` 在赋值前抛出 ⇒ 被拒的重复注册
        不改动注册表；且 wiring 文件体把所有模块形态 import 排在任何 register 之前，
        失败的 reload 停在第一条 register ⇒ 既有 thunk 的 `__globals__`（wiring 的
        模块 dict）里 handler 模块绑定已全部重放，解析不受损——下面用对象同一性钉住。
        """
        before = DEFAULT_REGISTRY.names()
        with pytest.raises(NodeAlreadyRegistered):
            importlib.reload(wiring)
        assert DEFAULT_REGISTRY.names() == before
        assert ROUTE_HANDLERS["execute_task"] is task_handler.execute_task


# ---------------------------------------------------------------------------
# 2 — 惰性映射五件
# ---------------------------------------------------------------------------


class TestLazyMappingSurface:
    def test_iter_yields_static_keys_in_declared_order(self) -> None:
        assert tuple(ROUTE_HANDLERS) == _HANDLER_KEYS
        assert _HANDLER_KEYS == (
            "respond_to_general_query",
            "get_additional_info",
            "create_research_plan",
            "create_image_query",
            "create_file_query",
            "create_kb_query",
            "execute_task",
            "code_agent",
        )

    def test_membership_len_iter_do_not_touch_registry(self) -> None:
        """键集三件在**装配缺失**态仍成立：p19 绊线与 p49/p71 成员断言不能被
        fail-closed 伪装成真失败（键集是编排层自己的声明，注册表只负责值）。"""
        _unwire()
        assert "execute_task" in ROUTE_HANDLERS
        assert len(ROUTE_HANDLERS) == 8
        assert list(ROUTE_HANDLERS) == list(_HANDLER_KEYS)
        assert set(ROUTE_HANDLERS) == set(_HANDLER_KEYS)

    def test_setitem_writes_override_table_not_registry(self) -> None:
        sentinel = object()
        before = DEFAULT_REGISTRY.names()
        key = "create_kb_query"
        real_before = ROUTE_HANDLERS[key]
        ROUTE_HANDLERS[key] = sentinel
        try:
            assert ROUTE_HANDLERS[key] is sentinel
            assert DEFAULT_REGISTRY.names() == before  # 不进注册表
            assert DEFAULT_REGISTRY.entry(key).provider() is real_before  # 注册表未动
        finally:
            del ROUTE_HANDLERS[key]
        assert ROUTE_HANDLERS[key] is real_before  # 覆盖表清空 ⇒ 回落注册表解析

    def test_monkeypatch_undo_falls_back_to_registry_resolution(self) -> None:
        """实测 pytest 语义：`setitem` 旧值 = `dic.get(name, notset)`（本映射 ⇒ 解析到
        注册表真身），撤销 = `dic[key] = 旧值`。映射对「写入值与注册表当前解析同一
        对象」按撤销处理（清覆盖、不留快照），否则 17 处 setitem 会在覆盖表留下
        永久快照，把「每次现取」钉死成测试顺序依赖。"""
        key = "respond_to_general_query"
        real_before = ROUTE_HANDLERS[key]
        fake = object()
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(ROUTE_HANDLERS, key, fake)
            assert ROUTE_HANDLERS[key] is fake
            assert ROUTE_HANDLERS.get(key) is fake
        assert ROUTE_HANDLERS[key] is real_before
        assert ROUTE_HANDLERS._overrides == {}  # noqa: SLF001 — 撤销不留快照的直接证据

    def test_undo_then_module_patch_still_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """撤销后的覆盖表必须是空的：随后 patch application 模块属性仍能透进来。

        本用例是「sticky 快照」缺陷的回归钉：若撤销把真身写回覆盖表，这里拿到的
        会是旧对象——测试能过但什么都没测到，正是注册表设计口径 2 要避免的形态。
        """
        key = "create_kb_query"
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(ROUTE_HANDLERS, key, object())
            assert ROUTE_HANDLERS._overrides.get(key) is not None  # noqa: SLF001
        sentinel = object()
        monkeypatch.setattr(general_handler, key, sentinel)
        assert ROUTE_HANDLERS[key] is sentinel


# ---------------------------------------------------------------------------
# 3 — fail-closed 面
# ---------------------------------------------------------------------------


class TestFailClosedMapping:
    def test_getitem_static_key_unregistered_raises_with_wiring_hint(self) -> None:
        _unwire()
        with pytest.raises(NodeNotRegistered) as excinfo:
            ROUTE_HANDLERS["execute_task"]
        assert WIRING_HINT in str(excinfo.value)
        assert "application wiring not loaded" in str(excinfo.value)
        assert "execute_task" in str(excinfo.value)

    def test_get_static_key_unregistered_raises_not_default(self) -> None:
        """`:234` 的 `.get(handler_name)` 在装配缺失时**必须抛**，绝不静默成 None：
        否则 :235 的 `unknown route` ValueError 把「装配没跑」伪装成「路由到了不存在
        的支路」（node_registry 模块 docstring 第 1 条的调用点侧兑现）。"""
        _unwire()
        with pytest.raises(NodeNotRegistered):
            ROUTE_HANDLERS.get("execute_task")
        with pytest.raises(NodeNotRegistered):
            ROUTE_HANDLERS.get("execute_task", None)

    def test_get_unknown_key_returns_default_even_unwired(self) -> None:
        """不在静态键集 ⇒ 返回 default（装配缺失与否同形），保住原 ValueError 分支。"""
        assert ROUTE_HANDLERS.get("no_such_route") is None
        _unwire()
        assert ROUTE_HANDLERS.get("no_such_route", "sentinel") == "sentinel"

    def test_getitem_unknown_key_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            ROUTE_HANDLERS["no_such_route"]


# ---------------------------------------------------------------------------
# 4 — 对象同一性 / thunk 每次现取
# ---------------------------------------------------------------------------


class TestThunkIdentity:
    def test_execute_task_identity_with_current_module_attribute(self) -> None:
        """p49:225 的同一条判据在本文件独立钉一遍：映射值必须是 application 侧
        **当前**模块属性，不是 import 期快照。"""
        assert ROUTE_HANDLERS["execute_task"] is task_handler.execute_task

    def test_module_attribute_patch_visible_through_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = object()
        monkeypatch.setattr(general_handler, "respond_to_general_query", sentinel)
        assert ROUTE_HANDLERS["respond_to_general_query"] is sentinel


# ---------------------------------------------------------------------------
# 5 — 调用点形态同源（注册表 awaits/takes_config 声明的唯一事实来源）
# ---------------------------------------------------------------------------


def _function_node(name: str) -> ast.AST:
    """取 `lg_supervisor.py` 模块体里的顶层函数定义节点（含 async def）。"""
    tree = ast.parse(LG_SUPERVISOR_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"lg_supervisor.py 顶层找不到函数 {name}")


def _is_registry_resolve(node: ast.AST, name: str) -> bool:
    """节点形如 `node_registry.resolve(<name 字面量>)`。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "resolve"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "node_registry"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == name
    )


class TestCallSiteShapeSingleSource:
    def test_handler_await_call_site_is_single_and_config_kw(self) -> None:
        """`supervisor_dispatch` 对 handler 的 await 调用**只有一处**，形态
        `await handler(<state>, config=<cfg>)` ⇒ 8 键的 awaits=True/takes_config=True
        以此为唯一事实来源，改调用点必须同批改 wiring 的声明。"""
        fn = _function_node("supervisor_dispatch")
        awaited = [
            n.value
            for n in ast.walk(fn)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "handler"
        ]
        assert len(awaited) == 1, f"handler 的 await 调用应恰一处，实得 {len(awaited)}"
        call = awaited[0]
        assert len(call.args) == 1, "handler 位置参只接 state"
        assert [kw.arg for kw in call.keywords] == ["config"], "handler 只经 config 关键字"

    def test_route_fn_call_site_is_sync_single_arg(self) -> None:
        """路由调用形态是同步 `route_fn(state)`（不 await、不接 config），且解析语句
        每次调用现取：`route_fn = node_registry.resolve("route_query")` ⇒
        `route_query` 项 awaits=False/takes_config=False 的唯一判据源。"""
        fn = _function_node("supervisor_dispatch")
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "route_fn"
        ]
        assert len(calls) == 1
        call = calls[0]
        assert len(call.args) == 1 and not call.keywords
        awaited_values = {id(n.value) for n in ast.walk(fn) if isinstance(n, ast.Await)}
        assert id(call) not in awaited_values, "route_query 的调用点必须是同步形态"
        assigns = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "route_fn" for t in n.targets)
        ]
        assert len(assigns) == 1 and _is_registry_resolve(assigns[0].value, "route_query")

    def test_build_graph_resolves_analyze_and_route_query_at_build_time(self) -> None:
        """建图期解析、把真身交给 langgraph（不引入解析壳——壳属 M3-3）：
        `add_node` 恰 3 处，其中一处首参是 `node_registry.resolve(...)` 调用，
        且任何一处都不许再出现裸名 `analyze_and_route_query`（那意味着内联兜底复活）。"""
        fn = _function_node("build_supervisor_graph")
        add_calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "add_node"
        ]
        assert len(add_calls) == 3
        resolved = [
            c for c in add_calls if _is_registry_resolve(c.args[0], "analyze_and_route_query")
        ]
        assert len(resolved) == 1
        bare = [
            c
            for c in add_calls
            if isinstance(c.args[0], ast.Name) and c.args[0].id == "analyze_and_route_query"
        ]
        assert bare == [], "add_node 裸名参数 = 内联兜底复活"


# ---------------------------------------------------------------------------
# 6 — 运行时编译图等值（与 M3-1 第 6 组同法：读编译产物，不读源码文本）
# ---------------------------------------------------------------------------

#: supervisor 拓扑的 3 个业务节点名（`build_supervisor_graph` 的 add_node 集合；
#: handler 不再是图节点——子 agent 由 supervisor_dispatch 经 ROUTE_HANDLERS 在节点内调用）。
EXPECTED_SUPERVISOR_NODE_NAMES = frozenset(
    {"message_window", "analyze_and_route_query", "supervisor_dispatch"}
)

_PSEUDO_START = "__start__"
_PSEUDO_END = "__end__"

#: 编译图**完整边集**冻结基准：4 条三元组 `(source, target, conditional)`，
#: 按 `sorted()` 升序，写成全集相等（与 p178 的 EXPECTED_GRAPH_EDGES 同口径，
#: 不留「成员/子集判断」的假绿缺口）。
#: 取值来源：运行时一次枚举（非手抄源码）——
#:     PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "
#:       import os; os.environ.setdefault('DATABASE_URL','sqlite:///:memory:');
#:       import agentix.application.wiring;
#:       from agentix.orchestration.langgraph import lg_supervisor;
#:       g = lg_supervisor.build_supervisor_graph(checkpointer=None);
#:       print(sorted((e.source, e.target, bool(e.conditional)) for e in g.get_graph().edges))"
#: 取值日期：2026-10-29（T-M3-2；本批改造前后该图结构逐位一致，基准取自动态装配后的
#: 运行时产物本身）。**改它必须同批说明理由**——它是 M3 后续拆边批次的等价性基准线。
EXPECTED_SUPERVISOR_EDGES: Tuple[Tuple[str, str, bool], ...] = (
    (_PSEUDO_START, "message_window", False),
    ("analyze_and_route_query", "supervisor_dispatch", False),
    ("message_window", "analyze_and_route_query", False),
    ("supervisor_dispatch", _PSEUDO_END, False),
)

#: 边数单独钉一条（与 p178 同法）：常量整体被替换时给出「几条」的直接读数。
EXPECTED_SUPERVISOR_EDGE_COUNT = 4


class TestSupervisorCompiledGraphEquivalence:
    @pytest.fixture(scope="class")
    def compiled(self) -> Any:
        from agentix.orchestration.langgraph import lg_supervisor as mod

        return mod.build_supervisor_graph(checkpointer=None)

    @staticmethod
    def _edges(graph: Any) -> Tuple[Tuple[str, str, bool], ...]:
        return tuple((e.source, e.target, bool(e.conditional)) for e in graph.get_graph().edges)

    def test_business_node_set_is_exactly_the_three(self, compiled: Any) -> None:
        real = set(compiled.nodes) - {_PSEUDO_START}
        assert real == EXPECTED_SUPERVISOR_NODE_NAMES
        assert len(real) == 3, f"节点数漂移：{sorted(real)}"
        # handler 不得回到图节点面（去具体化的拓扑侧证据）：8 个键与节点集零交集
        assert not (set(compiled.nodes) & set(_HANDLER_KEYS))

    def test_full_edge_set_is_frozen(self, compiled: Any) -> None:
        """全集相等：逐条钉住每一条边，不写 `in` / 子集判断。"""
        edges = tuple(sorted(self._edges(compiled)))
        assert len(edges) == EXPECTED_SUPERVISOR_EDGE_COUNT, (
            f"边数漂移：实得 {len(edges)}，冻结基准 {EXPECTED_SUPERVISOR_EDGE_COUNT}；"
            f"差集（实 - 冻）={set(edges) - set(EXPECTED_SUPERVISOR_EDGES)}，"
            f"差集（冻 - 实）={set(EXPECTED_SUPERVISOR_EDGES) - set(edges)}"
        )
        assert edges == EXPECTED_SUPERVISOR_EDGES


# ---------------------------------------------------------------------------
# 7 — 静态守卫
# ---------------------------------------------------------------------------


def _imported_modules(path: pathlib.Path) -> List[Tuple[int, str]]:
    """全树 import 模块名（`ast.walk`：函数体内的延迟 import 同样计入）。

    口径注记（与 p178 第 7 组同）：`node.level == 0` 过滤意味着只扫**绝对导入**，
    跨包相对导入的兜底源是 p177 守卫 #5，本文件不复制第二份判据（约定 6 ④）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "agentix":
                found.extend((node.lineno, f"agentix.{alias.name}") for alias in node.names)
            else:
                found.append((node.lineno, node.module))
    return found


def _top_level_application_imports(path: pathlib.Path) -> List[Tuple[int, str]]:
    """**模块体顶层**（tree.body 直接子节点）的 agentix.application import。

    顶层 vs 全树分开判，是本批口径的最小必要拆分：§7.5 第③步拆的是「分派文件
    不再顶层 import 具体 handler」；2026-10-26 时点函数体内尚余 2 条 `llm_factory.get_llm_client`
    延迟 import（属 LLM 面、当时登记归 M3-4，该批刻意不动）。全树计数在下面单独立一条守卫
    冻结残留——2026-10-26 时点恰为 2 且全部是 llm_factory，M3-4b 卡2（2026-10-30）拆到
    0 条后升级为 0 条反向守卫：不放宽、只把残留显式登记进断言，出现任何 application import 即红。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: List[Tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            out.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name == "agentix.application"
                or alias.name.startswith("agentix.application.")
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mod = "agentix" if node.module == "agentix" else node.module
            if mod == "agentix.application" or mod.startswith("agentix.application."):
                out.append((node.lineno, mod))
    return out


def _top_level_imports_of(path: pathlib.Path, target: str) -> List[Tuple[int, str]]:
    """**模块体顶层**（tree.body 直接子节点）指向全限定模块 `target` 的 import 语句。

    这不是第二个判据源，而是上面两个既有口径（`_top_level_application_imports` 的
    「只扫 tree.body 直接子节点」 + `_imported_modules` 的「level==0 绝对导入、
    `from <父包> import <叶>` 折算成 `<父包>.<叶>` 全限定名」）原样收窄到指定目标
    模块：判定形状逐字沿用，只是把「前缀等于 agentix.application」换成「等于
    `target`」。同时覆盖 `import a.b.c.d` 与 `from a.b.c import d` 两种写法——
    契约钉的是「顶层把该模块拉进 import 期」这一效果，换写法不解约。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parent, _, leaf = target.rpartition(".")
    out: List[Tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            out.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name == target or alias.name.startswith(f"{target}.")
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == target or (
                node.module == parent and any(alias.name == leaf for alias in node.names)
            ):
                out.append((node.lineno, node.module))
    return out


class TestStaticDependencyGuards:
    def test_lg_supervisor_has_zero_top_level_application_imports(self) -> None:
        """本批目标判据：顶层 application import ⇒ 0 条（函数体内延迟 import 不计入
        本条，另由下一条冻结——2026-10-26 时点恰 2 条 llm_factory，2026-10-30 卡2 起
        为 0 条反向守卫；不许悄悄变多，也不许本批顺手删）。"""
        offenders = _top_level_application_imports(LG_SUPERVISOR_PATH)
        assert offenders == [], f"lg_supervisor.py 顶层出现 application import: {offenders}"

    def test_lg_supervisor_deferred_application_imports_are_gone(
        self,
    ) -> None:
        """全树（`ast.walk`，函数体内延迟 import 也计）反向守卫：supervisor 侧
        application 延迟 import 为 0 条。沿革：2026-10-26 冻结时恰 2 条，且目标模块都是
        `agentix.application.agents.llm_factory`（M3-2 简报「余 2 条 `get_llm_client` 归
        M3-4」的机器可读登记位）；2026-10-30 M3-4b 卡2 把两条边收到能力注入位
        （`capabilities.resolve("get_llm_client")`）⇒ 本条改为 0 条反向守卫——
        残留数只减不增，出现任何 application import 即红。"""
        residual = [
            (line, mod)
            for line, mod in _imported_modules(LG_SUPERVISOR_PATH)
            if mod == "agentix.application" or mod.startswith("agentix.application.")
        ]
        assert residual == [], f"supervisor 侧 application 延迟 import 应为 0 条，实得: {residual}"

    def test_wiring_does_not_import_langgraph(self) -> None:
        """wiring 是纯注册文件：import langgraph 会把 p66 守卫 #11 的 equality 棘轮
        白名单逼宽（node_registry 同款口径）。"""
        offenders = [
            (line, mod)
            for line, mod in _imported_modules(WIRING_PATH)
            if mod == "langgraph" or mod.startswith("langgraph.")
        ]
        assert offenders == [], f"wiring.py 出现 langgraph import: {offenders}"

    def test_orchestration_never_imports_application_wiring(self) -> None:
        """编排层不得 import 装配文件：§7.5 拆的是反向边，反向 import wiring 等于
        把 `agentix.application` 依赖换个名字绕回来（且会让 node_registry 的
        「不 import application」约束失去意义）。"""
        offenders: List[Tuple[str, int, str]] = []
        for path in sorted(ORCHESTRATION_DIR.rglob("*.py")):
            for line, mod in _imported_modules(path):
                if mod == "agentix.application.wiring" or mod.startswith(
                    "agentix.application.wiring."
                ):
                    offenders.append((str(path.relative_to(REPO_ROOT)), line, mod))
        assert offenders == [], f"orchestration 出现 wiring import: {offenders}"

    def test_lg_supervisor_has_no_top_level_import_of_lg_builder(self) -> None:
        """M3-5b 起 `lg_builder.py` **import 期**就经 `node_registry.resolve("route_query")`
        读注册表取 `route_query` 注解返回类型（langgraph 在 `add_conditional_edges`
        一刻读 `Literal`，而 `lg_builder` 在模块体就编译 graph ⇒ 取源只能发生在
        import 期）。`lg_supervisor` 自己 import 期不需要 `lg_builder`——顶层 import
        它等于把这份 import 期装配依赖**转嫁**给每一个 import supervisor 的进程，
        而编排层不许 import 装配根（上面那条守卫），supervisor 无法自救。
        `tests/test_p71_m2_orchestration_migration.py::TestLoadOrderSafety::
        test_supervisor_new_path_works_old_path_gone` 正是这么被砸红的：它在干净
        解释器（未挂线）里 import `lg_supervisor`，验证「旧路径已消失、新路径可
        导入」，顶层 import 让这一步在装配缺失态直接抛 `NodeNotRegistered`。
        唯一消费点在 `build_supervisor_graph()` 函数体内，下移为调用期 import：
        未装配时仍由 `lg_builder` 自己抛 `NodeNotRegistered`（fail-closed 不弱化）。
        附注：本守卫按 `node.level == 0` 过滤，只覆盖绝对导入形态，`from . import
        lg_builder` 这类同包相对导入由 p71 `test_supervisor_new_path_works_old_path_gone` 兜。
        """
        offenders = _top_level_imports_of(
            LG_SUPERVISOR_PATH, "agentix.orchestration.langgraph.lg_builder"
        )
        assert offenders == [], (
            "lg_supervisor.py 顶层 import lg_builder，会把 lg_builder 的 import 期"
            f"装配依赖转嫁给 supervisor 的所有 importer: {offenders}"
        )
