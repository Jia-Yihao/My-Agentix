"""P110：数据库 engine 惰性化（§24.2 G10 / §24.3 T1 ⑬ 落地回归）。

G10 的形态是「import 路径经过就建池」：`agentix.capabilities.mcp` →
`agentix.tools` → `adapters/search.py` → `infrastructure/core/__init__` →
`database.py` 的模块级 `create_engine`。后果三条，本文件逐条钉住：

1. 只测 MCP 客户端也必须具备数据库依赖（连驱动都得装齐）；
2. `DATABASE_URL` 写错的故障发生在 **import 期**，而不是启动校验点；
3. 「子进程只为跑一个工具」的轻加载路线（T2）被 import 副作用堵死。

现在 `engine` / `SessionLocal` 首次被**真正取用**才构造，且两处再导出面
（`database.py` 与 `infrastructure/core/__init__.py`）都必须保持惰性——
只改一层等于没改，因为 `from .database import engine` 本身就触发构造。

覆盖方式说明：判据是「import 期有没有调用 `create_engine`」，这是**进程级**事实，
在 pytest 进程内测会被其他测试的导入污染，所以关键三条跑真子进程；
其余（单例身份、`__getattr__` 语义、旧形状仍可用）在进程内测。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 子进程里用来计数 create_engine 调用的公共前导代码
_SPY = """
import sqlalchemy as _sa
_calls = []
_orig = _sa.create_engine
def _spy(*a, **k):
    # 只记 scheme：连接串含凭据，绝不进测试输出
    _calls.append(str(a[0]).split("://")[0])
    return _orig(*a, **k)
_sa.create_engine = _spy
"""


def _run(code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    child = {**os.environ, **(env or {}), "PYTHONPATH": str(PROJECT_ROOT)}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PROJECT_ROOT),
        env=child,
        timeout=180,
    )


# ---------------------------------------------------------------------------
# 1. import 期不建池（G10 的正题）
# ---------------------------------------------------------------------------


def test_importing_mcp_package_creates_no_engine() -> None:
    proc = _run(_SPY + "import agentix.capabilities.mcp\nprint('N=' + str(len(_calls)))")
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "N=0" in proc.stdout, f"import MCP 仍在建池：{proc.stdout[-600:]}"


def test_importing_tools_package_creates_no_engine() -> None:
    proc = _run(_SPY + "import agentix.tools\nprint('N=' + str(len(_calls)))")
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "N=0" in proc.stdout, f"import agentix.tools 仍在建池：{proc.stdout[-600:]}"


def test_importing_core_package_re_exports_lazily() -> None:
    """`from agentix.infrastructure.core import Base` 也不该顺手建池。

    这层最容易复发：包门面写一句 `from .database import engine` 就把惰性打回原形，
    而语法与类型检查都不会报错。
    """
    proc = _run(
        _SPY
        + "from agentix.infrastructure.core import Base, get_db\n"
        "print('N=' + str(len(_calls)))"
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "N=0" in proc.stdout, f"包门面仍在导入期建池：{proc.stdout[-600:]}"


def test_first_touch_creates_exactly_one_engine() -> None:
    proc = _run(
        _SPY
        + "import agentix.capabilities.mcp\n"
        "from agentix.infrastructure.core import engine\n"
        "from agentix.infrastructure.core import SessionLocal\n"
        "SessionLocal\n"
        "print('N=' + str(len(_calls)) + ' TYPE=' + type(engine).__name__)"
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    # 两个惰性名都取过，仍只建一次
    assert "N=1 TYPE=Engine" in proc.stdout, proc.stdout[-600:]


def test_missing_driver_fails_at_use_not_at_import() -> None:
    """G10 最难堪的一条：缺驱动时连「只想测 MCP 客户端」都 import 不动。

    现在 import 必须成功；真正取用 engine 才炸，且炸点是可定位的属性访问。
    """
    bad = "postgresql+nosuchdriver://u:p@127.0.0.1:5/db"
    imported = _run(
        "import agentix.capabilities.mcp, agentix.tools;"
        "print('IMPORT_OK')",
        {"DATABASE_URL": bad},
    )
    assert imported.returncode == 0, imported.stderr[-1500:]
    assert "IMPORT_OK" in imported.stdout

    touched = _run(
        "from sqlalchemy.exc import NoSuchModuleError\n"
        "from agentix.infrastructure.core import database\n"
        "try:\n"
        "    database.engine\n"
        "except NoSuchModuleError as exc:\n"
        "    print('RAISED=' + type(exc).__name__)\n"
        "else:\n"
        "    print('NO_RAISE')",
        {"DATABASE_URL": bad},
    )
    assert touched.returncode == 0, touched.stderr[-1500:]
    # 不是裸 ModuleNotFoundError：SQLAlchemy 把「方言驱动找不到」包成自己的
    # NoSuchModuleError。断言这个类型，而不是放宽成 Exception——后者会把
    # 「取用才炸」和「取用炸了句莫名其妙的」混为一谈。
    assert "RAISED=NoSuchModuleError" in touched.stdout, touched.stdout[-600:]


# ---------------------------------------------------------------------------
# 2. 旧形状仍然可用（惰性不能改语义）
# ---------------------------------------------------------------------------


def test_from_import_still_yields_the_shared_engine() -> None:
    from agentix.infrastructure.core import engine as via_package
    from agentix.infrastructure.core.database import engine as via_module
    from agentix.infrastructure.core.database import get_engine

    assert via_package is via_module
    assert get_engine() is via_module


def test_retrieval_layer_shares_the_same_engine() -> None:
    """R3.1 的共享 engine 契约不许被惰性化打断（retrieval 侧不再是第二个实例）。"""
    pytest.importorskip("sqlmodel")
    from agentix.infrastructure.core.database import get_engine
    from agentix.retrieval.infra.db.session import engine as retrieval_engine
    from agentix.retrieval.infra.db.session import get_engine as retrieval_get_engine

    assert retrieval_engine is get_engine()
    assert retrieval_get_engine() is get_engine()


def test_session_factory_is_a_stable_object() -> None:
    """`SessionLocal` 反复取用必须是同一个 sessionmaker，不能每次新建。"""
    from agentix.infrastructure.core import SessionLocal as via_package
    from agentix.infrastructure.core.database import get_session_factory

    assert via_package is get_session_factory()
    assert get_session_factory() is get_session_factory()


def test_unknown_attribute_raises_attribute_error() -> None:
    """`__getattr__` 不能把拼错的属性静默吞成 None。"""
    import agentix.infrastructure.core as core_package
    from agentix.infrastructure.core import database as database_module

    # 逐个写属性访问，不写成 `getattr(module, "常量")`：后者被 ruff B009 判成
    # 「用常量键绕路」，而这里要的恰恰是「访问一个不存在的名字」这条普通路径。
    with pytest.raises(AttributeError):
        core_package.engine_but_misspelled
    with pytest.raises(AttributeError):
        database_module.engine_but_misspelled


def test_lazy_names_are_visible_in_dir() -> None:
    import agentix.infrastructure.core as core_package

    from agentix.infrastructure.core import database as database_module

    assert "engine" in dir(core_package)
    assert "SessionLocal" in dir(database_module)


def test_no_module_level_create_engine_left() -> None:
    """反向钉子：源文件里不许再出现「模块级 create_engine 赋值」。

    惰性化的实现会随版本改写，但这条形状约束不该腐：一旦有人把
    `engine = create_engine(...)` 搬回顶层，测试立刻红。
    """
    source = (PROJECT_ROOT / "agentix/infrastructure/core/database.py").read_text(
        encoding="utf-8"
    )
    lines = [
        line
        for line in source.splitlines()
        if line.startswith("engine") and "create_engine" in line
    ]
    assert not lines, f"模块级建池又回来了：{lines}"

    pkg_init = (PROJECT_ROOT / "agentix/infrastructure/core/__init__.py").read_text(
        encoding="utf-8"
    )
    eager = [
        line
        for line in pkg_init.splitlines()
        if line.startswith("from .database import") and " engine" in line
    ]
    assert not eager, f"包门面又在导入期取 engine：{eager}"
