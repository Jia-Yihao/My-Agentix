"""FastAPI 路由内省工具。

fastapi 0.142 起 ``include_router`` 不再把子路由摊平进 ``app.routes``，而是挂一个惰性
``_IncludedRouter`` 占位对象（真正路由按需物化在 ``effective_candidates()`` /
``effective_low_priority_routes()`` 里）。契约类测试需要"摊平后的全量路径视图"，
这里统一处理：

- 命中带 ``effective_candidates`` 的对象 → 递归展开（新架构）；
- 否则原样返回（旧架构的 APIRoute/Route/Mount 直接可用，向后兼容）。

fastapi 在 requirements.txt 钉死 0.142.2，内省行为漂移会被全量套件立刻抓住。
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, List


def iter_effective_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """递归展开路由列表，返回可取 ``path``/``methods`` 的叶子路由对象。"""
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from iter_effective_routes(candidates())
            low = getattr(route, "effective_low_priority_routes", None)
            if callable(low):
                yield from iter_effective_routes(low())
            continue
        yield route


def effective_paths(routes: Iterable[Any]) -> List[str]:
    """摊平后的全部路径（跳过无 path 的对象，去重保序）。"""
    seen: dict[str, None] = {}
    for route in iter_effective_routes(routes):
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            seen.setdefault(path, None)
    return list(seen)
