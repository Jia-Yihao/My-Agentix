"""Phase 28（Ch9 §9.3.2）：需求澄清决策——确定性启发，零新增 LLM。

澄清是**正常控制流**，不是异常：不计失败、不入 HITL、不改路由终态。
flag 门在 ``general_handler._maybe_clarify``（本模块保持纯函数，便于离线测）。

信号（首命中即定论，reason 单值可解释）：

1. ``too_short``       —— 口语过短（stripped 长度 < ``MIN_QUESTION_CHARS``）；
2. ``domain_conflict`` —— 法域冲突（``DOMAIN_LEXICON`` 命中 ≥2 组）；
3. ``missing_subject`` —— 主体实体缺失（指示词在场、具体主体名词缺席）；
4. ``low_confidence``  —— 路由 ``Router.confidence`` 低于 ``CONFIDENCE_THRESHOLD``。

Gotcha：词表与澄清文案只用固定业务词，**不得含 PII**（沿既有脱敏通道）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from langchain_core.messages import AIMessage

__all__ = [
    "CONFIDENCE_THRESHOLD",
    "MIN_QUESTION_CHARS",
    "DOMAIN_LEXICON",
    "ClarifyDecision",
    "should_clarify",
    "clarify_reply",
]

#: 路由 confidence 低于此值 ⇒ 歧义（None = 未提供，不触发本信号）
CONFIDENCE_THRESHOLD: float = 0.5

#: 口语过短阈值（stripped 字符数）
MIN_QUESTION_CHARS: int = 6

#: 法域词表（组名 → 关键词）。命中 ≥2 组 ⇒ 法域冲突。
DOMAIN_LEXICON: Mapping[str, Tuple[str, ...]] = {
    "刑事": ("刑事", "犯罪", "判刑", "坐牢", "抢劫", "盗窃", "诈骗", "故意伤害", "取保"),
    "合同/债务": ("合同", "违约", "欠款", "借款", "债务", "借贷", "还款", "买卖", "租赁", "租房"),
    "婚姻家庭": ("离婚", "结婚", "抚养", "赡养", "继承", "婚姻", "彩礼", "出轨", "家暴"),
    "行政": ("行政复议", "行政诉讼", "行政处罚", "拆迁", "国土", "市场监管"),
}

#: 主体实体缺失：指示词（指代不明）在场……
_DEICTIC_TERMS: Tuple[str, ...] = ("这个", "那个", "上述", "这份", "那份", "此份")

#: ……且具体主体名词缺席（有其一即主体清楚）
_ENTITY_TERMS: Tuple[str, ...] = (
    "公司", "企业", "员工", "雇主", "甲方", "乙方", "买方", "卖方",
    "当事人", "法院", "律师", "合同", "本人", "我方", "平台", "政府",
)

#: 各 reason 的候选项（domain_conflict 动态取命中组名）
_OPTIONS: Mapping[str, Tuple[str, ...]] = {
    "too_short": ("我补充具体情形", "先给一般性说明"),
    "missing_subject": ("公司/企业相关", "个人之间", "其他主体"),
    "low_confidence": ("换个说法再问", "按常见情形先分析"),
}

#: 各 reason 的澄清文案（无 PII；候选项随后缀追加）
_PROMPTS: Mapping[str, str] = {
    "too_short": "在继续之前先和您确认一下：您的问题信息还比较少，请选择或补充具体情况。",
    "domain_conflict": "您提到的内容可能涉及多个法律领域，请先确认主要想咨询的方向。",
    "missing_subject": "您的问题里涉及的对象还不够明确，请先确认具体主体。",
    "low_confidence": "我对您的需求理解还不够确定，请先确认一下方向。",
}


@dataclass(frozen=True)
class ClarifyDecision:
    """澄清判定结果（纯值对象）。``should=False`` 时 reason/options 为空。"""

    should: bool
    reason: str = ""
    options: Tuple[str, ...] = ()


def should_clarify(question: str, *, confidence: Optional[float] = None) -> ClarifyDecision:
    """确定性澄清判定（纯函数、零 IO、零 LLM）。

    :param question: 当前用户输入（取 ``state.messages[-1].content``）。
    :param confidence: 路由 ``Router.confidence``；``None`` 视为未提供、不触发。
    """
    q = str(question or "").strip()
    if len(q) < MIN_QUESTION_CHARS:
        return ClarifyDecision(should=True, reason="too_short", options=_OPTIONS["too_short"])

    hit_groups = tuple(
        name for name, words in DOMAIN_LEXICON.items() if any(w in q for w in words)
    )
    if len(hit_groups) >= 2:
        return ClarifyDecision(
            should=True,
            reason="domain_conflict",
            options=hit_groups + ("以上都不是",),
        )

    if any(t in q for t in _DEICTIC_TERMS) and not any(t in q for t in _ENTITY_TERMS):
        return ClarifyDecision(should=True, reason="missing_subject", options=_OPTIONS["missing_subject"])

    if confidence is not None and confidence < CONFIDENCE_THRESHOLD:
        return ClarifyDecision(should=True, reason="low_confidence", options=_OPTIONS["low_confidence"])

    return ClarifyDecision(should=False)


def clarify_reply(decision: ClarifyDecision) -> AIMessage:
    """把澄清判定构造成结构化澄清应答（消息区；**不进状态栏/前缀**）。

    - ``content``：面向用户的中文澄清文案 + 候选项；
    - ``additional_kwargs["clarify"]``：``{"reason", "options"}`` 结构化候选，
      供上层/前端渲染；不含 hitl/degraded 标记（不入 HITL、不计失败）。
    """
    text = _PROMPTS.get(decision.reason, "在继续之前，我想先确认一下您的需求。")
    if decision.options:
        text += "您可以选择：" + "、".join(decision.options)
    msg = AIMessage(content=text)
    msg.additional_kwargs["clarify"] = {
        "reason": decision.reason,
        "options": list(decision.options),
    }
    return msg
