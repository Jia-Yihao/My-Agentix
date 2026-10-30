"""编排层非节点能力注入位（M3-5a，本批只收 `artifact_store` 的两键；2026-10-30 现值三键，见下方「键的归属」）。

## 这个文件为什么存在

`agentix/orchestration/strategies/multi_agent/contract.py` 有三条函数体内的
`agentix.application.services.artifact_store` import（改造前实测 `:94` / `:242` / `:272`），
是 §7.5 要拆的反向边之一。它们**不是节点**——不是图上的节点、也不经 `lg_builder` 建图，
只是编排层要用的**能力**（把内容外置成句柄、取回句柄背后的正文）。

不能把它们注册成 `agentix/orchestration/node_registry.py` 的第 11 个键：三条「注册表全集
相等」守卫（`tests/test_p178_node_registry.py:527`、
`test_p180` 的 `test_p3_chat_service_wires_ten_keys`、
`test_p179` 的 `test_names_exactly_the_wired_ten`）会把多出来的键判红，而那三条是本机制
等价性证明的基准线，一律不许放宽。所以本文件另立一把**与节点键集互不相交**的键集，
形态照抄节点注册表（同一套 fail-closed / thunk / 单源口径），但语义只到「名字 → 真身」。

## 三条不可让的设计口径（与 `node_registry` 同源，不另立）

1. **fail-closed**：未注册的 name 一律抛 `CapabilityNotRegistered`，不返回 `None`、
   不做内联兜底、不 `try/except` 降级。装配缺失必须在全链路里响亮暴露，静默兜底等于
   把「装配没跑」伪装成「跑通了」（`AGENTS.md` 硬性纪律 2 的 fail-open 只适用于
   指标/记忆/指令/预算这类**辅助**子系统，注入位是主链路）。
2. **provider 是零参 thunk**，返回**当前模块属性**而非 import 期快照；`resolve`
   **每次都走 thunk**、不缓存。这条语义由
   `tests/test_p181_capability_seam.py::TestConsumerBehaviorEquivalence::test_offload_text_observes_module_attr_patch`
   钉住。`tests/test_p78_index_cascade_isolate.py:227` / `:554` 是应用侧**直接 import 路径**
   （旧句 `agentix/application/services/chat_service.py`:376（2026-10-30 实测现值 :381）/
   `agentix/retrieval/domain/services/context_engine.py:204` 的函数体内 import）的降级用例，
   既不经 `contract.py` 也不经本文件，与本注入位**无依赖关系**，至多作为「模块属性 patch
   惯用法同形」的旁证——不得写成前提。
3. **同一个判定不许两个源**（`AGENTS.md` 纪律 6 ④）：「装配缺失」的可定位短语只有
   一份，即 `node_registry.WIRING_HINT`。本文件**复用**它、不另写字面量，异常文本
   因此与节点面同说法；日志里出现这条短语时，不必先猜是哪把键集没装配。

## 形态约束（由既有守卫反推）

- 本文件 **不 import `agentix.application`**：它就是为拆掉这条反向边而存在的。
- 本文件 **不 import `langgraph`**：`tests/test_p66_arch_guards.py:122-136` 对
  「导入 langgraph 的文件」做 equality 棘轮，新增文件 import 它就得往白名单加一行，
  等于扩大未经评审的例外面。
  两条均由 `tests/test_p181_capability_seam.py` 的 AST 守卫钉住（`ast.walk` 全树，
  函数体内的延迟 import 同样计入）。
- **不经包门面导出**：`tests/test_p71_m2_orchestration_migration.py:115-125` 要求
  `orchestration/**/__init__.py` 零 import，消费者一律
  `from agentix.orchestration.capabilities import ...` 全路径取用。

## 键的归属（唯一写入点是装配根）

哪些名字进这把键集，由 `agentix/application/wiring.py` 一处说了算；编排层不得 import
装配文件（那等于把反向边换个名字绕回来，由 `tests/test_p179_supervisor_wiring.py` 的
静态守卫钉住）。本批登记的键：`offload_content`、`get_artifact_store`（2026-10-30 现值三键：再加 `get_llm_client`；`:52-54` 那句「后续批 M3-4b 会把…」已兑现）。
后续批 M3-4b 会把 `llm_factory` 的三条边也接到本注入位（指针：
`docs/design/审批与中断恢复优化规划.md` §8 台账的 M3-4b 行），届时键数变三，
`tests/test_p181_capability_seam.py` 的全集相等清单同批改。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Tuple

from agentix.orchestration.node_registry import WIRING_HINT

__all__ = [
    "CapabilityError",
    "CapabilityNotRegistered",
    "CapabilityAlreadyRegistered",
    "CapabilityProvider",
    "CapabilityRegistry",
    "DEFAULT_CAPABILITIES",
    "register",
    "provider",
    "resolve",
    "require",
    "names",
]

#: provider 的形态：零参 thunk，返回能力的当前真身（同 `node_registry.NodeProvider`）。
CapabilityProvider = Callable[[], Any]


# ---------------------------------------------------------------------------
# 异常：全部 fail-closed，不带返回值兜底语义
# ---------------------------------------------------------------------------


class CapabilityError(RuntimeError):
    """能力注入位错误基类（用 `RuntimeError` 而非 `KeyError`：`KeyError` 会把消息再套
    一层 `repr` 引号，让「异常文本包含 name」这类断言在两种引号形态间摇摆）。"""


class CapabilityNotRegistered(CapabilityError):
    """请求的能力名不在注入位里 ⇒ 装配未加载或名字写错。

    同时携带缺失名与当前已注册集合（同 `node_registry.NodeNotRegistered` 的理由：
    只给名字看不出打错了什么，只给集合看不出哪个键没命中）。提示串复用 `WIRING_HINT`，
    本类刻意不重复那份字面量——措辞的唯一源在 `node_registry`。
    """

    def __init__(self, missing: Iterable[str], *, available: Iterable[str] = ()) -> None:
        self.missing: Tuple[str, ...] = tuple(missing)
        self.available: Tuple[str, ...] = tuple(available)
        # 单数便捷位：绝大多数抛出点只缺一个名字，消费者写 exc.name 即可。
        self.name: str = self.missing[0] if self.missing else ""
        super().__init__(
            f"capability(ies) not registered: {', '.join(repr(n) for n in self.missing)}"
            f" — {WIRING_HINT}（registered: {', '.join(repr(n) for n in self.available) or 'none'}）"
        )


class CapabilityAlreadyRegistered(CapabilityError):
    """同名重复注册且未显式 `override=True`（防装配被 import 两次时静默换实现）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"capability {name!r} is already registered — overwrite requires override=True"
        )


# ---------------------------------------------------------------------------
# 注入位本体
# ---------------------------------------------------------------------------


class CapabilityRegistry:
    """name → provider 的登记容器（写入只发生在装配期，读取发生在调用期）。

    与 `NodeRegistry` 的差别只有一处：这里不登记 `awaits` / `takes_config` 形态。
    节点面需要形态是因为 langgraph 建图要按形态包裹节点；能力面的消费者是编排层里
    的普通调用点，形态由被调真身自己决定——在此再存一份就是第二个判据源。
    """

    def __init__(self) -> None:
        self._providers: Dict[str, CapabilityProvider] = {}

    # -- 写入（装配期） ------------------------------------------------------

    def register(
        self,
        name: str,
        provider: CapabilityProvider,
        *,
        override: bool = False,
    ) -> None:
        """登记一个能力。

        `provider` 必须是**零参 thunk**且返回当前模块属性：写成
        `register("k", mod.func)` 就退化成 import 期快照，后续测试对应用侧模块属性的
        patch 会打不中，而打不中的表现是「测试通过但没测到东西」。

        `override` 默认关：同名重复注册直接抛 `CapabilityAlreadyRegistered`。装配代码
        可能被执行两次（测试收集、reload、双装配根），静默覆盖会让「哪个实现生效」取决于
        import 顺序——那是最难定位的一类漂移。
        """
        if not isinstance(name, str) or not name:
            raise ValueError(f"capability name must be a non-empty str, got {name!r}")
        if not callable(provider):
            raise ValueError(
                f"provider for capability {name!r} must be a zero-arg thunk, got {provider!r}"
            )
        if name in self._providers and not override:
            raise CapabilityAlreadyRegistered(name)
        self._providers[name] = provider

    # -- 读取（调用期） ------------------------------------------------------

    def provider(self, name: str) -> CapabilityProvider:
        """取 thunk 本身（需要保存间接引用的消费方用；一般消费方用 `resolve`）。"""
        found = self._providers.get(name)
        if found is None:
            raise CapabilityNotRegistered([name], available=self._sorted_names())
        return found

    def resolve(self, name: str) -> Any:
        """取真身：**每次都走 thunk**，不缓存。

        缓存会让「测试 patch application 模块属性」失效——本机制的等价性取证依赖
        patch 能被调用点读到（见 `tests/test_p181_capability_seam.py` 的 thunk 语义用例）。
        """
        return self.provider(name)()

    def require(self, names: Iterable[str]) -> None:
        """批量存在性校验：缺任意一个即抛，异常一次列全缺失名（逐个抛会把排查变成往返）。"""
        missing = [n for n in names if n not in self._providers]
        if missing:
            raise CapabilityNotRegistered(missing, available=self._sorted_names())

    def registered(self, name: str) -> bool:
        """存在性查询（不抛）。**仅供诊断/日志与守卫断言使用**；禁止用它做「未注册即跳过」的
        分支条件：那等于在本文件里给 fail-closed 开了一条合法绕行口，装配缺失会被伪装成
        「这条支路不该走」。现况留痕（`docs/design/审批与中断恢复优化规划.md` M3-6 行⑭，裁「留并
        加留痕」）：HEAD 实测零生产消费者，仅 `tests/test_p178_node_registry.py` 两条用例测局部实例。
        """
        return name in self._providers

    def names(self) -> Tuple[str, ...]:
        """当前已登记的能力名（有序，便于断言与日志）。"""
        return self._sorted_names()

    def _sorted_names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._providers))


#: 进程级默认注入位。`application` 装配期往里写，编排层从模块级函数读——之所以给
#: 「一个默认实例 + 模块级函数」而不是只给类：调用点在函数体内取能力，那里没有可注入
#: 的容器参数，全路径模块函数是唯一不需要改函数签名的接法。
DEFAULT_CAPABILITIES = CapabilityRegistry()


def register(
    name: str,
    provider: CapabilityProvider,
    *,
    override: bool = False,
) -> None:
    """写入 `DEFAULT_CAPABILITIES`（判据见 `CapabilityRegistry.register`，此处只转发）。"""
    DEFAULT_CAPABILITIES.register(name, provider, override=override)


def provider(name: str) -> CapabilityProvider:
    """取 thunk（见 `CapabilityRegistry.provider`）。"""
    return DEFAULT_CAPABILITIES.provider(name)


def resolve(name: str) -> Any:
    """取真身（见 `CapabilityRegistry.resolve`）。"""
    return DEFAULT_CAPABILITIES.resolve(name)


def require(names: Iterable[str]) -> None:
    """批量存在性校验（见 `CapabilityRegistry.require`）。"""
    DEFAULT_CAPABILITIES.require(names)


def names() -> Tuple[str, ...]:
    """已登记能力名（见 `CapabilityRegistry.names`）。"""
    return DEFAULT_CAPABILITIES.names()
