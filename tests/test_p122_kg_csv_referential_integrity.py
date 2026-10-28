"""KG CSV 生成器 + Neo4j 灌库链路的守卫（导入成功 ≠ 图谱可用）。

为什么要有这个文件：`scripts/legal_kg_to_csv.py` → Neo4j 批量导入是文档
（`docs/guides/数据准备指南.md` §填充真实数据 5.图谱）里写明的图谱灌库链路，
但 2026-10-25 录制质量基线（graphrag 路由需要 Neo4j 有数据）时第一次真跑，
就连环撞出 4 个错——而且**其中 3 个不报错**：

1. 关系端点用了注册表键（`auth::全国人民代表大会`）而不是 `_ensure_node()` 生成的
   节点 id（`auth_2`）→ `neo4j-admin` 在 input validation 阶段整体失败
   （`InputException: ... referring to missing node ...`），整库一条都进不去；
   且报错只指出**第一条**违例，逐条试跑排障几乎不可行。
2. CSV 头与导入命令的 label 是英文（`Article`/`Authority`…），运行时 Cypher 用的是
   中文功能性标识符（`法条`/`机关`…，见 `docs/design/测试数据域-本体Schema.md` §1.1）
   → 导入成功、节点数也对，但 `MATCH (f:法条)` 恒 0 行，graphrag 静默退化。
3. `neo4j:5.18` 镜像默认 locale 是 POSIX/ASCII，neo4j-admin 的 JVM 按
   `sun.jnu.encoding` 解码 argv，中文 label 落地成 `??????`（同样 0 行、同样无报错）。
   把命令写进挂载的 UTF-8 脚本都不够，必须显式 `LC_ALL=C.UTF-8`。
4. 预定义 Cypher 的属性名与关系方向和 schema 不符：`(o:机关 {名称: …})` 应为
   `{name: …}`、`(o:机关)-[:制定机关]->(doc)` 方向反了（库里是 73 条入边）
   → 单条查询恒 0 行。
5. `引用` 边在单遍循环里就地解析，前向引用（被引条号还没登记）被 `if cited_node:`
   静默跳过 → 图谱 252 条、SQL 侧 286 行，两边都不报错，只是少答引用类问题。

验收口径（逐条对应上面的错）：
1. 以真实种子在内存生成 assets，逐条关系断言 START/END 命中节点 id（全量违例）；
   端点里不许再出现 `::` 形态；同 label 内 id 不重复；
2. 生成物列名/label 与 doc §1.1 对齐（**基准从文档解析**，不是抄进测试），
   Dockerfile 的 `--nodes=`/`--relationships=` 与 CSV 头、文档三方一致；
   预定义 Cypher 只使用文档声明过的 label/属性/关系方向；
3. `neo4j_seeded` 段在导入前设定 UTF-8 locale；
4. 骨架边必须存在、节点规模不得缩水（下限），空边（案由/司法解释/上位法）
   钉成显式期望，将来补数据时要求显式改口径；
5. 引用边以**种子重算的期望集**为准，与遍历顺序无关。

本测试不依赖 Neo4j、不写仓库目录、不联网。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "legal_kg_to_csv.py"
SEED_JSON = REPO_ROOT / "data" / "legal" / "legal_seed.json"
SCHEMA_DOC = REPO_ROOT / "docs" / "design" / "测试数据域-本体Schema.md"
DOCKERFILE = REPO_ROOT / "Dockerfile"
CYPHER_DICT = (
    REPO_ROOT
    / "agentix"
    / "application"
    / "agents"
    / "kg_sub_graph"
    / "agentic_rag_agents"
    / "components"
    / "predefined_cypher"
    / "legal_cypher_dict.py"
)

# rel 资产键 -> (START label, END label)，与 _write_csv() 里的 :START_ID/:END_ID 一致。
# label 用**中文功能性标识符**：口径见 docs/design/测试数据域-本体Schema.md §1.1–1.2，
# 由 test_import_labels_agree_with_the_schema_doc() 与文档/ Dockerfile 互相对齐。
REL_LABELS: Dict[str, tuple[str, str]] = {
    "rel_article_belongs_chapter": ("法条", "章节"),
    "rel_chapter_belongs_regulation": ("章节", "法规"),
    "rel_article_cites_article": ("法条", "法条"),
    "rel_article_cause": ("法条", "案由"),
    "rel_article_interpretation": ("法条", "司法解释"),
    "rel_regulation_authority": ("法规", "机关"),
    "rel_interpretation_authority": ("司法解释", "机关"),
    "rel_upper_law": ("法规", "法规"),
}

NODE_LABELS: Dict[str, str] = {
    "nodes_regulation": "法规",
    "nodes_chapter": "章节",
    "nodes_article": "法条",
    "nodes_cause": "案由",
    "nodes_interpretation": "司法解释",
    "nodes_authority": "机关",
}

NODE_KEYS: tuple[str, ...] = tuple(NODE_LABELS)


def _load_generator():
    """按文件路径加载 scripts/ 下的生成器（scripts 不是包，也不该进 sys.path）。"""
    spec = importlib.util.spec_from_file_location("legal_kg_to_csv", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def assets() -> Dict[str, List[Dict[str, Any]]]:
    if not GENERATOR.exists():  # pragma: no cover - defensive
        pytest.skip(f"generator missing: {GENERATOR}")
    if not SEED_JSON.exists():  # pragma: no cover - defensive
        pytest.skip(f"seed missing: {SEED_JSON}")
    module = _load_generator()
    seed = json.loads(SEED_JSON.read_text(encoding="utf-8"))
    return module._build_graph_assets(seed)


def _label_to_ids(assets: Dict[str, List[Dict[str, Any]]]) -> Dict[str, set[str]]:
    """label -> 节点 id 集合（与节点 CSV 第一列同源）。"""
    out: Dict[str, set[str]] = {}
    for key, label in NODE_LABELS.items():
        rows = assets[key]
        assert rows, f"{key} is empty: generator produced no nodes"
        out[label] = {str(row["id"]) for row in rows}
    return out


def test_test_side_label_map_matches_the_writer(tmp_path) -> None:
    """本文件的 `NODE_LABELS` / `REL_LABELS` 必须与 `_write_csv()` 自己声明的一致。

    守卫的是**守卫本身**：手写映射一旦与写表器漂移（改 label 名、加关系类型），
    引用完整性检查会安静地只校验一半，看起来全绿、实际没验。
    """
    module = _load_generator()
    seed = json.loads(SEED_JSON.read_text(encoding="utf-8"))
    out_dir = tmp_path / "kg"
    out_dir.mkdir()  # _write_csv 不建目录（main() 才建），这里必须自己建
    module._write_csv(out_dir, module._build_graph_assets(seed))

    seen_node_labels: Dict[str, str] = {}
    seen_rel_labels: Dict[str, tuple[str, str]] = {}
    for csv_path in sorted(out_dir.glob("*.csv")):
        with csv_path.open(encoding="utf-8", newline="") as fp:
            header = next(csv.reader(fp))
        if csv_path.name.endswith("_nodes.csv"):
            match = re.match(r"^:ID\((.+)\)$", header[0])
            assert match, csv_path.name
            seen_node_labels[csv_path.stem] = match.group(1)
        else:
            start = re.match(r"^:START_ID\((\w+)\)$", header[0])
            end = re.match(r"^:END_ID\((\w+)\)$", header[1])
            assert start and end, csv_path.name
            seen_rel_labels[csv_path.stem] = (start.group(1), end.group(1))

    # 逐资产键对齐：文件名 -> label（写表器的文件名 stem 与 rel 键同名去掉前缀）
    for key, label in NODE_LABELS.items():
        stem = key.removeprefix("nodes_") + "_nodes"
        assert seen_node_labels.pop(stem, None) == label, key
    for key, labels in REL_LABELS.items():
        assert seen_rel_labels.pop(key, None) == labels, key
    assert not seen_node_labels, f"node files not covered by NODE_LABELS: {seen_node_labels}"
    assert not seen_rel_labels, f"rel files not covered by REL_LABELS: {seen_rel_labels}"


def test_every_relationship_endpoint_resolves(assets) -> None:
    """全量违例检查：任意一条悬空端点都会让 Neo4j 整库导入失败。"""
    ids = _label_to_ids(assets)
    dangling: List[str] = []
    edges = 0
    for rel_key, (start_label, end_label) in REL_LABELS.items():
        rows = assets[rel_key]
        for row in rows:
            edges += 1
            for side, label in (("start", start_label), ("end", end_label)):
                value = str(row[side])
                if value not in ids[label]:
                    dangling.append(f"{rel_key}.{side}={value!r} (label {label})")
    assert not dangling, "dangling relationship endpoints:\n  " + "\n  ".join(dangling[:20])
    # 骨架边（条目->章节->数据集）必须存在，否则导入成功但图是空的
    assert assets["rel_article_belongs_chapter"], "article->chapter edges vanished"
    assert assets["rel_chapter_belongs_regulation"], "chapter->regulation edges vanished"


def test_node_columns_are_the_documented_schema(tmp_path) -> None:
    """生成物的列名必须落在 `测试数据域-本体Schema.md` §1.1 声明的属性集内。

    口径**从文档解析**而不是抄进测试（纪律 7：被当核对基准的表不能是抄本）——
    抄本会和实现一起漂移，解析出来的基准不会。
    """
    doc = SCHEMA_DOC.read_text(encoding="utf-8")
    documented: Dict[str, set[str]] = {}
    for line in doc.splitlines():
        # 形如：| `法规` (Regulation) | 一个测试数据集条目… | `name`、`生效日期`… |
        m = re.match(r"^\|\s*`([^`]+)`\s*\(([^)]+)\)\s*\|.*\|\s*(.+?)\s*\|\s*$", line)
        if not m:
            continue
        props = set(re.findall(r"`([^`]+)`", m.group(3)))
        documented[m.group(1)] = props
    assert len(documented) == len(NODE_LABELS), (
        f"doc §1.1 declares {sorted(documented)} but generator emits {sorted(NODE_LABELS.values())}"
    )

    module = _load_generator()
    seed = json.loads(SEED_JSON.read_text(encoding="utf-8"))
    out_dir = tmp_path / "kg2"
    out_dir.mkdir()
    module._write_csv(out_dir, module._build_graph_assets(seed))

    mismatches: List[str] = []
    for key, label in NODE_LABELS.items():
        filename = f"{key.removeprefix('nodes_')}_nodes.csv"
        with (out_dir / filename).open(encoding="utf-8", newline="") as fp:
            header = next(csv.reader(fp))
        columns = {h.split(":")[0] for h in header[1:]}
        missing = columns - documented[label]
        if missing:
            mismatches.append(f"{label}: columns {sorted(missing)} not in doc §1.1")
    assert not mismatches, "\n  ".join(mismatches)


def test_predefined_cypher_relationship_directions_match_doc() -> None:
    """模板里两端**都带内联 label** 的关系模式，方向必须与 doc §1.2 一致。

    方向反了和属性名写错一样静默：Neo4j 不报错，只是 0 行。
    `docs_by_authority` 当年就是这个形态（库里 73 条 `制定机关` 边，
    模板按出边匹配 → 恒空；改成入边实测返回 3 行）。
    """
    doc = SCHEMA_DOC.read_text(encoding="utf-8")
    doc_rows = {
        (m.group(1), m.group(2), m.group(3))
        for m in re.finditer(
            r"^\|\s*`([^`]+)`\s*\|\s*\(([^)]+)\)\s*→\s*\(([^)]+)\)\s*\|", doc, flags=re.MULTILINE
        )
    }
    assert doc_rows, "doc §1.2 的关系表没解析到任何行"

    text = CYPHER_DICT.read_text(encoding="utf-8")
    patterns = [
        (m.group(1), m.group(2), m.group(3), "out")
        for m in re.finditer(r"\(([^)]*)\)-\[[^\]]*?:([^\]]+?)\]->\(([^)]*)\)", text)
    ] + [
        (m.group(1), m.group(2), m.group(3), "in")
        for m in re.finditer(r"\(([^)]*)\)<-\[[^\]]*?:([^\]]+?)\]-\(([^)]*)\)", text)
    ]
    assert patterns, "没解析到任何关系模式：模板形态变了，本守卫需要跟着改"

    checked = 0
    offenders: List[str] = []
    for left, rel_type, right, arrow in patterns:
        lm = re.search(r":([^\s{]+)", left)
        rm = re.search(r":([^\s{]+)", right)
        if not lm or not rm:
            continue  # 一端是变量（无内联 label），静态判不了方向，交给实测探针
        checked += 1
        start, end = (lm.group(1), rm.group(1)) if arrow == "out" else (rm.group(1), lm.group(1))
        if (rel_type, start, end) not in doc_rows:
            offenders.append(f"{rel_type}: 模板是 ({start})→({end})，doc §1.2 没有这条方向")
    assert checked >= 5, f"只核到 {checked} 个可判方向的模式，解析可能失效了"
    assert not offenders, "\n  ".join(offenders)


def test_neo4j_stage_declares_a_utf8_locale() -> None:
    """`neo4j_seeded` 段必须在导入之前设定 UTF-8 locale。

    钉的是第三个"静默为空"的错：neo4j-admin 的 JVM 按 locale 解码 argv
    （`sun.jnu.encoding`），镜像默认 POSIX/ASCII 时 `--nodes=法条=…` 的中文
    进不了 Java，库里落成 `??????`——导入 0 报错，运行时查询恒 0 行。
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    start = dockerfile.index("AS neo4j_seeded")
    stage = dockerfile[start : start + 6000]
    import_at = stage.index("neo4j-admin database import")
    locale_at = stage.find("C.UTF-8")
    assert locale_at != -1, "neo4j_seeded 段没有任何 UTF-8 locale 设置"
    assert locale_at < import_at, "locale 必须在 neo4j-admin 导入**之前**设置"


def test_import_command_labels_match_generated_csv(tmp_path) -> None:
    """Dockerfile `neo4j_seeded` 的 `--nodes=<Label>=` 必须与 CSV 头声明的 label 一致。

    这是第二个案发现场：CSV 头与导入命令当年都用英文（`Article`/`Authority`…），
    于是导入"成功"，但运行时 `MATCH (f:法条)` 永远为空——比报错更难发现，
    因为 graphrag 只是安静地答不上来。
    """
    module = _load_generator()
    seed = json.loads(SEED_JSON.read_text(encoding="utf-8"))
    out_dir = tmp_path / "kg3"
    out_dir.mkdir()
    module._write_csv(out_dir, module._build_graph_assets(seed))

    csv_labels: Dict[str, str] = {}
    rel_pairs: Dict[str, tuple[str, str]] = {}
    for csv_path in sorted(out_dir.glob("*.csv")):
        with csv_path.open(encoding="utf-8", newline="") as fp:
            header = next(csv.reader(fp))
        if csv_path.name.endswith("_nodes.csv"):
            csv_labels[csv_path.name] = re.match(r"^:ID\((.+)\)$", header[0]).group(1)
        else:
            rel_pairs[csv_path.name] = (
                re.match(r"^:START_ID\((.+)\)$", header[0]).group(1),
                re.match(r"^:END_ID\((.+)\)$", header[1]).group(1),
            )

    # 注释行先剥掉：本仓 Dockerfile 的说明里就写着 `--nodes=法条=…` 这种示例，
    # 不剥的话守卫会把注释当成命令（守卫被自己的文档绊倒）。
    dockerfile = "\n".join(
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    node_args = re.findall(r"--nodes=([^=\s]+)=([^\s]+)", dockerfile)
    rel_args = re.findall(r"--relationships=([^=\s]+)=([^\s]+)", dockerfile)
    assert len(node_args) == len(csv_labels), "neo4j_seeded 没有为每个节点文件都声明 label"
    assert len(rel_args) == len(rel_pairs), "neo4j_seeded 没有为每个关系文件都声明类型"

    drift: List[str] = []
    doc = SCHEMA_DOC.read_text(encoding="utf-8")
    doc_node_labels = set(
        re.findall(r"^\|\s*`([^`]+)`\s*\([^)]+\)\s*\|", doc, flags=re.MULTILINE)
    )
    doc_rel_rows = set(
        re.findall(
            r"^\|\s*`([^`]+)`\s*\|\s*\(([^)]+)\)\s*→\s*\(([^)]+)\)\s*\|", doc, flags=re.MULTILINE
        )
    )
    for label, path in node_args:
        name = Path(path).name
        if csv_labels.get(name) != label:
            drift.append(f"--nodes={label}={name} 与 CSV 头 {csv_labels.get(name)} 不一致")
        if label not in doc_node_labels:
            drift.append(f"--nodes={label} 不是 doc §1.1 声明的 label")
    for rel_type, path in rel_args:
        name = Path(path).name
        pair = rel_pairs.get(name)
        if pair and (rel_type, pair[0], pair[1]) not in doc_rel_rows:
            drift.append(f"--relationships={rel_type}={name} 方向/类型与 doc §1.2 不符")
    assert not drift, "\n  ".join(drift)


def test_predefined_cypher_only_uses_documented_identifiers() -> None:
    """预定义 Cypher 的 label 与过滤属性必须与 §1.1/§1.2 同源，否则查询静默返空。

    钉住这条是因为 `legal_cypher_dict.py` 里有两处 `(o:机关 {名称: …})` /
    `(f:法规 {名称: …})`：节点上的属性名是 `name`（doc §1.1 与生成物都是），
    用 `名称` 过滤永远匹配不到，而 Neo4j 不报错——只是 0 行。
    """
    doc = SCHEMA_DOC.read_text(encoding="utf-8")
    documented: Dict[str, set[str]] = {}
    for line in doc.splitlines():
        m = re.match(r"^\|\s*`([^`]+)`\s*\(([^)]+)\)\s*\|.*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            documented[m.group(1)] = set(re.findall(r"`([^`]+)`", m.group(3)))
    rel_types = set(re.findall(r"^\|\s*`([^`]+)`\s*\|\s*\(", doc, flags=re.MULTILINE))

    text = CYPHER_DICT.read_text(encoding="utf-8")
    offenders: List[str] = []
    for label, props in re.findall(r":([\u4e00-\u9fa5A-Za-z]+)\s*\{([^}]*)\}", text):
        if label not in documented:
            offenders.append(f"未声明的 label：:{label}")
            continue
        for prop in re.findall(r"([\u4e00-\u9fa5A-Za-z_]+)\s*:", props):
            if prop not in documented[label]:
                offenders.append(f":{label} 上没有属性 {prop}（doc §1.1）")
    for rel in re.findall(r"\[:([\u4e00-\u9fa5A-Za-z_]+)\]", text):
        if rel not in rel_types:
            offenders.append(f"未声明的关系类型：{rel}")
    assert not offenders, "\n  ".join(offenders)


def test_forward_reference_citations_are_not_dropped(assets) -> None:
    """引用边必须与遍历顺序无关：被引条号在种子后面出现时，边也不能丢。

    这是第 5 个案发（2026-10-25，同一轮录制基线里实测出来）：生成器原来在
    单遍循环里就地解析 `引用`，而种子里 **34/286** 个引用对指向同一数据集中
    还没登记的条号（前向引用），于是 `if cited_node:` 静默跳过——
    图谱 252 条引用边，SQL 侧 286 行齐全，两边看着都"导入成功"。
    症状是 `articles_cited_by` 少答，不报错、不改结构，所以只能靠
    "以种子为基准重算期望边集"来钉。

    期望值口径（纪律 7）：**从种子重算**，不把 286 抄进测试。
    """
    seed = json.loads(SEED_JSON.read_text(encoding="utf-8"))

    registered: set[tuple[str, str]] = set()
    for reg in seed.get("regulations", []) or []:
        law = reg.get("name")
        if not law:
            continue
        for chapter in reg.get("chapters", []) or []:
            for article in chapter.get("articles", []) or []:
                no = str(article.get("条号", "")).strip()
                if no:
                    registered.add((law, no))

    expected: set[tuple[str, str, str]] = set()
    dropped_as_target: List[tuple[str, str]] = []
    for reg in seed.get("regulations", []) or []:
        law = reg.get("name")
        if not law:
            continue
        for chapter in reg.get("chapters", []) or []:
            for article in chapter.get("articles", []) or []:
                no = str(article.get("条号", "")).strip()
                cited_list = article.get("引用")
                if not isinstance(cited_list, list):
                    cited_list = [cited_list] if cited_list else []
                for cited in cited_list:
                    target = (law, str(cited).strip())
                    if target not in registered:
                        dropped_as_target.append(target)
                        continue
                    expected.add((law, no, str(cited).strip()))

    # 节点 id -> (法规名, 条号)：用生成物自己的列，不依赖 _article_id 的实现细节
    node_of = {
        str(row["id"]): (str(row["法规"]), str(row["条号"])) for row in assets["nodes_article"]
    }
    actual = {
        (node_of[str(row["start"])][0], node_of[str(row["start"])][1], node_of[str(row["end"])][1])
        for row in assets["rel_article_cites_article"]
        if str(row["start"]) in node_of and str(row["end"]) in node_of
    }
    assert len(actual) == len(assets["rel_article_cites_article"]), "引用边出现重复 (start,end)"
    assert actual == expected, (
        f"引用边与种子期望不符：少 {sorted(expected - actual)[:5]} / "
        f"多 {sorted(actual - expected)[:5]}"
    )
    # 种子自身若有指向不存在条目的引用，那是数据缺口，必须显式可见而不是被吞掉
    if dropped_as_target:
        assert len(dropped_as_target) <= len(expected), (
            "悬空引用多于有效引用，种子数据本身需要复核"
        )


def test_no_registry_key_leaks_into_relationship_ids(assets) -> None:
    """`::` 是注册表键的形态，不是节点 id——端点里出现即 bug 复活。"""
    offenders: List[str] = []
    for rel_key in REL_LABELS:
        for row in assets[rel_key]:
            for side in ("start", "end"):
                if "::" in str(row[side]):
                    offenders.append(f"{rel_key}.{side}={row[side]!r}")
    assert not offenders, "\n  ".join(offenders[:10])


def test_node_id_schemes_are_unique_per_label(assets) -> None:
    """同一 label 内 id 不能重复：CSV 里重复 id 会让导入器静默合并或报错。"""
    for key in NODE_KEYS:
        rows = assets[key]
        node_ids = [str(row["id"]) for row in rows]
        assert len(node_ids) == len(set(node_ids)), f"duplicate ids in {key}"


def test_node_counts_match_documented_scale(assets) -> None:
    """规模守卫（防缩水）：口径来自 docs/guides/数据准备指南.md 的表。

    只设**下限**，不设上限——补数据是允许的，悄悄丢数据不是。
    """
    assert len(assets["nodes_regulation"]) >= 4
    assert len(assets["nodes_article"]) >= 1953
    assert len(assets["nodes_chapter"]) >= 163
    assert len(assets["nodes_interpretation"]) >= 67
    assert len(assets["nodes_authority"]) >= 7


def test_empty_optional_fields_do_not_create_edges(assets) -> None:
    """种子里 案由/司法解释/上位法 全空 -> 对应边为空是**数据现状**，不是丢边。

    钉住这条是为了：将来真去补这些字段时，本测试会失败并要求显式改期望值，
    而不是让人以为"边莫名其妙多了"。
    """
    assert assets["rel_article_cause"] == []
    assert assets["rel_article_interpretation"] == []
    assert assets["rel_upper_law"] == []
