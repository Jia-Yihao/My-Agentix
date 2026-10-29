"""M3-3「lg_builder 节点面去具体化（延迟解析壳）」的用例。

## 覆盖面

1. **AST 双面守卫**：`lg_builder.py` 内指向 `agentix.application*` 的 import
   语句（`ast.walk` 全树口径，函数体内延迟 import 也计）**恰 8 条**且目标模块
   逐个落在冻结白名单里（= 简报口径「15 → 8」）；**顶层**（module body 直接
   子节点）对 7 个被壳替代的 handler 模块的 import **恰 0 条**（唯一例外是
   `router._ensure_router` 辅助函数位，属 M3-5 的既定边界，按「名字集合」
   显式放行——壳名若被 import 回来，白名单与名字判据都会红）。
2. **签名同源守卫（AGENTS.md 纪律 6 ④ 的兜底）**：9 个壳逐个
   `inspect.signature(壳)` 与 `inspect.signature(node_registry.resolve(键))`
   **逐字相等**（参数名、kind、keyword-only、默认值、求值后的注解）；异步形态
   用 `node_registry.is_awaitable_result` 与注册项 `awaits` 对账（判据与注册表
   同源，不用 `iscoroutinefunction` 的字面判据）。这条是壳方案唯一的防腐守卫：
   把某个壳的 `config` 写成位置参数、删掉 `config` 形参（langgraph 就注入不到
   config）、或改参数名，都会当场炸。
3. **转发保真守卫**：把真身换成探针（patch application 模块属性，走 wiring 的
   thunk），断言 ① 壳打到**换后**的对象（调用期现取，覆盖面比改造前的
   import 期快照宽）；② `state` / `config` **同一对象**传下去（不复制、不裁剪
   ——HITL 的 `interrupt`/`stream_hitl` 通道就活在 config 里）；③ 返回值逐字
   透传。`execute_task` 的壳另经 `_wrap_execute_task_in_loop` 包一层再验保真。
4. **fail-closed 守卫**：独立 `NodeRegistry()` 空实例顶掉 `DEFAULT_REGISTRY`
   （不污染全局），壳必须抛 `NodeNotRegistered` 且消息含 `WIRING_HINT`——
   绝不静默降级、绝不返回 None。
5. **建图不依赖注册表守卫（本批存在的理由）**：subprocess 起干净解释器跑
   P1/P2/P3 三条探针——P1：只 import lg_builder，建图成功且 10 节点齐全、
   注册表为空（评测/基准链路零影响）；P2：同解释器对图 `ainvoke` 以
   `NodeNotRegistered` 响亮失败（不许有 import 期快照兜底）；P3：import
   chat_service 后注册表恰为 wiring 的 10 键。
6. **编译图等值守卫**：节点名集合与**完整边集**等于 p178 的
   `EXPECTED_GRAPH_NODE_NAMES` / `EXPECTED_GRAPH_EDGES`（**引用**该模块，
   不在本文件抄第二份）；10 处 `builder.add_node` 的 kwargs 形态
   （`**readonly_retry_kwargs` ×6、`**node_timeout_kwargs` ×10、
   `**node_error_handler_kwargs` ×2）未被改动。
7. **route_query 键≠壳名守卫**：`lg_builder.create_code_query` 解析的是
   `"code_agent"` 键（wiring 登记 `code_agent` → `code_handler.create_code_query`），
   不是 `"create_code_query"`。
8. **S2 收口（M3-2 遗留一般项）**：`_RegistryBackedHandlers` 对非静态键
   「可写不可读」的不对称已在该类 docstring 明文声明；本文件钉住
   「写入非静态键后 `in` / `iter` / `len` 均不可见、`get` 返回 default」这一
   **已声明**行为（只声明，不改行为）。

## 验收口径

- 项目级口径：`docs/architecture/分层架构与依赖规则.md` §7.5 第③步、
  `agentix/orchestration/node_registry.py` 的三条设计口径（fail-closed /
  零参 thunk 每次现取 / 形态声明唯一判据源）、
  `agentix/application/wiring.py` 的键名与形态登记（壳的键映射以它为准，
  本文件不另立第二份映射）。
- 本文件对 `DEFAULT_REGISTRY` / `ROUTE_HANDLERS` 覆盖表一律快照并还原
  （同 p178/p179 隔离口径）；subprocess 探针不改动本进程任何全局状态。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import pathlib
import subprocess
import sys
from typing import Any, List, Tuple

import pytest

import agentix.application.agents.code_handler as code_handler
import agentix.application.agents.file_handler as file_handler
import agentix.application.agents.general_handler as general_handler
import agentix.application.agents.guardrails as guardrails_module
import agentix.application.agents.image_handler as image_handler
import agentix.application.agents.router as router_module
import agentix.application.agents.task_handler as task_handler
from agentix.orchestration import node_registry
from agentix.orchestration.langgraph import lg_builder
from agentix.orchestration.langgraph.lg_supervisor import ROUTE_HANDLERS
from agentix.orchestration.node_registry import (
    WIRING_HINT,
    DEFAULT_REGISTRY,
    NodeNotRegistered,
    NodeRegistry,
)
from tests.test_p178_node_registry import (
    EXPECTED_GRAPH_EDGES,
    EXPECTED_GRAPH_EDGE_COUNT,
    EXPECTED_GRAPH_NODE_NAMES,
    EXPECTED_WIRED_NAMES,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LG_BUILDER_PATH = REPO_ROOT / "agentix" / "orchestration" / "langgraph" / "lg_builder.py"

#: 壳名 → 注册键（以 `agentix/application/wiring.py` 的登记为唯一真源，本表只是
#: 测试侧对「哪 9 个壳必须存在」的清单——逐条映射在守卫里现场核对注册表）。
#: 唯一「壳名 ≠ 键」的是 `create_code_query` → `code_agent`。
SHELL_TO_KEY: dict = {
    "analyze_and_route_query": "analyze_and_route_query",
    "route_query": "route_query",
    "respond_to_general_query": "respond_to_general_query",
    "create_kb_query": "create_kb_query",
    "get_additional_info": "get_additional_info",
    "create_image_query": "create_image_query",
    "create_file_query": "create_file_query",
    "execute_task": "execute_task",
    "create_code_query": "code_agent",
}

#: 被壳替代、顶层必须零 import 的 7 个 application 模块（简报点名的 7 条边）。
#: `router` 只禁**壳名**——`router._ensure_router`（纯辅助函数位）是 M3-5 的
#: 既定边界，按名字显式放行；把壳名 import 回来正是本批要拆的边。
SHELL_SOURCE_MODULES: dict = {
    "agentix.application.agents.router": {
        "analyze_and_route_query",
        "route_query",
        "respond_to_general_query",
    },
    "agentix.application.agents.guardrails": {"get_additional_info"},
    "agentix.application.agents.image_handler": {"create_image_query"},
    "agentix.application.agents.file_handler": {"create_file_query"},
    "agentix.application.agents.general_handler": {
        "respond_to_general_query",
        "create_kb_query",
    },
    "agentix.application.agents.task_handler": {"execute_task"},
    "agentix.application.agents.code_handler": {"create_code_query"},
}

#: lg_builder 全树 application import 的冻结白名单（8 条，多一条少一条都红）。
#: 名称取 import 目标模块的**完整模块名**；`router` 出现两条（lg_prompts 之外的
#: :44 `_ensure_router`），白名单是「逐条可枚出的模块名集合」，条数由计数断言单独钉。
APPLICATION_IMPORT_BASENAME_ALLOWLIST: frozenset = frozenset(
    {
        "lg_prompts",
        "kg_neo4j_conn",
        "legal_retriever",
        "multi_tool",
        "llm_factory",
        "router",
        "constants",
        "legal_cypher_dict",
    }
)
EXPECTED_APPLICATION_IMPORT_COUNT = 8


def _builder_tree() -> ast.Module:
    return ast.parse(LG_BUILDER_PATH.read_text(encoding="utf-8"))


def _application_imports_full_tree(tree: ast.Module) -> List[ast.AST]:
    """全树口径收集指向 agentix.application* 的 import 语句（函数体内延迟 import 也计，
    与 p177/p71 的取数口径一致——静态可达即算依赖）。"""
    out: List[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.startswith("agentix.application") for a in node.names):
                out.append(node)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("agentix.application"):
                out.append(node)
    return out


# ---------------------------------------------------------------------------
# 夹具：注册表 / 覆盖表隔离（同 p178/p179 口径，快照并还原）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_default_registry() -> Any:
    snapshot = dict(DEFAULT_REGISTRY._entries)  # noqa: SLF001 — 测试侧还原单例的唯一办法
    overrides = dict(ROUTE_HANDLERS._overrides)  # noqa: SLF001
    yield
    DEFAULT_REGISTRY._entries.clear()  # noqa: SLF001
    DEFAULT_REGISTRY._entries.update(snapshot)  # noqa: SLF001
    ROUTE_HANDLERS._overrides.clear()  # noqa: SLF001
    ROUTE_HANDLERS._overrides.update(overrides)  # noqa: SLF001


# ---------------------------------------------------------------------------
# 1 — AST 双面守卫
# ---------------------------------------------------------------------------


class TestAstImportGuards:
    def test_application_import_statements_exactly_eight(self) -> None:
        """全树口径 application import 语句**恰 8 条**（15 → 8，集合相等不写 `<=`）。"""
        found = _application_imports_full_tree(_builder_tree())
        assert len(found) == EXPECTED_APPLICATION_IMPORT_COUNT, (
            f"application import 语句数漂移：实得 {len(found)}，冻结基准 "
            f"{EXPECTED_APPLICATION_IMPORT_COUNT}"
        )
        targets = set()
        for node in found:
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names if a.name.startswith("agentix.application")]
            else:
                names = [node.module or ""]
            for name in names:
                targets.add(name.rsplit(".", 1)[-1])
        assert targets == set(APPLICATION_IMPORT_BASENAME_ALLOWLIST), (
            f"目标模块集合漂移：实得 {sorted(targets)}，白名单 "
            f"{sorted(APPLICATION_IMPORT_BASENAME_ALLOWLIST)}"
        )

    def test_top_level_shell_module_imports_zero(self) -> None:
        """顶层对 7 个 handler 模块的壳名 import **恰 0 条**。

        壳名一旦被 import 回来，建图期就会把具体实现绑回模块属性——本批的边
        等于没拆；纯别名（_heuristic_router 等）零消费者，也已随壳一并删除。
        """
        offenders: List[str] = []
        for node in _builder_tree().body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "agentix.application"
            ):
                got = {a.name for a in node.names}
                banned = SHELL_SOURCE_MODULES.get(node.module or "")
                if banned is None:
                    continue
                hit = got & banned
                if hit:
                    offenders.append(f"{node.module}:{sorted(hit)}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in SHELL_SOURCE_MODULES:
                        offenders.append(alias.name)
        assert offenders == [], f"顶层 import 回来了被壳替代的名字: {offenders}"

    def test_removed_pure_reexport_aliases_stay_removed(self) -> None:
        """四个零消费者纯 re-export 别名不得被顺手加回来（全树口径）。"""
        banned = {"_heuristic_router", "_extract_configurable", "_coerce_to_bool",
                  "AdditionalGuardrailsOutput"}
        for node in ast.walk(_builder_tree()):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "agentix.application"
            ):
                hit = {a.name for a in node.names} & banned
                assert not hit, f"零消费者的别名 {hit} 被重新 import 进 lg_builder"


# ---------------------------------------------------------------------------
# 2 — 签名同源守卫（壳方案唯一的防腐守卫）
# ---------------------------------------------------------------------------


class TestShellSignatures:
    @pytest.mark.parametrize("shell_name", sorted(SHELL_TO_KEY))
    def test_shell_signature_equals_registry_target(self, shell_name: str) -> None:
        """签名逐字相等（参数名 / kind / keyword-only / 默认值 / 求值后注解）。

        不做这条，「壳与被替换函数签名相同」就只是注释里的一句话；config
        写成位置参数或删掉 config 形参（langgraph 将注入不到 config）都无声。
        """
        key = SHELL_TO_KEY[shell_name]
        shell = getattr(lg_builder, shell_name)
        real = node_registry.resolve(key)
        sig_shell = inspect_signature(shell)
        sig_real = inspect_signature(real)
        assert sig_shell == sig_real, (
            f"壳 {shell_name} 与注册项 {key} 的签名不一致：壳={sig_shell}，"
            f"真身={sig_real}"
        )
        # 显式钉 keyword-only 的 config（注解相等可能被同名同 kind 的形态变化掩盖）
        params = list(sig_shell.parameters.values())
        has_config = any(p.name == "config" and p.kind == p.KEYWORD_ONLY for p in params)
        assert has_config == (node_registry.entry(key).takes_config is True), (
            f"壳 {shell_name} 的 keyword-only `config` 形参与注册项 takes_config 不同源"
        )

    @pytest.mark.parametrize("shell_name", sorted(SHELL_TO_KEY))
    def test_shell_async_form_matches_registered_awaits(self, shell_name: str) -> None:
        """异步形态与注册项 `awaits` 对账，判据用 `is_awaitable_result`（纪律 6 ④）。

        对真身做一次**不执行**的探测：异步函数拿到的只是未运行的协程对象
        （零副作用，用完即关）；同步真身（route_query）按登记形态只带 state，
        对探针入参立刻抛错或返回非 awaitable —— 判据仍走 is_awaitable_result。
        """
        key = SHELL_TO_KEY[shell_name]
        entry = node_registry.entry(key)
        real = node_registry.resolve(key)
        try:
            if entry.takes_config:
                result: Any = real(_ProbeState(), config={})
            else:
                result = real(_ProbeState())
        except Exception:  # noqa: BLE001 - 同步真身对探针入参抛错即视为非 awaitable，判据仍走 is_awaitable_result
            result = None
        awaiting = node_registry.is_awaitable_result(result)
        if asyncio.iscoroutine(result):
            result.close()
        assert awaiting is entry.awaits, (
            f"{key}：注册项 awaits={entry.awaits}，但真身调用结果按 "
            f"is_awaitable_result 判为 {awaiting}（判据必须与注册表同源）"
        )
        # 壳自身的可等待形态必须与注册项一致（壳把 awaits 判错会静默改执行语义）
        shell = getattr(lg_builder, shell_name)
        assert asyncio.iscoroutinefunction(shell) is entry.awaits


def inspect_signature(fn: Any) -> Any:
    """签名读取的唯一入口：`eval_str=True` 把字符串注解求值后再比。

    壳与真身分处两个模块、注解书写形态可能不同（真身带返回注解，壳刻意不带），
    不先归一「返回注解」这个**已声明不抄**的维度，比较会永远红；其余全部原样比。
    """
    sig = inspect.signature(fn, eval_str=True)
    # 返回注解按简报口径刻意不抄 → 对比前两侧都剥掉；剥掉后其余必须逐字相等。
    return sig.replace(return_annotation=inspect.Signature.empty)


class _ProbeState:
    """给同步真身的最小探针入参：任何属性访问都抛，保证探测不产生真实副作用。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"probe state touched {name!r}")

    def __getitem__(self, key: Any) -> Any:
        raise AssertionError(f"probe state indexed by {key!r}")


# ---------------------------------------------------------------------------
# 3 — 转发保真守卫
# ---------------------------------------------------------------------------


class _ForwardProbe:
    """记录调用入参并返回固定哨兵（同一对象透传，可断言 is 同一性）。"""

    def __init__(self, *, awaits: bool) -> None:
        self.seen_state: Any = _NOT_CALLED
        self.seen_config: Any = _NOT_CALLED
        self.calls = 0
        self.ret = {"probe": "return"}
        self._awaits = awaits

    async def __call__(self, state, *, config=None):
        self.calls += 1
        self.seen_state = state
        self.seen_config = config
        return self.ret

    def sync_call(self, state):
        self.calls += 1
        self.seen_state = state
        return self.ret


_NOT_CALLED: Any = object()

#: 壳名 → 真身所在模块与属性名（patch 这里 = 走 wiring thunk 的「模块形态」，
#: 证明壳打到的是**换后**的对象，覆盖面比改造前的 import 期快照宽）。
SHELL_TO_REAL_SOURCE: dict = {
    "analyze_and_route_query": (router_module, "analyze_and_route_query"),
    "respond_to_general_query": (general_handler, "respond_to_general_query"),
    "create_kb_query": (general_handler, "create_kb_query"),
    "get_additional_info": (guardrails_module, "get_additional_info"),
    "create_image_query": (image_handler, "create_image_query"),
    "create_file_query": (file_handler, "create_file_query"),
    "execute_task": (task_handler, "execute_task"),
    "create_code_query": (code_handler, "create_code_query"),
}


class TestForwardingFidelity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("shell_name", sorted(SHELL_TO_REAL_SOURCE))
    async def test_async_shell_forwards_same_objects(self, shell_name: str, monkeypatch) -> None:
        module, attr = SHELL_TO_REAL_SOURCE[shell_name]
        probe = _ForwardProbe(awaits=True)
        monkeypatch.setattr(module, attr, probe)

        state, config = object(), {"configurable": {"thread_id": "t"}}
        out = await getattr(lg_builder, shell_name)(state, config=config)

        assert probe.calls == 1, "壳没有打到 patch 后的真身（疑似 import 期快照）"
        assert probe.seen_state is state
        assert probe.seen_config is config, "config 必须同一对象传下去（HITL 通道活在 config 里）"
        assert out is probe.ret, "返回值必须逐字透传，不许包装/复制"

    def test_route_query_shell_forwards_same_state(self, monkeypatch) -> None:
        probe_state = object()
        probe = _ForwardProbe(awaits=False)
        monkeypatch.setattr(router_module, "route_query", probe.sync_call)

        out = lg_builder.route_query(probe_state)

        assert probe.calls == 1
        assert probe.seen_state is probe_state
        assert out is probe.ret

    @pytest.mark.asyncio
    async def test_execute_task_shell_fidelity_through_hitl_wrap(self, monkeypatch) -> None:
        """`execute_task` 的壳经 `_wrap_execute_task_in_loop` 包一层后仍保真。"""
        probe = _ForwardProbe(awaits=True)
        monkeypatch.setattr(task_handler, "execute_task", probe)

        wrapped = lg_builder._wrap_execute_task_in_loop(lg_builder.execute_task)
        state, config = object(), {"configurable": {"thread_id": "t"}}
        out = await wrapped(state, config=config)

        assert probe.calls == 1
        assert probe.seen_state is state
        assert probe.seen_config is config, "flag-off/未声明进环时包装壳必须原样透传 config"
        assert out is probe.ret

    def test_shells_resolve_at_call_time_not_import_time(self, monkeypatch) -> None:
        """壳在被调用时才读 DEFAULT_REGISTRY：换注册表实例即换实现（无 import 期快照）。

        建图发生在 import 期；若建图时把真身快照进节点，本用例在**建好图之后**
        顶掉注册表仍应打到旧实现——那才是壳要防住的形态，这里断言它打不到。
        """
        calls: List[int] = []

        async def late(state, *, config):
            calls.append(1)
            return {"late": True}

        registry = NodeRegistry()
        registry.register("respond_to_general_query", lambda: late, awaits=True, takes_config=True)
        monkeypatch.setattr(node_registry, "DEFAULT_REGISTRY", registry)

        out = asyncio.run(lg_builder.respond_to_general_query(object(), config={}))
        assert out == {"late": True}
        assert calls == [1]


# ---------------------------------------------------------------------------
# 4 — fail-closed 守卫
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("shell_name", sorted(set(SHELL_TO_KEY) - {"route_query"}))
    async def test_unregistered_key_raises_wiring_hint(self, shell_name: str, monkeypatch) -> None:
        """空注册表顶掉 DEFAULT_REGISTRY ⇒ 壳抛 NodeNotRegistered（含 WIRING_HINT）。

        独立 NodeRegistry() + monkeypatch，绝不触碰全局实例；且不许 except 后
        返回 None / 走内联兜底——静默降级会把「装配没跑」伪装成「跑通了」。
        """
        monkeypatch.setattr(node_registry, "DEFAULT_REGISTRY", NodeRegistry())

        shell = getattr(lg_builder, shell_name)
        with pytest.raises(NodeNotRegistered) as excinfo:
            await shell(object(), config={})
        assert WIRING_HINT in str(excinfo.value)
        assert SHELL_TO_KEY[shell_name] in str(excinfo.value)

    def test_route_query_shell_fail_closed(self, monkeypatch) -> None:
        """同步壳：调用即抛，不 await、不降级。"""
        monkeypatch.setattr(node_registry, "DEFAULT_REGISTRY", NodeRegistry())
        with pytest.raises(NodeNotRegistered) as excinfo:
            lg_builder.route_query(object())
        assert WIRING_HINT in str(excinfo.value)
        assert "route_query" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5 — 建图不依赖注册表守卫（subprocess 干净解释器，P1/P2/P3）
# ---------------------------------------------------------------------------

_P1_PROBE = """
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import agentix.orchestration.langgraph.lg_builder as b
from agentix.orchestration.node_registry import DEFAULT_REGISTRY
nodes = set(b.graph.get_graph().nodes) - {"__start__", "__end__"}
print("NODES:", sorted(nodes))
print("REGISTRY_EMPTY:", DEFAULT_REGISTRY.names() == ())
"""

_P2_PROBE = """
import os, asyncio
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import agentix.orchestration.langgraph.lg_builder as b
from agentix.orchestration.node_registry import DEFAULT_REGISTRY, WIRING_HINT, NodeNotRegistered
assert DEFAULT_REGISTRY.names() == (), "前置：装配未加载"
async def main():
    return await b.graph.ainvoke({"messages": []}, config={"configurable": {"thread_id": "p2"}})
try:
    asyncio.run(main())
    print("OUTCOME: no-raise (BAD)")
except NodeNotRegistered as exc:
    print("OUTCOME: NodeNotRegistered; HINT_IN_MSG:", WIRING_HINT in str(exc))
except BaseException as exc:
    print("OUTCOME: other-error(BAD):", type(exc).__name__, repr(exc)[:200])
"""

_P3_PROBE = """
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import agentix.application.services.chat_service  # noqa: F401
from agentix.orchestration.node_registry import DEFAULT_REGISTRY
print("NAMES:", sorted(DEFAULT_REGISTRY.names()))
"""


def _run_probe(code: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return proc


class TestGraphBuildIndependentOfRegistry:
    def test_p1_build_graph_without_registry(self) -> None:
        """P1：干净解释器只 import lg_builder ⇒ 建图成功、10 节点齐、注册表空。"""
        proc = _run_probe(_P1_PROBE)
        assert proc.returncode == 0, f"P1 import 期即失败：{proc.stderr[-800:]}"
        nodes_line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("NODES:")), "")
        got = set(eval(nodes_line.split("NODES:", 1)[1].strip()))
        assert got == EXPECTED_GRAPH_NODE_NAMES, f"P1 节点集漂移：{sorted(got)}"
        assert "REGISTRY_EMPTY: True" in proc.stdout, (
            f"P1 注册表非空（import 链里混进了装配）：{proc.stdout}"
        )

    def test_p2_ainvoke_fails_loud_without_registry(self) -> None:
        """P2：装配缺失 ⇒ 首次执行即 NodeNotRegistered（无 import 期快照兜底）。"""
        proc = _run_probe(_P2_PROBE)
        assert proc.returncode == 0, f"P2 解释器崩了：{proc.stderr[-800:]}"
        assert "OUTCOME: NodeNotRegistered; HINT_IN_MSG: True" in proc.stdout, (
            f"P2 读数异常（要么静默成功、要么错成了别的异常）：{proc.stdout}"
        )

    def test_p3_chat_service_wires_ten_keys(self) -> None:
        """P3：import chat_service ⇒ 注册表恰为 wiring 登记的 10 键。"""
        proc = _run_probe(_P3_PROBE)
        assert proc.returncode == 0, f"P3 import 失败：{proc.stderr[-800:]}"
        names_line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("NAMES:"))
        got = eval(names_line.split("NAMES:", 1)[1].strip())
        assert tuple(got) == EXPECTED_WIRED_NAMES, f"P3 注册键集 != wiring 10 键：{got}"


# ---------------------------------------------------------------------------
# 6 — 编译图等值守卫
# ---------------------------------------------------------------------------


class TestCompiledGraphEquivalence:
    def test_nodes_and_edges_equal_p178_baseline(self) -> None:
        compiled = lg_builder.build_graph(checkpointer=None)
        real_nodes = set(compiled.nodes) - {"__start__"}
        assert real_nodes == EXPECTED_GRAPH_NODE_NAMES
        assert len(real_nodes) == 10, f"节点数漂移：{sorted(real_nodes)}"
        edges = tuple(
            sorted((e.source, e.target, bool(e.conditional)) for e in compiled.get_graph().edges)
        )
        assert len(edges) == EXPECTED_GRAPH_EDGE_COUNT, (
            f"边数漂移：实得 {len(edges)}，冻结基准 {EXPECTED_GRAPH_EDGE_COUNT}；"
            f"差集（实 - 冻）={set(edges) - set(EXPECTED_GRAPH_EDGES)}，"
            f"差集（冻 - 实）={set(EXPECTED_GRAPH_EDGES) - set(edges)}"
        )
        assert edges == EXPECTED_GRAPH_EDGES

    def test_add_node_kwargs_shape_untouched(self) -> None:
        """10 处 add_node 的 `**` 展开形态未被改动（p147 判据 2 豁免的正是这个形态）。"""
        tree = _builder_tree()
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "build_graph"
        )
        calls = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_node"
        ]
        assert len(calls) == 10

        def spreads(call: ast.Call) -> set:
            return {
                kw.value.id
                for kw in call.keywords
                if kw.arg is None and isinstance(kw.value, ast.Name)
            }

        all_spreads = [spreads(c) for c in calls]
        timeout_nodes = [s for s in all_spreads if "node_timeout_kwargs" in s]
        readonly_nodes = [s for s in all_spreads if "readonly_retry_kwargs" in s]
        error_nodes = [s for s in all_spreads if "node_error_handler_kwargs" in s]
        assert len(timeout_nodes) == 10, "`**node_timeout_kwargs` 覆盖面不是 10"
        assert len(readonly_nodes) == 6, "`**readonly_retry_kwargs` 覆盖面不是 6 个只读节点"
        assert len(error_nodes) == 2, "`**node_error_handler_kwargs` 覆盖面不是 2 个有副作用节点"
        # add_node 不许出现内联策略构造（p147 同源口径，防止本批顺手改了实参形态）
        for call in calls:
            for kw in call.keywords:
                if kw.arg in ("retry_policy", "error_handler", "timeout", "cache_policy"):
                    assert isinstance(kw.value, ast.Name), (
                        f"add_node 策略实参必须是具名 kwargs 变量，不许内联：{ast.dump(call)[:120]}"
                    )


# ---------------------------------------------------------------------------
# 7 — 键≠壳名守卫
# ---------------------------------------------------------------------------


class TestKeyMapping:
    @pytest.mark.asyncio
    async def test_create_code_query_shell_resolves_code_agent_key(self, monkeypatch) -> None:
        """`create_code_query` 壳必须解析 `"code_agent"` 键（wiring 登记原样）。"""
        probe = _ForwardProbe(awaits=True)
        # 键换成 code_agent 的实现（探针）；若壳解析的是别的键，这里打不中或当场抛
        monkeypatch.setattr(code_handler, "create_code_query", probe)

        registry = NodeRegistry()
        registry.register("code_agent", lambda: probe, awaits=True, takes_config=True)
        monkeypatch.setattr(node_registry, "DEFAULT_REGISTRY", registry)

        out = await lg_builder.create_code_query(object(), config={})
        assert probe.calls == 1, "壳没有解析 code_agent 键"
        assert out is probe.ret

    def test_wiring_key_to_provider_mapping_still_code_agent(self) -> None:
        """wiring 侧判据源核对：`code_agent` 的 provider 就是 code_handler.create_code_query。

        「create_code_query」从来不是注册键——壳名≠键是 wiring 的登记事实，不是巧合。
        `entry` 对缺失键抛 `NodeNotRegistered`（fail-closed，不返回 None）。
        """
        assert DEFAULT_REGISTRY.resolve("code_agent") is code_handler.create_code_query
        with pytest.raises(NodeNotRegistered):
            DEFAULT_REGISTRY.entry("create_code_query")


# ---------------------------------------------------------------------------
# 8 — S2 收口：非静态键「可写不可读」（已声明行为的钉子）
# ---------------------------------------------------------------------------


class TestRegistryBackedDeclaredAsymmetry:
    def test_nonstatic_key_writable_but_invisible(self) -> None:
        """写入非静态键后 `in`/`iter`/`len` 均不可见，`get` 返回 default。

        这是 M3-2 遗留一般项 S2 的**已声明**行为（见 `_RegistryBackedHandlers`
        docstring 的声明段），本用例只钉现状、不改语义。
        """
        sentinel = object()
        ROUTE_HANDLERS["__nonstatic_key__"] = sentinel

        assert "__nonstatic_key__" not in ROUTE_HANDLERS
        assert "__nonstatic_key__" not in list(ROUTE_HANDLERS)
        assert len(ROUTE_HANDLERS) == 8
        assert ROUTE_HANDLERS.get("__nonstatic_key__", "default") == "default"
        with pytest.raises(KeyError):
            ROUTE_HANDLERS["__nonstatic_key__"]
