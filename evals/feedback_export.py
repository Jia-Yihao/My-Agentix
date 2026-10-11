#!/usr/bin/env python
r"""P2-1 反馈导出：EventPort ``message_feedback``（rating=down）→ 回流输入 JSONL。

链路定位（只做格式适配，回流管道零新增）：

    前端点踩 → POST /api/v1/feedback → EventPort(message_feedback)
    → 本脚本（导出 + 字段映射）→ evals/feedback_ingest.py（候选 golden 行）
    → 人工评审 → legal_golden.jsonl（**人工手动并入**，绝不自动改写）

字段映射（**语义诚实**，不硬造）：

- ``error_code="user_feedback_down"``：点踩是"用户判定的质量失败"，
  用一个专用错误码表达——``feedback_ingest.classify_failure`` 的首条规则
  （有 error_code 即失败样本）据此命中，且不与系统错误混淆；
  **不伪造** ``status="error"``（那暗示系统级故障，点踩是质量判定）。
- ``message`` = 用户 reason（已在端点侧过 PII 脱敏）；
- ``question``/``route_type``/``sources`` 等**留空**——反馈事件里没有
  原请求上下文，不臆造；候选行标注 needs_review 交人工补全。

数据源（两种，二选一）：

- ``--events-file``（默认）：``EVENT_PORT_BACKEND=jsonl`` 时的事件日志
  （``events/event_log.jsonl``），适合从落盘记录离线导出；
- ``--live``：当前进程内 ``get_event_port().list_recent(...)``——仅对
  memory 后端 + 同进程（如嵌入脚本）有意义，独立进程看不到内存事件，
  这是 EventPort 单进程写者假设的诚实边界。

用法（PowerShell）::

    .\.venv\Scripts\python.exe evals/feedback_export.py --events-file events/event_log.jsonl --out prod_feedback.jsonl
    .\.venv\Scripts\python.exe evals/feedback_ingest.py --input prod_feedback.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

EVALS_DIR: Path = Path(__file__).resolve().parent
DEFAULT_EVENTS_FILE = Path("events") / "event_log.jsonl"
DEFAULT_OUT = EVALS_DIR / "candidates" / "feedback_down.jsonl"

#: 与 agentix/interfaces/http/v1/feedback.py 约定同一字符串（复制常量，
#: 避免 evals 脚本反向 import 接口层——分层纪律）
MESSAGE_FEEDBACK_EVENT_TYPE = "message_feedback"

#: 点踩样本的失败信号码（见模块 docstring 的语义说明）
USER_FEEDBACK_DOWN_CODE = "user_feedback_down"


def load_events_from_file(path: Path) -> List[Dict[str, Any]]:
    """读 JSONL 事件日志，取 op=publish 且 type=message_feedback 的最新快照。

    日志可能含 op=publish 与 op=state 两类行（末次快照生效）；损坏行容忍跳过。
    """
    events: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("event_type") != MESSAGE_FEEDBACK_EVENT_TYPE:
            continue
        eid = entry.get("event_id")
        if not eid:
            continue
        events[str(eid)] = entry
    return list(events.values())


def load_events_live() -> List[Dict[str, Any]]:
    """当前进程内 EventPort 拉取（仅 memory 后端 + 同进程有意义）。"""
    from agentix.agent.events.port import get_event_port

    return list(get_event_port().list_recent(MESSAGE_FEEDBACK_EVENT_TYPE, limit=10_000))


def to_ingest_record(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """单条事件 → feedback_ingest 输入格式（rating=down 才回流）。

    纯函数，便于单测。up 反馈不进回流管道（点踩才是失败信号）。
    """
    payload = event.get("payload") or {}
    if payload.get("rating") != "down":
        return None
    # 事件行可能是 publish 行（payload 在记录内）或快照行（同形），
    # 防御性再取一层 dict(event) 的 payload
    if not payload and isinstance(event.get("payload"), dict):
        payload = dict(event["payload"])
    return {
        "error_code": USER_FEEDBACK_DOWN_CODE,
        "message": payload.get("reason") or "",
        "session_id": payload.get("session_id"),
        "trace_id": payload.get("trace_id"),
        "message_id": payload.get("message_id"),
        "rating": "down",
        "user": payload.get("user"),
        "timestamp": payload.get("ts"),
        # question / route_type / sources 不在反馈事件里——留空交人工补全，
        # 不臆造（evals/README.md §7 防自证纪律）
    }


def run(events_file: Path, out_path: Path, *, live: bool = False) -> int:
    """主流程：拉事件 → 过滤 down → 写 JSONL（feedback_ingest 的 --input 格式）。"""
    events = load_events_live() if live else load_events_from_file(events_file)
    if not live and not events_file.exists():
        print(f"[导出] 事件日志不存在：{events_file}", file=sys.stderr)
        print("[导出] 提示：EVENT_PORT_BACKEND=jsonl 时反馈才落盘；", file=sys.stderr)
        print("[导出]       memory 后端的事件只存在于进程内（单进程写者假设）。", file=sys.stderr)
        return 2
    records = [r for r in (to_ingest_record(e) for e in events) if r is not None]
    if not records:
        print(f"[导出] 共 {len(events)} 条 message_feedback 事件，无 rating=down，无需导出。")
        return 0
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"[导出] message_feedback {len(events)} 条 → down {len(records)} 条")
    print(f"[导出] 输出：{out_path}")
    print("[导出] 下一步：.\\.venv\\Scripts\\python.exe evals/feedback_ingest.py "
          f"--input {out_path}  （候选行需人工评审，绝不自动并入 golden）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feedback_export.py",
        description="EventPort message_feedback（rating=down）→ feedback_ingest 输入 JSONL。",
    )
    parser.add_argument(
        "--events-file",
        default=str(DEFAULT_EVENTS_FILE),
        help=f"JSONL 事件日志（EVENT_PORT_BACKEND=jsonl 落盘；默认：{DEFAULT_EVENTS_FILE}）",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 JSONL 路径")
    parser.add_argument(
        "--live",
        action="store_true",
        help="从当前进程内 EventPort 拉取（仅 memory 后端 + 同进程有意义）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return run(Path(args.events_file), Path(args.out), live=args.live)


if __name__ == "__main__":
    sys.exit(main())
