from datetime import datetime, timezone


def utc_now() -> datetime:
    # SQLite 存 naive UTC；与历史 utcnow() 行为一致，避免 aware/naive 混比
    # 用 timezone.utc 而非 datetime.UTC（后者需 Python 3.11+，项目 venv 为 3.10）
    return datetime.now(timezone.utc).replace(tzinfo=None)
