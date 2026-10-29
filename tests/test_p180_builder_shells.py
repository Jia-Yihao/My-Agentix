"""M3-3「lg_builder 节点面去具体化（延迟解析壳）」的用例。

## 覆盖面

1. **AST 双面守卫**：`lg_builder.py` 内指向 `agentix.application*` 的 import
   语句（`ast.walk` 全树口径，函数体内延迟 import 也计）**恰 1 条**且目标模块
   逐个落在冻结白名单里（M3-3 的「15 → 8」，M3-4 把 `create_research_plan` 真身
   迁出后收口到 3 条，M3-5b 再拆掉 `router._ensure_router` ⇒ 2026-10-29 时点
   2 条：`lg_prompts` / `llm_factory`；M3-4b 卡2（2026-10-30）拆掉 `llm_factory`
   ⇒ 现为 1 条：`lg_prompts`）；
   **顶层**（module body 直接子节点）对 8 个被壳替代的 handler 模块的 import
   **恰 0 条**（M3-5b 之前唯一的例外是 `router._ensure_router` 辅助函数位，
   那条边已随本批拆掉——按「名字集合」显式放行的口子仍在，放行名单为空）。
2. **签名同源守卫（AGENTS.md 纪律 6 ④ 的兜底）**：10 个壳逐个
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
5. **干净解释器的 import / 调用期契约守卫**：subprocess 起干净解释器跑
   P1/P2/P3/P5 四条探针（M3-5b 后 P1/P2 的「未装配」前提已反号，见该节注释）——
   P1：只 import lg_builder ⇒ 非零退出，stderr 含 `NodeNotRegistered` 与
   `WIRING_HINT`（取源块经注册表现取，未装配连 import 都过不去，fail-closed）；
   P2：先挂线建图、再清空注册表 ⇒ 调用期仍 `NodeNotRegistered`（守卫判据不变，
   只是把「未装配」从 import 期挪到调用期）；P3：import chat_service 后注册表
   恰为 wiring 的 10 键；P5：真实建图后 `analyze_and_route_query` 的出边集合
   等于 p178 `EXPECTED_GRAPH_EDGES` 筛出的分支集，不塌缩成 `{__end__}`。
5b. **import 顺序契约守卫（M3-5b）**：P4 探针用 `sys.meta_path` 在
   `lg_builder` 模块体**即将执行**的那一刻取样，断言注册表已含 `route_query`
   ——取源块改经注册表后，本模块的 import 期就要求装配已加载，`chat_service`
   的挂线位必须早于它对 `lg_builder` 的 import。
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
import agentix.application.agents.research_handler as research_handler
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
#: 测试侧对「哪 10 个壳必须存在」的清单——逐条映射在守卫里现场核对注册表）。
#: 唯一「壳名 ≠ 键」的是 `create_code_query` → `code_agent`。
SHELL_TO_KEY: dict = {
    "analyze_and_route_query": "analyze_and_route_query",
    "route_query": "route_query",
    "respond_to_general_query": "respond_to_general_query",
    "create_kb_query": "create_kb_query",
    "get_additional_info": "get_additional_info",
    "create_image_query": "create_image_query",
    "create_file_query": "create_file_query",
    "create_research_plan": "create_research_plan",
    "execute_task": "execute_task",
    "create_code_query": "code_agent",
}

#: 被壳替代、顶层必须零 import 的 8 个 application 模块（M3-3 点名的 7 条边 +
#: M3-4 迁入的 research_handler）。
#: `router` 禁的是**壳名**；M3-5b 之前该模块还留着一个按名字放行的辅助函数位
#: `router._ensure_router`（`route_query` 的注解取源要用它），那条边已随本批拆掉
#: ⇒ 本表对 `router` 只禁壳名，其余名字也不再被 import（白名单里已无 `router`）。
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
    "agentix.application.agents.research_handler": {"create_research_plan"},
    "agentix.application.agents.task_handler": {"execute_task"},
    "agentix.application.agents.code_handler": {"create_code_query"},
}

#: lg_builder 全树 application import 的冻结白名单（1 条，多一条少一条都红）。
#: 名称取 import 目标模块的**完整模块名**；条数由计数断言单独钉。
#: 两条的归口：`lg_prompts` 由 p71 字面钉住（业务 prompt 留 application）、
#: `llm_factory` 的消费者是 `check_hallucinations`（DEAD-CODE(P2-5) 有意不接线，
#: 它与 supervisor 侧的同名边一起归 M3-4b）。第三条 `router._ensure_router` 已随
#: M3-5b 拆掉——注解取源改经节点注册表
#: （2026-10-29 时点：全仓 `orchestration → application` 残留边 5 → 4）。
#: （2026-10-30 时点：M3-4b 卡2 收下 `llm_factory` 这一条——`check_hallucinations`
#: 体内取源改经能力注入位 `capabilities.resolve("get_llm_client")`，白名单余 1 条。）
APPLICATION_IMPORT_BASENAME_ALLOWLIST: frozenset = frozenset(
    {
        "lg_prompts",
    }
)
EXPECTED_APPLICATION_IMPORT_COUNT = 1


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
    def test_application_import_statements_exactly_two(self) -> None:
        """全树口径 application import 语句**恰 2 条**（M3-3 收 8 条，M3-4 迁走
        `create_research_plan` 真身后收口到 3 条，M3-5b 拆掉 `router._ensure_router`
        后为 2 条；集合相等不写 `<=`）。"""
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
        """顶层对 `SHELL_SOURCE_MODULES` 列出的 8 个模块的壳名 import **恰 0 条**。

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
    "create_research_plan": (research_handler, "create_research_plan"),
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
# 5 — 干净解释器的 import / 调用期契约守卫（subprocess，P1/P2/P3/P5）
# ---------------------------------------------------------------------------

# P1（M3-5b 反号后的前提）：取源块改经注册表现取真身 ⇒ **未装配连 import 都过不去**。
# 改造前这条探针断言的是「只 import lg_builder 也能建图成功且注册表空」，本批之后该
# 前提不再成立；剩下的正确事实是：未装配时在 import 期就响亮失败（fail-closed）。
_P1_PROBE = """
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import agentix.orchestration.langgraph.lg_builder as b
print("IMPORTED (BAD):", b.__name__)
"""

# P2：守卫主体不变（调用期 fail-closed + WIRING_HINT），只是把「未装配」从 import 期
# 挪到调用期——先挂线建好图，再清空 DEFAULT_REGISTRY 的登记项（p179::_unwire 同法）。
_P2_PROBE = """
import os, asyncio
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import agentix.application.wiring  # noqa: F401 - 先装配，lg_builder 的 import 期取源才走得通
import agentix.orchestration.langgraph.lg_builder as b
from agentix.orchestration.node_registry import DEFAULT_REGISTRY, WIRING_HINT, NodeNotRegistered
DEFAULT_REGISTRY._entries.clear()  # 未装配态（p179::_unwire 同法）
assert DEFAULT_REGISTRY.names() == (), "前置：注册表已清空"
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

# P5（M3-5b 其二的塌缩防线）：把编译图里 `analyze_and_route_query` 的**出边集合**打出来。
# 期望集合不在本文件抄字面量——由测试侧从 p178 的 EXPECTED_GRAPH_EDGES 按源节点筛出。
# 挂接失效时 langgraph 会静默把条件边塌成 `{__end__}`（读代码看不出来，只有实际出边能证）。
_P5_PROBE = """
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import agentix.application.wiring  # noqa: F401 - lg_builder 的 import 期取源需要注册表
from agentix.orchestration.langgraph import lg_builder
edges = lg_builder.graph.get_graph().edges
targets = sorted({e.target for e in edges if e.source == "analyze_and_route_query"})
print("BRANCH_TARGETS:", targets)
"""

_P4_ORDER_PROBE = """
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import sys
from importlib.abc import MetaPathFinder
from agentix.orchestration import node_registry

_TARGET = "agentix.orchestration.langgraph.lg_builder"


class _Spy(MetaPathFinder):
    # 取样点：lg_builder 的模块体**将要执行**的那一刻（find_spec 命中且仅首次）。
    fired = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname == _TARGET and not _Spy.fired:
            _Spy.fired = True
            print("ROUTE_QUERY_WIRED_BEFORE_LG_BUILDER:", "route_query" in node_registry.names())
        return None


sys.meta_path.insert(0, _Spy())
import agentix.application.services.chat_service  # noqa: F401
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


class TestCleanInterpreterImportContract:
    def test_p1_unwired_import_fails_loudly(self) -> None:
        """P1（前提已反号）：干净解释器 import lg_builder ⇒ 非零退出，
        stderr 含 `NodeNotRegistered` 与 `WIRING_HINT`。

        改造前这条断言的是「未装配也能建图成功」（建图期不读注册表）；M3-5b 把
        `route_query` 的注解取源改经注册表现取，而取源必须发生在 import 期建图之前
        ⇒ 未装配连 import 都过不去，这是设计口径（fail-closed），不是回归。
        """
        proc = _run_probe(_P1_PROBE)
        assert proc.returncode != 0, (
            f"P1 未装配竟 import 成功（说明取源块还留着 import 期兜底或退回具体模块）："
            f"{proc.stdout[-400:]}"
        )
        assert "NodeNotRegistered" in proc.stderr, (
            f"P1 stderr 无该异常名：{proc.stderr[-800:]}"
        )
        assert WIRING_HINT in proc.stderr, f"P1 stderr 无装配缺失短语：{proc.stderr[-800:]}"
        assert "IMPORTED (BAD)" not in proc.stdout

    def test_p2_ainvoke_fails_loud_without_registry(self) -> None:
        """P2：装配缺失（调用期）⇒ 首次执行即 NodeNotRegistered（无 import 期快照兜底）。

        守卫判的东西与改造前一致，只是「未装配」这一前提从 import 期挪到调用期：
        先 import wiring 让建图过得去，再清空注册表，然后调图。
        """
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

    def test_p5_conditional_edges_do_not_collapse(self) -> None:
        """P5：真实建图后 `analyze_and_route_query` 的出边集合**不塌缩**成 `{__end__}`。

        判据取自 p178 的 `EXPECTED_GRAPH_EDGES`（按源节点筛出），不在本文件抄第二份
        字面量——这条是 M3-5b 真正的「无声塌缩」防线：注解挂错形态时 langgraph 不报错，
        只是把所有分支悄悄收成一个终点。
        """
        source = "analyze_and_route_query"
        expected = {
            target for (src, target, _cond) in EXPECTED_GRAPH_EDGES if src == source
        }
        assert expected and source not in expected, (
            f"前置：期望分支集不自洽（{sorted(expected)}）"
        )
        proc = _run_probe(_P5_PROBE)
        assert proc.returncode == 0, f"P5 解释器崩了：{proc.stderr[-800:]}"
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("BRANCH_TARGETS:")), "")
        got = set(eval(line.split("BRANCH_TARGETS:", 1)[1].strip()))
        assert got == expected, (
            f"P5 条件边塌缩/漂移：实得 {sorted(got)}，期望 {sorted(expected)}"
        )


# ---------------------------------------------------------------------------
# 5b — import 顺序契约守卫（M3-5b 其一：取源动作只能在 import 期完成）
# ---------------------------------------------------------------------------


class TestWiringPrecedesBuilderImport:
    def test_p4_registry_ready_before_lg_builder_body(self) -> None:
        """P4：干净解释器 import chat_service 时，`route_query` 必须**先于**
        lg_builder 模块体的执行进入注册表。

        为什么需要这条守卫：lg_builder 的取源块（本批其二把它改经节点注册表现取
        真身）要在建图（模块级 `graph = build_graph(...)`）之前把真源 `route_query`
        的 `Literal` 注解挂到壳上 ⇒ **本模块的 import 期就要求装配已加载**，未装配时
        直接抛 `NodeNotRegistered`（fail-closed，不做 import 兜底）。
        chat_service 是本模块在非装配进程（`evals/`、`bench/` 直调入口）里的
        共同咽喉点，它自己的 `import agentix.application.wiring` 若仍排在
        `from agentix.orchestration.langgraph import lg_builder` 之后，
        lg_builder 就会在 import 期抛 `NodeNotRegistered`——挂线位必须提前。
        """
        proc = _run_probe(_P4_ORDER_PROBE)
        assert proc.returncode == 0, f"P4 解释器崩了：{proc.stderr[-800:]}"
        assert "ROUTE_QUERY_WIRED_BEFORE_LG_BUILDER: True" in proc.stdout, (
            "P4 顺序契约不成立：lg_builder 模块体即将执行时注册表里没有 route_query"
            f"（chat_service 的挂线位晚于它的 lg_builder import）。读数：{proc.stdout!r}"
        )


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
