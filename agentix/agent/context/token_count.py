"""M-P1-5 真分词/真计数（memory-optimization §5）：token 估算换分词器。

- ``CONTEXT_REAL_TOKEN_COUNT_ENABLED`` 默认 **关** ⇒ ``count_tokens`` 返回
  ``len(text) // 4``，与 Phase 18 起的全部估点**逐字节一致**（flag-off 等价）；
- flag-on ⇒ 复用 LLM 侧 tokenizer（``tiktoken`` cl100k_base，进程内懒加载
  一次；不新增服务）；tokenizer 缺失/加载失败 ⇒ CJK 感知启发式兜底
  （中文按字 + 其余 ``(rest+3)//4``，与检索域 ``estimate_tokens`` 同算法），
  **永不抛异常**——token 估算只是预算判断，不能把构建链炸掉。

接线点（7 处 ``len(x) // 4``）：``context/engine.py`` ×3、``context/simple.py``
×3、``context/compaction.py`` ×1。
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["count_tokens"]

_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")

_encoding_cache: Optional[object] = None
_encoding_failed = False


def _load_encoding():
    """懒加载 cl100k_base 编码器（一次成功后缓存；失败进进程级负缓存）。"""
    global _encoding_cache, _encoding_failed
    if _encoding_cache is not None:
        return _encoding_cache
    if _encoding_failed:
        return None
    try:
        import tiktoken

        _encoding_cache = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 - 编码器不可用走启发式兜底
        _encoding_failed = True
        return None
    return _encoding_cache


def _cjk_estimate(text: str) -> int:
    """CJK 感知启发式：中文/日文假名/韩文按字计 1，其余 ``(n+3)//4``。"""
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def count_tokens(text: str) -> int:
    """估算文本 token 数（flag-off ≡ ``len(text) // 4``）。"""
    from agentix.config import settings

    if not settings.CONTEXT_REAL_TOKEN_COUNT_ENABLED:
        return len(text) // 4
    enc = _load_encoding()
    if enc is None:
        return _cjk_estimate(text)
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception:  # noqa: BLE001 - 单条编码失败退启发式，不打断构建
        return _cjk_estimate(text)
