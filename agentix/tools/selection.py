"""工具选择过滤链（R3-4，Agent Runtime 架构文档 §17）。

核心原则：**LLM 不应该看到它没有权限使用的 Tool。**

选择链（文档 §17）::

    Tool Registry → Capability Filter → Permission Filter → Risk Filter
    → Context Filter → Tool Ranking → LLM Tool Selection

P0 落地范围：risk（policy.max_risk_level）→ 白/黑名单（allowed/denied）→
capabilities 三层过滤；以 :class:`ToolSelectionPolicy` 承载策略，
消费点为 tool_selection 节点（config["configurable"]["tool_policy"]，
缺省 None = 不过滤，行为与既有完全一致——Strangler 零行为变化纪律）。

风险等级沿用指南 v2.0 的 L0-L3 分级（§2.1.3），与文档 §18 的
LOW/MEDIUM/HIGH/CRITICAL 语义一一对应（映射唯一真源见
``human_review.declared_level_for``）。

**与执行层闸门的分工**（两者不是冗余，缺一即漏）：

- **本模块（选择层，声明级）**：在 LLM 选工具**之前**把不该看见的工具摘掉。
  此时还没有参数，因此只能按 ``spec.risk_level`` 过滤 —— 这是唯一可行的粒度。
- **执行层（有效级，参数感知）**：``ToolExecutor.execute`` 用
  ``max(声明级, 名称级, 参数提权)`` 判定。一个声明 ``L0`` 的只读工具被
  调用时带上 ``all=true``，选择层放它过去，执行层必须拦住。

故：**选择层用声明级是对的，但绝不能把它当作唯一防线**；反过来，
"选择层已按 L1 上限过滤"也不能替代执行层的有效级判定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from loguru import logger
from pydantic import BaseModel

from agentix.tools.definitions import RISK_LEVELS, ToolSpec, registry as tool_registry

#: P2（Tool Ranking 层）：查询/描述分词——ASCII 词（含 snake_case 拆分后的
#: 片段）+ CJK 连续段的 bigram（单字段 run 取单字）。CJK 用 bigram 而非
#: 单字：单字重叠（"的/了/查"）噪声太高，bigram（"法条/查询"）才是短查询
#: 与中文 description 的有效交集。
_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]+")


@dataclass(frozen=True)
class ToolSelectionPolicy:
    """一次请求的工具可见性策略（文档 §17 过滤链的输入）。

    - ``max_risk_level``：允许暴露给 LLM 的最高风险等级（默认 L1——
      L2 需人工确认的工具不应在无审批通道时进入工具列表）；
    - ``allowed_tools``：白名单（None = 不限制）；与黑名单同时生效，
      白名单优先语义 =「不在白名单即不可见」；
    - ``denied_tools``：黑名单（显式排除）；
    - ``allowed_capabilities``：能力标签白名单（None = 不限制）。
    """

    max_risk_level: str = "L1"
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = frozenset()
    allowed_capabilities: frozenset[str] | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_risk_level not in RISK_LEVELS:
            raise ValueError(
                f"invalid max_risk_level '{self.max_risk_level}' (must be one of {RISK_LEVELS})"
            )


def _risk_rank(level: str) -> int:
    return RISK_LEVELS.index(level)


def _spec_for_schema(schema: type[BaseModel]) -> ToolSpec | None:
    """按 schema title 回查注册表 spec（title == 工具注册名）。"""
    title = schema.model_json_schema().get("title", "")
    if title and tool_registry.has_tool(title):
        return tool_registry.get_spec(title)
    return None


def select_tool_schemas(
    schemas: Sequence[type[BaseModel]],
    policy: ToolSelectionPolicy,
    *,
    spec_overrides: Iterable[ToolSpec] = (),
) -> list[type[BaseModel]]:
    """按策略过滤 LLM 可见的工具 schema（纯函数，文档 §17 选择链）。

    过滤顺序：risk → 白/黑名单 → capabilities。**无 spec 的 schema 默认保留**
    （fail-open：声明缺失不误杀既有工具；配合 test_r1_tool_risk_levels
    守卫确保声明完备性）。

    :param spec_overrides: 测试/扩展用显式 spec 旁路（优先于 registry 查询）
    """
    overrides = {spec.name: spec for spec in spec_overrides}
    kept: list[type[BaseModel]] = []
    max_rank = _risk_rank(policy.max_risk_level)

    for schema in schemas:
        title = schema.model_json_schema().get("title", "")
        spec = overrides.get(title) or _spec_for_schema(schema)

        # 1) 风险闸门（声明缺失 → fail-open 保留）
        if spec is not None and _risk_rank(spec.risk_level) > max_rank:
            continue

        # 2) 白/黑名单
        if policy.allowed_tools is not None and title not in policy.allowed_tools:
            continue
        if title in policy.denied_tools:
            continue

        # 3) capabilities
        if policy.allowed_capabilities is not None:
            if spec is None:
                continue  # 无声明即无能力标签 → 能力受限请求下不可见
            if not (set(spec.capabilities) & set(policy.allowed_capabilities)):
                continue

        kept.append(schema)
    return kept


# -- P2（岗位对照·动态调度组合）：策略注入方 + Tool Ranking 层 ----------------


def build_tool_policy() -> ToolSelectionPolicy | None:
    """由 settings 构造本次请求的可见性策略（``tool_policy`` 注入方）。

    ``TOOL_SELECTION_ENABLED=false``（默认）或 ``TOOL_SELECTION_MAX_RISK``
    非法时返回 ``None`` = 调用方不注入 ``configurable.tool_policy``，
    tool_selection 节点收到 ``None`` 即走不过滤链——与基线逐位等价。
    非法值**不抛**（配置错误不该打挂单次请求）：记 error 后按不注入处理。
    """
    from agentix.config.settings import settings  # 函数内导入：避免 config 循环

    if not bool(getattr(settings, "TOOL_SELECTION_ENABLED", False)):
        return None
    max_risk = str(getattr(settings, "TOOL_SELECTION_MAX_RISK", "") or "").strip() or "L1"
    try:
        return ToolSelectionPolicy(max_risk_level=max_risk)
    except ValueError as exc:
        logger.error("invalid TOOL_SELECTION_MAX_RISK={!r}, tool_policy not injected: {}", max_risk, exc)
        return None


def tool_permission_check(tool_name: str, spec: ToolSpec | None, policy: Any) -> bool:
    """执行侧权限兜底（§17 Permission Filter 的执行层镜像，A2a）。

    由 ``get_tool_executor()`` 在 ``TOOL_SELECTION_ENABLED`` 打开（即
    ``build_tool_policy()`` 注入成功）时挂到
    ``ToolExecutor(permission_checker=...)``——与选择层同源同策略：

    - ``settings.DISABLED_TOOLS``（逗号分隔工具名）先判：声明层裁剪
      （``registry_init``）挡的是「LLM 看不见」，这里是同名配置的执行侧兜底；
    - 策略黑名单 ``denied_tools`` 命中 → 拒；白名单 ``allowed_tools``
      （非 None）不含 → 拒；能力白名单 ``allowed_capabilities``（非 None）
      与 spec 无交集（或 spec 缺失 → 无标签可证）→ 拒；其余放行。

    flag 关时本函数**根本不会被注入**（``build_tool_policy()`` 返回 ``None``），
    执行链与基线逐位等价。``ToolExecutor`` 对本函数的故障按拒绝处理
    （fail-closed），故内部不吞异常。
    """
    from agentix.config.settings import settings  # 函数内导入：与 build_tool_policy 同款

    disabled = {
        name.strip()
        for name in str(getattr(settings, "DISABLED_TOOLS", "") or "").split(",")
        if name.strip()
    }
    if tool_name in disabled:
        return False
    if policy is None:
        return True
    denied = getattr(policy, "denied_tools", None) or ()
    if tool_name in denied:
        return False
    allowed = getattr(policy, "allowed_tools", None)
    if allowed is not None and tool_name not in allowed:
        return False
    allowed_caps = getattr(policy, "allowed_capabilities", None)
    if allowed_caps is not None:
        if spec is None:
            return False
        if not (set(spec.capabilities) & set(allowed_caps)):
            return False
    return True


def _tokenize(text: str) -> set[str]:
    """分词为匹配 token 集（ASCII 词 + CJK bigram，见 ``_TOKEN_RE`` 注释）。"""
    tokens: set[str] = set()
    for match in _TOKEN_RE.finditer(text.lower()):
        piece = match.group(0)
        if "一" <= piece[0] <= "鿿":
            if len(piece) == 1:
                tokens.add(piece)
            else:
                tokens.update(piece[i : i + 2] for i in range(len(piece) - 1))
        else:
            tokens.add(piece)
    return tokens


def score_tool_match(name: str, description: str, query: str) -> float:
    """query 对 (工具名, 描述) 的相关度打分（0 = 无交集；确定性、无 I/O）。

    命名中命中权重 2、描述命中权重 1，按 query token 数归一——
    便于跨工具比较，也保证同分时调用方可按声明序稳定裁剪。
    """
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    name_hits = len(q_tokens & _tokenize(name))
    desc_hits = len(q_tokens & _tokenize(description))
    return (2 * name_hits + desc_hits) / len(q_tokens)


def rank_tool_schemas(
    schemas: Sequence[type[BaseModel]],
    query: str,
    *,
    top_k: int = 8,
) -> list[type[BaseModel]]:
    """Tool Ranking（选择链 ``… → Tool Ranking → LLM Tool Selection`` 的第五层）。

    按 ``score_tool_match`` 对 title + description 打分，**降序 + 声明序**
    稳定排序后取前 ``top_k``；空查询/全零分按声明序截断（不静默丢工具——
    与 ``select_tool_schemas`` 的 fail-open 纪律一致）。纯函数，无 I/O。
    """
    scored: list[tuple[float, int, type[BaseModel]]] = []
    for idx, schema in enumerate(schemas):
        js = schema.model_json_schema()
        title = str(js.get("title") or "")
        desc = str(js.get("description") or schema.__doc__ or "")
        scored.append((-score_tool_match(title, desc, query), idx, schema))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [schema for _, _, schema in scored[: max(0, top_k)]]
