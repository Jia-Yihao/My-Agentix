"""p126 · schema 缓存的**主动失效**入口（`docs/design/性能与优化路线图.md` E2）。

四条口径，每条都对应 E2 原文里的一句验收：

1. **「改表 → 主动清 → 立即可见」**：失效之后下一次取用必须读到新 schema，而不是继续吃
   TTL 里的旧值（`test_next_read_sees_the_new_schema_after_invalidation`）。
2. **默认姿态不许变**（纪律 1）：`TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS` 出厂 0 = 根本不缓存，
   此时失效入口必须返回"清了 0 条"且不产生任何行为差异
   （`test_ttl_off_*` 两条）。反过来，**主动失效不许被 TTL 开关吞掉**：Neo4j 的周期刷新
   默认也是关的，若失效只在"周期刷新开着"时才生效，默认部署里这个入口就是装饰品
   （`test_invalidate_works_even_when_periodic_refresh_is_off`）。
3. **只清该清的**：text2sql 的缓存键里带连接串，失效必须能按库收窄，不许一把清空所有库
   （`test_invalidate_clears_only_the_named_database`）——多租户/多目标库下"清全局"会把
   别人的热缓存一起打掉，那是可被利用的延迟放大。
4. **运维面 fail-open**（纪律 2）：清缓存是辅助设施，任何一侧报错都不得把 `/ops` 请求打成
   500，也不得让主链路受影响（`test_the_umbrella_service_degrades_instead_of_raising` +
   `test_ops_endpoint_still_answers_200_shape_when_everything_fails`）。
"""

from __future__ import annotations

import time

import pytest

import agentix.application.agents.text2sql  # noqa: F401,E402 - 先入打破既有循环导入（同 test_p85 口径）

from agentix.application.agents.kg_sub_graph import kg_neo4j_conn
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql import (
    schema_retrieval as schema_pkg,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.schema_retrieval import (
    node as schema_node,
)
from agentix.application.services import schema_cache
from agentix.config.settings import settings


@pytest.fixture(autouse=True)
def _isolate_caches():
    """两个缓存面都是进程级全局 ⇒ 用例进出各清一次，否则条数断言互相污染。"""
    schema_node._SCHEMA_CACHE.clear()
    kg_neo4j_conn.reset_neo4j_graph()
    yield
    schema_node._SCHEMA_CACHE.clear()
    kg_neo4j_conn.reset_neo4j_graph()


# ---------------------------------------------------------------------------
# text2sql schema 缓存
# ---------------------------------------------------------------------------


def _fake_reader(monkeypatch, values):
    """把 `_retrieve_schema_from_mysql` 换成"每次返回下一个值"，并记录调用次数。"""
    calls: list[int] = []

    async def _retrieve(question: str) -> dict:
        calls.append(len(calls))
        return {"tables": [{"table_name": values[len(calls) - 1]}]}

    monkeypatch.setattr(schema_node, "_retrieve_schema_from_mysql", _retrieve)
    monkeypatch.setattr(schema_node, "text2sql_database_url", lambda: "mysql://db-a")
    return calls


@pytest.mark.asyncio
async def test_next_read_sees_the_new_schema_after_invalidation(monkeypatch) -> None:
    """E2 验收原话：改表 → 主动清 → **立即可见**（而不是等 TTL 到期）。"""
    monkeypatch.setattr(settings, "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS", 600)
    calls = _fake_reader(monkeypatch, ["v1", "v2"])
    node = schema_node.create_schema_retrieval_node()
    state = {"question": "查 articles 表"}

    first = await node(state)
    second = await node(state)
    assert first == second, "TTL 开着时第二次必须吃到缓存"
    assert len(calls) == 1, "只该真读一次库"

    cleared = schema_node.invalidate_schema_cache()
    assert cleared == 1, "失效要回报真清了几条（运维要能核对'我清到了'）"

    third = await node(state)
    assert len(calls) == 2, "失效之后必须重新读库"
    assert third["schema_context"] != first["schema_context"], "读到的是新 schema，不是旧的"


def test_invalidate_clears_only_the_named_database() -> None:
    """按连接串收窄：别的库的条目不许被顺手清空。"""
    schema_node._SCHEMA_CACHE.update(
        {
            "mysql://db-a|q1": (time.time() + 600, {"tables": []}),
            "mysql://db-a|q2": (time.time() + 600, {"tables": []}),
            "mysql://db-b|q1": (time.time() + 600, {"tables": []}),
        }
    )
    assert schema_node.invalidate_schema_cache("mysql://db-a") == 2
    assert list(schema_node._SCHEMA_CACHE) == ["mysql://db-b|q1"], "db-b 的条目必须原样留着"
    assert schema_node.invalidate_schema_cache() == 1, "不带参数 = 清全局"
    assert schema_node._SCHEMA_CACHE == {}


def test_invalidate_with_an_unknown_url_is_a_no_op() -> None:
    """清不到不报错——但也不能谎报清了多少。"""
    schema_node._SCHEMA_CACHE["mysql://db-a|q1"] = (time.time() + 600, {"tables": []})
    assert schema_node.invalidate_schema_cache("mysql://db-zzz") == 0
    assert len(schema_node._SCHEMA_CACHE) == 1, "没命中就一条都不许动"


@pytest.mark.asyncio
async def test_ttl_off_caches_nothing_so_invalidation_reports_zero(monkeypatch) -> None:
    """出厂 TTL=0：每次现查 ⇒ 失效入口返回 0，且行为与它不存在时逐位相同（纪律 1）。"""
    monkeypatch.setattr(settings, "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS", 0)
    calls = _fake_reader(monkeypatch, ["v1", "v2", "v3"])
    node = schema_node.create_schema_retrieval_node()

    await node({"question": "q"})
    await node({"question": "q"})
    assert len(calls) == 2, "TTL=0 就不该有缓存命中"
    assert schema_node.invalidate_schema_cache() == 0
    assert schema_node.schema_cache_size() == 0


# ---------------------------------------------------------------------------
# Neo4j schema 周期刷新
# ---------------------------------------------------------------------------


class _FakeGraph:
    def __init__(self) -> None:
        self.refreshes = 0

    def refresh_schema(self) -> None:
        self.refreshes += 1


def test_invalidate_neo4j_schema_forces_the_next_get_to_refresh(monkeypatch) -> None:
    """刚刷过（TTL 远未到）也要能靠主动失效立刻重刷。"""
    monkeypatch.setattr(settings, "NEO4J_SCHEMA_REFRESH_SECONDS", 300.0)
    graph = _FakeGraph()
    kg_neo4j_conn._graph_cache = graph
    kg_neo4j_conn._schema_refreshed_at = time.monotonic()

    assert kg_neo4j_conn.get_neo4j_graph() is graph
    assert graph.refreshes == 0, "TTL 未到不该重刷"

    assert kg_neo4j_conn.invalidate_neo4j_schema() is True
    assert kg_neo4j_conn.get_neo4j_graph() is graph
    assert graph.refreshes == 1, "失效后第一次取用就重刷（连接复用，不重建）"
    assert kg_neo4j_conn.get_neo4j_graph() is graph
    assert graph.refreshes == 1, "标记必须被消费掉，不能每次取用都刷一遍"


def test_invalidate_works_even_when_periodic_refresh_is_off(monkeypatch) -> None:
    """周期刷新默认关（TTL=0）时主动失效**照样生效**——否则默认部署里这入口是装饰品。"""
    monkeypatch.setattr(settings, "NEO4J_SCHEMA_REFRESH_SECONDS", 0.0)
    graph = _FakeGraph()
    kg_neo4j_conn._graph_cache = graph
    kg_neo4j_conn._schema_refreshed_at = time.monotonic()

    assert kg_neo4j_conn.invalidate_neo4j_schema() is True
    kg_neo4j_conn.get_neo4j_graph()
    assert graph.refreshes == 1


def test_invalidate_neo4j_schema_without_a_graph_is_quiet_and_honest() -> None:
    """没有实例可刷 ⇒ 返回 False（不谎报 True），也不建连、不抛。"""
    assert kg_neo4j_conn.invalidate_neo4j_schema() is False
    assert kg_neo4j_conn._graph_cache is None, "失效不许顺手建连"


# ---------------------------------------------------------------------------
# 统一入口与运维面
# ---------------------------------------------------------------------------


def test_the_umbrella_service_reports_both_sides() -> None:
    """一个入口服务两套缓存（E2 原文：失效入口同时服务 text2sql + Neo4j）。"""
    schema_node._SCHEMA_CACHE["mysql://db-a|q1"] = (time.time() + 600, {"tables": []})
    graph = _FakeGraph()
    kg_neo4j_conn._graph_cache = graph
    kg_neo4j_conn._schema_refreshed_at = time.monotonic()

    report = schema_cache.invalidate_schema_caches()
    assert report["text2sql_schema_cleared"] == 1
    assert report["text2sql_schema_cache_remaining"] == 0, "清完这一侧必须真的是空的"
    assert report["neo4j_schema_marked_stale"] is True
    assert graph.refreshes == 0, "入口只打标记，重刷发生在下一次取用（不在请求路径上同步刷）"


def test_the_umbrella_service_degrades_instead_of_raising(monkeypatch) -> None:
    """纪律 2：任何一侧炸了都不能把清缓存这件事变成 500；错误如实进报告。"""

    def _boom(*_a, **_kw):
        raise RuntimeError("neo4j 侧不可用")

    monkeypatch.setattr(kg_neo4j_conn, "invalidate_neo4j_schema", _boom)
    report = schema_cache.invalidate_schema_caches()
    assert report["neo4j_schema_marked_stale"] is None, "失败不能报成 True"
    assert "neo4j" in report["errors"] and "不可用" in report["errors"]["neo4j"]
    assert report["text2sql_schema_cleared"] == 0, "另一侧照常执行完"


def test_the_package_facade_reexports_the_same_callables() -> None:
    """门面与实现必须是同一个对象：两套名字各留一份实现，失效入口就会挑错那一套。"""
    assert schema_pkg.invalidate_schema_cache is schema_node.invalidate_schema_cache
    assert schema_pkg.schema_cache_size is schema_node.schema_cache_size


def test_the_failed_side_reports_none_for_every_one_of_its_fields(monkeypatch) -> None:
    """同一侧的字段要么全是真数、要么全是 None——不许剩下半个看起来像真数的值。

    这一条盯的是 `remaining` 那个新字段：如果失败分支只把 `cleared` 置 None 而 `remaining`
    仍去读缓存，运维报告会出现"清未知但还剩 3 条"这种自相矛盾的读法。
    """

    def _boom(*_a, **_kw):
        raise RuntimeError("text2sql 侧不可用")

    monkeypatch.setattr(schema_pkg, "invalidate_schema_cache", _boom)
    report = schema_cache.invalidate_schema_caches()
    assert report["text2sql_schema_cleared"] is None
    assert report["text2sql_schema_cache_remaining"] is None, "失败侧不许留下半个真数"
    assert "text2sql" in report["errors"] and "不可用" in report["errors"]["text2sql"]
    assert report["neo4j_schema_marked_stale"] is False, "另一侧照常执行完（无实例 = False）"


@pytest.mark.asyncio
async def test_ops_endpoint_is_registered_and_answers_with_the_report(monkeypatch) -> None:
    from agentix.interfaces.http import ops_router

    paths = {getattr(r, "path", "") for r in ops_router.router.routes}
    assert "/ops/schema-cache/invalidate" in paths

    monkeypatch.setattr(
        schema_cache,
        "invalidate_schema_caches",
        lambda database_url=None: {"text2sql_schema_cleared": 7, "errors": {}},
    )
    answer = await ops_router.invalidate_schema_cache_endpoint(database_url=None)
    assert answer["text2sql_schema_cleared"] == 7
    assert answer["ok"] is True, "报告本身带 ok 位，运维脚本不必再看 HTTP 码"


@pytest.mark.asyncio
async def test_ops_endpoint_still_answers_200_shape_when_everything_fails(monkeypatch) -> None:
    """最坏情况（两侧都抛）也要返回一份能读的报告，而不是异常上抛。"""
    from agentix.interfaces.http import ops_router

    def _boom(*_a, **_kw):
        raise RuntimeError("全炸")

    monkeypatch.setattr(schema_cache, "invalidate_schema_caches", _boom)
    answer = await ops_router.invalidate_schema_cache_endpoint(database_url=None)
    assert answer["ok"] is False and "全炸" in answer["error"]
