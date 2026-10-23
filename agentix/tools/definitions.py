import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Type

from loguru import logger

# M0 shim：ToolSpec 与风险分级常量的规范位已迁 agentix/domain/tool/spec.py
# （对象同一，M6 清尾）——本模块继续作为 capabilities.tools 的转出口。
from agentix.domain.tool.spec import (  # noqa: F401
    CONTROL_REQUIRED_FROM,
    FORBIDDEN_RISK_LEVEL,
    RISK_LEVELS,
    ToolSpec,
)


class ToolRegistry:
    """Keep track of tool specifications and factories."""

    def __init__(self) -> None:
        self._specs: Dict[str, ToolSpec] = {}
        self._factories: Dict[str, Callable[[], Any]] = {}
        self._frozen: Optional[Tuple[str, ...]] = None

    def register(self, spec: ToolSpec, factory: Callable[[], Any]) -> None:
        # Phase 22（KV Cache 纪律）：暂停快照后拒绝运行期动态注册——
        # schema 前缀「首次构建后不可重排」（Gotcha：动态注册属违例）。
        if self._frozen is not None:
            logger.warning(
                "ToolRegistry frozen ({} tools); rejecting late registration of {!r}",
                len(self._frozen),
                spec.name,
            )
            return
        self._specs[spec.name] = spec
        self._factories[spec.name] = factory

    def freeze(self) -> Tuple[str, ...]:
        """暂停注册序快照（幂等）：返回快照并从现在起拒绝 ``register``。

        ``delete_tool`` 不受限（下线工具不重排序）；测试可用
        ``unfreeze`` 解冻。这是「首次构建后不可重排」的可执行形式。
        """
        if self._frozen is None:
            self._frozen = tuple(self._specs.keys())
        return self._frozen

    def is_frozen(self) -> bool:
        return self._frozen is not None

    def unfreeze(self) -> None:
        """解除暂停（测试隔离用；生产路径不调用）。"""
        self._frozen = None

    def get_spec(self, name: str) -> ToolSpec:
        return self._specs[name]

    def get_specs(self) -> Iterable[ToolSpec]:
        return self._specs.values()

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def delete_tool(self, name: str) -> bool:
        """注销工具（spec + factory 成对删除）。

        返回是否真的删了；不存在时静默返回 False——供测试夹具
        快照恢复与动态下线使用，不抛异常。
        """
        existed = name in self._specs
        self._specs.pop(name, None)
        self._factories.pop(name, None)
        return existed

    def tool_names(self) -> Iterable[str]:
        """Return registered tool names in registration order."""
        return self._specs.keys()

    def describe_groups(self) -> Dict[str, Tuple[str, ...]]:
        """按**首个** capability 标签分组的元数据目录（Phase 21）。

        - 无标签归入 ``"general"``；组序 = 首次出现的注册序，组内 = 注册序，
          同一注册状态下两次调用输出完全稳定（供 planner/文档消费）；
        - 条目形如 ``"name：description 首行"``（纯元数据，不触发任何工厂）。
        """
        groups: Dict[str, List[str]] = {}
        for spec in self._specs.values():
            key = spec.capabilities[0] if spec.capabilities else "general"
            first_line = (spec.description or "").strip().splitlines()
            short = first_line[0].strip() if first_line else ""
            entry = f"{spec.name}：{short}" if short else spec.name
            groups.setdefault(key, []).append(entry)
        return {key: tuple(entries) for key, entries in groups.items()}

    def create(self, name: str) -> Any:
        factory = self._factories.get(name)
        if factory is None:
            raise KeyError(f"No tool registered under name '{name}'")
        return factory()


registry = ToolRegistry()


def schema_prefix_hash(schema_classes: Optional[Iterable[Type[Any]]] = None) -> str:
    """LLM 可见工具 schema 序列的稳定哈希（测试辅助，Phase 22）。

    内容 = 各 schema 的 canonical JSON（``sort_keys``），顺序 = 声明序
    （注册序，不重排）——同配置连续两次调用必须一致；``CONTEXT_AUTOCOMPACT``
    等上下文 flag 不进 schema 内容，开关切换不改变哈希。

    缺省从 ``get_active_tools()`` 懒取（惰性 import 防环）；也可显式传入
    schema 类序列做确定性对比（不触发注册/工厂）。
    """
    if schema_classes is None:
        from agentix.tools.registry_init import get_active_tools

        schema_classes = get_active_tools()
    payload = [cls.model_json_schema() for cls in schema_classes]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
