"""声明式任务依赖与调度波次（任务分解与规划的显式化）。

《My Agentix_架构优化方案》§9.1 A 组缺口：

* **「串行/并行依赖显式声明并据此调度」** —— 现状：并发已落地
  （`kb_retrieval.py` 的 `asyncio.gather`、`edges.py` 的 `Send` fan-out），
  但**依赖关系是隐含在代码控制流里的**，没有可检视、可校验的声明。
* **「子任务可独立验证 + 失败局部重试」** —— 现状：「可独立」已达成，
  「局部重试」只做到检索通道级，多工具 task 级仍无。

**为什么要把依赖声明出来**：依赖藏在 `await` 的顺序里时——

1. **无法校验**：环形依赖只能靠运行期死锁暴露；
2. **无法解释**：调度顺序无人能一眼看懂，改动容易踩到隐式约束；
3. **无法重试**：不知道该重跑哪一段（重跑整个批次的代价可能不可接受）。

本模块提供两层：

- :func:`plan_waves` —— 把 ``TaskSpec`` 列表拓扑排序成**执行波次**：
  同一波次内可安全并行，波次之间必须串行。串行/并行因此成为**声明的推论**
  而非人工判断。
- :func:`validate_plan` —— 结构校验：缺失依赖、自依赖、环形依赖、重名。

**纯逻辑、零 I/O、零第三方依赖**：拓扑排序用 Kahn 算法，输出确定性
（同波次内按声明顺序，便于快照测试与复现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple

__all__ = [
    "TaskSpec",
    "PlanError",
    "PlanValidation",
    "plan_waves",
    "validate_plan",
    "summarize_plan",
]


class PlanError(ValueError):
    """计划结构非法（缺失依赖 / 自依赖 / 环形依赖 / 重名）。"""


@dataclass(frozen=True)
class TaskSpec:
    """一个子任务的**声明**（不持有执行体，只描述依赖与载荷）。

    :param name: 任务唯一名（波次与依赖均按名引用）。
    :param depends_on: 必须在它**之前**完成的任务名元组。
    :param payload: 传给执行体的载荷（纯数据，便于序列化与重放）。
    :param meta: 任意元信息（如工具名、超时、重试次数）。
    """

    name: str
    depends_on: Tuple[str, ...] = ()
    payload: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise PlanError("TaskSpec.name 不得为空")
        # 冻结化：允许调用方传 list，但内部统一为 tuple（可哈希、不可变）
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


@dataclass
class PlanValidation:
    """校验结果。"""

    ok: bool = True
    duplicates: List[str] = field(default_factory=list)
    missing: List[Tuple[str, str]] = field(default_factory=list)   # (task, 缺失依赖)
    self_deps: List[str] = field(default_factory=list)
    cycles: List[List[str]] = field(default_factory=list)

    def errors(self) -> List[str]:
        out: List[str] = []
        if self.duplicates:
            out.append(f"任务名重复：{self.duplicates}")
        if self.missing:
            out.append(f"依赖缺失：{self.missing}")
        if self.self_deps:
            out.append(f"自依赖：{self.self_deps}")
        if self.cycles:
            out.append(f"环形依赖：{self.cycles}")
        return out


def validate_plan(tasks: Sequence[TaskSpec]) -> PlanValidation:
    """结构校验（**不抛异常**，返回结果对象便于调用方决策）。"""
    result = PlanValidation()
    names = [t.name for t in tasks]

    seen: set = set()
    for name in names:
        if name in seen:
            result.duplicates.append(name)
        seen.add(name)

    known = set(names)
    for task in tasks:
        for dep in task.depends_on:
            if dep == task.name:
                result.self_deps.append(task.name)
            elif dep not in known:
                result.missing.append((task.name, dep))

    result.cycles = _find_cycles(tasks)
    result.ok = not (
        result.duplicates or result.missing or result.self_deps or result.cycles
    )
    return result


def plan_waves(
    tasks: Sequence[TaskSpec],
    *,
    strict: bool = True,
) -> List[List[TaskSpec]]:
    """把任务拓扑排序为**执行波次**（Kahn 算法，确定性输出）。

    - 第 0 波：无依赖的任务 —— 可**全部并行**；
    - 第 n 波：仅依赖前 n-1 波的任务 —— 必须等前序波次完成。

    同波次内保持**声明顺序**（稳定排序，便于快照测试）。

    :param strict: ``True``（默认）时结构非法直接抛 :class:`PlanError`；
        ``False`` 时尽力而为（忽略缺失依赖，仅对环报错）。
    """
    validation = validate_plan(tasks)
    if strict and not validation.ok:
        raise PlanError("；".join(validation.errors()) or "计划非法")
    if validation.cycles:
        raise PlanError(f"环形依赖无法调度：{validation.cycles}")

    by_name: Dict[str, TaskSpec] = {}
    order: Dict[str, int] = {}
    for idx, task in enumerate(tasks):
        if task.name not in by_name:
            by_name[task.name] = task
            order[task.name] = idx

    # 只保留已知依赖（非 strict 模式下静默忽略缺失项）
    deps: Dict[str, set] = {
        name: {d for d in task.depends_on if d in by_name}
        for name, task in by_name.items()
    }

    remaining = set(by_name)
    waves: List[List[TaskSpec]] = []
    while remaining:
        ready = sorted(
            (n for n in remaining if not (deps[n] & remaining)),
            key=lambda n: order[n],
        )
        if not ready:  # 理论上被 _find_cycles 拦住；兜底避免死循环
            raise PlanError(f"存在无法调度的任务（疑似环形依赖）：{sorted(remaining)}")
        waves.append([by_name[n] for n in ready])
        remaining -= set(ready)

    return waves


def summarize_plan(tasks: Sequence[TaskSpec]) -> Dict[str, Any]:
    """把计划压成可上报/可断言的摘要。"""
    validation = validate_plan(tasks)
    waves = plan_waves(tasks, strict=False) if not validation.cycles else []
    return {
        "task_count": len(tasks),
        "valid": validation.ok,
        "errors": validation.errors(),
        "wave_count": len(waves),
        "waves": [[t.name for t in wave] for wave in waves],
        # 最大并发度 = 最宽波次宽度（据此可预分配并发额度）
        "max_parallelism": max((len(w) for w in waves), default=0),
        # 串行深度 = 波次数
        "serial_depth": len(waves),
    }


def _find_cycles(tasks: Sequence[TaskSpec]) -> List[List[str]]:
    """找出所有参与环的任务链（DFS 三色标记，输出确定性）。"""
    known = {t.name for t in tasks}
    deps: Dict[str, List[str]] = {
        t.name: [d for d in t.depends_on if d in known] for t in tasks
    }
    order = {t.name: i for i, t in enumerate(tasks)}

    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {name: WHITE for name in known}
    stack: List[str] = []
    cycles: List[List[str]] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in deps.get(node, []):
            if color.get(nxt, BLACK) == GRAY:
                # 找到回边：截取栈中从 nxt 起的一段即为环
                if nxt in stack:
                    cycles.append(stack[stack.index(nxt) :])
            elif color.get(nxt, BLACK) == WHITE:
                visit(nxt)
        stack.pop()
        color[node] = BLACK

    for name in sorted(known, key=lambda n: order[n]):
        if color[name] == WHITE:
            visit(name)
    return cycles


def iter_parallel_groups(tasks: Sequence[TaskSpec]) -> Iterable[List[TaskSpec]]:
    """便捷迭代器：逐个产出可并行的任务组（等价于 ``plan_waves``）。"""
    yield from plan_waves(tasks)
