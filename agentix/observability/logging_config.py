"""JSON 日志：纯标准库 JsonFormatter + setup_logging（dictConfig）。

字段规范（增量设计 §3.4）：
- 固定字段：ts / level / logger / msg / request_id / trace_id
- extra 白名单：stage / kb_id / status / latency_ms / error_code / doc_id / job_id / tool
- request_id 缺省 "-"；exc 仅在异常时出现（截断 2000 字符）
- json.dumps(default=str) + ensure_ascii=False
"""
from __future__ import annotations

import json
import logging
import logging.config
from datetime import datetime, timezone

from agentix.observability.context import current_request_id, current_trace_id

_EXTRA_WHITELIST = (
    "stage",
    "kb_id",
    "status",
    "latency_ms",
    "error_code",
    "doc_id",
    "job_id",
    "tool",
)
_EXC_MAX_CHARS = 2000


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染为单行 JSON（只依赖标准库）。"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload: dict = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": current_request_id(),
            "trace_id": current_trace_id(),
        }
        for key in _EXTRA_WHITELIST:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[:_EXC_MAX_CHARS]
        return json.dumps(payload, ensure_ascii=False, default=str)


def _build_dict_config(json_enabled: bool, level: str) -> dict:
    formatter: dict = (
        {
            "json": {
                "()": JsonFormatter,
            }
        }
        if json_enabled
        else {
            "plain": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            }
        }
    )
    formatter_name = "json" if json_enabled else "plain"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": formatter,
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": formatter_name,
            }
        },
        "root": {
            "handlers": ["console"],
            "level": level,
        },
    }


def setup_logging() -> None:
    """应用启动时调用一次；LOG_JSON_ENABLED=false 时退回纯文本格式。"""
    from agentix.config.settings import settings

    logging.config.dictConfig(
        _build_dict_config(settings.LOG_JSON_ENABLED, settings.LOG_LEVEL)
    )
