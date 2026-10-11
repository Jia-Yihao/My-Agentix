# SLO 目录（P2 可观测）

> 服务等级目标与错误预算语义。SLI 全部映射到既有 Prometheus 指标（`/metrics`），
> 不引入新埋点；告警规则见 `docker/monitoring/alert_rules.yml`。
> 阶段定位：0.y.z 产品期先**建档与观测**，错误预算耗尽→冻结功能发布的硬流程在 1.0 后启用。

## 1. SLO 目录

| # | SLI | 指标（PromQL 口径） | SLO | 测量窗口 |
|---|---|---|---|---|
| S1 | 服务可用性 | `1 - (sum(rate(request_total{status=~"5.."}[5m])) / sum(rate(request_total[5m])))` | ≥ 99.5% | 滚动 30 天 |
| S2 | 流式首 token 延迟 | 检索域 SSE 首事件时间（观测：`retrieval_latency_seconds` 分位近似） | P95 < 3s | 滚动 7 天 |
| S3 | 拒答正确率 | `reject_accuracy`（离线评估，evals/run_eval.py） | ≥ 95% | 每次评估 |
| S4 | 引用正确性 | `citation_correctness`（离线评估） | ≥ 90% | 每次评估 |
| S5 | 答案证据锚定 | `grounding_low_score_total` 低分率（`increase[30m]` / `increase(request_total[30m])`） | 低分率 < 5% | 滚动 30 天 |

> S1 的 `request_total` 标签口径以 `agentix/observability/metrics.py` 实际标签为准；S2 在
> 首事件计时埋点补齐前先用 retrieval 端到端延迟分位数近似（偏差记录在案）。

## 2. 错误预算

- 预算 = (1 − SLO) × 测量窗口内的总请求量。
- 预算消耗 > 100% → **冻结功能发布**，只允许可靠性修复；恢复条件：预算重新为正。
- 消耗速率告警（burn rate）：
  - 1h 窗口消耗速率 > 14.4×（等效 2% 可用性损失/天）→ P1 告警
  - 6h 窗口消耗速率 > 6× → P2 告警
- 0.y.z 阶段：预算报表**人工周检**（见 §4），不阻断发布。

## 3. 分级路由

| 级别 | 触达 | 响应目标 | 对应告警（现有） |
|---|---|---|---|
| P1 | 电话 | 立即响应 | BackendDown / HighErrorRate / CircuitOpen |
| P2 | IM 群 | 工作时间 30min | HighP95Latency / RateLimitStorm / ComponentDegraded / GroundingLowScoreSpike / GatewayFallbackCascade |
| P3 | 周报 | 下个工作日 | 趋势类（成本、预算消耗） |

告警路由（severity → 接收端）配置在 `docker/monitoring/alertmanager.yml`。

## 4. 周检清单

1. Grafana 服务健康板：S1/S2 实际值 vs SLO；
2. `grounding_low_score_total` / `gateway_fallback_total` 周增量与突增事件回看；
3. 离线评估三指标（S3/S4/S5 口径）与 `evals/baselines/quality.json` 基线对比；
4. 错误预算消耗表更新，>50% 时在周会升级讨论。
