"""R3-6 GroundingValidator 测试（架构文档 §23-25 / §104：Evidence Layer）。

覆盖：
① 句子切分（中文边界、短句/纯数字过滤）
② bigram 归一化与包含度
③ validate_answer_grounding：锚定 / 幻觉 / 无证据 fail-open / 空答案
④ 阈值敏感性
⑤ 报告结构与观测模式标记
⑥ GroundingValidator 类封装（接口稳定，为语义级实现留接缝）
⑦ 接线面：AgentToolContext.last_grounding 字段
"""

from __future__ import annotations


# ---------- 测试数据 ----------

_EVIDENCE = [
    {
        "content": "《中华人民共和国劳动合同法》第三十九条规定，劳动者严重违反用人单位的规章制度的，用人单位可以解除劳动合同。",
        "source": "劳动合同法",
    },
    {
        "content": "试用期工资不得低于本单位相同岗位最低档工资或者劳动合同约定工资的百分之八十。",
        "source": "劳动合同法实施条例",
    },
]

_GROUNDED_ANSWER = (
    "根据《中华人民共和国劳动合同法》第三十九条，劳动者严重违反用人单位的规章制度的，"
    "用人单位可以解除劳动合同。"
    "此外，试用期工资不得低于本单位相同岗位最低档工资或者劳动合同约定工资的百分之八十。"
)

_HALLUCINATED_ANSWER = (
    "根据《中华人民共和国民法典》第九百九十九条，任何用人单位不得在节假日安排员工加班。"
    "同时，劳动者入职后即可享受每年二十天的带薪年假，无需工作满一年。"
)


# ---------- ① 句子切分 ----------

def test_split_claims_chinese_boundaries():
    from agentix.domain.policies.grounding_verify import split_claims

    text = (
        "第一句话内容足够长可以保留。第二句话也很长呀可以被保留！短句。"
        "第三句话；第四句话内容同样达到长度要求可以保留！"
    )
    claims = split_claims(text)
    # 短句（"短句。"）被过滤
    assert len(claims) == 3
    assert all(len(c) >= 8 for c in claims)


def test_split_claims_filters_numbers_and_short():
    from agentix.domain.policies.grounding_verify import split_claims

    text = "12345。\n1.2.3.\n这一句是实质内容没有问题。\n短。"
    claims = split_claims(text)
    assert claims == ["这一句是实质内容没有问题。".rstrip("。")]


def test_split_claims_empty():
    from agentix.domain.policies.grounding_verify import split_claims

    assert split_claims("") == []
    assert split_claims(None) == []  # type: ignore[arg-type]


# ---------- ② 归一化与包含度 ----------

def test_normalize_strips_punct_and_spaces():
    from agentix.domain.policies.grounding_verify import _normalize

    assert _normalize("合同 法，第二十条。") == "合同法第二十条"
    assert _normalize("ABC abc") == "abcabc"  # lower + 去空白
    assert _normalize("A B-C") == "abc"


def test_containment_basic():
    from agentix.domain.policies.grounding_verify import (
    _bigrams,
    _containment,
    _normalize,
)

    ev = _bigrams(_normalize("用人单位可以解除劳动合同"))
    claim_same = _bigrams(_normalize("用人单位可以解除劳动合同，但需通知工会"))
    claim_none = _bigrams(_normalize("员工每年享受二十天带薪年假"))
    assert _containment(claim_same, ev) > 0.5
    assert _containment(claim_none, ev) == 0.0
    # 空 bigram 防御
    assert _containment(set(), ev) == 0.0


# ---------- ③ validate_answer_grounding ----------

def test_validate_grounded_answer_high_score():
    from agentix.domain.policies.grounding_verify import validate_answer_grounding

    report = validate_answer_grounding(_GROUNDED_ANSWER, _EVIDENCE)
    assert report.skipped_reason is None
    assert report.total_claims == 2
    assert report.supported_claims == 2
    assert report.score == 1.0
    assert report.unsupported == []
    assert report.mode == "observe"


def test_validate_hallucinated_answer_low_score():
    from agentix.domain.policies.grounding_verify import validate_answer_grounding

    report = validate_answer_grounding(_HALLUCINATED_ANSWER, _EVIDENCE)
    assert report.skipped_reason is None
    assert report.total_claims == 2
    assert report.supported_claims == 0
    assert report.score == 0.0
    # 未锚定句进报告（预览截断）
    assert len(report.unsupported) == 2
    assert all(len(u) <= 60 for u in report.unsupported)


def test_validate_mixed_answer():
    from agentix.domain.policies.grounding_verify import validate_answer_grounding

    mixed = _GROUNDED_ANSWER + "另外员工每年自动获得三十天带薪年假无需申请。"
    report = validate_answer_grounding(mixed, _EVIDENCE)
    assert report.total_claims == 3
    assert report.supported_claims == 2
    assert 0.0 < report.score < 1.0
    assert len(report.unsupported) == 1


def test_validate_no_evidence_fail_open():
    """无证据不判定（fail-open）：与 citation_verify 同哲学。"""
    from agentix.domain.policies.grounding_verify import validate_answer_grounding

    report = validate_answer_grounding(_HALLUCINATED_ANSWER, [])
    assert report.skipped_reason == "no_evidence"
    assert report.score is None
    assert report.total_claims == 2
    # chunks 全空内容同样视作无证据
    report2 = validate_answer_grounding(_HALLUCINATED_ANSWER, [{"source": "x"}])
    assert report2.skipped_reason == "no_evidence"


def test_validate_empty_answer():
    from agentix.domain.policies.grounding_verify import validate_answer_grounding

    report = validate_answer_grounding("无法回答。", _EVIDENCE)
    assert report.skipped_reason == "empty_answer"
    assert report.total_claims == 0


# ---------- ④ 阈值敏感性 ----------

def test_validate_threshold_sensitivity():
    from agentix.domain.policies.grounding_verify import validate_answer_grounding

    partial = "用人单位单方解除劳动合同需要严格满足法定情形，且员工有权申请劳动仲裁维护自身权益。"
    low_threshold = validate_answer_grounding(partial, _EVIDENCE, support_threshold=0.2)
    high_threshold = validate_answer_grounding(partial, _EVIDENCE, support_threshold=0.9)
    # 阈值越低越容易判"有支撑"
    assert low_threshold.score >= high_threshold.score


# ---------- ⑤ 报告结构 ----------

def test_report_to_dict_shape():
    from agentix.domain.policies.grounding_verify import validate_answer_grounding

    report = validate_answer_grounding(_HALLUCINATED_ANSWER, _EVIDENCE)
    d = report.to_dict()
    assert set(d.keys()) == {
        "mode", "total_claims", "supported_claims", "score", "unsupported", "skipped_reason",
    }
    assert d["mode"] == "observe"


# ---------- ⑥ GroundingValidator 类封装 ----------

def test_grounding_validator_class_wrapper():
    from agentix.domain.policies.grounding_verify import GroundingValidator

    validator = GroundingValidator(support_threshold=0.9)
    report = validator.validate(_GROUNDED_ANSWER, _EVIDENCE)
    assert report.score == 1.0
    # 与等价函数调用结果一致
    from agentix.domain.policies.grounding_verify import validate_answer_grounding

    assert report.to_dict() == validate_answer_grounding(
        _GROUNDED_ANSWER, _EVIDENCE, support_threshold=0.9
    ).to_dict()


# ---------- ⑦ 接线面 ----------

def test_agent_tool_context_has_last_grounding():
    from agentix.retrieval.usecases.chat_agent_tools import AgentToolContext

    assert "last_grounding" in AgentToolContext.__dataclass_fields__


def test_settings_defaults_observe_mode():
    from agentix.retrieval.settings import settings

    assert settings.GROUNDING_VALIDATOR_ENABLED is True
    assert settings.GROUNDING_SUPPORT_THRESHOLD == 0.35
    assert settings.GROUNDING_LOW_SCORE_WARN == 0.6
