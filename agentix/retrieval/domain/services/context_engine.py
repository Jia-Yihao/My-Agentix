"""R3-5 Context Engine（架构文档 §24-28 + 指南 v2.0 L1⑦）。

本模块是检索片段进入 Prompt 前的统一装配层，四件事：

1. **静态前缀版本化**：``STATIC_PREFIX`` 是 build_prompt 头部的常量文案，
   ``STATIC_PREFIX_VERSION`` 由 ``content_version`` 对其求 sha256——
   前缀文案任何改动都会让版本号变化，配合不变性测试保证
   「同一版本号 ⇒ 同一前缀文案」（Prompt 回放 / eval 归因的前提）。
2. **测试数据 authority 排序**（§26 Ranking）：按 ``source_authority_level``
   元数据把片段分成五档——测试数据正式文本 > 示例说明 > 处理机构案例 >
   二级资料 > 普通网页；同档保持检索分序（稳定排序），
   缺失/未知档案位为 0（fail-open：存量数据顺序不变）。
3. **Context Budget**（§27）：token 级贪心装配，超预算的低档片段被
   丢弃/截断；``budget_tokens<=0`` 表示不限额（默认，行为不变）。
4. **大文档隔离通道**（指南 L1⑦，复用 artifact_store）：单个片段超过
   阈值时全文外置到 ArtifactStore，Prompt 内只留预览 + artifact 句柄；
   ``isolate_threshold<=0`` 表示关闭（默认，行为不变——启用需配合
   取回通道，否则 LLM 看不到被隔离的正文）。

另提供 **召回溯源**（``provenance_enabled``）：每条进 Prompt 的片段前面加一行
``[S1] 文档名 · 第3页``，标号与来源随 meta 的 ``provenance`` 回传给调用方，
用于出答后把引用卡对齐到模型真正标过的片段。默认关 ⇒ 装配结果逐字不变。

另提供 **状态栏**（``build_status_bar``）：scope / 文档数 / 召回数 /
案件阶段等单行字段，随 Prompt 注入（指南 L1⑦ 状态栏字段上线）。

所有函数均为纯函数（隔离除外，副作用收敛在 ArtifactStore），无 IO。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping, Optional

from agentix.retrieval.domain.services.provenance import (
    format_block,
    provenance_entries,
    provenance_label,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. 静态前缀 + 版本化
# ---------------------------------------------------------------------------

# build_prompt 头部静态文案（与 chat_service.build_prompt 保持逐字一致）。
# 修改这里必须同步更新 STATIC_PREFIX_VERSION 的语义说明并在 CHANGELOG 记录。
STATIC_PREFIX = """你是 My Agentix，一个基于文档的助手。请严格基于下方文档内容回答问题。
"""

from agentix.observability.versions import content_version  # noqa: E402

#: 静态前缀的内容版本号（sha256 前 12 位）。前缀文案变化 ⇒ 版本号变化。
STATIC_PREFIX_VERSION = content_version(STATIC_PREFIX, prefix="ctx-")


# ---------------------------------------------------------------------------
# 2. token 估算（CJK-aware 启发式，非精确 tokenizer）
# ---------------------------------------------------------------------------

_CJK_THRESHOLD = 0x2E80  # CJK 部首/符号区起点；之上按 1 char ≈ 1 token 估


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 字符按 1 字 1 token，其余按 4 字符 1 token。

    只用于 Budget 装配的相对比较，不追求与具体 tokenizer 对齐。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) >= _CJK_THRESHOLD)
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


# ---------------------------------------------------------------------------
# 3. 测试数据 authority 排序（架构文档 §26：authority 维度）
# ---------------------------------------------------------------------------

AUTHORITY_LEVEL_KEY = "source_authority_level"

#: 档位表：数值越大越权威。未知/缺失 = 0（fail-open，稳定排序不改变顺序）。
AUTHORITY_TIERS: dict[str, int] = {
    # tier 5：测试数据正式文本
    "statute": 5,
    "law": 5,
    "法律": 5,
    "法律正式文本": 5,
    "正式文本": 5,
    # tier 4：示例说明
    "judicial_interpretation": 4,
    "judicial": 4,
    "司法解释": 4,
    # tier 3：处理机构案例
    "case": 3,
    "court_case": 3,
    "案例": 3,
    "法院案例": 3,
    "判例": 3,
    "裁判文书": 3,
    # tier 2：二级资料
    "secondary": 2,
    "二级资料": 2,
    "评论": 2,
    "教科书": 2,
    "专著": 2,
    # tier 1：普通网页
    "web": 1,
    "webpage": 1,
    "网页": 1,
    "普通网页": 1,
}

# 入库侧别名（kb_ingest/kb_service/services/legal_metadata.py 的 alias 集合）
_AUTHORITY_ALIASES = (
    "authority_level",
    "source_authority",
    "来源权威等级",
    "权威等级",
)


def authority_tier_of(meta: Mapping[str, Any] | None) -> int:
    """读取片段元数据的 authority 档位（0 = 未知/缺失）。"""
    if not meta:
        return 0
    raw = None
    for key in (AUTHORITY_LEVEL_KEY, *_AUTHORITY_ALIASES):
        value = (meta or {}).get(key)
        if value not in (None, ""):
            raw = value
            break
    if raw is None:
        return 0
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return max(0, min(5, int(raw)))
    text = str(raw).strip().lower()
    if text.isdigit():
        return max(0, min(5, int(text)))
    return AUTHORITY_TIERS.get(text, 0)


def rank_docs_by_authority(docs: list) -> list:
    """按 authority 档位降序稳定排序；档位缺失（0）的片段相对顺序不变。"""
    return sorted(docs, key=lambda d: -authority_tier_of(getattr(d, "metadata", None)))


# ---------------------------------------------------------------------------
# 4. Context Budget（架构文档 §27）
# ---------------------------------------------------------------------------

#: 截断比例的唯一字面量：domain 层不 import settings（分层约束），生产调用方
#: 由 ``Settings.CONTEXT_BUDGET_TRUNCATE_RATIO`` 传入，该键的默认值也取自这里，
#: 避免同一个数在代码与配置里各写一遍。
DEFAULT_BUDGET_TRUNCATE_RATIO = 0.9


def apply_context_budget(
    docs: list,
    *,
    budget_tokens: int = 0,
    truncate_ratio: float = DEFAULT_BUDGET_TRUNCATE_RATIO,
) -> tuple[list, dict]:
    """按 token 预算贪心装配片段，返回 (保留的 [(doc, text, truncated)] 列表, 统计)。

    - ``budget_tokens<=0``：不限额，全部原样保留（默认，行为不变）。
    - 预算内放不下整段时截断最后一条放得下的片段；之后的片段整体丢弃。
    - 返回元素为 ``(doc, text, truncated: bool)``，不修改原 doc 对象。
    """
    stats = {"budget_tokens": budget_tokens, "kept": len(docs), "dropped": 0, "truncated": 0}
    if budget_tokens is None or budget_tokens <= 0:
        return [(d, d.page_content, False) for d in docs], stats

    kept: list[tuple[Any, str, bool]] = []
    remaining = int(budget_tokens)
    for doc in docs:
        text = doc.page_content or ""
        cost = estimate_tokens(text)
        if cost <= remaining:
            kept.append((doc, text, False))
            remaining -= cost
            continue
        if remaining > 0:
            ratio = (remaining * float(truncate_ratio)) / max(cost, 1)
            cut = max(1, int(len(text) * ratio))
            kept.append((doc, text[:cut], True))
            stats["truncated"] += 1
            remaining = 0
    # dropped = 输入数 - 保留数（截断的那条计 kept，不计 dropped）
    stats["dropped"] = max(0, len(docs) - len(kept))
    stats["kept"] = len(kept)
    return kept, stats


# ---------------------------------------------------------------------------
# 5. 大文档隔离通道（指南 L1⑦，复用 artifact_store）
# ---------------------------------------------------------------------------

_ISOLATE_MARKER = "[本片段超长已外置（artifact_id={aid}），如需完整原文请按片段内容重新检索]"


def isolate_oversize_docs(
    docs: list,
    *,
    isolate_threshold: int = 0,
    trace_id: str = "-",
    source: str = "",
) -> tuple[list[tuple[Any, str, bool]], list[dict]]:
    """把超过阈值的片段全文外置到 ArtifactStore，Prompt 内只留预览 + 句柄。

    - ``isolate_threshold<=0``：关闭，全部原样（默认，行为不变）。
    - 外置失败自动降级为内联（offload_content 语义，永不丢内容）。
    - 返回 ([(doc, text, isolated)] 列表, 外置记录列表)。
    """
    if isolate_threshold is None or isolate_threshold <= 0:
        return [(d, d.page_content, False) for d in docs], []

    from agentix.application.services.artifact_store import offload_content

    out: list[tuple[Any, str, bool]] = []
    records: list[dict] = []
    for doc in docs:
        text = doc.page_content or ""
        if len(text) <= isolate_threshold:
            out.append((doc, text, False))
            continue
        result = offload_content(
            text,
            kind="context_doc",
            inline_threshold=1,  # 已过自检阈值，强制走外置分支
            summary=str((doc.metadata or {}).get("source", ""))[:200],
            trace_id=trace_id,
            rebuildable=True,  # 向量库里还有原文，可重建
            meta={"source": source, "chunk_id": (doc.metadata or {}).get("chunk_id")},
        )
        if result.is_offloaded and result.ref is not None:
            aid = result.ref.artifact_id
            out.append((doc, f"{result.ref.preview}\n{_ISOLATE_MARKER.format(aid=aid)}", True))
            records.append(
                {
                    "artifact_id": aid,
                    "chars": len(text),
                    "chunk_id": (doc.metadata or {}).get("chunk_id"),
                    "source": str((doc.metadata or {}).get("source", "")),
                }
            )
        else:
            # 降级为内联：内容不丢，只记诊断
            out.append((doc, text, False))
            records.append(
                {
                    "artifact_id": None,
                    "chars": len(text),
                    "degraded_reason": result.degraded_reason,
                    "chunk_id": (doc.metadata or {}).get("chunk_id"),
                }
            )
    return out, records


# ---------------------------------------------------------------------------
# 6. 统一装配管线（Builder：Ranking → Isolation → Budget → 拼接）
# ---------------------------------------------------------------------------

def context_doc_key(doc: Any) -> str:
    """装配层稳定键：优先 chunk_id，缺失时回退**原文**内容哈希。

    与检索层的去重口径一致；用原文而非隔离/截断后的文本，保证调用方能拿这个键
    把「实际进 Prompt 的片段」与 citations / hit_chunks / eval contexts 对齐。
    """
    meta = getattr(doc, "metadata", None) or {}
    chunk_id = meta.get("chunk_id")
    if chunk_id not in (None, ""):
        return str(chunk_id)
    text = getattr(doc, "page_content", "") or ""
    return "c:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def assemble_context(
    docs: list,
    *,
    ranking_enabled: bool = True,
    budget_tokens: int = 0,
    isolate_threshold: int = 0,
    trace_id: str = "-",
    source: str = "",
    separator: str = "\n\n",
    truncate_ratio: Optional[float] = None,
    provenance_enabled: bool = False,
) -> tuple[str, dict]:
    """检索片段 → Prompt 正文 的统一入口。

    返回 (context 字符串, meta 字典)。meta 除 ranking/budget/isolation 统计外，
    还回传 ``kept_doc_keys`` / ``kept_texts``——预算生效被裁剪时，调用方据此把
    引用列表与评测 contexts 收窄到模型真正看过的片段，避免「引用未读片段」。

    ``provenance_enabled=True`` 时再回传 ``provenance``（与 context 同序的
    ``[S1]`` 标号 + 来源记录）：片段正文前加一行标号，模型据此在答案里标注出处；
    标号自身也占 token，故先按标号开销预留预算再装配。``False``（默认）
    时 ``kept_texts`` 与 context 逐字等价于未引入溯源之前。
    """
    meta: dict[str, Any] = {
        "input_docs": len(docs),
        "context_prefix_version": STATIC_PREFIX_VERSION,
    }
    if not docs:
        return "", meta

    ordered = rank_docs_by_authority(docs) if ranking_enabled else list(docs)
    if ranking_enabled:
        tiers = [authority_tier_of(getattr(d, "metadata", None)) for d in ordered]
        meta["authority_tiers"] = tiers

    isolated, iso_records = isolate_oversize_docs(
        ordered, isolate_threshold=isolate_threshold, trace_id=trace_id, source=source
    )
    if iso_records:
        meta["isolated"] = iso_records

    # 隔离后文本可能变短，Budget 以隔离后的文本计（隔离记录的 preview 是 LLM 实际可见量）。
    # 每个临时 doc 带回原文 key，供调用方做引用/评测对齐。
    ordered_keys = [context_doc_key(d) for d in ordered]
    CtxDoc = type("_CtxDoc", (), {})
    wrapped: list = []
    for (doc, text, _), key in zip(isolated, ordered_keys):
        item = CtxDoc()
        item.page_content = text
        item.metadata = getattr(doc, "metadata", None)
        item._src_key = key
        wrapped.append(item)

    budget_kwargs: dict[str, Any] = {"budget_tokens": budget_tokens}
    if truncate_ratio is not None:
        budget_kwargs["truncate_ratio"] = truncate_ratio
    if provenance_enabled and budget_tokens and budget_tokens > 0:
        # 标号行同样进 Prompt：先按全部候选的标号开销预留预算，
        # 装配完才不会因为多了几行 `[S1] 文档名 · 第3页` 而超窗。
        overhead = sum(
            estimate_tokens(provenance_label(getattr(d, "metadata", None), index=i))
            for i, d in enumerate(ordered, 1)
        )
        budget_kwargs["budget_tokens"] = max(1, int(budget_tokens) - overhead)
    kept, budget_stats = apply_context_budget(wrapped, **budget_kwargs)
    meta["budget"] = budget_stats
    meta["kept_doc_keys"] = [getattr(d, "_src_key", "") for d, _, _ in kept]
    meta["kept_texts"] = [text for _, text, _ in kept]

    if provenance_enabled:
        entries = provenance_entries([d for d, _, _ in kept])
        meta["provenance"] = entries
        context = separator.join(
            format_block(entry, text)
            for entry, (_, text, _) in zip(entries, kept)
        )
    else:
        context = separator.join(text for _, text, _ in kept)
    meta["final_docs"] = len(kept)
    return context, meta


# ---------------------------------------------------------------------------
# 7. 状态栏（指南 L1⑦：状态栏字段上线）
# ---------------------------------------------------------------------------

def build_status_bar(**fields: Any) -> str:
    """把非空字段拼成单行状态栏：``【对话状态】k=v | k=v | ...``。

    None / 空串 / 空列表字段自动跳过；字段顺序即传入顺序。
    """
    pairs = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, dict)) and not value:
            continue
        pairs.append(f"{key}={value}")
    if not pairs:
        return ""
    return "【对话状态】" + " | ".join(pairs)
