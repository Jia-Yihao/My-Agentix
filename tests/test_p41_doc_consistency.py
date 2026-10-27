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
from urllib.parse import urlsplit

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = PROJECT_ROOT / "docs"

#: 含「配置键表格」的文档（行首为 `| \`KEY\``）
CONFIG_DOCS = [
    "docs/architecture/配置管理.md",
    "docs/architecture/运行时护栏.md",
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
#: `.github/` 同样排除：CI 配置由 tests/test_ci_gate_contract.py 与
#: tests/test_p46_toolchain_parity.py 按步骤逐条守（比"路径存在"强得多），
#: 而 CHANGELOG 里的 `.github/...` 提及多为历史陈述，交给通用路径守卫只会误报。
_PATH_PREFIX = (
    r"(?:agentix|tests|evals|scripts|kb_ingest|web|docs|config|skills)"
)

#: 扩展名白名单。`jsonl` 刻意**不**进这里：文档里的 jsonl 提及多是数据文件名或命令行
#: 示例，交给通用路径守卫只会把"数据没录制"和"文档腐烂"混成同一条红。
#: 2026-10-26 起 `evals/baseline_predictions.jsonl` 已入库，但它由**内容守卫**看住而不是
#: 由这里——`tests/test_ci_gate_contract.py::test_committed_predictions_cover_the_golden_set_exactly`
#: 核的是"存在 + 条数与金标同源 + id 唯一且集合相等"，比"路径存在"强得多。
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
    # 2026-10-26 评测集下线：说明书 docx 是 2026-10-22 的**快照**（README 声明「以 md 为准」），
    # 其中的门禁基线路径是当时的事实陈述。改 docx 等于改快照，而快照的现值由 md 负责。
    "evals/baselines/quality.json",
    # 2026-10-27 文档分层：原 overview.md 合并进 README 后移入 docs/archive/（现名 项目全景-归档快照.md）。
    # docx 是同一份快照，里面写的仍是当时的位置；改它等于改快照，故按同一条口径豁免（现值由 README §14 负责）。
    # 下面这条字符串刻意保持旧路径：它匹配的是 docx 快照里的原文，不是仓库现存的文件。
    "docs/architecture/overview.md",
}

#: md 中**有意保留**的已删除路径（同 DOCX_INTENTIONAL_REFS 的道理，但只给**历史陈述**用）。
#:
#: 2026-10-26 评测集下线后，三条路径从仓库消失：门禁基线、录制预测、以及两个只服务
#: 金标集的守卫文件。历史文档（ENGINEERING_LOG / 迁移规划 / 优化路线）里提到它们是
#: **事实陈述**——「当时有这个文件、当初守的是这条」——改写它们等于伪造历史；
#: 而现时态文档（README / PROJECT_SPEC / evals/README）必须改写成「评测集已下线」。
#: 两条纪律的分工靠本清单划线：现时态文档里出现这些路径仍会红（不在清单豁免范围之外
#: 的引用一律照红），历史文档里出现则放行。
MD_INTENTIONAL_REFS = {
    "evals/baselines/quality.json",
    "evals/baseline_predictions.jsonl",
    "evals/legal_golden.jsonl",
    "tests/test_p124_quality_baseline.py",
    "tests/test_p120_golden50_retrieval_summary.py",
}

#: **历史陈述类**文档前缀：只有这些文档里才允许出现上面的已删除路径。
#:
#: 判据是「这句话在讲过去发生过什么」还是「在讲现在是什么样」：
#: 迁移规划 / 优化路线 / 工程日志 / 评测录制 runbook 属于前者——它们记录
#: 「当时有这个文件、当初守的是这条」，改写等于伪造历史；现时态文档（README、
#: PROJECT_SPEC、guides、LIMITATIONS、architecture/**）属于后者，必须改写成
#: 「评测集已下线」，出现旧路径一律照红。
HISTORY_DOC_PREFIXES = (
    "docs/migration/",
    "docs/design/",
    "docs/工程口径日志.md",
    "docs/runbooks/评测基线录制.md",
)


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
        rel = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        history = rel.startswith(HISTORY_DOC_PREFIXES)
        text = path.read_text(encoding="utf-8")
        for m in _MD_PATH_RE.finditer(text):
            ref = m.group(1)
            if _ABBREV_RE.search(ref):
                continue
            if history and ref in MD_INTENTIONAL_REFS:
                continue
            found.setdefault(ref, rel)
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
            "KB_SCOPE_DESCRIPTION",
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
            "HITL_PENDING_PERSIST_ENABLED",
            "CONTEXT_PRECOMPACT_REINJECT_ENABLED",
            "MEMORY_PROFILE_SCOPE_ENABLED",
            "TEXT2SQL_DRY_RUN_ENABLED",
            "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS",
            "TEXT2SQL_DATABASE_URL",
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
            # LG-1-3：三层退避与原生默认对齐开关
            "RETRY_BACKOFF_ALIGNED",
            # LG-1-1：只读节点原生重试开关
            "NODE_RETRY_ENABLED",
            # LG-3-2：主图节点原生超时（0 = 不传 timeout 形参）
            "NODE_TIMEOUT_SECONDS",
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
            # Phase S（§24.3 T0 执行边界加固）
            "TOOL_SYNC_OFFLOAD_ENABLED",
            "TOOL_HANDLER_CONTRACT_ENABLED",
            "TOOL_DUPLICATE_REGISTRATION_STRICT",
            "EXECUTION_SANDBOX_ENABLED",
            # Phase S·T2 ⑮（沙箱阶梯第三档）
            "TOOL_SANDBOX_SUBPROCESS_ENABLED",
            "TOOL_SANDBOX_SUBPROCESS_UID",
            "TOOL_SANDBOX_SUBPROCESS_ENV_EXTRA_KEYS",
            # code exec（运行时写代码执行任务）
            "CODE_EXEC_ENABLED",
            "CODE_EXEC_MAX_ROUNDS",
            "CODE_EXEC_TIMEOUT_S",
            "MCP_SUBPROCESS_ENV_SCRUB",
            "MCP_SUBPROCESS_ENV_EXTRA_KEYS",
            "DB_ECHO",
            "UVICORN_RELOAD",
            "SETTINGS_AUTHZ_ENABLED",
            # Phase K（checkpointer fail-closed 开关）
            "CHECKPOINTER_FAIL_CLOSED",
            # LG-2-6：图执行 durability（sync / async / exit，默认 async）
            "GRAPH_DURABILITY",
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
            "docs/architecture/分层架构与依赖规则.md",
            "docs/architecture/接口与契约.md",
            "docs/architecture/扩展机制.md",
            "docs/architecture/配置管理.md",
            "docs/architecture/运行时护栏.md",
            "docs/architecture/adr/README.md",
            "docs/architecture/adr/0001-分层架构与端口抽象.md",
            "docs/architecture/adr/0002-注册表驱动的插件机制.md",
            "docs/architecture/adr/0003-配置声明单一事实来源.md",
            "docs/版本管理策略.md",
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
        text = (PROJECT_ROOT / "docs/architecture/分层架构与依赖规则.md").read_text(encoding="utf-8")
        assert "零例外" in text or "无（原唯一例外已清除）" in text


def _declared_add_node_count() -> int:
    """从 build_graph 函数体里数 builder.add_node(...) 调用次数。"""
    text = (PROJECT_ROOT / "agentix/orchestration/langgraph/lg_builder.py").read_text(
        encoding="utf-8"
    )
    body = text.split("def build_graph(", 1)[1].split("\ndef ", 1)[0]
    return body.count("builder.add_node(")


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
            PROJECT_ROOT / "docs/design/编排方案选型论证.md"
        ).read_text(encoding="utf-8")
        claims = {int(m) for m in re.findall(r"只注册了?\s*(\d+)\s*个节点", text)}
        assert claims, "设计文档里未找到节点数声明，正则可能失效"
        assert claims == {real}, (
            f"编排方案选型论证.md 声称 {sorted(claims)} 个节点，"
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
    README 写 1557、`docs/guides/测试分层策略.md` 写 1380、
    `docs/runbooks/发布与回滚.md` 写 1380，而真实值是 1784。
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


# ---------------------------------------------------------------------------
# 交付面守卫（2026-10-23 扩）
#
# 前面几节拦的是路径腐烂、配置键腐烂、golden/节点/测试数腐烂。2026-10-23 把 README
# 与实现逐条对账，又挖出一批，**没有一条落在既有守卫的覆盖面里**：
#   - 徽章写 Neo4j 5.27 / Milvus 2.3，compose 与 Dockerfile 实际钉 5.18 / v2.4.17；
#   - 「web 是第 13 个 service」，真实 15 个且 web 排最后；
#   - 「CI 阻塞性能冒烟」，真实性能门在 perf-baseline.yml 按日巡检、不阻塞 PR；
#   - 「PROJECT_SPEC 23 章」，真实 25 章（§0–§24）；
#   - 「按 `difficulty`/`source` 分层」，golden 行里根本没有这两个字段；
#   - 目录把「参与贡献」排在 §16 之后，正文却夹在 §14 与 §15 之间；
#   - 六条 GitHub 链接指向未发布的远端（实测 404），还随 wheel 元数据分发出去。
#
# 共同点：**事实源不在 Python 里**——在 compose、在工作流 YAML、在数据行的键集、
# 在文档自身的结构里。所以每条都要显式把那个源读出来比对；且凡"没有声明就是绿"的
# 否定式断言，都配一条正面锚点，防止守卫悄悄退化成永远绿。
# ---------------------------------------------------------------------------

COMPOSE_PATH = PROJECT_ROOT / "docker-compose.yml"
CI_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE_PATH = PROJECT_ROOT / "Dockerfile"
PROJECT_SPEC_PATH = DOCS_ROOT / "项目实现手册.md"

#: 本仓库当前没配任何 git remote，该 URL 实测 404。
#: 当前态文档（含随 wheel 分发的 pyproject 元数据）不许再出现它。
UNPUBLISHED_REMOTE = "github.com/Jia-Yihao/Legelbot"

#: 当前态文档（`CHANGELOG.md`、`docs/design/`、`docs/migration/` 是历史记录，不在内）
CURRENT_STATE_ROOT_DOCS = [
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "pyproject.toml",
]
CURRENT_STATE_DOC_GLOBS = (
    "docs/architecture/*.md",
    "docs/guides/*.md",
    "docs/runbooks/*.md",
)

_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

#: shields.io 惯例：徽章 alt 文本写「名字-版本号」，例如 `![Neo4j-5.18](…)`
_BADGE_VERSION_RE = re.compile(
    r"!\[[^\]]*?([A-Za-z][A-Za-z0-9_-]*)-(\d+(?:\.\d+)+)[^\]]*\]\("
)


def _readme() -> str:
    return (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")


def _compose_services() -> List[str]:
    """按文件顺序抽出 compose `services:` 下的顶层键（唯一事实源＝compose 本身）。"""
    services: List[str] = []
    in_services = False
    for line in COMPOSE_PATH.read_text(encoding="utf-8").splitlines():
        if line == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith((" ", "#")):
            break  # 下一个顶层块（networks / volumes）
        hit = re.match(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$", line)
        if hit:
            services.append(hit.group(1))
    return services


def _image_pins() -> Dict[str, str]:
    """compose 的 `image:` 与 Dockerfile 的 `FROM` 钉死的镜像版本（键＝镜像名）。"""
    pins: Dict[str, str] = {}
    for line in COMPOSE_PATH.read_text(encoding="utf-8").splitlines():
        hit = re.match(r"\s*image:\s*([^\s#]+)", line)
        if not hit:
            continue
        ref = hit.group(1).split("/")[-1]
        if ":" in ref:
            name, tag = ref.split(":", 1)
            pins.setdefault(name.lower(), tag.lstrip("v"))
    for hit in re.finditer(
        r"^FROM ([a-z0-9._-]+):([0-9][^ \t]*)",
        DOCKERFILE_PATH.read_text(encoding="utf-8"),
        re.M,
    ):
        pins.setdefault(hit.group(1).lower(), hit.group(2))
    return pins


def _ci_gate_needs() -> List[str]:
    text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    block = re.search(
        r"^  ci-gate:\n(?:.*\n)*?    needs:\n((?:      - [a-z0-9-]+\n)+)", text, re.M
    )
    assert block, "ci.yml 里没有 ci-gate.needs——工作流结构变了，守卫需同步"
    return re.findall(r"^      - ([a-z0-9-]+)$", block.group(1), re.M)


class TestDocumentedComposeServices:
    def test_service_count_and_web_ordinal_match_compose(self):
        services = _compose_services()
        assert len(services) >= 10, (
            f"compose 服务只抽出 {len(services)} 个，解析可能失效：{services}"
        )
        hit = re.search(r"compose 共 (\d+) 个 service，web 是第 (\d+) 个", _readme())
        assert hit, (
            "README 里找不到「compose 共 N 个 service，web 是第 M 个」声明——"
            "要么措辞变了（守卫需同步），要么这条口径被人删了"
        )
        assert int(hit.group(1)) == len(services), (
            f"README 声称 {hit.group(1)} 个 service，compose 实际 {len(services)} 个：{services}"
        )
        assert "web" in services, "compose 里没有 web service，README 的序数声明无处对照"
        assert int(hit.group(2)) == services.index("web") + 1, (
            f"README 说 web 是第 {hit.group(2)} 个，compose 里它在第 "
            f"{services.index('web') + 1} 位：{services}"
        )

    def test_quickstart_named_services_exist(self):
        """§10 第 5 步点名的 service 必须都在 compose 里，否则命令直接报错。"""
        hit = re.search(r"docker compose up -d ([a-z0-9_ ]+)\n", _readme())
        assert hit, "README §10 第 5 步的点名启动命令变了，守卫需同步"
        named = hit.group(1).split()
        missing = sorted(set(named) - set(_compose_services()))
        assert not missing, f"README 点名的 service 不在 compose 里：{missing}"


class TestDocumentedBadgeVersions:
    def test_badge_version_labels_match_pinned_images(self):
        badges = _BADGE_VERSION_RE.findall(_readme())
        if not badges:
            # 摘掉徽章必须留下「为什么没有」的说明，否则只是把腐烂换成了空白页
            assert "未发布到公共远端" in _readme(), (
                "README 既没有状态徽章，也没有托管状态说明——"
                "要么恢复徽章（版本须与镜像钉版本一致），要么写清为何不挂"
            )
            return
        pins = _image_pins()
        stale = []
        for name, ver in badges:
            pin = pins.get(name.lower())
            if pin and not pin.startswith(ver):
                stale.append(f"{name}: 徽章写 {ver}，compose/Dockerfile 实际钉 {pin}")
        assert not stale, "徽章版本与镜像钉版本不一致：\n  " + "\n  ".join(stale)

    def test_image_pins_are_actually_collected(self):
        """防空真：钉版本抽取若失效，上一条的「不一致」检查就永远绿。"""
        pins = _image_pins()
        assert {"neo4j", "milvus", "postgres"} <= set(pins), (
            f"钉版本抽取失败，只拿到 {sorted(pins)}"
        )

    def test_badge_regex_finds_a_known_shape(self):
        """防空真：正则写错时上一条会「找不到徽章」而静默通过。"""
        sample = "[![Neo4j-5.18](https://img.shields.io/x)](https://example.com)"
        assert _BADGE_VERSION_RE.findall(sample) == [("Neo4j", "5.18")]


class TestDocumentedCIGateJobs:
    def test_readme_job_list_equals_workflow_needs(self):
        needs = _ci_gate_needs()
        line = next((s for s in _readme().splitlines() if "job 全绿" in s), None)
        assert line, "README 里没有 ci-gate job 清单声明，正则/措辞变了"
        hit = re.search(r"要求([一二三四五六七八九十]+)个\s*job 全绿：(.+)", line)
        assert hit, f"ci-gate 声明句式变了：{line[:60]!r}"
        claimed_count = _CN_DIGITS.get(hit.group(1))
        assert claimed_count == len(needs), (
            f"README 说「{hit.group(1)} 个 job」，ci.yml 的 needs 有 {len(needs)} 项：{needs}"
        )
        claimed = re.findall(r"`([a-z][a-z0-9-]*)`（", hit.group(2))
        assert claimed == needs, (
            f"README 的 job 清单与 ci.yml 的 needs 不等：\n"
            f"  README: {claimed}\n  ci.yml: {needs}"
        )

    def test_perf_gate_not_described_as_merge_blocker(self):
        """性能门的真实归属是 perf-baseline.yml（每日巡检），不是合入阻塞。

        腐烂原文是「CI 阻塞性能冒烟」/「阻塞性能冒烟」——读者据此会以为 PR 会被
        p95 劣化挡住，实际不会。正面锚点（README 必须写明 perf-baseline 的归属）
        同条断言，防止整段被删后本检查变成空转。
        """
        banned = ("阻塞性能冒烟", "CI 阻塞", "性能冒烟进")
        offenders: List[str] = []
        targets = [PROJECT_ROOT / "README.md"]
        targets += [p for g in CURRENT_STATE_DOC_GLOBS for p in sorted(PROJECT_ROOT.glob(g))]
        for path in targets:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(b in line for b in banned):
                    offenders.append(f"{rel}:{i}  {line.strip()[:100]}")
        assert not offenders, (
            "以下文档把性能冒烟写成合入阻塞（真实形态：perf-baseline.yml 每日巡检、"
            "不阻塞 PR）：\n  " + "\n  ".join(offenders)
        )
        assert "perf-baseline" in _readme(), (
            "README 必须保留性能门归属的正向说明，否则本条守卫只是在等下一次腐烂"
        )


class TestDocumentedSpecChapters:
    _CLAIM_RE = re.compile(r"共 (\d+) 章（§0–§(\d+)）")

    def _real_chapters(self) -> List[int]:
        text = PROJECT_SPEC_PATH.read_text(encoding="utf-8")
        return sorted(int(n) for n in re.findall(r"^## (\d+)\.", text, re.M))

    def test_spec_chapter_numbers_are_contiguous(self):
        nums = self._real_chapters()
        assert nums, "PROJECT_SPEC 里没数到编号章节，正则可能失效"
        assert nums == list(range(len(nums))), (
            f"PROJECT_SPEC 章节编号不连续（{nums}）——「共 N 章（§0–§M）」的写法依赖连续编号"
        )

    def test_chapter_count_claims_match_spec(self):
        nums = self._real_chapters()
        total, last = len(nums), nums[-1]
        found = 0
        stale: List[str] = []
        # 只扫「现行」文档：`docs/archive/**` 是快照，里面的章数/结构陈述是当时的事实，
        # 不随现行章节数变化（2026-10-27 起 项目全景-归档快照.md 归档，故从本清单移除）。
        for rel in ("README.md",):
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            for hit in self._CLAIM_RE.finditer(text):
                found += 1
                if int(hit.group(1)) != total or int(hit.group(2)) != last:
                    stale.append(
                        f"{rel}: 声称共 {hit.group(1)} 章（§0–§{hit.group(2)}），"
                        f"真实 {total} 章（§0–§{last}）"
                    )
        assert found >= 1, "没抽到任何章数声明，正则可能失效"
        assert not stale, "说明书章数已腐烂：\n  " + "\n  ".join(stale)


class TestReadmeTocMatchesBody:
    def _toc_entries(self) -> List[str]:
        block = re.search(r"^## 目录\n(.*?)\n---", _readme(), re.M | re.S)
        assert block, "README 里找不到「## 目录」块"
        return re.findall(r"^- \[([^\]]+)\]\(", block.group(1), re.M)

    def test_toc_order_equals_body_heading_order(self):
        toc = self._toc_entries()
        assert len(toc) >= 15, f"目录只抽到 {len(toc)} 项，正则可能失效"
        body = [
            h for h in re.findall(r"^## (.+)$", _readme(), re.M) if h.strip() != "目录"
        ]
        assert toc == body, (
            "目录与正文标题顺序不一致（读者按目录跳转会落到错误的章节）：\n"
            f"  目录: {toc}\n  正文: {body}"
        )

    def test_numbered_sections_are_monotonic(self):
        nums = [
            int(m.group(1))
            for m in re.finditer(r"^## (\d+)\.", _readme(), re.M)
        ]
        assert nums == sorted(nums), f"README 章节编号不是单调递增：{nums}"


class TestNoUnpublishedRemoteLinks:
    def test_current_state_docs_do_not_link_the_unpublished_remote(self):
        targets = [PROJECT_ROOT / rel for rel in CURRENT_STATE_ROOT_DOCS]
        targets += [
            p for glob in CURRENT_STATE_DOC_GLOBS for p in sorted(PROJECT_ROOT.glob(glob))
        ]
        offenders = [
            p.relative_to(PROJECT_ROOT).as_posix()
            for p in targets
            if UNPUBLISHED_REMOTE in p.read_text(encoding="utf-8")
        ]
        assert not offenders, (
            f"当前态文档仍在引用未发布的远端 {UNPUBLISHED_REMOTE}（实测 404）：\n  "
            + "\n  ".join(offenders)
            + "\n发布到公共远端后，再按 README §10 的注释回填链接与徽章。"
        )

    def test_history_still_holds_it(self):
        """防空真：豁免面必须真的含该串，否则上一条等于什么都没拦。"""
        assert UNPUBLISHED_REMOTE in (
            PROJECT_ROOT / "CHANGELOG.md"
        ).read_text(encoding="utf-8"), (
            "CHANGELOG 里已查不到该 URL——豁免面前提消失，"
            "请把上一条的空转风险一并处理（改成正向锚点或扩大扫描面）"
        )


class TestDocumentedCheckpointShard:
    """checkpoint 分库口径守卫（2026-10-23 真 Redis Stack 实测引入）。

    背景：R3.2 把 `REDIS_CHECKPOINT_URL` 放在 db2，而 RediSearch 只能在 db0 建索引——
    真实后果是 checkpointer **从未激活过**（静默降级 MemorySaver），而当时的两条守卫
    一条只断言 URL 以 `/2` 结尾、一条只在源码里 grep 字符串，全套件 4700+ 条全绿。
    本类把「文档说的分库」和「代码默认分库」钉在同一个事实源上，防止两侧各自漂移。

    扫描面只取**当前态**文档；`docs/design/` 与 CHANGELOG 是历史记录（按
    `TestTestCountHasSingleSource` 的同一范围界定），保留"当时那样设计"的原文，
    靠文末的修正注记说明，不参与本守卫。
    """

    #: 当前态文档扫描面
    SCOPE = ["README.md", "docs/项目实现手册.md"]

    _DB2_RE = re.compile(r"db\s*2(?!\d)|6379/2")

    #: 允许出现 db2 的语境标记——只有「明确标为历史」或「在陈述报错」时才算合法。
    #: 2026-10-23 写这条守卫时，README 里那句 `已改到 db2（与 cache 分库）` 正是漏网之鱼：
    #: 它谈的是分库却没出现 checkpoint 一词，也没标注历史，属于真·错误陈述。
    #: 故扫描词除 checkpoint 外还收「分片 / 分库」，历史表述必须显式带这些标记。
    _HISTORY_MARKERS = ("原先", "曾经", "曾用", "曾把", "历史", "已修正", "db != 0", "非 0")

    _SCOPE_WORDS = ("checkpoint", "REDIS_CHECKPOINT_URL", "分片", "分库")

    def _lines(self):
        for rel in self.SCOPE:
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), start=1):
                low = line.lower()
                if any(word.lower() in low for word in self._SCOPE_WORDS):
                    yield rel, i, line

    def test_no_current_doc_puts_checkpoint_on_nonzero_db(self):
        offenders = [
            f"{rel}:{i}: {line.strip()[:110]}"
            for rel, i, line in self._lines()
            if self._DB2_RE.search(line)
            and not any(marker in line for marker in self._HISTORY_MARKERS)
        ]
        assert not offenders, (
            "当前态文档把 LangGraph checkpoint 分片写成非 0 库（RediSearch 只能在 db0 "
            "建索引，写成 db2 会让读者照抄后静默失去会话持久化）；"
            "确属历史表述请带「原先/曾用/已修正」标记：\n  " + "\n  ".join(offenders)
        )

    def test_code_default_agrees_with_docs(self):
        """代码默认库号必须与文档口径同源，且只能是 0。"""
        from agentix.config import settings

        default = type(settings).model_fields["REDIS_CHECKPOINT_URL"].default
        tail = (urlsplit(default).path or "/0").lstrip("/") or "0"
        assert tail == "0", f"代码默认 REDIS_CHECKPOINT_URL={default!r} 不在 db0"

    def test_readme_keeps_the_measured_error_string_as_anchor(self):
        """防空转：README 必须留着那条实测报错，作为"为什么是 db0"的证据锚点。"""
        assert "Cannot create index on db != 0" in _readme(), (
            "README §10 丢了实测报错串——那条降级说明就退回了'听说的'而不是'测过的'"
        )

    def test_scope_actually_contains_checkpoint_lines(self):
        """防空真：扫描面若一条相关行都没有，前两条等于没跑。"""
        assert sum(1 for _ in self._lines()) >= 5, (
            f"扫描面里 checkpoint 相关行太少，SCOPE 可能失效：{self.SCOPE}"
        )


class TestDocumentedMetricLabels:
    """文档里的 PromQL 只准用**声明过**的标签名（2026-10-23 实跑暴露）。

    背景：把 runbook 的三条 PromQL 真跑一遍才发现两处写错——
    ``llm_tokens_total{direction=...}``（线上标签其实是 ``type``；``direction`` 只是
    ``record_llm_tokens()`` 的**形参名**，从未成为标签）和
    ``http_requests_total by (path)``（真实标签是 ``endpoint``）。第二条尤其阴：
    ``by (path)`` 不报错，只把所有序列折成一个空标签 ``{}``，看着"有数"却是全量混合。
    本守卫把「文档用到的标签」钉回 ``agentix/observability/metrics.py`` 的声明集。

    判定单位是**代码单元**（围栏块 / 反引号片段）而不是行：见 ``_code_units`` 里记的
    那次自咬——一行里同时提两个指标时，按行解析会把对其中一个正确的 ``by (type)``
    拿去跟另一个的标签集比。边界由 ``test_unit_scoping_is_narrow_but_not_blind`` 钉住。
    """

    #: 当前态、且真的写了 PromQL 的文档
    SCOPE = [
        "README.md",
        "docs/runbooks/性能与成本采数.md",
        "docs/runbooks/值班响应手册.md",
    ]

    _METRICS_SOURCE = "agentix/observability/metrics.py"

    #: 指标声明：Counter/Gauge/Histogram("name", "doc", ["label", ...])
    _DECL_RE = re.compile(
        r"(?:Counter|Gauge|Histogram)\(\s*\"(?P<name>[a-zA-Z0-9_:]+)\"\s*,"
        r"[^()]*?\[(?P<labels>[^\]]*)\]",
        re.S,
    )

    #: 文档侧的 `metric{...}` 用法（PromQL 标签匹配 / `by (...)` 里的标签集）
    _USAGE_RE = re.compile(r"\b(?P<name>[a-z][a-z0-9_:]*)\{(?P<body>[^{}]*)\}")

    #: 聚合分桶子句：``by (…)`` / ``without (…)``
    _BY_RE = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")

    #: 直方图/摘要的派生序列后缀与合成标签
    _SUFFIXES = ("_bucket", "_count", "_sum")
    _SYNTHETIC_LABELS = {"le", "quantile"}

    def _declared(self) -> Dict[str, Set[str]]:
        text = (PROJECT_ROOT / self._METRICS_SOURCE).read_text(encoding="utf-8")
        declared = {}
        for m in self._DECL_RE.finditer(text):
            labels = {t.strip().strip("\"'") for t in m.group("labels").split(",")}
            declared[m.group("name")] = {t for t in labels if t}
        return declared

    def _base(self, name: str, declared: Dict[str, Set[str]]):
        """把文档里的序列名还原成声明的指标名；不是已声明指标则返回 None（不猜）。"""
        if name in declared:
            return name
        for suf in self._SUFFIXES:
            if name.endswith(suf) and name[: -len(suf)] in declared:
                return name[: -len(suf)]
        return None

    _INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

    def _code_units(self, text: str):
        """把文档切成「可执行代码单元」：围栏块整块一个单元，正文里每个反引号片段各一个。

        为什么按单元而不是按行：本守卫初版按行解析 ``by (…)`` 属于哪个指标，结果把
        自己作者写的说明文判成违规——一句话里同时提到 ``llm_tokens_total`` 和
        ``http_requests_total`` 时，``by (type)``（对前者是**正确**标签）会拿去跟后者的
        标签集比。按单元切之后，指标名必须出现在**同一个**代码单元里才参与判定，
        说明文里孤立引用的 ``by (path)`` 之类不再被误判，可执行片段照旧全查。
        """
        units = []
        in_fence = False
        fence_start = 0
        fence_lines = []
        for i, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("```"):
                if not in_fence:
                    in_fence, fence_start, fence_lines = True, i, []
                else:
                    in_fence = False
                    units.append((fence_start, "\n".join(fence_lines)))
                continue
            if in_fence:
                fence_lines.append(line)
                continue
            units.extend((i, m.group(1)) for m in self._INLINE_CODE_RE.finditer(line))
        if in_fence:  # 未闭合围栏也要判，别静默丢掉半截 PromQL
            units.append((fence_start, "\n".join(fence_lines)))
        return units

    def _unit_usages(self, unit: str, declared: Dict[str, Set[str]]):
        """扫一个代码单元 → ``[(指标名, 序列名|'by', 标签, 允许标签集), ...]``。"""
        for m in self._USAGE_RE.finditer(unit):
            base = self._base(m.group("name"), declared)
            if base is None:
                continue
            for token in m.group("body").split(","):
                label = token.split("=")[0].strip().strip("\"'")
                if label:
                    yield base, m.group("name"), label, declared[base]
        # `by (...)` / `without (...)` 分桶同样只能是声明过的标签：
        # 实测那条 `by (path)` 就是**不报错但静默折空**的典型。
        present = [
            name for name in declared if re.search(r"\b" + re.escape(name) + r"\b", unit)
        ]
        if not present:
            return
        allowed = set().union(*(declared[n] for n in present))
        for group in self._BY_RE.finditer(unit):
            for token in group.group(1).split(","):
                label = token.strip().strip("\"'")
                if label:
                    yield present[0], "by", label, allowed

    def _usages(self):
        declared = self._declared()
        for rel in self.SCOPE:
            path = PROJECT_ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for line, unit in self._code_units(text):
                for base, series, label, allowed in self._unit_usages(unit, declared):
                    yield rel, line, base, series, label, allowed

    def test_docs_use_only_declared_metric_labels(self):
        offenders = []
        for rel, i, base, series, label, allowed in self._usages():
            if label in allowed or label in self._SYNTHETIC_LABELS:
                continue
            where = f'{series}{{{label}=…}}' if series != "by" else f"by ({label})"
            offenders.append(
                f"{rel}:{i}  {where}  —— {base} 的标签只有 {sorted(allowed)}"
            )
        assert not offenders, (
            "文档写了不存在的 Prometheus 标签（PromQL 不报错，只会静默把序列折空）：\n  "
            + "\n  ".join(offenders)
        )

    def test_scope_actually_contains_metric_usages(self):
        """防空真：扫描面里必须真的抓到已声明指标的标签用法。"""
        caught = {(rel, label) for rel, _, _, _, label, _ in self._usages()}
        assert len(caught) >= 3, (
            f"只抓到 {sorted(caught)}——SCOPE 或正则失效，上一条守卫等于没跑：{self.SCOPE}"
        )

    def test_the_two_measured_wrong_labels_stay_out(self):
        """正向锚点：本轮实测抓错的两个标签名，必须一个都不能再出现在扫描面里。"""
        banned = {"direction", "path"}
        hits = [
            f"{rel}:{i}  {label}"
            for rel, i, _, _, label, _ in self._usages()
            if label in banned
        ]
        assert not hits, f"已证伪的标签名又回来了：{hits}"

    def test_unit_scoping_is_narrow_but_not_blind(self):
        """窄化的边界：只砍掉"说明文里的孤立引用"，可执行片段照旧必须抓到。

        这条用例是本轮自己被守卫咬到之后加的——按行解析时，README 里一句同时提到两个
        指标的话会把对其中一个**正确**的 ``by (type)`` 判成错。改成按代码单元解析后，
        必须同时验证两件事：单元里带指标名的错标签仍能抓出（不是把守卫调哑），
        以及不带指标名的纯引用不再误报。
        """
        declared = self._declared()
        assert "http_requests_total" in declared and "llm_tokens_total" in declared
        assert "path" not in declared["http_requests_total"]
        assert "direction" not in declared["llm_tokens_total"]

        # 可执行形状（runbook 围栏里的真实句式）：错标签必须被抓出来
        caught = [u[2] for u in self._unit_usages(
            "sum(increase(http_requests_total[30m])) by (path)", declared)]
        assert caught == ["path"], caught

        # 花括号用法同样要抓出来
        caught = [u[2] for u in self._unit_usages(
            'llm_tokens_total{direction="prompt"}', declared)]
        assert caught == ["direction"], caught

        # 说明文里孤立引用（单元内没有指标名）：不判，否则文档不能谈论历史错误
        assert list(self._unit_usages("by (path)", declared)) == []
        assert list(self._unit_usages('{direction="prompt"}', declared)) == []

        # 正确写法在同一个单元里也必须放行（防"一律报错"式假绿）
        assert all(u[2] in u[3] for u in self._unit_usages(
            'llm_tokens_total{type="prompt"}', declared))
