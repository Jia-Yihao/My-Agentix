"""L4 生产压缩器（Phase 22）：application 侧接 LLM 通道。

经 ``llm_factory.get_llm_client`` 复用共享网关配置（角色 ``summarizer``
profile：零温度 + 既有超时/重试分档）。仅由组合根
（``agent_loop_adapter``）在 ``CONTEXT_AUTOCOMPACT=True`` 时注入；
离线 record / ``--judge`` eval 路径永不构造本类（Gotcha：L4 需 LLM，
不得成为离线依赖）。压缩结果仍受调用方五要素机制断言守卫。
"""

from __future__ import annotations

from agentix.agent.context.compaction import COMPACT_RULES

__all__ = ["LLMCompactor"]


class LLMCompactor:
    """``CompactorPort`` 生产实现：单次 LLM 调用压缩轨迹正文。"""

    async def compact(self, text: str, *, budget_tokens: int) -> str:
        from agentix.application.agents.llm_factory import get_llm_client

        model = get_llm_client(
            temperature=0.0, tags=["compaction"], role="summarizer"
        )
        prompt = (
            f"{COMPACT_RULES}\n"
            f"Target budget: ~{max(1, budget_tokens)} tokens.\n"
            f"---\n{text}"
        )
        response = await model.ainvoke([{"role": "user", "content": prompt}])
        content = getattr(response, "content", response)
        return str(content)
