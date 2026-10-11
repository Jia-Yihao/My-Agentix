"""Memory P0 验收评测（Phase 32 · M-P0，v2 Stage B1）——离线、确定性、零 LLM。

只增报告键（总纲 §6：evals 指标只增，不进质量门禁）：

- ``profile_stability``：画像卡派生稳定性——增量折入 ≡ 排序重放 ≡ 乱序重放
  （1.0 = 完全一致）；
- ``conflict_precision``：冲突标记准确率——该标记的标记（画像卡/语义卡
  只标记不覆写、resolve_conflicts 窗内标记）、不该标记的不误报（子串外共存、
  跨窗不裁决、tags 全集不同不裁决）；
- ``privacy_leak``：PII 零留存抽查——写路径产物（InMemoryMemory /
  SemanticMemoryStore / redact_pii 自身）中残留的原文 PII 计数，**必须为 0**。

运行：``python evals/memory_eval.py`` 输出 summary JSON。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# 双路径导入（与 run_eval 同风格）：直跑 ``python evals/memory_eval.py`` 时
# sys.path[0] = evals/ ⇒ 先把仓库根补进 sys.path；包导入/pytest 已在路径中则不动。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agentix.agent.memory import InMemoryMemory, redact_pii, resolve_conflicts
from agentix.agent.memory.service import CONFLICT_TIME_WINDOW_DAYS
from agentix.application.services.long_term_memory import (
    derive_profile,
    fold_profile_card,
)
from agentix.application.services.semantic_memory import (
    MemoryCard,
    SemanticMemoryStore,
    merge_cards,
)
from agentix.domain.agent.types import MemoryItem, MemoryKind, MemoryScope

# ---------------------------------------------------------------------------
# fixtures（离线确定性：固定时间戳，不引 time.time()）
# ---------------------------------------------------------------------------

_TS = 1_700_000_000.0


def _profile_events() -> List[Dict[str, Any]]:
    """MemorySummary.to_dict() 形状的事件（含同键不同值的 preferences）。"""
    return [
        {
            "session_id": "s1",
            "user_id": "u1",
            "summary": "讨论了合同违约金条款。",
            "topics": ["合同"],
            "key_facts": [],
            "message_count": 4,
            "created_at": _TS,
            "access_count": 0,
            "last_accessed": _TS,
            "source_message_ids": [],
            "preferences": {"答复风格": "简洁"},
        },
        {
            "session_id": "s2",
            "user_id": "u1",
            "summary": "讨论了侵权责任划分。",
            "topics": ["合同", "侵权"],
            "key_facts": [],
            "message_count": 6,
            "created_at": _TS + 100.0,
            "access_count": 0,
            "last_accessed": _TS + 100.0,
            "source_message_ids": [],
            "preferences": {"答复风格": "详尽"},
        },
        {
            "session_id": "s3",
            "user_id": "u1",
            "summary": "补充确认了条款细节。",
            "topics": ["合同"],
            "key_facts": [],
            "message_count": 2,
            "created_at": _TS + 200.0,
            "access_count": 0,
            "last_accessed": _TS + 200.0,
            "source_message_ids": [],
            "preferences": {},
        },
    ]


_PII_FIXTURES: List[str] = [
    "联系电话 13812345678",
    "邮箱 test@example.com",
    "身份证 110101199001011234",
    "住址 中关村大街27号",
    "我叫张三",
    "my name is John Smith",
    "信用卡 4111 1111 1111 1111",
]


def _mem(
    content: str,
    *,
    confidence: float = 0.5,
    tags: tuple = (),
    stored_at: float = None,  # type: ignore[assignment]
) -> MemoryItem:
    meta: Dict[str, Any] = {} if stored_at is None else {"stored_at": stored_at}
    return MemoryItem(
        content=content,
        kind=MemoryKind.USER,
        scope=MemoryScope.USER,
        subject="u1",
        confidence=confidence,
        tags=tags,
        meta=meta,
    )


class _FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def hset(self, key, mapping=None, **kw):
        self.store.setdefault(key, {}).update(mapping or {})

    async def hgetall(self, key):
        return dict(self.store.get(key, {}))

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


# ---------------------------------------------------------------------------
# ① profile_stability
# ---------------------------------------------------------------------------


def evaluate_profile_stability() -> float:
    events = _profile_events()
    base = derive_profile(events).profile_card
    checks: List[bool] = [base is not None]
    if base is None:
        return 0.0
    # 乱序重放（derive 内部排序 ⇒ 应与基线逐位一致）
    checks.append(derive_profile(list(reversed(events))).profile_card == base)
    # 增量折入（按 derive 同序顺序折入 ⇒ 应与重放逐位一致）
    ordered = sorted(events, key=lambda e: (e["created_at"], e["session_id"]))
    inc: Dict[str, Any] | None = None
    for event in ordered:
        inc = fold_profile_card(inc, event)
    checks.append(inc == base)
    return 1.0 if all(checks) else 0.0


# ---------------------------------------------------------------------------
# ② conflict_precision
# ---------------------------------------------------------------------------


def evaluate_conflict_precision() -> float:
    decisions: List[bool] = []
    window = CONFLICT_TIME_WINDOW_DAYS * 86400.0

    # 1. 画像卡：同键不同值 → 标记且原值不覆写
    card = derive_profile(_profile_events()).profile_card
    assert card is not None
    decisions.append(card["user_constraints"].get("答复风格") == "简洁")
    rows = [r for r in card.get("conflicts", []) if r.get("key") == "答复风格"]
    decisions.append(
        len(rows) == 1 and set(rows[0].get("values") or []) == {"简洁", "详尽"}
    )

    # 2. 语义卡：子串冲突 → 标记不覆写（version+1）
    old = MemoryCard(
        card_id="old1", card_type="preference", content="用户偏好简洁回答"
    )
    merged, rep = merge_cards(
        [old],
        [
            MemoryCard(
                card_id="n1",
                card_type="preference",
                content="用户偏好简洁回答不带出处",
            )
        ],
    )
    decisions.append(rep.conflicts == 1)
    row = merged[0].conflicts[0] if merged[0].conflicts else {}
    decisions.append(
        merged[0].content == old.content
        and merged[0].version == 2
        and set(row.get("values") or [])
        == {"用户偏好简洁回答", "用户偏好简洁回答不带出处"}
    )

    # 3. 语义卡：内容无关 → 共存不误报
    base = MemoryCard(card_id="a1", card_type="preference", content="偏好表格呈现")
    m2, r2 = merge_cards(
        [base], [MemoryCard(card_id="a2", card_type="preference", content="先检索再回答")]
    )
    decisions.append(r2.conflicts == 0 and len(m2) == 2 and m2[0].conflicts == [])

    # 4. resolve：窗内同键 → 裁决 + 胜者标记败者
    out = resolve_conflicts(
        [
            _mem("旧说法", confidence=0.4, tags=("preference",), stored_at=_TS),
            _mem(
                "新说法",
                confidence=0.9,
                tags=("preference",),
                stored_at=_TS - 86400.0,
            ),
        ]
    )
    decisions.append(len(out) == 1 and out[0].content == "新说法")
    rows = out[0].meta.get("conflicts") or []
    decisions.append(
        len(rows) == 1 and rows[0].get("content") == "旧说法"
        and rows[0].get("confidence") == 0.4
    )

    # 5. resolve：窗外同键 → 历史独立保留（各出胜者，互不覆盖）
    out = resolve_conflicts(
        [
            _mem("现值", confidence=0.4, tags=("preference",), stored_at=_TS),
            _mem(
                "历史值",
                confidence=0.9,
                tags=("preference",),
                stored_at=_TS - (window + 86400.0),
            ),
        ]
    )
    decisions.append(len(out) == 2)

    # 6. resolve：tags 全集不同 → 不同事实不裁决（tags[0] 相同也不裁决）
    out = resolve_conflicts(
        [
            _mem("甲", confidence=0.5, tags=("法律", "合同")),
            _mem("乙", confidence=0.9, tags=("法律", "侵权")),
        ]
    )
    decisions.append(len(out) == 2)

    # 7. resolve：tags 集合相同（次序无关）→ 同键裁决
    out = resolve_conflicts(
        [
            _mem("丙", confidence=0.5, tags=("合同", "法律")),
            _mem("丁", confidence=0.9, tags=("法律", "合同")),
        ]
    )
    decisions.append(len(out) == 1 and out[0].content == "丁")

    return sum(1 for d in decisions if d) / len(decisions) if decisions else 0.0


# ---------------------------------------------------------------------------
# ③ privacy_leak（写路径产物中残留的原文 PII 计数，必须为 0）
# ---------------------------------------------------------------------------


async def evaluate_privacy_leak() -> int:
    leak = 0
    for raw in _PII_FIXTURES:
        if raw in redact_pii(raw):
            leak += 1  # 脱敏自身未覆盖

    mem = InMemoryMemory()
    for raw in _PII_FIXTURES:
        await mem.remember(
            MemoryItem(
                content=raw, kind=MemoryKind.USER, scope=MemoryScope.USER, subject="u1"
            )
        )
    stored_texts = [it.content for it in mem.all_items()]
    for raw in _PII_FIXTURES:
        if any(raw in text for text in stored_texts):
            leak += 1  # InMemoryMemory 写路径残留

    store = SemanticMemoryStore(redis_client=_FakeRedis())
    await store.store_cards(
        "u1",
        [
            MemoryCard(card_id=f"c{i}", card_type="preference", content=raw)
            for i, raw in enumerate(_PII_FIXTURES)
        ],
    )
    cards = await store.recall_cards("u1", limit=len(_PII_FIXTURES))
    card_texts = [c.content for c in cards]
    for raw in _PII_FIXTURES:
        if any(raw in text for text in card_texts):
            leak += 1  # SemanticMemoryStore 写路径残留
    return leak


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


async def build_memory_report() -> Dict[str, Any]:
    """M-P0 验收报告（离线确定性；privacy_leak 必须为 0 才算通过）。"""
    return {
        "profile_stability": evaluate_profile_stability(),
        "conflict_precision": evaluate_conflict_precision(),
        "privacy_leak": await evaluate_privacy_leak(),
    }


def main() -> None:
    report = asyncio.run(build_memory_report())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
