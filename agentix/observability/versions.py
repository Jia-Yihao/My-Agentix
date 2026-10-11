"""Stable identifiers used to correlate answers, evaluations and deployments."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def content_version(value: Any, *, prefix: str = "v") -> str:
    """Return a short deterministic version for a prompt/configuration payload."""
    if isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}{hashlib.sha256(raw).hexdigest()[:12]}"


PROMPT_VERSION = content_version(
    "agentix_retrieval.chat.build_prompt.v1:strict-document-grounded-answer",
    prefix="prompt-",
)

