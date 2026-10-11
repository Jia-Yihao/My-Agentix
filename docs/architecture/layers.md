# 分层架构与依赖规则

本文档定义 My Agentix 的代码分层、各层职责、**依赖方向规则**及其自动化守卫。
任何 PR 违反规则都会被 CI 拦下，请在编码前通读。

---

## 1. 分层总览

```
┌─────────────────────────────────────────────────────────────┐
│  interfaces/            接口层（协议转换）                     │
│  HTTP 路由、请求/响应模型、中间件                               │
└───────────────────────────┬─────────────────────────────────┘
                            │ 调用（允许）
┌───────────────────────────▼─────────────────────────────────┐
│  application/           应用层（编排与业务规则）                │
│  Agent 图、服务（会话/记忆/观测/任务）                          │
└───────────┬───────────────────────────────┬─────────────────┘
            │ 依赖抽象（允许）               │ 实现注入（运行时）
┌───────────▼───────────────┐   ┌───────────▼─────────────────┐
│  domain/                  │   │  infrastructure/            │
│  领域层（零外部依赖）       │◄──┤  基础设施层（实现 ports）     │
│  错误 / 策略 / 端口 / 状态  │实现│  LLM / 向量库 / 图库 / 持久化 │
└───────────────────────────┘   └─────────────────────────────┘
```

**核心思想**：`application` 依赖 `domain` 定义的**抽象**（Protocol），
`infrastructure` **实现**这些抽象。依赖箭头最终指向 `domain`，而非具体实现。

**终态顶层包归属**（统一执行链 Stage M 后，M6 收口）：

| 顶层包 | 层次归属 | 依赖方向 |
|---|---|---|
| `interfaces/` | 接口层 | → application / domain |
| `application/` | 应用层（用例编排） | → domain / orchestration / policies / tools |
| `orchestration/` | 编排层（AgentLoop、LangGraph 图结构、多智能体策略） | → domain（纯编排，不 import LangGraph 之外的上层） |
| `policies/` | 横切策略执行件（Planner / Verifier / HITL / Guardrails） | → domain |
| `tools/` | 工具层（注册面 / gateway 执行闸 / adapters） | → domain / policies（经 gateway） |
| `agent/` | Context Provider 家（context / memory / events / hooks / skills / evolution） | → domain only |
| `runtime/` | Agent Runtime（步骤 / 预算 / 生命周期 / 控制平面） | → runtime / domain（运行期自包含） |
| `capabilities/` | 能力层（mcp/） | → tools / domain |
| `retrieval/` | 检索域垂直切片（自带 api / domain / infra / usecases） | 域内自治 |
| `infrastructure/` | 基础设施层（实现 ports） | → domain |
| `observability/` `reliability/` `security/` `crawler/` | 横切支撑 | → domain / config |

---

## 2. 各层职责

### 2.1 `interfaces/` — 接口层

**职责**：协议转换与鉴权。把 HTTP 请求翻译成应用层调用，把结果翻译成响应。

| 允许 | 禁止 |
|------|------|
| 参数校验、请求/响应模型定义 | ❌ 业务编排（多步骤流程） |
| 调用 application 服务 | ❌ 直接操作 ORM / 数据库会话 |
| 错误 → HTTP 状态码映射 | ❌ 直接调用 `infrastructure` 具体实现 |
| 中间件（trace / 鉴权 / 日志） | ❌ 包含检索/推理逻辑 |

**代表文件**：`interfaces/http/v1/chat.py`（322 行，仅做「校验 → 调服务 → 组装响应」）、
`interfaces/http/v1/upload.py`、`interfaces/http/models/*.py`。

> 反例（已修复）：历史上 `chat.py` 曾在路由函数内建会话、写 `chat_message` CRUD、
> 编排落库与响应组装（653 行）。这违反规则 1，已下沉到 `ChatService`。

### 2.2 `application/` — 应用层

**职责**：编排领域能力完成用例。持有 LangGraph 图、服务编排、事务边界。

| 允许 | 禁止 |
|------|------|
| 编排 domain 端口 / 策略 | ❌ 直接 import `infrastructure` 具体实现 |
| 定义 Agent 图与节点 | ❌ import `interfaces`（HTTP 契约） |
| 通过注入获取仓储 / 图 / LLM | ❌ 在模块顶层 new 基础设施客户端 |

**代表文件**：`application/services/chat_service.py`、
`application/agents/kg_sub_graph/**`、`application/services/observability.py`。
（图结构 `orchestration/langgraph/lg_builder.py` 归编排层，见 §1「终态顶层包归属」。）

> **注入模式**：`ChatService.__init__` 接收 `SessionRepositoryPort` / `MessageRepositoryPort`
> （`domain.ports` 的 Protocol）与 `graph_getter`（`GraphProviderPort`）。
> 默认实现由 `infrastructure.persistence.repositories` 提供，但类型标注面向抽象。

### 2.3 `domain/` — 领域层

**职责**：表达领域概念与规则。**零外部依赖**（仅 stdlib + typing）。

| 子包 | 内容 |
|------|------|
| `errors.py` | 错误**词汇表**：`ErrorCode` 枚举、`DEGRADABLE`、`HTTP_FATAL`、`AgentError` |
| `status.py` | 响应状态枚举 `RequestStatus`（ok / degraded / error） |
| `tracing.py` | `trace_id` 的 ContextVar 与读写函数 |
| `agent/` `runtime/` `tool/` | Agent / Runtime / Tool 的**领域类型与端口**（M0 拆分，纯契约） |
| `policies/` | 判定**函数**：`is_retryable`、`aretry`、冲突仲裁 |
| `ports/` | 抽象接口（`Protocol`）+ `Payload` TypedDict |

> **词汇表 vs 策略**的分区：`errors.py` 回答「有哪些错、属哪一类」；
> `policies/` 回答「遇到这类错该怎么做」。

### 2.4 `infrastructure/` — 基础设施层

**职责**：实现 `domain.ports` 定义的抽象，封装外部系统。

| 子包 | 内容 |
|------|------|
| `llm/` | LLM 调用加固（`retry.py`，re-export `domain.policies.retry`） |
| `persistence/` | 数据库引擎、CRUD、**仓储实现**、Redis Checkpointer |
| `text2sql/` | Wren 文本转 SQL 适配器 |
| `core/` | 日志、安全、限流 |

> 原 `knowledge/`（向量库 / Reranker / Embedding）已归检索域 `agentix/retrieval/infra/`；
> 原 `tools/`（ToolRegistry / ToolSpec）已随 M1/M6 迁至顶层 `agentix/tools/`。

| 允许 | 禁止 |
|------|------|
| 实现 `domain.ports` 的 Protocol | ❌ import `interfaces` |
| import `domain`（实现契约所需） | ❌ 反向依赖 application 的业务逻辑 |

---

## 3. 依赖规则（强制）

| # | 规则 | 守卫测试 |
|---|------|---------|
| **R1** | `interfaces` 不得包含业务编排与 ORM 操作 | `test_p16_chat_service.py`（`chat.py` 零 CRUD import） |
| **R2** | `application` 不得 import `infrastructure` 具体实现 / `interfaces` | `test_p22_obs_scope.py`（AST 扫描 + 白名单**相等**断言） |
| **R3** | `domain` 不得 import `infrastructure` / `interfaces` / `application` | `test_p17_domain_layer.py`（AST + 子进程导入观测） |
| **R4** | `infrastructure` 不得 import `interfaces` | `test_p17_domain_layer.py::TestInfrastructureInterfacesBan` |

### 本地运行守卫

```bash
pytest tests/test_p17_domain_layer.py tests/test_p22_obs_scope.py -q
```

### 守卫的「棘轮」语义

规则 2 的守卫使用 **集合相等断言**（`==`，非 `issubset`）：

- 出现**新**违规 → 失败（挡住新债）
- 已登记违规**被修复** → 也失败（逼迫收紧白名单常量，防止白名单长期残留）

当前 `application → interfaces` 的白名单为**空集**（零容忍，P2-6 完成后达成）。

---

## 4. 横切关注点的归属

| 关注点 | 归属层 | 说明 |
|--------|--------|------|
| `trace_id` 传播 | `agentix/domain/tracing.py` | 纯 ContextVar，无依赖；`interfaces` 中间件写入，各层读取 |
| 日志 | `agentix/infrastructure/core/logger.py` | `loguru` 封装；`get_logger(service="...")` |
| 错误分类 | `domain`（词汇表 + 策略） | `application` 消费，`interfaces` 映射为 HTTP |
| 指标埋点 | `agentix/application/services/observability.py` | LLM 工厂与 `ChatService` 调用 |
| 配置 | `agentix/config/settings.py` | 全层可读；不得在业务代码里散落 `os.getenv` |

---

## 5. 已知例外：**无**（原唯一例外已清除）

| 文件 | 例外内容 | 状态 |
|------|---------|------|
| ~~`domain/models/__init__.py`~~ | 历史上 re-export 了 `interfaces.http.models` 与 `infrastructure.persistence.db.models`（违反 R3） | ✅ **已删除**（P2-6 T7，2026-09-11）。全仓无生产引用，故连同其在 `domain/__init__.py` 的 PEP 562 惰性导出一并移除；`test_p17` 的「豁免只允许一条」收紧为**零豁免** |

**当前 R1–R4 四条规则零例外。** 守卫 `tests/test_p17_domain_layer.py` 的
`_domain_files()` 已是「无条件产出全部文件」，且新增
`test_no_exemption_remains_in_guard`（AST 判定）防止有人再把豁免加回来。

> **不要新增例外。** 若确需，请在 PR 中说明理由并同步更新守卫白名单——守卫会强制你显式登记。
> 历史例外只有过一条且已被清除，这是刻意维持的记录；新增例外会立刻被上述守卫拦下。

---

## 6. 新增文件的归属决策树

```
你要写的代码是…
├── HTTP 路由 / 请求响应模型 / 中间件？          → interfaces/
├── 编排多个步骤完成一个用例？                   → application/services/
├── 写图结构（lg_builder/lg_states/lg_supervisor）？ → orchestration/langgraph/
├── 写节点与业务 prompt？                        → application/agents/
├── 主循环 / 执行器 / 多智能体策略？              → orchestration/
├── Planner / Verifier / HITL / Guardrails？      → policies/
├── 内置工具 / 执行闸门？                         → tools/{adapters,gateway}/
├── 纯领域概念（错误码 / 状态 / 判定规则）？       → domain/
├── 抽象接口（要被多种实现替换）？                → domain/ports/
├── 对接外部系统（DB / 向量库 / LLM / 缓存）？     → infrastructure/
└── 跨层工具（日志 / 安全 / 限流）？              → infrastructure/core/
```

---

## 7. 相关文档

- [interfaces.md](interfaces.md) — 端口与契约的详细定义
- [extension.md](extension.md) — 在正确的层新增能力
- [adr/0001](adr/0001-layered-architecture-with-ports.md) — 分层决策记录
