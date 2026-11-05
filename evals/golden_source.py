"""评测入口的 ``--golden`` 缺省解析：默认题集不存在时**响亮退出**（T3）。

**为什么存在**：``evals/legal_golden.jsonl`` / ``evals/legal_golden_ext.jsonl`` 已于
2026-10-26 随评测集下线删除，但 6 个评测入口的 ``--golden`` 缺省串还指向它们
（``run_eval.py`` / ``hybrid_eval.py`` / ``ragas_report.py`` / ``golden_to_promptfoo.py``
/ ``feedback_ingest.py`` / ``scripts/demo/local_kb_demo.py``）。缺省指向一个不存在的
文件，最坏的收法不是报错而是**静默跑空**：题集读成 0 条 → 指标 0 或整表空白 →
退出码 0 → 看的人以为"评测跑过了"。本模块把这条路径统一成**非零退出 + 可用题集清单
+ 改道示例**。

**保守边**（规划文档 §7 Q6 夜间默认取值，待维护者追认）：只改提示与退出码——
本模块**不新建任何题集**，也**不把缺省改指到别的题集**（那是 Q6 的另一边，未裁决）。
显式传 ``--golden`` 且文件存在时，各入口的行为逐位不变。

**清单来自目录扫描**，不是硬编码名单：写死一份"可用题集"清单就是下一个会腐烂的
抄本（AGENTS.md 纪律 7 同一个毛病）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

__all__ = [
    "EVALS_DIR",
    "MISSING_GOLDEN_EXIT",
    "scan_available_goldens",
    "missing_golden_message",
    "ensure_default_golden",
]

#: 题集扫描根目录（本模块住在 evals/ 包里，扫描自身所在目录）
EVALS_DIR: Path = Path(__file__).resolve().parent

#: 题集的识别条件：**文件名**含该关键字的 ``.jsonl``（改名即换集，不写死具体文件名）
GOLDEN_KEYWORD = "golden"
GOLDEN_SUFFIX = ".jsonl"

#: 缺省题集缺失时的退出码。取 2 与各入口既有的"参数/输入不对"用法一致，
#: 不复用 3（3 在 run_eval 里专指"缺端点/未实现"），也不复用 0。
MISSING_GOLDEN_EXIT = 2


def _visible_files(root: Path, pattern: str) -> List[Path]:
    """目录扫描，跳过隐藏目录与 ``__pycache__``（它们不是题集，是工具垃圾）。"""
    found: List[Path] = []
    try:
        candidates = sorted(root.rglob(pattern))
    except OSError:  # pragma: no cover - 目录在扫描中途消失：如实报"扫不到"
        return found
    for path in candidates:
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") or part == "__pycache__" for part in rel_parts):
            continue
        if not path.is_file():
            continue
        found.append(path)
    return found


def scan_available_goldens(evals_dir: Optional[Path] = None) -> List[Path]:
    """当前**真实存在**的题集清单（按路径排序，稳定输出便于人和测试对照）。"""
    root = Path(evals_dir) if evals_dir is not None else EVALS_DIR
    if not root.exists():
        return []
    return [
        path
        for path in _visible_files(root, f"*{GOLDEN_SUFFIX}")
        if GOLDEN_KEYWORD in path.name.lower()
    ]


def _row_count(path: Path) -> Optional[int]:
    """题集行数（给"这份集有多大"的直观信息）；读不到就返回 None，不影响主提示。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:  # pragma: no cover - 提示路径绝不因统计失败而炸
        return None


def missing_golden_message(
    *, entry: str, default: Path, available: Sequence[Path], example_flags: str = ""
) -> str:
    """响亮退出的完整提示（多行，交给调用方打到 stderr）。"""
    lines: List[str] = [
        f"[{entry}] 默认题集不存在：{default}",
        f"[{entry}] 该集已随评测集下线删除；本轮不新建题集、也不把默认改指别的集"
        "（规划文档 §7 Q6 保守边，待维护者追认）。",
        f"[{entry}] 与其静默跑空、把空样本报成 0 分，这里直接非零退出。",
    ]
    if available:
        lines.append(
            f"[{entry}] 当前可用题集（扫描 {Path(default).parent} 所得，非硬编码清单）："
        )
        for path in available:
            rows = _row_count(path)
            suffix = f"（{rows} 行）" if rows is not None else ""
            lines.append(f"[{entry}]   - {path}{suffix}")
        lines.append(
            f"[{entry}] 改道示例（显式指定题集即可，其余行为逐位不变）："
            f"\n        .venv/Scripts/python.exe {entry} --golden {available[0]}"
            f" {example_flags}".rstrip()
        )
    else:
        lines.append(
            f"[{entry}] 当前可用题集：{Path(default).parent} 下扫描不到任何"
            f" *{GOLDEN_SUFFIX} 且文件名含 \u201c{GOLDEN_KEYWORD}\u201d 的文件。"
        )
        lines.append(
            f"[{entry}] 改道指引：先按 evals/README.md 的题集接入流程放一份评测集，"
            f"再跑 .venv/Scripts/python.exe {entry} --golden <题集路径>"
        )
    return "\n".join(lines)


def ensure_default_golden(
    explicit: Any,
    *,
    entry: str,
    default: Path,
    example_flags: str = "",
    exit_code: int = MISSING_GOLDEN_EXIT,
) -> Tuple[Any, int]:
    """解析 ``--golden``：返回 ``(生效的题集参数, 退出码)``。

    * 调用方**显式**传了值（字符串或路径列表）⇒ 原样返回、退出码 0：
      行为与改动前逐位一致，含"传了个不存在的文件"这种由各入口自己报错的情形；
    * 未传且默认文件存在 ⇒ 返回默认路径（同样逐位一致）；
    * 未传且默认文件不存在 ⇒ 打印可用清单与改道示例，退出码非零，
      第一项返回 ``None``（调用方必须先看退出码，别拿 None 继续跑）。
    """
    if explicit:
        return explicit, 0
    if Path(default).exists():
        return default, 0
    print(
        missing_golden_message(
            entry=entry,
            default=Path(default),
            available=scan_available_goldens(Path(default).parent),
            example_flags=example_flags,
        ),
        file=sys.stderr,
    )
    return None, exit_code
