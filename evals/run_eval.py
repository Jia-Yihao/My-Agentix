"""P2-1 法律领域评估集：批量跑分 + 确定性打分 + CSV 汇总。

设计依据：``My Agentix_架构优化方案.md`` P2-1（第 1159–1180 行「建立法律领域评估集与回归」）。

核心原则（方案第 7 章「验证器优先确定性」）：

* **确定性指标**（默认、可离线、零外部依赖）
  - ``route_accuracy``：断言 ``route_type`` 是否与 golden 一致
  - ``citation_hit_rate``：期望法条是否出现在系统返回的 ``sources`` 中
  - ``reject_accuracy``：越界问题是否正确拒答/降级；非越界问题是否被误拒
* **语义相关度**（LLM-as-a-Judge）：仅作**辅助**指标，默认关闭；``judge_similarity``
  经真实 LLM 端点出分（带 ``judge_model`` 标注），无端点/输出不可解析时
  诚实抛错（``NotImplementedError`` / ``JudgeError``），绝不给假分；
  启用前必须人工抽样校准，且不进门禁。
* **配对比较**（``--compare A B``）：两份预测离线打分后按 id 配对，
  五指标做 percentile bootstrap 95% CI；CI 含 0 → 标注「噪声区间」。

两种后端：

* ``--offline``（**默认**）：不连接任何服务，读取 ``evals/baseline_predictions.jsonl``
  中已录制的系统实际输出，与 golden 对齐打分。
* ``--live`` / ``--record``：调用真实
  ``agentix.application.services.chat_service.ChatService.ask`` 逐条提问，
  并落盘到 ``baseline_predictions.jsonl``（需 LM Studio + Docker + 向量库，本仓库
  的离线开发环境跑不了）。

用法::

    python evals/run_eval.py --help
    python evals/run_eval.py --offline
    python evals/run_eval.py --record      # 在具备完整服务的机器上录制基线
    python evals/run_eval.py --compare a.jsonl b.jsonl   # 配对显著性
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Phase 31：声称一致性检查（Ch7 §7.9）。双路径导入——
# * ``python evals/run_eval.py`` 直跑：sys.path[0] = evals/，顶层名可用；
# * pytest / importlib 以文件路径加载 run_eval：evals/ 不在 sys.path，
#   走包名 ``evals.claim_check``（pythonpath=["."]）。
# 两个模块互不反向 import（claim_check 不 import run_eval），不成环。
try:
    from claim_check import build_report as _claim_build_report
    from claim_check import check_row as _claim_check_row
except ModuleNotFoundError as _cc_exc:  # pragma: no cover
    if _cc_exc.name != "claim_check":
        raise
    from evals.claim_check import build_report as _claim_build_report
    from evals.claim_check import check_row as _claim_check_row

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

EVALS_DIR: Path = Path(__file__).resolve().parent
DEFAULT_GOLDEN: Path = EVALS_DIR / "legal_golden.jsonl"
DEFAULT_PREDICTIONS: Path = EVALS_DIR / "baseline_predictions.jsonl"
DEFAULT_RESULTS_DIR: Path = EVALS_DIR / "results"

# ---------------------------------------------------------------------------
# 路由 -> 出口节点映射
#
# 来自 ``agentix/application/agents/router.py``：
#   * ``route_query``（第 156-159 行）声明 **6 个出口节点**
#   * ``route_type``（第 76/83/112 行 + ``ALLOWED_TYPES`` 第 91-99 行）共 **7 类**
#   * 其中 ``graphrag-query`` 与 ``text2sql-query`` 共用 ``create_research_plan``
# ---------------------------------------------------------------------------

EXIT_NODE_BY_ROUTE: Dict[str, str] = {
    "general-query": "respond_to_general_query",
    "additional-query": "get_additional_info",
    "graphrag-query": "create_research_plan",
    "text2sql-query": "create_research_plan",
    "image-query": "create_image_query",
    "file-query": "create_file_query",
    "kb-query": "create_kb_query",
}

#: ``route_query`` 的 6 个出口节点（评测必须全覆盖）
EXPECTED_EXIT_NODES: frozenset = frozenset(EXIT_NODE_BY_ROUTE.values())

#: 7 类合法 ``route_type``
ALLOWED_ROUTE_TYPES: frozenset = frozenset(EXIT_NODE_BY_ROUTE.keys())

# ---------------------------------------------------------------------------
# 拒答判定口径（确定性）
# ---------------------------------------------------------------------------

#: 显式拒答状态（系统或编排层给出的“已拒答”语义）
REJECT_STATUSES: frozenset = frozenset({"rejected", "refused"})

#: 降级状态：越界问题若降级且**未携带任何引用**，视为正确拒答
DEGRADED_STATUSES: frozenset = frozenset({"degraded"})

#: 文本层面的拒答信号。
#: 注意：**不要**收录“专业律师”之类的词——统一免责声明
#: （``lg_prompts.LEGAL_DISCLAIMER``）每条回答都会带，会带来系统性误判。
REFUSAL_MARKERS: Tuple[str, ...] = (
    "无法回答",
    "不能回答",
    "不予回答",
    "无法提供",
    "无法协助",
    "不能协助",
    "抱歉",
    "超出我的",
    "不在我的",
    "无法处理",
)

# ---------------------------------------------------------------------------
# Privacy-aware（Ch7 §7.17）：PII 离线检测
#
# 纪律：
# * 纯 stdlib 正则、离线执行，命中样本**只记布尔与类型**（id/kinds 进报告，
#   匹配原文与上下文一律不进 summary/CSV/日志——防 PII 随报告外泄）；
# * 命中样本标 privacy_flagged，**applicable 类指标的分母排除之**；
#   全量口径（route_accuracy 等）语义不变，全部报告键只增、不进门禁。
# ---------------------------------------------------------------------------

#: PII 正则族（手机 / 身份证 / 银行卡，均为纯数字定长匹配 + 边界断言防误伤）
PII_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "national_id": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
}


def detect_pii(text: Any) -> List[str]:
    """返回文本命中的 PII 类型列表（只有类型，不含任何原文片段）。"""
    content = str(text or "")
    if not content:
        return []
    return [kind for kind, pattern in PII_PATTERNS.items() if pattern.search(content)]

# ---------------------------------------------------------------------------
# Prompt Sensitivity（Ch7 §7.5）：确定性指令包装微扰
#
# 扰动**只作用于指令包装**（前缀/后缀/语气），不改写问题本体；golden 的
# must_contain / expected_sources 等语义断言不参与扰动，变体间指标可比。
# 缺省关闭：不开 --sensitivity 时无 sensitivity_* 键，输出与现状字节等价。
# ---------------------------------------------------------------------------

#: (变体名, 包装函数)。控制组 original 必须在首位（range 的基准）。
SENSITIVITY_VARIANTS: Tuple[Tuple[str, Any], ...] = (
    ("original", lambda question: question),
    (
        "prefix_formal",
        lambda question: f"请依据中国现行有效法律规范，专业地回答下面的问题：{question}",
    ),
    (
        "suffix_source",
        lambda question: f"{question}（回答时请注明所依据的法条。）",
    ),
    ("polite_opening", lambda question: f"您好，{question}"),
)


def wrap_sensitivity_variant(question: str, name: str) -> str:
    """按变体名包装问题；未知变体名诚实抛 KeyError（不静默退回原句）。"""
    for variant_name, wrapper in SENSITIVITY_VARIANTS:
        if variant_name == name:
            return str(wrapper(str(question)))
    raise KeyError(f"未知 sensitivity 变体：{name}")

# ---------------------------------------------------------------------------
# 引用（source）归一化
# ---------------------------------------------------------------------------

#: 匹配「第577条」「577条」「577 条」等，捕获条号数字
_ARTICLE_RE = re.compile(r"第?\s*(\d+)\s*条")
#: R2-3：条号支持中文数字（「第一百四十八条」），供答案文本与期望引用统一解析
_ARTICLE_ANY_RE = re.compile(r"第?\s*([0-9零一二三四五六七八九十百千]+)\s*条")
#: 兜底：任意数字串（用于 document_id 形式的来源）
_DIGITS_RE = re.compile(r"(\d+)")
#: 连续中文串（用于抽法规名）
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
#: 需要剥离的书名号/空白/常见法令前缀
_LAW_STRIP_RE = re.compile(r"[《》〈〉\s·・\-_/]+")
_LAW_PREFIXES: Tuple[str, ...] = ("中华人民共和国",)

# ---------------------------------------------------------------------------
# R2-3 引用正确性评分（指南 v2.0 §2.3.7②）
#
# citation_hit_rate 只验证「期望法条出现在 sources 里」（检索层命中）；
# 引用正确性进一步要求：答案文本真实引用了该法条（《法名》第X条，支持中文数字）
# 且该引用可归因到 sources（防伪造引用）。两者同时满足才算一条正确引用。
# ---------------------------------------------------------------------------

#: 答案文本中的《法名》第X条引用（中/阿数字均支持）
_ANSWER_CITATION_RE = re.compile(
    r"《(?P<law>[^《》\n]{2,40})》\s*第\s*(?P<article>[0-9零一二三四五六七八九十百千]+)\s*条"
)

#: 中文数字 → 整数（法条条号常见范围：1-9999）
_CN_DIGIT_VALUES = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                    "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT_VALUES = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(text: str) -> Optional[int]:
    """中文数字转整数（支持 十/百/千 组合，如 五百七十七→577、二十→20、一零五→105）。

    解析失败返回 None（fail-open：评分时该条按不命中处理，不抛错）。
    """
    text = str(text).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, section, digit = 0, 0, 0  # section：千/百/十累积的段值
    for ch in text:
        if ch in _CN_DIGIT_VALUES:
            digit = _CN_DIGIT_VALUES[ch]
        elif ch in _CN_UNIT_VALUES:
            unit = _CN_UNIT_VALUES[ch]
            if digit == 0:
                digit = 1  # 「十」=10、「十五」=15（十位缺省一）
            if unit == 10:
                section += digit * 10
            elif unit == 100:
                total += section + digit * 100
                section = 0
            else:  # 千
                total += section + digit * 1000
                section = 0
            digit = 0
        else:
            return None
    result = total + section + digit
    return result if result > 0 else None


def _answer_citations(answer_text: Any) -> List[Tuple[str, str]]:
    """抽取答案文本中的 (法规名, 条号整数文本) 引用序列（去重保序）。

    法规名直接取书名号内文本（``law`` 命名组）再剥离前缀，避免
    :func:`_extract_law` 的 CJK 连串把「第」字并入法规名。
    """
    seen, out = set(), []
    for match in _ANSWER_CITATION_RE.finditer(str(answer_text or "")):
        law = _extract_law(match.group("law"))
        raw_article = match.group("article")
        article_int = _cn_to_int(raw_article)
        if law is None or article_int is None:
            continue
        key = (law, article_int)
        if key not in seen:
            seen.add(key)
            out.append((law, str(article_int)))
    return out


def score_citation_correctness(
    expected_citations: Optional[Sequence[Any]],
    actual_sources: Optional[Sequence[Any]],
    answer_text: Any = "",
) -> float:
    """引用正确性（确定性，指南 §2.3.7②）：期望法条同时满足
    ①可归因到实际 ``sources``（复用 :func:`_citation_matches` 口径）；
    ②答案文本中真实出现《法名》第X条（中文数字归一后比对条号）。

    * ``expected_citations`` 为空 ⇒ 无引用要求，返回 1.0（空真；聚合层用
      ``citation_correctness_expected`` 区分真实分母，模式同 citation_hit_rate）。
    * 条号比对：中文/阿拉伯数字统一转整数（``第148条`` ≡ ``第一百四十八条``）。
    """
    expected = list(expected_citations or [])
    if not expected:
        return 1.0
    actual = list(actual_sources or [])
    text_citations = _answer_citations(answer_text)
    correct = 0
    for exp in expected:
        source_hit = any(_citation_matches(exp, act) for act in actual)
        exp_text = _flatten_citation(exp)
        exp_law = _extract_law(exp_text)
        exp_article_raw = _extract_article(exp_text)
        exp_article_int = _cn_to_int(exp_article_raw) if exp_article_raw else None
        text_hit = any(
            (exp_law is None or exp_law in law or law in exp_law)
            and (
                exp_article_int is None
                or _cn_to_int(article) == exp_article_int
            )
            for law, article in text_citations
        )
        if source_hit and text_hit:
            correct += 1
    return correct / len(expected)


def _flatten_citation(value: Any) -> str:
    """把一条 source（字符串或 dict）展平为可解析的文本。"""
    if isinstance(value, dict):
        keys = ("law", "article", "source", "document_id", "content_snippet", "name")
        parts: List[str] = []
        for key in keys:
            raw = value.get(key)
            if raw:
                parts.append(str(raw))
        if parts:
            return " ".join(parts)
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _extract_article(text: str) -> Optional[str]:
    """抽取条号（阿拉伯数字或中文数字原文，如「第五百七十七条」→「五百七十七」）。"""
    match = _ARTICLE_ANY_RE.search(text)
    if match:
        return match.group(1)
    match = _DIGITS_RE.search(text)
    return match.group(1) if match else None


def _extract_law(text: str) -> Optional[str]:
    """抽取法规名（去除书名号/空白/「中华人民共和国」前缀后的首个连续中文串）。"""
    cleaned = _LAW_STRIP_RE.sub("", text)
    match = _CJK_RUN_RE.search(cleaned)
    if not match:
        return None
    law = match.group(0)
    for prefix in _LAW_PREFIXES:
        if law.startswith(prefix) and len(law) > len(prefix) + 1:
            law = law[len(prefix):]
    return law


def _citation_matches(expected: Any, actual: Any) -> bool:
    """判断单条期望引用是否被某条实际来源命中。

    规则：法规名互相包含（处理「民法典」vs「中华人民共和国民法典」），
    且（若期望含条号）条号必须相等。
    """
    expected_text = _flatten_citation(expected)
    actual_text = _flatten_citation(actual)

    exp_law = _extract_law(expected_text)
    act_law = _extract_law(actual_text)

    if exp_law is None:
        # 期望无法解析出法规名：退化为子串包含判定
        return expected_text in actual_text or actual_text in expected_text
    if act_law is None:
        return False

    if not (exp_law in act_law or act_law in exp_law):
        return False

    exp_article = _extract_article(expected_text)
    if exp_article is None:
        # 只约束法规名的期望：法规名命中即算命中
        return True
    act_article = _extract_article(actual_text)
    # R2-3：条号统一转整数比对（「148」≡「一百四十八」）；任一解析失败退回字符串相等
    exp_int, act_int = _cn_to_int(exp_article), _cn_to_int(act_article or "")
    if exp_int is not None and act_int is not None:
        return exp_int == act_int
    return exp_article == act_article


# ---------------------------------------------------------------------------
# 打分函数（纯函数，零外部依赖，可单测）
# ---------------------------------------------------------------------------


def score_route(expected: Optional[str], actual: Optional[str]) -> bool:
    """路由准确率（确定性）：``route_type`` 归一化后严格相等。"""
    if expected is None or actual is None:
        return False
    return str(expected).strip().lower() == str(actual).strip().lower()


def score_citations(
    expected_sources: Optional[Sequence[Any]],
    actual_sources: Optional[Sequence[Any]],
) -> float:
    """引用命中率（确定性）：期望法条被实际 ``sources`` 命中的比例。

    * ``expected_sources`` 为空 ⇒ 无引用要求，返回 ``1.0``（vacuous truth）。
      闲聊类 / 拒答类 / text2sql 计数类均属此情形。
    * 否则返回 ``命中条数 / 期望条数``，取值 ``[0.0, 1.0]``。
    """
    expected = list(expected_sources or [])
    if not expected:
        return 1.0
    actual = list(actual_sources or [])
    hits = sum(
        1 for exp in expected if any(_citation_matches(exp, act) for act in actual)
    )
    return hits / len(expected)


def score_must_contain(
    expected_keywords: Optional[Sequence[Any]],
    answer_text: Optional[str],
) -> float:
    """关键信息点召回率（确定性）：答案文本包含期望关键词的比例。

    ``golden`` 自带的 ``must_contain`` 字段此前**从未被任何代码消费**——
    断言写在数据里、没人读，等价于没写。本函数补上它的评分口径。

    * ``expected_keywords`` 为空 ⇒ 无关键词要求，返回 ``1.0``（vacuous truth，
      与 :func:`score_citations` 同一模式；聚合层用 ``must_contain_expected_count``
      暴露真实分母，避免免费满分稀释均值）。
    * 只在**答案文本**上匹配，不看 ``sources``：``must_contain`` 约束的是
      「回答有没有说到这个点」，不是「检索有没有捞到这条法条」（后者是
      citation 系列的职责）。例如 L017「民法典一共有多少条规定？」的
      ``must_contain=["民法典"]`` 要求答案文本里出现「民法典」。
    * 大小写不敏感（覆盖 ``PDF`` 这类英文关键词）；中文无大小写，不受影响。
    """
    expected = [str(kw) for kw in (expected_keywords or []) if str(kw).strip()]
    if not expected:
        return 1.0
    haystack = str(answer_text or "").lower()
    hits = sum(1 for kw in expected if kw.lower() in haystack)
    return hits / len(expected)


def _message_signals_refusal(message: Optional[str]) -> bool:
    """文本层面是否出现拒答信号。"""
    if not message:
        return False
    text = str(message)
    return any(marker in text for marker in REFUSAL_MARKERS)


def score_reject(
    expect_reject: bool,
    status: Optional[str],
    message: Optional[str],
    sources: Optional[Sequence[Any]],
) -> bool:
    """拒答正确率（确定性）。

    * ``expect_reject=True``（越界问题）：正确拒答（状态显式拒答 或 文本含拒答信号），
      或「降级且未携带任何引用」⇒ 记为正确。
    * ``expect_reject=False``（非越界）：不得出现拒答信号 ⇒ 未误拒即为正确。
      （单纯的 ``degraded`` 不算误拒，因为降级仍可能给出实质性回答。）
    """
    status_norm = str(status or "").strip().lower()
    refusal_signal = status_norm in REJECT_STATUSES or _message_signals_refusal(message)
    has_sources = bool(list(sources or []))

    if expect_reject:
        if refusal_signal:
            return True
        if status_norm in DEGRADED_STATUSES and not has_sources:
            return True
        return False
    return not refusal_signal


class JudgeError(RuntimeError):
    """LLM-as-a-Judge 诚实失败（输出不可解析等）——绝不返回假分。"""


#: judge 评委系统提示：只允许输出 0–1 数字，杜绝「看起来合理」的长文解释。
JUDGE_SYSTEM_PROMPT = (
    "你是严格的语义相似度评委。任务：评估助手回答与给定问题的语义相关度"
    "（回答是否实质回应了问题、信息是否对位），而不是文风好坏。"
    "只输出一个 0 到 1 之间、最多两位小数的数字：1.0 表示完全对位，"
    "0.0 表示与问题毫无关系。禁止输出任何其他文字。"
)

#: judge 用户提示模板（golden 无标准答案字段，故以问题为参照基准）。
JUDGE_PROMPT_TEMPLATE = """【问题】
{question}

【助手回答】
{answer}

请输出语义相关度（0 到 1 的数字）："""

#: 从评委输出中提取 0–1 分数：0.x / 1.0 / 0 / 1（拒绝 95 这类越界数字）。
_JUDGE_SCORE_RE = re.compile(r"(?<![\d.])(0\.\d{1,4}|1\.0{1,4}|0|1)(?![\d.])")


def _judge_preflight(model: Optional[str] = None) -> str:
    """judge 端点预检 + 模型分档解析（诚实失败，绝不给假分）。

    * ``OPENAI_API_KEY`` / ``OPENAI_API_BASE`` 任一为空 →
      ``NotImplementedError``（main 捕获后退出码 3，中文提示含「校准」）；
    * ``model`` 缺省经 ``ModelGateway.resolve_model("standard")`` 解析
      （分档显式配置优先，空回退 ``LLM_MODEL``）——模型名随
      summary 的 ``judge_model`` 标注输出。

    惰性导入：离线默认路径完全不触碰 agentix 依赖链（同 run_live 先例）。
    """
    from agentix.config import settings as _settings

    api_key = (getattr(_settings, "OPENAI_API_KEY", "") or "").strip()
    api_base = (getattr(_settings, "OPENAI_API_BASE", "") or "").strip()
    if not api_key or not api_base:
        raise NotImplementedError(
            "judge_similarity 未配置端点（OPENAI_API_KEY / OPENAI_API_BASE 为空）——"
            "LLM-as-a-Judge 需人工抽样校准并接通真实端点后才能启用，"
            "诚实失败、绝不给假分。"
        )
    if model:
        return str(model)
    from agentix.infrastructure.llm.model_gateway import get_model_gateway

    return get_model_gateway().resolve_model("standard")


def _parse_judge_score(text: Any) -> float:
    """解析评委输出为 0–1 分数；不可解析 → :class:`JudgeError`（不猜不填）。"""
    matches = _JUDGE_SCORE_RE.findall(str(text or ""))
    if not matches:
        raise JudgeError(
            "judge 输出不可解析为 0–1 分数（拒绝给假分）："
            f"{str(text)[:120]!r}"
        )
    return max(0.0, min(1.0, float(matches[0])))


def judge_similarity(
    question: str, actual: str, *, model: Optional[str] = None
) -> float:
    """LLM-as-a-Judge 语义相关度（**辅助指标，默认不启用**）。

    评估「助手回答 ``actual`` 对「问题 ``question`` 的语义相关度」——
    golden 无标准答案字段，故以问题为参照基准（回答是否实质回应了问题）。

    方案第 7 章要求「评委有系统性偏好」，因此该指标必须人工抽样校准后
    才能作为参考；未配置端点时直接抛出 ``NotImplementedError``，
    输出不可解析时抛 :class:`JudgeError`——**绝不返回未经验证的假分**。

    * ``model`` 缺省经 :func:`_judge_preflight` 分档解析并回写
      summary 的 ``judge_model``；
    * 空回答直接返回 0.0（确定性地板，非伪造）；
    * 同步 CLI 语境执行（``asyncio.run``）——在已有事件循环内调用会
      由 asyncio 报错，不会静默阻塞。

    ⚠️ 校准纪律：未校准前请勿把本分数用作门禁（quality.json 五 metric
    门结构不变，judge 仅进摘要附注）。
    """
    if not str(actual or "").strip():
        return 0.0
    resolved = _judge_preflight(model)

    # 惰性导入：仅 --judge 显式开启时才触碰 agentix 依赖链。
    from agentix.application.services.llm_client import LLMClient

    client = LLMClient(model=resolved, temperature=0.0)
    reply = asyncio.run(
        client.chat(
            system_prompt=JUDGE_SYSTEM_PROMPT,
            user_message=JUDGE_PROMPT_TEMPLATE.format(
                question=str(question or ""), answer=str(actual)
            ),
        )
    )
    return _parse_judge_score(reply)


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """逐行读取 JSONL，返回 dict 列表；空行忽略，非法行报错定位。"""
    rows: List[Dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_no} 不是合法 JSON：{exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_no} 顶层必须是 JSON 对象")
            rows.append(payload)
    return rows


def _pick_route(prediction: Dict[str, Any]) -> Optional[str]:
    """兼容 ``route_type``（录制 schema）与 ``route``（ChatService.ask 返回）两种字段名。"""
    for key in ("route_type", "route"):
        value = prediction.get(key)
        if value is not None:
            return str(value)
    return None


def _pick_multi_agent(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 ``ChatService.ask`` 响应提取 supervisor 指标（Phase 19）。

    扁平拓扑（multi_agent=False）恒返回 ``None``；supervisor 拓扑时从末条
    消息 ``additional_kwargs["multi_agent"]`` 取
    ``{handler, ok, rounds, failure_mode, reason, incremental_ratio}``。
    """
    state = (response.get("metadata") or {}).get("agent_state") or {}
    messages = state.get("messages") or []
    for msg in reversed(messages):
        kwargs = getattr(msg, "additional_kwargs", None)
        if kwargs is None and isinstance(msg, dict):
            kwargs = msg.get("additional_kwargs")
        if isinstance(kwargs, dict) and isinstance(kwargs.get("multi_agent"), dict):
            return kwargs["multi_agent"]
    return None


def _aggregate_multi_agent(prediction_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """supervisor 指标聚合（Phase 19：信息增量 / 六失败模式进离线汇总）。

    扁平拓扑预测行 ``multi_agent=None`` ⇒ 仅 ``multi_agent_meta_count=0``，
    其余键缺席（聚合层按缺键显示 —，不把「没有」读成 0）。
    """
    metas = [
        row["multi_agent"]
        for row in prediction_rows
        if isinstance(row.get("multi_agent"), dict)
    ]
    if not metas:
        return {"multi_agent_meta_count": 0}

    rounds = [
        m["rounds"] for m in metas if isinstance(m.get("rounds"), (int, float))
    ]
    increments = [
        m["incremental_ratio"]
        for m in metas
        if isinstance(m.get("incremental_ratio"), (int, float))
    ]
    failure_modes: Dict[str, int] = {}
    for meta in metas:
        key = str(meta.get("failure_mode") or "unknown")
        failure_modes[key] = failure_modes.get(key, 0) + 1
    ok_count = sum(1 for meta in metas if meta.get("ok"))
    return {
        "multi_agent_meta_count": len(metas),
        "multi_agent_ok_rate": round(ok_count / len(metas), 4),
        "multi_agent_rounds_mean": (
            round(sum(rounds) / len(rounds), 3) if rounds else None
        ),
        "multi_agent_incremental_ratio_mean": (
            round(sum(increments) / len(increments), 4) if increments else None
        ),
        "multi_agent_failure_modes": failure_modes,
    }


#: 统一退出契约的明确终态集合（``domain/runtime/lifecycle.py`` 单源三态；
#: ``status`` 不在集合 = 未知/缺失，绝不计入「终态到达」）。
_EXIT_TERMINAL_STATUSES = frozenset({"completed", "interrupted", "failed"})


def _aggregate_multi_agent_exit(
    prediction_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """退出契约报告键（总纲 §6 · Multi-Agent 四键中的三键）。

    ``exit_reason`` 分布 / 预算耗尽率 / 取消终态到达率——**只增报告键**，
    不进 CI ``METRICS`` 五键门 / ``quality_gate`` / ``CSV_FIELDS``。

    与 :func:`_aggregate_multi_agent` 分离：后者只读 Phase 19 既有键
    （p88「评测面零接触」断言钉住），本函数读 Stage F2c 退出契约键
    ``status`` / ``exit_reason`` / ``truncated``（meta 缺 ``exit_reason``
    ⇒ 返回 ``{}``，不把「没有」读成 0）。

    口径：

    * ``multi_agent_exit_reason_dist``——``exit_reason`` 词表计数
      （lifecycle 单源四值：completed / max_iterations / interrupted / error）；
    * ``multi_agent_budget_exhaust_rate``——``max_iterations`` 占比
      （预算/轮次耗尽；``truncated == (exit_reason == "max_iterations")``
      等式由 p88 钉住，此处不重复推断）；
    * ``multi_agent_cancel_terminal_rate``——**分母 = 发生过取消/打断的运行**
      （``exit_reason == "interrupted"``，peer 粒度取消与断连同源），
      分子 = 其中 ``status`` 落在明确终态集合的占比（到达 ≠ 悬挂/未知）；
      无取消样本 ⇒ ``None`` + ``multi_agent_cancel_terminal_applicable=False``
      （离线 golden 不注入取消，诚实显示 —，运行时取消终态由 p61 断言）。
    """
    metas = [
        row["multi_agent"]
        for row in prediction_rows
        if isinstance(row.get("multi_agent"), dict)
    ]
    with_reason = [
        m for m in metas if isinstance(m.get("exit_reason"), str)
    ]
    if not with_reason:
        return {}
    dist: Dict[str, int] = {}
    for meta in with_reason:
        key = str(meta["exit_reason"])
        dist[key] = dist.get(key, 0) + 1
    cancelled = [
        m for m in with_reason if m["exit_reason"] == "interrupted"
    ]
    terminal = sum(
        1 for m in cancelled if m.get("status") in _EXIT_TERMINAL_STATUSES
    )
    return {
        "multi_agent_exit_reason_dist": dict(sorted(dist.items())),
        "multi_agent_budget_exhaust_rate": round(
            dist.get("max_iterations", 0) / len(with_reason), 4
        ),
        "multi_agent_cancel_terminal_applicable": bool(cancelled),
        "multi_agent_cancel_terminal_rate": (
            round(terminal / len(cancelled), 4) if cancelled else None
        ),
    }


# ---------------------------------------------------------------------------
# 评估核心
# ---------------------------------------------------------------------------


def _task_success(golden: Dict[str, Any], prediction: Optional[Dict[str, Any]]) -> bool:
    """单次运行的「任务成功」判定（确定性口径）。

    成功 = 路由正确 **且** 拒答判定正确 **且**（若有引用要求）引用全中
    **且**（若有关键信息点要求）关键词全中。
    刻意用「全中」而非部分命中：可靠性指标关心的是「这次运行是否完全正确」。

    ``must_contain`` 一项是补上的：golden 里 61 条声明了关键信息点，
    但此前没有任何代码读取该字段，等于断言写了没人看。
    """
    if prediction is None:
        return False
    if not score_route(golden.get("route_type"), _pick_route(prediction)):
        return False
    sources = list(prediction.get("sources") or [])
    if not score_reject(
        bool(golden.get("expect_reject")),
        prediction.get("status"),
        prediction.get("message") or "",
        sources,
    ):
        return False
    expected = list(golden.get("expected_sources") or [])
    if expected and score_citations(expected, sources) < 1.0:
        return False
    keywords = list(golden.get("must_contain") or [])
    if keywords and score_must_contain(keywords, prediction.get("message")) < 1.0:
        return False
    return True


def compute_pass_k(
    golden_rows: Sequence[Dict[str, Any]],
    prediction_rows: Sequence[Dict[str, Any]],
    *,
    k: Optional[int] = None,
) -> Dict[str, Any]:
    """计算 Pass^k 类可靠性指标（τ-bench 口径）。

    * ``pass_k``：**所有 k 次运行都成功**的任务占比（严格可靠性）。
      与 ``pass@k``（至少一次成功）相反——``pass^k`` 衡量「稳定正确」，
      这对法律场景尤为重要（偶发错误不可接受）。
    * ``pass_at_k``：至少一次成功的任务占比（能力上界）。
    * ``mean_task_success``：所有运行的加权成功率（细粒度信号）。

    口径说明：
    * 每条 golden 对应 **同 id 的全部预测行**（``--repeat N`` 或多次录制产生）；
      ``k`` 指定时每任务取前 k 次，缺省取全部。
    * **无任何预测的任务不计入分母**（缺失由 ``missing_prediction_count`` 单独报告），
      避免把「没跑」当成「跑失败」而虚低。
    * **一个任务都没有可评测运行（``tasks == 0``）时，三个指标返回 ``None`` 而非
      ``0.0``**：这不是「全部失败」，是「没有数据」。返回 0.0 会让「预测文件为空」
      在报告里显示成「任务成功率 0%」——把缺失读成彻底失败。``None`` 经
      :mod:`evals.ablations.aggregate` 渲染为「—」，并附 ``pass_k_applicable=False``
      与 ``pass_k_unavailable_reason`` 说明原因。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for prediction in prediction_rows:
        pid = prediction.get("id")
        if pid is not None:
            grouped.setdefault(str(pid), []).append(prediction)

    tasks = 0
    pass_all = 0
    pass_any = 0
    success_total = 0
    run_total = 0
    tasks_with_full_runs = 0
    max_runs = 0

    for golden in golden_rows:
        gid = str(golden.get("id"))
        runs = grouped.get(gid, [])
        if k is not None:
            runs = runs[:k]
        n = len(runs)
        if n == 0:
            continue
        tasks += 1
        max_runs = max(max_runs, n)
        if k is not None and n >= k:
            tasks_with_full_runs += 1
        successes = [_task_success(golden, run) for run in runs]
        success_total += sum(successes)
        run_total += n
        if all(successes):
            pass_all += 1
        if any(successes):
            pass_any += 1

    if tasks == 0:
        return {
            "pass_k": None,
            "pass_at_k": None,
            "mean_task_success": None,
            "pass_k_tasks": 0,
            "pass_k_requested": k,
            "pass_k_max_runs_observed": 0,
            "pass_k_tasks_with_full_runs": 0,
            "pass_k_tasks_without_predictions": len(golden_rows),
            "pass_k_applicable": False,
            "pass_k_unavailable_reason": (
                "no_predictions_for_any_golden_row"
                f"（{len(golden_rows)} 条 golden 全部没有对应预测）"
            ),
        }

    return {
        "pass_k": round(pass_all / tasks, 4),
        "pass_at_k": round(pass_any / tasks, 4),
        "mean_task_success": round(success_total / run_total, 4) if run_total else None,
        "pass_k_tasks": tasks,
        "pass_k_requested": k,
        "pass_k_max_runs_observed": max_runs,
        "pass_k_tasks_with_full_runs": tasks_with_full_runs,
        "pass_k_tasks_without_predictions": len(golden_rows) - tasks,
        "pass_k_applicable": True,
        "pass_k_unavailable_reason": None,
    }


#: Best@k 覆盖的 rubric 类指标（全部为 0–1 分值；布尔判定折成 0.0/1.0）。
BEST_AT_K_COMPONENTS: Tuple[str, ...] = (
    "route_accuracy",
    "citation_hit_rate",
    "citation_correctness",
    "must_contain",
    "reject_accuracy",
)


def _rubric_components(golden: Dict[str, Any], prediction: Dict[str, Any]) -> Dict[str, float]:
    """单次运行的 rubric 类分项得分（0–1；复用既有打分函数，口径同 evaluate）。"""
    sources = list(prediction.get("sources") or [])
    message = prediction.get("message") or ""
    expected = list(golden.get("expected_sources") or [])
    expected_citations = list(golden.get("expected_citations") or [])
    keywords = list(golden.get("must_contain") or [])
    return {
        "route_accuracy": (
            1.0
            if score_route(golden.get("route_type"), _pick_route(prediction))
            else 0.0
        ),
        "citation_hit_rate": float(score_citations(expected, sources)),
        "citation_correctness": float(
            score_citation_correctness(expected_citations, sources, message)
        ),
        "must_contain": float(score_must_contain(keywords, message)),
        "reject_accuracy": (
            1.0
            if score_reject(
                bool(golden.get("expect_reject")),
                prediction.get("status"),
                message,
                sources,
            )
            else 0.0
        ),
    }


def compute_best_at_k(
    golden_rows: Sequence[Dict[str, Any]],
    prediction_rows: Sequence[Dict[str, Any]],
    *,
    k: Optional[int] = None,
) -> Dict[str, Any]:
    """Best@k（Ch7 §7.14 能力上界；只增、不进门禁）。

    口径：
    * 对 rubric 类指标（:data:`BEST_AT_K_COMPONENTS`），**每任务**取 k 次运行的
      分项 max，再对有预测的任务求均值——衡量「运气好那次能到多高」，
      与 ``pass^k``（全部稳定成功）构成能力上界/可靠性下界的对照。
    * ``pass_k`` / ``pass_at_k`` / ``mean_task_success`` 完全不动（compute_pass_k 负责）。
    * **无预测 → 全部 None + applicable=False；每任务至多 1 次运行（repeat=1）
      → 同样 applicable=False 并给出 n/a 原因**（单次采样无 max 区分度）。
    * 部分任务多次采样时：多采样任务按 max 计入，单采样任务按其单次值计入
      （任务集合与 pass_k 一致，不静默丢任务）。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for prediction in prediction_rows:
        pid = prediction.get("id")
        if pid is not None:
            grouped.setdefault(str(pid), []).append(prediction)

    per_component_best: Dict[str, List[float]] = {
        name: [] for name in BEST_AT_K_COMPONENTS
    }
    tasks = 0
    tasks_with_multiple_runs = 0
    max_runs = 0

    for golden in golden_rows:
        runs = grouped.get(str(golden.get("id")), [])
        if k is not None:
            runs = runs[:k]
        if not runs:
            continue
        tasks += 1
        if len(runs) > 1:
            tasks_with_multiple_runs += 1
        max_runs = max(max_runs, len(runs))
        best = {name: 0.0 for name in BEST_AT_K_COMPONENTS}
        for run in runs:
            for name, value in _rubric_components(golden, run).items():
                best[name] = max(best[name], value)
        for name in BEST_AT_K_COMPONENTS:
            per_component_best[name].append(best[name])

    if tasks == 0:
        reason: Optional[str] = "no_predictions_for_any_golden_row"
    elif tasks_with_multiple_runs == 0:
        reason = "n_a_single_run（每任务至多 1 次运行，repeat=1 无 Best@k 区分度）"
    else:
        reason = None
    applicable = reason is None

    return {
        "best_at_k": {
            name: (
                round(sum(values) / len(values), 4) if applicable and values else None
            )
            for name, values in per_component_best.items()
        },
        "best_at_k_applicable": applicable,
        "best_at_k_tasks": tasks,
        "best_at_k_tasks_with_multiple_runs": tasks_with_multiple_runs,
        "best_at_k_requested": k,
        "best_at_k_max_runs_observed": max_runs,
        "best_at_k_reason": reason,
    }


def _difficulty_key(golden: Dict[str, Any]) -> str:
    """golden 的难度分组键：缺省 ``unknown``；非法字符归一下划线。"""
    raw = str(golden.get("difficulty") or "unknown").strip().lower()
    key = re.sub(r"[^a-z0-9_]", "_", raw).strip("_")
    return key or "unknown"


def _aggregate_by_difficulty(
    golden_rows: Sequence[Dict[str, Any]],
    rows: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """难度分层汇总（Phase 31；只增、不进门禁）。

    * 旧 golden 行无 ``difficulty`` 字段 → 缺省 ``unknown`` 组（数据不动）。
    * 每组输出 ``difficulty_<group>_*``：count / route_accuracy（全量口径）与
      三组 applicable 类指标（分母同享 privacy_flagged 排除）。
    * ``rows`` 由 evaluate 按 golden 顺序一一对应构造，故可按下标 zip。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for golden, row in zip(golden_rows, rows):
        groups.setdefault(_difficulty_key(golden), []).append(row)

    out: Dict[str, Any] = {}
    for name in sorted(groups):
        members = groups[name]
        clean = [row for row in members if not row["privacy_flagged"]]
        total = len(members)

        cite_pool = [row for row in clean if row["citation_expected"]]
        mc_pool = [row for row in clean if row["must_contain_expected"]]
        reject_pool = [row for row in clean if row["expect_reject"]]

        out[f"difficulty_{name}_count"] = total
        out[f"difficulty_{name}_route_accuracy"] = (
            round(sum(1 for row in members if row["route_ok"]) / total, 4) if total else 0.0
        )
        out[f"difficulty_{name}_citation_expected_count"] = len(cite_pool)
        out[f"difficulty_{name}_citation_hit_rate_applicable"] = (
            round(sum(row["citation_hit_rate"] for row in cite_pool) / len(cite_pool), 4)
            if cite_pool
            else 0.0
        )
        out[f"difficulty_{name}_must_contain_expected_count"] = len(mc_pool)
        out[f"difficulty_{name}_must_contain_applicable"] = (
            round(sum(row["must_contain_score"] for row in mc_pool) / len(mc_pool), 4)
            if mc_pool
            else 0.0
        )
        out[f"difficulty_{name}_reject_expected_count"] = len(reject_pool)
        out[f"difficulty_{name}_reject_accuracy_applicable"] = (
            round(sum(1 for row in reject_pool if row["reject_ok"]) / len(reject_pool), 4)
            if reject_pool
            else 0.0
        )
    return out


def evaluate(
    golden_rows: Sequence[Dict[str, Any]],
    prediction_rows: Sequence[Dict[str, Any]],
    *,
    use_judge: bool = False,
    k: Optional[int] = None,
) -> Dict[str, Any]:
    """对 golden 与预测逐条打分，返回 ``{"summary": {...}, "rows": [...]}``。

    * 预测缺失（golden 有 id、预测没有）时按「全错」计，``status`` 记为
      ``missing_prediction``，绝不静默跳过。
    * ``use_judge=True`` 时先做端点预检（无端点 → ``NotImplementedError``
      诚实失败，不产半截结果），再对每条调用 :func:`judge_similarity`；
      summary 附 ``judge_model`` / ``judge_mean`` / ``judge_count`` 标注
      （``judge_*`` 前缀只增不改，五 metric 门结构零变化）。
    * ``k`` 控制 Pass^k 可靠性指标的采样次数（见 :func:`compute_pass_k`）；
      缺省使用每条任务的全部预测行。
    """
    judge_model: Optional[str] = None
    if use_judge:
        # 端点预检 fail fast：无端点在打分前诚实失败，不写出半截结果。
        judge_model = _judge_preflight()

    index: Dict[str, Dict[str, Any]] = {}
    for prediction in prediction_rows:
        pid = prediction.get("id")
        if pid is not None:
            index[str(pid)] = prediction

    rows: List[Dict[str, Any]] = []
    for golden in golden_rows:
        gid = str(golden.get("id"))
        prediction = index.get(gid)

        if prediction is None:
            actual_route: Optional[str] = None
            actual_sources: List[Any] = []
            message = ""
            status = "missing_prediction"
        else:
            actual_route = _pick_route(prediction)
            actual_sources = list(prediction.get("sources") or [])
            message = prediction.get("message") or ""
            status = prediction.get("status")

        route_ok = score_route(golden.get("route_type"), actual_route)
        citation_hit_rate = score_citations(
            golden.get("expected_sources"), actual_sources
        )
        # P2-1 补丁：记录该条是否有引用要求（expected_sources 非空）。
        # 空真（vacuous truth）条目单条记 1.0 语义合理，但聚合层必须可见，
        # 否则均值会被免费满分稀释（真实 golden：102 条里 46 条有引用要求、
        # 56 条无非空 expected_sources ⇒ 均值地板 56/102 ≈ 0.549）。
        citation_expected = bool(list(golden.get("expected_sources") or []))
        # R2-3（指南 §2.3.7②）引用正确性：期望法条须同时可归因到 sources
        # 且真实出现在答案文本中（防伪造引用；中文条号归一比对）。
        expected_citations = list(golden.get("expected_citations") or [])
        citation_correctness = score_citation_correctness(
            expected_citations, actual_sources, message
        )
        # 关键信息点召回：golden 的 must_contain 字段（61/102 条非空）此前无消费方。
        expected_keywords = list(golden.get("must_contain") or [])
        must_contain_score = score_must_contain(expected_keywords, message)
        reject_ok = score_reject(
            bool(golden.get("expect_reject")),
            status,
            message,
            actual_sources,
        )

        row: Dict[str, Any] = {
            "id": gid,
            "question": golden.get("question", ""),
            "expected_route": golden.get("route_type"),
            "actual_route": actual_route,
            "route_ok": route_ok,
            "citation_hit_rate": round(citation_hit_rate, 4),
            "citation_expected": citation_expected,
            "citation_correctness": round(citation_correctness, 4),
            "citation_correctness_expected": bool(expected_citations),
            "must_contain_score": round(must_contain_score, 4),
            "must_contain_expected": bool(expected_keywords),
            "expect_reject": bool(golden.get("expect_reject")),
            "reject_ok": reject_ok,
            "status": status,
        }

        if use_judge:
            row["judge_similarity"] = judge_similarity(
                str(golden.get("question", "")), str(message)
            )

        # Phase 31 privacy（Ch7 §7.17）：question+message 过 PII 正则族，
        # 只记布尔与类型——匹配原文/上下文绝不进 rows/summary/CSV。
        pii_kinds = sorted(set(detect_pii(golden.get("question")) + detect_pii(message)))
        row["privacy_flagged"] = bool(pii_kinds)
        row["privacy_kinds"] = pii_kinds
        # Phase 31 声称一致性（Ch7 §7.9）：缺预测诚实记 missing_prediction；
        # 证据池 = 实际 sources ∪ golden 期望引用（人工核定真值并入）。
        row["claim_status"] = (
            "missing_prediction"
            if prediction is None
            else _claim_check_row(
                message,
                actual_sources,
                list(golden.get("expected_sources") or [])
                + list(golden.get("expected_citations") or []),
            )
        )

        rows.append(row)

    total = len(rows)
    route_accuracy = (
        sum(1 for row in rows if row["route_ok"]) / total if total else 0.0
    )
    citation_mean = (
        sum(row["citation_hit_rate"] for row in rows) / total if total else 0.0
    )
    reject_accuracy = (
        sum(1 for row in rows if row["reject_ok"]) / total if total else 0.0
    )

    # P2-1 补丁：把「空真地板」从均值里显式暴露出来。
    # - citation_expected_count：有引用要求（expected_sources 非空）的条数，
    #   是 citation_hit_rate 的**真实分母**；
    # - citation_free_hit_count：无引用要求而自动得 1.0 的条数（空真条目），
    #   它们被计入 citation_hit_rate 均值但不提供任何区分度；
    # - citation_hit_rate_applicable：仅在真实分母上求均值。分母为 0 时取
    #   0.0（绝不能又是 1.0——否则就是新的空真），此时以 count 字段为准。
    # Phase 31 privacy：applicable 类分母排除 privacy_flagged 样本（全量口径
    # 不动）；0 命中时与排除前逐位相同。free_hit 恒按「无引用要求」计数，
    # 带 PII 的有要求条目两个计数都不落——只在 privacy_* 键里可见。
    citation_expected_rows = [
        row for row in rows if row["citation_expected"] and not row["privacy_flagged"]
    ]
    citation_expected_count = len(citation_expected_rows)
    citation_free_hit_count = sum(1 for row in rows if not row["citation_expected"])
    citation_hit_rate_applicable = (
        sum(row["citation_hit_rate"] for row in citation_expected_rows)
        / citation_expected_count
        if citation_expected_count
        else 0.0
    )

    # R2-3：引用正确性真实分母（expected_citations 非空的条目），模式同上
    cc_expected_rows = [
        row
        for row in rows
        if row["citation_correctness_expected"] and not row["privacy_flagged"]
    ]
    cc_expected_count = len(cc_expected_rows)
    citation_correctness_applicable = (
        sum(row["citation_correctness"] for row in cc_expected_rows) / cc_expected_count
        if cc_expected_count
        else 0.0
    )

    # 关键信息点真实分母（must_contain 非空的条目）。与 citation 系列同一模式：
    # 全量均值会被空真条目的免费 1.0 抬高，因此额外给出「只在有要求的条目上」的口径。
    mc_expected_rows = [
        row for row in rows if row["must_contain_expected"] and not row["privacy_flagged"]
    ]
    mc_expected_count = len(mc_expected_rows)
    mc_free_hit_count = sum(1 for row in rows if not row["must_contain_expected"])
    must_contain_applicable = (
        sum(row["must_contain_score"] for row in mc_expected_rows) / mc_expected_count
        if mc_expected_count
        else 0.0
    )

    # 拒答正确率的适用口径。reject_accuracy 以 total 为分母，而 golden 里
    # expect_reject=True 的只占少数（13/102）：其余条目只要「不误拒」就算对，
    # 于是拒答指标被大量非越界条目抬到一个很高的地板（≈ 1 - 13/102 = 0.8725），
    # 对「越界问题有没有被正确拒掉」几乎没有区分度。这里给出两个互补的真实口径：
    #   * reject_accuracy_applicable   —— 分母 = expect_reject=True，衡量「该拒的拒了没」
    #   * reject_false_refusal_rate    —— 分母 = expect_reject=False，衡量「不该拒的误拒了没」
    # 均为 0 分母时取 0.0 + count 字段兜底（同 citation 系列，绝不制造新的空真）。
    # Phase 31：两个池同样排除 privacy_flagged（分母与 count/summary 键同源，保持自洽）。
    reject_expected_rows = [
        row for row in rows if row["expect_reject"] and not row["privacy_flagged"]
    ]
    reject_expected_count_rows = len(reject_expected_rows)
    reject_accuracy_applicable = (
        sum(1 for row in reject_expected_rows if row["reject_ok"])
        / reject_expected_count_rows
        if reject_expected_count_rows
        else 0.0
    )
    reject_non_reject_rows = [
        row for row in rows if not row["expect_reject"] and not row["privacy_flagged"]
    ]
    reject_false_refusal_rate = (
        sum(1 for row in reject_non_reject_rows if not row["reject_ok"])
        / len(reject_non_reject_rows)
        if reject_non_reject_rows
        else 0.0
    )

    golden_distribution: Dict[str, int] = {}
    for golden in golden_rows:
        key = str(golden.get("route_type"))
        golden_distribution[key] = golden_distribution.get(key, 0) + 1

    judge_values: List[float] = [
        float(row["judge_similarity"])
        for row in rows
        if row.get("judge_similarity") is not None
    ]

    summary: Dict[str, Any] = {
        "total": total,
        "route_accuracy": round(route_accuracy, 4),
        "citation_hit_rate": round(citation_mean, 4),
        "reject_accuracy": round(reject_accuracy, 4),
        "route_ok_count": sum(1 for row in rows if row["route_ok"]),
        "reject_ok_count": sum(1 for row in rows if row["reject_ok"]),
        # 越界样本计数 = applicable 池同源（Phase 31 privacy 排除后；rows 与
        # golden 1:1，0 命中时与旧「按 golden 计数」逐位相同）。
        "reject_expected_count": reject_expected_count_rows,
        # 拒答的适用口径（只增不改：reject_accuracy 语义不变）
        "reject_accuracy_applicable": round(reject_accuracy_applicable, 4),
        "reject_false_refusal_rate": round(reject_false_refusal_rate, 4),
        # P2-1 补丁：空真可见性字段（只增不改，旧键语义不变）
        "citation_expected_count": citation_expected_count,
        "citation_free_hit_count": citation_free_hit_count,
        "citation_hit_rate_applicable": round(citation_hit_rate_applicable, 4),
        # R2-3：引用正确性聚合（expected_citations 为真实分母）
        "citation_correctness": round(
            sum(row["citation_correctness"] for row in rows) / total if total else 0.0, 4
        ),
        "citation_correctness_expected_count": cc_expected_count,
        "citation_correctness_applicable": round(citation_correctness_applicable, 4),
        # 关键信息点（must_contain）聚合：真实分母口径，模式同 citation 系列
        "must_contain": round(
            sum(row["must_contain_score"] for row in rows) / total if total else 0.0, 4
        ),
        "must_contain_expected_count": mc_expected_count,
        "must_contain_free_hit_count": mc_free_hit_count,
        "must_contain_applicable": round(must_contain_applicable, 4),
        "reject_non_reject_count": len(reject_non_reject_rows),
        "missing_prediction_count": sum(
            1 for row in rows if row["status"] == "missing_prediction"
        ),
        "golden_route_distribution": golden_distribution,
        # Phase 26：judge 辅助指标标注（judge_* 前缀只增不改——五 metric
        # 门结构零变化，judge 不进门禁）。
        "judge_enabled": bool(use_judge),
        "judge_model": judge_model,
        "judge_count": len(judge_values),
        "judge_mean": (
            round(sum(judge_values) / len(judge_values), 4)
            if judge_values
            else None
        ),
    }
    # Pass^k 可靠性指标（只增不改：旧键语义不变）。
    # 单次运行时 pass_k == mean_task_success，属退化情形，仍如实输出。
    summary.update(compute_pass_k(golden_rows, prediction_rows, k=k))
    # Phase 31 Best@k（Ch7 §7.14 能力上界）：repeat=1 时 applicable=False 标注 n/a。
    summary.update(compute_best_at_k(golden_rows, prediction_rows, k=k))
    # Phase 31 难度分层（Ch7 分层评估）：旧 golden 无 difficulty 字段 →
    # 缺省 unknown 归组；键名 difficulty_*，全部只增、不进门禁。
    summary.update(_aggregate_by_difficulty(golden_rows, rows))
    # Phase 31 privacy 汇总：只记布尔/类型/id，PII 原文零留存（Ch7 §7.17）。
    flagged_rows = [row for row in rows if row["privacy_flagged"]]
    kind_counts: Dict[str, int] = {}
    for row in flagged_rows:
        for kind in row["privacy_kinds"]:
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
    summary["privacy_flagged_count"] = len(flagged_rows)
    summary["privacy_flagged_ids"] = [row["id"] for row in flagged_rows]
    summary["privacy_flagged_kinds"] = dict(sorted(kind_counts.items()))
    summary["privacy_applicable_excluded"] = sum(
        1
        for row in flagged_rows
        if row["citation_expected"]
        or row["citation_correctness_expected"]
        or row["must_contain_expected"]
        or row["expect_reject"]
    )
    summary["privacy_note"] = "PII 纯离线正则检测，只记布尔/类型不进报告原文"
    # Phase 31 声称一致性（Ch7 §7.9）：只标记不判分；无 trajectory 诚实 skip。
    summary["claim_consistency"] = _claim_build_report(
        rows, has_predictions=bool(prediction_rows)
    )
    # Phase 19：supervisor 指标（信息增量 / 六失败模式）——扁平拓扑仅
    # multi_agent_meta_count=0，其余键缺席（只增不改）。
    summary.update(_aggregate_multi_agent(prediction_rows))
    # Stage T（总纲 §6）：退出契约报告键（exit_reason 分布 / 预算耗尽率 /
    # 取消终态到达率）——meta 无 exit_reason ⇒ 本组键缺席，与上一行正交。
    summary.update(_aggregate_multi_agent_exit(prediction_rows))
    return {"summary": summary, "rows": rows}


# ---------------------------------------------------------------------------
# Prompt Sensitivity（Ch7 §7.5）：变体间指标极差
# ---------------------------------------------------------------------------

#: sensitivity 报告的指标键（applicable 类口径；全量口径不参与极差）
SENSITIVITY_METRICS: Tuple[str, ...] = (
    "route_accuracy",
    "citation_hit_rate_applicable",
    "citation_correctness_applicable",
    "must_contain_applicable",
    "reject_accuracy_applicable",
)


def compute_sensitivity_ranges(
    golden_rows: Sequence[Dict[str, Any]],
    prediction_rows: Sequence[Dict[str, Any]],
    *,
    k: Optional[int] = None,
) -> Dict[str, Any]:
    """指令包装变体间的指标极差（Phase 31；只增、不进门禁）。

    * 预测行带 ``sensitivity_variant`` 时按变体分桶、各自 :func:`evaluate`
      后对 :data:`SENSITIVITY_METRICS` 取 ``max - min``（round 4）。
    * 变体数 < 2 → 返回空 dict：不开 ``--sensitivity`` / 无变体数据时
      **不产生任何 ``sensitivity_*`` 键，输出与现状字节等价**。
    """
    variants: List[str] = []
    for row in prediction_rows:
        name = row.get("sensitivity_variant")
        if name and str(name) not in variants:
            variants.append(str(name))
    if len(variants) < 2:
        return {}

    per_variant: Dict[str, Dict[str, Any]] = {}
    for name in variants:
        subset = [
            row for row in prediction_rows if str(row.get("sensitivity_variant")) == name
        ]
        per_variant[name] = evaluate(golden_rows, subset, k=k)["summary"]

    out: Dict[str, Any] = {
        "sensitivity_enabled": True,
        "sensitivity_variants": variants,
        "sensitivity_variant_count": len(variants),
    }
    for metric in SENSITIVITY_METRICS:
        values = [float(summary.get(metric) or 0.0) for summary in per_variant.values()]
        out[f"sensitivity_range_{metric}"] = round(max(values) - min(values), 4)
    return out


# ---------------------------------------------------------------------------
# 结果落盘
# ---------------------------------------------------------------------------

CSV_FIELDS: Tuple[str, ...] = (
    "id",
    "question",
    "expected_route",
    "actual_route",
    "route_ok",
    "citation_hit_rate",
    "citation_expected",
    "citation_correctness",
    "citation_correctness_expected",
    "must_contain_score",
    "must_contain_expected",
    "expect_reject",
    "reject_ok",
    "status",
)
"""逐条 CSV 列。

新增列是为了让「复核」这一动作真的能做：审阅者拿到 CSV 就能自己重算
summary 里的每个均值，而不必反推公式。REVIEW 请求（raw CSV +
per-sample predictions）正是依赖这份逐条明细。"""


def write_results(
    result: Dict[str, Any],
    results_dir: Path,
    timestamp: str,
) -> Tuple[Path, Path]:
    """写出 ``<timestamp>.csv``（逐条）与 ``<timestamp>.summary.json``（汇总）。"""
    out_dir = Path(results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / f"{timestamp}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in result["rows"]:
            writer.writerow({field: row.get(field) for field in CSV_FIELDS})

    summary_path = out_dir / f"{timestamp}.summary.json"
    payload = {"timestamp": timestamp, "summary": result["summary"]}
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    return csv_path, summary_path


def _fmt_ratio(value: Any) -> str:
    """格式化比例；``None``（无数据）与 ``0.0``（全失败）必须可区分。"""
    if value is None:
        return "—（无数据）"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def format_summary_line(summary: Dict[str, Any]) -> str:
    """终端汇总行（中文）。"""
    line = (
        "评估汇总：共 {total} 条 | 路由准确率 {route} | 引用命中率 {cite} | "
        "关键信息点 {must} | 拒答正确率 {reject}（越界样本 {reject_expected} 条，缺失预测 {missing} 条；"
        "引用要求样本 {cite_expected} 条，另有 {cite_free} 条无引用要求自动计满分）"
    ).format(
        total=summary.get("total", 0),
        route=summary.get("route_accuracy", 0.0),
        cite=summary.get("citation_hit_rate", 0.0),
        must=summary.get("must_contain", 0.0),
        reject=summary.get("reject_accuracy", 0.0),
        reject_expected=summary.get("reject_expected_count", 0),
        missing=summary.get("missing_prediction_count", 0),
        cite_expected=summary.get("citation_expected_count", 0),
        cite_free=summary.get("citation_free_hit_count", 0),
    )
    # 适用口径行：把被空真抬高的指标换算回真实分母，避免「看起来很好」。
    line += (
        "\n适用口径：引用命中率 {cite_app}（仅 {cite_expected} 条有引用要求）| "
        "关键信息点 {must_app}（仅 {must_expected} 条有关键词要求）| "
        "拒答正确率 {reject_app}（仅 {reject_expected} 条越界样本）| "
        "误拒率 {false_refusal}（{non_reject} 条非越界样本中被误拒的比例，越低越好）"
    ).format(
        cite_app=_fmt_ratio(summary.get("citation_hit_rate_applicable")),
        cite_expected=summary.get("citation_expected_count", 0),
        must_app=_fmt_ratio(summary.get("must_contain_applicable")),
        must_expected=summary.get("must_contain_expected_count", 0),
        reject_app=_fmt_ratio(summary.get("reject_accuracy_applicable")),
        reject_expected=summary.get("reject_expected_count", 0),
        false_refusal=_fmt_ratio(summary.get("reject_false_refusal_rate")),
        non_reject=summary.get("reject_non_reject_count", 0),
    )
    # Pass^k 可靠性（仅在有多次采样时补充，避免单次运行时的噪声行）
    max_runs = summary.get("pass_k_max_runs_observed", 0) or 0
    if max_runs > 1:
        line += (
            "\n可靠性指标：Pass^{k} {pass_k}（全部 {max_runs} 次均成功的任务占比）| "
            "pass@k {pass_at_k}（至少一次成功）| 平均单次成功率 {mean}"
        ).format(
            k=max_runs,
            max_runs=max_runs,
            pass_k=_fmt_ratio(summary.get("pass_k")),
            pass_at_k=_fmt_ratio(summary.get("pass_at_k")),
            mean=_fmt_ratio(summary.get("mean_task_success")),
        )
    elif not summary.get("pass_k_applicable", True):
        # 无数据时说清是「没跑」而不是「全错」
        line += "\n可靠性指标：—（{}）".format(
            summary.get("pass_k_unavailable_reason") or "无可评测预测"
        )
    # Phase 26：judge 辅助指标（仅 --judge 显式开启时出现；只作参考不进门禁）
    if summary.get("judge_enabled"):
        line += (
            "\n辅助指标：judge 语义相关度 {mean}（model={model}，{count} 条；"
            "仅参考、不进门禁——启用前需人工抽样校准）"
        ).format(
            mean=_fmt_ratio(summary.get("judge_mean")),
            model=summary.get("judge_model") or "—",
            count=summary.get("judge_count", 0),
        )
    # Phase 31：privacy / claim / sensitivity —— 仅在有命中/开启时追加行，
    # 零命中（= 常态）时输出与旧版字节等价。
    flagged_count = int(summary.get("privacy_flagged_count") or 0)
    if flagged_count:
        flagged_ids = [str(x) for x in summary.get("privacy_flagged_ids") or []]
        shown = ", ".join(flagged_ids[:5]) + (" 等" if len(flagged_ids) > 5 else "")
        line += "\n隐私口径：PII 命中 {n} 条（{ids}；已从适用口径分母排除，仅记类型不记原文）".format(
            n=flagged_count, ids=shown
        )
    claim_report = summary.get("claim_consistency") or {}
    if claim_report.get("applicable") and claim_report.get("mismatch_count"):
        mismatch_ids = [str(x) for x in claim_report.get("mismatch_ids") or []]
        shown = ", ".join(mismatch_ids[:5]) + (" 等" if len(mismatch_ids) > 5 else "")
        line += "\n声称一致性：不符 {n} 条（{ids}；只标记不判分，须人工复核）".format(
            n=claim_report.get("mismatch_count"), ids=shown
        )
    if summary.get("sensitivity_enabled"):
        range_parts = [
            f"{metric} 极差 {summary.get('sensitivity_range_' + metric)}"
            for metric in SENSITIVITY_METRICS
        ]
        line += "\n提示敏感性（{count} 变体）：{parts}".format(
            count=summary.get("sensitivity_variant_count"),
            parts=" | ".join(range_parts),
        )
    return line


# ---------------------------------------------------------------------------
# 配对比较（--compare）：percentile bootstrap 显著性（Ch7 §7.14）
# ---------------------------------------------------------------------------

#: 可配对比较的逐条指标（键，中文标签）。与 summary 五 metric 同源，
#: 但比较在**逐条配对**上做——差 1–2pp 可能只是波动（§7.14）。
COMPARE_METRICS: Tuple[Tuple[str, str], ...] = (
    ("route_ok", "路由准确率"),
    ("citation_hit_rate", "引用命中率"),
    ("citation_correctness", "引用正确性"),
    ("must_contain_score", "关键信息点"),
    ("reject_ok", "拒答正确率"),
)

#: bootstrap 默认参数：固定 seed 保证可复现；95% CI（alpha=0.05）。
COMPARE_N_RESAMPLES = 2000
COMPARE_SEED = 1729


def _bootstrap_percentile_ci(
    diffs: Sequence[float],
    *,
    n_resamples: int = COMPARE_N_RESAMPLES,
    seed: int = COMPARE_SEED,
    alpha: float = 0.05,
) -> Tuple[Optional[float], Optional[float]]:
    """对均值差做 percentile bootstrap → (low, high)；样本不足返回 (None, None)。

    纯 stdlib（``random.Random`` 固定种子）——不引入 numpy，保证离线可复现。
    """
    values = list(diffs)
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(rng.choices(values, k=n)) / n for _ in range(n_resamples)
    )
    lo_idx = max(0, int((alpha / 2) * n_resamples))
    hi_idx = min(n_resamples - 1, int((1 - alpha / 2) * n_resamples) - 1)
    return means[lo_idx], means[hi_idx]


def compare_rows(
    rows_a: Sequence[Dict[str, Any]],
    rows_b: Sequence[Dict[str, Any]],
    *,
    n_resamples: int = COMPARE_N_RESAMPLES,
    seed: int = COMPARE_SEED,
) -> Dict[str, Any]:
    """按 id 配对两组 evaluate 结果行 → 各指标均值差 + 95% bootstrap CI。

    * 配对集 = 两边 id 交集（按 id 排序，保证与输入顺序无关）；
      各边独有条目数如实报告（不静默丢弃）。
    * ``Δ = A − B``（正数 = A 更好）。
    * ``significant`` = 95% CI 不含 0；否则标注「噪声区间」。
    * 同 id 多行（Pass^k 重复采样）取**首次出现**行参与配对。
    """
    def _index(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            rid = str(row.get("id"))
            out.setdefault(rid, row)
        return out

    idx_a = _index(rows_a)
    idx_b = _index(rows_b)
    ids = sorted(set(idx_a) & set(idx_b))

    metrics: Dict[str, Dict[str, Any]] = {}
    for key, label in COMPARE_METRICS:
        a_vals = [float(idx_a[i].get(key, 0.0)) for i in ids]
        b_vals = [float(idx_b[i].get(key, 0.0)) for i in ids]
        diffs = [a - b for a, b in zip(a_vals, b_vals)]
        low, high = _bootstrap_percentile_ci(
            diffs, n_resamples=n_resamples, seed=seed
        )
        metrics[key] = {
            "label": label,
            "a_mean": round(sum(a_vals) / len(a_vals), 4) if ids else None,
            "b_mean": round(sum(b_vals) / len(b_vals), 4) if ids else None,
            "delta": round(sum(diffs) / len(diffs), 4) if ids else None,
            "ci_low": round(low, 4) if low is not None else None,
            "ci_high": round(high, 4) if high is not None else None,
            "significant": (
                bool(low is not None and (low > 0 or high < 0))
                if low is not None
                else None
            ),
        }

    return {
        "paired_count": len(ids),
        "a_only_count": len(set(idx_a) - set(idx_b)),
        "b_only_count": len(set(idx_b) - set(idx_a)),
        "n_resamples": n_resamples,
        "seed": seed,
        "metrics": metrics,
    }


def format_compare_report(
    report: Dict[str, Any], *, label_a: str = "A", label_b: str = "B"
) -> str:
    """把配对比较结果渲染成中文终端报告（显著性标注可读）。"""
    lines = [
        "配对比较（{a} vs {b}）：配对样本 {n} 条"
        "（{a} 独有 {ao} / {b} 独有 {bo}）；Δ = {a} − {b}，"
        "percentile bootstrap {resamples} 次（seed={seed}，95% CI）".format(
            a=label_a,
            b=label_b,
            n=report["paired_count"],
            ao=report["a_only_count"],
            bo=report["b_only_count"],
            resamples=report["n_resamples"],
            seed=report["seed"],
        )
    ]
    for key, _label in COMPARE_METRICS:
        m = report["metrics"][key]
        if m["delta"] is None or m["ci_low"] is None:
            verdict = "样本不足，不可判定"
        elif m["significant"]:
            verdict = "显著"
        else:
            verdict = "不显著（噪声区间）"
        lines.append(
            "  {label}: {a}={av} {b}={bv} Δ={delta} 95% CI=[{low}, {high}] → {verdict}".format(
                label=m["label"],
                a=label_a,
                av=_fmt_ratio(m["a_mean"]),
                b=label_b,
                bv=_fmt_ratio(m["b_mean"]),
                delta="—" if m["delta"] is None else f"{m['delta']:+.4f}",
                low="—" if m["ci_low"] is None else f"{m['ci_low']:+.4f}",
                high="—" if m["ci_high"] is None else f"{m['ci_high']:+.4f}",
                verdict=verdict,
            )
        )
    return "\n".join(lines)


def run_compare(
    predictions_a: Path,
    predictions_b: Path,
    *,
    golden_path: Path = DEFAULT_GOLDEN,
    k: Optional[int] = None,
) -> int:
    """``--compare A B``：两份预测各自离线打分 → 逐条配对 → 显著性报告。

    纯离线（不连任何外部服务、不写结果文件）；返回进程退出码。
    """
    golden_path = Path(golden_path)
    if not golden_path.exists():
        print(f"[配对比较] 未找到评估集文件：{golden_path}", file=sys.stderr)
        return 2
    for path in (predictions_a, predictions_b):
        if not Path(path).exists():
            print(f"[配对比较] 未找到预测文件：{path}", file=sys.stderr)
            return 2

    golden_rows = load_jsonl(golden_path)
    rows_a = evaluate(golden_rows, load_jsonl(Path(predictions_a)), k=k)["rows"]
    rows_b = evaluate(golden_rows, load_jsonl(Path(predictions_b)), k=k)["rows"]

    report = compare_rows(rows_a, rows_b)
    if report["paired_count"] == 0:
        print(
            "[配对比较] 两份预测无任何相同 id，无法配对——不给不可判定的结论。",
            file=sys.stderr,
        )
        return 2

    print(
        format_compare_report(
            report,
            label_a=Path(predictions_a).name,
            label_b=Path(predictions_b).name,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# 后端：offline / live
# ---------------------------------------------------------------------------

MISSING_BASELINE_HINT = (
    "[离线模式] 未找到基线预测文件：{path}\n"
    "请先在具备 LM Studio + Docker（Redis/Neo4j/向量库）的机器上运行：\n"
    "    python evals/run_eval.py --record\n"
    "生成基线后再回到本环境执行 `python evals/run_eval.py --offline`。"
)


def run_offline(
    golden_path: Path = DEFAULT_GOLDEN,
    predictions_path: Path = DEFAULT_PREDICTIONS,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    *,
    use_judge: bool = False,
    timestamp: Optional[str] = None,
    k: Optional[int] = None,
) -> int:
    """离线跑分：读取已录制预测 → 打分 → 落盘。返回进程退出码。"""
    golden_path = Path(golden_path)
    predictions_path = Path(predictions_path)

    if not golden_path.exists():
        print(f"[离线模式] 未找到评估集文件：{golden_path}", file=sys.stderr)
        return 2
    if not predictions_path.exists():
        print(
            MISSING_BASELINE_HINT.format(path=predictions_path),
            file=sys.stderr,
        )
        return 2

    golden_rows = load_jsonl(golden_path)
    prediction_rows = load_jsonl(predictions_path)

    result = evaluate(golden_rows, prediction_rows, use_judge=use_judge, k=k)
    # Phase 31：录制数据带 sensitivity_variant 时自动出极差键；
    # 无变体（常态）→ 空 dict，输出与现状字节等价。
    result["summary"].update(
        compute_sensitivity_ranges(golden_rows, prediction_rows, k=k)
    )
    stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path, summary_path = write_results(result, results_dir, stamp)

    print(format_summary_line(result["summary"]))
    print(f"逐条明细：{csv_path}")
    print(f"汇总 JSON：{summary_path}")
    return 0


def run_live(
    golden_path: Path = DEFAULT_GOLDEN,
    predictions_path: Path = DEFAULT_PREDICTIONS,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    *,
    use_judge: bool = False,
    timestamp: Optional[str] = None,
    repeat: int = 1,
    k: Optional[int] = None,
    sensitivity: bool = False,
) -> int:
    """在线跑分：调用真实 ``ChatService.ask`` 逐条提问 → 落盘 → 打分。

    ⚠️ 需要 LM Studio + Docker + 向量库全链路可用；本地离线开发环境
    无法运行（本函数代码就绪但不在离线环境执行）。

    ``repeat > 1`` 时每条问题重复提问 N 次（产生 N 行同 id 预测），
    用于计算 Pass^k 可靠性指标。**注意**：温度为 0 的确定性模型下多次运行
    结果相同，Pass^k 与单次成功等价；只有温度 > 0 时该指标才有区分度。

    ``sensitivity=True``（``--sensitivity``）：对**指令包装**做确定性微扰
    （:data:`SENSITIVITY_VARIANTS`，问题本体与 golden 断言不动），按
    每题 × 每变体 × repeat 录制；预测行带 ``sensitivity_variant``，汇总
    自动附 ``sensitivity_range_*`` 极差键（只增、不进门禁）。关闭时录制
    行的字段集合与旧版逐字节一致。
    """
    golden_path = Path(golden_path)
    predictions_path = Path(predictions_path)
    repeat = max(1, int(repeat))

    if not golden_path.exists():
        print(f"[在线模式] 未找到评估集文件：{golden_path}", file=sys.stderr)
        return 2

    # 惰性导入：离线模式下完全不触碰 agentix 依赖链
    from agentix.application.services.chat_service import get_chat_service

    golden_rows = load_jsonl(golden_path)
    stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    service = get_chat_service()

    async def _ask_all() -> List[Dict[str, Any]]:
        recorded: List[Dict[str, Any]] = []
        for golden in golden_rows:
            gid = str(golden.get("id"))
            question = str(golden.get("question", ""))
            # 指令包装变体只包指令、不改问题本体；关闭时单条 (None, 原句)
            # ——出站 message、session_id 生成与旧版逐字节一致。
            plan: List[Tuple[Optional[str], str]] = (
                [
                    (name, wrap_sensitivity_variant(question, name))
                    for name, _wrapper in SENSITIVITY_VARIANTS
                ]
                if sensitivity
                else [(None, question)]
            )
            for variant_name, outgoing in plan:
                variant_seg = f"{variant_name}-" if variant_name is not None else ""
                for attempt in range(repeat):
                    session_id = f"eval-{gid}-{stamp}-{variant_seg}{attempt}"
                    try:
                        response = await service.ask(
                            message=outgoing,
                            session_id=session_id,
                        )
                        row: Dict[str, Any] = {
                            "id": gid,
                            "attempt": attempt,
                            "route_type": response.get("route"),
                            "sources": response.get("sources") or [],
                            "message": response.get("message") or "",
                            "status": response.get("status") or "ok",
                            # Phase 19：supervisor 指标（扁平拓扑恒 None）
                            "multi_agent": _pick_multi_agent(response),
                        }
                    except Exception as exc:  # noqa: BLE001 —— 单条失败不阻断整体录制
                        row = {
                            "id": gid,
                            "attempt": attempt,
                            "route_type": None,
                            "sources": [],
                            "message": f"[live 调用失败] {exc}",
                            "status": "error",
                            "multi_agent": None,
                        }
                    if variant_name is not None:
                        row["sensitivity_variant"] = variant_name
                    recorded.append(row)
        return recorded

    recorded_rows = asyncio.run(_ask_all())

    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    with predictions_path.open("w", encoding="utf-8") as handle:
        for row in recorded_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    variant_part = f" × {len(SENSITIVITY_VARIANTS)} 指令变体" if sensitivity else ""
    print(
        f"[在线模式] 已录制 {len(recorded_rows)} 条预测"
        f"（{len(golden_rows)} 题 × {repeat} 次{variant_part}）→ {predictions_path}"
    )

    result = evaluate(golden_rows, recorded_rows, use_judge=use_judge, k=k or repeat)
    # Phase 31：带变体数据出 sensitivity_range_*（无变体 → 空 dict，字节等价）。
    result["summary"].update(
        compute_sensitivity_ranges(golden_rows, recorded_rows, k=k)
    )
    csv_path, summary_path = write_results(result, results_dir, stamp)
    print(format_summary_line(result["summary"]))
    print(f"逐条明细：{csv_path}")
    print(f"汇总 JSON：{summary_path}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造中文帮助信息的 CLI 解析器。"""
    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description=(
            "P2-1 法律领域评估集批量跑分与回归。\n"
            "默认离线（--offline）：读取 evals/baseline_predictions.jsonl 打分，"
            "不连接任何外部服务。\n"
            "确定性指标：路由准确率 / 引用命中率 / 拒答正确率；"
            "LLM-as-a-Judge 语义相似度默认关闭且需人工校准。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    mode = parser.add_argument_group("运行模式")
    mode.add_argument(
        "--offline",
        action="store_true",
        help="离线模式（默认）：用已录制的 baseline_predictions.jsonl 打分",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="在线模式：调用真实 ChatService.ask 逐条提问并落盘（需 LM Studio + Docker）",
    )
    mode.add_argument(
        "--record",
        action="store_true",
        help="录制基线：等价于 --live，用于生成 baseline_predictions.jsonl",
    )

    paths = parser.add_argument_group("路径")
    paths.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN),
        help=f"评估集路径（默认：{DEFAULT_GOLDEN}）",
    )
    paths.add_argument(
        "--predictions",
        default=str(DEFAULT_PREDICTIONS),
        help=f"预测/基线文件路径（默认：{DEFAULT_PREDICTIONS}）",
    )
    paths.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help=f"结果输出目录（默认：{DEFAULT_RESULTS_DIR}）",
    )

    judge = parser.add_argument_group("辅助指标（LLM-as-a-Judge，默认关闭）")
    judge.add_argument(
        "--judge",
        dest="judge",
        action="store_true",
        help=(
            "启用语义相关度打分（辅助指标，仅进摘要附注不进门禁）；\n"
            "需人工抽样校准 + 已配置端点（OPENAI_API_KEY/OPENAI_API_BASE），\n"
            "无端点诚实失败（退出码 3），绝不给假分"
        ),
    )
    judge.add_argument(
        "--no-judge",
        dest="judge",
        action="store_false",
        help="关闭语义相关度打分（默认）",
    )
    parser.set_defaults(judge=False)

    compare = parser.add_argument_group("配对比较（percentile bootstrap 显著性）")
    compare.add_argument(
        "--compare",
        nargs=2,
        metavar=("A", "B"),
        default=None,
        help=(
            "配对比较两份预测文件：各自离线打分后按 id 配对，\n"
            "对五指标做 percentile bootstrap 95%% CI；CI 含 0 → 标注\n"
            "「不显著（噪声区间）」（Ch7 §7.14：差 1–2pp 可能是波动）"
        ),
    )

    reliability = parser.add_argument_group("可靠性指标（Pass^k）")
    reliability.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help=(
            "在线模式下每道题重复提问 N 次（默认 1），用于计算 Pass^k。\n"
            "注意：温度 0 的确定性模型下多次运行结果相同，该指标无区分度。"
        ),
    )
    reliability.add_argument(
        "--k",
        type=int,
        default=None,
        metavar="K",
        help=(
            "Pass^k 的 k 值（默认取每任务实际采样次数）。\n"
            "离线模式下预测文件含同 id 多行时，可用本参数限定取前 K 次。"
        ),
    )

    sensitivity = parser.add_argument_group("提示敏感性（Ch7 §7.5，默认关闭）")
    sensitivity.add_argument(
        "--sensitivity",
        action="store_true",
        help=(
            "在线模式：对指令包装做确定性微扰（前缀/后缀/语气变体，\n"
            "问题本体与 must_contain 断言不动），汇总 N 变体间指标极差\n"
            "（sensitivity_range_*，只增、不进门禁）；缺省关闭 = 输出与\n"
            "现状字节等价。离线打分不支持本开关，但已录制的变体数据\n"
            "（带 sensitivity_variant 字段）离线打分时会自动附极差键。"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口：解析参数并分发到对应后端。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    use_judge = bool(args.judge)
    if use_judge:
        print(
            "[提示] LLM-as-a-Judge 为辅助指标：人工抽样校准前请勿用作门禁；"
            "未配置端点将诚实失败（退出码 3），绝不给假分。",
            file=sys.stderr,
        )

    if args.sensitivity and (args.compare or not (args.live or args.record)):
        print(
            "[提示敏感性] --sensitivity 仅在线模式（--live/--record）可用："
            "离线预测是已录制的固定输出，不含指令包装变体——现场微扰必须"
            "在线重新提问。已录制的变体数据离线打分时会自动附 sensitivity_range_* 键。",
            file=sys.stderr,
        )
        return 2

    if args.compare:
        if args.live or args.record:
            print("[配对比较] --compare 只支持离线打分，不能与 --live/--record 同用。",
                  file=sys.stderr)
            return 2
        return run_compare(
            Path(args.compare[0]),
            Path(args.compare[1]),
            golden_path=Path(args.golden),
            k=args.k,
        )

    try:
        if args.live or args.record:
            return run_live(
                golden_path=Path(args.golden),
                predictions_path=Path(args.predictions),
                results_dir=Path(args.results_dir),
                use_judge=use_judge,
                repeat=args.repeat,
                k=args.k,
                sensitivity=bool(args.sensitivity),
            )
        return run_offline(
            golden_path=Path(args.golden),
            predictions_path=Path(args.predictions),
            results_dir=Path(args.results_dir),
            use_judge=use_judge,
            k=args.k,
        )
    except NotImplementedError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 3
    except JudgeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
