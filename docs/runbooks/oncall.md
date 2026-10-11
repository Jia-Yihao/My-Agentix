# Oncall Runbook —— 告警响应与降级排查树

> 适用：docker/monitoring/alert_rules.yml 全部 8 条告警 ｜ 更新：2026-09-27（补 Grounding 低分 / Gateway fallback 两条）

## 响应总原则

1. 先看 `/readiness` 与 `/metrics`（免鉴权），再进容器日志；
2. 降级 ≠ 宕机：多数降级路径下**核心问答仍可用**（分级降级设计），优先恢复依赖而非重启应用；
3. 每次处置按 `docs/runbooks/postmortems.md` 留档。

## 告警 → 动作映射

### My AgentixBackendDown（critical）
- 现象：Prometheus 抓取 `backend:8000/metrics` 失败 1 分钟
- 排查：`docker ps`（容器退出？）→ `docker compose logs backend --tail 100` → 启动期失败多半是 selfcheck/secret_key fail-fast
- 恢复：修复配置后 `docker compose up -d backend`；非配置问题抓 core dump 截图留档再重启

### My AgentixHighErrorRate（critical，5xx > 1% 持续 5min）
- 排查：`http_requests_total{status=~"5.."}` 按 endpoint 维度拆 → 定位异常路由 → 日志搜 trace_id（响应体会回显 trace_id）
- 常见根因：LLM 上游超限/断连、Redis 分片故障、DB 连接池耗尽

### My AgentixHighP95Latency（warning，P95 > 10s 持续 5min）
- 排查顺序：`llm_request_duration`（上游慢？）→ `rag_retrieval_duration`（索引劣化？）→ `llm_concurrency`（并发排队打满？说明 LLM_MAX_CONCURRENCY 偏小或上游变慢）
- 恢复：上游慢 → 观察熔断器是否打开（见下）；并发打满 → 临时调大 `LLM_MAX_CONCURRENCY` 或扩容上游配额

### My AgentixRateLimitStorm（warning，>5 次/秒）
- 排查：`rate_limit_triggered_total{scope}` —— `global`=全局限流中间件触发，`chat`=检索域聊天路由
- 定位滥用 IP：日志按 `X-Forwarded-For` 首跳聚合
- 恢复：单点滥用 → 边缘封禁；正常高峰 → 调 `RATE_LIMIT_GLOBAL_RPM/BURST`

### My AgentixComponentDegraded（warning，新降级事件）
- `component="checkpointer"` → 会话不持久化（MemorySaver 兜底）。排查树：
  1. `_degraded_reason`（日志搜 `checkpointer`）：
     - `langgraph-checkpoint-redis not installed` → 镜像依赖缺失，重建镜像
     - `CHECKPOINTER_TYPE=memory (explicit)` → 有人改了配置，确认是否有意
     - 异常类名 → 看 `REDIS_CHECKPOINT_URL`（db2 分片）连通性
  2. 核心问答不受影响，但**重启即丢进行中会话**——尽快恢复
- `component="chat"` → 检索通道部分失败（general_handler 标记）。查 `rag_retrieval_*` 分项

### My AgentixCircuitOpen（critical，熔断器 OPEN）
- 现象：`circuit_state{name}=2`，下游连续失败达阈值，请求快速拒绝走降级响应
- 排查：熔断器 name 指向的下游（LLM/embedding）→ 直接探活上游 API
- 恢复：上游恢复后半开探测成功自动 CLOSED；**不要手动 reset 生产熔断器**（掩盖真实故障）

### My AgentixGroundingLowScoreSpike（warning，30min 内 grounding 低分答案 > 10 次）
- 现象：`grounding_low_score_total` 突增——拒答锚点头部依据不足的信号
- 排查：核对 kb 文档/权限域变化、grounding 阈值配置漂移、上游模型漂移；查观测面板 grounding 低分 trace
- 恢复：锚定阈值配置回滚 → 补语料/修权限 → 观察 30min 曲线回落

### My AgentixGatewayFallbackCascade（warning，15min 内 fallback > 5 次）
- 现象：主模型连续失败、二道兜底接管——成功率假象，主模型可能不可用
- 排查：日志与 metrics 中 `primary_model` / `fallback_model` 分层对比 → 主模型 API key/配额/网络
- 恢复：主模型恢复即自动归位；持续则按预案切换主模型配置（`docker compose up -d backend`）

## 升级路径

15 分钟未定位 → 冻结变更 + 导出 `docker compose logs` 全量 + `curl /metrics` 快照留档。
