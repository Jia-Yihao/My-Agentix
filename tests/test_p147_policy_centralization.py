"""LG-0-3 策略集中守卫：LangGraph 的节点策略只能从 ``policies.py`` 出。

## 验收口径（指导文档 §4.1 LG-0-3「怎么算过」）

1. 全仓 ``add_node(`` 调用不出现裸 ``RetryPolicy(`` / ``TimeoutPolicy(`` 字面构造；
2. 策略形参（``retry_policy`` / ``cache_policy`` / ``error_handler`` / ``timeout``）
   的实参**只从** ``agentix/orchestration/langgraph/policies.py`` 出；
3. 既有用例不改（本文件只新增，不调整任何既有守卫的判据）。

## 两条必须说清的判据细节

**同名消歧**：仓里另有一个**自研** ``RetryPolicy``
（``agentix/retrieval/infra/tasks/reliable.py``，检索索引批次的退避配置，
消费者 ``index_worker.py`` / ``scripts/migration/backfill_vectors.py``）。它与
LangGraph 无关，本守卫**不许**把它算作违规——所以判据不是「出现了
``RetryPolicy(`` 这个字面」，而是「**这个名字是不是从** ``langgraph*`` **导入的**」。

**形参名口径**：0.2 线 ``add_node`` 的重试形参叫 ``retry=``，1.x 叫
``retry_policy=``。写成旧名不会重试，只会静默无效，因此单独有一条绊线盯着它
（指导文档 §7 风险表「``retry`` / ``retry_policy`` 写混」一行）。

扫描范围是生产代码目录（``agentix/``、``scripts/``）；``tests/`` 里为验证策略
行为而直接构造 ``RetryPolicy`` 是用例的正常形态，不在禁止之列。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Iterator

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PRODUCTION_DIRS = ("agentix", "scripts")
POLICIES_PATH = REPO_ROOT / "agentix" / "orchestration" / "langgraph" / "policies.py"

#: LangGraph 的策略类型名（用于同名消歧，见模块 docstring）。
POLICY_CLASS_NAMES = frozenset({"RetryPolicy", "TimeoutPolicy"})

#: ``add_node`` 中属于「策略」的形参名（1.x 口径）。
POLICY_KWARG_NAMES = frozenset({"retry_policy", "cache_policy", "error_handler", "timeout"})

#: 0.2 线的旧名，1.x 里写了不生效——出现即违规。
LEGACY_POLICY_KWARG_NAMES = frozenset({"retry"})


def _production_sources() -> list:
    out = []
    for sub in PRODUCTION_DIRS:
        root = REPO_ROOT / sub
        if not root.exists():
            continue
        out.extend(
            p
            for p in sorted(root.rglob("*.py"))
            if "__pycache__" not in p.parts and p.name != "__init__.py"
        )
    return out


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _langgraph_policy_bindings(tree: ast.Module) -> tuple:
    """返回 ``(绑定到 langgraph 策略类的局部名, 绑定到 langgraph 的模块别名)``。

    覆盖三种写法：

    - ``from langgraph.types import RetryPolicy``（含 ``as`` 改名）
    - ``import langgraph.types`` / ``as lgt`` 之后写 ``lgt.RetryPolicy(...)``
    - 直接写全路径 ``langgraph.types.RetryPolicy(...)``（由调用点文本判，见下）
    """
    names: set = set()
    module_aliases: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("langgraph"):
            for alias in node.names:
                if alias.name in POLICY_CLASS_NAMES:
                    names.add(alias.asname or alias.name)
                elif alias.name == "types":
                    module_aliases.add(alias.asname or "types")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("langgraph"):
                    module_aliases.add(alias.asname or alias.name.split(".")[0])
    return names, module_aliases


def _policy_constructions(tree: ast.Module) -> list:
    """树内**由 langgraph 导入而来**的策略类构造调用：``[(行号, 调用文本)]``。"""
    names, module_aliases = _langgraph_policy_bindings(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        try:
            callee = ast.unparse(node.func)
        except Exception:  # noqa: BLE001 - 解析不出调用文本就判「不是策略构造」，宁漏不误伤
            continue
        root, _, last = callee.rpartition(".")
        if last not in POLICY_CLASS_NAMES:
            continue
        sourced = (
            callee in names
            or (not root and last in names)
            or callee.startswith("langgraph")
            or (bool(root) and root.split(".")[0] in module_aliases)
        )
        if sourced:
            out.append((node.lineno, callee))
    return out


def _policies_imported_names(tree: ast.Module) -> set:
    """本文件从 ``...orchestration.langgraph.policies`` 导入的名字（含 ``as`` 后的局部名）。"""
    out: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        absolute = module.endswith("orchestration.langgraph.policies")
        relative = bool(node.level) and module in ("policies", "langgraph.policies")
        if absolute or relative:
            for alias in node.names:
                out.add(alias.asname or alias.name)
    return out


def _add_node_calls(tree: ast.Module) -> Iterator:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_node":
            yield node


def _value_references(value: ast.expr, sanctioned: set) -> bool:
    """实参表达式是否引用了来自 ``policies`` 的名字（判据 2 的可溯源要求）。"""
    return any(isinstance(sub, ast.Name) and sub.id in sanctioned for sub in ast.walk(value))


class TestProductionCodeHasNoInlineLanggraphPolicy:
    """判据 1：生产代码里不出现裸 ``RetryPolicy(`` / ``TimeoutPolicy(`` 字面构造。"""

    def test_only_policies_module_constructs_langgraph_policies(self):
        violations = []
        for path in _production_sources():
            if path == POLICIES_PATH:
                continue
            violations.extend(
                (path.relative_to(REPO_ROOT).as_posix(), line, text)
                for line, text in _policy_constructions(_parse(path))
            )
        assert not violations, (
            "LangGraph 策略只能在 agentix/orchestration/langgraph/policies.py 构造"
            "（LG-0-3 策略集中）。注册处内联构造会把退避参数散成多份字面量，"
            "改一处漏一处且不会报错：\n"
            + "\n".join(f"  {f}:{ln}  {text}(...)" for f, ln, text in violations)
        )

    def test_policies_module_itself_constructs_them(self):
        """防空真：上面的豁免必须落在实处——policies.py 里确实有策略构造。

        没有这条，把 policies.py 改成「什么都不构造」也能让上一条通过。
        """
        classes = {callee.rpartition(".")[2] for _, callee in _policy_constructions(_parse(POLICIES_PATH))}
        assert POLICY_CLASS_NAMES <= classes, (
            f"policies.py 应构造 {sorted(POLICY_CLASS_NAMES)}，实得 {sorted(classes)}"
        )

    def test_scanned_surface_is_not_vacuous(self):
        files = _production_sources()
        add_node_sites = sum(len(list(_add_node_calls(_parse(path)))) for path in files)
        assert len(files) > 200, f"只扫到 {len(files)} 个生产文件，扫描面可能失效"
        assert add_node_sites >= 30, (
            f"只扫到 {add_node_sites} 处 add_node 调用（期望 ≥30：主图 10 + KG/text2sql 子图 27）"
            "——判据会空跑"
        )


class TestAddNodePolicyArgsComeFromPolicies:
    """判据 2：``add_node`` 的策略实参只能来自 ``policies.py``。"""

    def test_policy_kwargs_reference_policies(self):
        offenders = []
        for path in _production_sources():
            tree = _parse(path)
            sanctioned = _policies_imported_names(tree)
            for call in _add_node_calls(tree):
                for kw in call.keywords:
                    if kw.arg is None:  # ** 展开：LG-3-2 用它实现「关时完全不传」
                        continue
                    if kw.arg not in POLICY_KWARG_NAMES:
                        continue
                    if not _value_references(kw.value, sanctioned):
                        offenders.append(
                            f"  {path.relative_to(REPO_ROOT).as_posix()}:{call.lineno} "
                            f"{kw.arg}=...（实参未引用 policies.py 导出的名字）"
                        )
        assert not offenders, (
            "add_node 的策略形参必须由 agentix/orchestration/langgraph/policies.py "
            "导出的工厂产出（LG-0-3）：" + "".join(offenders)
        )

    def test_legacy_retry_kwarg_is_not_used(self):
        offenders = []
        for path in _production_sources():
            for call in _add_node_calls(_parse(path)):
                for kw in call.keywords:
                    if kw.arg in LEGACY_POLICY_KWARG_NAMES:
                        offenders.append(
                            f"  {path.relative_to(REPO_ROOT).as_posix()}:{call.lineno} retry=..."
                        )
        assert not offenders, (
            "0.2 线的 add_node(retry=...) 在 1.x 不生效（形参名是 retry_policy），"
            "写了只会静默不重试：" + "".join(offenders)
        )


class TestPoliciesModuleContract:
    """集中点自身的契约：工厂存在、产出类型正确、默认值与原生逐项相同。"""

    def test_factories_exported(self):
        from agentix.orchestration.langgraph import policies

        for name in ("readonly_retry_policy", "node_timeout"):
            assert callable(getattr(policies, name, None)), f"policies.{name} 必须是可调用工厂"
        assert {"readonly_retry_policy", "node_timeout"} <= set(policies.__all__), (
            "工厂必须进 __all__，否则导入方与守卫的白名单口径不一致"
        )

    def test_readonly_retry_policy_matches_native_defaults_item_by_item(self):
        """LG-1-3「与原生默认同源」的锚点：默认值必须由 ``RetryPolicy`` 自己给出。

        本断言同时是反抄本守卫：一旦有人把数字抄进 ``readonly_retry_policy``
        并抄错，这里就红。
        """
        from langgraph.types import RetryPolicy

        from agentix.orchestration.langgraph.policies import NATIVE_RETRY_DEFAULTS, readonly_retry_policy

        produced = readonly_retry_policy()
        baseline = RetryPolicy()
        assert produced == baseline, (
            f"readonly_retry_policy() 与原生 RetryPolicy() 默认值不一致："
            f"产出={produced._asdict()} 原生={baseline._asdict()}"
        )
        assert NATIVE_RETRY_DEFAULTS == baseline, (
            "NATIVE_RETRY_DEFAULTS 快照失真（框架默认值变了，LG-1-3 的基准需同步）"
        )

    def test_readonly_retry_policy_accepts_overrides(self):
        from langgraph.types import RetryPolicy

        from agentix.orchestration.langgraph.policies import readonly_retry_policy

        assert readonly_retry_policy(max_attempts=5) == RetryPolicy(max_attempts=5)

    def test_node_timeout_is_none_when_disabled(self):
        """LG-3-2 口径①：关的时候返回 None，调用方据此**完全不传** timeout。"""
        from agentix.orchestration.langgraph.policies import node_timeout

        assert node_timeout() is None
        assert node_timeout(0) is None
        assert node_timeout(0, 0) is None
        assert node_timeout(-1) is None, "非正值按「关」处理：配置里的 0 不许变成负秒数"

    def test_node_timeout_builds_policy_when_enabled(self):
        from langgraph.types import TimeoutPolicy

        from agentix.orchestration.langgraph.policies import node_timeout

        assert node_timeout(12.5) == TimeoutPolicy(run_timeout=12.5, idle_timeout=None, refresh_on="auto")
        assert node_timeout(12.5, 3.0) == TimeoutPolicy(
            run_timeout=12.5, idle_timeout=3.0, refresh_on="auto"
        )
        assert node_timeout(idle_timeout=3.0) == TimeoutPolicy(
            run_timeout=None, idle_timeout=3.0, refresh_on="auto"
        )
        assert node_timeout(12.5, refresh_on="heartbeat") == TimeoutPolicy(
            run_timeout=12.5, idle_timeout=None, refresh_on="heartbeat"
        )

    def test_kwarg_names_match_installed_langgraph_signature(self):
        """形参名口径必须与**已安装**框架一致——框架改名时这里先红，而不是静默失效。"""
        from langgraph.graph.state import StateGraph

        params = set(inspect.signature(StateGraph.add_node).parameters)
        missing = POLICY_KWARG_NAMES - params
        assert not missing, f"已安装 langgraph 的 add_node 没有这些形参：{sorted(missing)}"
        assert not (LEGACY_POLICY_KWARG_NAMES & params), (
            "add_node 又出现 retry 形参（0.2 线旧名）——升级方向或本守卫口径需要复核"
        )


class TestGuardActuallyBites:
    """反空转：把判据跑在**刻意违规 / 刻意合规**的样本源码上，必须一红一绿。

    只断言「当前代码里没有违规」是不够的：扫描器坏掉时同样成立
    （防空真口径同 ``tests/test_dependency_consistency.py``）。
    """

    VIOLATING_SOURCE = """
from langgraph.graph import StateGraph
from langgraph.types import RetryPolicy

builder = StateGraph(dict)
builder.add_node("general", handler, retry_policy=RetryPolicy(max_attempts=5))
"""

    COMPLIANT_SOURCE = """
from langgraph.graph import StateGraph
from agentix.orchestration.langgraph.policies import readonly_retry_policy

builder = StateGraph(dict)
builder.add_node("general", handler, retry_policy=readonly_retry_policy())
"""

    FULL_PATH_SOURCE = """
import langgraph.types as lgt
from langgraph.graph import StateGraph

builder = StateGraph(dict)
builder.add_node("general", handler, timeout=lgt.TimeoutPolicy(run_timeout=3))
"""

    LEGACY_SOURCE = """
from langgraph.graph import StateGraph
from langgraph.types import RetryPolicy

builder = StateGraph(dict)
builder.add_node("general", handler, retry=RetryPolicy(max_attempts=5))
"""

    SELF_DEVELOPED_SOURCE = """
from agentix.retrieval.infra.tasks.reliable import RetryPolicy

policy = RetryPolicy(max_attempts=3)
"""

    def test_inline_construction_is_detected_as_violation(self):
        tree = ast.parse(self.VIOLATING_SOURCE)
        assert _policy_constructions(tree), (
            "抓不到内联 RetryPolicy(...) 构造 ⇒ 判据 1 形同虚设"
        )
        call = next(_add_node_calls(tree))
        kw = next(k for k in call.keywords if k.arg == "retry_policy")
        assert not _value_references(kw.value, _policies_imported_names(tree)), (
            "内联构造被判定为来自 policies ⇒ 判据 2 形同虚设"
        )

    def test_aliased_module_construction_is_detected(self):
        tree = ast.parse(self.FULL_PATH_SOURCE)
        assert _policy_constructions(tree), "抓不到 lgt.TimeoutPolicy(...) 这种别名构造"

    def test_policies_sourced_arg_is_clean(self):
        tree = ast.parse(self.COMPLIANT_SOURCE)
        assert _policy_constructions(tree) == [], (
            "从 policies.py 导入的工厂调用被误判为内联构造 ⇒ 会对 LG-1-1 误伤"
        )
        call = next(_add_node_calls(tree))
        kw = next(k for k in call.keywords if k.arg == "retry_policy")
        assert _value_references(kw.value, _policies_imported_names(tree)), (
            "合规写法被判违规 ⇒ 判据 2 会把 LG-1-1 的接线全卡住"
        )

    def test_self_developed_retry_policy_is_not_a_violation(self):
        """同名消歧：自研 ``reliable.RetryPolicy``（检索索引退避）不算违规。"""
        assert _policy_constructions(ast.parse(self.SELF_DEVELOPED_SOURCE)) == [], (
            "把自研 RetryPolicy 当成 langgraph 策略 ⇒ 会误伤既有检索代码"
        )

    def test_legacy_kwarg_is_detected(self):
        tree = ast.parse(self.LEGACY_SOURCE)
        call = next(_add_node_calls(tree))
        assert any(kw.arg in LEGACY_POLICY_KWARG_NAMES for kw in call.keywords), (
            "抓不到 0.2 线旧形参名 retry= ⇒ 风险表里的「写混」一行无人守"
        )


@pytest.mark.parametrize("factory", ["readonly_retry_policy", "node_timeout"])
def test_factories_are_deterministic(factory):
    """工厂必须可重复调用、结果一致（注册期求值，不能带状态）。"""
    from agentix.orchestration.langgraph import policies

    fn = getattr(policies, factory)
    assert fn() == fn(), f"{factory}() 两次调用结果不同——注册期求值会随调用顺序漂移"
