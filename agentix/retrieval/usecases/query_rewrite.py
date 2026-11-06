# -*- coding: utf-8 -*-
"""R-P2-1 查询改写/HyDE（rag-optimization §6，F2b-b）。

两可选变换、独立 flag 门控（键沿 ``GROUNDING_*`` 检索域三处同步，默认关 = 与
基线逐位等价）：

- ``RETRIEVAL_QUERY_REWRITE_ENABLED``：多轮改写（意图补全）——结合历史对话把
  查询改写为自足的检索式；
- ``RETRIEVAL_HYDE_ENABLED``：HyDE（假设性文档扩展）——先生成一段知识库风格
  的假设性答案片段，再拿它去检索。

改写/扩展是纯增益尝试——LLM 调用失败、超时、空输出、与原查询等价，
一律回退原 query（fail-open），绝不阻断检索主链路。
"""

from __future__ import annotations

import asyncio
import logging
import re

from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)


def rewrite_timeout_seconds() -> float:
    """LLM 调用有界：超时按失败处理（fail-open 回原 query）。

    三个界限值都在**调用时**读 settings——模块级常量会把 import 时的值钉死，
    运行时改配置或测试 monkeypatch 都不生效，等于给同一份配置造了第二个取值时机。
    """
    return float(settings.RETRIEVAL_QUERY_REWRITE_TIMEOUT_SECONDS)


def max_query_chars() -> int:
    """改写查询的输出上界。"""
    return int(settings.RETRIEVAL_QUERY_REWRITE_MAX_QUERY_CHARS)


def max_doc_chars() -> int:
    """假设性文档（HyDE）的输出上界。"""
    return int(settings.RETRIEVAL_QUERY_REWRITE_MAX_DOC_CHARS)


REWRITE_PROMPT = """你是一名检索查询改写助手。把用户的查询改写为一条自足、可直接检索的检索式：
- 结合【历史对话】补全省略的主语、指代与领域限定（意图补全），去掉与检索无关的寒暄；
- 保留原查询的核心语义与全部专有名词，不要引入查询之外的新事实；
- 只输出改写后的检索式本身，不要解释、不要引号、不要换行。
【历史对话】
{history}
【查询】
{question}"""

HYDE_PROMPT = """你是一名检索文档生成助手。针对下面的查询，直接写出一段可能出现在知识库中的答案片段（假设性文档）：
- 用陈述句、贴合查询主题，像知识库文档的正文；
- 200 字以内，只输出片段本身，不要解释、不要标题、不要引号、不要换行。
【查询】
{question}"""

_WS_RE = re.compile(r"\s+")


def _same_query(a: str, b: str) -> bool:
    """去空白、忽略大小写后等价 ⇒ 视为未改写。"""
    return _WS_RE.sub("", (a or "")).lower() == _WS_RE.sub("", (b or "")).lower()


def normalize_query_output(raw: str, original: str) -> tuple[str, bool]:
    """LLM 改写输出 → (生效查询, 是否生效)。

    取首个非空行、去包裹引号/反引号、超长截断；空/等价/异常输出 ⇒
    回退原查询且 ``applied=False``（缓存池不加后缀，与 flag-off 同池）。
    """
    text = (raw or "").strip()
    if not text:
        return original, False
    for line in text.splitlines():
        line = line.strip()
        if line:
            text = line
            break
    text = text.strip().strip("\"'`“”‘’「」").strip()
    if not text:
        return original, False
    limit = max_query_chars()
    if len(text) > limit:
        text = text[:limit]
    if _same_query(text, original):
        return original, False
    return text, True


def normalize_document_output(raw: str, original: str) -> tuple[str, bool]:
    """LLM HyDE 输出 → (生效检索文本, 是否生效)。空/等价 ⇒ 回退原查询。"""
    text = (raw or "").strip()
    if not text:
        return original, False
    text = text.strip("\"'`“”‘’「」").strip()
    if not text:
        return original, False
    limit = max_doc_chars()
    if len(text) > limit:
        text = text[:limit]
    if _same_query(text, original):
        return original, False
    return text, True


async def _invoke_llm(
    *, prompt: str, llm, model_name: str | None, name: str
) -> str:
    """经熔断/限流通道调 LLM，返回纯文本；调用方包 try/except。"""
    from agentix.retrieval.usecases.chat_llm import create_chat_llm, invoke_with_breaker

    if llm is None:
        llm = create_chat_llm(model_name=model_name, chat_mode="fast")
    resp = await asyncio.wait_for(
        invoke_with_breaker(
            llm, [{"role": "user", "content": prompt}], name=name
        ),
        timeout=rewrite_timeout_seconds(),
    )
    return str(getattr(resp, "content", "") or resp or "")


async def rewrite_query(
    *,
    question: str,
    history: str = "",
    model_name: str | None = None,
    llm=None,
) -> tuple[str, bool]:
    """多轮改写（意图补全）。任何失败回退原查询（fail-open）。"""
    if not (question or "").strip():
        return question, False
    try:
        prompt = REWRITE_PROMPT.format(
            history=(history or "").strip() or "（无）",
            question=question,
        )
        raw = await _invoke_llm(
            prompt=prompt, llm=llm, model_name=model_name, name="llm.query_rewrite"
        )
        return normalize_query_output(raw, question)
    except Exception:  # noqa: BLE001 - 改写失败绝不阻断检索
        logger.warning("query rewrite failed (fail-open)", exc_info=True)
        return question, False


async def hyde_expand(
    *,
    question: str,
    model_name: str | None = None,
    llm=None,
) -> tuple[str, bool]:
    """HyDE 假设性文档扩展。任何失败回退原查询（fail-open）。"""
    if not (question or "").strip():
        return question, False
    try:
        prompt = HYDE_PROMPT.format(question=question)
        raw = await _invoke_llm(
            prompt=prompt, llm=llm, model_name=model_name, name="llm.hyde_expand"
        )
        return normalize_document_output(raw, question)
    except Exception:  # noqa: BLE001 - 扩展失败绝不阻断检索
        logger.warning("hyde expand failed (fail-open)", exc_info=True)
        return question, False
