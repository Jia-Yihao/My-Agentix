# Runbook：LLM 降级链演练（P1-1，跨 base_url fallback）

> 状态：**待环境就绪后执行**。前置：T6 适配层已合入（`LLM_FALLBACK_MODEL_SPECS`）。
> **诚实边界（重要）**：fallback 只接线在**检索域 `query_knowledge_base` 工具链路**
> （`agentix/retrieval/usecases/chat_agent_tools.py`）；编排域（llm_factory /
> Agent Core 主循环）**不消费** fallback。因此演练必须走**带知识库检索的请求**
> 才能触发降级——纯对话/任务路由类请求不会降级。

## 前置条件

- 主端点（LM Studio 或云 API）正常服务；
- 备用端点可用（示例：LM Studio 加载第二模型，监听 `http://localhost:1234/v1`）；
- 备用端点的密钥已写入**独立环境变量**（不与主 `LLM_API_KEY` 复用——密钥隔离纪律）。

## 演练步骤（PowerShell）

### 第 1 步：配置降级链

在 `.env` 中设置（三元组格式 `model|base_url|api_key_env`）：

```powershell
# .env 追加：
# LLM_FALLBACK_MODEL_SPECS=qwen/qwen3.5-9b|http://localhost:1234/v1|LLM_FALLBACK_API_KEY
# LLM_FALLBACK_API_KEY=lm-studio          # LM Studio 校验任意非空值即可
Add-Content .env "LLM_FALLBACK_MODEL_SPECS=qwen/qwen3.5-9b|http://localhost:1234/v1|LLM_FALLBACK_API_KEY"
Add-Content .env "LLM_FALLBACK_API_KEY=lm-studio"
```

### 第 2 步：启动后端

```powershell
.\.venv\Scripts\python.exe -m uvicorn agentix.main:application --port 8000
```

### 第 3 步：记录降级前基线

记录 Prometheus 中 `gateway_fallback_total` 当前值（浏览器打开
`http://localhost:9090`）：

```promql
gateway_fallback_total
```

### 第 4 步：停掉主端点

停掉主 LLM 服务（LM Studio Stop / 云端点改错地址），保持备用端点存活。

### 第 5 步：发一次带知识库检索的请求

```powershell
$body = @{ message = "示例文档异常的构成要件是什么？"; session_id = "drill-p11" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/chat" -Method Post -Body $body -ContentType "application/json"
```

> 必须是触发 `query_knowledge_base` 工具的问题（知识库问答类）；
> 纯寒暄/任务请求不落检索域，不触发降级。

### 第 6 步：断言降级生效

1. **响应非主模型**：响应应来自备用模型（内容质量下降但服务不中断——这正是设计意图）；
2. **降级打点增长**：

```promql
gateway_fallback_total
```

   相对第 3 步基线应 +1（label：`primary_model` / `fallback_model`，已在
   p100 白名单内，零新增标签）；
3. **日志留痕**：后端日志出现 `model fallback succeeded on attempt`。

### 第 7 步：恢复主端点

重启主 LLM 服务，确认后续请求恢复主模型（`gateway_fallback_total` 不再增长）。

### 第 8 步：回滚配置

演练后清空两键即回滚（默认空 = 与基线逐位等价）：

```powershell
# .env 中将 LLM_FALLBACK_MODEL_SPECS 置空或删除该行
```

## 执行留痕

执行完成后在本节下方追加记录（日期、主/备模型快照、演练结果、
`gateway_fallback_total` 增量），并同步 `CHANGELOG.md`。
