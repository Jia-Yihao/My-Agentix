"""p173——T8「路由结果跨请求缓存 + 扩 fast-path 覆盖面」验收。

口径：``agentix/application/agents/routing_cache.py`` 模块 docstring +
``agentix/config/settings.py::ROUTING_CACHE_ENABLED``。

覆盖面与验收口径（按任务卡 ①–⑤ + 三条必须自证的命题）：

1. ``TestFlagDefault``——新 flag 默认关（约定 1）。
2. ``TestFlagOffEquivalence``——flag-off **逐位等价**：
   ① 缓存模块**没有被 import**（``sys.modules`` 里查不到）；
   ② ``routing_cache_total`` 三个取值零增量；
   ③ 路由节点打出的日志**逐行等于基线模板序列**（多一行即红）；
   ④ 「惰性 import 不许上提到 router 模块级」静态 AST 判据（在
      ``TestFlagOffEquivalence.test_flag_off_never_imports_cache_module`` 内，
      补住运行时判据①对顶部 import 的假绿盲区）。
3. ``TestHitSkipsModel``——命中**不构造模型**：第二次同问句把
   ``get_llm_client`` 换成必炸哨兵，仍返回缓存结论。
4. ``TestCacheStoresNoOriginalText``——键里没有问句原文（键 = 8 位换代令牌 +
   sha256）、值里既无 ``question`` 也无 ``reasoning``；且**换代**（换 ``LLM_MODEL`` /
   换提示词段）会让旧条目失效。
5. ``TestCacheUnit``——有界 LRU、TTL 到期、空白归一化、返回值是副本。
6. ``TestFailOpen``——读/写/重建任一抛异常 ⇒ ``warning`` + 当作未命中，问答照常返回。
7. ``TestObservability``——``routing_cache_total{result}`` 取值封闭枚举；
   命中样本在 trajectory 落一步 ``route.cache_hit`` 且带 ``cached=True``。
8. ``TestFastPathExpansion``——新增的助理元问题规则：命中 reason / 意图名 /
   近似不命中样本；reason 词表由 ``FASTPATH_REASONS`` 单源给出（不另抄名单），
   并钉住「扩面只落 general-query」这条边界。
9. ``TestGenerationTokenSingleComputation``——换代令牌单点计算（庚-3 卡 β1，
   台账表 G G6）：一次请求只算一枚令牌（查侧算、传写侧复用；查侧算不出时
   写侧自算兜底），B1–B4 逐条钉计数、兜底与写入条目代前缀同源。
10. ``TestTierRoutingGeneration``——档位路由输入并入换代令牌（庚-3 卡 γ，台账表
    G G7）：档位模型名 / 角色档位表 / 档位 flag 任一改动 ⇒ 令牌换代（C1/C2/C4），
    端到端换档强制重新分类（C3，主症状的行为面），并同源守卫——指纹侧列出的
    档位键必须覆盖 ``model_gateway._TIER_SETTINGS_KEY`` 的全部键（C5，允许多列、禁止少列）。

全部用例离线可跑：无真实 LLM（一律 ``get_llm_client`` 桩），无 Redis/Postgres/Milvus。
复验：``.venv/Scripts/python.exe -m pytest tests/test_p173_routing_cache.py -q``

已知边界（未钉住的面，不要读成"已验证"）
----------------------------------------

1. **跨进程不共享**：缓存是进程内单例，多 worker 部署的命中率按进程分摊；
   本文件只在单进程内断言，不验证"另起一个进程能否命中"（按设计不命中）。
2. **Q1 未裁**：失效口径是保守版「换代失效」。语料换代**不**让缓存失效这一条
   没有用例可钉（路由不读语料，无观测量）；Q1 裁决若增加语料维度，需同步补用例。
3. **``reasoning`` 丢弃**只在本文件钉「值里没有 ``reasoning`` 键」；生产侧没有
   消费点这一事实靠代码位置（``code_handler.py:326`` / ``task_handler.py:105``
   原样透传）阅读确认，无用例守护。

4. **只服务单轮**：写侧判据是 ``len(state.messages) == 1``（``router.py`` 里 ``_store_routing_cache`` 的调用点），
   多轮请求既不查也不写缓存。本文件钉的是单轮口径下的查/写判据，**不**验证多轮
   请求的命中率（按设计恒不命中）。
"""

from __future__ import annotations

import importlib
import re
import sys
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from agentix.application.agents import router as router_mod
from agentix.application.agents.router import (
    FASTPATH_REASONS,
    _fastpath_router,
    analyze_and_route_query,
)
from agentix.config import ablation
from agentix.config import settings as app_settings
from agentix.observability import metrics as metrics_mod
from agentix.orchestration.langgraph.lg_states import AgentState, Router

_CACHE_MODULE = "agentix.application.agents.routing_cache"


def _cache_mod():
    """取当前存活的路由缓存模块。

    刻意**不在文件顶部 import**：flag-off 等价性用例要证明"路由链没把该模块拉
    进来"，顶部 import 会让那条用例永远为假绿。``importlib.import_module`` 走
    ``sys.modules``，因此拿到的是路由链同款的那个模块对象（同一个单例缓存）。
    """
    return importlib.import_module(_CACHE_MODULE)


def _state(question: str) -> AgentState:
    return AgentState(messages=[HumanMessage(content=question)])


def _boom(**_kwargs):
    raise AssertionError("缓存命中应短路：不得构造/调用 LLM 分类器")


class _StubStructuredModel:
    """``get_llm_client`` 桩：``with_structured_output`` 回放固定输出。"""

    def __init__(self, output=None, *, error: Exception | None = None):
        self._output = output
        self._error = error
        self.calls = 0
        self.schemas: list = []

    def with_structured_output(self, cls):
        self.schemas.append(cls)
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._output


class _RecordingLogger:
    """记录路由节点的日志出口（模板与实参分开留，便于逐行对照基线）。

    替换 ``router_mod.logger`` 而不是往 loguru 挂 sink：本文件要断言的是
    "**路由节点**有没有多打一行"，全局 sink 会把别的模块的日志一起收进来，
    断言就得靠关键词过滤——那样"新加了一行缓存日志"可能因为措辞不同而逃过。
    """

    def __init__(self):
        self.calls: list[tuple[str, str, tuple]] = []

    def _record(self, level, msg, args):
        self.calls.append((level, str(msg), tuple(args)))

    def info(self, msg, *args, **kwargs):
        self._record("info", msg, args)

    def debug(self, msg, *args, **kwargs):
        self._record("debug", msg, args)

    def warning(self, msg, *args, **kwargs):
        self._record("warning", msg, args)

    def error(self, msg, *args, **kwargs):
        self._record("error", msg, args)

    def templates(self) -> list[str]:
        return [msg for _lvl, msg, _args in self.calls]

    def text(self) -> str:
        return "\n".join(f"{lvl} {msg} {args}" for lvl, msg, args in self.calls)


@pytest.fixture(autouse=True)
def _isolated_routing_state(monkeypatch):
    """消融开关回默认（router_enabled=True）+ 每例清空进程内缓存。

    宿主若导出了 ``AGENTIX_ABLATION_ROUTER_ENABLED=false``，路由节点会整体绕过
    意图分类（固定 kb-query），本文件大量用例会以"模型没被调用"形式假红。
    缓存是进程级单例，不清理会让上一条用例的条目串到下一条。
    """
    monkeypatch.setenv(f"{ablation.ENV_PREFIX}ROUTER_ENABLED", "true")
    assert ablation.is_enabled("router_enabled") is True
    cache = _cache_mod().get_routing_cache()
    cache.clear()
    yield
    cache.clear()


def _routing_cache_sample(result: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value("routing_cache_total", {"result": result}) or 0.0


def _prometheus_available() -> bool:
    """指标后端是否真的接上了。

    判据取自指标模块自身的结构：``_NoopMetric`` 只在 ``prometheus_client``
    导入失败的那个 ``except`` 分支里定义，因此"存在这个类"就等于"当前是兜底态"。
    不另写一份 import 探测（约定 6 ④：判定不许两个源）。
    """
    return not hasattr(metrics_mod, "_NoopMetric")


# ===========================================================================
# 1) 新 flag 默认关
# ===========================================================================


class TestFlagDefault:
    def test_default_is_off(self):
        assert app_settings.ROUTING_CACHE_ENABLED is False

    def test_module_constants_are_fixed(self):
        """TTL / 容量是模块常量，**刻意不新增配置键**（约定 4 的同步面越窄越好）。"""
        mod = _cache_mod()
        assert mod._TTL_S == 300.0
        assert mod._MAX_ENTRIES == 1024


# ===========================================================================
# 2) flag-off 逐位等价（命题一）
# ===========================================================================


class TestFlagOffEquivalence:
    @pytest.mark.asyncio
    async def test_flag_off_never_imports_cache_module(self, monkeypatch):
        """flag-off：缓存模块**连加载都不发生**（惰性 import 在 flag 判真之后）。"""
        sys.modules.pop(_CACHE_MODULE, None)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        model = _StubStructuredModel(Router(type="general-query", logic="llm: baseline"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert result["router"].type == "general-query"
        assert model.calls == 1
        assert _CACHE_MODULE not in sys.modules, "flag-off 不得加载缓存模块"

        # 台账表 G G10：上面的运行时判据对「惰性 import 被上提到模块顶部」零敏感——
        # 顶部 import 后模块早已进 sys.modules，本用例开头 pop 照样摘掉、flag-off 路径
        # 不再触发，运行时断言仍绿。补一条与运行时同源的静态 AST 守卫：routing_cache 的
        # import 节点不许出现在 router 的模块级语句里（含模块级 if/try 分支、try 的
        # except 臂与模块级类体——它们都在 import router 时即执行）；只允许落在函数体内
        # （惰性 import 的真实位置）。
        import ast as _ast
        import inspect as _inspect

        leaf = _CACHE_MODULE.rsplit(".", 1)[-1]

        def _imports_cache(node):
            if isinstance(node, _ast.Import):
                return any(
                    a.name == leaf or a.name.endswith("." + leaf) for a in node.names
                )
            if isinstance(node, _ast.ImportFrom):
                if node.module and (
                    node.module == _CACHE_MODULE or node.module.endswith("." + leaf)
                ):
                    return True
                return any(a.name == leaf for a in node.names)
            return False

        router_tree = _ast.parse(_inspect.getsource(router_mod))
        # 扫模块级并继续下探类体与 except 臂（都在 import 时即刻执行，其中的 import
        # 实质就是模块级 import）；只跳过函数体（内部的才是合法的惰性 import）。
        module_level: list = []
        stack = list(router_tree.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                module_level.append(node)
            for _field, value in _ast.iter_fields(node):
                if isinstance(value, list):
                    for child in value:
                        if isinstance(child, (_ast.stmt, _ast.ExceptHandler)):
                            stack.append(child)
        hoisted = [n for n in module_level if _imports_cache(n)]
        assert not hoisted, (
            f"routing_cache 的 import 被上提到 router 模块级（{len(hoisted)} 处）——"
            "惰性 import 契约破了，flag-off 不再零加载"
        )
        # 正向哨兵：router 里确实存在 routing_cache 的 import（在函数体内）。否则上面的
        # 「模块级为空」会因压根没这 import 而假绿——用全树 walk 确认它至少出现过一次。
        assert any(_imports_cache(n) for n in _ast.walk(router_tree)), (
            "router 里找不到任何 routing_cache import——正向哨兵失效，上面的守卫无从证伪"
        )

    @pytest.mark.asyncio
    async def test_flag_off_has_no_metric_delta(self, monkeypatch):
        """flag-off：``routing_cache_total`` 三个取值都零增量。"""
        if not _prometheus_available():
            pytest.skip("prometheus_client 不可用，指标断言跳过")
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: baseline"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        before = {r: _routing_cache_sample(r) for r in ("hit", "miss", "error")}

        await analyze_and_route_query(_state("今天天气怎么样"), config={})
        await analyze_and_route_query(_state("今天天气怎么样"), config={})

        after = {r: _routing_cache_sample(r) for r in ("hit", "miss", "error")}
        assert after == before, "flag-off 不得产生任何 routing_cache_total 增量"

    @pytest.mark.asyncio
    async def test_flag_off_log_lines_are_baseline_exact(self, monkeypatch):
        """flag-off：路由节点的日志**逐行等于基线模板序列**（多一行即红）。"""
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        recorder = _RecordingLogger()
        monkeypatch.setattr(router_mod, "logger", recorder)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm")),
        )

        await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert recorder.templates() == [
            "-----Analyze user query type-----",
            "Analyze user query type completed, result: {}",
        ]
        assert "routing cache" not in recorder.text()

    @pytest.mark.asyncio
    async def test_flag_off_keeps_baseline_schema(self, monkeypatch):
        """flag-off：结构化输出 schema 仍是基类 ``Router``（逐位等价的前提）。"""
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "TASK_EXEC_ENABLED", False)
        monkeypatch.setattr(app_settings, "CODE_EXEC_ENABLED", False)
        model = _StubStructuredModel(Router(type="kb-query", logic="llm"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert model.schemas == [Router]


# ===========================================================================
# 3) 命中不构造模型（命题二）
# ===========================================================================


class TestHitSkipsModel:
    @pytest.mark.asyncio
    async def test_second_call_hits_cache_without_building_model(self, monkeypatch):
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        model = _StubStructuredModel(
            Router(
                type="graphrag-query",
                logic="llm: primary",
                question="刑法第264条为什么这么规定",
                decision="graph",
                confidence=0.82,
                reasoning="模型叙述：这句话复述了问句本身",
            )
        )
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        question = "刑法第264条为什么这么规定"

        first = await analyze_and_route_query(_state(question), config={})
        assert model.calls == 1

        # 第二次：把模型入口换成必炸哨兵——命中仍要拿到结论
        monkeypatch.setattr(router_mod, "get_llm_client", _boom)
        second = await analyze_and_route_query(_state(question), config={})

        assert second["router"].type == first["router"].type == "graphrag-query"
        assert second["router"].logic == "llm: primary"
        assert second["router"].decision == "graph"
        assert second["router"].confidence == 0.82
        # 用**当前请求的原文**回填 question；reasoning 不缓存 ⇒ 命中时为 None
        assert second["router"].question == question
        assert second["router"].reasoning is None

    @pytest.mark.asyncio
    async def test_hit_rebuilds_question_from_current_request(self, monkeypatch):
        """同一键（空白差异被归一化）但原文不同 ⇒ 回填的是**这次**的原文。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm")),
        )

        await analyze_and_route_query(_state("今天 天气 怎么样"), config={})
        monkeypatch.setattr(router_mod, "get_llm_client", _boom)
        second = await analyze_and_route_query(_state("  今天   天气  怎么样  "), config={})

        assert second["router"].question == "  今天   天气  怎么样  "


# ===========================================================================
# 4) 缓存不含原文（命题三）
# ===========================================================================


class TestCacheStoresNoOriginalText:
    @pytest.mark.asyncio
    async def test_key_and_value_carry_no_original_text(self, monkeypatch):
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(
                Router(
                    type="kb-query",
                    logic="llm: kb",
                    reasoning="模型叙述：劳动合同里的竞业限制条款有效吗",
                )
            ),
        )
        question = "劳动合同里的竞业限制条款有效吗"

        await analyze_and_route_query(_state(question), config={})

        cache = _cache_mod().get_routing_cache()
        assert len(cache) == 1
        ((key, (_expires_at, value)),) = list(cache.entries.items())
        # 键 = 8 位换代令牌 + sha256 十六进制摘要，问句原文不可能出现在其中
        assert re.fullmatch(r"[0-9a-f]{8}:[0-9a-f]{64}", key), key
        assert question not in key
        assert "竞业限制" not in key
        # 值只四个字段：不存 question（原文）、不存 reasoning（可能复述原文）
        assert set(value) == {"type", "logic", "decision", "confidence"}
        assert "reasoning" not in value and "question" not in value
        assert "竞业限制" not in str(value)

    @pytest.mark.asyncio
    async def test_generation_change_invalidates_previous_entries(self, monkeypatch):
        """换 ``LLM_MODEL``（换代令牌的输入之一）⇒ 同问句不再命中旧条目。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(app_settings, "LLM_MODEL", "model-generation-a")
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: gen-a"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        question = "今天天气怎么样"

        await analyze_and_route_query(_state(question), config={})
        old_generation = _cache_mod().generation_token()

        monkeypatch.setattr(app_settings, "LLM_MODEL", "model-generation-b")
        new_generation = _cache_mod().generation_token()
        assert new_generation != old_generation

        # 换代后必须重新问模型（旧条目按键失配，等价于未命中）
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm: gen-b")),
        )
        second = await analyze_and_route_query(_state(question), config={})

        assert second["router"].logic == "llm: gen-b"
        cache = _cache_mod().get_routing_cache()
        assert len(cache) == 2  # 两个换代令牌各一条（旧条目 TTL/LRU 到期回收）
        assert {key.split(":")[0] for key in cache.entries} == {
            old_generation,
            new_generation,
        }

    @pytest.mark.asyncio
    async def test_prompt_block_change_invalidates_previous_entries(self, monkeypatch):
        """换提示词段（``TASK_EXEC_ENABLED`` 拼入 ``TASK_EXEC_ROUTE_BLOCK`` + 换类）⇒ 换代。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(app_settings, "TASK_EXEC_ENABLED", False)
        monkeypatch.setattr(app_settings, "CODE_EXEC_ENABLED", False)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm: base")),
        )
        question = "今天天气怎么样"

        await analyze_and_route_query(_state(question), config={})
        base_generation = _cache_mod().generation_token()

        monkeypatch.setattr(app_settings, "TASK_EXEC_ENABLED", True)
        assert _cache_mod().generation_token() != base_generation
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: task-on"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        second = await analyze_and_route_query(_state(question), config={})

        assert model.calls == 1, "提示词换代后必须重新分类（不得命中旧条目）"
        assert second["router"].logic == "llm: task-on"


# ===========================================================================
# 5) 缓存本体：有界 LRU / TTL / 归一化 / 副本
# ===========================================================================


class TestCacheUnit:
    def test_normalize_and_digest(self):
        mod = _cache_mod()
        assert mod.normalize_question("  今天\t天气\n怎么样  ") == "今天 天气 怎么样"
        assert mod.normalize_question(None) == ""
        assert mod.question_digest("今天  天气") == mod.question_digest(" 今天 天气 ")
        assert mod.question_digest("今天天气") != mod.question_digest("明天天气")

    def test_put_get_roundtrip_stores_four_fields(self):
        mod = _cache_mod()
        cache = mod.RoutingCache()
        router_obj = Router(
            type="general-query",
            logic="llm: hi",
            question="你好呀",
            decision="d",
            confidence=0.5,
            reasoning="r",
        )

        cache.put("你好呀", router_obj, generation="gen1")

        assert cache.get("你好呀", generation="gen1") == {
            "type": "general-query",
            "logic": "llm: hi",
            "decision": "d",
            "confidence": 0.5,
        }

    def test_get_returns_copy(self):
        mod = _cache_mod()
        cache = mod.RoutingCache()
        cache.put("q", Router(type="kb-query", logic="l"), generation="gen1")

        first = cache.get("q", generation="gen1")
        first["logic"] = "tampered"

        assert cache.get("q", generation="gen1")["logic"] == "l"

    def test_generation_mismatch_is_miss(self):
        mod = _cache_mod()
        cache = mod.RoutingCache()
        cache.put("q", Router(type="kb-query"), generation="gen1")

        assert cache.get("q", generation="gen2") is None

    def test_ttl_expiry(self):
        mod = _cache_mod()
        cache = mod.RoutingCache(ttl_s=0.0)
        cache.put("q", Router(type="kb-query"), generation="gen1")

        assert cache.get("q", generation="gen1") is None
        assert len(cache) == 0, "过期条目应当场摘掉"

    def test_lru_eviction_is_bounded(self):
        mod = _cache_mod()
        cache = mod.RoutingCache(max_entries=2)
        for i in range(3):
            cache.put(f"q{i}", Router(type="kb-query", logic=str(i)), generation="gen1")

        assert len(cache) == 2
        assert cache.get("q0", generation="gen1") is None, "最久未用的应当被淘汰"
        assert cache.get("q1", generation="gen1") is not None
        assert cache.get("q2", generation="gen1") is not None

    def test_hit_refreshes_lru_recency(self):
        mod = _cache_mod()
        cache = mod.RoutingCache(max_entries=2)
        cache.put("q0", Router(type="kb-query"), generation="gen1")
        cache.put("q1", Router(type="kb-query"), generation="gen1")

        cache.get("q0", generation="gen1")  # 命中把 q0 提到最新
        cache.put("q2", Router(type="kb-query"), generation="gen1")

        assert cache.get("q0", generation="gen1") is not None
        assert cache.get("q1", generation="gen1") is None

    def test_reset_clears_singleton(self):
        mod = _cache_mod()
        cache = mod.get_routing_cache()
        cache.put("q", Router(type="kb-query"), generation="gen1")

        mod.reset_routing_cache()

        assert len(cache) == 0


# ===========================================================================
# 换代令牌单点计算（庚-3 卡 β1，台账表 G G6：收口批复用一次）
# ===========================================================================


def _wrap_counting_token(monkeypatch, mod) -> list:
    """把 ``mod.generation_token`` 换成**计数包装**（原样放行真身，逐次记录返回值）。

    包装挂在模块属性上：``router.py`` 两处调用点都是 ``routing_cache.generation_token()``
    的模块属性取法，桩对两侧同时生效——计数才等于"一次请求实际算了几枚令牌"。
    """
    tokens: list = []
    real = mod.generation_token

    def _counting(*args, **kwargs):
        token = real(*args, **kwargs)
        tokens.append(token)
        return token

    monkeypatch.setattr(mod, "generation_token", _counting)
    return tokens


class TestGenerationTokenSingleComputation:
    """B1–B4：一次请求只算一枚换代令牌（查侧算出后传给写侧复用，写侧拿不到才自算）。"""

    @pytest.mark.asyncio
    async def test_b1_miss_path_computes_generation_token_once(self, monkeypatch):
        """B1（RED 主力）：未命中（走 LLM ⇒ 写侧）的一次请求，令牌只算 **1** 次。

        改前现树：查侧 :384 与写侧 :433 各算一次 ⇒ 计数为 2 ⇒ 本用例必红。
        """
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        mod = _cache_mod()
        tokens = _wrap_counting_token(monkeypatch, mod)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm: b1")),
        )

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert result["router"].logic == "llm: b1", "未命中必须照常走 LLM 分类"
        assert len(tokens) == 1, (
            f"一次请求应只算一枚换代令牌（查侧算、写侧复用），实际算了 {len(tokens)} 次"
        )

    @pytest.mark.asyncio
    async def test_b2_hit_path_computes_generation_token_once(self, monkeypatch):
        """B2（防回归钉）：命中路径仍只算 1 枚——复用上提后别把它算成两次或零次。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        mod = _cache_mod()
        question = "今天天气怎么样"
        # 预置与当前同代的条目：预置用真身算令牌，发生在装桩**之前**，不进计数。
        mod.get_routing_cache().put(
            question,
            Router(type="kb-query", logic="llm: preset-b2"),
            generation=mod.generation_token(),
        )
        tokens = _wrap_counting_token(monkeypatch, mod)
        monkeypatch.setattr(router_mod, "get_llm_client", _boom)  # 命中不得构造模型

        result = await analyze_and_route_query(_state(question), config={})

        assert result["router"].logic == "llm: preset-b2"
        assert len(tokens) == 1, f"命中路径应只算一枚令牌，实际 {len(tokens)} 次"

    @pytest.mark.asyncio
    async def test_b3_lookup_token_failure_keeps_degradation_and_store_selfcomputes(
        self, monkeypatch
    ):
        """B3（兜底面）：查侧令牌算不出 ⇒ 既有收口不劣化，且写侧自算兜底仍写成功。

        构造取"第一次调用抛 RuntimeError、其后放行"（简报给出的两种候选之一，
        取舍理由见执行报告：本路径一次请求的令牌调用点最多两处——查侧一枚 +
        写侧兜底一枚，"前两次都抛"会把写侧兜底一并打死，与"兜底仍写成功"的
        预期矛盾）。``cache.get`` 保持真身：查侧在算令牌处抛，降级路径与
        ``test_lookup_exception_degrades_to_miss`` 钉的是同一条早退臂。
        """
        if not _prometheus_available():
            pytest.skip("prometheus_client 不可用，指标断言跳过")
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        mod = _cache_mod()
        recorder = _RecordingLogger()
        monkeypatch.setattr(router_mod, "logger", recorder)
        cache = mod.get_routing_cache()

        calls = {"n": 0}
        real = mod.generation_token

        def _fail_then_pass(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("token generation exploded")
            return real(*args, **kwargs)

        monkeypatch.setattr(mod, "generation_token", _fail_then_pass)
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: survived b3"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        before = _routing_cache_sample("error")

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        # 既有收口逐字不劣化：结果照常、文案照常、result="error" 计数 +1
        assert result["router"].logic == "llm: survived b3", "查侧令牌故障不得影响问答结果"
        assert model.calls == 1
        assert _routing_cache_sample("error") >= before + 1
        assert "routing cache lookup failed" in recorder.text()
        # 写侧兜底：查侧传下 None ⇒ 写侧自算（本路径第二次调用放行）并写成功
        assert calls["n"] == 2, (
            f"查侧一枚 + 写侧兜底一枚，实际令牌调用 {calls['n']} 次"
        )
        assert len(cache) == 1, "写侧自算兜底仍应把成功结果写进缓存"
        ((key, _entry),) = list(cache.entries.items())
        assert key.split(":")[0] == real(), "兜底写入的代前缀必须是当前代"

    @pytest.mark.asyncio
    async def test_b4_stored_generation_prefix_is_the_computed_token(self, monkeypatch):
        """B4（复用同源）：写入条目的代前缀 == 本次请求算出的那枚令牌。

        键形状 ``f"{generation}:{digest}"``（routing_cache.py ``_cache_key``）。
        "真复用了、不是碰巧同值"由 B1 的计数 ==1 与本条的同源断言**联合**钉死：
        全场只算过一枚令牌 ⇒ 写入前缀只可能来自那枚被复用的令牌。
        """
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        mod = _cache_mod()
        tokens = _wrap_counting_token(monkeypatch, mod)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm: b4")),
        )

        await analyze_and_route_query(_state("今天天气怎么样"), config={})

        cache = mod.get_routing_cache()
        assert len(cache) == 1
        ((key, _entry),) = list(cache.entries.items())
        prefix = key.split(":")[0]
        assert tokens, "本次请求至少算出过一枚令牌"
        assert set(tokens) == {prefix}, (
            f"写入条目代前缀 {prefix} 与本次算出的令牌 {tokens} 不同源"
        )


# ===========================================================================
# 档位路由输入并入换代令牌（庚-3 卡 γ，台账表 G G7）
# ===========================================================================


class TestTierRoutingGeneration:
    """C1–C5：档位模型名 / 角色档位表 / 档位 flag 任一改动 ⇒ 令牌换代，且端到端重新分类。

    并入令牌的是档位路由的**输入值**（配置值指纹），不是复刻档位判定——所以
    flag 关（默认态）下改档位值同样换代（C4 钉这条声明的口径）。
    """

    def _generation(self):
        return _cache_mod().generation_token()

    def test_c1_tier_model_change_bumps_generation(self, monkeypatch):
        """C1：档位路由开、router 指到 fast 档，改 ``LLM_MODEL_FAST`` ⇒ 令牌必须换代。

        改前必红：现树令牌只读 ``LLM_MODEL``（第 ③ 段），改档位名令牌不变
        ——台账 G7 行的「该配置下换模型不换代」正是这个形态。
        """
        monkeypatch.setattr(app_settings, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(app_settings, "LLM_ROLE_TIER_MAP", "router=fast")
        monkeypatch.setattr(app_settings, "LLM_MODEL_FAST", "model-tier-a")
        before = self._generation()

        monkeypatch.setattr(app_settings, "LLM_MODEL_FAST", "model-tier-b")
        after = self._generation()

        assert after != before, "档位模型名变了但换代令牌未变：旧模型的缓存条目会继续命中"

    def test_c2_role_tier_map_change_bumps_generation(self, monkeypatch):
        """C2：flag 开，``router=fast`` → ``router=reasoning`` ⇒ 令牌换代。"""
        monkeypatch.setattr(app_settings, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(app_settings, "LLM_MODEL_REASONING", "model-tier-r")
        monkeypatch.setattr(app_settings, "LLM_ROLE_TIER_MAP", "router=fast")
        before = self._generation()

        monkeypatch.setattr(app_settings, "LLM_ROLE_TIER_MAP", "router=reasoning")
        after = self._generation()

        assert after != before, "角色→档位映射变了但换代令牌未变：换档后旧条目会继续命中"

    @pytest.mark.asyncio
    async def test_c3_tier_model_change_forces_reclassification(self, monkeypatch):
        """C3（主症状的**行为面**）：改档位模型值后，第二次同问句请求必须重新问模型。

        改前必红：令牌不含档位值 ⇒ 第二次仍命中旧条目（stub 计数停在 1、缓存
        只有一条）。光断"令牌变了"不算闭合——本条钉的是缓存端到端行为：
        新代写入第二条、两条代前缀不同、日志出口没有新增行。
        """
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(app_settings, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(app_settings, "LLM_ROLE_TIER_MAP", "router=fast")
        monkeypatch.setattr(app_settings, "LLM_MODEL_FAST", "model-tier-a")
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: tier-a"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        recorder = _RecordingLogger()
        monkeypatch.setattr(router_mod, "logger", recorder)
        question = "今天天气怎么样"

        first = await analyze_and_route_query(_state(question), config={})
        assert first["router"].logic == "llm: tier-a"
        assert model.calls == 1
        cache = _cache_mod().get_routing_cache()
        assert len(cache) == 1
        first_key = next(iter(cache.entries))
        first_logs = recorder.templates()

        monkeypatch.setattr(app_settings, "LLM_MODEL_FAST", "model-tier-b")

        second = await analyze_and_route_query(_state(question), config={})

        assert second["router"].logic == "llm: tier-a"
        assert model.calls == 2, "档位模型变了第二次仍命中旧条目：主症状闭合要求重新问模型"
        assert len(cache) == 2, "换代后同问句应写入第二条新代条目（旧代条目由 LRU/TTL 回收）"
        second_key = next(k for k in cache.entries if k != first_key)
        assert first_key.split(":")[0] != second_key.split(":")[0], (
            "两条条目的代前缀相同：换代没有落到键上"
        )
        # 日志出口没增多：第二次请求的模板序列与第一次逐行相等
        assert recorder.templates()[len(first_logs):] == first_logs

    def test_c4_tier_inputs_merged_even_when_flag_off(self, monkeypatch):
        """C4：档位 flag 关（默认态），改 ``LLM_MODEL_STANDARD`` ⇒ 令牌仍换代。

        钉住「并入的是输入指纹、与是否接线无关」这条**声明的口径**，防后来者
        改成条件性并入（只在 flag 开时才并档位值）——那是另一种"换模型不换代"：
        flag 一开的那一瞬间旧档位值的条目还挂着旧代前缀。
        """
        monkeypatch.setattr(app_settings, "LLM_TIER_ROUTING_ENABLED", False)
        monkeypatch.setattr(app_settings, "LLM_MODEL_STANDARD", "model-std-a")
        before = self._generation()

        monkeypatch.setattr(app_settings, "LLM_MODEL_STANDARD", "model-std-b")
        after = self._generation()

        assert after != before, "flag 关时档位输入不并进令牌：条件性并入会重新打开 G7 的漂移窗口"

    def test_c5_tier_keys_cover_gateway_registry(self):
        """C5（同源守卫）：指纹侧档位键名表必须**覆盖** ``model_gateway._TIER_SETTINGS_KEY`` 的全部值。

        允许多列（指纹侧多并一枚键只是保守多换代），禁止少列（网关表新增档键
        没并进令牌 ⇒ 又出现"换模型不换代"，且这次是**静默**的新档漂移）。
        """
        keys = getattr(_cache_mod(), "_TIER_MODEL_SETTINGS_KEYS", None)
        assert keys is not None, (
            "routing_cache 未暴露 _TIER_MODEL_SETTINGS_KEYS——守卫需要具名键表才能与网关表对账"
        )
        from agentix.infrastructure.llm.model_gateway import _TIER_SETTINGS_KEY

        registry_values = set(_TIER_SETTINGS_KEY.values())
        assert registry_values <= set(keys), (
            f"网关档位表存在未并入换代令牌的档键（少列即静默漂移）：{sorted(registry_values - set(keys))}"
        )


# ===========================================================================
# 6) fail-open（约定 2）
# ===========================================================================


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_lookup_exception_degrades_to_miss(self, monkeypatch):
        if not _prometheus_available():
            pytest.skip("prometheus_client 不可用，指标断言跳过")
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        cache = _cache_mod().get_routing_cache()
        recorder = _RecordingLogger()
        monkeypatch.setattr(router_mod, "logger", recorder)

        def _explode(*_args, **_kwargs):
            raise RuntimeError("cache store exploded")

        monkeypatch.setattr(cache, "get", _explode)
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: survived"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        before = _routing_cache_sample("error")

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert result["router"].logic == "llm: survived", "缓存故障不得影响问答结果"
        assert model.calls == 1
        assert _routing_cache_sample("error") >= before + 1
        assert "routing cache lookup failed" in recorder.text()

    @pytest.mark.asyncio
    async def test_store_exception_degrades_silently(self, monkeypatch):
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        cache = _cache_mod().get_routing_cache()
        recorder = _RecordingLogger()
        monkeypatch.setattr(router_mod, "logger", recorder)

        def _explode(*_args, **_kwargs):
            raise RuntimeError("cache put exploded")

        monkeypatch.setattr(cache, "put", _explode)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm: ok")),
        )

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert result["router"].logic == "llm: ok"
        assert "routing cache store failed" in recorder.text()

    @pytest.mark.asyncio
    async def test_rebuild_failure_degrades_to_miss(self, monkeypatch):
        """缓存里的类型在**当前** router 类上校验不过 ⇒ 当作未命中（不是崩溃）。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        mod = _cache_mod()
        mod.get_routing_cache().put(
            "今天天气怎么样",
            Router(type="kb-query", logic="poisoned"),
            generation=mod.generation_token(),
        )
        # 直接污染存储：写入一个当前 schema 不接受的 type
        key = next(iter(mod.get_routing_cache().entries))
        _expires_at, value = mod.get_routing_cache().entries[key]
        value["type"] = "not-a-router-type"
        recorder = _RecordingLogger()
        monkeypatch.setattr(router_mod, "logger", recorder)
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: fresh"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert result["router"].logic == "llm: fresh"
        assert "routing cache entry rebuild failed" in recorder.text()

    @pytest.mark.asyncio
    async def test_fallback_results_are_not_cached(self, monkeypatch):
        """LLM 抛错走兜底 ⇒ **不写缓存**（把一次失败复制给后续同问句是有害的）。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(error=RuntimeError("llm down")),
        )

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert result["router"].type == "kb-query"
        assert len(_cache_mod().get_routing_cache()) == 0

    @pytest.mark.asyncio
    async def test_invalid_type_fallback_is_not_cached(self, monkeypatch):
        """白名单外 type 走兜底 ⇒ **不写缓存**。

        与 ``test_fallback_results_are_not_cached`` 的分工：那条钉「异常出口」
        （LLM 抛错，`router.py` 的 `return {"router": fallback_router}` 一路）；
        本条钉「白名单外出口」（`if not router_type or router_type not in
        allowed_types` 之后的 kb-query 兜底 `return` 一路）。两处兜底共用
        「不写缓存」这条承诺（写缓存只在成功路径上，靠语句顺序保证），
        但走的是 router.py 里两个不同的 `return`，故各钉各的。

        构造越域实例的唯一可行形态：``Router.type`` 是 ``Literal[7 枚]``
        （lg_states.py），``model_validate({"type": "not-a-type", ...})`` 会先抛
        ValidationError（fail-closed 口径，router.py 校验点就近注释），根本走不到
        白名单判据行；所以必须用**跳过校验的实例** ``Router.model_construct(...)``
        ——router.py 的 ``isinstance(raw_response, Router)`` 三目会让它免过校验
        直接进取值。这条理由是本用例"能不能红"的全部依据。
        """
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        # 问句刻意取「今天天气怎么样」：实测不命中 _heuristic_router 的关键词表，
        # 兜底臂因此落到 kb-query 那一支（若命中启发式，钉的就不是这一条出口）。
        out_of_domain = Router.model_construct(
            type="not-a-router-type",
            logic="model: 越域理由必须原文透出",
            question="今天天气怎么样",
        )
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(out_of_domain),
        )

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert result["router"].type == "kb-query"
        # 钉 logic 取源（response.logic → 兜底构造的 `logic or` 前项）：非空时透出的
        # 必须是模型给的理由原文，而不是 "fallback: invalid router output" 兜底文案
        # ——否则"把兜底文案改成伪造的理由"没人管。
        assert result["router"].logic == "model: 越域理由必须原文透出"
        assert len(_cache_mod().get_routing_cache()) == 0


# ===========================================================================
# 7) 可观测性：指标封闭枚举 + 轨迹 cached 标记
# ===========================================================================


class TestObservability:
    def test_result_label_enum_is_closed(self):
        assert metrics_mod.ROUTING_CACHE_RESULT_VALUES == frozenset({"hit", "miss", "error"})

    def test_out_of_enum_label_falls_back_to_other(self):
        """表外取值落 ``other``：指标基数由枚举封死（约定 3）。"""
        if not _prometheus_available():
            pytest.skip("prometheus_client 不可用，指标断言跳过")
        from prometheus_client import REGISTRY

        before = REGISTRY.get_sample_value("routing_cache_total", {"result": "other"}) or 0.0
        metrics_mod.record_routing_cache("please-do-not-become-a-label")

        after = REGISTRY.get_sample_value("routing_cache_total", {"result": "other"}) or 0.0
        assert after >= before + 1

    @pytest.mark.asyncio
    async def test_hit_and_miss_counters(self, monkeypatch):
        if not _prometheus_available():
            pytest.skip("prometheus_client 不可用，指标断言跳过")
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm")),
        )
        miss_before = _routing_cache_sample("miss")
        hit_before = _routing_cache_sample("hit")

        await analyze_and_route_query(_state("今天天气怎么样"), config={})
        await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert _routing_cache_sample("miss") >= miss_before + 1
        assert _routing_cache_sample("hit") >= hit_before + 1

    @pytest.mark.asyncio
    async def test_hit_writes_trajectory_step_with_cached_flag(self, monkeypatch):
        """命中样本可对账：trajectory 里落一步 ``route.cache_hit`` + ``cached=True``。"""
        from agentix.observability.trajectory import (
            TrajectoryRecorder,
            reset_recorder,
            set_recorder,
        )

        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(Router(type="kb-query", logic="llm")),
        )
        question = "今天天气怎么样"
        await analyze_and_route_query(_state(question), config={})  # 先写入缓存

        monkeypatch.setattr(router_mod, "get_llm_client", _boom)
        recorder = TrajectoryRecorder(name="p173")
        token = set_recorder(recorder)
        try:
            await analyze_and_route_query(_state(question), config={})
        finally:
            reset_recorder(token)

        steps = [s for s in recorder.steps if s["name"] == "route.cache_hit"]
        assert len(steps) == 1
        assert steps[0]["attributes"]["cached"] is True
        assert steps[0]["attributes"]["route_type"] == "kb-query"
        assert steps[0]["status"] == "ok"


# ===========================================================================
# 8) fast-path 扩面
# ===========================================================================

#: 新增规则命中表：``(问题, 期望 reason, 期望 logic)``。目标意图名固定
#: ``general-query``（扩面边界：只收助理元问题）。
_FASTPATH_EXPANSION_HITS = (
    (
        "你有哪些功能",
        "capability_meta",
        "fastpath: assistant capability meta (deterministic, LLM classifier skipped)",
    ),
    (
        "你会干什么",
        "capability_meta",
        "fastpath: assistant capability meta (deterministic, LLM classifier skipped)",
    ),
    (
        "你能帮我做什么？",
        "capability_meta",
        "fastpath: assistant capability meta (deterministic, LLM classifier skipped)",
    ),
    (
        "你是AI吗",
        "identity_meta",
        "fastpath: assistant identity meta (deterministic, LLM classifier skipped)",
    ),
    (
        "你是什么模型",
        "identity_meta",
        "fastpath: assistant identity meta (deterministic, LLM classifier skipped)",
    ),
    (
        "我明白了",
        "ack_meta",
        "fastpath: acknowledgement meta (deterministic, LLM classifier skipped)",
    ),
    (
        "谢谢你的解答",
        "ack_meta",
        "fastpath: acknowledgement meta (deterministic, LLM classifier skipped)",
    ),
)

#: 近似但不命中表：贴到规则边缘、但**不该**短路（必须落回 LLM 分类）。
#: 本常量与上方 ``_FASTPATH_EXPANSION_HITS``（应短路集）同 router 侧的 fastpath 判据
#: 函数 ``agentix/application/agents/router.py::_fastpath_router``（其 reason 词表单源为
#: ``FASTPATH_REASONS``）**同源维护**：改 router 的任一 fastpath 规则，须同批改这两份样本；
#: 反之改样本也要回看规则——否则规则与用例各说一套（router 侧的对称登记见 ``_fastpath_router``
#: 函数定义之前的模块级注释，其 docstring 另有一处仅指 ``_FASTPATH_EXPANSION_HITS`` 的既有登记）。
#: 这里刻意用符号名而非行号锚点，避免 router.py 位移把注释钉错位置（防腐规则）。
_FASTPATH_EXPANSION_NEAR_MISSES = (
    # 能力：一旦带上实质动作就不再是元问题
    "你有能力处理这个案子吗",
    "你能帮我做这份合同的风险分析吗",
    # 身份：身份词不在封闭枚举内（这类会引出实质咨询）
    "你是律师吗",
    # 确认 + 追问：多一句就要交给分类器
    "明白了，接下来帮我看下这个条款的风险",
    "知道了，那第264条怎么规定的",
)


class TestFastPathExpansion:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("question, expected_reason, expected_logic", _FASTPATH_EXPANSION_HITS)
    async def test_new_rules_hit_general_query_with_zero_llm(
        self, monkeypatch, question, expected_reason, expected_logic
    ):
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", True)
        monkeypatch.setattr(router_mod, "get_llm_client", _boom)
        saved: list[str] = []
        monkeypatch.setattr(router_mod, "record_llm_calls_saved", saved.append)

        result = await analyze_and_route_query(_state(question), config={})

        assert saved == [expected_reason]
        assert result["router"].type == "general-query"
        assert result["router"].logic == expected_logic
        assert expected_reason in FASTPATH_REASONS

    @pytest.mark.parametrize("question", _FASTPATH_EXPANSION_NEAR_MISSES)
    def test_near_misses_fall_back_to_llm(self, question):
        assert _fastpath_router(question) is None

    def test_expansion_only_targets_general_query(self):
        """扩面边界：新增规则**只落 general-query**，绝不产 kb/graphrag/text2sql。"""
        expanded_reasons = FASTPATH_REASONS - {"chit_chat", "statute_direct"}
        assert expanded_reasons == {"capability_meta", "identity_meta", "ack_meta"}
        for question, reason, _logic in _FASTPATH_EXPANSION_HITS:
            hit = _fastpath_router(question)
            assert hit is not None
            assert hit[0] == reason
            assert hit[1].type == "general-query"

    def test_reason_vocabulary_is_single_sourced(self):
        """``FASTPATH_REASONS`` 是唯一真源：命中语料覆盖到的 reason 与它**相等**。

        新增一条规则却忘了登记 reason（或反过来登记了从不产出的 reason），
        这条用例都会红——这也是"metrics 里的注释只放指针、不另抄名单"的守卫。
        """
        produced = {
            _fastpath_router(question)[0] for question, _reason, _logic in _FASTPATH_EXPANSION_HITS
        }
        for baseline_question in ("你好", "刑法第264条"):
            baseline_hit = _fastpath_router(baseline_question)
            assert baseline_hit is not None
            produced.add(baseline_hit[0])
        assert produced == FASTPATH_REASONS

    def test_length_gate_still_guards_every_new_rule(self):
        """长度闸对新增规则同样生效（不放宽：>60 字符一律不短路）。"""
        for question, _reason, _logic in _FASTPATH_EXPANSION_HITS:
            padded = question + "！" * 60
            assert len(padded) > 60
            assert _fastpath_router(padded) is None


# ===========================================================================
# 边界：缓存不接管 fast-path / 消融分支
# ===========================================================================


class TestCacheDoesNotShadowEarlierBranches:
    @pytest.mark.asyncio
    async def test_ablation_branch_wins_over_cache(self, monkeypatch):
        """消融关路由 ⇒ 固定 kb-query，**不查缓存**（治理层绕过先于缓存）。"""
        monkeypatch.setenv(f"{ablation.ENV_PREFIX}ROUTER_ENABLED", "false")
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(router_mod, "get_llm_client", _boom)
        cache = _cache_mod().get_routing_cache()
        cache.put(
            "今天天气怎么样",
            Router(type="general-query"),
            generation=_cache_mod().generation_token(),
        )

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert result["router"].type == "kb-query"
        assert result["router"].logic == "ablation: router disabled (fixed kb-query)"

    @pytest.mark.asyncio
    async def test_fastpath_hit_does_not_touch_cache_nor_saved_metric(self, monkeypatch):
        """fast-path 命中 ⇒ 直接返回，不查缓存、不写缓存（确定性命中不占缓存）。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", True)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        monkeypatch.setattr(router_mod, "get_llm_client", _boom)
        recorder = _RecordingLogger()
        monkeypatch.setattr(router_mod, "logger", recorder)

        result = await analyze_and_route_query(_state("你好"), config={})

        assert result["router"].type == "general-query"
        assert len(_cache_mod().get_routing_cache()) == 0
        assert "routing cache" not in recorder.text()
        assert "routing fast-path hit" in recorder.text()

    @pytest.mark.asyncio
    async def test_multi_turn_history_neither_reads_nor_writes_cache(self, monkeypatch):
        """历史窗口 >1 条 ⇒ 既不查也不写；末句相同也不得共用结论。

        键只覆盖末句，而分类器输入是 ``[system] + state.messages``：两段历史不同、
        末句相同的请求若共用一条结论，上一轮的 ``logic`` 会被拼进这一轮的 system
        prompt。写侧漏判同样致命——多轮请求写进来的条目会被后面的单轮同问句命中。
        因此本用例**两个方向都断言**：写侧由「多轮不写、缓存为 0」钉住；查侧由
        「单轮写过同问句之后、多轮同末句不得命中」钉住（下面最后一段）。
        """
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "ROUTING_CACHE_ENABLED", True)
        model = _StubStructuredModel(Router(type="graphrag-query", logic="llm: turn1"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        cache = _cache_mod().get_routing_cache()
        question = "刑法第264条为什么这么规定"

        first_history = AgentState(
            messages=[HumanMessage(content="我在研究盗窃罪"), HumanMessage(content=question)]
        )
        await analyze_and_route_query(first_history, config={})
        assert model.calls == 1
        assert len(cache) == 0, "多轮请求不得写缓存：键只覆盖末句，历史不进键"

        # 历史不同、末句相同：必须重新问模型，不得命中上一条结论
        second_history = AgentState(
            messages=[HumanMessage(content="我在研究诈骗罪"), HumanMessage(content=question)]
        )
        await analyze_and_route_query(second_history, config={})
        assert model.calls == 2, "历史不同、末句相同不得共用缓存结论"
        assert len(cache) == 0

        # 单轮同一问句仍照常写 + 命中（缓存只服务单轮）
        await analyze_and_route_query(_state(question), config={})
        assert model.calls == 3
        assert len(cache) == 1, "单轮请求仍应写入缓存"
        monkeypatch.setattr(router_mod, "get_llm_client", _boom)
        hit = await analyze_and_route_query(_state(question), config={})
        assert hit["router"].type == "graphrag-query"
        assert model.calls == 3, "单轮命中必须跳过模型"

        # 查侧判据：单轮已写入同问句条目，多轮同末句请求仍不得命中它
        # （缓存此刻非空——查侧判据若缺失，这次会直接回放缓存、模型零调用）
        model.calls = 0
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        same_tail_history = AgentState(
            messages=[HumanMessage(content="我在研究盗窃罪"), HumanMessage(content=question)]
        )
        await analyze_and_route_query(same_tail_history, config={})
        assert model.calls == 1, "多轮同末句不得命中单轮写入的缓存"
        assert len(cache) == 1, "多轮请求仍不得写缓存"
