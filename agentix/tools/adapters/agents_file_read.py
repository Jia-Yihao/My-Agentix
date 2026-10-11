"""F1b2·M-P1-4 ``read_agents_file`` 取回工具（AGENTS 级联的运行期按需半程）。

背景：``instructions.build_instructions`` 的子目录层只在**构建时**加载消息
触达的目录；AgentLoop 运行中自行决定进入的子目录，rules 不重建 ⇒ 需要一个
L0 只读工具按需取回约定文件（Claude Code「root 常驻、子目录按需」的运行期
通道）。

职责切分（与 ``read_artifact`` / ``read_skill`` 同款）：

- **声明**（spec + LLM schema）在本文件，由 ``registry_init`` 在
  ``settings.AGENTS_CASCADE_ENABLED=true`` 时挂进声明清单（flag-off 零注册，
  声明清单与基线逐项一致）；
- **实现**（handler → 向上查找 + 读取截断）在组合根
  ``agentix/agent/context/install.py``（capabilities 不反向依赖上层）。

治理语义：

- **只读**：``side_effect=False``、声明 L0；名称（read_agents_file）不命中
  ``human_review.py`` 词表（无 write/run/exec 等词）⇒ 有效风险 LOW，
  任何模式下免审批；
- schema 类名 ``read_agents_file`` == 工具注册名（title 契约——否则
  ``selection._spec_for_schema`` 的 title 回查静默 miss）；
- 正文上限与指令层同源（单文件 4k + 截断提示）——取回不等于把整个文件树
  倾倒进上下文；
- 查找**以 cwd 为项目边界**（``stop=cwd``），绝不越出项目读文件。
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

__all__ = [
    "read_agents_file",
    "validate_read_agents_file_args",
    "build_read_agents_file_spec",
]


class read_agents_file(BaseModel):
    """``read_agents_file`` 入参（类名即工具名：schema title 必须等于注册名，
    否则 ``selection._spec_for_schema`` 的 title 回查会静默 miss）。"""

    path: str = Field(
        description=(
            "项目内路径（文件或目录，如 src/foo/bar.py 或 src/foo）——"
            "工具从该位置向上查找最近的 AGENTS.md/CLAUDE.md"
        )
    )


def validate_read_agents_file_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化 ``read_agents_file`` 入参；非法输入抛 ``ValueError``。"""
    path = str(args.get("path") or "").strip()
    if not path or len(path) > 512:
        raise ValueError("path must be non-empty and <=512 chars")
    return {"path": path}


def build_read_agents_file_spec() -> ToolSpec:
    """构造 ``read_agents_file`` 声明（L0 只读、零副作用；免审批）。"""
    return ToolSpec(
        name="read_agents_file",
        risk_level="L0",  # 只读取回；词表（write/run/exec/...）不命中 → LOW
        description=(
            "按项目内路径向上查找并读取最近的 AGENTS.md/CLAUDE.md 约定文件（只读）。\n"
            "何时用：要进入某个子目录工作、或其协作约定（目录级 AGENTS.md）"
            "可能影响做法，而该目录的约定未被注入当前上下文时。\n"
            "何时不能用：项目根的约定已常驻注入（直接遵守即可，不必重读）；"
            "path 为空、越出项目边界或向上没有约定文件（本工具返回 found=false，"
            "不会去项目外找）。\n"
            "注意：只读、零副作用、免审批；正文超 4k 确定性截断并提示。"
        ),
        parameters=read_agents_file.model_json_schema(),
        side_effect=False,
        capabilities=("instructions",),
        timeout_ms=10_000,
    )
