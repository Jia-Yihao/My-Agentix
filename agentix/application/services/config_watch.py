"""配置变更监测（Phase 22，KV Cache 纪律 Ch2 §2.4）。

运行中检测到影响行为的配置变更 ⇒ 向**轨迹 append** 一条 ``system_notice``
——只通知、不重建静态前缀（KV 前缀不被打穿）。

- 首次调用只记 config hash 基线（返回 False）；
- 哈希与通知**排除 secret 字段**，且 detail 只含键名/哈希、不含任何配置值
  （Gotcha：system_notice 不得含 PII）；
- 全程自吞异常——观测失败不影响主链路。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from loguru import logger

from agentix.config.settings import settings

__all__ = ["config_hash", "notice_if_changed", "reset_config_watch"]

#: 键名含这些片段 ⇒ 视为 secret：不进哈希、不进通知（凭据/PII 防线）。
_SECRET_KEY_HINTS = ("key", "secret", "token", "password", "credential", "dsn")

#: 模块级基线：进程内最近一次已知的非 secret 配置快照。
_prev: Optional[Dict[str, str]] = None


def _snapshot() -> Dict[str, str]:
    """非 secret 配置的规范化快照（键排序、值统一为 str）。"""
    try:
        dump: Dict[str, Any] = dict(settings.model_dump())
    except Exception:  # noqa: BLE001 - dump 失败兜底走字段遍历
        dump = {
            k: getattr(settings, k, None)
            for k in getattr(settings, "model_fields", {})
        }
    out: Dict[str, str] = {}
    for k in sorted(dump):
        if any(hint in k.lower() for hint in _SECRET_KEY_HINTS):
            continue
        out[k] = str(dump[k])
    return out


def config_hash() -> str:
    """当前非 secret 配置的 sha256（canonical JSON，键排序）。"""
    blob = json.dumps(_snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def notice_if_changed(recorder: Optional[Any] = None) -> bool:
    """检测配置变更；变更 ⇒ append ``system_notice`` 并返回 True。

    首次调用只记基线（返回 False）。``detail`` 只含事件名/变更键名/哈希
    前缀，**不含任何配置值**。``recorder`` 缺省取应用侧轨迹 recorder。
    """
    global _prev
    try:
        current = _snapshot()
        if _prev is None:
            _prev = current
            return False
        if current == _prev:
            return False
        changed: List[str] = [k for k in current if current[k] != _prev.get(k)]
        changed += [k for k in _prev if k not in current]
        _prev = current
        detail = {
            "event": "config_change",
            "changed_keys": sorted(changed),
            "config_hash": config_hash()[:16],
        }
        if recorder is None:
            from agentix.application.services.trajectory import (
                get_trajectory_recorder,
            )

            recorder = get_trajectory_recorder()
        recorder.record(
            kind="notice", name="system_notice", status="ok", detail=detail
        )
        logger.info(
            "runtime config change detected: {} key(s) -> system_notice appended",
            len(changed),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响主链路
        logger.debug("config watch failed (non-fatal): {}", exc)
        return False


def reset_config_watch() -> None:
    """清空基线（下次调用重新记基线；测试隔离用）。"""
    global _prev
    _prev = None
