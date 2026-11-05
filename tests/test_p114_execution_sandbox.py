"""P114：执行位置端口 `ExecutionSandbox`（§24.3 T2 ⑭ 落地回归，ADR-0007）。

这个端口要回答的是「一次工具调用**在哪儿**跑」，而它刻意**不**回答「跑得了多远」：
能跑的两档（`in_process` / `thread`）都不隔离。因此本文件最重要的一批断言不是
"沙箱很硬"，而是**三件容易互相混淆的事各就各位**：

1. flag 关 ⇒ 声明完全惰性（`spec.sandbox` 一个字都不查，与加固前逐位等价）；
2. flag 开 ⇒ 默认档与 flag 关**逐位等价**（缝不改变结果，只改变代码路径）；
3. flag 开 + 声明了未实现的档位 ⇒ **拒绝执行**，handler 一次都不被调用，
   且**不静默退回进程内**（这是 fail-closed 的全部意义；退回就是"把安全声明当装饰"）。

分七层，缺任一层都算没验完：

1. **默认姿态**：flag 默认关、`ToolSpec.sandbox` 默认 `in_process`、阶梯顺序、
   「词汇表 − 实现集」的差集恰好是 `{subprocess, container}`（差集必须可见，
   否则 ADR-0007 里"T2 还差什么"又变成文档里的一句话）。⑮ 落地后这一条**仍以
   flag-off 为准**，并成对钉一条"开了 `TOOL_SANDBOX_SUBPROCESS_ENABLED` 就差集
   收窄到 `{container}`"——两侧都可查，"新开关默认关"才不是口头承诺。
2. **声明期校验**：四档都**可以**声明（阶梯是词汇表），阶梯外的词当场 `ValueError`；
   拼写错与"还没实现"是两种错，不能混成一类。
3. **端口形状与分层**：两个实现都真的满足 `ExecutionSandbox` 协议；
   域层端口 AST 里不得出现任何 `agentix.*` 或第三方 import（ADR-0001 依赖方向）；
   infrastructure 实现不得反向 import executor。
4. **口径同源**：G1 的下放分支全项目只有一份——executor 源码里不得再出现
   `asyncio.to_thread`，且 `_invoke` 与 `InProcessSandbox.run` 对同一批 handler
   给出逐位相同的结果。
5. **执行路径（真跑 `ToolExecutor.execute`）**：上面三条姿态各自的行为都用真 handler、
   真计数验证，包括观测钩子的先后（被闸拒不触发 before）。
6. **`thread` 档真的让循环空得出来**：量「同一条 0.2s 同步 handler 在三种
   (档位 × 全局下放开关) 组合下，事件循环最大停摆时长」——这条同时是 §24.3 T2
   验收列里「同步 handler 不阻塞并发协程」压测的最小版。
7. **配置面 + 部署面**：新键在 settings / `.env.example` / selfcheck /
   两份架构文档 / p41 手清单里都在；基线 compose 转发默认 False；
   staging override 钉 **true** 且与 runbook 配置矩阵的加粗声明逐字对齐（反向钉子）。
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import logging
import re
import time
from pathlib import Path

import pytest
import yaml

from agentix.config import settings
from agentix.domain.tool.sandbox import (
    SANDBOX_DEFAULT,
    SANDBOX_LADDER,
    ExecutionSandbox,
    SandboxUnavailable,
)
from agentix.tools import sandbox as sandbox_module
from agentix.tools.gateway.executor import ToolExecutor
from agentix.tools.definitions import ToolSpec
from agentix.tools.sandbox import (
    InProcessSandbox,
    SubprocessSandbox,
    ThreadSandbox,
    implemented_kinds,
    invoke_in_process,
    resolve_sandbox,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEY = "EXECUTION_SANDBOX_ENABLED"


def _spec(name: str, *, sandbox: str = SANDBOX_DEFAULT, **kw: object) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"p114 probe 何时用：测试；何时不能用：生产（{name}）",
        risk_level="L0",
        sandbox=sandbox,  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def _sync_handler(args: dict) -> dict:
    return {"echo": args}


async def _async_handler(args: dict) -> dict:
    return {"echo": args}


async def _async_gen_handler(args: dict):
    yield args


class _AsyncCallable:
    async def __call__(self, args: dict) -> dict:
        return {"echo": args}


class _SyncCallable:
    def __call__(self, args: dict) -> dict:
        return {"echo": args}


@pytest.fixture
def sandbox_on(monkeypatch):
    """打开端口（默认关的那些用例走另一条路径，不共用本 fixture）。"""
    monkeypatch.setattr(settings, KEY, True)


@pytest.fixture
def sandbox_off(monkeypatch):
    monkeypatch.setattr(settings, KEY, False)


# ---------------------------------------------------------------------------
# 1. 默认姿态
# ---------------------------------------------------------------------------


def test_port_flag_defaults_to_off() -> None:
    field = type(settings).model_fields[KEY]
    assert field.default is False, "端口默认必须是关：它关着时才与加固前逐位等价"


def test_spec_default_kind_is_in_process() -> None:
    assert ToolSpec(name="p114_default", description="d").sandbox == "in_process"
    assert SANDBOX_DEFAULT == "in_process"


def test_ladder_is_ordered_from_least_to_most_isolated() -> None:
    """顺序是语义的一部分：从左到右「坏了波及多远」递减，文档与 ADR 都按它写。"""
    assert SANDBOX_LADDER == ("in_process", "thread", "subprocess", "container")


def test_unimplemented_rungs_are_exactly_the_future_items(monkeypatch) -> None:
    """差集可见：⑮/⑰ 没落地这件事在代码里就能被查出来，不靠文档一句话。

    ⚠️ ⑮ 落地后这条**仍以 flag-off 为准**（约定 1：关着时与 ⑭ 逐位等价），所以这里
    显式把 `TOOL_SANDBOX_SUBPROCESS_ENABLED` 钉成 False 再断言——不钉的话，本机 .env
    或 CI 变量一改，这条守卫就从"差集可见"退化成"看运气"。开着的差集另测一条。
    """
    monkeypatch.setattr(settings, "TOOL_SANDBOX_SUBPROCESS_ENABLED", False)
    gap = tuple(k for k in SANDBOX_LADDER if k not in implemented_kinds())
    assert gap == ("subprocess", "container")
    assert implemented_kinds() == ("in_process", "thread")


def test_enabling_the_subprocess_tier_narrows_the_gap_to_container_only(
    monkeypatch,
) -> None:
    """⑮ 的开着的形态：差集只剩 `container`，且 `subprocess` 可被解析到实现。

    这条与上一条**成对**存在：只有"关着为空、开着收窄"两侧都可查，约定 1 的
    「新开关默认关」才不是一句口头承诺。
    """
    monkeypatch.setattr(settings, "TOOL_SANDBOX_SUBPROCESS_ENABLED", True)
    gap = tuple(k for k in SANDBOX_LADDER if k not in implemented_kinds())
    assert gap == ("container",)
    assert implemented_kinds() == ("in_process", "subprocess", "thread")
    assert isinstance(resolve_sandbox("subprocess"), SubprocessSandbox)


# ---------------------------------------------------------------------------
# 2. 声明期校验：拼写错 vs 还没实现，是两类错
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", SANDBOX_LADDER)
def test_every_ladder_rung_is_declareable(kind: str) -> None:
    """`subprocess` 现在跑不了，但**声明它是合法的**：合法性由阶梯管，
    可用性由注册表管。混在一起的后果是 ⑮ 落地当天要改一批声明。"""
    assert _spec(f"p114_declare_{kind}", sandbox=kind).sandbox == kind


@pytest.mark.parametrize("junk", ["Subprocess", "inprocess", "", "docker", "thread "])
def test_non_ladder_words_are_rejected_at_declaration(junk: str) -> None:
    with pytest.raises(ValueError, match="sandbox"):
        _spec("p114_junk", sandbox=junk)


def test_declaration_rejection_names_the_ladder_not_a_secret() -> None:
    with pytest.raises(ValueError) as caught:
        _spec("p114_junk2", sandbox="microvm")
    message = str(caught.value)
    assert "microvm" in message and "in_process" in message
    assert "container" in message, "报错要给出可用词汇表，否则改不动"


# ---------------------------------------------------------------------------
# 3. 端口形状与分层（ADR-0001 的依赖方向）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("impl", [InProcessSandbox(), ThreadSandbox()])
def test_implementations_satisfy_the_port(impl: ExecutionSandbox) -> None:
    assert isinstance(impl, ExecutionSandbox), "端口不是文档，协议必须真的对得上"
    assert impl.kind in SANDBOX_LADDER


def _module_tree(rel: str) -> ast.Module:
    return ast.parse((PROJECT_ROOT / rel).read_text(encoding="utf-8"))


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_domain_port_imports_nothing_from_the_project_or_third_party() -> None:
    """端口在域层 ⇒ 零第三方、零 agentix 内部依赖（ADR-0001）。"""
    tree = _module_tree("agentix/domain/tool/sandbox.py")
    roots = _imported_roots(tree)
    assert "agentix" not in roots, "域层端口反向依赖了实现层"
    assert roots <= {"__future__", "typing", "abc", "dataclasses", "enum"}, roots


def test_infrastructure_implementation_does_not_import_the_executor() -> None:
    """依赖方向只能是 executor → sandbox；反过来会把端口定义拽进执行链。"""
    roots = {
        node.module
        for node in ast.walk(_module_tree("agentix/tools/sandbox.py"))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("gateway" in m for m in roots), roots


# ---------------------------------------------------------------------------
# 4. 口径同源：G1 的下放分支只许有一份
# ---------------------------------------------------------------------------


def test_executor_keeps_no_second_copy_of_the_offload_branch() -> None:
    src = (PROJECT_ROOT / "agentix/tools/gateway/executor.py").read_text(encoding="utf-8")
    assert "to_thread" not in src, "executor 里又出现一份下放实现 ⇒ 两条语义会各自漂移"
    assert "invoke_in_process" in src, "executor 必须复用沙箱里那份口径"


HANDLER_MATRIX = [
    ("sync_fn", _sync_handler),
    ("async_fn", _async_handler),
    ("async_gen_fn", _async_gen_handler),
    ("async_callable", _AsyncCallable()),
    ("sync_callable", _SyncCallable()),
]


@pytest.mark.parametrize("offload", [False, True])
@pytest.mark.parametrize("name,handler", HANDLER_MATRIX)
def test_in_process_sandbox_is_bitwise_the_old_invoke(name: str, handler, offload, monkeypatch) -> None:
    """同一条 handler，三份入口（`_invoke` / `InProcessSandbox.run` / `invoke_in_process`）
    结果逐位一致——「默认档 = 现状」这句话必须是跑出来的，不是注释里写的。"""
    monkeypatch.setattr(settings, "TOOL_SYNC_OFFLOAD_ENABLED", offload)

    async def _run() -> tuple[object, object, object]:
        return (
            await ToolExecutor._invoke(handler, {"a": 1}),
            await InProcessSandbox().run(handler, {"a": 1}),
            await invoke_in_process(handler, {"a": 1}),
        )

    via_invoke, via_sandbox, via_helper = asyncio.run(_run())
    if name == "async_gen_fn":
        # 每次调用都造一个新对象，身份不可能相等——比的是「三条入口都拿到同一种东西」
        assert all(inspect.isasyncgen(x) for x in (via_invoke, via_sandbox, via_helper))
    else:
        assert repr(via_invoke) == repr(via_sandbox) == repr(via_helper), name
        assert via_invoke == {"echo": {"a": 1}}


def test_resolve_sandbox_maps_each_kind_to_its_own_implementation() -> None:
    assert isinstance(resolve_sandbox("in_process"), InProcessSandbox)
    assert isinstance(resolve_sandbox("thread"), ThreadSandbox)


@pytest.mark.parametrize(
    ("kind", "reason_phrase"),
    # ⑮ 落地后两类不可用的**措辞必须继续分开**，因为修法不同：一个是"开开关"，
    # 一个是"等 ⑰ / 改声明"。合成一句 = 运维照着报错改不动。
    [("subprocess", "switched off"), ("container", "not implemented")],
)
def test_future_rungs_refuse_with_their_own_reason(
    kind: str, reason_phrase: str, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "TOOL_SANDBOX_SUBPROCESS_ENABLED", False)
    with pytest.raises(SandboxUnavailable) as caught:
        resolve_sandbox(kind)
    message = str(caught.value)
    assert reason_phrase in message
    assert kind in message
    assert "in_process" in message, "报错要给出真能跑的档位，否则改不动"
    if kind == "subprocess":
        assert (
            "TOOL_SANDBOX_SUBPROCESS_ENABLED" in message
        ), "这类拒绝要把开关名写进报错，否则只能靠猜"


def test_a_word_outside_the_ladder_is_a_different_error() -> None:
    with pytest.raises(SandboxUnavailable, match="not in the ladder"):
        resolve_sandbox("microvm")


# ---------------------------------------------------------------------------
# 5. 执行路径（真跑 execute）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declaration_is_inert_when_the_flag_is_off(sandbox_off) -> None:
    """flag 关 = 声明什么都不改：这里**故意**声明一个跑不了的 `subprocess`，
    它必须照常在进程内执行成功。这条是「逐位等价」最强的一次反证。"""
    calls: list[dict] = []

    def handler(args: dict) -> dict:
        calls.append(args)
        return {"did": "work"}

    ex = ToolExecutor()
    ex.register_handler("p114_inert", handler, spec=_spec("p114_inert", sandbox="subprocess"))
    result = await ex.execute("p114_inert", {"x": 1})
    assert result.success is True
    assert result.data == {"did": "work"}
    assert calls == [{"x": 1}], "flag 关时 handler 照旧被调用"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "reason_phrase"),
    [("subprocess", "switched off"), ("container", "not implemented")],
)
async def test_unavailable_rung_refuses_without_calling_handler(
    sandbox_on, caplog, kind: str, reason_phrase: str, monkeypatch
) -> None:
    """fail-closed 的正身：**一次都没调用**，且不是退回进程内。

    ⚠️ ⑮ 落地后这条**仍要跑**：它现在钉的是"档位声明了但当前拿不到"这一类，
    而拿不到的原因有两种（开关关着 / 还没实现），两种都必须拒在同一处、
    同一形态。原先只钉 `subprocess` 一种，落地 ⑮ 后若不改就等于把 fail-closed
    的证据换成了"开开关"的证据——⑰ 那条腿没人钉了。
    """
    monkeypatch.setattr(settings, "TOOL_SANDBOX_SUBPROCESS_ENABLED", False)
    calls: list[dict] = []
    # 工具名必须**按档位区分**：`register_handler` 只在 registry 里还没有同名工具时登记
    # spec，同名复用会静默拿到第一条的 spec（本文件第一轮参数化就是这样把 container
    # 测成了 subprocess）。这是 registry 的既有语义，不是沙箱的。
    tool_name = f"p114_refused_{kind}"

    def handler(args: dict) -> dict:
        calls.append(args)
        return {"did": "work"}

    ex = ToolExecutor()
    ex.register_handler(tool_name, handler, spec=_spec(tool_name, sandbox=kind))
    with caplog.at_level(logging.WARNING):
        result = await ex.execute(tool_name, {"x": 1})
    assert calls == [], "声明跑不了的档位却仍执行 ⇒ 安全声明成了装饰"
    assert result.success is False
    assert result.metadata["reason"] == "sandbox_unavailable"
    assert result.metadata["sandbox"] == kind
    assert result.retryable is False, "重试换不来一个实现"
    assert reason_phrase in (result.error or "")
    assert tool_name in caplog.text


@pytest.mark.asyncio
async def test_refusal_does_not_trigger_observation_hooks(sandbox_on) -> None:
    """被闸拒不是一次执行：before/after 都不该响（与风险/权限闸门同口径）。"""
    seen: list[str] = []

    async def handler(args: dict) -> dict:
        seen.append("handler")
        return {}

    ex = ToolExecutor()
    ex.register_handler("p114_obs", handler, spec=_spec("p114_obs", sandbox="container"))
    ex.set_observation_hooks(
        before=lambda name, args: seen.append("before"),
        after=lambda name, result: seen.append("after"),
    )
    result = await ex.execute("p114_obs", {})
    assert result.success is False
    assert seen == [], "拒绝路径碰了观测钩子 = 把闸门拒绝记成了一次执行"


@pytest.mark.asyncio
async def test_refusal_outranks_retry(sandbox_on) -> None:
    """retryable 的工具也一样：终局，不进重试循环（否则一次配置错要打 N 条日志）。"""
    calls: list[int] = []

    async def handler(args: dict) -> dict:
        calls.append(1)
        return {}

    ex = ToolExecutor()
    ex.register_handler(
        "p114_retry",
        handler,
        spec=_spec("p114_retry", sandbox="subprocess", retryable=True, max_retries=2),
    )
    result = await ex.execute("p114_retry", {})
    assert result.success is False and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("offload", [False, True])
@pytest.mark.parametrize("name,handler", HANDLER_MATRIX)
async def test_flag_on_with_default_kind_equals_flag_off(
    name: str, handler, offload, monkeypatch
) -> None:
    """端口打开 + 默认档 ⇒ 与关着逐位一致（成功标志、返回值、错误文本三者都比）。

    两次执行各用**自己的工具名**：`ToolRegistry` 是进程级单例，共用名字会让第一次
    注册的 spec 吃掉第二次——那样比的是同一个对象，不是两条路径。
    """
    monkeypatch.setattr(settings, "TOOL_SYNC_OFFLOAD_ENABLED", offload)

    async def _run(flag_on: bool) -> tuple[bool, object, object]:
        monkeypatch.setattr(settings, KEY, flag_on)
        tag = "on" if flag_on else "off"
        tool = f"p114_equiv_{name}_{tag}_{offload}"
        ex = ToolExecutor()
        ex.register_handler(tool, handler, spec=_spec(tool))
        result = await ex.execute(tool, {"a": 1})
        return result.success, result.data, result.error

    off = await _run(False)
    on = await _run(True)
    if name == "async_gen_fn":
        # 两次执行拿到两个不同的 generator 对象：比形状，不比身份
        assert off[0] == on[0] and off[2] == on[2], name
        assert inspect.isasyncgen(on[1]), name
    else:
        assert on == off, f"{name}：flag-on 默认档与 flag-off 结果不一致"


@pytest.mark.asyncio
async def test_refusal_follows_the_registry_not_a_hardcoded_list(sandbox_on, monkeypatch) -> None:
    """反向验证：拒绝来自注册表，不是来自写死的字符串——把 `thread` 从注册表摘掉，
    声明 `thread` 的工具就必须被拒，而 `in_process` 照常跑。"""
    monkeypatch.delitem(sandbox_module._SANDBOXES, "thread")
    ran: list[str] = []

    async def handler(args: dict) -> dict:
        ran.append(args["tag"])
        return {}

    ex = ToolExecutor()
    ex.register_handler("p114_thread_gone", handler, spec=_spec("p114_thread_gone", sandbox="thread"))
    ex.register_handler("p114_still_ok", handler, spec=_spec("p114_still_ok"))
    refused = await ex.execute("p114_thread_gone", {"tag": "thread"})
    ok = await ex.execute("p114_still_ok", {"tag": "in"})
    assert refused.success is False and refused.metadata["reason"] == "sandbox_unavailable"
    assert ok.success is True
    assert ran == ["in"], "registry 里有的档位必须照跑，被摘掉的必须不跑"


def test_invoke_in_process_is_the_same_function_both_paths_use() -> None:
    """`InProcessSandbox.run` 与 `_invoke` 指向同一份实现（不是两份等价的代码）。"""
    assert "invoke_in_process" in InProcessSandbox.run.__code__.co_names


# ---------------------------------------------------------------------------
# 6. thread 档真的让事件循环空得出来（T2 验收列「不阻塞并发协程」最小版）
# ---------------------------------------------------------------------------


def _blocking_handler(args: dict) -> str:
    time.sleep(0.2)  # 真的占住调用它的那个线程/循环
    return "done"


async def _measure_stall(tool: str, kind: str) -> float:
    """跑「0.2s 同步 handler + 一个 5ms ticker」，返回循环相邻 tick 的最大停摆秒数。

    量的是**停摆时长**而不是 tick 计数：计数会受机器负载影响，而"0.2s 里循环一次都没转"
    这件事在两种情况下差了一个数量级，判据因此是宽的、也是真的。
    """
    stamps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            stamps.append(time.perf_counter())
            await asyncio.sleep(0.005)

    ex = ToolExecutor()
    ex.register_handler(tool, _blocking_handler, spec=_spec(tool, sandbox=kind))
    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.03)  # 让 ticker 先转起来
    result = await ex.execute(tool, {})
    await asyncio.sleep(0.03)  # 再留一段：阻塞用例里循环要到 handler 跑完才重新转
    stop.set()
    await task
    assert result.success is True, result.error
    assert len(stamps) >= 4, "ticker 没转起来，本用例没有信息量"
    return max(b - a for a, b in zip(stamps, stamps[1:]))


@pytest.mark.parametrize(
    "flag_on,kind,offload,expect_blocks_loop",
    [
        (False, "in_process", False, True),   # 加固前的现状：0.2s 全占
        (False, "in_process", True, False),   # G1：靠**全局** flag 才不占
        (True, "in_process", False, True),    # 端口开 + 默认档：语义照旧，不偷偷下放
        (True, "thread", False, False),       # 端口开 + 声明 thread：**不碰全局 flag** 就不占
    ],
)
def test_thread_declaration_replaces_the_global_offload_flag(
    flag_on: bool, kind: str, offload: bool, expect_blocks_loop: bool, monkeypatch
) -> None:
    """第四行是这个端口存在的理由：`sandbox="thread"` 让**这条**工具不再占住循环，
    而不必打开全局 `TOOL_SYNC_OFFLOAD_ENABLED` 去改所有 handler 的循环亲和语义。
    第三行是它的反面保证：光把端口打开、什么都不声明，行为一个字都不变。"""
    monkeypatch.setattr(settings, KEY, flag_on)
    monkeypatch.setattr(settings, "TOOL_SYNC_OFFLOAD_ENABLED", offload)
    # 每个用例拿到自己的工具名：`ToolRegistry` 是进程级单例，撞名会串规格。
    tool = f"p114_stall_{int(flag_on)}_{kind}_{int(offload)}"
    stall = asyncio.run(_measure_stall(tool, kind))
    if expect_blocks_loop:
        assert stall >= 0.1, f"{kind}/flag={flag_on}/offload={offload} 本应占住循环，停摆 {stall:.3f}s"
    else:
        assert stall < 0.1, f"{kind}/flag={flag_on}/offload={offload} 不该占住循环，停摆 {stall:.3f}s"


# ---------------------------------------------------------------------------
# 7. 配置面 + 部署面
# ---------------------------------------------------------------------------


def _declares_key(text: str) -> bool:
    """键名必须**整词**出现。

    变异测试暴露的口径：`KEY in text` 会把 `EXECUTION_SANDBOX_ENABLED_TYPO`
    也算成「已同步」——而自检查表 / p41 清单里真正被登记的恰恰是那个拼错的键。
    """
    return re.search(rf"(?<![A-Za-z0-9_]){KEY}(?![A-Za-z0-9_])", text) is not None


@pytest.mark.parametrize(
    "rel",
    [
        ".env.example",
        "agentix/config/selfcheck.py",
        "docs/architecture/配置管理.md",
        "docs/architecture/运行时护栏.md",
        "tests/test_p41_doc_consistency.py",
        "docker-compose.yml",
    ],
)
def test_key_declared_everywhere(rel: str) -> None:
    text = (PROJECT_ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")
    assert _declares_key(text), f"未同步到 {rel}"


def test_key_declared_everywhere_guard_has_teeth() -> None:
    """反向钉子：把键名加一个后缀，整词匹配就必须不再认它（用源码里那个键当被测串，
    比造一个字面量更抗重命名）；同时裸 `KEY = False` 这种写法仍要算数。"""
    assert _declares_key(f"{KEY} = False")
    assert _declares_key(f"- {KEY}=${{{KEY}:-False}}")
    assert not _declares_key(f"'{KEY}_TYPO'"), "后缀键不是这个键，登记表里写错名必须报出来"
    assert not _declares_key(f"'PRE_{KEY}'")


def test_env_example_pins_it_off() -> None:
    """`.env.example` 写 true 会让每个新部署的执行路径突变。"""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig", errors="replace")
    assert f"{KEY}=false" in text


def test_compose_forwards_it_off_by_default() -> None:
    """否则「flag-off 逐位等价」到部署层就断了。"""
    doc = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = doc["services"]["backend"]["environment"]
    entries = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
    line = next((x for x in entries if str(x).startswith(f"{KEY}=")), None)
    assert line is not None, "docker-compose.yml 没把端口开关转发给 backend"
    assert line.endswith(":-False}") or line.endswith(":-false}"), line


def _staging_override_env() -> dict:
    doc = yaml.safe_load(
        (PROJECT_ROOT / "docker/compose.override.staging.yml").read_text(encoding="utf-8")
    )
    env = doc["services"]["backend"]["environment"]
    assert isinstance(env, dict)
    return env


def _matrix_claim(cell: str) -> str | None:
    match = re.search(r"\*\*([^*]+)\*\*", cell)
    return match.group(1).strip().strip("`").lower() if match else None


def _matrix_row(key: str, text: str) -> list[str]:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"| `{key}`"):
            return [cell.strip() for cell in stripped.strip("|").split("|")]
    raise AssertionError(f"配置矩阵里没有 `{key}` 这一行")


def test_runbook_matrix_staging_claim_matches_the_override() -> None:
    """staging 打开这件事**必须**同时在 override 里真设：矩阵不是愿望清单。"""
    text = (PROJECT_ROOT / "docs/runbooks/预发环境与配置矩阵.md").read_text(encoding="utf-8")
    cells = _matrix_row(KEY, text)
    assert len(cells) == 4, f"矩阵行应 4 列：{cells}"
    claim = _matrix_claim(cells[2])
    assert claim == "true", f"矩阵的 staging 列应加粗声明 true，实际：{cells[2]}"
    assert str(_staging_override_env()[KEY]).strip().lower() == "true"


def test_matrix_guard_has_teeth() -> None:
    """反向钉子：把声明改成 false，上面那条比对就必须对不上。"""
    text = (PROJECT_ROOT / "docs/runbooks/预发环境与配置矩阵.md").read_text(encoding="utf-8")
    tampered = text.replace("| `EXECUTION_SANDBOX_ENABLED` | false | **true**", "| `EXECUTION_SANDBOX_ENABLED` | false | **false**", 1)
    assert tampered != text, "篡改锚点没命中，本钉子已失效"
    cells = _matrix_row(KEY, tampered)
    assert _matrix_claim(cells[2]) != str(_staging_override_env()[KEY]).strip().lower()


def test_adr_0007_exists_and_is_indexed() -> None:
    """ADR 是这条决策的落点；写了文件不进索引 = 下一次没人找得到（编号规则见 adr/README）。"""
    adr = PROJECT_ROOT / "docs/architecture/adr/0007-执行沙箱端口.md"
    assert adr.exists()
    body = adr.read_text(encoding="utf-8")
    for section in ("## Context", "## Decision", "## Rationale", "## Consequences"):
        assert section in body, f"ADR-0007 缺 {section}（Nygard 格式）"
    assert "拒绝执行" in body and "不是" in body, "必须写清端口 ≠ 隔离"
    index = (PROJECT_ROOT / "docs/architecture/adr/README.md").read_text(encoding="utf-8")
    assert "0007-执行沙箱端口.md" in index


def test_project_spec_links_the_port_and_keeps_t2_open() -> None:
    """台账不许把 ⑭ 的落地写成 T2 的完成。"""
    text = (PROJECT_ROOT / "docs/项目实现手册.md").read_text(encoding="utf-8")
    assert "0007-执行沙箱端口.md" in text
    assert "⑮" in text, "⑮（subprocess）仍未收口，台账必须这么说"
