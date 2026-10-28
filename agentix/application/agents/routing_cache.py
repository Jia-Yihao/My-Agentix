"""路由结果跨请求缓存（进程内、有界 LRU + TTL）。

为什么需要它
------------
``analyze_and_route_query`` 每来一个问题都要构造一次 ``router`` 角色 LLM 并发一次
意图分类请求。同一个问句在一段时间内被反复问（前端重试、同一 FAQ 被多个人问、
评测集回放）时，这部分成本是纯重复——缓存命中的收益就是省掉**一整次模型调用**
（零客户端构造、零请求）。

为什么是**进程内**、不引 Redis
------------------------------
路由结果很小（四个字段）、价值仅在于省一次 LLM 调用；为它再拉一条外部连接
（Redis / 新连接池）只会多一种故障模式与一份运维负担。代价必须说清：
**每个进程各自一份、不跨副本共享**——多 worker 部署时命中率按进程分摊，
进程重启即清空。**本模块不是分布式缓存**，不要按分布式缓存的语义使用它。

键 / 值 / 换代（三段）
----------------------
* **键**：``sha256(normalized_question)`` 的十六进制摘要 **+ generation token**。
  问句先 ``strip()`` 并把连续空白折叠成单个空格，因此纯排版差异不产生两个键；
  **问句原文绝不作为键**——键里不留用户内容，键进日志/指标也不会泄露原文。
* **值**：只存 ``{"type", "logic", "decision", "confidence"}`` 四个字段。
  - **不存 ``question``**：命中时用**当前这次请求的原文**回填，缓存里因此不含
    任何用户输入；
  - **不存 ``reasoning``**：它是模型的叙述性文本，可能整段复述问句（等于又把
    原文存了一份）；而且生产侧**没有任何消费点读它**——全仓只有
    ``code_handler.py:326`` / ``task_handler.py:105`` 把它从上一个 ``Router``
    原样拷进下一个，没有分支按它决策。省空间只是附带收益，主要理由是"不存一个
    没有消费者的原文派生字段"。
* **换代失效（Q1 未裁决时的保守口径）**：generation token 由四项拼起来取 CRC32
  ——① router 提示词**实际生效文本**（含 ``TASK_EXEC_ROUTE_BLOCK`` /
  ``CODE_EXEC_ROUTE_BLOCK`` 是否拼入）；② 实际使用的 router 类名（``Router`` /
  ``TaskRouter`` / ``CodeRouter`` / ``TaskCodeRouter``，由既有 ``_router_class``
  决定）；③ 主模型标识（``settings.LLM_MODEL``，只读配置，**不为此构造 LLM
  客户端**）；④ ``ablation.is_enabled("router_enabled")``。任一变化 ⇒ 键变了
  ⇒ 旧条目再也命中不到（随后被 LRU / TTL 回收）。
* **适用范围（已知边界）：只服务单轮请求**。调用点只在 ``state.messages`` 恰好
  1 条时才查、才写；历史窗口 >1 条时既不查也不写。原因是**键只覆盖末句**，而
  分类器输入是 ``[system] + state.messages``（含窗口历史）——两段历史不同、末句
  相同的请求会共用一条结论，且缓存值里的 ``logic`` 会被下游拼进 system prompt。
  只加查侧判据不够：多轮请求照样会把按末句算出的条目写进来，后面的单轮同问句
  会命中它，所以**写侧必须同一判据**。要让多轮请求也命中，得先把历史并入键。

  失效口径的**边界**（不要读成"正确性已证明"）：

  - **语料换代不会让本缓存失效**：路由提示词里没有语料，语料变化不改变"这句话
    该去哪条支路"这个判定；
  - **进程重启自然失效**（纯内存态，无持久化）；
  - **TTL 到期失效**（``_TTL_S``）；
  - 以上是 Q1（缓存失效口径）未裁决时的保守版，**不等于 Q1 的答案**：Q1 若要求
    按语料 / 知识库版本失效，只需在 :func:`generation_token` 里再加一个维度。

开关与 fail-open
----------------
业务总闸 ``settings.ROUTING_CACHE_ENABLED`` 默认 **关**；调用点
（``router.analyze_and_route_query``）把 import 放在 flag 判真之后，因此 flag-off
时**本模块连加载都不会发生**（用例读 ``sys.modules`` 钉死）。

读写与重建的异常一律由调用点捕获，``logger.warning`` 留痕后**当作未命中**
（AGENTS.md 纪律 2）：缓存是加速器，它坏掉绝不能把一次问答弄挂。
"""

from __future__ import annotations

import hashlib
import re
import time
import zlib
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from agentix.config import ablation, settings

#: 条目存活上限（秒）。路由结论不会在几分钟内失效，"过期即重算"就是这里的安全网。
_TTL_S = 300.0

#: 容量上限。单条值只有四个短字段，1024 条约几十 KB——足够覆盖热问句，又不会
#: 让常驻内存随问答量增长。
_MAX_ENTRIES = 1024

#: 进缓存的值字段（**封闭集合**）：``question`` 与 ``reasoning`` 刻意不在其中，
#: 理由见模块 docstring。
_CACHED_FIELDS: Tuple[str, ...] = ("type", "logic", "decision", "confidence")

#: 连续空白折叠成单个空格——排版差异不该产生两个缓存条目。
_WHITESPACE = re.compile(r"\s+")


def normalize_question(question: Optional[str]) -> str:
    """问句归一化：``strip()`` + 连续空白折叠。"""
    return _WHITESPACE.sub(" ", (question or "").strip())


def question_digest(question: Optional[str]) -> str:
    """归一化问句的 sha256 十六进制摘要（键的前半段；原文不进键）。"""
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()


def generation_token() -> str:
    """当前"路由换代令牌"：四项拼起来取 CRC32，8 位十六进制。

    为什么每请求现算而不缓存令牌：四项里三项是 ``settings`` 的读取、一项是既有
    :func:`ablation.is_enabled`；CRC32 在约 2KB 提示词上耗时微秒级，远低于一次
    LLM 调用。反过来把令牌缓存在进程里，会让"开关/提示词变了但令牌没变"这种
    静默失配变成新的故障模式（纪律 6 ④：判定只有一个源，不要多造一个快照）。

    四项的取值**全部复用既有判定的唯一来源**（``_exec_flags`` /
    ``_router_prompt_text`` / ``_router_class`` / ``settings`` / ``ablation``），
    本模块不另立平台或开关判点。
    """
    # 延迟 import：本模块被 router 惰性加载时 router 已就绪；反向 import 放在
    # 函数内可避免模块级循环依赖。
    from agentix.application.agents import router as router_module

    task_exec_on, code_exec_on = router_module._exec_flags()
    parts = (
        router_module._router_prompt_text(task_exec_on, code_exec_on),
        router_module._router_class(task_exec_on, code_exec_on).__name__,
        str(getattr(settings, "LLM_MODEL", "") or ""),
        "1" if ablation.is_enabled("router_enabled") else "0",
    )
    joined = "\x1f".join(parts)
    return format(zlib.crc32(joined.encode("utf-8")) & 0xFFFFFFFF, "08x")


def _cache_key(question: Optional[str], generation: str) -> str:
    return f"{generation}:{question_digest(question)}"


class RoutingCache:
    """有界 LRU + TTL 的路由结果缓存（一个进程一份，见 :func:`get_routing_cache`）。

    ``OrderedDict`` 的插入序即 LRU 序：命中与写入都 ``move_to_end``，淘汰永远从
    头部（最久未用）取走，容量由 ``max_entries`` 封死。
    """

    def __init__(self, *, ttl_s: float = _TTL_S, max_entries: int = _MAX_ENTRIES) -> None:
        self._ttl_s = float(ttl_s)
        self._max_entries = int(max_entries)
        #: 键 → (到期时刻, 四字段值)。测试要直接对账「键里没有原文、值里没有
        #: question/reasoning」，因此它是可读的内部存储，不另开只读视图。
        self._entries: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()

    @property
    def entries(self) -> "OrderedDict[str, Tuple[float, Dict[str, Any]]]":
        """内部存储的只读句柄（对账用：``len`` / 迭代键 / 读值）。"""
        return self._entries

    def get(self, question: Optional[str], *, generation: str) -> Optional[Dict[str, Any]]:
        """查一条；未命中 / 已过期返回 ``None``。返回的是**副本**，改它不污染缓存。"""
        key = _cache_key(question, generation)
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            # 过期条目当场摘掉：对外"过期"与"没有缓存"是同一件事
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return dict(value)

    def put(self, question: Optional[str], router_obj: Any, *, generation: str) -> None:
        """写一条（只取 :data:`_CACHED_FIELDS` 四个字段），并维持容量上限。"""
        key = _cache_key(question, generation)
        value: Dict[str, Any] = {
            field: getattr(router_obj, field, None) for field in _CACHED_FIELDS
        }
        value["logic"] = value.get("logic") or ""
        self._entries[key] = (time.monotonic() + self._ttl_s, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


#: 进程内单例。多副本部署时每个进程各自一份，**不共享**（见模块 docstring）。
_CACHE = RoutingCache()


def get_routing_cache() -> RoutingCache:
    """取进程内单例缓存。"""
    return _CACHE


def reset_routing_cache() -> None:
    """清空进程内缓存（测试隔离与人工操作用）。"""
    _CACHE.clear()


__all__ = [
    "RoutingCache",
    "generation_token",
    "get_routing_cache",
    "normalize_question",
    "question_digest",
    "reset_routing_cache",
]
