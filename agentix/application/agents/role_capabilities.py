"""按角色差异化：工具集与上下文策略的**声明式**定义。

《My Agentix_架构优化方案》§9.1 D 组缺口「各角色在模型/工具集/上下文真实差异化」：
现状 —— 六个角色的**模型配置**已齐备并接线（`LLMProfile` 的温度/超时/重试，
批次 5 接线 4 处），但**工具集与上下文策略仍完全共享**，故该条维持 ⚠️。

**为什么工具集也要差异化**：给「路由」角色挂上 `cypher_query` 是**纯噪声**——
它只做意图分类，多出来的工具 schema 既占 token（每轮都进 prompt），
又制造误选机会。同理，「摘要」角色不需要工具；「幻觉判定」只需要看答案，
不需要重新检索。**工具集是按角色裁剪的，不是全局共享的。**

**为什么上下文也要差异化**：路由只需要最近一两轮对话；
摘要需要完整检索结果；通用问答需要历史 + 记忆。给路由灌入 20 轮历史
既无收益又抬高延迟与成本。

**本模块只做声明，不做执行**：它是一张**可检视、可测试、可评审**的表格
（与 `infrastructure/tools/registry_init.py` 的声明式清单同构）。
执行侧按需查询 :func:`tools_for_role` / :func:`context_policy_for`。

设计约束：**零外部依赖**（不 import infrastructure / interfaces），
故可用在任意层；工具名以字符串声明，实际可用性由调用方与注册表求交。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional

from agentix.application.agents.llm_factory import Role

__all__ = [
    "ContextPolicy",
    "RoleCapability",
    "ROLE_CAPABILITIES",
    "ALL_TOOLS",
    "WILDCARD_ALL",
    "tools_for_role",
    "context_policy_for",
    "capability_summary",
    "roles_sharing_tools",
]

#: 本框架已知的全部工具名（与 `registry_init._build_declarations` 对齐；
#: 用元组声明以保持「零依赖」，实际可用集由调用方传入求交）
ALL_TOOLS: tuple = (
    "cypher_query",
    "predefined_cypher",
    "microsoft_graphrag_query",
    "text2sql_query",
    "search",
)

#: 工具名通配符：表示「按调用方给定的可用集全给」
WILDCARD_ALL = "*"


@dataclass(frozen=True)
class ContextPolicy:
    """角色的上下文装配策略（数值全部可被 settings 覆盖）。"""

    #: 注入的历史消息条数上限（0 = 不注入历史）
    max_history_messages: int = 20
    #: 是否注入长期记忆摘要
    include_memory: bool = False
    #: 是否把检索到的证据（sources）注入上下文
    include_sources: bool = True
    #: 检索结果注入条数上限（仅 include_sources 时生效）
    max_retrieved_docs: int = 5
    #: 是否注入工具清单（纯判定角色不需要）
    include_tools: bool = True

    def to_dict(self) -> Dict[str, object]:
        return {
            "max_history_messages": self.max_history_messages,
            "include_memory": self.include_memory,
            "include_sources": self.include_sources,
            "max_retrieved_docs": self.max_retrieved_docs,
            "include_tools": self.include_tools,
        }


@dataclass(frozen=True)
class RoleCapability:
    """一个角色的完整能力声明（模型侧见 `LLMProfile`，此处是工具 + 上下文）。"""

    role: Role
    tools: FrozenSet[str]
    context: ContextPolicy
    rationale: str = ""
    #: 是否参与「写」类动作（供人工审批策略参考）
    may_write: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "role": self.role,
            "tools": sorted(self.tools),
            "context": self.context.to_dict(),
            "may_write": self.may_write,
            "rationale": self.rationale,
        }


#: 角色能力表（**唯一事实来源**）
ROLE_CAPABILITIES: Dict[Role, RoleCapability] = {
    "router": RoleCapability(
        role="router",
        tools=frozenset(),  # 纯意图分类：给工具只会制造误选
        context=ContextPolicy(
            max_history_messages=4,   # 分类只需最近一两轮
            include_memory=False,
            include_sources=False,    # 分类不基于证据
            include_tools=False,      # 不暴露工具清单
        ),
        rationale="只做意图分类，工具与证据均属噪声；历史越短越省 token、越稳",
    ),
    "hallucination": RoleCapability(
        role="hallucination",
        tools=frozenset(),  # 只审已生成的答案，不重新检索
        context=ContextPolicy(
            max_history_messages=6,
            include_memory=False,
            include_sources=True,     # 需要证据来对照答案
            max_retrieved_docs=8,     # 判定需要看全证据
            include_tools=False,
        ),
        rationale="判「答案是否有据」需要完整证据，但绝不能再检索（否则无法判原始答案）",
    ),
    "planner": RoleCapability(
        role="planner",
        tools=frozenset(ALL_TOOLS),  # 规划需要知道有哪些工具可用
        context=ContextPolicy(
            max_history_messages=10,
            include_memory=True,      # 规划受益于历史偏好
            include_sources=False,    # 规划阶段尚无证据
            include_tools=True,
        ),
        rationale="规划要「知道有哪些工具」，但不需要证据正文",
    ),
    "tool": RoleCapability(
        role="tool",
        tools=frozenset(ALL_TOOLS),  # 工具调用角色：全集
        context=ContextPolicy(
            max_history_messages=10,
            include_memory=False,
            include_sources=True,
            max_retrieved_docs=5,
            include_tools=True,
        ),
        rationale="执行工具选择与参数构造，需要完整工具集与部分证据上下文",
    ),
    "summarizer": RoleCapability(
        role="summarizer",
        tools=frozenset(),  # 摘要阶段工具调用已结束
        context=ContextPolicy(
            max_history_messages=8,
            include_memory=False,
            include_sources=True,
            max_retrieved_docs=20,    # 摘要是「把证据讲清楚」，需要尽量全
            include_tools=False,
        ),
        rationale="工具阶段已结束；上下文重点在证据覆盖度而非工具清单",
    ),
    "general": RoleCapability(
        role="general",
        tools=frozenset(ALL_TOOLS),
        context=ContextPolicy(
            max_history_messages=20,  # 通用问答最依赖对话历史
            include_memory=True,      # 跨会话记忆在此最有价值
            include_sources=True,
            max_retrieved_docs=5,
            include_tools=True,
        ),
        rationale="面向用户的主生成角色：历史 + 记忆 + 证据三者都最完整",
    ),
}


def tools_for_role(role: str, available: Optional[Iterable[str]] = None) -> List[str]:
    """返回该角色实际可用的工具名（与 ``available`` 求交，稳定排序）。

    :param available: 调用方给定的可用工具集（如经 `DISABLED_TOOLS` 裁剪后的集合）；
        ``None`` 表示用本模块的 ``ALL_TOOLS``。
    未知角色 → 返回空列表（**保守**：不认识的角色不给工具）。
    """
    capability = ROLE_CAPABILITIES.get(role)  # type: ignore[arg-type]
    if capability is None:
        return []
    pool = set(available) if available is not None else set(ALL_TOOLS)
    return sorted(capability.tools & pool)


def context_policy_for(role: str) -> ContextPolicy:
    """返回该角色的上下文策略；未知角色返回**最保守**策略（不注入记忆/工具）。"""
    capability = ROLE_CAPABILITIES.get(role)  # type: ignore[arg-type]
    if capability is None:
        return ContextPolicy(
            max_history_messages=0,
            include_memory=False,
            include_sources=False,
            max_retrieved_docs=0,
            include_tools=False,
        )
    return capability.context


def capability_summary() -> Dict[str, Dict[str, object]]:
    """全表摘要（便于快照测试与文档生成）。"""
    return {role: cap.to_dict() for role, cap in ROLE_CAPABILITIES.items()}


def roles_sharing_tools() -> Dict[str, List[str]]:
    """反向索引：工具 → 使用它的角色列表（便于评估「某工具该不该保留」）。"""
    index: Dict[str, List[str]] = {name: [] for name in ALL_TOOLS}
    for role, capability in ROLE_CAPABILITIES.items():
        for name in capability.tools:
            index.setdefault(name, []).append(role)
    return {name: sorted(roles) for name, roles in index.items()}
