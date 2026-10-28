"""P129：同名工具重复注册（PROJECT_SPEC §24.2 G14 落地回归）。

G14 的原始形态：``ToolExecutor.register_handler`` 只在 registry **没有**同名条目时
登记 spec，于是第二份 ``ToolSpec`` 一个字都不落地（handler 却被覆盖）。⑮ 之后这不再
只是"描述文本对不上"：丢掉的是 ``sandbox`` 声明——一条工具声明在墙里跑、注册却被
静默忽略，正是 ADR-0007 拒绝的那一类"以为有墙"。

本文件钉住四面，缺任一面都算没验完：

1. **判定单源**：字段差异由 ``domain/tool/spec.py::diff_spec_fields`` 算，执行器与
   注册表两个入口都读它——两个"什么算不同"的实现就是下一次漂移的起点。
2. **默认语义未变 + 响亮告警**：flag 关（默认）时仍是**第一次赢**（executor）
   / **最后一次赢**（registry），注册结果逐位不变；差别只在多一条点名告警。
   严格模式（``TOOL_DUPLICATE_REGISTRATION_STRICT=true``）才拒绝注册，且必须
   **在改动 handler 之前**抛——半改状态比静默丢弃更难查。
3. **告警不搬内容**：只点名哪个字段不同，不回显字段值（``description`` 可能是
   远端 MCP 塞进来的长文本，甚至含凭据形态的串）。**例外且刻意**：executor 侧
   还回显存活声明的 ``sandbox`` / ``risk_level`` 取值——两者都是枚举白名单里的
   常数，排障时必须知道执行到底会落在哪一档，而这正是 G14 要防的"以为有墙"。
4. **同步面与部署面**：新键五处（settings / selfcheck / .env.example /
   配置管理.md / p41 清单）齐备；compose 转发默认 False；staging override
   显式钉 false（宿主机 ``.env`` 不许决定预发是否 fail-closed）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterator

import pytest
import yaml
from loguru import logger as loguru_logger

from agentix.config import settings
from agentix.domain.tool.spec import ToolSpec, diff_spec_fields
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import ToolExecutor

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _pin_strict_off(monkeypatch):
    """宿主 ``.env`` 里设了 ``TOOL_DUPLICATE_REGISTRATION_STRICT=true`` 也不许改本文件结论。

    审查登记项 N2：此前只有 ``strict_on`` 会改这个开关，默认路径的用例直接读
    **live** settings——CI 或开发机导出了这个变量，本文件会大面积红，而失败的
    不是被测行为。默认值改由 ``Settings.model_fields`` 的声明值断言（那是真源），
    运行期一律从 False 起跑。
    """
    monkeypatch.setattr(settings, "TOOL_DUPLICATE_REGISTRATION_STRICT", False)


def _spec(name: str, **overrides) -> ToolSpec:
    base = dict(name=name, description="p129 declaration", timeout_ms=1_000)
    base.update(overrides)
    return ToolSpec(**base)


def _handler(label: str):
    async def handler(args: dict) -> dict:
        return {"which": label}

    return handler


@pytest.fixture
def tool_name() -> Iterator[str]:
    """每次用一个唯一工具名，结束时连同 spec 一起注销（模块级单例是共享状态）。"""
    name = f"p129_{uuid.uuid4().hex[:10]}"
    try:
        yield name
    finally:
        tool_registry.delete_tool(name)


@pytest.fixture
def strict_on(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_DUPLICATE_REGISTRATION_STRICT", True)


@pytest.fixture
def loguru_warnings():
    """loguru 不经过 stdlib logging，注册表那一侧要用它自己的 sink 收。"""
    messages: list[str] = []
    sink_id = loguru_logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        yield messages
    finally:
        loguru_logger.remove(sink_id)


# ---------------------------------------------------------------------------
# 1. 判定单源：什么算「不同」
# ---------------------------------------------------------------------------


def test_diff_reports_every_differing_field() -> None:
    a = _spec("p129_diff")
    b = _spec(
        "p129_diff",
        description="another",
        sandbox="subprocess",
        timeout_ms=2_000,
    )
    assert set(diff_spec_fields(a, b)) == {"description", "sandbox", "timeout_ms"}


def test_identical_specs_have_no_difference() -> None:
    a = _spec("p129_diff_same")
    b = _spec("p129_diff_same")
    assert a == b, "前提：ToolSpec 值相等"
    assert diff_spec_fields(a, b) == ()


# ---------------------------------------------------------------------------
# 2. executor 入口：默认告警 + 第一次赢
# ---------------------------------------------------------------------------


def test_first_registration_is_silent(caplog, tool_name) -> None:
    executor = ToolExecutor()
    with caplog.at_level(logging.WARNING):
        executor.register_handler(tool_name, _handler("first"), spec=_spec(tool_name))
    assert "TOOL DUPLICATE REGISTRATION" not in caplog.text, caplog.text


def test_identical_reregistration_is_silent(caplog, tool_name) -> None:
    """幂等重注册（同 spec 重建 handler）是既有惯用法，不该被当成冲突。"""
    executor = ToolExecutor()
    spec = _spec(tool_name)
    with caplog.at_level(logging.WARNING):
        executor.register_handler(tool_name, _handler("first"), spec=spec)
        executor.register_handler(tool_name, _handler("second"), spec=spec)
    assert "TOOL DUPLICATE REGISTRATION" not in caplog.text, caplog.text


def test_handler_only_reregistration_has_nothing_to_compare(caplog, tool_name) -> None:
    """不给 spec 的重复注册没有第二份声明可比——它本来就是"重绑实现"的用法。"""
    executor = ToolExecutor()
    executor.register_handler(tool_name, _handler("first"), spec=_spec(tool_name))
    with caplog.at_level(logging.WARNING):
        executor.register_handler(tool_name, _handler("second"))
    assert "TOOL DUPLICATE REGISTRATION" not in caplog.text, caplog.text


def test_executor_warns_when_the_second_spec_differs(caplog, tool_name) -> None:
    executor = ToolExecutor()
    executor.register_handler(tool_name, _handler("first"), spec=_spec(tool_name))
    with caplog.at_level(logging.WARNING):
        executor.register_handler(
            tool_name, _handler("second"), spec=_spec(tool_name, sandbox="subprocess")
        )
    text = caplog.text
    assert "TOOL DUPLICATE REGISTRATION" in text, text
    assert tool_name in text, "得点名是哪条工具"
    assert "sandbox" in text, "得点名差在哪个字段"
    assert "DISCARDED" in text, "得说清后一份声明没有落地"
    assert "TOOL_DUPLICATE_REGISTRATION_STRICT" in text, "得给出把它变成硬错误的开关"


def test_executor_warning_names_the_surviving_declaration(caplog, tool_name) -> None:
    """只说"丢弃了"不够——排障要知道执行时读到的到底是哪一份。"""
    executor = ToolExecutor()
    executor.register_handler(tool_name, _handler("first"), spec=_spec(tool_name))
    with caplog.at_level(logging.WARNING):
        executor.register_handler(
            tool_name, _handler("second"), spec=_spec(tool_name, sandbox="subprocess")
        )
    assert "sandbox='in_process'" in caplog.text, caplog.text


def test_executor_keeps_the_first_declaration_but_replaces_the_handler(caplog, tool_name) -> None:
    executor = ToolExecutor()
    first = _handler("first")
    second = _handler("second")
    executor.register_handler(tool_name, first, spec=_spec(tool_name))
    with caplog.at_level(logging.WARNING):
        executor.register_handler(tool_name, second, spec=_spec(tool_name, sandbox="subprocess"))
    assert tool_registry.get_spec(tool_name).sandbox == "in_process", "默认语义：第一次赢"
    result = asyncio.run(executor.execute(tool_name, {}))
    assert result.success is True, result
    assert result.data == {"which": "second"}, "handler 确实被后者替换（与基线一致）"


def test_executor_warning_does_not_echo_field_values(caplog, tool_name) -> None:
    """只点名字段，不回显值：description 来自远端 MCP 时是不可信长文本。"""
    executor = ToolExecutor()
    sentinel = "P129-SECRET-SENTINEL"
    executor.register_handler(tool_name, _handler("first"), spec=_spec(tool_name))
    with caplog.at_level(logging.WARNING):
        executor.register_handler(
            tool_name, _handler("second"), spec=_spec(tool_name, description=sentinel)
        )
    assert "description" in caplog.text
    assert sentinel not in caplog.text, "告警把字段内容搬进了日志"


def test_strict_mode_refuses_a_differing_duplicate(tool_name, strict_on) -> None:
    executor = ToolExecutor()
    executor.register_handler(tool_name, _handler("first"), spec=_spec(tool_name))
    with pytest.raises(ValueError) as excinfo:
        executor.register_handler(
            tool_name, _handler("second"), spec=_spec(tool_name, sandbox="subprocess")
        )
    message = str(excinfo.value)
    assert "TOOL DUPLICATE REGISTRATION" in message
    assert "sandbox" in message
    assert "TOOL_DUPLICATE_REGISTRATION_STRICT" in message


def test_strict_mode_leaves_the_first_handler_in_place(tool_name, strict_on) -> None:
    """拒绝必须发生在 handler 赋值之前——半改状态比静默丢弃更难查。"""
    executor = ToolExecutor()
    executor.register_handler(tool_name, _handler("first"), spec=_spec(tool_name))
    with pytest.raises(ValueError):
        executor.register_handler(
            tool_name, _handler("second"), spec=_spec(tool_name, sandbox="subprocess")
        )
    result = asyncio.run(executor.execute(tool_name, {}))
    assert result.success is True
    assert result.data == {"which": "first"}
    assert tool_registry.get_spec(tool_name).sandbox == "in_process"


def test_strict_mode_allows_identical_reregistration(tool_name, strict_on) -> None:
    executor = ToolExecutor()
    spec = _spec(tool_name)
    executor.register_handler(tool_name, _handler("first"), spec=spec)
    executor.register_handler(tool_name, _handler("second"), spec=spec)
    assert executor.has_handler(tool_name)


# ---------------------------------------------------------------------------
# 3. registry 入口：默认仍是「最后一次赢」，严格模式在改动前抛
# ---------------------------------------------------------------------------


def test_registry_warns_when_the_later_declaration_differs(loguru_warnings, tool_name) -> None:
    tool_registry.register(_spec(tool_name), lambda: "first")
    tool_registry.register(_spec(tool_name, sandbox="subprocess"), lambda: "second")
    text = "\n".join(loguru_warnings)
    assert "TOOL DUPLICATE REGISTRATION" in text, text
    assert tool_name in text
    assert "sandbox" in text
    assert "OVERWRITES" in text, "注册表这一侧是最后一次赢，措辞必须与 executor 分开"


def test_registry_last_wins_stays_the_default(loguru_warnings, tool_name) -> None:
    tool_registry.register(_spec(tool_name), lambda: "first")
    tool_registry.register(_spec(tool_name, sandbox="subprocess"), lambda: "second")
    assert tool_registry.get_spec(tool_name).sandbox == "subprocess"
    assert tool_registry.create(tool_name) == "second"


def test_registry_strict_mode_keeps_the_first_declaration(tool_name, strict_on) -> None:
    tool_registry.register(_spec(tool_name), lambda: "first")
    with pytest.raises(ValueError) as excinfo:
        tool_registry.register(_spec(tool_name, sandbox="subprocess"), lambda: "second")
    message = str(excinfo.value)
    assert "description" not in message, "没变的字段不该出现在差异里"
    assert "sandbox" in message
    assert tool_registry.get_spec(tool_name).sandbox == "in_process"
    assert tool_registry.create(tool_name) == "first", "factory 也不许半改"


def test_registry_strict_mode_reports_every_differing_field(tool_name, strict_on) -> None:
    tool_registry.register(_spec(tool_name), lambda: "first")
    with pytest.raises(ValueError) as excinfo:
        tool_registry.register(
            _spec(tool_name, description="other", sandbox="subprocess", timeout_ms=2_000),
            lambda: "second",
        )
    for field in ("description", "sandbox", "timeout_ms"):
        assert field in str(excinfo.value)


def test_frozen_registry_still_wins_over_strict_mode(loguru_warnings, tool_name, strict_on) -> None:
    """冻结期本来就拒绝一切注册（并已告警）；严格模式不必再抛一次。"""
    tool_registry.register(_spec(tool_name), lambda: "first")
    tool_registry.freeze()
    try:
        tool_registry.register(_spec(tool_name, sandbox="subprocess"), lambda: "second")
    finally:
        tool_registry.unfreeze()
    text = "\n".join(loguru_warnings)
    assert "frozen" in text
    assert tool_registry.get_spec(tool_name).sandbox == "in_process"


# ---------------------------------------------------------------------------
# 4. 同步面与部署面
# ---------------------------------------------------------------------------


def test_settings_field_defaults_off() -> None:
    """默认值只认 ``Settings`` 的**声明值**，不读 live 实例。

    审查登记项 N2：live 值会被宿主 ``.env`` 改写，拿它当"默认关"的证据等于让被核
    对象自己当基准（纪律 7 同一条教训）。运行期从 False 起跑由 autouse fixture 保证。
    """
    field = type(settings).model_fields["TOOL_DUPLICATE_REGISTRATION_STRICT"]
    assert field.default is False


@pytest.mark.parametrize(
    "rel",
    [
        ".env.example",
        "agentix/config/selfcheck.py",
        "docs/architecture/配置管理.md",
        "docs/architecture/运行时护栏.md",
    ],
)
def test_key_declared_everywhere(rel: str) -> None:
    text = (PROJECT_ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")
    assert "TOOL_DUPLICATE_REGISTRATION_STRICT" in text, f"未同步到 {rel}"


def test_env_example_pins_it_off() -> None:
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig", errors="replace")
    assert "TOOL_DUPLICATE_REGISTRATION_STRICT=false" in text


def test_compose_forwards_it_off_by_default() -> None:
    doc = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = doc["services"]["backend"]["environment"]
    entries = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
    line = next(
        (x for x in entries if str(x).startswith("TOOL_DUPLICATE_REGISTRATION_STRICT=")),
        None,
    )
    assert line is not None, "docker-compose.yml 没有把开关转发给 backend"
    assert line.endswith(":-False}") or line.endswith(":-false}"), line


def test_staging_override_pins_it_off() -> None:
    """宿主机 ``.env`` 不许决定预发是否 fail-closed（G13 的同一课）。"""
    doc = yaml.safe_load(
        (PROJECT_ROOT / "docker/compose.override.staging.yml").read_text(encoding="utf-8")
    )
    env = doc["services"]["backend"]["environment"]
    assert str(env["TOOL_DUPLICATE_REGISTRATION_STRICT"]).strip().lower() == "false"


def test_project_spec_g14_records_the_landing() -> None:
    """只在 **G14 小节内**核修复状态。

    审查登记项 N4：此前断言的是"整篇 PROJECT_SPEC 里出现 `已落地`"，而该串在
    全文出现二十多次——把 G14 状态改回"未落地"，这条用例照样绿。核对范围必须
    跟着被核对象走，否则守卫只是在测"文件里有人说过已落地"。
    """
    text = (PROJECT_ROOT / "docs/项目实现手册.md").read_text(encoding="utf-8", errors="replace")
    start = text.index("**G14 ·")
    end = text.index("**G9 勘误", start)
    section = text[start:end]
    assert "TOOL_DUPLICATE_REGISTRATION_STRICT" in section, "G14 小节里要写清严格模式开关"
    assert "已落地收口" in section, "G14 的修复状态要写清楚，别让下一个人按旧结论办事"


# ---------------------------------------------------------------------------
# 5. 条数守卫：文档里抄的「本文件多少条」必须由本轮采集现算
# ---------------------------------------------------------------------------

#: 文档声明本文件条数时必须用的短语。用固定短语而不是散落的"N 条"，是因为
#: 历史条目里会有"当时 26 条"这类叙述，模糊匹配会把它们当成当前声明去核。
P129_COUNT_CLAIM = re.compile(r"p129 共 (\d+) 条")

#: 会复述本文件用例数的文档。**为什么要有守卫**：2026-10-26 独立审查登记的一般项
#: N3——套件总数有 `TestTestCountHasSingleSource` 盯着，单文件条数没有，于是同一
#: 个数字在五处手抄（README / CHANGELOG / PROJECT_SPEC / runtime-guardrails /
#: roadmap），改一条用例就要靠人记得改五处。纪律 7 说"被当核对基准的表必须是生成物"，这里把数字交给本轮
#: 真实采集面现算。
DOCS_CLAIMING_P129_COUNT = (
    "README.md",
    "CHANGELOG.md",
    "docs/项目实现手册.md",
    "docs/architecture/运行时护栏.md",
    "docs/design/性能与优化路线图.md",
)


def test_docs_declaring_this_files_case_count_match_the_live_collection(request) -> None:
    """写进文档的本文件条数，必须等于**这一次采集**真正收到的条数。

    身份源只认采集侧 `nodeid`（p125 的 S1 教训：`location` 派生自 `.pyc` 里的
    `co_filename`，容器复用宿主字节码时会与被采文件分家）。采集被 `-k` / 指定
    nodeid / `--lf` / `--deselect` 人为缩窄时响亮跳过——那时 `session.items` 不是
    全量基准，拿它核文档反而是错的。`-m "not integration"` 缩窄不了本文件。
    """
    config = request.session.config
    keyword = getattr(config.option, "keyword", None)
    initial_args = list(getattr(config.option, "file_or_dir", None) or [])
    last_failed = getattr(config.option, "lf", False)
    deselected = list(getattr(config.option, "deselect", None) or [])
    if keyword or last_failed or deselected or any("::" in str(arg) for arg in initial_args):
        pytest.skip("本次采集被 -k / nodeid / --lf / --deselect 缩窄过，session.items 不是全量基准")

    own_name = Path(__file__).name
    live = sum(
        1
        for item in request.session.items
        if PurePosixPath(str(item.nodeid).split("::", 1)[0]).name == own_name
    )
    assert live > 0, f"本文件一条都没采到（{own_name}），没法核文档"

    offenders: list[str] = []
    claims = 0
    for rel in DOCS_CLAIMING_P129_COUNT:
        path = PROJECT_ROOT / rel
        if not path.exists():
            offenders.append(f"{rel} 不存在")
            continue
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            for match in P129_COUNT_CLAIM.finditer(line):
                claims += 1
                if int(match.group(1)) != live:
                    offenders.append(f"{rel}:{line_no} 写 {match.group(1)}，本轮实采 {live}")
    assert claims, (
        "一处 `p129 共 N 条` 的声明都没找到——短语或文档清单失效了；"
        f"本轮实采 {live} 条，真源应写在 README §11 与本次 CHANGELOG 条目"
    )
    assert not offenders, (
        "文档里的本文件条数与本轮实采不符（改用例就要同改这几处）：\n  " + "\n  ".join(offenders)
    )
