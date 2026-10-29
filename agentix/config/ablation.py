"""消融实验特性开关（Ablation Flags）——``AGENTIX_ABLATION_*`` 的唯一读取入口。

为什么需要这个模块
------------------
``evals/ablations/variants.json`` 定义了 A–G 七个变体，``evals/ablations/run_ablation.py``
会把每个变体的 flags 以 ``AGENTIX_ABLATION_<FLAG>=true/false`` 注入子进程环境。

但在本模块落地之前，**没有任何生产代码读取过这些变量**：注入是「只写不读」的，
七个变体跑出来必然逐位相同，消融表会是一张七行全等的表，而它看起来完全正常。
本模块补上读取端，并把「开关是否真的接进管线」这件事变成代码里可断言的事实。

**本模块最重要的不变式：不设置任何 ``AGENTIX_ABLATION_*`` 时，全部取
:data:`FLAG_DEFAULTS`（= 生产链路的真实现状），因此对现网行为零影响。**
只有显式注入非默认值时行为才会改变。

三类开关
--------
* ``WIRED_FLAGS`` —— 已接线。改动会真正改变生产行为，每一项都注明了接线点，
  审计时可直接跳过去看那行代码。
* ``UNWIRED_FLAGS`` —— 未接线。代码里**不存在**可切换的实现，设成非默认值等于
  要求代码做不到的事。编排器会据此拒绝该变体（见 ``run_ablation`` 的契约守卫），
  而不是让它跑出一份「看起来有效、实则与基线不可区分」的结果。
* ``INFORMATIONAL_FLAGS`` —— 信息性标记，只用于标识变体身份（如 ``full_pipeline``），
  不参与开关判定。

用法
----
::

    from agentix.config import ablation

    if not ablation.is_enabled("retrieval_enabled"):
        ...  # 跳过检索，直接作答

    flags = ablation.load_ablation_flags()
    flags.wired_overrides      # 真正会改变行为的开关
    flags.unwired_overrides    # 声明了但代码做不到的开关（应为空，否则编排器拒绝）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

#: 环境变量前缀。与 ``evals/ablations/run_ablation.py`` 的 ``FLAG_ENV_PREFIX`` 一致。
ENV_PREFIX = "AGENTIX_ABLATION_"

#: 各开关的「现状默认值」——即不注入任何环境变量时，生产链路的真实行为。
#:
#: 这些值必须反映 **代码现在实际怎么跑**，而不是实验设计上希望怎么跑：
#:
#: * ``retrieval_enabled=True``：``create_kb_query`` 默认走 KB 多工具检索。
#: * ``router_enabled=True``：``analyze_and_route_query`` 默认调 LLM 做意图分类。
#: * ``tools_enabled=True``：``create_research_plan`` 默认进入 KG 多工具子图。
#: * ``memory_enabled=False``：``settings.MEMORY_CONTEXT_ENABLED`` 默认 ``False``
#:   （见 ``agentix/config/settings.py``），长期记忆召回默认关闭。
#: * ``multi_agent=False``：主图为**扁平图**（``lg_builder.build_graph`` 注册
#:   9 节点）；``True`` ⇒ ``lg_builder.get_active_graph`` 切到 supervisor 拓扑
#:   （Phase 19 接线，见 ``WIRED_FLAGS``）。
#: * ``full_pipeline=True``：信息性标记。
FLAG_DEFAULTS: Dict[str, bool] = {
    "retrieval_enabled": True,
    "router_enabled": True,
    "tools_enabled": True,
    "memory_enabled": False,
    "multi_agent": False,
    "full_pipeline": True,
}

#: 已接线的开关 → 接线点（file:line 语义，供审计直接定位）。
WIRED_FLAGS: Dict[str, str] = {
    "retrieval_enabled": (
        "agentix/application/agents/general_handler.py::create_kb_query "
        "（False ⇒ 跳过 KB/图谱检索，直接 LLM 直答）"
    ),
    "router_enabled": (
        "agentix/application/agents/router.py::analyze_and_route_query + route_query "
        "（False ⇒ 不调 LLM 意图分类，固定走 kb-query 通道）"
    ),
    "tools_enabled": (
        "agentix/application/agents/research_handler.py::create_research_plan "
        "（False ⇒ 不进入 KG 多工具子图，退回直接 LLM 作答）"
    ),
    "memory_enabled": (
        "agentix/application/services/chat_service.py::_maybe_memory_context "
        "（True ⇒ 在 settings.MEMORY_CONTEXT_ENABLED 之外额外开启长期记忆召回）"
    ),
    "multi_agent": (
        "agentix/orchestration/langgraph/lg_builder.py::get_active_graph + "
        "agentix/orchestration/langgraph/lg_supervisor.py::supervisor_dispatch "
        "（True ⇒ 图切 supervisor 拓扑：message_window → analyze_and_route_query → "
        "supervisor_dispatch，节点内 Manager 协调 Proposer=路由子 agent / Reviewer）"
    ),
}

#: 未接线的开关 → 为什么接不上。设成非默认值即视为「无效应变体」。
#: Phase 19（2026-09-26）起 ``multi_agent`` 已迁入 WIRED_FLAGS，当前为空——
#: 机制保留：未来新增开关未接线时仍应登记在此，由 run_ablation 契约守卫拒绝。
UNWIRED_FLAGS: Dict[str, str] = {}

#: 信息性标记：只标识变体身份，不参与开关判定，不会触发「未接线」拒绝。
INFORMATIONAL_FLAGS: Tuple[str, ...] = ("full_pipeline",)


def _coerce_bool(raw: str) -> Optional[bool]:
    """把环境变量的字符串值解析成 bool；无法识别时返回 None。"""
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return None


@dataclass(frozen=True)
class AblationFlags:
    """一次进程生命周期内生效的消融开关快照。"""

    #: 开关名 → 生效值（含默认值）
    values: Mapping[str, bool]
    #: 显式注入且与默认值不同的开关（原始解析结果，含未接线项）
    explicit_overrides: Mapping[str, bool]
    #: 无法解析的环境变量（如 AGENTIX_ABLATION_FOO=maybe）
    invalid: Mapping[str, str]

    def get(self, name: str) -> bool:
        """取开关的生效值；未知开关按 ``False`` 处理。"""
        return bool(self.values.get(name, False))

    def is_default(self, name: str) -> bool:
        """该开关是否仍处于默认值（未被显式覆盖）。"""
        return name not in self.explicit_overrides

    @property
    def wired_overrides(self) -> Dict[str, bool]:
        """显式覆盖且已接线的开关——这些才是真正改变行为的。"""
        return {
            k: v
            for k, v in self.explicit_overrides.items()
            if k in WIRED_FLAGS
        }

    @property
    def unwired_overrides(self) -> Dict[str, bool]:
        """显式覆盖但**未接线**的开关——代码做不到，编排器应拒绝该变体。"""
        return {
            k: v
            for k, v in self.explicit_overrides.items()
            if k in UNWIRED_FLAGS
        }

    @property
    def is_effectively_default(self) -> bool:
        """所有显式覆盖都落在已接线上（或没有覆盖）⇒ 变体语义可被代码满足。"""
        return not self.unwired_overrides

    def describe(self) -> str:
        """一行摘要，便于写进评测报告的元数据。"""
        overrides = ", ".join(
            f"{k}={str(v).lower()}" for k, v in sorted(self.explicit_overrides.items())
        )
        return overrides or "(all defaults)"


def load_ablation_flags(env: Optional[Mapping[str, str]] = None) -> AblationFlags:
    """从环境变量读取消融开关。

    Parameters
    ----------
    env : Mapping[str, str], optional
        环境变量映射；默认取 ``os.environ``。测试可传入字典以免污染进程环境。

    Returns
    -------
    AblationFlags
        开关快照。无法识别的取值会记录在 ``invalid`` 中并退回默认值
        （**不抛异常**：一个写错的环境变量不该把整条问答链路打挂）。
    """
    source: Mapping[str, str] = os.environ if env is None else env

    values = dict(FLAG_DEFAULTS)
    explicit: Dict[str, bool] = {}
    invalid: Dict[str, str] = {}

    for key, raw in source.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX) :].lower()
        if name not in FLAG_DEFAULTS:
            invalid[name] = raw  # 未知开关名，记下来
            continue
        parsed = _coerce_bool(raw)
        if parsed is None:
            invalid[name] = raw
            continue
        values[name] = parsed
        if parsed != FLAG_DEFAULTS[name]:
            explicit[name] = parsed

    return AblationFlags(values=values, explicit_overrides=explicit, invalid=invalid)


def is_enabled(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """单开关便捷查询（热路径用）。

    刻意不做缓存：``os.environ`` 在进程生命周期内不变，读 6 个键的成本远低于
    缓存失效带来的测试串扰风险。调用点都是每请求一次量级，不构成热点。
    """
    return load_ablation_flags(env).get(name)


__all__ = [
    "ENV_PREFIX",
    "FLAG_DEFAULTS",
    "WIRED_FLAGS",
    "UNWIRED_FLAGS",
    "INFORMATIONAL_FLAGS",
    "AblationFlags",
    "load_ablation_flags",
    "is_enabled",
]
