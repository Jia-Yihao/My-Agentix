"""
索引任务 DTO
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class IndexJobData(BaseModel):
    id: str
    kb_id: str
    status: str
    progress: int
    error_message: str | None
    error_code: str | None = None
    attempts: int = 0
    diff_summary: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class IndexJobTimingConfigData(BaseModel):
    """索引任务时序配置（暴露给前端做「任务停滞」提示，避免前端写死阈值后漂移）。

    单位均为秒；均为**生效值**（代码侧已做下限兜底，不是原始配置）。
    """

    job_timeout_seconds: int
    stale_queued_seconds: int
    stale_running_seconds: int
    # 前端「停留过久可重试」提示阈值：僵尸判定线 + 缓冲，避免提示出现时后端还不认
    stall_hint_seconds: int
