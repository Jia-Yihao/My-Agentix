from datetime import date, timedelta

from kb_service.schemas.ingest import MySQLIngestRequest
from kb_service.schemas.search import SearchRequest
from kb_service.services.legal_metadata import normalise_legal_metadata


def test_legal_metadata_normalises_aliases_and_dates():
    value = normalise_legal_metadata({"valid_from": date(2024, 1, 1), "权威等级": "official"})
    assert value["legal_effective_from"] == "2024-01-01"
    assert value["source_authority_level"] == "official"


def test_ingest_and_search_expose_legal_filters():
    ingest = MySQLIngestRequest(connection_url="mysql+pymysql://x", table="laws")
    assert ingest.extra_metadata == {}
    assert SearchRequest(query="劳动法", effective_on=date(2024, 1, 1)).effective_on == date(2024, 1, 1)


# ---------------------------------------------------------------------------
# R2-2b（指南 §2.2.7①）：四字段效力契约入库透传
# ---------------------------------------------------------------------------


def test_effective_status_derived_from_dates():
    """无显式状态 → 由日期推导（与检索侧语义一致：失效日含当天、严格大于才 expired）。"""
    today = date.today()
    future = (today + timedelta(days=30)).isoformat()
    past = (today - timedelta(days=30)).isoformat()

    assert normalise_legal_metadata({"legal_effective_from": future})["effective_status"] == "pending"
    assert normalise_legal_metadata({"legal_effective_to": past})["effective_status"] == "expired"
    assert normalise_legal_metadata({"legal_effective_to": today.isoformat()})["effective_status"] == "in_force"
    # 完整有效期窗口 → in_force；且规范四字段与旧键互镜像
    both = normalise_legal_metadata({"legal_effective_from": past, "legal_effective_to": future})
    assert both["effective_status"] == "in_force"
    assert both["effective_date"] == past
    assert both["expire_date"] == future


def test_effective_status_explicit_normalised():
    """显式状态归一化（中英别名 → 规范值），显式值优先于日期推导。"""
    assert normalise_legal_metadata({"effective_status": "已废止"})["effective_status"] == "repealed"
    assert normalise_legal_metadata({"effective_status": "EXPIRED"})["effective_status"] == "expired"
    # 显式 in_force 覆盖过期日期推导（数据修复场景）
    past = (date.today() - timedelta(days=1)).isoformat()
    assert normalise_legal_metadata({"effective_status": "in_force", "legal_effective_to": past})[
        "effective_status"
    ] == "in_force"


def test_replaced_by_passthrough_and_no_fields_defaults_in_force():
    value = normalise_legal_metadata({"replaced_by": "民法典", "被替代法": "旧法"})
    assert value["replaced_by"] == "民法典"  # 规范名优先，别名不覆盖

    empty = normalise_legal_metadata({})
    assert empty["effective_status"] == "in_force"  # 存量默认有效


def test_bad_date_fail_open():
    """日期坏 → 推导 fail-open 为 in_force（不误标失效）。"""
    assert normalise_legal_metadata({"legal_effective_to": "不是日期"})["effective_status"] == "in_force"


def test_ingest_request_accepts_effectiveness_fields():
    """MySQLIngestRequest 支持显式效力标注（人工清单标注已失效入口）。"""
    req = MySQLIngestRequest(
        connection_url="mysql+pymysql://x",
        table="laws",
        effective_status="expired",
        replaced_by="民法典",
    )
    assert req.effective_status == "expired"
    assert req.replaced_by == "民法典"
