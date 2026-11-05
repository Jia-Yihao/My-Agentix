"""子代理结果契约与共享证据面。

Peer 结果进黑板的面从 giant string 换成结构化契约::

    {status, claims[], evidence_refs[], artifact_refs[], confidence, cost}

- ``claims``：输出的**有界**要点提取（非空行前 5 条 × 200 字符）——黑板承载
  要点而非全文；完整输出仍留在 ``ManagerOutcome.peer_results``（不丢信息）；
- ``evidence_refs``：``meta["evidence"]``（EVIDENCE_POOL 形态）逐条经
  ``offload_content`` 外置，主状态只留 ``obs://`` 句柄（共享证据面）；
- ``artifact_refs``：``meta["artifacts"]`` 键值 + 输出/元数据中既有的
  ``obs://`` 句柄原样透传；
- 契约面过 ``max_result_chars`` 时整体外置为 ``obs://`` 句柄
  （从「丢字符」变「可取回」），降级路径回基线截断。

两个 flag 均**默认关**（``settings.py``，五处同步）：

- ``SUBAGENT_RESULT_CONTRACT_ENABLED`` —— 契约面 + 证据外置；
- ``MULTIAGENT_EARLY_EXIT_ENABLED`` —— proposer 证据充分即跳过其余
  peer/轮次（复用既有 ``information_increment`` 判据）。

flag-off = Manager 现行为逐位等价（``bound_result_face`` 全文截断）。

架构注记：本模块要用的两项应用侧能力（内容外置、产物存储）自 **M3-5a** 起不再直接
import ``agentix.application``，改经编排层同层的**非节点能力注入位**
``agentix/orchestration/capabilities.py`` 按键名在**调用期**解析（每次现取，不缓存）。
键由 ``agentix/application/wiring.py`` 在装配期登记（``offload_content`` /
``get_artifact_store``）；未装配时注入位抛 ``CapabilityNotRegistered``、绝不静默退回
进程内兜底实现，但**抛出后是否可见按调用点分**：``_offload_text`` 原样抛给调用方；
``try_offload_face`` 与 ``resolve_evidence_ref`` 的既有 ``except Exception`` 契约（降级
返回 ``None``、绝不抛）会把它兜成降级；``resolve_evidence_ref`` 在调用方**显式传入**
``store`` 时不经注入位，未装配也能解析（所以那条路不依赖装配）。本模块不 import manager
（manager import 本模块，避免环）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agentix.config import settings
from agentix.orchestration import capabilities
from agentix.orchestration.strategies.multi_agent.peer import (
    PeerResult,
    has_information_increment,
)

__all__ = [
    "SubAgentResult",
    "contract_face",
    "try_offload_face",
    "resolve_evidence_ref",
    "evidence_sufficient",
    "contract_enabled",
    "early_exit_enabled",
]

_OBS_PREFIX = "obs://"
#: obs:// 句柄扫描（ArtifactStore id = 字母数字/下划线/短横）
_OBS_RE = re.compile(r"obs://[A-Za-z0-9_\-]+")
_MAX_CLAIMS = 5
_CLAIM_CHARS = 200
#: 内联降级（证据过短未外置）时保留在 evidence_refs 的短串上限
_INLINE_EVIDENCE_CHARS = 120
#: 证据强制外置阈值（>0；EVIDENCE_POOL 单条 ≤4000，几乎总是外置成功）
_EVIDENCE_OFFLOAD_THRESHOLD = 2000
#: 契约面整体外置阈值（1 = 除空串必外置）
_FACE_FORCE_OFFLOAD = 1


def contract_enabled() -> bool:
    """``SUBAGENT_RESULT_CONTRACT_ENABLED``（默认关 = 基线全文截断面）。"""
    return bool(getattr(settings, "SUBAGENT_RESULT_CONTRACT_ENABLED", False))


def early_exit_enabled() -> bool:
    """``MULTIAGENT_EARLY_EXIT_ENABLED``（默认关 = 每 peer 照常调用）。"""
    return bool(getattr(settings, "MULTIAGENT_EARLY_EXIT_ENABLED", False))


def _derive_claims(
    output: Any, *, max_claims: int = _MAX_CLAIMS, max_chars: int = _CLAIM_CHARS
) -> List[str]:
    """输出 → 有界断言要点（非空行、保序去重、行内截断）。"""
    claims: List[str] = []
    seen = set()
    for line in str(output or "").splitlines():
        text = line.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        claims.append(text[:max_chars])
        if len(claims) >= max_claims:
            break
    return claims


def _offload_text(text: str, *, kind: str) -> str:
    """内容 → ``obs://`` 句柄；外置失败/过短 → 有界内联短串。

    能力经注入位解析（M3-5a）：``resolve`` 每次现取应用侧当前模块属性，所以测试
    patch ``artifact_store.offload_content`` 照常生效。解析不到（装配没跑）时
    抛 ``CapabilityNotRegistered`` 响亮失败——本函数不吞它。
    """
    offload_content = capabilities.resolve("offload_content")
    result = offload_content(
        text,
        kind=kind,
        inline_threshold=_EVIDENCE_OFFLOAD_THRESHOLD,
        summary="multi-agent subagent evidence",
    )
    if result.is_offloaded and result.ref is not None:
        return f"{_OBS_PREFIX}{result.ref.artifact_id}"
    return (result.inline or "")[:_INLINE_EVIDENCE_CHARS]


def _extract_evidence_refs(meta: Optional[Dict[str, Any]]) -> List[str]:
    """``meta["evidence"]``（EVIDENCE_POOL 形态）→ obs:// 引用列表。"""
    refs: List[str] = []
    evidence = (meta or {}).get("evidence")
    if not isinstance(evidence, (list, tuple)):
        return refs
    for item in evidence:
        if isinstance(item, dict):
            content = str(item.get("content", "") or "")
        else:
            content = str(item or "")
        if content:
            refs.append(_offload_text(content, kind="evidence"))
    return refs


def _extract_artifact_refs(result: PeerResult) -> List[str]:
    """产物键值 + 输出/元数据中既有 obs:// 句柄（原样透传，不重复）。"""
    refs: List[str] = []
    meta = result.meta or {}
    artifacts = meta.get("artifacts")
    if isinstance(artifacts, dict):
        refs.extend(f"{key}={str(value)[:100]}" for key, value in artifacts.items())
    elif isinstance(artifacts, (list, tuple)):
        refs.extend(str(item)[:120] for item in artifacts)
    try:
        meta_text = json.dumps(meta, ensure_ascii=False, default=str)[:4000]
    except Exception:  # noqa: BLE001 - meta 序列化失败不影响契约主链路
        meta_text = ""
    for text in (str(result.output or ""), meta_text):
        for handle in _OBS_RE.findall(text):
            if handle not in refs:
                refs.append(handle)
    return refs


@dataclass
class SubAgentResult:
    """子代理结果契约——替代 giant string 进黑板的结构化面。"""

    status: str = "ok"
    claims: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    artifact_refs: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    cost: Dict[str, float] = field(
        default_factory=lambda: {"tokens": 0, "latency_ms": 0.0}
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "claims": list(self.claims),
            "evidence_refs": list(self.evidence_refs),
            "artifact_refs": list(self.artifact_refs),
            "confidence": self.confidence,
            "cost": {
                "tokens": int(self.cost.get("tokens", 0) or 0),
                "latency_ms": float(self.cost.get("latency_ms", 0.0) or 0.0),
            },
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SubAgentResult":
        """容错反序列化：缺键/错型走缺省，绝不抛（往返测试的对偶）。"""
        raw = data if isinstance(data, dict) else {}
        cost = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
        confidence = raw.get("confidence")
        try:
            conf_val = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            conf_val = None
        try:
            tokens_val = int(cost.get("tokens", 0) or 0)
        except (TypeError, ValueError):
            tokens_val = 0
        try:
            latency_val = float(cost.get("latency_ms", 0.0) or 0.0)
        except (TypeError, ValueError):
            latency_val = 0.0
        return cls(
            status=str(raw.get("status", "ok") or "ok"),
            claims=[str(c) for c in raw.get("claims", []) or []],
            evidence_refs=[str(r) for r in raw.get("evidence_refs", []) or []],
            artifact_refs=[str(r) for r in raw.get("artifact_refs", []) or []],
            confidence=conf_val,
            cost={"tokens": tokens_val, "latency_ms": latency_val},
        )

    @classmethod
    def from_peer_result(cls, result: PeerResult) -> "SubAgentResult":
        """从 PeerResult 派生契约（cost 取 peer 自报 meta，缺省 0——不臆造）。"""
        meta = result.meta or {}
        confidence = meta.get("confidence")
        try:
            conf_val = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            conf_val = None
        tokens = meta.get("tokens")
        latency = meta.get("latency_ms")
        try:
            tokens_val = int(tokens) if tokens is not None else 0
        except (TypeError, ValueError):
            tokens_val = 0
        try:
            latency_val = float(latency) if latency is not None else 0.0
        except (TypeError, ValueError):
            latency_val = 0.0
        return cls(
            status="ok" if result.ok else "error",
            claims=_derive_claims(result.output),
            evidence_refs=_extract_evidence_refs(meta),
            artifact_refs=_extract_artifact_refs(result),
            confidence=conf_val,
            cost={"tokens": max(0, tokens_val), "latency_ms": max(0.0, latency_val)},
        )


def contract_face(result: PeerResult) -> str:
    """PeerResult → 紧凑 JSON 契约面（无空格，未截断）。"""
    return json.dumps(
        SubAgentResult.from_peer_result(result).to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def try_offload_face(payload: str, *, kind: str = "result_contract") -> Optional[str]:
    """契约面整体外置 → ``…[结果面已外置，可取回 obs://…]``；失败 → None。

    调用方（manager）在 ``None`` 时降级回基线 ``bound_result_face`` 截断。
    """
    if not str(payload or ""):
        return None
    try:
        offload_content = capabilities.resolve("offload_content")
        result = offload_content(
            payload,
            kind=kind,
            inline_threshold=_FACE_FORCE_OFFLOAD,
            summary="subagent result contract",
        )
    except Exception:  # noqa: BLE001 - 外置失败或能力未装配都降级基线截断，不阻断
        return None
    if result.is_offloaded and result.ref is not None:
        return f"…[结果面已外置，可取回 {_OBS_PREFIX}{result.ref.artifact_id}]"
    return None


def resolve_evidence_ref(
    ref: Any, *, store: Optional[Any] = None
) -> Optional[str]:
    """``obs://id`` → 产物内容；未知 id / 非句柄 → None（缺省回退）。

    调用方约定：``None`` = 引用不可解析（已淘汰/非句柄），降级跳过或用
    原始文本——绝不抛。
    """
    text = str(ref or "")
    if not text.startswith(_OBS_PREFIX):
        return None
    artifact_id = text[len(_OBS_PREFIX):]
    if not artifact_id:
        return None
    try:
        target = store if store is not None else capabilities.resolve("get_artifact_store")()
        return target.get(artifact_id)
    except Exception:  # noqa: BLE001 - 存储异常或未装配的默认 store 按不可解析处理
        return None


def evidence_sufficient(result: Any, context: str) -> bool:
    """早停判据：提案**带证据**且相对上下文**有信息增量**。

    复用既有 :func:`has_information_increment`（书 §10 判据单源）；两个条件
    缺一不可——有证据但纯复读（echo）不早停，有增量但无证据不早停（证据
    充分性由 ``meta["evidence"]`` 或输出内嵌 obs:// 句柄表征）。
    """
    if not isinstance(result, PeerResult) or not result.ok:
        return False
    meta = result.meta or {}
    has_evidence = bool(meta.get("evidence")) or bool(
        _OBS_PREFIX in str(result.output or "")
    )
    if not has_evidence:
        return False
    return has_information_increment(str(result.output or ""), str(context or ""))
