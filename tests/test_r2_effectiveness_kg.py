"""R2-2 收口：KG/Cypher 记录级效力后过滤测试（Neo4j 路径）。

背景：R2-2 效力过滤此前只覆盖向量检索路径（_similarity_search_with_fetch）；
本轮补齐 Neo4j cypher 记录路径（filter_records_in_force + cypher_tools 节点接线）。

LightRAG / GraphRAG 路径返回纯文本（无结构化效力字段），记录级后过滤不可判，
维持现状不接线（决策记录见差距分析文档）。
"""

from __future__ import annotations

from agentix.domain.policies.legal_effectiveness import (
    attach_effective_status,
    filter_records_in_force,
)


def _rec(**meta):
    base = {"name": "劳动合同法", "article": "第三十九条"}
    base.update(meta)
    return base


# ---------- 基本过滤 ----------

def test_in_force_records_kept_and_annotated():
    recs = [
        _rec(effective_status="in_force"),
        _rec(expire_date="2099-12-31"),  # 日期推导 → in_force
    ]
    out = filter_records_in_force(recs)
    assert len(out) == 2
    assert all(r["effective_status"] == "in_force" for r in out)
    # 原记录对象不被修改（纯函数）：第二条原本无 status 字段，保留原样
    assert "effective_status" not in recs[1]


def test_expired_and_pending_records_dropped():
    recs = [
        _rec(effective_status="expired"),
        _rec(effective_status="已废止"),
        _rec(effective_date="2099-01-01"),  # 未生效
        _rec(expire_date="2000-01-01"),     # 已过失效日
    ]
    out = filter_records_in_force(recs)
    assert out == []


def test_expire_date_inclusive_same_day():
    """失效当天仍有效（与向量路径语义一致）。"""
    recs = [_rec(expire_date="2000-01-01")]
    out = filter_records_in_force(recs, now="2000-01-01")
    assert len(out) == 1


# ---------- fail-open：无字段记录零变化 ----------

def test_records_without_fields_pass_through_untouched():
    """存量图数据无效力字段：原样保留且不注入任何字段（统计查询零影响）。"""
    recs = [_rec(), {"count": 42}, "plain-string-record", {"聚合": "结果"}]
    out = filter_records_in_force(recs)
    assert out == recs
    # 不注入 effective_status
    assert all("effective_status" not in r for r in out if isinstance(r, dict))


def test_bad_date_fail_open():
    """日期解析失败 → 视为有效（宁可漏报不误报）。"""
    recs = [_rec(effective_date="不是日期", expire_date="garbage")]
    out = filter_records_in_force(recs)
    assert len(out) == 1


def test_non_dict_records_pass():
    class Weird:
        pass

    recs = [Weird(), None, 123]
    out = filter_records_in_force(recs)
    assert out == recs


# ---------- 开关与别名 ----------

def test_enabled_false_passthrough():
    recs = [_rec(effective_status="expired"), _rec()]
    assert filter_records_in_force(recs, enabled=False) == recs


def test_date_key_aliases_recognized():
    recs = [
        _rec(legal_effective_from="2099-01-01"),  # 别名：未生效
        _rec(生效日期="2099-01-01"),              # 中文别名
        _rec(失效日期="2000-01-01"),              # 中文别名：已失效
    ]
    out = filter_records_in_force(recs)
    assert out == []


def test_attach_effective_status_includes_replaced_by():
    hit = attach_effective_status({"chunk_id": "c1"}, {"replaced_by": "民法典"})
    assert hit["effective_status"] == "in_force"  # 缺失默认有效
    assert hit["replaced_by"] == "民法典"


# ---------- 接线面 ----------

def test_cypher_node_module_imports():
    """接线模块可导入（cypher node 的过滤器 import 链无循环依赖）。"""
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components import cypher_tools  # noqa: F401
