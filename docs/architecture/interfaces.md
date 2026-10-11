# 接口与契约定义

本文档汇总 My Agentix 的**抽象接口（端口）**、**数据契约**与**扩展点签名**。
所有接口均以「面向抽象、可替换实现」为设计目标。

---

## 1. 端口抽象（`domain/ports`）

端口是 `application` 依赖、`infrastructure` 实现的**契约边界**。
全部使用 `typing.Protocol` + `@runtime_checkable`，因此**无需继承**即可满足（结构化子类型）。

> 设计约束：`domain/ports` 包**零第三方依赖**（仅 stdlib + typing）。
> 因此外部类型（SQLAlchemy Session、LangGraph 图）一律用 `Any` 表达——
> 领域层只关心「能按契约调用」，不关心具体实现类型。

### 1.1 `SessionRepositoryPort`

```python
@runtime_checkable
class SessionRepositoryPort(Protocol):
    def get(self, db: Any, *, id: str) -> Optional[Any]: ...
    def create(self, db: Any, *, obj_in: Any) -> Any: ...
    def update_activity(self, db: Any, *, session_id: str) -> Any: ...
```

| 方法 | 契约 |
|------|------|
| `get` | 按 id 取会话；不存在返回 `None`（不抛异常） |
| `create` | 创建会话并返回持久化后的实体（含生成的 id） |
| `update_activity` | 刷新活跃时间（用于排序 / 过期回收） |

**默认实现**：`infrastructure/persistence/repositories.py::SessionRepository`

### 1.2 `MessageRepositoryPort`

```python
@runtime_checkable
class MessageRepositoryPort(Protocol):
    def get_latest_by_session(self, db: Any, *, session_id: str) -> Optional[Any]: ...
    def create(self, db: Any, *, obj_in: Any) -> Any: ...
    def get_by_session(self, db: Any, *, session_id: str,
                       skip: int = 0, limit: int = 100) -> List[Any]: ...
    def delete_by_session(self, db: Any, *, session_id: str) -> int: ...
```

| 方法 | 契约 |
|------|------|
| `get_latest_by_session` | 取该会话最后一条消息（`order_index` 递增依据）；无则 `None` |
| `create` | 创建消息并返回持久化实体 |
| `get_by_session` | 分页拉取会话历史 |
| `delete_by_session` | 清空会话消息，返回删除条数 |

**默认实现**：`infrastructure/persistence/repositories.py::MessageRepository`

### 1.3 `GraphProviderPort`

```python
@runtime_checkable
class GraphProviderPort(Protocol):
    def __call__(self) -> Any:
        """返回**当前**的已编译图（不是构造期快照）。"""
```

**为何是「可调用对象」而非「图实例」**：启动期会因 Redis Checkpointer 初始化而
**重新编译并覆盖**主图。若注入图实例，会固化启动前的旧图（值绑定陷阱）。
`ChatService` 每次请求调用 `graph_getter()` 取当前图。

**默认实现**：`application/services/chat_service.py` 中的惰性 getter。

### 1.4 Agent Core 2.0 七端口（规范位 `agentix/domain/agent/ports.py`；旧路径 domain/ports/agent_core.py 已于 M6 删除——M0 拆分）

Agent = Model + **Harness**。Harness 五职责 ↔ 七端口对照（见
[迁移规划](../migration/Agent_Core_2.0_规划.md) §0）：

| Harness 职责 | 端口 | 契约要点 |
|--------------|------|----------|
| Context | `ContextPort.build` | `TaskSpec` + 轨迹 → `ContextSnapshot`（确定性、可截断标记） |
| 规划 | `PlannerPort.plan` | → `Plan`（不可变步骤；环检测由实现保证） |
| 工具 | `ToolSelectorPort.select` | 按 `max_risk_level` 过滤后的 schema 列表 |
| 验证 | `VerifierPort.verify` | → `VerificationResult`；Proposer–Reviewer 隔离 |
| 记忆 | `MemoryPort.recall/remember` | scope 隔离；写入路径做 PII 脱敏 |
| 事件 | `EventPort.publish/wait` | 外部事件唤醒（Long-running） |
| 纠偏 | `RollbackPort.snapshot/restore` | 检查点（Memory ≠ Checkpoint） |

类型载体见 `domain/agent/types.py`（M0 拆分规范位；旧路径 domain/agent_types.py 已随 M6 删除）：`TaskSpec` / `Plan` / `Observation` /
`VerificationResult` / `MemoryItem` / `ContextSnapshot` / `FailureLayer`。
全部 dataclass 字段为纯数据、可序列化；**零第三方依赖**。

**默认实现（Phase 2+，M2/M3 迁移后）**：Context / Memory / Event → `agentix/agent/*`；
Planner / Verifier / HITL → `agentix/policies/*`；StepExecutor → `agentix/orchestration/executor.py`
（Fake 实现见 `tests/test_agent_*`）。

---

## 2. 数据契约（`domain/ports/payloads.py`）

创建型载体使用 **stdlib `TypedDict`**（非 pydantic），用于在 `application` 与
`infrastructure` 之间传递写库数据，避免两者反向依赖 `interfaces` 的 HTTP 契约。

| TypedDict | 镜像的 HTTP 契约 | 用途 |
|-----------|-----------------|------|
| `SessionCreatePayload` | `ChatSessionCreate` | 创建会话 |
| `MessageCreatePayload` | `ChatMessageCreate` | 创建消息 |
| `SessionSnapshotPayload` | `ChatSessionSnapshotCreate` | 会话快照 |
| `ConversationSnapshotPayload` | `ConversationHistorySnapshotCreate` | 对话历史快照 |

**设计取舍**：

- **不做运行时校验**：`TypedDict` 是类型标注；错误键在 SQLAlchemy 模型构造处 **fail-fast** 暴露（而非 pydantic 的静默忽略）
- **可选字段用 `Optional[...]` + 必填键**：Python 3.10 无 `NotRequired`，且 `ports` 包禁第三方依赖（不能用 `typing_extensions`）——显式优于隐式缺省
- **边界转换**：HTTP 端点在 `interfaces` 层 `model_dump()` 后传入，语义等价

---

## 3. 错误与状态契约

### 3.1 `ErrorCode`（`domain/errors.py`）

按**四层故障分类**组织：

| 层 | 错误码 | 处置 |
|----|--------|------|
| **API 层**（上游模型） | `LLM_TIMEOUT` / `LLM_RATE_LIMIT` / `LLM_UNAVAILABLE` | 可降级 / 映射 504 / 429 / 503 |
| **工具层** | `TOOL_NOT_FOUND` / `TOOL_EXEC_FAILED` / `RETRIEVAL_FAILED` / `GRAPH_UNAVAILABLE` | 可降级（部分成功语义） |
| **上下文层** | `CONTEXT_OVERFLOW` | 可降级 |
| **控制流层** | `MAX_STEPS_EXCEEDED` / `AGENT_TIMEOUT` | 500 / 504 |
| **业务（非错误）** | `NO_EVIDENCE` / `OUT_OF_SCOPE` | 正常应答（无证据 / 越界拒答） |

```python
class AgentError(Exception):
    def __init__(self, code: ErrorCode, message: str, *,
                 detail: str | None = None, retryable: bool = False) -> None: ...
```

**契约**：
- `DEGRADABLE`：可降级为「有礼貌的拒答」，响应 `status="degraded"`
- `HTTP_FATAL`：必须暴露为 5xx / 4xx，**不得**返回 200
- `detail`：内部细节，仅 `DEBUG` 下返回给客户端

### 3.2 `RequestStatus`（`domain/status.py`）

| 值 | 含义 | 前端表现 |
|----|------|---------|
| `ok` | 正常应答 | 正常样式 |
| `degraded` | 系统降级（部分能力不可用） | 警告样式 + 重试提示 |
| `error` | 失败 | 错误样式 |

> **合规要求**：法律场景下「系统故障」与「知识库无记载」**必须可区分**，
> 否则用户可能把降级兜底当成法律答复。

---

## 4. 扩展点契约

### 4.1 工具（`agentix/tools/definitions.py`；原 infrastructure/tools → capabilities/tools，M6 后旧路径已删）

```python
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str          # 必须包含「何时用 / 何时不能用」反例
    parameters: Dict[str, Any]  # JSON Schema（通常来自 pydantic model_json_schema()）

class ToolRegistry:
    def register(self, spec: ToolSpec, factory: Callable[[], Any]) -> None: ...
    def get_spec(self, name: str) -> ToolSpec: ...
    def get_specs(self) -> Iterable[ToolSpec]: ...
    def has_tool(self, name: str) -> bool: ...
    def tool_names(self) -> Iterable[str]: ...
    def create(self, name: str) -> Any: ...
```

**契约**：
- `description` 必须写明**反例**（何时不该用）——这是降低工具误选的首要手段
- `factory` 必须是**懒加载**（调用时才构造），保证注册期无副作用
- 注册幂等（重复注册同名工具覆盖）

### 4.2 Provider（LLM / Embedding / Reranker）

LLM 通过 `LLMProfile` 描述角色化配置：

```python
@dataclass(frozen=True)
class LLMProfile:
    role: Role                      # router | planner | tool | summarizer | general | hallucination
    model: str
    temperature: float
    timeout: float = 30.0
    max_retries: int = 2
    max_tokens: Optional[int] = None
    tags: List[str] = field(default_factory=list)
```

详见 [extension.md §新增 Provider](extension.md#新增-provider)。

---

## 5. HTTP 接口契约（对外公共 API）

以下路由属于**兼容性承诺范围**（见 [VERSIONING.md §4](../VERSIONING.md)）：

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/chat/` | 非流式问答 |
| `POST` | `/api/v1/chat/stream` | SSE 流式问答（`metadata` / `message` / `tool_start` / `tool_end` / `done` / `error` 事件） |
| `POST` | `/api/v1/upload/` | 文件上传 |
| `GET` | `/api/v1/sessions/{id}/messages` | 会话历史 |
| `DELETE` | `/api/v1/sessions/{id}/messages` | 清空会话 |
| `POST` | `/api/v1/events` | 外部事件 webhook（`202` 受理 + `event_id`；同 `event_key` 幂等去重；空/控制字符 `event_type` → `422`；payload JSON > 8192 字符 → `413`；喂 `WAITING_EXTERNAL_EVENT` 等待） |
| `GET` | `/health` | 健康检查（Redis / Neo4j / Milvus） |
| `GET` | `/ops/*` | 运维指标与恢复端点 |

响应契约（`ChatResponse`）：

```python
class ChatResponse(BaseModel):
    answer: str
    status: Literal["ok", "degraded", "no_evidence", "out_of_scope"] = "ok"
    error_code: str | None = None
    sources: list[dict] = []
    trace_id: str | None = None
```

---

## 6. 相关文档

- [layers.md](layers.md) — 接口应放在哪一层
- [extension.md](extension.md) — 实现这些接口的完整步骤
- [configuration.md](configuration.md) — 配置驱动的组件装配
