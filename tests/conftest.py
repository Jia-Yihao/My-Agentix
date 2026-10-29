"""
Pytest configuration.

Make the project importable in all environments without requiring an editable install.
Some CI/runner setups use `--import-mode=importlib` or otherwise do not add the repo root
to `sys.path` consistently, so we do it explicitly here.
"""

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 测试统一使用 SQLite 内存库，避免 settings 单例读到 .env 的部署形态
# （compose 内 postgres/mysql 主机名在本地无法解析）。
# setdefault：若 CI/运行环境显式导出了 DATABASE_URL，则尊重显式值。
# 必须在 conftest 顶层（任何测试 import settings 单例之前）执行——
# pydantic Settings 实例化后不再回头读环境变量（R3.8 教训：r31 单文件
# 内的 setdefault 在与 p35 组跑时因导入顺序失效）。
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# M3-2（§7.5 第②步）：测试套件的装配根，等价于 agentix/main.py 的挂线——
# lg_supervisor 的 ROUTE_HANDLERS/route_query 自本批起经节点注册表解析，装配缺失
# 即 fail-closed 抛 NodeNotRegistered，故注册线必须在任何测试模块 import 编排层之前
# 生效。必须排在上面的 DATABASE_URL setdefault 之后（R3.8 教训：本行经 settings
# 单例拉起 pydantic Settings，早于环境变量铺垫会把 .env 的部署形态冻进单例）。
import agentix.application.wiring  # noqa: E402, F401 - 装配期注册节点 handler（§7.5 第②步）

