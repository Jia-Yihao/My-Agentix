"""第四轮修复守卫：把"写好了但**没接线**"的四处接上，并锁死回归。

背景（2026-09-23 第四轮扫描，审计模式「声明与生效的落差」）
--------------------------------------------------------
一个能力可以做到 **已实现 + 有单测 + 覆盖率是绿的**，却**从未被生产路径调用**。
更糟的是：那个单测会反过来成为它的洗白材料——读者看到"有测试、覆盖通过"，
会合理地推断"这条校验在生效"。本轮定位到四处：

1. ``validate_app_settings()`` 只被一个测试调用，**启动路径从不调用它**。
   危害不是"少了个检查"，而是整族生产配置错误（限流关闭 / 输出过滤与 PII 脱敏
   关闭 / milvus 未开向量归一化 / 超时参数非法）既拦不住、也不留日志；其中
   ``VECTOR_STORE_BACKEND=milvus`` + ``VECTOR_NORMALIZE=false`` 是**静默的相似度
   口径错误**（结果错、无异常、无日志）。
2. ``RERANK_BREAKER_FAILURE_THRESHOLD`` / ``RERANK_BREAKER_RECOVERY_SECONDS``
   从未在 settings 里声明过，但 ``rerank.py`` 用 ``getattr(_rs, 名字, 字面量)`` 读，
   于是**永远走字面量兜底**——旋钮看起来可配，实际是死的，环境变量永不生效。
3. ``CircuitBreakerRegistry.get_or_create`` 命中同名实例时**静默丢弃**新参数，
   调用方会以为自己配的阈值生效了（首建者胜本身是刻意的，但静默不是）。
4. ``rate_limit_dependency`` 把"是否限流"错绑在 ``REQUIRE_API_KEY`` 上，
   于是"我把鉴权关了"（本地调试常见）会连带静默关掉限流。

本文件的两条纪律
----------------
* **防空真**：每条守卫先断言"目标存在 / 检测器确实能测到东西"，否则一旦正则或
  路径失效，守卫会永远绿——那就是又制造了一个"看起来在守、其实没守"的东西。
* **守契约，不守实现**：例如不锁 ``getattr`` 的写法，只锁"这个名字必须能解析"。

命名：``test_p47_*`` 与其它 ``pNN`` 无先后关系，只作归档分组。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENTIX = PROJECT_ROOT / "agentix"
MAIN_PY = AGENTIX / "main.py"
RATELIMIT_PY = AGENTIX / "infrastructure" / "core" / "ratelimit.py"
RERANK_PY = AGENTIX / "retrieval" / "infra" / "rag" / "rerank.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AST 小工具
# ---------------------------------------------------------------------------

def _find_function(src: str, name: str) -> ast.AST | None:
    """在源码里找（任意层级的）函数定义——比只扫 ``tree.body`` 稳。"""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _called_names(node: ast.AST) -> set[str]:
    """收集子树里所有被调用的名字（``f()`` 取 f，``a.b()`` 取 b）。"""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _attribute_names(node: ast.AST) -> set[str]:
    """收集子树里所有属性访问的**属性名**（``x.FOO`` 取 FOO）。

    为什么这比正则好：只在 ``ast.Attribute`` 节点上取，**不会**误伤注释和
    docstring 里的同名文字。本仓库里 ``rate_limit_dependency`` 的 docstring
    就特意写了 ``REQUIRE_API_KEY`` 来说明"为什么不用它"——正则会被这行骗到，
    AST 不会。
    """
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def _files_calling(func_name: str, root: Path) -> set[str]:
    """扫 ``root`` 下所有 .py，返回**以裸名调用了** ``func_name`` 的文件（相对路径）。"""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(_read(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for sub in ast.walk(tree):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == func_name
            ):
                found.add(path.relative_to(PROJECT_ROOT).as_posix())
                break
    return found


#: 幽灵配置模式：``getattr(<settings>, "字面量名字", <默认值>)``
_PHANTOM_RE = re.compile(
    r"""getattr\(\s*(_?settings|_rs|_unified_settings)\s*,\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""
)


def _phantom_candidates(src: str) -> list[tuple[str, str]]:
    """返回 ``[(接收者表达式, 被读取的字段名), ...]``。"""
    return [(m.group(1), m.group(2)) for m in _PHANTOM_RE.finditer(src)]


# ---------------------------------------------------------------------------
# 日志 / 导入工具
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _loguru_messages(level: str = "DEBUG"):
    """捕获 loguru 的消息文本。

    **必须在 ``agentix.main`` 导入之后再挂 sink**：``main.py`` 在模块级调用
    ``configure_logging()``，而它第一步就是 ``logger.remove()``（无参 = 清掉所有
    sink）。先挂后导入，sink 会被当场删掉，测试就会以一个难懂的"捕获不到日志"失败。
    """
    from loguru import logger as _lg

    buf: list[str] = []
    sink_id = _lg.add(buf.append, level=level, format="{message}")
    try:
        yield buf
    finally:
        _lg.remove(sink_id)


@pytest.fixture(scope="module")
def main_mod():
    """导入一次 ``agentix.main``（导入链很重，实测约 6 秒；session 内靠 sys.modules 缓存）。"""
    import agentix.main as module

    return module


class _FakeDoc:
    def __init__(self, text: str):
        self.page_content = text


class _DummyRequest:
    """仅用于限流的早返回路径；真跑起来需要 Redis，这里刻意不碰。"""

    class _Client:
        host = "127.0.0.1"

    class _Url:
        path = "/chat"

    client = _Client()
    url = _Url()


# ===========================================================================
# 一、validate_app_settings() 必须真的接在启动路径上
# ===========================================================================

class TestSettingsValidationIsWired:
    """守卫「声明 vs 生效」：函数存在 ≠ 有人调用。"""

    def test_helper_is_defined_in_main(self, main_mod):
        fn = _find_function(_read(MAIN_PY), "_run_settings_validation")
        assert fn is not None, (
            "agentix/main.py 里找不到 _run_settings_validation；"
            "启动期配置校验的接线点被删掉了。"
        )
        assert callable(main_mod._run_settings_validation)

    def test_startup_invokes_the_helper(self):
        """``_startup()`` 必须调用它。这是"接上线"的定义。"""
        fn = _find_function(_read(MAIN_PY), "_startup")
        assert fn is not None, "main.py 里找不到 _startup"
        called = _called_names(fn)
        assert called, "防空真：_startup 子树里一个调用都没解析到，AST 走歪了"
        assert "_run_settings_validation" in called, (
            "启动路径没有调用 _run_settings_validation —— "
            "配置校验又变回「只被测试调用」的死代码了。"
        )

    def test_validate_app_settings_has_a_non_test_caller(self):
        """扫整个 agentix/ 包：除了测试，必须真有生产代码调用它。"""
        callers = _files_calling("validate_app_settings", AGENTIX)
        assert callers, (
            "防空真：在 agentix/ 下扫不到任何 validate_app_settings 调用点，"
            "说明扫描器或包路径失效（这本身就该失败）。"
        )
        assert "agentix/main.py" in callers, (
            f"validate_app_settings 的生产调用点缺失，当前只在 {sorted(callers)} 里出现。"
        )

    def test_call_site_detector_is_non_vacuous(self):
        """防空真：检测器必须能**区分**"真调用"与"只是提到了这个名字"。"""
        assert _files_calling("__definitely_not_a_real_call_name__", AGENTIX) == set()

        synthetic_with = "async def _startup():\n    _run_settings_validation()\n"
        assert "_run_settings_validation" in _called_names(ast.parse(synthetic_with))

        synthetic_without = "async def _startup():\n    await run_startup_selfcheck()\n"
        assert "_run_settings_validation" not in _called_names(ast.parse(synthetic_without))

        # 原文里当然出现这个名字（定义处 + 调用处），但"出现"不等于"被调用"：
        # 定义本身不能算调用。
        only_def = "def _run_settings_validation() -> None:\n    pass\n"
        assert "_run_settings_validation" not in _called_names(ast.parse(only_def)), (
            "检测器把『定义了但没调用』也当成了调用——那它就守不住任何东西。"
        )


class TestSettingsValidationPolicy:
    """三态语义：开发不阻断但必须留日志；生产有 error 就拒绝启动。"""

    @pytest.fixture
    def rs(self):
        """``agentix.retrieval.settings`` **模块**（而非其实例），便于打补丁。"""
        from agentix.retrieval import settings as module

        return module

    def test_dev_errors_are_logged_but_not_blocking(self, monkeypatch, rs, main_mod):
        """开发环境：逐条 error **不阻断**——阻断会把校验变成新的故障源。"""
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setattr(rs, "validate_app_settings", lambda: (["boom-dev"], []))

        with _loguru_messages() as buf:
            main_mod._run_settings_validation()  # 不抛

        assert buf, "防空真：捕获不到任何日志，sink 可能没挂上"
        assert any("boom-dev" in m for m in buf), f"error 内容没有进日志：{buf!r}"

    def test_dev_warnings_are_logged(self, monkeypatch, rs, main_mod):
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setattr(rs, "validate_app_settings", lambda: ([], ["warn-dev"]))

        with _loguru_messages() as buf:
            main_mod._run_settings_validation()

        assert any("warn-dev" in m for m in buf), f"warning 没有进日志：{buf!r}"

    def test_dev_pass_logs_an_info_line(self, monkeypatch, rs, main_mod):
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setattr(rs, "validate_app_settings", lambda: ([], []))

        with _loguru_messages() as buf:
            main_mod._run_settings_validation()

        assert any("passed" in m for m in buf), f"通过时也应留痕：{buf!r}"

    def test_production_raises_on_errors(self, monkeypatch, rs, main_mod):
        """生产环境的 error ⇒ **拒绝启动**（带着错误语义对外服务更危险）。"""
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setattr(rs, "validate_app_settings", lambda: (["boom-prod"], []))

        with _loguru_messages() as buf:
            with pytest.raises(RuntimeError, match="refusing to start"):
                main_mod._run_settings_validation()

        assert any("boom-prod" in m for m in buf), (
            "抛异常前必须先把 error 记进日志，否则排障时只看到一个 RuntimeError 壳"
        )

    def test_production_warnings_alone_do_not_block(self, monkeypatch, rs, main_mod):
        """生产环境只有 warning（没有 error）时必须放行——FAISS 之类是"建议"不是"错误"。"""
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setattr(rs, "validate_app_settings", lambda: ([], ["just-a-warning"]))

        with _loguru_messages() as buf:
            main_mod._run_settings_validation()

        assert any("just-a-warning" in m for m in buf), f"warning 未记录：{buf!r}"

    def test_production_passes_when_clean(self, monkeypatch, rs, main_mod):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setattr(rs, "validate_app_settings", lambda: ([], []))

        with _loguru_messages() as buf:
            main_mod._run_settings_validation()

        assert any("passed" in m for m in buf), f"通过时也应留痕：{buf!r}"

    def test_env_var_switches_production_mode(self, monkeypatch):
        """守卫测试本身的正确性：``PRODUCTION`` 是由 ``ENVIRONMENT`` 派生的。

        它是 ``@property``（无 setter），所以只能用环境变量切——这正是上面几条
        测试都走 ``monkeypatch.setenv`` 的原因。
        """
        from agentix.retrieval.settings import settings as rs_instance

        monkeypatch.setenv("ENVIRONMENT", "production")
        assert rs_instance.PRODUCTION is True
        monkeypatch.setenv("ENVIRONMENT", "development")
        assert rs_instance.PRODUCTION is False


# ===========================================================================
# 二、RERANK_BREAKER_* 必须是真字段（并且真的是从 env 读的）
# ===========================================================================

class TestRerankBreakerConfigIsReal:
    def test_fields_declared_with_legacy_defaults(self):
        """字段必须存在，且默认值等于原来写死的兜底值——**行为零变化**。"""
        from agentix.retrieval.settings import settings as rs

        assert int(rs.RERANK_BREAKER_FAILURE_THRESHOLD) == 5
        assert float(rs.RERANK_BREAKER_RECOVERY_SECONDS) == 60.0

    def test_settings_reads_them_from_env(self):
        """端到端证明"环境变量真的能被读到"——这正是修复前失效的性质。

        用**子进程**而不是 ``importlib.reload``：类体里的 ``os.getenv`` 只在导入时
        求值一次，想在原地验证就得 reload；而 reload 会把模块级 ``settings``
        重新绑定成新对象，其它已 ``from ... import settings`` 的模块仍指向旧对象，
        在一个 1700+ 用例的 session 里那是污染而不是测试。
        """
        code = (
            "import os\n"
            "os.environ['RERANK_BREAKER_FAILURE_THRESHOLD'] = '9'\n"
            "os.environ['RERANK_BREAKER_RECOVERY_SECONDS'] = '12.5'\n"
            "from agentix.retrieval.settings import settings as s\n"
            "print(s.RERANK_BREAKER_FAILURE_THRESHOLD, s.RERANK_BREAKER_RECOVERY_SECONDS)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, f"子进程失败：{proc.stderr}"
        assert proc.stdout.split() == ["9", "12.5"], (
            f"环境变量没有生效，读到的仍是兜底值：{proc.stdout!r}"
        )

    def test_rerank_uses_direct_reads_not_getattr_fallback(self):
        src = _read(RERANK_PY)
        assert "RERANK_BREAKER" in src, "防空真：rerank.py 里根本没提这两个名字"
        assert not re.search(r'getattr\(\s*_rs\s*,\s*["\']RERANK_BREAKER', src), (
            "rerank.py 又回去用 getattr(..., 字面量) 读了——那会让环境变量静默失效"
        )
        assert re.search(r"_rs\.RERANK_BREAKER_FAILURE_THRESHOLD", src)
        assert re.search(r"_rs\.RERANK_BREAKER_RECOVERY_SECONDS", src)

    def test_rerank_creates_breaker_with_configured_values(self, monkeypatch):
        """行为层证明：改字段 → 走完 ``rerank_documents`` 后熔断器上真的是新值。"""
        from agentix.reliability.circuit_breaker import registry as breaker_registry
        from agentix.retrieval.infra.rag import rerank as rerank_mod
        import agentix.retrieval.settings as rs_mod

        snapshot = dict(breaker_registry._breakers)
        breaker_registry._breakers.clear()
        try:
            monkeypatch.setattr(rs_mod.settings, "RERANK_BREAKER_FAILURE_THRESHOLD", 7)
            monkeypatch.setattr(rs_mod.settings, "RERANK_BREAKER_RECOVERY_SECONDS", 42.0)

            class _OkClient:
                def __init__(self, *a, **k):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def post(self, *a, **k):
                    class _Resp:
                        def raise_for_status(self):
                            pass

                        def json(self):
                            return {"results": [{"index": 1, "relevance_score": 0.9}]}

                    return _Resp()

            monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _OkClient)
            docs = [_FakeDoc("低分"), _FakeDoc("高分")]
            asyncio.run(rerank_mod.rerank_documents("q", docs))

            breaker = breaker_registry.get(rerank_mod._RERANK_BREAKER_NAME)
            assert breaker is not None, "rerank_documents 没有创建熔断器，断言前提不成立"
            assert breaker.failure_threshold == 7, (
                f"熔断器没吃到配置值：{breaker.failure_threshold} != 7"
            )
            assert float(breaker.recovery_timeout) == 42.0
        finally:
            breaker_registry._breakers.clear()
            breaker_registry._breakers.update(snapshot)

    def test_env_example_documents_the_knobs(self):
        """旋钮必须是可发现的。

        修掉"死旋钮"之后如果没人知道它存在，只是把"死的"换成"隐形的"。
        登记形态用**注释示例**：熔断参数有代码内默认值，写成生效行会让
        ``.env`` 侧触发"缺项"告警噪声（自检只比字段集合）。
        """
        from agentix.config.selfcheck import parse_env_keys

        example = PROJECT_ROOT / ".env.example"
        text = _read(example)
        names = (
            "RERANK_BREAKER_FAILURE_THRESHOLD",
            "RERANK_BREAKER_RECOVERY_SECONDS",
            "LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
            "LLM_CIRCUIT_BREAKER_RECOVERY_SECONDS",
        )
        for name in names:
            assert name in text, f"{name} 未在 .env.example 登记，运维无从发现这个旋钮"

        live = parse_env_keys(example)
        for name in names:
            assert name not in live, (
                f"{name} 被写成了生效行；.env 未同步时启动自检会报「缺项」噪声。"
                f"熔断参数有代码内默认值，应保持注释示例形态。"
            )

    def test_no_phantom_settings_fields_anywhere(self):
        """把「幽灵配置」这一类问题一次性锁死。

        ``getattr(settings, "名字", 兜底值)`` 是这类 bug 的**气味**：出现字面量兜底
        几乎总意味着那个字段并不存在，于是旋钮其实是死的、且静默。
        修复前全仓只有 rerank.py 命中（2 处）；这里把"每个这样的名字都必须能解析"
        变成一条常驻守卫，未来的新幽灵配置会被立刻抓到。
        """
        from agentix.config import settings as unified
        from agentix.retrieval.settings import settings as rs

        candidates: list[tuple[str, str, str]] = []
        for path in sorted(AGENTIX.rglob("*.py")):
            for receiver, name in _phantom_candidates(_read(path)):
                candidates.append((path.relative_to(PROJECT_ROOT).as_posix(), receiver, name))

        assert len(candidates) >= 30, (
            f"防空真：只扫到 {len(candidates)} 处 getattr-with-literal，正则可能失效"
            f"（修复当日实测 33 处）"
        )

        unresolved = []
        for rel, receiver, name in candidates:
            target = unified if receiver == "_unified_settings" else rs
            try:
                getattr(target, name)
            except AttributeError:
                unresolved.append(f"{rel}: getattr({receiver}, {name!r}, ...)")

        assert not unresolved, (
            "以下位置的配置字段并不存在 —— getattr 的字面量兜底会永远生效，"
            "环境变量形同虚设：\n  " + "\n  ".join(unresolved)
        )

    def test_phantom_detector_is_non_vacuous(self):
        """防空真：检测器要能区分「字面量 getattr」和「变量 getattr」。"""
        assert _phantom_candidates("x = getattr(settings, 'FOO', 1)") == [("settings", "FOO")]
        assert _phantom_candidates('x = getattr(_rs, "BAR", 1)') == [("_rs", "BAR")]
        # 动态名字（真·鸭子类型）不该被误报
        assert _phantom_candidates("x = getattr(settings, name, 1)") == []
        assert _phantom_candidates("x = getattr(other_obj, 'FOO', 1)") == []


# ===========================================================================
# 三、熔断器注册表：同名不同参必须说话（保留首建者胜的语义）
# ===========================================================================

class TestCircuitBreakerRegistryParamDrift:
    @pytest.fixture
    def reg(self):
        """快照 + 还原全局注册表。

        为什么需要：``registry`` 是模块级单例，且**没有** autouse fixture 帮它复原
        （``test_r1_rerank_breaker.py`` 里那个 autouse 只作用于它自己的文件）。
        本文件要新建同名实例，必须自己保证不把状态漏给别的用例。
        """
        from agentix.reliability.circuit_breaker import registry

        snapshot = dict(registry._breakers)
        registry._breakers.clear()
        try:
            yield registry
        finally:
            registry._breakers.clear()
            registry._breakers.update(snapshot)

    def test_same_name_returns_the_same_instance(self, reg):
        first = reg.get_or_create("probe_same", failure_threshold=5, recovery_timeout=60.0)
        second = reg.get_or_create("probe_same", failure_threshold=5, recovery_timeout=60.0)
        assert second is first

    def test_first_wins_and_second_call_warns(self, reg, caplog):
        """首建者胜（刻意）+ 参数不一致时**告警**（修复点）。

        修复前是彻底静默的：调用方以为自己配的阈值生效了，实际没生效、且永远查不出来。
        """
        first = reg.get_or_create("probe_drift", failure_threshold=5, recovery_timeout=60.0)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agentix.reliability.circuit_breaker"):
            second = reg.get_or_create("probe_drift", failure_threshold=1, recovery_timeout=0.05)

        assert second is first
        assert first.failure_threshold == 5, "首建者胜的语义被改动了"
        assert float(first.recovery_timeout) == 60.0

        text = caplog.text
        assert "probe_drift" in text, f"参数漂移必须点名熔断器名：{text!r}"
        assert "failure_threshold" in text and "recovery_timeout" in text, (
            f"告警必须说清哪几个参数不一致：{text!r}"
        )

    def test_matching_params_are_silent(self, reg, caplog):
        """参数一致时不该有噪声——否则告警会被习惯性忽略。"""
        reg.get_or_create("probe_silent", failure_threshold=5, recovery_timeout=60.0)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agentix.reliability.circuit_breaker"):
            reg.get_or_create("probe_silent", failure_threshold=5, recovery_timeout=60.0)

        noise = [r for r in caplog.records if r.name == "agentix.reliability.circuit_breaker"]
        assert not noise, f"参数一致时不该告警：{[r.getMessage() for r in noise]}"

    def test_half_open_max_calls_drift_is_also_reported(self, reg, caplog):
        """三个参数都要比，别只比前两个。"""
        reg.get_or_create(
            "probe_half", failure_threshold=5, recovery_timeout=60.0, half_open_max_calls=3
        )

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agentix.reliability.circuit_breaker"):
            reg.get_or_create(
                "probe_half", failure_threshold=5, recovery_timeout=60.0, half_open_max_calls=9
            )

        assert "half_open_max_calls" in caplog.text, (
            f"half_open_max_calls 漂移未被报告：{caplog.text!r}"
        )


# ===========================================================================
# 四、限流开关不得与鉴权开关耦合
# ===========================================================================

class TestRateLimitGateIsIndependent:
    def test_auth_on_must_not_turn_rate_limit_on(self, monkeypatch):
        """★ 核心回归：修复前 ``REQUIRE_API_KEY=True`` 会**启用**限流。

        "限流该不该开"只看 ``RATE_LIMIT_ENABLED``。这里把鉴权打开、限流关掉，
        断言连令牌桶都没被构造。
        """
        from agentix.config import settings as unified
        from agentix.infrastructure.core import ratelimit as rl

        monkeypatch.setattr(unified, "REQUIRE_API_KEY", True)
        monkeypatch.setattr(unified, "RATE_LIMIT_ENABLED", False)

        touched = {"n": 0}

        def _should_not_be_called():
            touched["n"] += 1
            raise AssertionError("限流关闭时不该构造令牌桶")

        monkeypatch.setattr(rl, "get_rate_limiter", _should_not_be_called)

        asyncio.run(rl.rate_limit_dependency(_DummyRequest()))
        assert touched["n"] == 0, (
            "鉴权开关不该决定限流——REQUIRE_API_KEY=True + RATE_LIMIT_ENABLED=False "
            "时仍然走到了令牌桶，说明耦合还在。"
        )

    def test_rate_limit_off_returns_immediately(self, monkeypatch):
        """限流关闭 = 直通，且零开销（不碰 Redis）。"""
        from agentix.config import settings as unified
        from agentix.infrastructure.core import ratelimit as rl

        monkeypatch.setattr(unified, "RATE_LIMIT_ENABLED", False)
        monkeypatch.setattr(
            rl, "get_rate_limiter", lambda: pytest.fail("关闭时不该取限流器")
        )

        assert asyncio.run(rl.rate_limit_dependency(_DummyRequest())) is None

    def test_dependency_reads_rate_limit_enabled(self):
        fn = _find_function(_read(RATELIMIT_PY), "rate_limit_dependency")
        assert fn is not None, "ratelimit.py 里找不到 rate_limit_dependency"
        attrs = _attribute_names(fn)
        assert attrs, "防空真：一个属性访问都没解析到，AST 走歪了"
        assert "RATE_LIMIT_ENABLED" in attrs

    def test_dependency_does_not_read_require_api_key(self):
        """AST 而不是正则：docstring 里**故意**写了 REQUIRE_API_KEY 解释为什么不用它。"""
        src = _read(RATELIMIT_PY)
        assert "REQUIRE_API_KEY" in src, (
            "防空真：连注释里都没提到，说明我的 docstring 被删了（或这个断言已失效）"
        )
        fn = _find_function(src, "rate_limit_dependency")
        assert "REQUIRE_API_KEY" not in _attribute_names(fn), (
            "rate_limit_dependency 又去读 REQUIRE_API_KEY 了——"
            "那会让『关掉鉴权』连带静默关掉限流。"
        )
