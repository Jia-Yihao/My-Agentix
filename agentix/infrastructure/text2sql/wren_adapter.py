"""Wren 语义层适配器——Text2SQL 的 WrenAI 执行后端（SDK 集成）。

WrenAI 上游仓（github.com/WrenAI/WrenAI）提供的是
**语义 SQL 引擎**（非 LLM 管线）：MDL 语义建模定义（模型/指标/关系/RLAC）
+ wren-core（DataFusion 系）规划器。本适配器把既有 Text2SQL 工作流的
「SQL 执行」一步从 MySQL 直连升级为 Wren 语义层执行：

    LLM 生成 SQL（既有，不变）→ 语法校验（既有，不变）
      → [wren] dry_run 语义校验 → [wren] query 语义执行 → pyarrow → dicts
      → [mysql] 直连执行（默认路径 / wren 降级路径）

双通道加载（都延迟导入，未装包不炸）：
1. **SDK 通道**（首选）：``wren_langchain.WrenToolkit.from_project``——
   需要 CLI 准备好的 Wren 项目（``wren_project.yml`` + ``target/mdl.json``），
   连接信息由项目 ``profiles.yml`` 解析；
2. **直连引擎通道**：``wren.engine.WrenEngine``——manifest 直接取
   ``target/mdl.json``，数据源/连接信息来自 settings（WREN_DATA_SOURCE /
   WREN_CONNECTION_INFO）。

两通道都不可用（``wrenai`` 未安装 / 项目路径无效）时抛
:class:`WrenNotAvailable`——调用方（sql_execution 节点）捕获后**降级回
MySQL** 并在 steps 留痕 ``sql_execution_wren_degraded``，主链路绝不中断。
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentix.config import settings

logger = logging.getLogger("agentix.infrastructure.text2sql.wren")

__all__ = [
    "WrenNotAvailable",
    "WrenSQLBackend",
    "get_wren_backend",
]


class WrenNotAvailable(RuntimeError):
    """wrenai 未安装、Wren 项目无效或引擎初始化失败。"""


class WrenSQLBackend:
    """Wren 语义层执行后端（懒加载引擎，首次使用才建连接）。"""

    def __init__(self, project_path: str | Path) -> None:
        self._project_path = Path(project_path).expanduser().resolve()
        self._toolkit: Any = None      # wren_langchain.WrenToolkit（SDK 通道）
        self._engine: Any = None       # wren.engine.WrenEngine（直连通道）
        self._manifest_b64: Optional[str] = None

    # -- 可用性 -----------------------------------------------------------

    @property
    def project_path(self) -> Path:
        return self._project_path

    def _ensure_project(self) -> None:
        if not (self._project_path / "target" / "mdl.json").exists():
            raise WrenNotAvailable(
                f"Wren project invalid at {self._project_path}: "
                "target/mdl.json not found (run `wren context build` first)"
            )

    def _load_manifest_b64(self) -> str:
        if self._manifest_b64 is None:
            manifest_file = self._project_path / "target" / "mdl.json"
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            self._manifest_b64 = base64.b64encode(
                json.dumps(manifest).encode("utf-8")
            ).decode()
        return self._manifest_b64

    def _ensure_ready(self) -> None:
        """懒加载：SDK 通道优先，失败落直连引擎通道。"""
        if self._toolkit is not None or self._engine is not None:
            return
        self._ensure_project()
        try:  # SDK 通道：WrenToolkit（连接信息走项目 profiles.yml）
            from wren_langchain import WrenToolkit  # type: ignore

            self._toolkit = WrenToolkit.from_project(self._project_path)
            logger.info("Wren backend ready (sdk channel): %s", self._project_path)
            return
        except ImportError:
            logger.info("wren_langchain not installed, trying direct engine")
        except Exception as exc:  # noqa: BLE001 - 项目无效等 → 落直连通道重试
            logger.warning("WrenToolkit init failed (%s), trying direct engine", exc)

        try:  # 直连通道：WrenEngine + settings 连接信息
            from wren.engine import WrenEngine  # type: ignore

            self._engine = WrenEngine(
                manifest_str=self._load_manifest_b64(),
                data_source=settings.WREN_DATA_SOURCE,
                connection_info=json.loads(settings.WREN_CONNECTION_INFO or "{}"),
            )
            logger.info("Wren backend ready (direct channel): %s", self._project_path)
        except ImportError as exc:
            raise WrenNotAvailable(
                "wrenai package not installed (pip install wrenai); "
                "install pending user approval"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise WrenNotAvailable(f"WrenEngine init failed: {exc}") from exc

    # -- 能力 -------------------------------------------------------------

    def dry_plan(self, sql: str) -> str:
        """MDL 语义展开：返回目标方言的最终 SQL（不执行）。"""
        self._ensure_ready()
        if self._toolkit is not None:
            return str(self._toolkit.dry_plan(sql))
        return str(self._engine.dry_plan(sql))

    def validate(self, sql: str) -> None:
        """语义校验：规划但不执行（失败抛 WrenNotAvailable/底层异常）。"""
        self._ensure_ready()
        if self._toolkit is not None:
            self._toolkit.dry_run(sql)
        else:
            self._engine.dry_run(sql)

    def execute(self, sql: str, max_rows: int | None = None) -> List[Dict[str, Any]]:
        """语义执行：RLAC/指标定义在规划期生效，返回 list[dict]。"""
        self._ensure_ready()
        limit = int(max_rows or settings.WREN_QUERY_LIMIT)
        table = (
            self._toolkit.query(sql, limit=limit)
            if self._toolkit is not None
            else self._engine.query(sql, limit=limit)
        )
        return list(table.to_pylist())[:limit]


def get_wren_backend() -> Optional[WrenSQLBackend]:
    """按 settings 构建后端；未启用/路径为空/项目无效 → None（走 MySQL）。"""
    if (settings.TEXT2SQL_BACKEND or "mysql").lower() != "wren":
        return None
    if not settings.WREN_PROJECT_PATH:
        logger.warning("TEXT2SQL_BACKEND=wren but WREN_PROJECT_PATH empty; fallback to mysql")
        return None
    backend = WrenSQLBackend(settings.WREN_PROJECT_PATH)
    try:
        backend._ensure_project()  # noqa: SLF001 - 工厂内的前置校验
    except WrenNotAvailable as exc:
        logger.warning("Wren project invalid (%s); fallback to mysql", exc)
        return None
    return backend
