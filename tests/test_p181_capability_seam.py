"""M3-5a「非节点能力注入位」的用例（`agentix/orchestration/capabilities.py` + `contract.py` 三处改点）。

## 覆盖面

1. **键集不相交守卫（本批存在的理由）**：能力面 `names()` 与节点面
   `DEFAULT_REGISTRY.names()` 的交集必须为空集。把能力注册成第 11 个节点键会同时破三条
   「注册表全集相等」守卫（`tests/test_p178_node_registry.py:527`、
   `tests/test_p180_builder_shells.py` 的 P3 探针 `test_p3_chat_service_wires_ten_keys`、
   `tests/test_p179_supervisor_wiring.py:121-122`），
   这三条一律不许放宽——所以注入位另立一把键集，本文件把「两把键集互不相交」钉成断言。
2. **装配面**：装配后能力键集**全集相等**于两键清单（不是子集、不是成员判断）。
3. **真身同源**：`resolve("offload_content") is artifact_store.offload_content`——注入位
   只登记间接引用，不许存抄本、不许包一层。
4. **fail-closed 面**：未注册键 ⇒ `CapabilityNotRegistered`，且异常文本含
   `node_registry.WIRING_HINT`；`require` 一次列全缺失名；重复注册默认拒绝（`override=True`
   才放行）；`provider` 返回 thunk 本身、`resolve` 每次现取不缓存。
5. **提示串单源守卫**：`capabilities.py` 源码**不得出现** `"application wiring not loaded"`
   字面量，必须 `from agentix.orchestration.node_registry import WIRING_HINT` 复用
   （`AGENTS.md` 纪律 6 ④：同一个判定不许两个源）。
6. **AST 依赖守卫**：`capabilities.py` 全树不 import `langgraph`（p66 的 equality 棘轮）
   与 `agentix.application`（本机制要拆的就是这条边）；`contract.py` 全树
   `agentix.application` import == 0；全仓 `agentix/orchestration/**` 同类语句 == 5，
   且分布恰为 `lg_builder.py` 3 + `lg_supervisor.py` 2（M3-5a 落地时的读数；
   2026-10-29 的 M3-5b 收掉 `lg_builder` 的 `router._ensure_router` 一条后是
   **== 4，分布 `lg_builder.py` 2 + `lg_supervisor.py` 2**，判据取自本文件的
   `EXPECTED_RESIDUAL_TOTAL` / `EXPECTED_RESIDUAL_EDGES` 两个常量）。
7. **消费者行为等价**：patch application 侧模块属性后 `contract._offload_text` 必须读到
   降级实现——**thunk 每次现取这条语义由本文件的
   `TestConsumerBehaviorEquivalence::test_offload_text_observes_module_attr_patch` 钉住**；
   `tests/test_p78_index_cascade_isolate.py:227` / `:554` 是应用侧**直接 import 路径**
   （`agentix/application/services/chat_service.py` 的 `_index_memory_context` 函数体内
   `from agentix.application.services.artifact_store import offload_content` /
   `agentix/retrieval/domain/services/context_engine.py:204`）的降级用例，既不经
   `contract.py` 也不经 `capabilities.py`，与本注入位**无依赖关系**，至多作为「模块属性
   patch 惯用法同形」的旁证提及，**不得**写成注入位的前提；未 patch 时经注入位走真身外置成
   `obs://`，取回路径同理命中默认 store；显式传入 `store` 时 `resolve_evidence_ref` 不得经
   注入位再取默认 store（`contract.py::resolve_evidence_ref` 的 `store is not None` 短路逐字
   保持，HEAD `c3acb8e` 实测该语句在 `:283`）；**未装配态 + 显式 `store`**
   仍必须能解析（返工项 R1 的守卫：显式传 store 本就不需要任何装配）。
8. **未装配态 fail-closed 探针**：subprocess 起干净解释器，只 import `contract` 并调
   `_offload_text` ⇒ 必须抛 `CapabilityNotRegistered`（与 M3-3 的 P1/P2 探针同形态）。

## 验收口径

- 机制口径由主会话裁决并登记在 `docs/design/审批与中断恢复优化规划.md` §8 台账的
  M3-4b / M3-5 行：**非节点能力注入位**，未注册即响亮失败，提示串复用 `WIRING_HINT`。
- 键集形态口径见 `agentix/orchestration/node_registry.py` 模块 docstring 的三条设计口径
  （fail-closed / 零参 thunk 每次现取 / 同一个判定单源）。
- 本批**只收 `artifact_store` 的两键**：后续批 M3-4b 会把 `llm_factory` 的 3 条边也接到
  同一注入位，届时第 2 组的两键清单要同批改为三键——指针同上（§8 台账 M3-4b 行）。
  现在不预登记任何键：预登记会让「装配面全集相等」这条守卫提前变成另一批的判据。

## 隔离口径

模块级函数写的是进程级单例 `DEFAULT_CAPABILITIES`，所以用到它的用例一律快照并还原
（同 p178/p179/p180 口径）：本文件不得给后续批次留脏注册表，也不得依赖执行顺序；
subprocess 探针不改动本进程任何全局状态。

## AST helper 的单源说明

取数用的 `_imported_modules` 直接复用 `tests/test_p178_node_registry.py:535` 的实现，
不在本文件复制第二份（纪律 6 ④）。它的口径比 `tests/test_p177_arch_closure.py` 窄：
只扫**绝对导入**（`level == 0`），跨包相对导入的兜底源是 p177 守卫 #5；
`agentix/orchestration/` 全目录当前零相对导入（grep `^\\s*from \\.` 零命中），
故本批两条口径等价。
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
from typing import Any, Dict, List, Tuple

import pytest

import agentix.application.services.artifact_store as artifact_store
from agentix.orchestration import capabilities, node_registry
from agentix.orchestration.capabilities import (
    DEFAULT_CAPABILITIES,
    CapabilityAlreadyRegistered,
    CapabilityError,
    CapabilityNotRegistered,
    CapabilityRegistry,
)
from agentix.orchestration.node_registry import WIRING_HINT
from agentix.orchestration.strategies.multi_agent import contract
from tests.test_p178_node_registry import _imported_modules

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPABILITIES_PATH = REPO_ROOT / "agentix" / "orchestration" / "capabilities.py"
CONTRACT_PATH = (
    REPO_ROOT / "agentix" / "orchestration" / "strategies" / "multi_agent" / "contract.py"
)
ORCHESTRATION_DIR = REPO_ROOT / "agentix" / "orchestration"

#: 装配后的能力面全集（**两键，全集相等**）。后续批 M3-4b 会把 `llm_factory` 的三条边
#: 也收到同一注入位，届时本清单显式改为三键并同批改注释——指针：
#: `docs/design/审批与中断恢复优化规划.md` §8 台账的 M3-4b 行。
EXPECTED_CAPABILITY_NAMES = ["get_artifact_store", "offload_content"]

#: 本批之后全仓 `orchestration → application` 的残留边分布。
#: 2026-10-26 时点（M3-5a 落地后）：**8 − 3 = 5**，分布 `lg_builder.py` 3 +
#: `lg_supervisor.py` 2；2026-10-29 时点（M3-5b 拆掉 `lg_builder` 的
#: `router._ensure_router` 一条后）：4 = `lg_builder.py` 2 + `lg_supervisor.py` 2。
#: 按「相对 `agentix/orchestration/` 的文件路径 → 条数」冻结，键用相对路径而不是
#: basename：同名文件散在子目录时 basename 会把两条边并成一格、把分布判错。
EXPECTED_RESIDUAL_EDGES = {
    "langgraph/lg_builder.py": 2,
    "langgraph/lg_supervisor.py": 2,
}
#: 全仓残留总数，由 §8.3 的批次划分决定（`lg_prompts` 一条与 `llm_factory` 三条
#: 分别归 M3-5b/M3-4b/M3-6，本批一律不动）。
#: 2026-10-29 时点：M3-5b 收下 `router._ensure_router` 这一条 ⇒ 总数 5 → 4
#: （`lg_prompts` 那条它字面钉在 p71，本批未动；`llm_factory` 三条仍归 M3-4b/M3-6）。
EXPECTED_RESIDUAL_TOTAL = 4


# ---------------------------------------------------------------------------
# 夹具：能力单例隔离（快照并还原，同 p178/p179/p180 口径）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_default_capabilities() -> Any:
    snapshot = dict(DEFAULT_CAPABILITIES._providers)  # noqa: SLF001 — 测试侧还原单例的唯一办法
    yield
    DEFAULT_CAPABILITIES._providers.clear()  # noqa: SLF001
    DEFAULT_CAPABILITIES._providers.update(snapshot)  # noqa: SLF001


def _unwire() -> None:
    """构造「装配缺失」态：清空默认能力单例（autouse 夹具负责还原）。"""
    DEFAULT_CAPABILITIES._providers.clear()  # noqa: SLF001 — 与 p179 `_unwire` 同法


def _application_imports(path: pathlib.Path) -> List[Tuple[int, str]]:
    """指向 `agentix.application*` 的 import 语句（全树口径，函数体内延迟 import 也计）。"""
    return [
        (line, mod)
        for line, mod in _imported_modules(path)
        if mod == "agentix.application" or mod.startswith("agentix.application.")
    ]


# ---------------------------------------------------------------------------
# 1 — 键集不相交（本批评据的核心）
# ---------------------------------------------------------------------------


class TestKeySetsAreDisjoint:
    def test_capability_names_do_not_overlap_node_names(self) -> None:
        """两把键集**互不相交**：能力面不得往节点注册表里塞键。

        判据写成交集为空集（不是「长度相加等于并集长度」那种间接式）：本批之后
        任何往 `DEFAULT_REGISTRY` 加第 11 个键的改动，都会先被 p178/p179/p180 的
        全集相等守卫判红；这条断言补的是另一半——能力面自己也不能悄悄借用节点键名，
        否则「这个名字走的是哪把键集」又变成两个源。
        """
        overlap = set(capabilities.names()) & set(node_registry.names())
        assert overlap == set(), f"能力键与节点键重叠（会把两把键集混成一个判定源）：{overlap}"

    def test_fresh_registry_starts_empty(self) -> None:
        """空实例即「没有任何隐式键」：能力面不含内置键，装配缺失态一定抛（不靠兜底实现）。
        节点面的 10 键全集仍由 p178/p179 冻结，本文件不复制第三份节点键清单。"""
        reg = CapabilityRegistry()
        assert reg.names() == ()


# ---------------------------------------------------------------------------
# 2 — 装配面（全集相等）
# ---------------------------------------------------------------------------


class TestAssemblySurface:
    def test_capability_names_are_exactly_the_registered_two(self) -> None:
        assert sorted(capabilities.names()) == EXPECTED_CAPABILITY_NAMES

    def test_resolved_providers_are_the_application_objects_themselves(self) -> None:
        """真身同源：注入位返回的就是应用侧当前模块属性，不是抄本、不是包装。"""
        assert capabilities.resolve("offload_content") is artifact_store.offload_content
        assert capabilities.resolve("get_artifact_store") is artifact_store.get_artifact_store


# ---------------------------------------------------------------------------
# 3 — fail-closed 面与 thunk 语义
# ---------------------------------------------------------------------------


class TestFailClosedAndThunkSemantics:
    def test_resolve_unregistered_raises_capability_not_registered_with_hint(self) -> None:
        _unwire()
        with pytest.raises(CapabilityNotRegistered) as excinfo:
            capabilities.resolve("offload_content")
        text = str(excinfo.value)
        assert WIRING_HINT in text, f"异常文本必须含可定位短语：{text}"
        assert "offload_content" in text

    def test_require_lists_every_missing_name_in_one_exception(self) -> None:
        """批量校验一次列全（同 node_registry 的理由：逐个抛会把排查变成多轮往返）。"""
        _unwire()
        with pytest.raises(CapabilityNotRegistered) as excinfo:
            capabilities.require(["offload_content", "get_artifact_store"])
        assert set(excinfo.value.missing) == {"offload_content", "get_artifact_store"}

    def test_duplicate_registration_is_rejected_unless_override(self) -> None:
        reg = CapabilityRegistry()
        reg.register("k", lambda: "first")
        with pytest.raises(CapabilityAlreadyRegistered):
            reg.register("k", lambda: "second")
        # 被拒的重复注册不得改动已有实现（抛在赋值之前）。
        assert reg.resolve("k") == "first"
        reg.register("k", lambda: "second", override=True)
        assert reg.resolve("k") == "second"

    def test_provider_returns_the_thunk_itself(self) -> None:
        reg = CapabilityRegistry()

        def thunk() -> str:
            return "body"

        reg.register("k", thunk)
        assert reg.provider("k") is thunk
        assert reg.resolve("k") == "body"

    def test_resolve_re_reads_the_thunk_every_time(self) -> None:
        """不缓存真身：装配后改应用侧模块属性，解析必须立刻看到新对象。"""
        sentinel = object()
        reg = CapabilityRegistry()
        holder = {"value": "original"}
        reg.register("k", lambda: holder["value"])
        assert reg.resolve("k") == "original"
        holder["value"] = sentinel
        assert reg.resolve("k") is sentinel

    @pytest.mark.parametrize(
        ("name", "provider"),
        [("", lambda: 1), (None, lambda: 1), (5, lambda: 1), ("k", "not-callable")],  # type: ignore[arg-type]
    )
    def test_register_rejects_bad_arguments(self, name: Any, provider: Any) -> None:
        reg = CapabilityRegistry()
        with pytest.raises(ValueError):
            reg.register(name, provider)

    def test_exception_hierarchy_is_observable(self) -> None:
        """`CapabilityError` 是 `RuntimeError` 子类、`CapabilityNotRegistered` 属之：
        消费者按基类兜一层就能拿到「装配缺失」，不必逐字匹配异常名。"""
        assert issubclass(CapabilityError, RuntimeError)
        assert issubclass(CapabilityNotRegistered, CapabilityError)
        assert issubclass(CapabilityAlreadyRegistered, CapabilityError)


# ---------------------------------------------------------------------------
# 4 — 提示串单源（WIRING_HINT 复用，不许另写字面量）
# ---------------------------------------------------------------------------


class TestWiringHintSingleSource:
    def test_module_does_not_restate_the_wiring_hint_literal(self) -> None:
        src = CAPABILITIES_PATH.read_text(encoding="utf-8")
        for literal in ('"application wiring not loaded"', "'application wiring not loaded'"):
            assert literal not in src, f"capabilities.py 复述了 WIRING_HINT 字面量：{literal}"

    def test_module_imports_the_hint_from_node_registry(self) -> None:
        """复用必须是 import，且**只有一处**：两处 import 同名常量说明有人在另写判据。"""
        src = CAPABILITIES_PATH.read_text(encoding="utf-8")
        assert "WIRING_HINT" in src
        tree = ast.parse(src)
        hint_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "agentix.orchestration.node_registry"
            and any(alias.name == "WIRING_HINT" for alias in node.names)
        ]
        assert len(hint_imports) == 1, f"WIRING_HINT 的 import 应恰 1 处，实得 {len(hint_imports)}"
        assert capabilities.WIRING_HINT is node_registry.WIRING_HINT

    def test_registered_name_is_not_rewritten_by_the_module(self) -> None:
        """常量值不得被 capabilities 侧改写（漂移后 p178:596 与守卫 #4 会各说一套）。"""
        assert capabilities.WIRING_HINT == "application wiring not loaded"


# ---------------------------------------------------------------------------
# 5 — AST 依赖守卫
# ---------------------------------------------------------------------------


class TestStaticDependencyGuards:
    def test_file_exists_at_the_declared_location(self) -> None:
        assert CAPABILITIES_PATH.is_file(), f"落点不符：{CAPABILITIES_PATH}"

    @pytest.mark.parametrize("prefix", ["langgraph", "agentix.application"])
    def test_capabilities_file_has_no_forbidden_import(self, prefix: str) -> None:
        """`langgraph`：`tests/test_p66_arch_guards.py:122-136` 是 equality 棘轮，
        新文件 import 它就得往白名单加一行（扩大未经评审的例外面）。
        `agentix.application`：本文件存在的理由就是拆掉这条反向边，自己绝不能先沾上。"""
        offenders = [
            (line, mod)
            for line, mod in _imported_modules(CAPABILITIES_PATH)
            if mod == prefix or mod.startswith(prefix + ".")
        ]
        assert offenders == [], f"capabilities.py 出现 {prefix} import: {offenders}"

    def test_contract_file_has_zero_application_imports(self) -> None:
        """本批的落点判据：`contract.py` 全树 0 条（改造前实测 3 条）。"""
        assert _application_imports(CONTRACT_PATH) == []

    def test_orchestration_residual_edges_are_exactly_four(self) -> None:
        """全仓 `agentix/orchestration/**` 的 application import **总数** 4 条
        （M3-5a 落地后是 5 条；2026-10-29 的 M3-5b 收掉 `lg_builder` 的
        `router._ensure_router` 一条 ⇒ 现为 4 条）。"""
        total = sum(len(_application_imports(p)) for p in sorted(ORCHESTRATION_DIR.rglob("*.py")))
        assert total == EXPECTED_RESIDUAL_TOTAL, f"残留边总数漂移：实得 {total}"

    def test_residual_edge_distribution_is_the_two_declared_files(self) -> None:
        """分布逐文件冻结：多一条、少一条、换了文件，都在这里红。

        总数相等但分布漂移（例如把 `lg_builder` 的一条挪进别的文件）是本批最典型的
        「假拆边」形态，所以除总数之外还钉这张分布表。
        """
        distribution: Dict[str, int] = {}
        for path in sorted(ORCHESTRATION_DIR.rglob("*.py")):
            count = len(_application_imports(path))
            if count:
                distribution[path.relative_to(ORCHESTRATION_DIR).as_posix()] = count
        assert distribution == EXPECTED_RESIDUAL_EDGES, f"残留边分布漂移：{distribution}"

    def test_capabilities_not_exported_through_any_package_door(self) -> None:
        """能力面不经包门面导出：`orchestration/**/__init__.py` 里不得出现 `capabilities`。

        口径来源是既有的 `tests/test_p71_m2_orchestration_migration.py:115-125`
        （`orchestration` 系门面必须零 import）——本文件不复制那份门面判据，
        只钉「新增的能力面没被塞进门面」这条新事实：消费者一律全路径取用。
        """
        for init in sorted(REPO_ROOT.glob("agentix/orchestration/**/__init__.py")):
            text = init.read_text(encoding="utf-8")
            assert "capabilities" not in text, f"{init} 出现能力面导出"


# ---------------------------------------------------------------------------
# 6 — 消费者行为等价（注入位对调用点是透明的）
# ---------------------------------------------------------------------------


class _FakeStore:
    """最小 store 替身：只实现 `get`，用于钉「显式 store 优先」。"""

    def __init__(self, items: Dict[str, str]) -> None:
        self._items = items

    def get(self, artifact_id: str) -> Any:
        return self._items.get(artifact_id)


class TestConsumerBehaviorEquivalence:
    def test_offload_text_observes_module_attr_patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """降级实现经应用侧**模块属性** patch 生效 ⇒ 证明 provider 每次现取，
        不是装配期快照。**本用例就是 thunk 语义的钉子**；
        `tests/test_p78_index_cascade_isolate.py:227` / `:554` 走的是应用侧函数体内
        **直接 import** 的路径（`chat_service.py` 的 `_index_memory_context` 函数体内
        import / `context_engine.py:204`），与本注入位
        无依赖关系，只是同一种模块属性 patch 惯用法，不是本用例的前提。"""

        def _degrade(content, **kwargs):  # noqa: ANN001, ANN202 - 与 p78 同形的降级替身
            return artifact_store.OffloadResult(is_offloaded=False, inline=content)

        monkeypatch.setattr(artifact_store, "offload_content", _degrade)
        out = contract._offload_text("证" * 3000, kind="evidence")
        assert not out.startswith("obs://"), f"降级后仍给了句柄：{out[:40]}"
        assert out == "证" * 120  # 内联短串上限（_INLINE_EVIDENCE_CHARS）

    def test_offload_text_through_seam_offloads_for_real(self) -> None:
        """未 patch 时经注入位走真身：长证据外置成 `obs://` 句柄，且句柄可取回原文。"""
        text = "Y" * 2500
        handle = contract._offload_text(text, kind="evidence")
        assert handle.startswith("obs://"), f"未外置：{handle[:40]}"
        assert contract.resolve_evidence_ref(handle) == text

    def test_short_evidence_stays_inline(self) -> None:
        """短于阈值（2000）不外置——降级分支的取值逐字保持。"""
        out = contract._offload_text("短证据" * 10, kind="evidence")
        assert out == "短证据" * 10
        assert not out.startswith("obs://")

    def test_resolve_evidence_ref_prefers_explicit_store(self) -> None:
        """显式传 store 时**不得调用**注入位取回的默认 store 工厂
        （`contract.py::resolve_evidence_ref` 的 `store is not None` 短路逐字保持，HEAD `c3acb8e`
        实测该语句在 `:283`）。

        判据不是「没解析」：`resolve` 只是取 thunk，它一定会执行。真正的短路证据是
        「解析到的工厂没有被调用」——工厂里登记一次调用即可观察（若把它写成抛异常，
        会被本函数既有的 `except Exception` 吞掉，变成假绿）。
        """
        called: List[str] = []

        def _poison_store() -> Any:
            called.append("get_artifact_store()")
            return _FakeStore({"abc123": "不该被读到"})

        capabilities.register("get_artifact_store", lambda: _poison_store, override=True)
        assert (
            contract.resolve_evidence_ref("obs://abc123", store=_FakeStore({"abc123": "正文"}))
            == "正文"
        )
        assert called == [], f"显式 store 时仍走了默认 store 工厂：{called}"

    def test_resolve_evidence_ref_explicit_store_works_unwired(self) -> None:
        """返工项 R1 的守卫：**未装配态 + 调用方显式传 store** 必须仍能解析出正文
        （与 `56857ff` 改前语义一致——改前 try 里那句 import 恒成功，这条路根本不依赖装配）。

        上一批把 `capabilities.resolve` 放在 `store` 短路**之前**无条件执行，抛出的
        `CapabilityNotRegistered` 会被本函数既有的 `except Exception` 吞成 `None`，
        于是「显式传 store」这条路在未装配进程里静默失效。修法是把 resolve 折进
        `else` 支（`contract.py::resolve_evidence_ref`，HEAD `c3acb8e` 实测在 `:283`）。

        判据写成「返回值 == 正文」而**不是**「不抛异常」：本函数的既有契约是绝不抛
        （`except Exception` 兜一切），断言不抛会绿而测不到东西（假绿）。
        """
        _unwire()
        assert capabilities.names() == (), "前置：本用例必须在未装配态下跑"
        assert (
            contract.resolve_evidence_ref("obs://abc123", store=_FakeStore({"abc123": "正文"}))
            == "正文"
        )

    def test_resolve_evidence_ref_without_store_goes_through_seam(self) -> None:
        """store 缺省时经注入位取回**工厂**并调用它（把实现换成替身即可观察解析点）。

        provider 登记的必须是「返回工厂的 thunk」——与 wiring 的
        `lambda: artifact_store.get_artifact_store` 同构；直接把工厂当 provider 会让
        `resolve` 提前把它调用掉，读到的就不是工厂而是产物本身。
        """
        seen: List[str] = []

        def _fake_store() -> Any:
            seen.append("called")
            return _FakeStore({"abc123": "经注入位取回"})

        capabilities.register("get_artifact_store", lambda: _fake_store, override=True)
        assert contract.resolve_evidence_ref("obs://abc123") == "经注入位取回"
        assert seen == ["called"]

    def test_resolve_evidence_ref_non_handle_returns_none(self) -> None:
        """非句柄 / 空 id ⇒ None（原缺省回退语义，不经过注入位）。"""
        assert contract.resolve_evidence_ref("普通文本") is None
        assert contract.resolve_evidence_ref("obs://") is None

    def test_try_offload_face_degrades_to_none_when_unwired(self) -> None:
        """契约面外置点在 `try:` 内 ⇒ 未装配态沿用既有降级（返回 None，由 manager
        回基线截断），本批不改这条降级分支。"""
        _unwire()
        assert contract.try_offload_face("契约面正文") is None


# ---------------------------------------------------------------------------
# 7 — 未装配态探针（subprocess 干净解释器）
# ---------------------------------------------------------------------------

_UNWIRED_PROBE = """
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
from agentix.orchestration.strategies.multi_agent.contract import _offload_text
_offload_text("证据" * 2000, kind="evidence")
print("OUTCOME: no-raise (BAD)")
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


class TestUnwiredProcessFailsLoud:
    def test_contract_without_assembly_raises_capability_not_registered(self) -> None:
        """干净解释器（不 import 任何装配根）只 import `contract` 并调 `_offload_text`
        ⇒ 必须抛 `CapabilityNotRegistered` 并带 `WIRING_HINT`——本批唯一的现况行为变化：
        未装配态从「隐式可用」变「响亮失败」。"""
        proc = _run_probe(_UNWIRED_PROBE)
        assert (
            proc.returncode != 0
        ), f"未装配态竟静默成功（说明还有 import 期快照兜底）：{proc.stdout}"
        assert "CapabilityNotRegistered" in proc.stderr, f"stderr 无该异常名：{proc.stderr[-800:]}"
        assert WIRING_HINT in proc.stderr, f"stderr 无装配缺失短语：{proc.stderr[-800:]}"
        assert "no-raise (BAD)" not in proc.stdout
