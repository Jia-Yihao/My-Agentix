"""E4 · embedding 缓存启动预热的守卫（路线图 `docs/design/性能与优化路线图.md` §2 E4 的剩余半段）。

验收口径（三条，逐条对应用户级纪律）
------------------------------------
1. **默认关**（AGENTS.md 纪律 1）：`EMBEDDING_CACHE_PREWARM_ENABLED=False` 时
   `run_prewarm_from_settings()` 必须**一条网络请求都不发、一个 embeddings 实例都不构造**——
   "等价"不是靠措辞声明，而是把 `create_embeddings` 换成"一被调用就 raise"的桩来证伪。
2. **辅助子系统 fail-open**（纪律 2）：预热失败只留 `logger.warning` 与报告里的 `error` 字段，
   绝不把异常递给启动路径——预热的作用是让第一次查询更快，它自己没有"必须成功"的资格。
3. **计数面不新造 label**（纪律 3）：hit/miss 计数复用 `CachedEmbeddings.embed_documents`
   里既有的 `record_cache_hit/miss("embedding")` 接线；本模块**不许**出现新的 `record_cache_*`
   调用（新 label 必须先进 `test_p100` 的 `ALLOWED_METRIC_LABELS`，本批刻意不走那条路）。

覆盖面：纯函数解析 / 报告形态 / 冷缓存与热缓存两种真实结局 / 三种跳过原因 /
新键默认值与自校验 / 启动装配与退出取消 / 文档同步面。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from agentix.retrieval.infra.embedding.cache import CachedEmbeddings, EmbeddingCache
from agentix.retrieval.infra.embedding.prewarm import (
    DEFAULT_PREWARM_MAX,
    parse_prewarm_queries,
    prewarm_embeddings,
    run_prewarm_from_settings,
)
from agentix.retrieval.settings import settings as retrieval_settings

REPO = Path(__file__).resolve().parent.parent
PREWARM_PY = REPO / "agentix" / "retrieval" / "infra" / "embedding" / "prewarm.py"
MAIN_PY = REPO / "agentix" / "main.py"

_PREWARM_KEYS = (
    "EMBEDDING_CACHE_PREWARM_ENABLED",
    "EMBEDDING_CACHE_PREWARM_QUERIES",
    "EMBEDDING_CACHE_PREWARM_MAX",
)


class _FakeEmbeddings:
    """记录调用次数与批次的假后端：预热"有没有真的打网络"只能这样观测。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._fail = fail

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        if self._fail:
            raise RuntimeError("embedding endpoint down")
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]

    @property
    def embedded_batches(self) -> int:
        return len(self.calls)

    @property
    def embedded_texts(self) -> int:
        return sum(len(batch) for batch in self.calls)


def _cached(
    tmp_path, model: str = "fake-model"
) -> tuple[CachedEmbeddings, _FakeEmbeddings, EmbeddingCache]:
    inner = _FakeEmbeddings()
    cache = EmbeddingCache(str(tmp_path / "embedding_cache.db"))
    return CachedEmbeddings(inner, cache, model=model), inner, cache


# --------------------------------------------------------------------------
# 1. 纯函数：清单解析
# --------------------------------------------------------------------------


def test_parse_prewarm_queries_splits_strips_and_dedupes_in_order() -> None:
    """分隔符只认 `;`／`；`／换行——半角逗号在英文问句内部就会出现，用它切会把一条问法切成两半。"""
    raw = "  民法典第188条 ; 诉讼时效是几年;;  民法典第188条 ；合同编通则解释第X条\n重复的一条\n重复的一条"
    got = parse_prewarm_queries(raw, limit=10)
    assert got == [
        "民法典第188条",
        "诉讼时效是几年",
        "合同编通则解释第X条",
        "重复的一条",
    ], got
    # 去重必须保序：清单顺序就是运维读到的顺序，排完序没人能对表
    assert got[0] == "民法典第188条"


def test_parse_prewarm_queries_never_splits_on_a_comma() -> None:
    """反例钉子：一条含逗号的问法必须原样保留（这条断言的就是"分隔符集合"本身）。"""
    assert parse_prewarm_queries("因不可抗力不能履行合同的, 部分或者全部免除责任?", limit=5) == [
        "因不可抗力不能履行合同的, 部分或者全部免除责任?"
    ]


def test_parse_prewarm_queries_applies_the_limit_after_dedupe() -> None:
    """上限截断在去重**之后**：重复项不许吃掉配额。

    取样刻意做成有鉴别力的：`"a;a;b;c"` + limit=2 在"先去重再截断"下是 `["a","b"]`
    （2 条真问法），在"先截断再去重"下只剩 `["a"]`（1 条）——差一条，红得出来。
    """
    got = parse_prewarm_queries("a;a;b;c", limit=2)
    assert got == ["a", "b"], got
    # 另一侧：重复项不得占满配额（这条抓的是"去重"本身，不是顺序）
    assert parse_prewarm_queries(";".join(["a", "b", "a", "c", "d"]), limit=3) == ["a", "b", "c"]


def test_parse_prewarm_queries_treats_non_positive_limit_as_empty() -> None:
    """`MAX<=0` 是非法配置（自校验会报 error），但解析器不靠那条活着：一律给空清单。"""
    assert parse_prewarm_queries("a;b", limit=0) == []
    assert parse_prewarm_queries("a;b", limit=-1) == []


def test_parse_prewarm_queries_defaults_to_the_documented_cap() -> None:
    assert (
        inspect.signature(parse_prewarm_queries).parameters["limit"].default == DEFAULT_PREWARM_MAX
    )


# --------------------------------------------------------------------------
# 2. 真缓存上的两种结局
# --------------------------------------------------------------------------


def test_cold_cache_prewarm_embeds_once_and_persists(tmp_path) -> None:
    """冷启动唯一该有的形态：一次批量请求（不是 N 次）、结果落进 sqlite。"""
    embeddings, inner, cache = _cached(tmp_path)
    report = prewarm_embeddings(embeddings, ["问一", "问二", "问三"])
    assert (
        inner.embedded_batches == 1
    ), "预热必须合并成一次批量调用，逐条打端点等于把启动时间乘以条数"
    assert inner.embedded_texts == 3
    assert report == {
        "requested": 3,
        "cached": 0,
        "embedded": 3,
        "elapsed_ms": pytest.approx(report["elapsed_ms"], abs=60_000),
        "error": None,
    }
    assert cache.count() == 3, report


def test_warm_cache_prewarm_touches_the_backend_zero_times(tmp_path) -> None:
    """第二次预热的全部价值就在这条：命中缓存 ⇒ 一次网络都不发。"""
    embeddings, inner, cache = _cached(tmp_path)
    prewarm_embeddings(embeddings, ["问一", "问二"])
    assert inner.embedded_batches == 1
    report = prewarm_embeddings(embeddings, ["问一", "问二"])
    assert inner.embedded_batches == 1, report
    assert report["cached"] == 2 and report["embedded"] == 0 and report["error"] is None
    assert cache.count() == 2


def test_prewarm_report_counts_duplicate_texts_against_the_backend(tmp_path) -> None:
    """清单里重复的文本由缓存层去重 ⇒ 后端只该收到一条。"""
    embeddings, inner, _ = _cached(tmp_path)
    report = prewarm_embeddings(embeddings, ["同一条", "同一条"])
    assert report["requested"] == 2
    assert inner.embedded_texts == 1, "批内去重是缓存层的既有职责，预热不许绕过它自己再拼一份"


def test_different_model_keys_do_not_share_vectors(tmp_path) -> None:
    """换嵌入模型即整体失效——预热不许成为这条规则的新例外。"""
    inner = _FakeEmbeddings()
    cache = EmbeddingCache(str(tmp_path / "embedding_cache.db"))
    a = CachedEmbeddings(inner, cache, model="model-a")
    b = CachedEmbeddings(inner, cache, model="model-b")
    prewarm_embeddings(a, ["同一句话"])
    report = prewarm_embeddings(b, ["同一句话"])
    assert report["cached"] == 0 and report["embedded"] == 1, report


# --------------------------------------------------------------------------
# 3. fail-open（纪律 2）
# --------------------------------------------------------------------------


def test_backend_failure_is_reported_not_raised(tmp_path) -> None:
    """端点挂了只能变成报告里的 `error` + 一条 warning，不能把异常递给启动路径。"""
    inner = _FakeEmbeddings(fail=True)
    cache = EmbeddingCache(str(tmp_path / "embedding_cache.db"))
    embeddings = CachedEmbeddings(inner, cache, model="fake-model")
    report = prewarm_embeddings(embeddings, ["问一"])
    assert report["error"], report
    assert "embedding endpoint down" in report["error"]
    assert cache.count() == 0, "失败的一批不该留下半成品条目"


def test_empty_manifest_is_a_clean_no_op(tmp_path) -> None:
    """空清单：不发请求、报告自证 `requested=0`。"""
    embeddings, inner, _ = _cached(tmp_path)
    report = prewarm_embeddings(embeddings, [])
    assert inner.embedded_batches == 0
    assert report["requested"] == 0 and report["error"] is None


# --------------------------------------------------------------------------
# 4. 三种跳过原因 + 默认关的等价守卫
# --------------------------------------------------------------------------


def _patch(monkeypatch, **values) -> None:
    for key, value in values.items():
        monkeypatch.setattr(retrieval_settings, key, value, raising=False)


def _boom_on_construction(monkeypatch) -> None:
    """把构造入口换成"一调用就红"的桩：这就是 flag-off 逐位等价的证伪装置。"""

    def _raise(*_a, **_k):
        raise AssertionError("预热被跳过时不许构造 embeddings 实例")

    monkeypatch.setattr("agentix.retrieval.infra.embedding.client.create_embeddings", _raise)


def test_flag_off_constructs_nothing_and_says_why(monkeypatch) -> None:
    """纪律 1 的正身：开关关 ⇒ 连实例都不构造（不是"构造了但不调用"）。"""
    _patch(
        monkeypatch,
        EMBEDDING_CACHE_PREWARM_ENABLED=False,
        EMBEDDING_CACHE_PREWARM_QUERIES="问一;问二",
        EMBEDDING_CACHE_PREWARM_MAX=DEFAULT_PREWARM_MAX,
    )
    _boom_on_construction(monkeypatch)
    report = run_prewarm_from_settings()
    assert report["skipped"] == "disabled", report
    assert report["requested"] == 0 and report["error"] is None


def test_cache_disabled_skips_because_warming_nothing_would_only_cost_money(monkeypatch) -> None:
    """`EMBEDDING_CACHE_ENABLED=false` 时预热打出的向量无处可存 ⇒ 白烧一次端点调用，必须跳过。"""
    _patch(
        monkeypatch,
        EMBEDDING_CACHE_PREWARM_ENABLED=True,
        EMBEDDING_CACHE_ENABLED=False,
        EMBEDDING_CACHE_PREWARM_QUERIES="问一",
        EMBEDDING_CACHE_PREWARM_MAX=DEFAULT_PREWARM_MAX,
    )
    _boom_on_construction(monkeypatch)
    report = run_prewarm_from_settings()
    assert report["skipped"] == "cache_disabled", report


def test_empty_query_list_skips_even_with_the_flag_on(monkeypatch) -> None:
    """开关开着但清单为空是**最常见的误配**（先开 flag 再填清单）⇒ 必须响亮说明而不是静默 0 条。"""
    _patch(
        monkeypatch,
        EMBEDDING_CACHE_PREWARM_ENABLED=True,
        EMBEDDING_CACHE_ENABLED=True,
        EMBEDDING_CACHE_PREWARM_QUERIES="   ;  \n ",
        EMBEDDING_CACHE_PREWARM_MAX=DEFAULT_PREWARM_MAX,
    )
    _boom_on_construction(monkeypatch)
    report = run_prewarm_from_settings()
    assert report["skipped"] == "empty_list", report


def test_run_prewarm_swallows_construction_failures(monkeypatch) -> None:
    """构造阶段就炸（缺 `EMBEDDING_API_KEY` 这类）也只留 warning：启动不许被预热打断。"""

    def _raise(*_a, **_k):
        raise RuntimeError("no api key")

    _patch(
        monkeypatch,
        EMBEDDING_CACHE_PREWARM_ENABLED=True,
        EMBEDDING_CACHE_ENABLED=True,
        EMBEDDING_CACHE_PREWARM_QUERIES="问一",
        EMBEDDING_CACHE_PREWARM_MAX=DEFAULT_PREWARM_MAX,
    )
    monkeypatch.setattr("agentix.retrieval.infra.embedding.client.create_embeddings", _raise)
    report = run_prewarm_from_settings()
    assert report["error"] and "no api key" in report["error"], report
    assert (
        report["requested"] == 1
    ), "报告仍要说清原本要刷几条，否则运维分不清「没配」和「配了但炸了」"


# --------------------------------------------------------------------------
# 5. 新键：默认值、自校验、五处同步面
# --------------------------------------------------------------------------


def test_new_prewarm_keys_exist_and_default_off() -> None:
    """纪律 1：新增 flag 一律 `False`/`0`/空串起步（观测类"默认开"的例外不适用于会打网络的预热）。"""
    source = (REPO / "agentix" / "retrieval" / "settings.py").read_text(encoding="utf-8")
    for key in _PREWARM_KEYS:
        assert hasattr(retrieval_settings, key), f"{key} 未在检索域配置层定义"
    assert retrieval_settings.EMBEDDING_CACHE_PREWARM_ENABLED is False
    assert retrieval_settings.EMBEDDING_CACHE_PREWARM_QUERIES == ""
    assert retrieval_settings.EMBEDDING_CACHE_PREWARM_MAX == DEFAULT_PREWARM_MAX
    # 默认值必须就写在源码里，而不是只写在类上（读代码的人据此判断出厂形态）
    assert '_bool_env("EMBEDDING_CACHE_PREWARM_ENABLED", "false")' in source
    assert '_bool_env("EMBEDDING_CACHE_PREWARM_ENABLED", "true")' not in source


def test_selfcheck_rejects_a_non_positive_prewarm_max(monkeypatch) -> None:
    """`MAX=0` 与"关掉"不是一回事：前者是配置错误，必须被启动自检报出来。"""
    from agentix.retrieval.settings import validate_app_settings

    _patch(monkeypatch, EMBEDDING_CACHE_PREWARM_MAX=0)
    errors, _warnings = validate_app_settings()
    assert any("EMBEDDING_CACHE_PREWARM_MAX" in e for e in errors), errors


def test_env_example_declares_the_three_keys() -> None:
    """`.env.example` 是部署侧唯一的清单；注释行也算（与 `RETRIEVAL_HYBRID_ENABLED` 同口径）。"""
    text = (REPO / ".env.example").read_text(encoding="utf-8-sig", errors="replace")
    for key in _PREWARM_KEYS:
        assert key in text, f"{key} 未写入 .env.example"


def test_the_keys_live_on_exactly_one_settings_surface() -> None:
    """键只许存在于**一个**配置面：它们属于 `agentix/retrieval/settings.py` 的 `EMBEDDING_CACHE_*` 家族。

    统一配置层（`agentix/config/settings.py`，pydantic）再放一份同名键，就是"同一个开关两个源"
    （纪律 6 ④），而且会当场红在 p41 的双向守卫上——那张手写清单只覆盖 pydantic 字段，
    所以这条钉子负责把"为什么不放那边"变成有主的事实，而不是每个批次重新解释一次。
    """
    unified = (REPO / "agentix" / "config" / "settings.py").read_text(encoding="utf-8")
    for key in _PREWARM_KEYS:
        assert key not in unified, f"{key} 同时出现在统一配置层 ⇒ 两个源"
        assert key in (REPO / "agentix" / "retrieval" / "settings.py").read_text(encoding="utf-8")
    # 读取端也必须同源：预热只经检索域 settings 取值
    assert "from agentix.retrieval.settings import settings" in PREWARM_PY.read_text(
        encoding="utf-8"
    )


def test_prewarm_reuses_the_existing_hit_miss_counter_and_adds_no_new_label() -> None:
    """纪律 3：新 label 必须先进 p100 白名单。本批刻意不新增指标——计数面早已在缓存层。"""
    source = PREWARM_PY.read_text(encoding="utf-8")
    assert "record_cache" not in source, (
        "预热自己不该再打一份 hit/miss 计数：`CachedEmbeddings.embed_documents` 已经打过，"
        "再打一次就是同一个事实两个源（纪律 6 ④）；要新增指标请先把它登记进 ALLOWED_METRIC_LABELS"
    )


# --------------------------------------------------------------------------
# 6. 启动装配（**必须用 AST，不许用子串**）
#
# 这三条初版是文本子串守卫，变异实测当场证明它们**不承重**（2026-10-26 复测，
# 改判据前 / 后各跑一轮同一套五段变异，`.tmp/p127_mutation.py`）：
# ① 把 `if _retrieval_settings.EMBEDDING_CACHE_PREWARM_ENABLED:` 改成 `if True:`
#    ⇒ 改前 25 条全绿；改后 1 failed（本组第 1 条）。原因：键名还留在上一行注释里，
#    `in` 与 `index()` 两个判据都由**散文**满足，而注释不是控制流。
# ② 把 `_shutdown` 里的 `_embedding_prewarm_task.cancel()` 换成 `pass`
#    ⇒ 改前 25 条全绿；改后 1 failed（本组第 3 条）。原因：同一个函数里滞后采样那段
#    也有 `.cancel()`，子串判据分不清"取消的是谁"。
# 这正是纪律 6 ④ 说的那件事：守卫的判据必须与它声称守护的**结构**同源，
# 而不是与"文档里提到了这个词"同源。
# --------------------------------------------------------------------------


def _main_tree() -> ast.Module:
    return ast.parse(MAIN_PY.read_text(encoding="utf-8"))


def _async_fn(tree, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"main.py 里找不到 async def {name}()")


def _refs(node, name: str) -> bool:
    """节点子树里是否出现名为 ``name`` 的属性访问或标识符。

    写成兼容两种形态是有原因的：同一个函数既可以叫 ``asyncio.to_thread``（属性），
    也可以先 ``from asyncio import to_thread`` 再裸叫（名字）。只认其中一种的守卫，
    会在一趟无害的重排里变成假阳性——那和装饰性守卫一样糟。
    """
    return any(
        (isinstance(c, ast.Attribute) and c.attr == name)
        or (isinstance(c, ast.Name) and c.id == name)
        for c in ast.walk(node)
    )


def _refs_prewarm_callable(node, name: str) -> bool:
    """`run_prewarm_from_settings` 可以是裸 `Name`，也可以被别名接到 `Attribute` 上——
    但**不许**匹配任何同名局部变量，所以下放判据只认调用位上的这个引用。"""
    if isinstance(node, ast.Name) and node.id == name:
        return True
    return isinstance(node, ast.Attribute) and node.attr == name


def _assign_targets(stmts, name: str) -> list[ast.Assign]:
    found: list[ast.Assign] = []
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                found.append(node)
    return found


def test_startup_creates_the_prewarm_task_inside_the_flag_branch() -> None:
    """任务创建必须落在**判这个 flag 的那个 `if` 的分支体内**——散文里提到键名不算门控。"""
    tree = _main_tree()
    startup = _async_fn(tree, "_startup")
    gate = next(
        (
            node
            for node in ast.walk(startup)
            if isinstance(node, ast.If) and _refs(node.test, "EMBEDDING_CACHE_PREWARM_ENABLED")
        ),
        None,
    )
    assert gate is not None, "_startup 里没有以 EMBEDDING_CACHE_PREWARM_ENABLED 为条件的分支"
    inside = _assign_targets(gate.body, "_embedding_prewarm_task")
    assert inside, "flag 分支体内没有创建预热任务（门控成了摆设）"
    assert any(_refs(call, "create_task") for stmt in inside for call in ast.walk(stmt)), inside
    # 反向：这个赋值**只能**出现在 flag 分支里。分支外再放一份 = 出厂默认就打端点。
    everywhere = _assign_targets([startup], "_embedding_prewarm_task")
    inside_ids = {id(a) for a in inside}
    stray = [a for a in everywhere if id(a) not in inside_ids]
    assert not stray, f"_startup 里有 {len(stray)} 处不受 flag 控制的任务赋值"


def test_module_level_task_handle_exists_and_starts_as_none() -> None:
    """句柄必须是模块级且初值为 `None`：asyncio 只持弱引用，不存取的 task 可能被 GC。"""
    tree = _main_tree()
    decls = [
        node
        for node in tree.body  # 只看模块顶层，函数内的同名赋值不算
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(t, ast.Name) and t.id == "_embedding_prewarm_task"
            for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        )
    ]
    assert len(decls) == 1, decls
    value = decls[0].value
    assert isinstance(value, ast.Constant) and value.value is None, "初值必须是 None（未派发态）"


def test_shutdown_cancels_the_prewarm_task_itself() -> None:
    """取消的**必须是预热那个句柄**：`.cancel()` 出现在同函数的别处（滞后采样）不算数。"""
    tree = _main_tree()
    shutdown = _async_fn(tree, "_shutdown")
    cancels = [
        node
        for node in ast.walk(shutdown)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cancel"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_embedding_prewarm_task"
    ]
    assert cancels, "_shutdown 里没有对 _embedding_prewarm_task 调用 .cancel()"


def test_prewarm_runs_off_the_event_loop() -> None:
    """同步 sqlite + 同步 http 调用绝不许在事件循环里跑（AGENTS.md 代码风格第三条）。"""
    tree = _main_tree()
    worker = _async_fn(tree, "_embedding_prewarm_worker")
    offloaded = [
        node
        for node in ast.walk(worker)
        if isinstance(node, ast.Call)
        and _refs(node.func, "to_thread")
        and any(_refs_prewarm_callable(arg, "run_prewarm_from_settings") for arg in node.args)
    ]
    assert offloaded, "预热本体没有经 asyncio.to_thread 下放"


# --------------------------------------------------------------------------
# 7. 文档面：别让"未做"这句话活到已做之后
# --------------------------------------------------------------------------


def test_roadmap_no_longer_declares_prewarm_missing() -> None:
    text = (REPO / "docs" / "design" / "性能与优化路线图.md").read_text(encoding="utf-8")
    assert "启动预热仍未做" not in text, "路线图的状态列还在说 E4 未落地"
    assert "EMBEDDING_CACHE_PREWARM_ENABLED" in text


def test_project_spec_declares_the_prewarm_keys() -> None:
    text = (REPO / "docs" / "项目实现手册.md").read_text(encoding="utf-8")
    for key in _PREWARM_KEYS:
        assert key in text, f"{key} 未登记进 PROJECT_SPEC"
