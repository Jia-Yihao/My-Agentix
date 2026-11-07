"""编排层节点注册表底座（M3-1，零行为变化）。

## 这个文件为什么存在

拆边修法分三步：①在 `orchestration`
侧定义节点注册协议 ②`application` 装配期注册全部 handler ③建图/分派文件只接收注册表、
不再顶层 import 具体 handler。本文件只做第 ① 步——**协议与判据源**，不拆任何一条
现存 import（拆边属后续批次 M3-2…），因此本批对生产链路零影响：没有任何调用点读它。

## 为什么是「延迟解析」而不是「装配期注入」

`agentix/orchestration/langgraph/lg_builder.py` 文件末尾的 `checkpointer = MemorySaver()`
+ `graph = build_graph(checkpointer=checkpointer)`（时点 `:480-481`）（2026-10-30 实测：这两句在同文件 `:480-481`，是**模块体尾部的实例化段**而非严格末行——其后 `:482-524` 是 Phase 19 supervisor 接线与 `safety_guardrails`，与建图时序无关，故「延迟解析」的结论不变）在**模块 import 期**就执行，不存在「先装配、再建图」的时间窗；且大量测试
直接 `import lg_builder` 而不经过 `application`。所以注册表必须支持：建图期只登记
**名字与形态**，节点**首次被调用时**才向注册表解析真身。本批提供这个底座。

## 三条不可让的设计口径

1. **fail-closed**：未注册的 name 一律抛 `NodeNotRegistered`，不返回 `None`、不做
   内联兜底实现、不 `try/except` 降级。`CONTRIBUTING.md` 的工程约定 2 的 fail-open 只适用于
   指标/记忆/指令/预算这类**辅助**子系统；注册表是主链路，静默兜底等于让「装配没跑」
   伪装成「跑通了」——而且只会在全链路回归里以更难定位的形态暴露。异常文本因此必须
   自带 `application wiring not loaded` 这个可定位短语。
2. **provider 是零参 thunk**（`lambda: router_mod.execute_task`），返回**当前模块属性**
   而非 import 期快照。这样测试对 application 模块属性的 patch 仍然生效；`resolve`
   **每次都走 thunk**、不缓存，否则一次 patch 之后注册表会把旧实现钉死。
3. **`awaits` / `takes_config` 的唯一判据源是注册表项本身**（`CONTRIBUTING.md` 的工程约定 6 ④：
   同一个判定不许两个源）。消费者只读 `entry(name)`，不得各自再算一遍
   `iscoroutinefunction(...)`。异步与否的**判据**本身取「调用后能否拿到可 await 的
   结果」（`is_awaitable_result`）——按 `iscoroutinefunction` 字面判会把类形式的
   `async __call__` 误判成同步。注意：本 helper 与 `agentix/orchestration/executor.py:68`
   的内联判据目前是**同式两源**（两份字面实现、零相互引用），不是同源；executor 侧改指
   本 helper 属后续批次，该二源债已登记入台账（§8 的
   M3-1 行待追认列与 §8.2 表 R 的 R1 行）；闭合前 `CONTRIBUTING.md` 的工程约定 6 ④ 仍未闭合。

## 命名与形态的先例

对齐仓内既有注册表写法，不另立风格：`executor.py:40-47` 的
`MappingStepExecutor.register(name, fn)`、`strategies/multi_agent/isolation.py:177-199`
的 `LiveSubagentRegistry.register/get`。

## 两条硬约束（由既有守卫反推，别在后续批次里破）

- 本文件 **不 import `langgraph`**：导入它的文件属于一份冻结的等式集合，新增文件
  导入它就得往白名单加一行，等于扩大未经评审的例外面。
- 本文件 **不 import `agentix.application`**：它就是为拆掉这条反向边而存在的。
  两条约束由 `tests/test_p178_node_registry.py` 的 AST 守卫钉住（含函数体内的延迟 import）。
- 包门面保持零 import：`tests/test_p71_m2_orchestration_migration.py:115-125` 要求
  `orchestration/**/__init__.py` 无任何 import，所以注册表**不经 `__init__.py` 导出**，
  消费者一律 `from agentix.orchestration.node_registry import ...` 全路径取用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Tuple

__all__ = [
    "WIRING_HINT",
    "NodeRegistryError",
    "NodeNotRegistered",
    "NodeAlreadyRegistered",
    "NodeEntry",
    "NodeRegistry",
    "DEFAULT_REGISTRY",
    "register",
    "provider",
    "resolve",
    "require",
    "entry",
    "names",
    "is_awaitable_result",
]

#: 未注册时必须出现在异常文本里的短语。写死成常量，是为了让「为什么失败」在日志里
#: 只有一个说法：装配缺失就是装配缺失，不许被后续批次的调用点包装成别的错误码。
WIRING_HINT = "application wiring not loaded"

#: provider 的形态：零参 thunk，返回节点的当前真身。
NodeProvider = Callable[[], Any]


# ---------------------------------------------------------------------------
# 异常：全部 fail-closed，不带返回值兜底语义
# ---------------------------------------------------------------------------


class NodeRegistryError(RuntimeError):
    """注册表错误基类（`RuntimeError` 而非 `KeyError`：`KeyError` 会把消息再套一层
    `repr` 引号，让「异常文本包含 name」这类断言在两种引号形态间摇摆）。"""


class NodeNotRegistered(NodeRegistryError):
    """请求的节点名不在注册表里 ⇒ 装配未加载或名字写错。

    同时携带缺失名与当前已注册集合：只给名字，排查时还得再跑一次才知道打错了什么；
    只给集合，又看不出究竟哪个键没命中。
    """

    def __init__(self, missing: Iterable[str], *, available: Iterable[str] = ()) -> None:
        self.missing: Tuple[str, ...] = tuple(missing)
        self.available: Tuple[str, ...] = tuple(available)
        # 单数便捷位：绝大多数抛出点只缺一个名字，消费者写 exc.name 即可。
        self.name: str = self.missing[0] if self.missing else ""
        super().__init__(
            f"node(s) not registered: {', '.join(repr(n) for n in self.missing)}"
            f" — {WIRING_HINT}（registered: {', '.join(repr(n) for n in self.available) or 'none'}）"
        )


class NodeAlreadyRegistered(NodeRegistryError):
    """同名重复注册且未显式 `override=True`。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"node {name!r} is already registered — overwrite requires override=True"
            f"（防装配被 import 两次时静默换实现）"
        )


# ---------------------------------------------------------------------------
# 注册表项：节点「叫什么、同步还是异步、接不接 config」的唯一记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeEntry:
    """一个节点的登记项。

    真身**不存这里**（存的是 thunk），否则就退化成 import 期快照，模块属性 patch 失效。
    """

    name: str
    provider: NodeProvider
    awaits: bool
    takes_config: bool


def is_awaitable_result(result: Any) -> bool:
    """异步判据的实现：调用后拿到可 await 的结果即为异步。

    刻意用 `__await__` 探测而不是 `iscoroutinefunction`：后者的字面判据对「类形式
    `async def __call__`」为 False，会把异步节点登记成同步（`CONTRIBUTING.md` 的工程约定 6 ④）。
    与 `executor.py:68` 的既有写法**同式两源**（两份 `hasattr(result, "__await__")`
    字面实现，互不引用），**不是**同源；executor 侧的内联判据须在后续批次改指本 helper，
    届时本 helper 才成为唯一实现。该二源债已登记入台账（同上指针：规划 §8 的 M3-1 行待追认列
    与 §8.2 表 R 的 R1 行）。
    """
    return hasattr(result, "__await__")


# ---------------------------------------------------------------------------
# 注册表本体
# ---------------------------------------------------------------------------


class NodeRegistry:
    """name → `NodeEntry` 的登记容器（写入只发生在装配期，读取发生在建图/执行期）。"""

    def __init__(self) -> None:
        self._entries: Dict[str, NodeEntry] = {}

    # -- 写入（装配期） ------------------------------------------------------

    def register(
        self,
        name: str,
        provider: NodeProvider,
        *,
        awaits: bool,
        takes_config: bool,
        override: bool = False,
    ) -> None:
        """登记一个节点。

        `awaits` / `takes_config` 必须由调用方**显式**给（keyword-only）：让注册表猜形态
        就是第二个判据源，且猜错时（类形式的 `async __call__`）只有运行时才暴露。

        `override` 默认关：同名重复注册直接抛 `NodeAlreadyRegistered`。理由是后续批次的
        装配代码可能被 import 两次（测试收集、脚本入口、惰性导入环），静默覆盖会让
        「哪个实现生效」取决于 import 顺序——那是最难定位的一类漂移。
        """
        if not isinstance(name, str) or not name:
            raise ValueError(f"node name must be a non-empty str, got {name!r}")
        if not callable(provider):
            raise ValueError(
                f"provider for node {name!r} must be a zero-arg thunk, got {provider!r}"
            )
        for flag, value in (("awaits", awaits), ("takes_config", takes_config)):
            if not isinstance(value, bool):
                raise ValueError(f"{flag} for node {name!r} must be a bool, got {value!r}")
        if name in self._entries and not override:
            raise NodeAlreadyRegistered(name)
        self._entries[name] = NodeEntry(
            name=name,
            provider=provider,
            awaits=awaits,
            takes_config=takes_config,
        )

    # -- 读取（建图期 / 执行期） --------------------------------------------

    def entry(self, name: str) -> NodeEntry:
        """形态记录的唯一读取入口（名字、同步/异步、接不接 config）。"""
        found = self._entries.get(name)
        if found is None:
            raise NodeNotRegistered([name], available=self._sorted_names())
        return found

    def provider(self, name: str) -> NodeProvider:
        """取 thunk 本身（建图期登记用：壳要保存间接引用，不能取真身）。"""
        return self.entry(name).provider

    def resolve(self, name: str) -> Any:
        """取真身：**每次都走 thunk**，不缓存。

        缓存会让「测试 patch application 模块属性」失效——后续批次的等价性取证依赖
        patch 能被节点侧读到（见 `tests/test_p178_node_registry.py` 的 thunk 语义用例）。
        """
        return self.entry(name).provider()

    def require(self, names: Iterable[str]) -> None:
        """批量存在性校验：缺任意一个即抛，异常一次列全缺失名。

        一次列全是必要的：建图入口有 10 个节点名，逐个抛的话改一次装配只能发现一个缺失，
        回归就变成十轮往返。
        """
        missing = [n for n in names if n not in self._entries]
        if missing:
            raise NodeNotRegistered(missing, available=self._sorted_names())

    def registered(self, name: str) -> bool:
        """存在性查询（不抛）。**仅供诊断/日志与守卫断言使用**；要拿形态仍必须走 `entry`。
        禁止用它做「未注册即跳过」的分支条件：那等于在本文件里给 fail-closed 开了一条
        合法绕行口（见模块 docstring 第 1 条），装配缺失会被伪装成「这条支路不该走」。
        需要「该不该走这条支路」的判定请由调用方的显式配置/意图决定，节点解析本身
        一律 `resolve`/`require`，缺失即抛。现况留痕（M3-6 行⑭，裁「留并加留痕」）：HEAD 实测零生产消费者，仅 `tests/test_p178_node_registry.py` 两条用例测局部实例。
        """
        return name in self._entries

    def names(self) -> Tuple[str, ...]:
        """当前已登记的节点名（有序，便于断言与日志）。"""
        return self._sorted_names()

    def _sorted_names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._entries))


#: 进程级默认注册表。`application` 装配期往里写，`lg_builder` / `lg_supervisor` /
#: `contract` 从模块级函数读——之所以给「一个默认实例 + 模块级函数」而不是只给类：
#: 后续批次要在**模块 import 期建图**的路径上取节点，那里没有可注入的容器参数，
#: 全路径模块函数是唯一不需要改函数签名的接法。
DEFAULT_REGISTRY = NodeRegistry()


def register(
    name: str,
    provider: NodeProvider,
    *,
    awaits: bool,
    takes_config: bool,
    override: bool = False,
) -> None:
    """写入 `DEFAULT_REGISTRY`（判据见 `NodeRegistry.register`，此处只是转发）。"""
    DEFAULT_REGISTRY.register(
        name,
        provider,
        awaits=awaits,
        takes_config=takes_config,
        override=override,
    )


def provider(name: str) -> NodeProvider:
    """取 thunk（见 `NodeRegistry.provider`）。"""
    return DEFAULT_REGISTRY.provider(name)


def resolve(name: str) -> Any:
    """取真身（见 `NodeRegistry.resolve`）。"""
    return DEFAULT_REGISTRY.resolve(name)


def require(names: Iterable[str]) -> None:
    """批量存在性校验（见 `NodeRegistry.require`）。"""
    DEFAULT_REGISTRY.require(names)


def entry(name: str) -> NodeEntry:
    """只读形态记录（见 `NodeRegistry.entry`）。"""
    return DEFAULT_REGISTRY.entry(name)


def names() -> Tuple[str, ...]:
    """已登记节点名（见 `NodeRegistry.names`）。"""
    return DEFAULT_REGISTRY.names()
