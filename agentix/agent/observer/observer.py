"""Observer：把 Observation 追加到内存轨迹（Append-only）。

Phase 10 将与 services.trajectory 收敛；此处只保留最小可测实现。
"""

from __future__ import annotations

from typing import List

from agentix.domain.agent.types import Observation

__all__ = ["InMemoryObserver"]


class InMemoryObserver:
    def __init__(self) -> None:
        self._items: List[Observation] = []

    def record(self, obs: Observation) -> None:
        self._items.append(obs)

    @property
    def items(self) -> tuple[Observation, ...]:
        return tuple(self._items)

    def clear(self) -> None:
        self._items.clear()
