"""
Core application utilities.

`engine` / `SessionLocal` 走 PEP 562 `__getattr__` **惰性再导出**（§24.2 G10 /
§24.3 T1 ⑬）：本包被 import 时不建连接池。直接在顶层 `from .database import engine`
就等于「凡是路径经过本包的导入都必须在 import 期具备数据库依赖」——缺驱动即
import 失败，配置错误也在 import 期而不是启动校验点暴露。既有调用点的形状不变：
`from agentix.infrastructure.core import engine` 仍然可用，只是取到才建。
"""

from .database import Base, get_db, init_db, session_scope
from .logger import configure_logging, get_logger

#: 需要按需构造的惰性再导出名（其余名字 import 期即就位，无副作用）
_LAZY_NAMES = frozenset({"engine", "SessionLocal"})

__all__ = [
    "Base",
    "SessionLocal",
    "engine",
    "get_db",
    "init_db",
    "session_scope",
    "configure_logging",
    "get_logger",
]


def __getattr__(name: str):
    if name in _LAZY_NAMES:
        from agentix.infrastructure.core import database as _database

        resolved = getattr(_database, name)
        globals()[name] = resolved  # 同一个单例只构造一次
        return resolved
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_NAMES)
