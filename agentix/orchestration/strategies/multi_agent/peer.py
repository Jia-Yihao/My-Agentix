"""Multi-Agent Peer 协议与信息增量判据（Phase 11）。

书 §10：先 Peer（Proposer–Reviewer）+ Manager，不做 Decentralized 首期。
共享/隔离两轴：Peer 默认**隔离** trajectory 与预算，只通过结构化消息交换
（信息增量为正才继续通信，防止 echo chamber / 无效轮转）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Tuple

__all__ = [
    "AgentRole",
    "PeerMessage",
    "PeerResult",
    "information_increment",
    "has_information_increment",
    "token_overlap_ratio",
]


class AgentRole(str, Enum):
    PROPOSER = "proposer"
    REVIEWER = "reviewer"
    MANAGER = "manager"


@dataclass(frozen=True)
class PeerMessage:
    """Peer 间唯一交换面：纯结构化，不携带对方 trajectory/prompt。"""

    sender: str
    receiver: str
    role: AgentRole
    content: str
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "role": self.role.value,
            "content": self.content,
            "meta": dict(self.meta),
        }


@dataclass
class PeerResult:
    agent_id: str
    role: AgentRole
    output: str
    ok: bool = True
    error: str = ""
    steps_used: int = 0
    messages_sent: Tuple[PeerMessage, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)


def _content_tokens(text: str) -> frozenset:
    """粗粒度 token：CJK 按字、拉丁按词，足够做信息增量近似。"""
    if not text:
        return frozenset()
    tokens = set()
    buf = []
    for ch in text:
        if ch.isalnum() or "一" <= ch <= "鿿":
            buf.append(ch.lower())
        else:
            if buf:
                tokens.add("".join(buf))
                buf = []
    if buf:
        tokens.add("".join(buf))
    return frozenset(tokens)


def token_overlap_ratio(new_text: str, seen_text: str) -> float:
    """new 相对 seen 的 token 覆盖率 ∈ [0,1]；seen 空 → 0.0。"""
    new_t = _content_tokens(new_text)
    seen_t = _content_tokens(seen_text)
    if not new_t or not seen_t:
        return 0.0
    return len(new_t & seen_t) / len(new_t)


def information_increment(
    candidate: str,
    context: str,
    *,
    min_new_tokens: int = 1,
    max_overlap: float = 0.95,
) -> Dict[str, Any]:
    """信息增量判据（书 §10）：候选相对上下文是否带来**新信息**。

    返回::

        {
          "incremental": bool,
          "new_token_count": int,
          "overlap_ratio": float,
          "reason": str,
        }

    规则：
    - 新 token 数 < ``min_new_tokens`` → 不增量（空洞/重复）；
    - overlap > ``max_overlap`` → 不增量（近似复读）。
    """
    cand = _content_tokens(candidate)
    ctx = _content_tokens(context)
    new_tokens = cand - ctx
    overlap = token_overlap_ratio(candidate, context)
    n_new = len(new_tokens)
    if n_new < min_new_tokens:
        reason = "no_new_tokens"
        incremental = False
    elif overlap > max_overlap:
        reason = "high_overlap_echo"
        incremental = False
    else:
        reason = "ok"
        incremental = True
    return {
        "incremental": incremental,
        "new_token_count": n_new,
        "overlap_ratio": round(overlap, 4),
        "reason": reason,
    }


def has_information_increment(
    candidate: str, context: str, **kwargs: Any
) -> bool:
    return bool(information_increment(candidate, context, **kwargs)["incremental"])
