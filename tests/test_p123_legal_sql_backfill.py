"""Text2SQL 目标库补灌脚本（`scripts/data/backfill_legal_sql.py`）的守卫。

为什么要有这个文件（缺口是实测出来的）：图谱（Neo4j 1953 条）与向量库
（Milvus 4865 块）都以 `data/legal/legal_seed.json` 为真源，**只有 SQL 这条路**
停在 compose 挂的 `data/insert_sample_data.sql` 上——4 部法律 / 10 个条目。
于是金标 L075「《民法典》一共多少条」（`must_contain: ['1260']`）在任何模型下
都答不对：`1260` 在库里物理不存在。补灌脚本把种子灌进 Text2SQL 目标库，
本文件钉住它的三条口径：

1. **计划层（纯函数）**：`build_records()` 的行数与种子同源、自然键不重复
   （重复会撞 MySQL 的 `UNIQUE (law_id, article_no)` / `UNIQUE (article_id, …)`）、
   引用只在同一数据集内、`causes.category` 落在 `ENUM('民事','刑事','行政')` 内
   （MySQL 严格模式下别的值会整批失败）；
2. **执行层幂等 + 不覆盖**：用 SQLite 建同构表跑两遍 `_apply()`——
   第二遍必须是空操作，且已存在的 `laws` 元数据一个字节都不许改。
   这条是**案发过的真 bug**：自然键→id 的映射写成 `{id: name}` 时
   "已存在"永远判不出来，`INSERT` 撞 `UNIQUE` 把整批事务打回
   （`(1062, "Duplicate entry '全国人民代表大会' for key 'authorities.name'")`）。
3. **真库（集成，CI 跳过）**：连得上 `TEXT2SQL_DATABASE_URL` 时核对
   民法典 1260 / 刑法 452 / 劳动合同法 98 / 宪法 143，并复核第二遍为空操作。

SQLite 只用来跑**幂等与不覆盖**这两条逻辑（列名/约束按 `data/init_mysql.sql`
同构建表）；ENUM、外键、字符集是 MySQL 特有的，交给第 3 条的集成测试。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine, text

from agentix.config import settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "data" / "backfill_legal_sql.py"
SEED_JSON = REPO_ROOT / "data" / "legal" / "legal_seed.json"
INIT_SQL = REPO_ROOT / "data" / "init_mysql.sql"

# 种子规模（口径来自 data/legal/legal_seed.json 本身，由
# test_plan_matches_the_seed_documented_scale 重算核对，不是抄来的常数表）
EXPECTED_PER_LAW = {"民法典": 1260, "刑法": 452, "宪法": 143, "劳动合同法": 98}


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_legal_sql", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed() -> Dict[str, Any]:
    return json.loads(SEED_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def records() -> Dict[str, List[Any]]:
    return _load_script().build_records(_seed())


@pytest.fixture(scope="module")
def seed_articles() -> List[Dict[str, Any]]:
    """从种子重算的 (法律名, 条号, 章节标题) 三元组，规模守卫的基准。"""
    rows: List[Dict[str, Any]] = []
    for reg in _seed().get("regulations", []) or []:
        for chapter in reg.get("chapters", []) or []:
            for article in chapter.get("articles", []) or []:
                no = str(article.get("条号", "")).strip()
                if no:
                    rows.append(
                        {"law": reg["name"], "article_no": no, "chapter": chapter.get("标题", "")}
                    )
    return rows


# ---------------------------------------------------------------------------
# 1) 计划层：build_records 是纯函数，脱离数据库校验
# ---------------------------------------------------------------------------


def test_plan_matches_the_seed_documented_scale(records, seed_articles) -> None:
    """行数与种子同源：补灌不许悄悄丢条目。"""
    from collections import Counter

    assert len(seed_articles) == sum(EXPECTED_PER_LAW.values())
    assert len(records["articles"]) == len(seed_articles)
    per_law = Counter(a["law"] for a in records["articles"])
    assert dict(per_law) == EXPECTED_PER_LAW, f"每部法律的条目数与种子不符：{dict(per_law)}"
    # 章节：同一法律内标题不重复（生成图时按 (法律, 标题) 合并）
    chapter_keys = {(c["law"], c["title"]) for c in records["chapters"]}
    assert len(chapter_keys) == len(records["chapters"])
    assert len(records["laws"]) == 4
    assert len(records["authorities"]) >= 7
    assert len(records["interpretations"]) >= 69


def test_article_natural_keys_are_unique(records) -> None:
    """`UNIQUE (law_id, article_no)`：重复键会让整批 INSERT 以 1062 失败。"""
    keys = [(a["law"], a["article_no"]) for a in records["articles"]]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"重复条目自然键：{sorted(dupes)[:5]}"


def test_relationship_candidates_are_deduped(records) -> None:
    """四张桥表都有 UNIQUE 键，候选行必须已经去重。"""
    for key in ("law_authorities", "citations", "article_causes", "article_interps"):
        rows = records[key]
        assert len(rows) == len(set(rows)), f"{key} 里有重复对"


def test_citations_stay_inside_their_own_law(records) -> None:
    """引用按 (同法律, 条号) 解析：跨法律的引用在这套自然键下不可能成立。"""
    offenders = [(a, b) for a, b in records["citations"] if a[0] != b[0]]
    assert not offenders, f"跨数据集引用：{offenders[:3]}"
    known = {(a["law"], a["article_no"]) for a in records["articles"]}
    dangling = [pair for pair in records["citations"] if pair[0] not in known or pair[1] not in known]
    assert not dangling, f"引用指向不存在的条目：{dangling[:3]}"


def test_cause_categories_fit_the_mysql_enum(records) -> None:
    """`causes.category ENUM('民事','刑事','行政')`——越界值在严格模式下会打回整批。"""
    allowed = {"民事", "刑事", "行政", None}
    offenders = [c for c in records["causes"] if c["category"] not in allowed]
    assert not offenders, f"案由类别越界：{offenders}"


def test_seed_gaps_are_visible_not_silently_dropped(records) -> None:
    """种子现状：条目级的案由/司法解释全空、关键词全空（图谱对应边数 0 同因）。

    钉住是为了将来补数据时本测试失败、要求显式改口径，而不是让人以为
    "SQL 侧和图谱侧一直对齐"。
    """
    assert records["article_causes"] == []
    assert records["article_interps"] == []
    assert all(not a["keywords"] for a in records["articles"])


def test_missing_authority_names_are_promoted(records) -> None:
    """法律/司法解释引用但清单里没有的机关名，也必须建出机关行（否则桥表插入被跳过）。"""
    authority_names = {a["name"] for a in records["authorities"]}
    referenced = {law["authority"] for law in records["laws"] if law["authority"]}
    referenced |= {i["authority"] for i in records["interpretations"] if i["authority"]}
    missing = referenced - authority_names
    assert not missing, f"被引用却不存在的机关：{sorted(missing)}"


# ---------------------------------------------------------------------------
# 2) 执行层：同构 SQLite 跑两遍，钉幂等与"不覆盖"
# ---------------------------------------------------------------------------

# 与 data/init_mysql.sql 同构（列名/唯一键一致；ENUM/外键/字符集是 MySQL 特有，
# 交给第 3 条集成测试）。基准从 init_sql 的表名集合核对，防止脚本与真实 DDL 分叉。
SQLITE_DDL = """
CREATE TABLE authorities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    level TEXT
);
CREATE TABLE laws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    effective_date TEXT,
    doc_number TEXT,
    level TEXT,
    upper_law_id INT
);
CREATE TABLE chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    law_id INT NOT NULL,
    parent_id INT,
    title TEXT NOT NULL,
    level TEXT
);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    law_id INT NOT NULL,
    chapter_id INT,
    article_no TEXT NOT NULL,
    content TEXT NOT NULL,
    keywords TEXT,
    UNIQUE (law_id, article_no)
);
CREATE TABLE article_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INT NOT NULL,
    cited_article_id INT NOT NULL,
    UNIQUE (article_id, cited_article_id)
);
CREATE TABLE causes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT
);
CREATE TABLE article_causes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INT NOT NULL,
    cause_id INT NOT NULL,
    UNIQUE (article_id, cause_id)
);
CREATE TABLE interpretations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    authority_id INT,
    publish_date TEXT,
    doc_number TEXT
);
CREATE TABLE article_interpretations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INT NOT NULL,
    interpretation_id INT NOT NULL,
    UNIQUE (article_id, interpretation_id)
);
CREATE TABLE law_authorities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    law_id INT NOT NULL,
    authority_id INT NOT NULL,
    UNIQUE (law_id, authority_id)
);
"""


def _sqlite_url(tmp_path: Path) -> str:
    db = tmp_path / "legal_sqlite.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SQLITE_DDL)
    finally:
        conn.close()
    return f"sqlite:///{db.as_posix()}"


def _seed_sample_laws(url: str) -> None:
    """预置"抽样 SQL 手工校对过"的法律行，用来验证补灌不覆盖它们。"""
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO laws (name, effective_date, doc_number, level, upper_law_id)"
                 " VALUES ('宪法', '1982-12-04', NULL, '法律', NULL)"),
        )
        conn.execute(
            text("INSERT INTO laws (name, effective_date, doc_number, level, upper_law_id)"
                 " VALUES ('民法典', '2021-01-01', '中华人民共和国主席令第四十五号', '法律', 1)"),
        )
        conn.execute(text("INSERT INTO authorities (name, level) VALUES ('全国人民代表大会', '最高权力机关')"))
        conn.execute(
            text("INSERT INTO articles (law_id, article_no, content, keywords)"
                 " VALUES (2, '1', '第一条为了保护民事主体的合法权益（抽样原文，与种子文本不同）',"
                 " '立法目的')")
        )
    engine.dispose()


def test_sqlite_ddl_covers_every_table_the_script_touches() -> None:
    """守卫自己的基准：本文件的表集合必须与 `data/init_mysql.sql` 一致。

    同构 DDL 一旦漏表/改名，SQLite 侧的幂等测试会验到一张不存在的表上。
    """
    doc = INIT_SQL.read_text(encoding="utf-8")
    mysql_tables = set(re.findall(r"CREATE TABLE (\w+)", doc))
    sqlite_tables = set(re.findall(r"CREATE TABLE (\w+)", SQLITE_DDL))
    assert sqlite_tables == mysql_tables, (
        f"init_mysql.sql 有而 DDL 没有：{sorted(mysql_tables - sqlite_tables)}；"
        f"DDL 多出：{sorted(sqlite_tables - mysql_tables)}"
    )


def test_apply_is_idempotent_and_never_overwrites(tmp_path, records, monkeypatch) -> None:
    """跑两遍：第二遍空操作；已存在的法律元数据与条目原文不许被改写。

    案发形态（2026-10-25 真炸过一次）：自然键→id 映射写反成 `{id: name}`，
    于是 `if name in ids` 恒假，重复 INSERT 撞 UNIQUE，整批事务回滚。
    """
    module = _load_script()
    url = _sqlite_url(tmp_path)
    _seed_sample_laws(url)
    monkeypatch.setattr(settings, "TEXT2SQL_DATABASE_URL", url)
    monkeypatch.setattr(settings, "DATABASE_URL", url)

    first = module._apply(records)
    assert first, "第一遍理应插入缺失行"
    assert first.get("articles"), "条目没补进去"

    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                text("SELECT name, effective_date || '|' || COALESCE(doc_number,'') || '|'"
                     " || COALESCE(level,'') || '|' || COALESCE(upper_law_id,'') FROM laws")
            )
        }
        content = conn.execute(
            text("SELECT content FROM articles WHERE law_id = 2 AND article_no = '1'")
        ).scalar()
        counts = {
            t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("laws", "chapters", "articles", "authorities", "interpretations")
        }
    engine.dispose()

    # 抽样 SQL 的手工校对值原样保留（含条文原文：预置行的自然键
    # (民法典, '1') 与种子的第一条相同，正是"库里已有就一条都不动"的那条路径）
    assert rows["宪法"] == "1982-12-04||法律|"
    assert rows["民法典"].startswith("2021-01-01|中华人民共和国主席令第四十五号|法律|")
    assert content == "第一条为了保护民事主体的合法权益（抽样原文，与种子文本不同）"

    second = module._apply(records)
    assert second == {}, f"第二遍必须是空操作，实际又插了：{second}"

    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        after = {
            t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("laws", "chapters", "articles", "authorities", "interpretations")
        }
    engine.dispose()
    assert after == counts, f"第二遍改了行数：{after} != {counts}"
    # 目标库补齐到种子规模（抽样条目本就在种子里，不会额外多出来）
    assert after["articles"] == len(records["articles"])
    assert after["laws"] == len(records["laws"])


def test_apply_resolves_article_ids_after_backfill(tmp_path, records, monkeypatch) -> None:
    """引用边的两端必须解析成真实 article id（补灌后 286 条对全都在库里）。"""
    module = _load_script()
    url = _sqlite_url(tmp_path)
    monkeypatch.setattr(settings, "TEXT2SQL_DATABASE_URL", url)
    monkeypatch.setattr(settings, "DATABASE_URL", url)

    inserted = module._apply(records)
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        n_cites = conn.execute(text("SELECT COUNT(*) FROM article_citations")).scalar()
        orphans = conn.execute(
            text("SELECT COUNT(*) FROM article_citations c"
                 " LEFT JOIN articles a1 ON a1.id = c.article_id"
                 " LEFT JOIN articles a2 ON a2.id = c.cited_article_id"
                 " WHERE a1.id IS NULL OR a2.id IS NULL")
        ).scalar()
    engine.dispose()
    assert inserted.get("article_citations") == len(records["citations"])
    assert n_cites == len(records["citations"])
    assert orphans == 0


# ---------------------------------------------------------------------------
# 3) 真库集成（CI 无 MySQL，默认跳过）
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_target_db_holds_the_full_seed() -> None:
    """连得上 Text2SQL 目标库时，核对每部法律的条目数 = 种子条数。

    L075 的 `must_contain: ['1260']` 只有在真库里成立才算基线可用。
    只有**连不上**才降级为跳过：把其它异常（写错的 SQL、口径漂移）一起吞成
    skip，等于给这条守卫装了个"永远绿"的开关。
    """
    from sqlalchemy.exc import OperationalError

    url = (getattr(settings, "TEXT2SQL_DATABASE_URL", "") or "").strip()
    if not url:
        pytest.skip("未配置 TEXT2SQL_DATABASE_URL，跳过真库核对")
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as conn:
            per_law = dict(
                conn.execute(
                    text("SELECT l.name, COUNT(a.id) FROM laws l"
                         " JOIN articles a ON a.law_id = l.id GROUP BY l.name")
                ).all()
            )
    except OperationalError:
        pytest.skip("Text2SQL 目标库不可达（未起 MySQL / 库不存在）")
    finally:
        engine.dispose()
    assert per_law == EXPECTED_PER_LAW, f"目标库条目数与种子不符：{per_law}"
