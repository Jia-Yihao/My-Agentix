"""P109：工具 handler 类型契约（§24.3 T1 ⑧ 落地回归）。

G1 的教训是「`timeout_ms` 对非协程 handler 形同虚设」，但那件事原本只有在
某次调用真的没被截断时才会被发现。⑧ 把它前移到**注册时刻**：
`TOOL_HANDLER_CONTRACT_ENABLED=true` 时，`register_handler` 给非协程 handler
打一条点名告警，说清「这条工具的超时要靠 `TOOL_SYNC_OFFLOAD_ENABLED` 才生效」。

覆盖四面，缺任一面都算没验完：

1. **判定口径**（`executor.py::handler_shape`）：必须与 `_invoke` 的
   「能不能拿到一个可 await 的结果」一致，而不是照抄 `iscoroutinefunction`——
   类的异步 ``__call__`` 用后者会被误判成阻塞 handler，每个类形式的工具都吃一条假告警。
2. **真实入口**：真的调用 `ToolExecutor.register_handler` 与 `execute`，
   断言 flag-off 不产生任何日志（与加固前逐位等价）、flag-on 只对该告的那些响。
3. **同步面**：新键在 `settings.py` / `.env.example` /
   `selfcheck.DEFAULT_IGNORED_MISSING` / `配置管理.md` 表 /
   `运行时护栏.md` 表五处都在。
4. **部署面**：`docs/runbooks/预发环境与配置矩阵.md` 的配置矩阵、
   `docker/compose.override.staging.yml` 与基线 `docker-compose.yml` 的转发三者对齐
   ——矩阵里写「staging=true」而 override 没设，等于给运维一张假图。
   第 6 节已从「三个已知 flag」推广成**整表通则**（凡加粗声明具体值的 staging 单元格
   都逐行核对 override），推广当刻就抓出 `UVICORN_RELOAD` 不只是纸面漂移。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentix.config import settings
from agentix.tools.gateway import executor as executor_module
from agentix.tools.gateway.executor import ToolExecutor, handler_shape

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
def contract_on(monkeypatch):
    """打开闸门——默认关的那些用例走的是另一条路径，不共用本 fixture。"""
    monkeypatch.setattr(settings, "TOOL_HANDLER_CONTRACT_ENABLED", True)


@pytest.fixture
def contract_off(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_HANDLER_CONTRACT_ENABLED", False)


# ---------------------------------------------------------------------------
# 1. handler_shape：口径与 _invoke 一致
# ---------------------------------------------------------------------------


def test_plain_functions_are_classified() -> None:
    assert handler_shape(_sync_handler) == "sync"
    assert handler_shape(_async_handler) == "async"
    assert handler_shape(_async_gen_handler) == "async_gen"


def test_async_dunder_call_is_async_not_a_false_positive() -> None:
    """类的异步 `__call__`：`asyncio.iscoroutinefunction(实例)` 是 False。

    照抄那个判定的话，每个类形式的工具都会吃一条「非协程」假告警，
    而且 `_invoke` 实际会 await 到它的协程——告警说的是不存在的问题。
    """
    instance = _AsyncCallable()
    assert asyncio.iscoroutinefunction(instance) is False, "前提：stdlib 判定确实会漏"
    assert handler_shape(instance) == "async"


def test_sync_dunder_call_is_sync() -> None:
    assert handler_shape(_SyncCallable()) == "sync"


def test_partial_of_async_handler_stays_async() -> None:
    """functools.partial 包 async 函数：`iscoroutinefunction` 自 3.8 起会解包。"""
    assert handler_shape(functools.partial(_async_handler)) == "async"
    assert handler_shape(functools.partial(_sync_handler)) == "sync"


# ---------------------------------------------------------------------------
# 2. flag-off 逐位等价：一条日志都不许多打
# ---------------------------------------------------------------------------


def test_flag_defaults_to_off() -> None:
    assert settings.TOOL_HANDLER_CONTRACT_ENABLED is False


def test_registration_is_silent_when_flag_off(caplog, contract_off) -> None:
    """闸门关着却开始打告警 = 改了既有部署的启动日志面，flag-off 等价性破了。"""
    with caplog.at_level(logging.WARNING):
        ToolExecutor().register_handler("p109_silent_sync", _sync_handler)
    assert caplog.text == "", caplog.text


def test_registration_does_not_raise_when_flag_on(caplog, contract_on) -> None:
    """契约是**告警**不是拒绝：注册失败会让一个观测开关变成新的故障源。"""
    executor = ToolExecutor()
    with caplog.at_level(logging.WARNING):
        executor.register_handler("p109_warn_sync", _sync_handler)
    assert executor.has_handler("p109_warn_sync")


# ---------------------------------------------------------------------------
# 3. flag-on：只对该告的那些响
# ---------------------------------------------------------------------------


def test_sync_handler_is_named_with_the_fix(caplog, contract_on) -> None:
    with caplog.at_level(logging.WARNING):
        ToolExecutor().register_handler("p109_warn_sync", _sync_handler)
    text = caplog.text
    assert "TOOL HANDLER CONTRACT" in text
    assert "p109_warn_sync" in text, "得点名是哪条工具"
    assert "TOOL_SYNC_OFFLOAD_ENABLED" in text, "得给出解除办法，而不是只报问题"


def test_async_handler_produces_no_warning(caplog, contract_on) -> None:
    with caplog.at_level(logging.WARNING):
        ToolExecutor().register_handler("p109_warn_async", _async_handler)
    assert caplog.text == "", caplog.text


def test_async_callable_object_produces_no_warning(caplog, contract_on) -> None:
    with caplog.at_level(logging.WARNING):
        ToolExecutor().register_handler("p109_warn_async_obj", _AsyncCallable())
    assert caplog.text == "", caplog.text


def test_sync_callable_object_is_warned(caplog, contract_on) -> None:
    with caplog.at_level(logging.WARNING):
        ToolExecutor().register_handler("p109_warn_sync_obj", _SyncCallable())
    assert "p109_warn_sync_obj" in caplog.text


def test_async_generator_gets_its_own_reason(caplog, contract_on) -> None:
    """异步生成器不是「阻塞」，下放线程也治不了它——告警必须说不同的话。"""
    with caplog.at_level(logging.WARNING):
        ToolExecutor().register_handler("p109_warn_agen", _async_gen_handler)
    text = caplog.text
    assert "async generator" in text
    assert "TOOL_SYNC_OFFLOAD_ENABLED" not in text, "别把两个原因混成一条建议"


def test_declared_timeout_is_included_when_spec_exists(caplog, contract_on, monkeypatch) -> None:
    monkeypatch.setattr(
        executor_module.tool_registry,
        "get_spec",
        lambda name: SimpleNamespace(timeout_ms=250),
    )
    with caplog.at_level(logging.WARNING):
        ToolExecutor().register_handler("p109_warn_timeout", _sync_handler)
    assert "250" in caplog.text


def test_missing_spec_degrades_to_question_mark_not_crash(caplog, contract_on, monkeypatch) -> None:
    """告警取不到声明值不影响注册——诊断面的故障不得变成执行面的故障（§22.4 fail-open）。"""

    def _boom(name: str):
        raise KeyError(name)

    monkeypatch.setattr(executor_module.tool_registry, "get_spec", _boom)
    executor = ToolExecutor()
    with caplog.at_level(logging.WARNING):
        executor.register_handler("p109_warn_nospec", _sync_handler)
    assert "?" in caplog.text
    assert executor.has_handler("p109_warn_nospec")


# ---------------------------------------------------------------------------
# 4. 执行路径没有被契约检查改变（真跑到 await）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offload,handler,expected",
    [
        (False, _sync_handler, {"echo": {"a": 1}}),
        (True, _sync_handler, {"echo": {"a": 1}}),
        (True, _async_handler, {"echo": {"a": 1}}),
        (False, _AsyncCallable(), {"echo": {"a": 1}}),
        (True, _AsyncCallable(), {"echo": {"a": 1}}),
    ],
)
def test_invoke_results_are_unchanged_by_the_contract(offload, handler, expected, monkeypatch) -> None:
    """契约闸门只读不写：五种 handler × 两种下放开关，结果都必须照旧。"""
    monkeypatch.setattr(settings, "TOOL_HANDLER_CONTRACT_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_SYNC_OFFLOAD_ENABLED", offload)

    async def _run() -> object:
        return await ToolExecutor._invoke(handler, {"a": 1})

    assert asyncio.run(_run()) == expected


# ---------------------------------------------------------------------------
# 5. 同步面：新键五处都在，且默认为关
# ---------------------------------------------------------------------------


def test_settings_field_exists_and_defaults_off() -> None:
    model = type(settings)
    field = model.model_fields["TOOL_HANDLER_CONTRACT_ENABLED"]
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
    assert "TOOL_HANDLER_CONTRACT_ENABLED" in text, f"未同步到 {rel}"


def test_env_example_pins_it_off() -> None:
    """`.env.example` 写 true 会让每个新部署的启动日志面突变。"""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig", errors="replace")
    assert "TOOL_HANDLER_CONTRACT_ENABLED=false" in text


# ---------------------------------------------------------------------------
# 6. 部署面：runbook 的配置矩阵与 staging override 不许各说一套
#
# 这一节是**修出来的**：⑧ 落文档时把三个执行边界 flag 写进了
# `docs/runbooks/预发环境与配置矩阵.md` 的「配置矩阵」，声称 staging 为 true，
# 但 `docker/compose.override.staging.yml` 里一个都没设——矩阵那一列纯属愿望。
# runbook 验收清单第 5 条要求「配置矩阵逐项 diff（`docker compose config` 输出比对）」，
# 于是这张表是被当真用的。表里写了什么，override 就得是什么。
# ---------------------------------------------------------------------------

#: 执行边界三个 flag：键名 → override 里应有的字面值（字符串，compose 里就是这么写的）
BOUNDARY_FLAGS = {
    "TOOL_HANDLER_CONTRACT_ENABLED": "true",
    "MCP_SUBPROCESS_ENV_SCRUB": "true",
    "TOOL_SYNC_OFFLOAD_ENABLED": "false",
}


def _staging_override_env() -> dict:
    doc = yaml.safe_load(
        (PROJECT_ROOT / "docker/compose.override.staging.yml").read_text(encoding="utf-8")
    )
    env = doc["services"]["backend"]["environment"]
    assert isinstance(env, dict), "staging override 的 backend 环境应为映射"
    return env


def _matrix_rows(text: str, key: str) -> list[list[str]]:
    """抽出配置矩阵里该键的行，按 `|` 切成单元格（col1=dev，col2=staging，col3=prod）。"""
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and f"`{key}`" in stripped:
            rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows


def test_staging_override_pins_every_boundary_flag() -> None:
    env = _staging_override_env()
    missing = [key for key in BOUNDARY_FLAGS if key not in env]
    assert not missing, f"staging override 没设这些执行边界 flag：{missing}"


@pytest.mark.parametrize("key,expected", sorted(BOUNDARY_FLAGS.items()))
def test_runbook_matrix_staging_column_matches_override(key: str, expected: str) -> None:
    text = (PROJECT_ROOT / "docs/runbooks/预发环境与配置矩阵.md").read_text(encoding="utf-8")
    rows = _matrix_rows(text, key)
    assert len(rows) == 1, f"{key} 在矩阵里应当只有一行，实际 {len(rows)} 行"
    cells = rows[0]
    assert len(cells) == 4, f"矩阵行应是 4 列，实际：{cells}"
    claim = re.search(r"\*\*(true|false)\*\*", cells[2], re.IGNORECASE)
    assert claim, f"{key} 的 staging 单元格没有明确写 **true**/**false**：{cells[2]}"
    actual = str(_staging_override_env()[key]).strip().lower()
    assert claim.group(1).lower() == expected, f"矩阵对 {key} 的声明漂移：{claim.group(1)}"
    assert actual == expected, (
        f"{key}：runbook 矩阵说 staging={expected}，override 实际是 {actual}"
    )


def _matrix_violations(env: dict) -> list[str]:
    """整张配置矩阵的通则：staging 列里**加粗声明**的值，override 必须真的设成同值。

    抽成纯函数是为了能做反向钉子——同一段判定喂给一份被删掉键的 override，
    必须报出漂移，否则这条守卫等于没写。
    """
    text = (PROJECT_ROOT / "docs/runbooks/预发环境与配置矩阵.md").read_text(encoding="utf-8")
    violations: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        key_match = re.match(r"^\|\s*`([A-Z][A-Z0-9_]{3,})`\s*\|", stripped)
        if not key_match:
            continue
        key = key_match.group(1)
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        claim_match = re.search(r"\*\*([^*]+)\*\*", cells[2])
        if not claim_match:
            continue  # 未加粗的 staging 单元格是描述（「显式注入」「按需」），不是声明
        claim = claim_match.group(1).strip().strip("`").lower()
        if key not in env:
            violations.append(f"{key}: 矩阵声明 staging={claim}，override 未设")
            continue
        actual = str(env[key]).strip().lower()
        if claim not in actual:
            violations.append(f"{key}: 矩阵声明 staging={claim}，override 实际 {actual}")
    return violations


def test_every_bold_staging_claim_is_pinned_in_the_override() -> None:
    """G13 的通则化。本轮由它抓出第二处漂移：矩阵写 staging `UVICORN_RELOAD`/`DB_ECHO`
    = false，override 一个都没设。

    `UVICORN_RELOAD` 那条不是纸面问题——基线 compose 写的是 `${UVICORN_RELOAD:-${DEBUG:-True}}`，
    而 compose 的插值在**宿主机**求值（读 .env / shell 环境，与 override 给容器设的 DEBUG 无关），
    宿主机一句 `DEBUG=true` 就让预发以 `--reload` 起。实测：
    `DEBUG=true docker compose -f docker-compose.yml -f docker/compose.override.staging.yml config`
    曾渲染出 `UVICORN_RELOAD: "true"`（现已由 override 钉成 false）。
    """
    violations = _matrix_violations(_staging_override_env())
    assert not violations, "配置矩阵与 staging override 漂移：\n  " + "\n  ".join(violations)


def test_general_matrix_rule_has_teeth() -> None:
    tampered = dict(_staging_override_env())
    tampered.pop("UVICORN_RELOAD")
    tampered["MCP_SUBPROCESS_ENV_SCRUB"] = "false"
    reported = "\n".join(_matrix_violations(tampered))
    assert "UVICORN_RELOAD" in reported, "删掉 override 的键，通则必须报出来"
    assert "MCP_SUBPROCESS_ENV_SCRUB" in reported, "改掉 override 的值，通则必须报出来"


def test_compose_forwards_the_contract_flag_off_by_default() -> None:
    """基线 compose 必须转发展示位且默认 False——否则「flag-off 逐位等价」到部署层就断了。"""
    doc = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = doc["services"]["backend"]["environment"]
    entries = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
    line = next((x for x in entries if str(x).startswith("TOOL_HANDLER_CONTRACT_ENABLED=")), None)
    assert line is not None, "docker-compose.yml 没有把 ⑧ 的开关转发给 backend"
    assert line.endswith(":-False}") or line.endswith(":-false}"), line


def test_guard_is_not_a_tautology() -> None:
    """反向钉子：把矩阵里的声明改掉，比对必须报错（否则上面那条等于没测）。"""
    text = (PROJECT_ROOT / "docs/runbooks/预发环境与配置矩阵.md").read_text(encoding="utf-8")
    tampered = text.replace("| `TOOL_SYNC_OFFLOAD_ENABLED` | false | **false**", "| `TOOL_SYNC_OFFLOAD_ENABLED` | false | **true**", 1)
    assert tampered != text, "篡改锚点没命中，本钉子已失效"
    cells = _matrix_rows(tampered, "TOOL_SYNC_OFFLOAD_ENABLED")[0]
    claim = re.search(r"\*\*(true|false)\*\*", cells[2], re.IGNORECASE)
    assert claim.group(1).lower() != BOUNDARY_FLAGS["TOOL_SYNC_OFFLOAD_ENABLED"]
