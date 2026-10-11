"""P41：文档一致性守卫（防「文档说一套、代码是另一套」）。

批次 13 写/改了大量文档（架构文档、ADR、CHANGELOG、配置表）。文档最大的风险
不是写错，而是**写完就腐烂** —— 代码改了、文件挪了，文档里的路径和配置键
还停在原地，后来者按文档找过去扑空。

本文件把两类最常见的腐烂**自动化**拦截：

1. **配置键漂移**：配置文档表格里列出的键，必须在 `Settings` 里真的存在。
   （批次 13 实测：25 个真实配置键全部存在，仅 `EXCEEDED` / `WARN`
     是 `BudgetVerdict` 枚举成员被误抽——故设了小 allowlist。）
2. **路径漂移**：文档里以反引号引用的 `agentix/…`、`tests/…` 等仓库内路径
   必须真实存在。（批次 13 实测发现 3 处真实腐烂：
   `agentix/services/lightrag_service.py`、
   `agentix/api/lightrag_router.py`、`kb_ingest/core/config.py`，均已修正。）
3. **覆盖盲区**（2026-10-04 扩）：根目录当前态文档（README / SECURITY /
   CONTRIBUTING / CODE_OF_CONDUCT / evals/README）与说明书 `docs/*.docx`
   此前不在扫描集内——docx 没有反引号定界，改为**逐段 + 首尾边界**正则抽取；
   `CHANGELOG.md` 刻意除外（历史记录，「当时」的事实陈述不随代码改写，
   同 TestTestCountHasSingleSource 的界定）。前缀补 `docs`/`config`/`.github`/
   `skills`，扩展名补 `vue/ts/js/mjs/mermaid/png/csv`。

设计要点：**只校验「明确是配置/路径」的上下文**，
不做全文大写标识符的粗暴匹配——后者会把 prompt 常量名、枚举成员、
甚至 SQL 关键字（`SELECT` / `FROM` / `JOIN`）都当成配置键，误报满天飞，
守卫很快就会被 `-k` 跳过或删掉。
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Set

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = PROJECT_ROOT / "docs"

#: 含「配置键表格」的文档（行首为 `| \`KEY\``）
CONFIG_DOCS = [
    "docs/architecture/configuration.md",
    "docs/architecture/runtime-guardrails.md",
]

#: 允许出现在配置表里、但**不是** Settings 字段的标识符（枚举成员等）
NON_SETTINGS_IDENTIFIERS: Set[str] = {
    "OK",
    "WARN",
    "EXCEEDED",          # BudgetVerdict
    "COMPLETE",
    "DEGRADED",
    "INCOMPLETE",        # CompletionSignal
    "PENDING",
    "APPROVED",
    "REJECTED",          # ReviewDecision / RunState
}

#: 路径中含 `.../` 缩写（如 `agentix/.../node.py`）的不校验——那不是真实路径
_ABBREV_RE = re.compile(r"/\.\.\./")

#: 根目录「当前态」文档（也入扫描集）。CHANGELOG.md 刻意排除：它是历史记录，
#: 里面的旧路径/旧文件名是**事实陈述**，不随代码搬迁改写。
ROOT_MD_DOCS = [
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "evals/README.md",
]

#: 路径前缀白名单。`data/` 刻意排除：其内容是运行时产物（embedding_cache、
#: cron jobs、trajectory dump 等），不入库、不保证存在。
_PATH_PREFIX = (
    r"(?:agentix|tests|evals|scripts|kb_ingest|web|docs|config|\.github|skills)"
)

#: 扩展名白名单。`jsonl` 刻意排除：`evals/baseline_predictions.jsonl` 是
#: 「待录制、未入库」的产物，检查它只会得到一条永远红的守卫。
_PATH_EXT = r"(?:py|md|toml|json|txt|sh|yml|yaml|vue|ts|js|mjs|mermaid|png|csv)"

#: md 侧：反引号定界，天然有头尾边界。
_MD_PATH_RE = re.compile(
    r"`(" + _PATH_PREFIX + r"/[A-Za-z0-9_./-]+\." + _PATH_EXT + r")`"
)

#: docx 侧：纯文本无定界——头部 lookbehind 防从词中间截断，尾部 lookahead
#: 防 `legal_golden.jsonl` 被抽成 `legal_golden.json` 这类前缀假匹配。
_DOCX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + _PATH_PREFIX + r"/[A-Za-z0-9_./-]+\." + _PATH_EXT
    + r")(?![A-Za-z0-9])"
)

#: docx 中**有意保留**的已删除路径（M5/M6 删除面的留档说明句：「原 xxx，已删」
#: 引用旧路径是事实陈述、不是腐烂）。只允许 ≤5 条，防膨胀成万能豁免。
DOCX_INTENTIONAL_REFS = {
    "agentix/reliability/release_gate.py",   # 说明书删除面留档段
}


def _docs_iter():
    """遍历「当前态」md 文档：根目录清单 + docs/ 下全部 .md（含子目录）。"""
    for rel in ROOT_MD_DOCS:
        path = PROJECT_ROOT / rel
        if path.is_file():
            yield path
    yield from sorted(DOCS_ROOT.rglob("*.md"))


def _config_keys() -> Dict[str, str]:
    """从配置文档表格抽取配置键 → 出处文档。"""
    found: Dict[str, str] = {}
    for rel in CONFIG_DOCS:
        path = PROJECT_ROOT / rel
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\|\s*`([A-Z][A-Z0-9_]{3,})`\s*\|", line)
            if m:
                found.setdefault(m.group(1), rel)
    return found


def _doc_paths() -> Dict[str, str]:
    """从全部 md 文档（根目录 + docs/）抽取仓库内路径引用 → 出处文档。"""
    found: Dict[str, str] = {}
    for path in _docs_iter():
        text = path.read_text(encoding="utf-8")
        for m in _MD_PATH_RE.finditer(text):
            ref = m.group(1)
            if _ABBREV_RE.search(ref):
                continue
            found.setdefault(ref, str(path.relative_to(PROJECT_ROOT)))
    return found


def _docx_paragraph_texts(path: Path) -> List[str]:
    """逐段抽取 docx 文本。

    段/制表/换行边界必须保留成分隔符——裸剥 XML 标签会把相邻 run 与相邻段
    粘成假路径（实测：`test_dependency_consistency.py` + 制表符 +
    `requirements.txt` 被粘成 `…pyrequirements.txt`；跨段的 `agentix/` +
    `config/…` 被粘成一条**恰好存在**的假路径，把真实断行漏掉）。
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    texts: List[str] = []
    for chunk in re.split(r"</w:p>", xml):
        chunk = re.sub(r"<w:tab\b[^>]*/>", "\t", chunk)
        chunk = re.sub(r"<w:(?:br|cr)\b[^>]*/>", "\n", chunk)
        texts.append(re.sub(r"<[^>]+>", "", chunk))
    return texts


def _docx_paths() -> Dict[str, str]:
    """从说明书 docx 抽取仓库内路径引用 → 出处（含行号/有意留档过滤）。"""
    found: Dict[str, str] = {}
    for path in sorted(DOCS_ROOT.glob("*.docx")):
        rel_doc = str(path.relative_to(PROJECT_ROOT))
        for text in _docx_paragraph_texts(path):
            for m in _DOCX_PATH_RE.finditer(text):
                ref = m.group(1)
                if _ABBREV_RE.search(ref) or ref in DOCX_INTENTIONAL_REFS:
                    continue
                found.setdefault(ref, rel_doc)
    return found


class TestDocumentedConfigKeys:
    def test_every_documented_key_exists_in_settings(self):
        from agentix.config import settings

        known = set(settings.__dict__.keys())
        missing: List[str] = []
        for key, origin in sorted(_config_keys().items()):
            if key in NON_SETTINGS_IDENTIFIERS:
                continue
            if key not in known:
                missing.append(f"{key}（出自 {origin}）")
        assert not missing, (
            "以下配置键写在文档里，但 Settings 中并不存在（文档漂移）：\n  "
            + "\n  ".join(missing)
            + "\n\n要么在 settings.py 补上该字段，要么从文档移除它。"
        )

    def test_we_actually_found_keys(self):
        """防空真：正则若失效会导致上面那条永远绿。"""
        keys = _config_keys()
        assert len(keys) >= 20, f"抽取到的配置键过少（{len(keys)}），正则可能失效"

    def test_new_settings_are_documented(self):
        """反向校验：批次 13 新增的关键配置必须出现在文档里。

        ⚠️ 这张表是**手写的**，所以它只能证明"表里的键被文档化了"，
        无法证明"新加的键被文档化了"——新字段只要没人往这张表里加，
        就会**静默绕过**本用例。2026-09 新增的 `KB_API_KEY` 就是这种情况，
        已补进表内。**新增 Settings 字段时，请一并把键加到这里**，
        否则这条守卫对它是失效的。
        """
        documented = set(_config_keys())
        for key in (
            "OBSERVABILITY_PAYLOAD_CAPTURE",
            "ARTIFACT_INLINE_THRESHOLD",
            "TOKEN_BUDGET_PER_REQUEST",
            "TOOL_RESULT_MAX_ROWS",
            "HUMAN_REVIEW_ENABLED",
            "COMPLETION_VALIDATOR_STRICT",
            "TRAJECTORY_SINK",
            "TRAJECTORY_PATH",
            "NEO4J_LEGAL_JSON_PATH",
            "KB_API_KEY",
            "AGENT_LOOP_ENABLED",
            "EVENT_PORT_BACKEND",
            "V1_REQUIRE_JWT",
            "SKILLS_ENABLED",
            "SKILLS_DIR",
            "HOOKS_ENABLED",
            "CONTEXT_AUTOCOMPACT",
            "NOTIFY_USER_ENABLED",
            "MEMORY_CONSOLIDATION_ENABLED",
            "AGENTIC_RAG_ENABLED",
            "CLARIFY_ROUTING_ENABLED",
            "BUDGET_NOTICE_ENABLED",
            "CONTROL_PLANE_MAX_CONCURRENCY",
            "CONTROL_PLANE_MAX_DEPTH",
            "MULTIAGENT_PEER_MAX_STEPS",
            "MULTIAGENT_PEER_MAX_MESSAGES",
            "MULTIAGENT_BUS_ENABLED",
            "MULTIAGENT_SUBAGENT_ISOLATION_ENABLED",
            "TASK_EXEC_ENABLED",
            "AGENT_MAX_REPLANS",
            "TOOL_SELECTION_ENABLED",
            "TOOL_SELECTION_MAX_RISK",
            "TOOL_DEFERRED_TOOLS",
            "TOOL_RESULT_CONTRACT_ENABLED",
            "TOOL_GATEWAY_ENFORCED",
            "AGENT_CONTEXT_BUDGET_TOKENS",
            "CONTEXT_COMPACT_LADDER",
            "TOOL_GUARDRAILS_ENABLED",
            "AGENT_ROLLBACK_ENABLED",
            "FAILURE_BOUNDARY_ENABLED",
            "CRON_ENABLED",
            "CRON_TICK_SECONDS",
            "MEMORY_FANOUT_ENABLED",
            "MEMORY_REVIEW_ENABLED",
            "MEMORY_TOOLS_ENABLED",
            "KG_PLANNER_SHADOW_ENABLED",
            "KG_PLANNER_UNIFIED_ENABLED",
            "NEO4J_SCHEMA_REFRESH_SECONDS",
            "NEO4J_QUERY_TIMEOUT_SECONDS",
            "KB_LOCAL_FUSION_ENABLED",
            "KB_LOCAL_FUSION_METHOD",
            "MEMORY_VECTOR_RECALL_ENABLED",
            "CONTEXT_REAL_TOKEN_COUNT_ENABLED",
            "MEMORY_INDEX_ENABLED",
            "AGENTS_CASCADE_ENABLED",
            "CONTEXT_ISOLATE_PERSIST_ENABLED",
            "LG_INSTRUCTIONS_ENABLED",
            "LG_TOKEN_HISTORY_BUDGET",
            "LG_STATUS_TAIL_ENABLED",
            "AGENT_LLM_PLANNER_ENABLED",
            "AGENT_TOOL_ROUND_CONCURRENCY",
            "HITL_IN_LOOP_ENABLED",
            "CONTEXT_PRECOMPACT_REINJECT_ENABLED",
            "MEMORY_PROFILE_SCOPE_ENABLED",
            "TEXT2SQL_DRY_RUN_ENABLED",
            "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS",
            "KG_SCHEMA_AWARE_GENERATION_ENABLED",
            "EVIDENCE_POOL_ENABLED",
            "VERIFIER_GROUNDING_ENABLED",
            "LATENCY_BREAKDOWN_ENABLED",
            "LATENCY_TOTAL_BUDGET_MS",
            "EVENT_LOOP_LAG_METRIC_ENABLED",
            "ROUTING_FASTPATH_ENABLED",
            "LLM_TIER_ROUTING_ENABLED",
            "LLM_ROLE_TIER_MAP",
            "VERIFICATION_MAX_LLM_CALLS",
            "VERIFICATION_TIMEOUT_MS",
            "VERIFICATION_TOKEN_BUDGET",
            "NEO4J_COST_GUARD_ENABLED",
            "MILVUS_NPROBE",
            "TOOL_OUTPUT_BUDGET_TOKENS",
            "ADMISSION_CONTROL_ENABLED",
            "ADMISSION_MAX_CONCURRENT_RUNS",
            "ADMISSION_MAX_QUEUE",
            "RETRY_BUDGET_ENABLED",
            "RETRY_BUDGET_TOKENS",
            "ADAPTIVE_TOOL_TIMEOUT_ENABLED",
            "LOAD_SHEDDING_ENABLED",
            "BULKHEAD_RETRIEVAL_SIZE",
            "BULKHEAD_NEO4J_SIZE",
            "BULKHEAD_MCP_SIZE",
            "BULKHEAD_WEB_SEARCH_SIZE",
            "SHARED_HTTP_MAX_CONNECTIONS",
            "EVENT_BATCH_MAX_SIZE",
            "EVENT_BATCH_FLUSH_MS",
            "TRAJECTORY_ASYNC_WRITE_ENABLED",
            "SSE_COALESCE_ENABLED",
            "SSE_QUEUE_SIZE",
            "SUBAGENT_RESULT_CONTRACT_ENABLED",
            "MULTIAGENT_EARLY_EXIT_ENABLED",
            "LLM_FALLBACK_MODEL_SPECS",
            "FEEDBACK_ENABLED",
            "FEEDBACK_PII_REDACT",
        ):
            assert key in documented, f"{key} 未在配置文档中说明"

    def test_non_settings_allowlist_is_small(self):
        """allowlist 不该无限膨胀——膨胀说明抽取正则该收紧了。"""
        assert len(NON_SETTINGS_IDENTIFIERS) <= 15


class TestDocumentedPaths:
    def test_every_documented_path_exists(self):
        missing: List[str] = []
        for ref, origin in sorted(_doc_paths().items()):
            if not (PROJECT_ROOT / ref).exists():
                missing.append(f"{ref}（出自 {origin}）")
        assert not missing, (
            "以下文档引用的路径在仓库中不存在（文档腐烂）：\n  "
            + "\n  ".join(missing)
            + "\n\n请更新文档指向新位置，或删掉该引用。"
        )

    def test_we_actually_found_paths(self):
        refs = _doc_paths()
        assert len(refs) >= 20, f"抽取到的路径引用过少（{len(refs)}），正则可能失效"


class TestCoverageBlindSpots:
    """覆盖盲区守卫（2026-10-04 扩）：根目录文档与说明书 docx。

    修复前这两类完全不在扫描集里——SECURITY.md 的 ChatWidget.vue、
    说明书里的旧路径都可以腐烂而无人察觉。
    """

    def test_root_docs_contribute_paths(self):
        """根目录文档必须真的被扫到（扫不到 = 上面的断言对它们失效）。"""
        refs = [
            ref for ref, origin in _doc_paths().items()
            if origin in ROOT_MD_DOCS
        ]
        assert len(refs) >= 20, (
            f"根目录文档只抽到 {len(refs)} 条路径引用——ROOT_MD_DOCS "
            "或正则可能失效"
        )

    def test_docx_paths_exist(self):
        missing: List[str] = []
        for ref, origin in sorted(_docx_paths().items()):
            if not (PROJECT_ROOT / ref).exists():
                missing.append(f"{ref}（出自 {origin}）")
        assert not missing, (
            "说明书 docx 引用的路径在仓库中不存在（文档腐烂）：\n  "
            + "\n  ".join(missing)
            + "\n\n请更新 docx 指向新位置；若属有意保留的删除面留档，"
            "将其加入 DOCX_INTENTIONAL_REFS 并写明理由。"
        )

    def test_docx_scan_actually_found_paths(self):
        """防空真：docx 抽取失效（改名/损坏）会让上一条永远绿。"""
        refs = _docx_paths()
        assert len(refs) >= 100, (
            f"docx 只抽到 {len(refs)} 条路径引用——抽取逻辑可能失效"
        )

    def test_docx_intentional_allowlist_is_small(self):
        """有意留档的豁免必须是小集合，防膨胀成万能豁免。"""
        assert len(DOCX_INTENTIONAL_REFS) <= 5


class TestArchitectureDocsPresent:
    """架构文档体系的完整性（批次 11 交付，防被误删）。"""

    @pytest.mark.parametrize(
        "rel",
        [
            "docs/architecture/README.md",
            "docs/architecture/layers.md",
            "docs/architecture/interfaces.md",
            "docs/architecture/extension.md",
            "docs/architecture/configuration.md",
            "docs/architecture/runtime-guardrails.md",
            "docs/architecture/adr/README.md",
            "docs/architecture/adr/0001-layered-architecture-with-ports.md",
            "docs/architecture/adr/0002-registry-based-plugin-mechanism.md",
            "docs/architecture/adr/0003-declarative-configuration-single-source.md",
            "docs/VERSIONING.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "LICENSE",
        ],
    )
    def test_file_exists(self, rel: str):
        assert (PROJECT_ROOT / rel).is_file(), f"缺失交付物：{rel}"

    def test_layers_doc_states_zero_exemptions(self):
        """依赖规则零例外——这条结论必须与代码现状同步。"""
        text = (PROJECT_ROOT / "docs/architecture/layers.md").read_text(encoding="utf-8")
        assert "零例外" in text or "无（原唯一例外已清除）" in text


# ---------------------------------------------------------------------------
# 数值口径守卫（批次 14 补）
#
# 上面两类守卫拦的是「路径腐烂」和「配置键腐烂」，拦不住**数字腐烂**。
# 实测腐烂样本（本次修复前）：
#   - 22 条 golden 的三处声明（真实 102 条）—— 分散在架构 README / evals README；
#   - 「只注册了 7 个节点」（真实 8 个）—— 代码注释与设计文档各一处；
#   - 「3 个确定性指标」（真实 5 个打分函数）；
#   - README 的测试数「1388+ passed」（真实 1500+）。
# 这类腐烂最阴的地方是：数字看起来精确，读者会默认它被核对过。
#
# 设计原则同前：只守**有唯一事实源**的数字。凡是能从代码/数据直接算出来的，
# 就与其比对；算不出来的（如"性能提升 30%"）不在这里假装能守。
# ---------------------------------------------------------------------------

GOLDEN_PATH = PROJECT_ROOT / "evals" / "legal_golden.jsonl"

#: 会声明 golden 规模的文档
GOLDEN_CLAIM_DOCS = [
    "README.md",
    "docs/architecture/README.md",
    "evals/README.md",
]


def _golden_facts() -> Dict[str, int]:
    """从 golden 数据本身算出事实（唯一事实源）。"""
    rows = [
        json.loads(line)
        for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {
        "total": len(rows),
        "reject": sum(1 for row in rows if row.get("expect_reject")),
        "routes": len({row["route_type"] for row in rows}),
    }


def _declared_add_node_count() -> int:
    """从 build_graph 函数体里数 builder.add_node(...) 调用次数。"""
    text = (PROJECT_ROOT / "agentix/orchestration/langgraph/lg_builder.py").read_text(
        encoding="utf-8"
    )
    body = text.split("def build_graph(", 1)[1].split("\ndef ", 1)[0]
    return body.count("builder.add_node(")


class TestDocumentedGoldenNumbers:
    def test_golden_size_claims_match_data(self):
        """文档里「N 条 golden」必须等于 legal_golden.jsonl 的真实条数。"""
        facts = _golden_facts()
        stale: List[str] = []
        for rel in GOLDEN_CLAIM_DOCS:
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            for match in re.finditer(r"(\d+)\s*条\s*\*{0,2}\s*golden", text):
                if int(match.group(1)) != facts["total"]:
                    stale.append(f"{rel}: 声称 {match.group(1)} 条 golden，真实 {facts['total']}")
        assert not stale, "golden 条数声明已腐烂：\n  " + "\n  ".join(stale)

    def test_reject_row_claims_match_data(self):
        """文档里「N 条越界」必须等于真实 ``expect_reject=True`` 的条数。

        约定：文档中所有「N 条越界」一律指**总数**。真实 golden 的 13 条越界样本
        全部落在 `general-query` 一类里，因此即便是按类描述的场合，写出来的数字
        也应与总数相同——否则读者无法判断这个数字的作用域。
        （修复前 `evals/README.md` 写「general-query 26（含 10 条越界拒答）」，
         而同一段下一行又写「共 13 条」，同一段内自相矛盾。）
        """
        facts = _golden_facts()
        stale: List[str] = []
        for rel in GOLDEN_CLAIM_DOCS:
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            for match in re.finditer(r"(\d+)\s*条\*{0,2}\s*越界", text):
                if int(match.group(1)) != facts["reject"]:
                    stale.append(
                        f"{rel}: 声称 {match.group(1)} 条越界，真实 {facts['reject']}"
                    )
        assert not stale, "越界条数声明已腐烂：\n  " + "\n  ".join(stale)

    def test_golden_distribution_line_matches_data(self):
        """`evals/README.md` 的「当前分布」段里每个「路由 N」都要对上真实计数。"""
        text = (PROJECT_ROOT / "evals/README.md").read_text(encoding="utf-8")
        match = re.search(r"当前分布([^\n]*(?:\n[^\n]+){0,2})", text)
        assert match, "未找到「当前分布」段，文档结构变了"
        segment = match.group(1)

        pairs = re.findall(r"`([a-z0-9-]+)`\s*(\d+)", segment)
        assert len(pairs) >= 7, (
            f"抽取到的路由分布项过少（{len(pairs)}，{pairs}），正则可能失效"
        )

        from collections import Counter

        rows = [
            json.loads(line)
            for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        real = Counter(row["route_type"] for row in rows)

        stale = [
            f"{route}: 文档 {count}，真实 {real.get(route, 0)}"
            for route, count in pairs
            if int(count) != real.get(route, 0)
        ]
        assert not stale, "golden 路由分布已腐烂：\n  " + "\n  ".join(stale)

    def test_we_actually_extracted_claims(self):
        """防空真：正则若失效，上面两条会永远绿。"""
        found = 0
        for rel in GOLDEN_CLAIM_DOCS:
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            found += len(re.findall(r"\d+\s*条\s*\*{0,2}\s*golden", text))
        assert found >= 2, f"抽取到的 golden 条数声明过少（{found}），正则可能失效"


class TestDocumentedGraphNodeCount:
    """主图节点数必须在代码注释与设计文档里与真实注册数一致。"""

    def test_lg_builder_comment_matches_registered_nodes(self):
        real = _declared_add_node_count()
        assert real > 0, "build_graph 里没数到 add_node 调用，正则/结构变了"
        text = (PROJECT_ROOT / "agentix/orchestration/langgraph/lg_builder.py").read_text(
            encoding="utf-8"
        )
        claims = {int(m) for m in re.findall(r"只注册了?\s*(\d+)\s*个", text)}
        claims |= {int(m) for m in re.findall(r"注册下列\s*(\d+)\s*个节点", text)}
        assert claims, "未在 lg_builder.py 中找到节点数声明，正则可能失效"
        assert claims == {real}, (
            f"lg_builder.py 声称注册 {sorted(claims)} 个节点，实际 add_node 调用 {real} 次"
        )

    def test_design_doc_node_count_matches_code(self):
        real = _declared_add_node_count()
        text = (
            PROJECT_ROOT / "docs/design/orchestration-rationale.md"
        ).read_text(encoding="utf-8")
        claims = {int(m) for m in re.findall(r"只注册了?\s*(\d+)\s*个节点", text)}
        assert claims, "设计文档里未找到节点数声明，正则可能失效"
        assert claims == {real}, (
            f"orchestration-rationale.md 声称 {sorted(claims)} 个节点，"
            f"实际 add_node 调用 {real} 次"
        )


class TestDocumentedTestCount:
    """README 的测试数必须**自证是「某次实测」**，而不是一句无来源的常数。

    为什么不直接断言「声称数 == 真实 passed 数」：在测试里拿到真实 passed 数需要
    递归调用 pytest（或解析运行产物），既慢又脆；而参数化会让「数 def test_」
    严重低估。所以这里守一个能 100% 稳定成立、且真正有用的性质：

    **该数字必须带实测日期。** 没有日期的「1388+ passed」读起来像一条永久成立
    的保证；带上「2026-09-22 实测」就明确是一条会过期的测量值，改动它的人被
    提醒要重新实测并更新日期。这正是本文档腐烂样本（1388+ → 实际 1500+）的
    治理方式：不追求自动对齐，而追求「过期可见」。
    """

    def test_readme_passed_count_is_labelled_as_measurement(self):
        text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"^.*\d+\s*passed.*$", text, re.M)
        assert match, "README 中未找到 `N passed` 声明，正则可能失效"
        line = match.group(0)
        assert re.search(r"\d{4}-\d{2}-\d{2}", line), (
            f"README 的测试数缺少实测日期，读起来像永久保证：{line.strip()!r}\n"
            f"请改为「2026-09-22 实测」形式，并在重新实测后同步更新。"
        )
        assert "Python 3." in line, (
            f"README 的测试数应同时标注实测的解释器版本（同一份用例在不同 Python "
            f"上结果可能不同）：{line.strip()!r}"
        )


class TestTestCountHasSingleSource:
    """测试数只许写在 README 一处——操作性文档不得复述。

    **为什么值得单独一条守卫。** 2026-09 实测同一时点有三个数：
    README 写 1557、`docs/guides/testing.md` 写 1380、
    `docs/runbooks/release-and-rollback.md` 写 1380，而真实值是 1784。
    三个数、零个对——**这不是"忘了更新一处"，而是"同一个事实写了三遍"的必然结果**：
    只要允许复述，就总有一处会先腐烂，而且腐烂的那处会被当成权威引用。

所以治理方式不是"把三个数都改对"（下次还会漂），而是**只留一处真源**：
 README §11。其余文档改为指向它。

    **范围界定（重要）**：只约束「操作性/当前状态」类文档
    （`docs/guides/`、`docs/runbooks/`、`docs/architecture/`）。
    `CHANGELOG.md` 与 `docs/design/` 是**历史记录**——里面写的是
    "当时那个提交上有多少条用例"，是**事实陈述**，必须保留、不得改写，
    故不在本守卫范围内。
    """

    #: 操作性文档（当前状态类）
    OPERATIONAL_GLOBS = (
        "docs/guides/*.md",
        "docs/runbooks/*.md",
        "docs/architecture/*.md",
    )

    #: 只有 3 位以上才算"测试数声明"——避免误伤「8 passed」这类局部计数
    _CLAIM_RE = re.compile(r"\d{3,}\s*passed")

    def test_only_readme_declares_the_suite_size(self):
        offenders = []
        for pattern in self.OPERATIONAL_GLOBS:
            for path in sorted(PROJECT_ROOT.glob(pattern)):
                text = path.read_text(encoding="utf-8")
                for i, line in enumerate(text.splitlines(), start=1):
                    if self._CLAIM_RE.search(line):
                        rel = path.relative_to(PROJECT_ROOT).as_posix()
                        offenders.append(f"{rel}:{i}  {line.strip()[:100]}")
        assert not offenders, (
            "以下操作性文档复述了测试总数。请改为指向 README §11——"
            "同一个事实写两处，必有一处先腐烂：\n  " + "\n  ".join(offenders)
        )

    def test_the_single_source_actually_declares_it(self):
        """防空真：真源里必须真的有这个数字，否则上面的断言只是「哪里都没有」。"""
        text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        assert self._CLAIM_RE.search(text), (
            "README 里没有 `N passed` 声明——那么「操作性文档不许复述」就失去了意义"
        )

    def test_guard_scope_is_not_empty(self):
        """防空真：glob 若写错，扫描集为空会让上面的断言永远绿。"""
        found = [
            p for pattern in self.OPERATIONAL_GLOBS
            for p in PROJECT_ROOT.glob(pattern)
        ]
        assert len(found) >= 5, (
            f"操作性文档只找到了 {len(found)} 个，glob 可能失效：{self.OPERATIONAL_GLOBS}"
        )
