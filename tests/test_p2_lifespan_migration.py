"""企业化 P2-1.1：on_event → lifespan 迁移回归测试。

FastAPI 已弃用 on_event；本文件锁定：
1. 入口模块保留 _startup/_shutdown 两个生命周期函数（由 lifespan 闭包驱动）；
2. 应用注册的是 lifespan 协议（router.lifespan_context，fastapi 0.142 起可能被
   merged_lifespan 包装层包裹）；
3. 旧的 startup_event/shutdown_event 装饰器路径不复存在。
"""

from __future__ import annotations

import agentix.main as main_module
from agentix.main import application


def test_startup_shutdown_functions_present():
    assert callable(main_module._startup)
    assert callable(main_module._shutdown)


def test_app_uses_lifespan_protocol():
    lifespan_ctx = application.router.lifespan_context
    # fastapi 0.142+ 会把 app 与各子路由的 lifespan 合并为 merged_lifespan 包装层，
    # 语义不变（仍由 lifespan 协议驱动 _startup/_shutdown），故两种名字都接受。
    assert getattr(lifespan_ctx, "__name__", "") in ("lifespan", "merged_lifespan")


def test_legacy_on_event_names_retired():
    assert not hasattr(main_module, "startup_event")
    assert not hasattr(main_module, "shutdown_event")
