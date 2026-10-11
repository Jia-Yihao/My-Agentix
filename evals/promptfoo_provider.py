#!/usr/bin/env python
"""promptfoo 自定义 Python provider（P1-5）：持久 worker 内直调 ChatService。

设计要点：

- **绕 HTTP 直调**：``from agentix.application.services.chat_service import
  get_chat_service`` → ``await service.ask(message, session_id)``——与
  ``run_eval.py --record`` 同一调用缝（run_eval.py:1790 同款），复用既有
  鉴权与路由，不重复造调用链。promptfoo 的持久 worker 进程与 service
  单例天然契合（worker 启动后首个请求才真正建链，后续复用）。
- **结构化输出**：把 ChatService 的结构化结果（route/sources/message/status）
  序列化成 JSON 字符串放进 ``output``——断言侧（promptfoo_asserts.py）
  解析后可做确定性判定，不靠自由文本正则猜。
- **诚实失败**：单条调用异常时返回带 ``error: true`` 的结果，让 promptfoo
  把它记为失败用例而不是静默吞掉。

promptfoo 调用约定（官方文档 2026-10 查证）：``call_api(prompt, options,
context)`` 返回 ``{"output": str, ...}``；vars 经 ``context.vars`` 注入。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

#: evals/ 目录加入 sys.path：promptfoo worker 的工作目录未必是仓库根，
#: 相对导入 agentix 需要仓库根在 sys.path（provider 由 promptfoo 以文件路径加载）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 会话 ID 前缀：promptfoo 用例与 run_eval 录制流量区分开（轨迹可归因）
_SESSION_PREFIX = "promptfoo"

_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """worker 进程级事件循环（持久 worker：一次创建全程复用）。"""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def call_api(prompt: str, options: Any, context: Any) -> Dict[str, Any]:
    """promptfoo provider 入口（同步签名，内部走事件循环）。"""
    vars_ = getattr(context, "vars", None) or (context or {}).get("vars", {})
    question = str(vars_.get("question") or prompt or "").strip()
    session_id = str(vars_.get("session_id") or f"{_SESSION_PREFIX}-{id(context)}")

    if not question:
        return {"output": json.dumps({"error": "empty question"}, ensure_ascii=False),
                "error": True}

    try:
        from agentix.application.services.chat_service import get_chat_service

        service = get_chat_service()
        response = _get_loop().run_until_complete(
            service.ask(message=question, session_id=session_id)
        )
        output = json.dumps(
            {
                "route": response.get("route"),
                "sources": response.get("sources") or [],
                "message": response.get("message") or "",
                "status": response.get("status") or "ok",
            },
            ensure_ascii=False,
        )
        return {"output": output}
    except Exception as exc:  # noqa: BLE001 - 单条用例失败如实上报 promptfoo
        return {
            "output": json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            ),
            "error": True,
        }


if __name__ == "__main__":
    # 本地自检：python evals/promptfoo_provider.py "问题文本"
    q = sys.argv[1] if len(sys.argv) > 1 else "你好"
    print(json.dumps(call_api(q, None, {"vars": {"question": q}}), ensure_ascii=False))
