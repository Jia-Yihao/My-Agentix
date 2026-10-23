"""终态完成度校验（**确定性**，不调用 LLM）。

《My Agentix_架构优化方案》§9.1 A 组缺口「完成判定来自验证器/环境终态」：
现状 —— 图是否「跑完」由 **LLM 不再发起工具调用** 决定（模型的自我判断），
KG 侧的 `final_answer_guard` 虽已接线但**默认关闭**。

（2026-09 更新：原先此处还并列提到「`validate_final_answer` 节点函数与
`ErrorCode.MAX_STEPS_EXCEEDED` 长期为死代码」。前者已随授权清理**删除**
——它唯一消费者是自身无法导入的遗留工作流；后者保留（对外错误码契约）
但语义改为「预留」。见 `tests/test_p24_deadcode_marking.py`。）

**问题**：把「完成」的判定权完全交给模型，等价于**让考生自己判卷**。
模型可以在证据为空时自信收尾，也可以在检索全挂时照样给出答案。

**本模块的定位**：提供一层**确定性**的终态校验——只看环境事实
（有没有证据？引用在不在？有没有未消解的错误？答案是否为空？），
不引入任何新的模型调用。它与 LLM 侧的 `final_answer_guard` **互补而非替代**：

| | 判据来源 | 成本 | 默认 |
|---|---|---|---|
| `final_answer_guard`（LLM） | 语义一致性 | 一次 LLM 调用 | 关 |
| 本模块（确定性） | 环境终态事实 | 零 | 开（仅记录） |

**默认只记录、不改判定**（``COMPLETION_VALIDATOR_STRICT=False``）：
先把「模型自认完成 vs 环境事实」的差异**观测**出来，再决定是否升级为强制。
直接改判定会让存量行为不可预测——这与项目「新能力默认不改变行为」的惯例一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "CompletionSignal",
    "CompletionVerdict",
    "CITATION_ROUTES",
    "validate_completion",
]

#: 这些路由的答案**必须**有引用来源，否则视为「无依据」
CITATION_ROUTES = frozenset({"graphrag", "kb-query", "kb", "text2sql"})

#: 答案低于该长度视为「实质为空」（仅免责声明/客套话）。
#: 取 6 是**为中文校准**的：中文单字信息密度高，「一加一等于二。」（7 字）
#: 已是完整答案；而真正的空壳（`""` / 「抱歉」/ 「无法回答」）都在 6 以下。
#: 调大可减少漏判但会误伤简短正确答案——故保守取小。
MIN_ANSWER_CHARS = 6


class CompletionSignal(str, Enum):
    """终态判定。"""

    #: 环境事实与「已完成」一致
    COMPLETE = "complete"
    #: 完成了，但有质量折损（缺引用 / 部分工具失败）
    DEGRADED = "degraded"
    #: 事实上未完成（空答案 / 未消解的错误）
    INCOMPLETE = "incomplete"


@dataclass
class CompletionVerdict:
    """校验结论（可直接进 metadata / 轨迹 / 指标）。"""

    signal: CompletionSignal
    reasons: List[str] = field(default_factory=list)
    checked: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_acceptable(self) -> bool:
        """是否可对外呈现（``COMPLETE`` 或 ``DEGRADED``）。"""
        return self.signal is not CompletionSignal.INCOMPLETE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal": self.signal.value,
            "reasons": list(self.reasons),
            "checked": dict(self.checked),
        }


def validate_completion(
    *,
    route: Optional[str] = None,
    answer: Optional[str] = None,
    sources: Optional[Sequence[Any]] = None,
    errors: Optional[Sequence[str]] = None,
    status: Optional[str] = None,
    tool_failures: int = 0,
    min_answer_chars: int = MIN_ANSWER_CHARS,
) -> CompletionVerdict:
    """依据**环境终态事实**判定完成度（纯函数，确定性，零 I/O）。

    判定顺序（先重后轻，返回**最严重**的一档）：

    1. ``status`` 为 ``error`` / 显式 ``errors`` 非空 → ``INCOMPLETE``
       （错误未消解就不算完成）；
    2. 答案为空或短于 ``min_answer_chars`` → ``INCOMPLETE``；
    3. 引用类路由（``CITATION_ROUTES``）但 ``sources`` 为空 → ``DEGRADED``
       （答案存在但**无依据**——测试数据领域这是严重质量问题）；
    4. 有工具失败（``tool_failures > 0``）但答案存在 → ``DEGRADED``；
    5. 其余 → ``COMPLETE``。

    空真防护：``sources`` 为 ``None``（未知）与 ``[]``（确认为空）**区别对待**——
    未知不判罚（避免把「没传」当「没有」），确认为空才判 ``DEGRADED``。
    """
    reasons: List[str] = []
    checked: Dict[str, Any] = {
        "route": route,
        "status": status,
        "answer_chars": len(answer or ""),
        "sources_known": sources is not None,
        "sources_count": len(sources) if sources is not None else None,
        "tool_failures": int(tool_failures),
    }

    # ① 未消解的错误
    error_list = [e for e in (errors or []) if e]
    if status == "error":
        reasons.append("终态为 error（错误未消解）")
    if error_list:
        reasons.append(f"存在未消解错误 {len(error_list)} 条")
    if reasons:
        return CompletionVerdict(CompletionSignal.INCOMPLETE, reasons, checked)

    # ② 实质空答案
    text = (answer or "").strip()
    if len(text) < max(1, int(min_answer_chars)):
        reasons.append(f"答案实质为空（{len(text)} < {min_answer_chars} 字符）")
        return CompletionVerdict(CompletionSignal.INCOMPLETE, reasons, checked)

    # ③ 引用类路由但无来源
    if route in CITATION_ROUTES and sources is not None and len(sources) == 0:
        reasons.append(f"路由 {route} 要求引用，但 sources 为空（答案无依据）")

    # ④ 部分工具失败
    if tool_failures > 0:
        reasons.append(f"有 {tool_failures} 个工具调用失败（结果可能不完整）")

    if reasons:
        return CompletionVerdict(CompletionSignal.DEGRADED, reasons, checked)
    return CompletionVerdict(CompletionSignal.COMPLETE, reasons, checked)
