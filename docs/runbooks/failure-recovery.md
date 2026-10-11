# Failure Recovery Runbook——失败恢复链与现场演示

> 对应 Agent 评估建议 P0 清单第 5 项「Failure Recovery：必须有真实失败场景」。
> 本文档描述系统内**真实存在**的四层失败恢复机制，并给出可复现的演示命令。
> 所有路径与命令均可在仓库根目录直接执行；除 Run API 演示外无需任何外部服务。

## 四层失败恢复机制

| 层 | 机制 | 实现位置 | 恢复语义 |
|---|------|---------|---------|
| L1 | **工具执行治理链** | `agentix/tools/gateway/executor.py` | 单工具超时/异常 → 按 `ToolSpec.retryable` 退避重试（默认 ≤2 次）→ 失败落 `ToolResult(success=False)`，**永不抛异常打断 Agent 主流程** |
| L2 | **模型 Fallback 链** | `agentix/infrastructure/llm/model_gateway.py` | 主模型失败 → 按分档路由自动切换兜底模型 → 成功即打点 `gateway_fallback_total{primary_model, fallback_model}` |
| L3 | **检索多级兜底** | 知识库子图（`create_kb_multi_tool_workflow`） | 本地检索无结果/低分 → 外部搜索补充（hybrid 模式）→ 仍无结果走拒答（`NO_HIT` 语义） |
| L4 | **运行级控制面** | `agentix/runtime/control_plane.py` + `agentix/interfaces/http/v1/runs.py` | 运行超时（`asyncio.wait_for` 统一施加）→ 终态 FAILED；用户取消 → 终态 CANCELLED；执行器异常 → FAILED + error 落账；**终态幂等**（失败/取消后不会被后续结果覆盖） |

## 演示一：任务级 Run API 的失败语义（离线可跑）

启动后端后（`python -m uvicorn agentix.main:application --port 8000`），按序执行
（Run API 为严格 Bearer JWT：`<token>` 换成有效 access token，不带票 ⇒ 401）：

```bash
# ① 未知执行器 → 400 UNKNOWN_AGENT（fail-fast，不产生悬挂运行）
curl -s -X POST http://localhost:8000/api/v1/agents/runs \
  -H "Content-Type: application/json" -H "Authorization: Bearer <token>" \
  -d '{"agent": "nope", "task": "x"}'
# → {"error_code": "UNKNOWN_AGENT", ...}

# ② 超时 → 运行落 FAILED（error=timeout after 0.1s），不留僵尸任务
curl -s -X POST http://localhost:8000/api/v1/agents/runs \
  -H "Content-Type: application/json" -H "Authorization: Bearer <token>" \
  -d '{"agent": "probe", "task": "slow", "options": {"delay_s": 3}, "timeout_s": 0.1}'
# 稍后查询：
curl -s http://localhost:8000/api/v1/agents/runs/<run_id> -H "Authorization: Bearer <token>"
# → {"state": "failed", "error": "timeout after 0.1s"}

# ③ 运行中取消 → CANCELLED 终态 + 底层 Task 真正被打断；重复取消幂等
curl -s -X POST http://localhost:8000/api/v1/agents/runs \
  -H "Content-Type: application/json" -H "Authorization: Bearer <token>" \
  -d '{"agent": "probe", "task": "long", "options": {"delay_s": 30}}'
curl -s -X POST http://localhost:8000/api/v1/agents/runs/<run_id>/cancel -H "Authorization: Bearer <token>"
# → {"state": "cancelled", "cancel_reason": "user_cancelled"}

# ④ 执行器异常 → FAILED + 错误信息落账（error 字段），可追溯
# ⑤ 真实 Agent 图执行器：{"agent": "chat", "task": "<问题>"}（走 ChatService.ask 主链路）
```

对应语义测试：`tests/test_agent_run_api.py`（9 用例，含"取消后终态不被执行器结果覆盖"）。

## 演示二：工具治理与 Fallback 链（测试即证据）

```bash
# 工具链：超时/重试/风险闸门/权限闸门/失败落 ToolResult（不抛异常）
./.venv/Scripts/python.exe -m pytest tests/test_p32_tool_guards.py tests/test_r1_tool_risk_levels.py -q
./.venv/Scripts/python.exe -m pytest tests/test_mcp_client.py -q           # 远端工具失败 → ToolResult(success=False)

# 模型 Fallback 链：主模型失败自动兜底 + fallback 指标
./.venv/Scripts/python.exe -m pytest tests/test_r3_model_gateway.py -q

# Run API 失败语义
./.venv/Scripts/python.exe -m pytest tests/test_agent_run_api.py -q
```

## 前端展示：执行轨迹中的异常步骤

聊天流式过程中，`error` 事件会作为 `error` 类步骤进入消息下方的
**「执行轨迹」折叠面板**（红色标注，附与上一步的时间间隔）——
用户无需查日志即可看到"哪一步、慢在哪、错在哪"。

实现：`web/src/modules/knowledge/utils/traceTimeline.ts`（`appendTraceEvent`）。

## 告警接线（生产环境）

- `My AgentixGatewayFallbackCascade`：15 分钟内兜底次数 > 5 → warning
  （主模型持续不可用的信号，见 `docker/monitoring/alert_rules.yml`）
- `My AgentixGroundingLowScoreSpike`：30 分钟内 grounding 低分 > 10 → warning

详见 [SLO 目录](slo.md) 与 [发布与回滚](release-and-rollback.md)。
