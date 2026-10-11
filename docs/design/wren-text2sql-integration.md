# WrenAI 语义层集成（Text2SQL 后端升级）

> 目标：把 WrenAI 的语义 SQL 引擎作为 My Agentix Text2SQL 的核心执行组件——
> LLM 生成 SQL 的既有管线不动，SQL 的**语义校验与执行**交给 Wren 引擎，
> 使 MDL 语义建模（模型/指标/关系定义/行级访问控制 RLAC）在问答链路中生效。

## 架构

```
用户问题
  ↓
Text2SQL Workflow（既有，零改动）
  schema 检索 → SQL 生成（LLM）→ 语法校验（既有）
  ↓
sql_execution 节点
  ├─ TEXT2SQL_BACKEND=mysql（默认）→ MySQL 直连（既有路径，steps=sql_execution）
  └─ TEXT2SQL_BACKEND=wren
       ├─ WrenSQLBackend.validate()  → dry_run 语义校验（不执行）
       ├─ WrenSQLBackend.execute()   → 语义执行（RLAC/指标在规划期生效）
       │     steps=sql_execution_wren
       └─ 不可用 → 降级回 MySQL 直连，steps=sql_execution_wren_degraded
```

设计纪律：
- **默认零行为变化**：`TEXT2SQL_BACKEND` 默认 `mysql`，不配置 Wren 时全链路与升级前一致；
- **绝不中断主链路**：wrenai 未安装 / 项目路径无效 / 语义校验或执行失败，一律降级
  MySQL 并在 steps 留痕（与既有"校验器不能成为单点故障"决策同语义）；
- **只读防线不动**：既有的 `_is_read_only_query` 与 `is_valid` 前置检查保持生效。

## 配置开关（`agentix/config/settings.py`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `TEXT2SQL_BACKEND` | `mysql` | `mysql` \| `wren` |
| `WREN_PROJECT_PATH` | `""` | CLI 准备好的 Wren 项目目录（含 `wren_project.yml` + `target/mdl.json`） |
| `WREN_DATA_SOURCE` | `duckdb` | 直连引擎通道的数据源（wren_langchain 缺失时使用） |
| `WREN_CONNECTION_INFO` | `{}` | 直连引擎通道的连接信息 JSON |
| `WREN_QUERY_LIMIT` | `1000` | Wren 执行默认行数上限 |

## 双通道加载（`agentix/infrastructure/text2sql/wren_adapter.py`）

1. **SDK 通道**（首选）：`wren_langchain.WrenToolkit.from_project(path)`——连接信息由
   项目 `profiles.yml` 解析（参考 WrenAI 上游仓 `sdk/wren-langchain`）；
2. **直连引擎通道**：`wren.engine.WrenEngine(manifest_str, data_source, connection_info)`
   ——manifest 直接读 `target/mdl.json`（base64 注入），连接信息来自 settings；
3. 两通道都不可用 → `WrenNotAvailable` → 节点降级 MySQL。

依赖安装（**暂不安装**——2026-09-22 决定：后续按项目需要再装；集成代码与
开关先行落地，默认 `mysql` 后端零行为变化。届时执行：）

```bash
# 待启用 wren 后端时执行：
./.venv/Scripts/python.exe -m pip install wrenai          # 含预编译 wren-core-py wheel，无需 Rust 工具链
./.venv/Scripts/python.exe -m pip install wren-langchain   # 可选：启用 SDK 通道（profiles.yml 解析）
```

## Wren 项目准备（启用前一次性）

1. 在 WrenAI 项目目录执行 `wren context init` → `wren context build`
   （产物：`wren_project.yml` + `target/mdl.json`，定义模型/字段/指标/关系/RLAC 规则）；
2. `settings.TEXT2SQL_BACKEND=wren`、`settings.WREN_PROJECT_PATH=<项目目录>`；
3. 重启后端即生效；回滚 = 把 `TEXT2SQL_BACKEND` 改回 `mysql`（或删除两个变量）。

## 参考材料

- WrenAI 上游源码仓：https://github.com/WrenAI/WrenAI
  （本地参考副本 `WrenAI-main` 已于 2026-09-22 删除——集成只需 PyPI 包
  `wrenai` / `wren-langchain`，无需本地源码）
- 适配器实现：`agentix/infrastructure/text2sql/wren_adapter.py`
- 节点接线：`agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/text2sql/sql_execution/node.py`
- 测试：`tests/test_wren_text2sql_adapter.py`（12 用例，零 wrenai 依赖可跑）
