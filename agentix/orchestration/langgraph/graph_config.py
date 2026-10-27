"""图调用 config 的**唯一构造点**（LG-2-3 · ``recursion_limit`` 统一）。

## 为什么要有这个模块

``recursion_limit`` 是 LangGraph 的**顶层** config 键（塞进 ``configurable`` 图不认），
控制单次 invoke / astream 的超级步上限。此前它只在 ``chat_service`` 的两条入口显式
写出，而恢复路径（``lg_builder.resume_in_loop`` / ``breakpoint_resume.resume_thread``）
只带 ``configurable.thread_id`` ⇒ 落到框架默认的 **25**：同一张图，主链路按
``MAX_ITERATIONS``（默认 10）截断，恢复请求却能多跑一倍多。这不是"恢复更宽容"，
而是两条路径的轮数预算各说一套（纪律 6 ④：同一个判定不许两个源）。

集中后：三处只调 ``thread_config()``，轮数预算每次调用现读
``settings.MAX_ITERATIONS``（运行期改值即生效，不必重建 config 构造器），
config 形状只在本模块定义。
"""

from __future__ import annotations

from typing import Any, Dict

from agentix.config import settings

__all__ = ["thread_config"]


def thread_config(thread_id: str, **configurable: Any) -> Dict[str, Any]:
    """构造图调用 config：线程标识 + 顶层轮数预算。

    Args:
        thread_id: 线程标识（checkpointer 的隔离键）。
        **configurable: 其余 ``configurable`` 键（如 ``image_path`` / ``incremental``），
            由调用方按需追加；它们不参与轮数预算。

    Returns:
        ``{"configurable": {"thread_id": ..., **configurable}, "recursion_limit": MAX_ITERATIONS}``。
    """
    return {
        "configurable": {"thread_id": thread_id, **configurable},
        "recursion_limit": int(settings.MAX_ITERATIONS),
    }
