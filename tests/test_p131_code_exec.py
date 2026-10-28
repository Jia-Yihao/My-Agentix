"""P2（岗位对照·运行时写代码执行 + 沙箱隔离）：声明治理 + fail-closed + POSIX 端到端。

把 P2 的 DoD 逐条钉死：

1. **flag 治理**：``CODE_EXEC_ENABLED`` 默认 off；flag-off 时 ``code_execute``
   **零注册**（工具注册表里没有它）、``code_agent`` 节点返回 ``{}`` 且
   **不发任何 LLM 请求**（与基线逐位等价）；
2. **声明治理**：L2 + 非空 ``control`` + ``sandbox="subprocess"`` +
   ``side_effect=True`` + ``retryable=False`` + 描述含「何时不能用」+
   **工具超时覆盖代码超时的整个取值域**（1–120 秒全区间）；且 ``code_execute``
   **不进模型可见的声明清单**、**不进任务拆解的可执行工具菜单**、
   **不进按注册表全集导出的 OpenAI schema 拼装点**（``list_tool_specs`` /
   ``list_openai_tools`` / ``get_tool_spec``）；且「不进 LLM 可见工具面」这个
   判定**只有一个源**（``agentix/tools/definitions.py``）——各拼装点不许另立一份；
3. **入参校验**：空/超长代码与非数字超时一律 ``ValueError``，且**错误信息不回显
   代码正文**（AGENTS.md 执行边界 ③）；
4. **墙外 fail-closed**（**任何平台都跑这一组**——它是本机制的核心承诺）：
   pytest 进程不在墙内 ⇒ ``sandbox_gates_active()`` 为假、``run_python_code``
   直接拒绝执行；
5. **POSIX 端到端**：subprocess 隔离档下真跑一段代码（成功 / 非零退出 / 超时被杀）；
6. **全局单例清理**：``code_execute`` 的 spec 与 executor 都在夹具里复位——
   否则 ``tests/test_r1_tool_risk_levels.py``（"已注册的工具必须都是 L0"）
   会被本文件装出来的 L2 声明污染；
7. **交叉不变式**：「非 LLM 可见工具」不得声明成 L0——MCP 导出面**只按 L0 过滤**，
   等级一旦写成 L0，它就会从那儿漏出去（口径 §4）；
8. **``PR_SET_PDEATHSIG``**：墙被 SIGKILL ⇒ 用户代码进程随之被杀（口径 §5），
   跳过判据取自**执行点**的能力探测（``parent_death_signal_supported()``），
   不在本文件另立平台判点；
9. **单文件条数**：文档里写的「p131 共 N 条」必须等于**这一轮真实的采集**条数
   （本文件涨条数时，README / CHANGELOG / runbook / PROJECT_SPEC 里的手抄数字会
   同时腐烂，而全量回归一个字都不红——纪律 7 把它落到用例计数上）。

另有两组"契约级"守卫（防的是**形态退化**，不是行为）：墙内侧运行时的 import 面
必须只有标准库、handler 必须能按 ``module``+``qualname`` 跨进程重建。
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentix import code_exec_runtime
from agentix.application.agents import llm_factory
from agentix.application.agents.code_handler import CodeDraft, create_code_query
from agentix.application.services.code_exec_bridge import install_code_execute_tool
from agentix.code_exec_runtime import (
    MAX_CODE_CHARS,
    MAX_TIMEOUT_S,
    in_sandbox_wall,
    run_python_code,
    sandbox_gates_active,
)
from agentix.config.settings import settings
from agentix.domain.tool.exec_env import scrub_env
from agentix.orchestration.langgraph.lg_states import AgentState
from agentix.tools import registry_init
from agentix.tools.adapters.code_exec import (
    CODE_EXEC_TOOL_NAME,
    build_code_execute_spec,
    code_exec_max_rounds,
    code_exec_timeout_s,
    validate_code_execute_args,
)
from agentix.tools.definitions import (
    NON_LLM_VISIBLE_TOOL_NAMES,
    is_llm_visible_tool,
    llm_visible_specs,
    registry as tool_registry,
)
from agentix.tools.gateway import executor as executor_mod
from agentix.tools.gateway.executor import ToolResult, get_tool_executor, reset_tool_executor
from agentix.tools.sandbox import RLIMIT_NPROC, probe_subprocess_caps

#: skip 判据必须与执行点的判定同源（纪律 6 ④）：读 ``probe_subprocess_caps()``
#: 的真值，而不是在本文件里另立一个"这台机器是不是 POSIX"的判点。
_CAPS = probe_subprocess_caps()
POSIX_ONLY = pytest.mark.skipif(
    not _CAPS["runnable"],
    reason=f"subprocess 隔离档要本机探测通过才谈得上；missing={_CAPS['missing']}",
)

#: ``PR_SET_PDEATHSIG`` 的 skip 判据同样必须与**执行点**同源（纪律 6 ④）：读
#: ``agentix.code_exec_runtime.parent_death_signal_supported()`` 的真值——那正是
#: 执行点自己的能力探测（``_LIBC`` 取不到 ⇒ 静默降级 ⇒ 这条承诺本就不成立）。
#: **不在本文件另写一份"是不是 Linux"**：判点分家就会出现"守卫在容器里被数成 0 条"
#: 那种形态（两个判点各说各话，谁也不知道自己测的是不是同一条承诺）。
_PDEATHSIG_OK, _PDEATHSIG_REASON = code_exec_runtime.parent_death_signal_supported()
PDEATHSIG_SUPPORTED = pytest.mark.skipif(
    not _PDEATHSIG_OK,
    reason=(
        f"PR_SET_PDEATHSIG 不可用（prctl 缺失）：{_PDEATHSIG_REASON}；"
        "口径 §5 的『墙被杀 ⇒ 孙进程随之被杀』这一条在本平台本就不成立"
        "（prctl 是 Linux 独有的，macOS 上没有）"
    ),
)

#: 仓库根：真墙按**脚本路径**起（``python <绝对路径>``），cwd 也必须是仓库根——
#: 墙内 worker 要能 ``import agentix``（它自己会 ``_pin_sys_path``，但 cwd 仍按父侧
#: 的做法落在仓库根）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 真墙的入口脚本（父侧就是按这个路径起它的，见 ``agentix/tools/sandbox.py``）。
WORKER_PATH = REPO_ROOT / "agentix" / "tools" / "subprocess_worker.py"

#: 工具名**不抄字面量**：从声明位 import 常量——字面量在仓内只允许出现在
#: ``agentix/tools/adapters/code_exec.py`` 与唯一真源
#: ``agentix/tools/definitions.py::NON_LLM_VISIBLE_TOOL_NAMES`` 两处。
_CODE_TOOL = CODE_EXEC_TOOL_NAME


# ---------------------------------------------------------------------------
# 全局单例清理 + flag 夹具（与 test_p49_task_execution 同款范式）
# ---------------------------------------------------------------------------


def _reset_code_decls() -> None:
    """清声明缓存 + 摘掉本文件装出来的 ``code_execute``。

    摘 registry 那一步是**必须**的：``code_execute`` 是 L2，而
    ``tests/test_r1_tool_risk_levels.py`` 断言"已注册的工具必须都是 L0"——
    漏摘就是给别的用例埋雷。
    """
    registry_init._DECLARATIONS.clear()
    tool_registry.delete_tool(_CODE_TOOL)


@pytest.fixture
def set_setting():
    """临时改一个 settings 键，退出时复原。

    ``CODE_EXEC_*`` 这些键由配置侧另行落地，可能还没进 ``Settings`` 的字段表——
    而 pydantic v2 对**未声明字段**的赋值会抛
    ``ValueError: object has no field``。所以按"字段是否已声明"分两路：
    已声明 → 走正常赋值（pydantic 会记进 ``__pydantic_fields_set__``）；
    未声明 → 直接写 ``__dict__``（读得到、能复原）。
    """
    restores = []

    def _apply(name: str, value) -> None:
        if name in getattr(settings, "model_fields", {}):
            original = getattr(settings, name)
            setattr(settings, name, value)
            restores.append(lambda: setattr(settings, name, original))
            return

        had = name in settings.__dict__
        original = settings.__dict__.get(name)
        settings.__dict__[name] = value

        def _restore() -> None:
            if had:
                settings.__dict__[name] = original
            else:
                settings.__dict__.pop(name, None)

        restores.append(_restore)

    yield _apply
    for restore in reversed(restores):
        restore()


@pytest.fixture
def code_off(set_setting):
    """flag-off：零注册、零行为（与基线逐位等价）。"""
    set_setting("CODE_EXEC_ENABLED", False)
    _reset_code_decls()
    reset_tool_executor()
    yield
    _reset_code_decls()
    reset_tool_executor()


@pytest.fixture
def code_on(set_setting):
    """flag-on：装上 ``code_execute``（声明 + handler）。"""
    set_setting("CODE_EXEC_ENABLED", True)
    _reset_code_decls()
    reset_tool_executor()
    install_code_execute_tool()
    yield
    _reset_code_decls()
    reset_tool_executor()


@pytest.fixture
def sandbox_on(set_setting):
    """打开 subprocess 隔离档（两把开关都要开，缺一把就是"声明了但跑不了"）。"""
    set_setting("EXECUTION_SANDBOX_ENABLED", True)
    set_setting("TOOL_SANDBOX_SUBPROCESS_ENABLED", True)
    set_setting("TOOL_SANDBOX_SUBPROCESS_UID", -1)
    set_setting("TOOL_SANDBOX_SUBPROCESS_ENV_EXTRA_KEYS", "")


def _state(content: str = "帮我算一下 6*7") -> AgentState:
    return AgentState(messages=[HumanMessage(content=content)])


# ---------------------------------------------------------------------------
# 替身（LLM / executor）：只替换边界，不替换被测逻辑
# ---------------------------------------------------------------------------


class _FakeDraftRunner:
    """``with_structured_output`` 的替身：每次 ``ainvoke`` 交出一份草稿。"""

    def __init__(self, owner: "_FakeLLM", structured_cls) -> None:
        self._owner = owner
        self.structured_cls = structured_cls

    async def ainvoke(self, messages):
        self._owner.draft_calls.append(messages)
        if not self._owner.drafts:
            raise AssertionError("起草次数超出预期：自愈轮次没有按上限收敛")
        return self._owner.drafts.pop(0)


class _FakeLLM:
    """替身 LLM：起草走 ``with_structured_output``，汇总走 ``ainvoke``。"""

    def __init__(self, drafts, summary: str = "汇总答案") -> None:
        self.drafts = list(drafts)
        self.summary = summary
        self.draft_calls: list = []
        self.summary_calls: list = []
        self.structured_cls = None

    def with_structured_output(self, cls):
        self.structured_cls = cls
        return _FakeDraftRunner(self, cls)

    async def ainvoke(self, messages):
        self.summary_calls.append(messages)
        return AIMessage(content=self.summary)


class _FakeExecutor:
    """替身执行器：按预设序列返回结果，并记录调用次数（"调了几次"就是断言对象）。"""

    def __init__(self, results) -> None:
        self._results = list(results)
        self.calls: list = []

    async def execute(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if not self._results:
            raise AssertionError("执行次数超出预期")
        return self._results.pop(0)


# ---------------------------------------------------------------------------
# 1. flag 治理
# ---------------------------------------------------------------------------


class TestFlagGovernance:
    """flag-off = 零注册 + 零 LLM 请求（Strangler 的"逐位等价"）。"""

    def test_flag_off_registers_nothing(self, code_off):
        install_code_execute_tool()
        assert tool_registry.has_tool(_CODE_TOOL) is False

    @pytest.mark.asyncio
    async def test_flag_off_executor_refuses_the_tool(self, code_off):
        result = await get_tool_executor().execute(_CODE_TOOL, {"code": "print(1)"})
        assert result.success is False
        assert "unknown tool" in (result.error or "")

    @pytest.mark.asyncio
    async def test_flag_off_node_is_a_noop_without_any_llm_call(self, code_off, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("flag-off 时不允许发出任何 LLM 请求")

        monkeypatch.setattr(llm_factory, "get_llm_client", _boom)
        assert await create_code_query(_state(), config={}) == {}


# ---------------------------------------------------------------------------
# 2. 声明治理
# ---------------------------------------------------------------------------


class TestDeclaration:
    """声明面（spec + schema）的硬约束。"""

    def test_spec_meets_the_declaration_guards(self):
        spec = build_code_execute_spec()
        assert spec.name == _CODE_TOOL
        assert spec.risk_level == "L2"  # L2 → 有效风险 HIGH；HITL 必须拦得住
        assert spec.control.strip()  # L2 及以上必填控制措施
        assert spec.sandbox == "subprocess"  # 进程内执行用户代码 = 没有墙
        assert spec.side_effect is True
        assert spec.retryable is False  # 有副作用的工具禁止自动重试
        assert "何时不能用" in spec.description
        assert spec.capabilities == ("code",)

    def test_tool_timeout_must_exceed_the_code_timeout(self):
        """工具超时必须**大于**代码超时，且基准取**取值域上界**。

        2026-10-26 返工改写：原断言 ``== int((5.0 + 30) * 1000)`` 把基准钉在
        "当前配置值"上——配置 20 秒而调用方传 ``timeout_s=120`` 时，工具侧会
        先于代码超时掐断整次调用，用户看到的是"没有理由的失败"。新口径：工具
        超时覆盖 ``timeout_s`` 的**整个取值域**，与当前配置值无关。
        """
        spec = build_code_execute_spec()
        assert spec.timeout_ms > code_exec_timeout_s() * 1000
        assert spec.timeout_ms == int((MAX_TIMEOUT_S + 30) * 1000)

    def test_tool_timeout_covers_every_allowed_code_timeout(self):
        """工具级超时必须覆盖 ``timeout_s`` 的**整个取值域**（1/20/60/120）。"""
        spec = build_code_execute_spec()
        for timeout_s in (1, 20, 60, 120):
            assert spec.timeout_ms > timeout_s * 1000, (
                f"timeout_s={timeout_s} 秒没被工具超时覆盖：代码超时还没到，"
                f"工具侧就会先把整次调用掐掉"
            )

    def test_schema_default_timeout_follows_the_configuration(self, set_setting):
        """LLM 可见 schema 的默认值必须来自配置。

        写死 20.0 时，配置成 60 秒的部署里模型看到的仍是"默认 20"并照着填
        ⇒ 配置被静默忽略。
        """
        from agentix.tools.adapters.code_exec import CodeExecuteSchema

        set_setting("CODE_EXEC_TIMEOUT_S", 60.0)
        assert CodeExecuteSchema(code="print(1)").timeout_s == 60.0
        set_setting("CODE_EXEC_TIMEOUT_S", 20.0)
        assert CodeExecuteSchema(code="print(1)").timeout_s == 20.0

    def test_spec_is_not_llm_visible(self):
        """``code_execute`` 由代码执行节点直接调用，**不进**模型可见的声明清单。"""
        from agentix.tools.registry_init import get_active_tools, get_declarations

        declared = {spec.name for spec, _schema in get_declarations()}
        assert _CODE_TOOL not in declared
        assert _CODE_TOOL not in {cls.__name__ for cls in get_active_tools()}


# ---------------------------------------------------------------------------
# 2b. LLM 可见面：code_execute 进不了任务拆解的可执行工具菜单
# ---------------------------------------------------------------------------


class TestLlmVisibleSurface:
    """``code_execute`` 是执行面，**不进任何 LLM 可见的工具清单**。"""

    def test_code_execute_never_enters_the_decompose_menu(self, code_on):
        """flag-on（声明 + handler 都在）时，菜单里也不许出现 ``code_execute``。

        回归的是"任务拆解工具菜单"这条漏口：菜单会被写进拆解提示词的 ``{menu}``，
        模型看得见就能点名调用——那等于把"跑任意代码"变成一次普通的工具选择，
        与"不进 LLM 可见工具面"的口径直接冲突。
        """
        from agentix.application.services.task_execution import _executable_menu

        assert _CODE_TOOL not in _executable_menu()

    def test_exclusion_does_not_change_the_other_tools_menu(self, code_on):
        """排除只摘掉 ``code_execute``，其它工具的菜单内容逐项不变。"""
        from agentix.application.services.task_execution import _executable_menu
        from agentix.tools.definitions import registry as tool_registry
        from agentix.tools.gateway.executor import get_tool_executor

        # 前置：``code_execute`` 确实已注册且有 handler——否则"菜单里没有它"
        # 可能只是因为它压根没进注册表，这条断言就测不出排除逻辑本身。
        executor = get_tool_executor()
        assert tool_registry.has_tool(_CODE_TOOL) is True
        assert executor.has_handler(_CODE_TOOL) is True

        expected = {
            spec.name: spec
            for spec in tool_registry.get_specs()
            if spec.name != _CODE_TOOL and executor.has_handler(spec.name)
        }
        assert _executable_menu() == expected


# ---------------------------------------------------------------------------
# 2c. LLM 可见面：判定只有一个源 + 全部拼装点都过筛
# ---------------------------------------------------------------------------


class TestLlmVisibleSurfaceSingleSource:
    """「不进 LLM 可见工具面」的判定必须有且只有一个源，且覆盖全部拼装点。

    上一轮只堵了任务拆解菜单那一条路；本组钉的是两件事：

    1. **同源**（纪律 6 ④）：判定集中在
       ``agentix/tools/definitions.py::NON_LLM_VISIBLE_TOOL_NAMES``，
       各拼装点不许另立一份（本地按名字排除 ⇒ 改一处漏一处）；
    2. **拼装点全覆盖**：``code_execute`` 的 spec 会被 ``register_handler``
       顺带登记进**全局注册表**，而按注册表全集导出的两个 OpenAI schema
       拼装点（``list_tool_specs`` / ``list_openai_tools``）也会把它带出去。
    """

    def test_the_single_source_holds_the_code_exec_tool(self):
        """守卫：真源集合必须含 ``CODE_EXEC_TOOL_NAME``（工具改名即红）。

        工具名**从声明位 import**，不在本文件抄字面量——否则改名时两处
        各自为政，守卫跟着一起"绿"，什么也钉不住。
        """
        assert CODE_EXEC_TOOL_NAME in NON_LLM_VISIBLE_TOOL_NAMES
        assert is_llm_visible_tool(CODE_EXEC_TOOL_NAME) is False

    def test_llm_visible_specs_filters_by_the_single_source(self):
        """真源里的工具被过滤掉，其它声明原样保留。"""
        from agentix.tools.definitions import ToolSpec

        hidden = ToolSpec(name=CODE_EXEC_TOOL_NAME, description="", parameters={})
        visible = ToolSpec(name="p131_visible_tool", description="", parameters={})
        kept = llm_visible_specs([hidden, visible])
        assert [spec.name for spec in kept] == ["p131_visible_tool"]

    def test_openai_schema_assembly_points_exclude_the_code_exec_tool(self, code_on):
        """flag-on 且工具已装上时，两个 OpenAI schema 拼装点都不得导出它。

        前置：``code_execute`` 确实已进全局注册表——``register_handler`` 会
        顺带把 spec 登记进去，所以"注册表全集"里**有**它；拼装点必须过筛。
        """
        from agentix.tools import list_openai_tools, list_tool_specs

        assert tool_registry.has_tool(CODE_EXEC_TOOL_NAME) is True

        openai_names = [tool["function"]["name"] for tool in list_openai_tools()]
        spec_names = [spec["name"] for spec in list_tool_specs()]
        assert CODE_EXEC_TOOL_NAME not in openai_names
        assert CODE_EXEC_TOOL_NAME not in spec_names

    def test_get_tool_spec_refuses_a_non_llm_visible_tool(self, code_on):
        """单点查询同样受约束：拿不到它的 OpenAI schema（否则等于漏一个拼装口）。"""
        from agentix.tools import get_tool_spec

        with pytest.raises(KeyError):
            get_tool_spec(CODE_EXEC_TOOL_NAME)

    def test_every_assembly_point_follows_the_same_source(self, code_on, monkeypatch):
        """判定只能有一个源：真源一改，所有拼装点必须同步跟着变。

        反证手法：把真源**放行**（集合置空）⇒ 任务拆解菜单与 OpenAI schema
        拼装点都必须跟着把 ``code_execute`` 放出来；若某处自己写了"按名字
        排除"，它不会跟着真源变 ⇒ 本守卫红（这正是"两个源"的形态）。
        再把真源**改回排除**，两个拼装点必须同步跟着排除。
        """
        import agentix.tools.definitions as defs
        from agentix.application.services.task_execution import _executable_menu
        from agentix.tools import list_openai_tools
        from agentix.tools.gateway.executor import get_tool_executor

        def _openai_names() -> set:
            return {tool["function"]["name"] for tool in list_openai_tools()}

        # 前置：工具已注册且有 handler ⇒ 真源放行时它本就该出现在拼装结果里。
        assert tool_registry.has_tool(CODE_EXEC_TOOL_NAME) is True
        assert get_tool_executor().has_handler(CODE_EXEC_TOOL_NAME) is True

        monkeypatch.setattr(defs, "NON_LLM_VISIBLE_TOOL_NAMES", frozenset())
        assert CODE_EXEC_TOOL_NAME in _executable_menu()
        assert CODE_EXEC_TOOL_NAME in _openai_names()

        monkeypatch.setattr(
            defs, "NON_LLM_VISIBLE_TOOL_NAMES", frozenset({CODE_EXEC_TOOL_NAME})
        )
        assert CODE_EXEC_TOOL_NAME not in _executable_menu()
        assert CODE_EXEC_TOOL_NAME not in _openai_names()


# ---------------------------------------------------------------------------
# 2d. 不变式：「非 LLM 可见工具」不得声明成 L0
# ---------------------------------------------------------------------------


def _registry_risk_levels() -> dict:
    """全局注册表里的 ``risk_level``（工具名 → 等级）。

    这一份是 **MCP 导出面真正读的那一份**（``agentix/capabilities/mcp/tool_source.py``
    只认 ``risk_level == "L0"``，遍历的就是这个注册表）。
    """
    return {spec.name: spec.risk_level for spec in tool_registry.get_specs()}


def _declaration_risk_levels() -> dict:
    """显式声明清单里的 ``risk_level``（工具名 → 等级）。

    这一份是**声明面**读的那一份（``registry_init.get_declarations()``），
    它是显式声明的清单，不是注册表全集 ⇒ 有些工具只在它这儿出现。
    """
    return {spec.name: spec.risk_level for spec, _schema in registry_init.get_declarations()}


def _declared_risk_levels() -> dict:
    """解析每个已声明工具的 ``risk_level``（工具名 → 等级）。

    取值来源有两条，**两条都要看**：全局注册表（``install_code_execute_tool``
    装上去的执行面工具在这一条里）与声明清单（``registry_init.get_declarations()``）。
    只看其中一条，就会把"某个名字恰好落在另一条里"读成"查不到声明"——而
    "查不到声明"正是这条守卫最危险的失效形态（它会让守卫对那个名字**空转**）。

    **注册表优先**（2026-10-26 独立审查一般项 N3）：这条守卫守的是
    「非 LLM 可见工具不得声明成 L0」，而 MCP 导出面读的是**注册表** ⇒ 取值必须
    以注册表为准，声明清单只作**补齐**（有些工具只在显式清单里出现）。
    反过来（声明清单优先）就会读到 MCP 导出面根本不读的那一份 ⇒ 守卫与被守卫
    分家，两边各说各话（纪律 6 ④ 的不同源形态）。
    """
    levels: dict = {}
    levels.update(_declaration_risk_levels())
    levels.update(_registry_risk_levels())  # 注册表覆盖声明清单 ⇒ 注册表优先
    return levels


def _risk_level_conflicts() -> dict:
    """两处来源**同时**给出同一个名字、但 ``risk_level`` 不一致时的取值对照。

    返回 ``{工具名: (注册表等级, 声明清单等级)}``；空字典 = 没有漂移。

    为什么必须**显式报出来**而不是"静默挑一个"：挑一个就等于把"两源漂移"这件事
    藏起来——此时守卫读的那一份可能恰好是 MCP 导出面**不读**的那一份，它会绿着，
    而被守卫的承诺（"非 LLM 可见工具不得是 L0"）在 MCP 导出面上**已经破了**。
    """
    registry_levels = _registry_risk_levels()
    declaration_levels = _declaration_risk_levels()
    return {
        name: (registry_levels[name], declaration_levels[name])
        for name in sorted(set(registry_levels) & set(declaration_levels))
        if registry_levels[name] != declaration_levels[name]
    }


class TestNonLlmVisibleToolsAreNotL0:
    """「非 LLM 可见工具」的声明不得是 L0。

    理由一句话：MCP 导出面（``agentix/capabilities/mcp/tool_source.py``）**只按
    ``risk_level == "L0"`` 过滤**，完全不认识 ``NON_LLM_VISIBLE_TOOL_NAMES`` ⇒
    一个被声明成 L0 的"非 LLM 可见工具"会从那儿漏出去，而 ``code_execute`` 的整个
    设计前提就是"不进 LLM 可见工具面"（口径 §4）。

    所以这条不是"风格要求"，是**两条承诺的交叉处**：LLM 可见面靠名字集合拦，
    MCP 导出面靠等级拦——等级一旦写成 L0，两边就会给出互相矛盾的答案。
    """

    def test_every_non_llm_visible_tool_declares_above_l0(self, code_on):
        """集合里每个工具都必须**解析得出一条声明**，且那条声明不得是 L0。

        覆盖面必须钉住：往集合里加一个**没人声明**的名字，这条守卫会因
        "查不到声明"而对它**空转**——看上去绿着，实际上那个名字一点也没被测到。
        """
        # 两处来源给出的等级必须一致：**不许静默挑一个**（N3）。挑一个就等于把
        # "两源漂移"藏起来——守卫读的那一份可能正是 MCP 导出面不读的那一份，
        # 于是它绿着，而被守卫的承诺在 MCP 导出面上已经破了。
        conflicts = _risk_level_conflicts()
        assert not conflicts, (
            f"注册表与声明清单的 risk_level 漂移了：{conflicts} ⇒ 守卫读到的"
            "取值与 MCP 导出面读的不是同一份（纪律 6 ④：同一个判定不许两个源）"
        )
        levels = _declared_risk_levels()
        unresolved = sorted(NON_LLM_VISIBLE_TOOL_NAMES - set(levels))
        assert not unresolved, (
            f"真源集合里的 {unresolved} 解析不出任何声明 ⇒ 这条守卫对它们**空转**："
            "往集合里加一个没人声明的名字，它就会悄悄从守卫的视野里消失"
        )
        offenders = {
            name: levels[name]
            for name in sorted(NON_LLM_VISIBLE_TOOL_NAMES)
            if levels[name] == "L0"
        }
        assert not offenders, (
            f"{offenders} 声明成了 L0 ⇒ MCP 导出面（只按 L0 过滤）会把它放出去，"
            "而它本该待在 LLM 可见工具面之外（口径 §4）"
        )

    def test_the_code_exec_tool_is_declared_above_l0(self, code_on):
        """``code_execute`` 本身必须是 L2（口径 §4 的声明表）。

        单独钉一条而不是只靠上一条：上一条是"集合里的每个名字"，这一条是
        "**这个**工具"——集合被清空时上一条会空转，这条仍然咬得住。
        """
        assert build_code_execute_spec().risk_level == "L2"
        assert _declared_risk_levels()[CODE_EXEC_TOOL_NAME] == "L2"


# ---------------------------------------------------------------------------
# 3. 入参校验
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    """非法入参必须 fail-fast，且**错误信息不回显代码正文**。"""

    def test_empty_code_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            validate_code_execute_args({"code": "   "})
        assert "print(" not in str(excinfo.value)

    def test_oversized_code_is_rejected_without_echoing_the_body(self):
        body = "print('SECRET-BODY')" + "x" * MAX_CODE_CHARS
        with pytest.raises(ValueError) as excinfo:
            validate_code_execute_args({"code": body})
        message = str(excinfo.value)
        assert "SECRET-BODY" not in message
        assert "print(" not in message

    def test_non_numeric_timeout_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            validate_code_execute_args({"code": "print(1)", "timeout_s": "abc"})
        assert "print(" not in str(excinfo.value)

    def test_valid_args_are_normalised(self):
        normalised = validate_code_execute_args({"code": "print(1)"})
        assert normalised["code"] == "print(1)"
        assert normalised["timeout_s"] == code_exec_timeout_s()


# ---------------------------------------------------------------------------
# 4. 墙外 fail-closed（任何平台都跑）
# ---------------------------------------------------------------------------


class TestOutsideWallFailsClosed:
    """墙外一律拒绝——**绝不**退回进程内执行用户代码。"""

    def test_pytest_process_is_not_inside_the_wall(self):
        assert in_sandbox_wall() is False
        ok, reason = sandbox_gates_active()
        assert ok is False
        assert reason  # 失败必须带原因（可观察），不能是空串

    @pytest.mark.asyncio
    async def test_run_python_code_refuses_outside_the_wall(self):
        with pytest.raises(RuntimeError) as excinfo:
            await run_python_code({"code": "print(1)"})
        assert "code execution refused" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. POSIX 端到端
# ---------------------------------------------------------------------------


@POSIX_ONLY
class TestPosixEndToEnd:
    """subprocess 隔离档下真跑一段代码（跳过条件与执行点同源）。"""

    @pytest.mark.asyncio
    async def test_code_runs_inside_the_wall(self, code_on, sandbox_on):
        result = await get_tool_executor().execute(
            _CODE_TOOL, {"code": "print(6*7)"}
        )
        assert result.success is True, result.error
        assert result.data["exit_code"] == 0
        assert "42" in result.data["stdout"]
        assert result.data["timed_out"] is False

    @pytest.mark.asyncio
    async def test_non_zero_exit_code_is_reported_as_is(self, code_on, sandbox_on):
        result = await get_tool_executor().execute(
            _CODE_TOOL, {"code": "raise SystemExit(3)"}
        )
        assert result.success is True, result.error
        assert result.data["exit_code"] == 3

    @pytest.mark.asyncio
    async def test_timeout_kills_the_process_and_returns_in_seconds(self, code_on, sandbox_on):
        started = time.monotonic()
        result = await get_tool_executor().execute(
            _CODE_TOOL, {"code": "while True:\n    pass", "timeout_s": 1}
        )
        elapsed = time.monotonic() - started
        assert result.success is True, result.error
        assert result.data["timed_out"] is True
        # 秒级返回：代码超时（1s）必须先于工具超时（>30s）生效。
        assert elapsed < 20, f"超时没有在秒级返回（实际 {elapsed:.1f}s）"


# ---------------------------------------------------------------------------
# 6. 节点流程（治理拒绝是终局 / 非零退出才自愈）
# ---------------------------------------------------------------------------


class TestHandlerFlow:
    """``code_agent`` 的两条流程契约。"""

    @pytest.mark.asyncio
    async def test_governance_refusal_is_terminal(self, code_on, monkeypatch):
        refusal = ToolResult(
            tool_name=_CODE_TOOL,
            success=False,
            error="sandbox_unavailable: the subprocess tier cannot honour its isolation",
        )
        fake_executor = _FakeExecutor([refusal])
        fake_llm = _FakeLLM([CodeDraft(code="print(1)")])
        monkeypatch.setattr(executor_mod, "get_tool_executor", lambda: fake_executor)
        monkeypatch.setattr(llm_factory, "get_llm_client", lambda *a, **k: fake_llm)

        out = await create_code_query(_state(), config={})

        assert len(fake_executor.calls) == 1  # 拒绝是终局：不自愈
        assert len(fake_llm.draft_calls) == 1
        assert "sandbox_unavailable" in out["messages"][0].content
        assert out["router"].type == "code-query"
        assert out["sources"] == []

    @pytest.mark.asyncio
    async def test_non_zero_exit_triggers_self_repair(self, code_on, monkeypatch, set_setting):
        set_setting("CODE_EXEC_MAX_ROUNDS", 2)
        failing = ToolResult(
            tool_name=_CODE_TOOL,
            success=True,
            data={
                "exit_code": 1,
                "stdout": "",
                "stderr": "boom",
                "timed_out": False,
                "truncated": False,
                "timeout_s": 20.0,
            },
        )
        succeeding = ToolResult(
            tool_name=_CODE_TOOL,
            success=True,
            data={
                "exit_code": 0,
                "stdout": "42",
                "stderr": "",
                "timed_out": False,
                "truncated": False,
                "timeout_s": 20.0,
            },
        )
        fake_executor = _FakeExecutor([failing, succeeding])
        fake_llm = _FakeLLM([CodeDraft(code="print(1)"), CodeDraft(code="print(42)")])
        monkeypatch.setattr(executor_mod, "get_tool_executor", lambda: fake_executor)
        monkeypatch.setattr(llm_factory, "get_llm_client", lambda *a, **k: fake_llm)

        out = await create_code_query(_state(), config={})

        assert len(fake_executor.calls) == 2  # 失败一次 ⇒ 修一次
        assert len(fake_llm.draft_calls) == 2
        # 第二轮的起草必须带着上一轮的真实输出（自愈的依据就是它）。
        assert "boom" in str(fake_llm.draft_calls[1][-1].content)
        assert "42" in str(fake_llm.summary_calls[0][-1].content)
        assert out["messages"][0].content == "汇总答案"

    def test_max_rounds_reader_never_returns_zero(self, set_setting):
        set_setting("CODE_EXEC_MAX_ROUNDS", 0)
        assert code_exec_max_rounds() >= 1

    @pytest.mark.asyncio
    async def test_invalid_args_are_terminal_and_never_executed(self, code_on, monkeypatch):
        """非法入参在**执行点之前**被挡下：终局、不执行、不自愈。"""
        fake_executor = _FakeExecutor([])  # 一次都不该被调用
        fake_llm = _FakeLLM([CodeDraft(code="x" * (MAX_CODE_CHARS + 1))])
        monkeypatch.setattr(executor_mod, "get_tool_executor", lambda: fake_executor)
        monkeypatch.setattr(llm_factory, "get_llm_client", lambda *a, **k: fake_llm)

        out = await create_code_query(_state(), config={})

        assert fake_executor.calls == []
        assert len(fake_llm.draft_calls) == 1  # 不自愈
        assert "code exceeds" in out["messages"][0].content
        # 错误信息不回显代码正文（执行边界 ③）。
        assert "xxxx" not in out["messages"][0].content


# ---------------------------------------------------------------------------
# 7. 契约级守卫（防形态退化）
# ---------------------------------------------------------------------------


class TestRuntimeContract:
    """墙内侧运行时的 import 面与 handler 形态——它们本身就是安全边界。"""

    def test_runtime_module_imports_stdlib_only(self):
        """模块级 import 只允许标准库。

        理由：墙内子进程会 import 本模块——import ``agentix.config.settings``
        会把宿主 ``.env`` 里的密钥读进墙里；模块级 import ``resource`` 会让本文件
        在 Windows 上根本导不进来。
        """
        source = Path(code_exec_runtime.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_level = [
            node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        imported: set = set()
        for node in module_level:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif node.module:
                imported.add(node.module.split(".")[0])
        assert "resource" not in imported
        assert not imported & {"agentix", "loguru", "pydantic", "langchain", "langchain_core"}

    def test_handler_is_rebuildable_across_processes(self):
        """handler 必须是模块级函数：墙内按 ``module``+``qualname`` 重建它。"""
        assert run_python_code.__module__ == "agentix.code_exec_runtime"
        assert run_python_code.__qualname__ == "run_python_code"
        assert "<" not in run_python_code.__qualname__  # 闭包/lambda 跨不过进程边界

    def test_rlimit_constants_are_the_declared_ones(self):
        assert code_exec_runtime.RLIMIT_AS_MB == 512
        assert code_exec_runtime.RLIMIT_CPU_S == 30
        assert code_exec_runtime.RLIMIT_FSIZE_MB == 32

    def test_sandbox_side_reads_the_same_numbers(self):
        """父侧下发的闸值与墙内侧声明的闸值必须是同一份数字。"""
        from agentix.tools import sandbox

        assert sandbox.RLIMIT_AS_MB == code_exec_runtime.RLIMIT_AS_MB
        assert sandbox.RLIMIT_CPU_S == code_exec_runtime.RLIMIT_CPU_S
        assert sandbox.RLIMIT_FSIZE_MB == code_exec_runtime.RLIMIT_FSIZE_MB


# ---------------------------------------------------------------------------
# 8. PR_SET_PDEATHSIG：墙被杀 ⇒ 孙进程随之被杀（口径 §5）
# ---------------------------------------------------------------------------


def _build_wall_request(code: str) -> dict:
    """按协议 v2 造一条给**真墙**的请求。

    协议版本从 worker 现读（不写死）：写死一个数字的话，协议一升版这条请求就变成
    "过期信封"，墙按 protocol mismatch 拒掉——而那个失败长得跟"墙起不来"一模一样，
    与真正的原因（版本号写死了）无关。闸值取**同一份常量**（墙内侧三道闸 + 父侧
    进程数闸）：两份数字漂移就会让这条用例测的不是生产配置。
    """
    from agentix.tools import subprocess_worker as worker

    return {
        "protocol": worker.PROTOCOL_VERSION,
        "tool_name": "run_python_code",
        "module": "agentix.code_exec_runtime",
        "qualname": "run_python_code",
        "arguments": {"code": code, "timeout_s": 120},
        "limits": {
            "as_mb": code_exec_runtime.RLIMIT_AS_MB,
            "cpu_s": code_exec_runtime.RLIMIT_CPU_S,
            "fsize_mb": code_exec_runtime.RLIMIT_FSIZE_MB,
            "nproc": RLIMIT_NPROC,
        },
        # 不降权：默认 uid 不可用（降权要 root），口径是"不配 uid 就不降"。
        "uid": -1,
        "nonce": secrets.token_hex(16),
    }


def _sleep_forever_code(pid_file: Path) -> str:
    """造一段"把自己的 pid 写下来、然后长时间睡"的用户代码。

    为什么先写 pid：光等墙的应答证明不了孙进程起来了（墙完全可能在孙进程起来
    **之前**就应答）——pid 文件出现才是"孙进程真的在跑"的证据。
    """
    return (
        "import os, time\n"
        f"with open({str(pid_file)!r}, 'w') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "time.sleep(600)\n"
    )


def _parent_death_signal_is_wired() -> bool:
    """执行点是不是真的把 ``preexec_fn=_set_parent_death_signal`` 接上了。

    静态读源码（AST）而不是靠行为反推：行为那一层只在有 ``prctl`` 的平台上落得下，
    而"闸没接线"这件事在任何平台上都该被钉住——否则接线被拆掉时，行为用例在
    别的平台上恒为 skipped，一句都不响。
    """
    source = Path(code_exec_runtime.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if func_name != "create_subprocess_exec":
            continue
        for keyword in node.keywords:
            if keyword.arg != "preexec_fn":
                continue
            value = keyword.value
            if isinstance(value, ast.IfExp):
                # 形如 `preexec_fn=_set_parent_death_signal if _IS_POSIX else None`
                value = value.body
            return isinstance(value, ast.Name) and value.id == "_set_parent_death_signal"
    return False


def _process_alive(pid: int) -> bool:
    """这个 pid 上还有没有**在跑**的进程。

    僵尸**不算**活着：墙被 SIGKILL 打死之后，孙进程会被内核重新挂到 PID 1（或最近的
    subreaper）身上；那个祖先若不回收它，它就以僵尸形态留在进程表里，
    ``os.kill(pid, 0)`` 仍然成功——但它已经不占 CPU、也不再是"留在机器上跑的孤儿"。
    口径 §5 说的是"随之被杀"，僵尸形态正是"已经被杀"的证据。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于我们：保守当成还活着
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[-1].split()
    except OSError:
        return True  # 读不到状态：保守当成还活着（宁可误报，不可漏报）
    if not fields:
        # 读到的是**空串**：状态同样读不出来（内容为空），与"读不到"同义 ⇒
        # 保守当成还活着。早前这里按"空 ⇒ 字段为空 ⇒ 判死"处理，与上面那句
        # docstring 的取向正好相反（判死会把"没读到"误报成"已死"）。
        return True
    return fields[0] != b"Z"


def _sentinel_envelope_line(text: str) -> str:
    """从墙的 stdout 里取出**带哨兵前缀**的那一行（协议信封）。

    worker 的失败信封走 **stdout**（``emit()`` 写 stdout，stderr 通常是空的）
    ⇒ 只读 stderr 的话，"请求写错 / 闸没落下 / 协议版本对不上"这些真因会被盖掉，
    只剩一个裸 exit code。没有这一行本身就是诊断信息：墙连信封都没写出来
    （请求没被解析 / 墙自己炸了 / 协议版本对不上）。
    """
    from agentix.tools.subprocess_worker import SENTINEL

    for line in text.splitlines():
        if line.startswith(SENTINEL):
            return line
    return ""


def _wait_for_user_code_pid(
    pid_file: Path, *, wall, stderr_path: Path, stdout_path: Path
) -> int:
    """等用户代码进程把自己的 pid 写下来（证明孙进程真的起来了）。

    墙提前退出 ⇒ 立刻失败，并把墙的 **stdout 与 stderr** 尾巴都带出来：请求写错、
    闸没落下、协议版本对不上这类原因都能从那儿看出来，不必猜。

    **为什么必须读 stdout**（N4）：worker 的失败信封走 stdout（``emit()`` 写 stdout，
    stderr 为空）⇒ 只读 stderr 的话诊断尾巴通常是空的，"墙提前退出"就只剩一个裸
    exit code，真因全被盖掉。
    """
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if pid_file.exists():
            text = pid_file.read_text(encoding="utf-8", errors="replace").strip()
            if text.isdigit():
                return int(text)
        if wall.poll() is not None:
            # 信封行在**全文**里找，不在尾巴里找：stdout 超过 2000 字符时，早先写下的
            # 失败信封会被尾部窗口切掉前缀，于是把"墙拒了这次请求"误报成"墙连信封都没写"。
            # 尾部只用于展示。
            try:
                stdout_all = stdout_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stdout_all = ""
            stdout_tail = stdout_all[-2000:]
            try:
                stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            except OSError:
                stderr_tail = ""
            envelope = _sentinel_envelope_line(stdout_all)
            if not envelope:
                envelope = (
                    "（stdout 里没有哨兵信封行 ⇒ 墙连信封都没写出来："
                    "请求写错 / 闸没落下 / 协议版本对不上）"
                )
            raise AssertionError(
                f"墙进程提前退出（exit={wall.returncode}）："
                f"stdout 哨兵信封：{envelope}；"
                f"stderr 尾巴：{stderr_tail or '（空）'}；"
                f"stdout 尾巴：{stdout_tail[-500:] or '（空）'}"
            )
        time.sleep(0.05)
    raise AssertionError("用户代码进程始终没起来（pid 文件没出现）")


class TestParentDeathSignal:
    """墙（跑 ``run_python_code`` 的那个进程）被 SIGKILL ⇒ 用户代码进程随之被杀。

    口径 §5 明写的一条承诺：上层超时杀掉墙时，墙的 ``killpg`` 只覆盖**墙自己的
    进程组**，而孙进程 ``start_new_session=True`` 自成一组 ⇒ 够不着它 ⇒ 没有
    ``PR_SET_PDEATHSIG`` 就会在机器上留下一个孤儿（资源闸挡不住无人回收的进程）。
    """

    def test_the_execution_point_wires_the_parent_death_signal(self):
        """静态钉住"执行点真的把 ``preexec_fn`` 接上了"。

        行为那一层之外还要这一条：``PR_SET_PDEATHSIG`` 只在有 ``prctl`` 的平台上
        落得下，行为用例在别的平台上恒为 skipped——而"闸没接线"这件事在任何平台上
        都该被钉住，否则接线被拆掉时没有一条用例会响。
        """
        assert _parent_death_signal_is_wired(), (
            "agentix/code_exec_runtime.py 起用户代码进程时没有把 "
            "preexec_fn=_set_parent_death_signal 接上 ⇒ 墙被杀时孙进程会变成孤儿"
        )

    def test_the_wall_request_satisfies_the_worker_protocol(self):
        """请求信封必须过**墙自己的**解析器（协议版本从 worker 现读）。

        这一条在任何平台上都跑：它证明"请求没写错"——否则在有 ``prctl`` 的平台上
        真跑时，请求被协议层拒掉会长得跟"墙起不来"一模一样，难归因。
        """
        from agentix.tools import subprocess_worker as worker

        request = _build_wall_request("print(1)")
        parsed = worker.read_request(stdin=io.StringIO(json.dumps(request) + "\n"))
        assert parsed["module"] == "agentix.code_exec_runtime"
        assert parsed["qualname"] == "run_python_code"
        assert parsed["arguments"]["code"] == "print(1)"

    @PDEATHSIG_SUPPORTED
    def test_the_user_code_process_dies_with_the_wall(self, tmp_path):
        """真跑：起**真墙**、真起用户代码进程，再 SIGKILL 墙，断言孙进程随之消失。

        为什么只杀墙那**一个** pid、不组杀：用户代码进程 ``start_new_session=True``
        自成一组，组杀顺带把它带走的话，这条用例测的就不是 ``PR_SET_PDEATHSIG``，
        而是"组杀"那另一条承诺。
        """
        pid_file = tmp_path / "user_code.pid"
        stdout_path = tmp_path / "wall.stdout"
        stderr_path = tmp_path / "wall.stderr"
        request = _build_wall_request(_sleep_forever_code(pid_file))
        user_pid = None
        wall = None
        try:
            env = scrub_env(project_root=str(REPO_ROOT))
            # 信封是 ensure_ascii=False 的 JSON：区域编码装不下就只剩一个裸 exit code。
            env["PYTHONIOENCODING"] = "utf-8"
            with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
                wall = subprocess.Popen(
                    [sys.executable, str(WORKER_PATH)],
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=str(REPO_ROOT),
                    env=env,
                )
                assert wall.stdin is not None
                wall.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
                wall.stdin.flush()
                wall.stdin.close()

                user_pid = _wait_for_user_code_pid(
                    pid_file, wall=wall, stderr_path=stderr_path, stdout_path=stdout_path
                )

                # 只杀墙这一个 pid（不组杀）：组杀顺带带走孙进程的话，这条用例测的
                # 就不是 PR_SET_PDEATHSIG 了。墙若已经自己退出了（极窄的竞态），
                # 那 PDEATHSIG 早已生效过——断言照样成立，不必在这里报错。
                with contextlib.suppress(ProcessLookupError):
                    os.kill(wall.pid, signal.SIGKILL)

                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline and _process_alive(user_pid):
                    time.sleep(0.05)
                assert not _process_alive(user_pid), (
                    f"墙被 SIGKILL 之后，用户代码进程 {user_pid} 还在跑 ⇒ "
                    "PR_SET_PDEATHSIG 没生效（孤儿进程留在机器上）"
                )
        finally:
            # 清场：墙要 reap；用户代码进程在"闸没生效"那一支还活着，必须杀掉，
            # 否则这条用例会**自己**在机器上留下一个孤儿（比被测的形态还糟）。
            if user_pid is not None and _process_alive(user_pid):
                with contextlib.suppress(OSError):
                    os.kill(user_pid, signal.SIGKILL)
            if wall is not None:
                with contextlib.suppress(Exception):  # noqa: BLE001 - 清场尽力而为
                    wall.wait(timeout=5)


# ---------------------------------------------------------------------------
# 9. 清理：全局注册表里不能留下 code_execute
# ---------------------------------------------------------------------------


def test_code_execute_leaves_no_registry_residue(set_setting):
    """装完再清：L2 声明不许留在全局注册表里（会污染 r1 的"已注册工具都是 L0"）。"""
    set_setting("CODE_EXEC_ENABLED", True)
    _reset_code_decls()
    reset_tool_executor()
    try:
        install_code_execute_tool()
        assert tool_registry.has_tool(_CODE_TOOL) is True
    finally:
        _reset_code_decls()
        reset_tool_executor()
    assert tool_registry.has_tool(_CODE_TOOL) is False


# ---------------------------------------------------------------------------
# 10. 单文件条数守卫：文档里写的条数 == 本轮实采条数
# ---------------------------------------------------------------------------

#: 文档里声明"本文件一共多少条用例"时**必须**用的短语（真源见 runbook 的数字行）。
#: 短语刻意带 `共`：历史口径块写的是"当时 N 条"，不会被这条守卫当成当前声明去核。
P131_COUNT_CLAIM = re.compile(r"p131 共 (\d+) 条")

#: 会复述本文件用例数的文档。**为什么要有守卫**（N7）：套件总数有
#: ``tests/test_p41_doc_consistency.py::TestTestCountHasSingleSource`` 盯着，
#: **单文件条数没有** ⇒ 本文件从 21 条涨到 38 条时，README / CHANGELOG / runbook /
#: PROJECT_SPEC 里那几处手抄数字会同时腐烂，而全量回归一个字都没红（纪律 7：
#: "被当核对基准的表必须是生成物，不是抄本"——这条把它落到用例计数上）。
#: **不含** ``docs/工程口径日志.md``：那是历史流水账（记的是"当时那轮"的读数），
#: 按历史陈述保留，不当当前声明去核；历史段落一律不写"共 N 条"这个短语。
DOCS_CLAIMING_P131_COUNT = (
    "README.md",
    "CHANGELOG.md",
    "docs/runbooks/子进程沙箱取证.md",
    "docs/项目实现手册.md",
)


def _this_file_marker_names() -> set:
    r"""本文件源码里**声明过**的 marker 名（AST 取装饰器，不数文档里的口头声称）。

    给 `-m` 的缩窄判据用：`-m` 只有在表达式点名了本文件真用过的 marker 时，才可能
    改变本文件的采集条数。

    ⚠️ 它取的是**装饰器名**，不是 `getfixturemarker` 意义上的 marker 注册名，所以
    `@pytest.mark.skipif(...)` 的别名（`POSIX_ONLY` / `PDEATHSIG_SUPPORTED`）和 `fixture`
    也会进来。**多判只会更常 skip、不会假红**，属安全侧；本判据只关心"有没有名字被点名"。
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    names: set = set()

    def _decorator_marker_name(node: ast.AST) -> str:
        # 三种写法：@pytest.mark.x / @pytest.mark.x(...) / @x（裸名）
        target = node.func if isinstance(node, ast.Call) else node
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute):
            inner = target.value
            if inner.attr == "mark" and isinstance(inner.value, ast.Name):
                return target.attr
        if isinstance(target, ast.Attribute):
            return target.attr
        if isinstance(target, ast.Name):
            return target.id
        return ""

    for item in ast.walk(tree):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in item.decorator_list:
                name = _decorator_marker_name(dec)
                if name:
                    names.add(name)
    return names


def _markexpr_names(markexpr: str) -> set:
    """从 `-m` 表达式里取出标识符（`not` / `and` / `or` 这些算子不影响判据，留着也无害）。"""
    return {token for token in re.split(r"[^0-9A-Za-z_]+", markexpr) if token}


def _collected_file_name(item: object) -> str:
    r"""采集项所属**文件**的名字，只认采集侧的 `nodeid`。

    为什么不用 `item.location[0]`：`location` 是从 **code object 的 `co_filename`**
    派生的，而 `co_filename` 是编译时写进 `.pyc` 的那条路径。bind-mount 让容器复用
    宿主 Windows 编的字节码时，同一个用例的 `location[0]` 是 `D:\Code\…\tests\x.py`、
    采集侧路径却是 `/repo/tests/x.py` ⇒ 两者分家。更要命的是 POSIX 上反斜杠**不是**
    分隔符，`Path(r"D:\…\x.py").name` 返回**整条字符串**，于是本文件被数成 0 条：
    守卫红在"取证流程"上，而不是它本该核的"文档"上。
    `nodeid` 由 pytest 用 `/` 拼出、相对 rootdir，两平台同形 ⇒ 只有一个身份源（纪律 6 ④）。
    """
    return PurePosixPath(str(item.nodeid).split("::", 1)[0]).name  # type: ignore[attr-defined]


def test_docs_declaring_this_files_case_count_match_the_live_collection(request) -> None:
    """写进文档的本文件用例数，必须等于**这一次采集**真正收到的条数。

    取值口径刻意不依赖 `-rs` 的输出，也不另跑一遍 `--collect-only` 子进程：
    `session.items` 就是产出终端那行"N passed / M skipped"的同一份清单，
    用它现算 ⇒ 文档核的是"执行链真用到的东西"，而不是第二套算法（纪律 6 ④）。

    采集被人为缩窄时**响亮跳过**并说明原因：`-k` / 指定 nodeid / `--lf` / `--deselect`
    都会让 `session.items` 只剩局部，那种时候拿它当基准去核文档反而是错的
    （`--deselect` 是 p125 那条同源守卫第 4 轮审查的 N1：漏了它，一次局部复跑会把
    "人为缩窄"报成"文档腐烂"）。`-m` **只在表达式点名了本文件声明过的 marker 时**才算缩窄
    （第 4 轮审查一般项 3 的初版把 `-m` 无条件列入，于是它在门禁口径
    `-m "not integration"` 下每次全量自我 skip，见 CHANGELOG 那条「闭合时自己引入的回归」）；
    本文件当前挂的是 `asyncio` 与两条 skipif 别名（`POSIX_ONLY` / `PDEATHSIG_SUPPORTED`），
    没有一个名字会被 `-m "not integration"` 点名 ⇒ 标准口径照常判定；将来若挂上会被点名的
    marker，这条守卫自动改为响亮 skip 而不是把局部复跑报成文档腐烂。
    ⚠️ **`--ignore` 掉本文件时这条守卫根本不会执行**（真空，不是绿）：所以"文档核过"
    这句话只在"本文件整份被采集"的那一轮里成立，读日志时先看 collected 行数。
    """
    config = request.session.config
    keyword = getattr(config.option, "keyword", None)
    initial_args = list(getattr(config.option, "file_or_dir", None) or [])
    last_failed = getattr(config.option, "lf", False)
    deselected = list(getattr(config.option, "deselect", None) or [])
    markexpr = getattr(config.option, "markexpr", None)
    # `-m` 只在**表达式点名了本文件用过的 marker** 时才算缩窄。为什么不能无条件把 `-m`
    # 当缩窄：门禁口径就是 `-m "not integration"`，无条件跳过 ⇒ 这条生成物守卫在最常用的
    # 那条命令里天天 self-skip（第 4 轮闭合时实测成 5083 passed / **41 skipped**，多出的
    # 那条 skip 就是它），守卫当场失去承重——比不写这条守卫更糟，因为它会让"文档核过"
    # 这句话变成假的。本文件当前挂的是 `asyncio` 与两条 skipif 别名（`POSIX_ONLY` /
    # `PDEATHSIG_SUPPORTED`），没有一个名字会被 `-m "not integration"` 点名 ⇒ 照常判定；
    # 将来若给本文件挂上 `-m` 会点名的 marker，这条守卫会自动改为响亮 skip 而不是假红。
    declared_markers = _this_file_marker_names()
    narrows_by_marker = bool(markexpr) and bool(
        declared_markers & _markexpr_names(str(markexpr))
    )
    if (
        keyword
        or narrows_by_marker
        or last_failed
        or deselected
        or any("::" in str(arg) for arg in initial_args)
    ):
        pytest.skip("本次采集被 -k / -m / nodeid / --lf / --deselect 缩窄过，session.items 不是全量基准")

    own_name = Path(__file__).name
    live = sum(1 for item in request.session.items if _collected_file_name(item) == own_name)
    assert live > 0, f"本文件一条都没采到（{own_name}），没法核文档"

    offenders: list[str] = []
    claims = 0
    for rel in DOCS_CLAIMING_P131_COUNT:
        path = REPO_ROOT / rel
        if not path.exists():
            offenders.append(f"{rel} 不存在")
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in P131_COUNT_CLAIM.finditer(line):
                claims += 1
                if int(match.group(1)) != live:
                    offenders.append(f"{rel}:{line_no} 写 {match.group(1)}，本轮实采 {live}")
    assert claims, (
        f"一条 `p131 共 N 条` 的声明都没找到——守卫范围或文档短语失效了；"
        f"实采 {live} 条，真源应写在 runbook 的数字行"
    )
    assert (
        not offenders
    ), "文档里的本文件用例数与本轮实采不符（改用例就要同改这几处）：\n  " + "\n  ".join(offenders)
