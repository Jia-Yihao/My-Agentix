# Runbook：性能与成本采数（P1-3 / P2-4 素材来源）

> 状态：**待环境就绪后执行**。本 runbook 只描述**方法**，产出数字誊进
> README「性能与成本」节。纪律：数字必须实测，标注测量环境与模型快照；
> 测不到就写测不到，不编造。

## 测量对象与数据源（全部已有，零新增指标）

| 指标 | 数据源 | 位置 |
|---|---|---|
| 首 token 时延（TTFT）P50/P95 | Prometheus 直方图 `agent_ttft_seconds` | `agentix/application/services/run_event_stream.py`（首个内容帧记一次/流） |
| 流总时长 | 终帧 `duration_ms` 字段 | 同上 |
| 单请求 token（prompt/completion） | Prometheus 计数器 `llm_tokens_total{model, direction}` | `agentix/observability/metrics.py` |
| 编排域分角色 token | `agent_llm_tokens_total{role}` | 同上 |
| 落库 token/成本明细 | `UsageRecord`（tokens_in/tokens_out/cost/model_name/branch） | `agentix/retrieval/usecases/chat_llm.py` |

## 采数步骤（PowerShell）

### 第 1 步：环境就绪

全栈环境按 `docs/runbooks/eval-recording.md` 第 1–2 步准备（LM Studio +
基础设施容器），并记录模型快照与温度——**数字与模型快照绑定，换模型须重测**。

### 第 2 步：跑一轮全量请求

```powershell
.\.venv\Scripts\python.exe evals/run_eval.py --record
```

102 条 golden 逐条走真实链路，TTFT 直方图与 token 计数器随之累积；
顺路完成 P0-1 基线录制（一鱼两吃）。

### 第 3 步：TTFT P50/P95（Prometheus 查询）

浏览器打开 `http://localhost:9090`（compose 默认 `PROMETHEUS_PORT=9090`），
执行 PromQL：

```promql
histogram_quantile(0.50, sum(rate(agent_ttft_seconds_bucket[10m])) by (le))
histogram_quantile(0.95, sum(rate(agent_ttft_seconds_bucket[10m])) by (le))
```

或直接从轨迹包统计（替代法）：`TRAJECTORY_SINK=jsonl` 落盘后，对每帧
`ttft_ms` 字段做 P50/P95 统计：

```powershell
# 轨迹 JSONL 中抽 ttft_ms 排序取分位（示例：对收集到 csv 后用 Excel/脚本统计）
Select-String -Path evals\results\*.jsonl -Pattern '"ttft_ms":\s*(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value } | Sort-Object {[double]$_}
```

### 第 4 步：单会话平均 token

```promql
sum(increase(llm_tokens_total[30m])) by (direction)
```

除以同窗口请求数（`route_accuracy` 对应的请求数或 `http_requests_total`）得
"单请求平均 token"；按 `branch=chat_stream` 聚合 `UsageRecord` 可得会话级明细。

### 第 5 步：誊写

数字填入 README「性能与成本」节表格，并附一段测量方法（环境、模型快照、
请求集、统计窗口）；结论如实——若首 token P95 超过 SLO 的 5s，写明归因，
不修饰。

## 成本 A/B 对比（P2-4）

优化项建议选 `LG_TOKEN_HISTORY_BUDGET`（token 级历史窗口，默认 0 关）：

1. 固定模型快照 + 温度；
2. flag-off 一轮 `--record`（输出文件记为 a.jsonl）；
3. `.env` 开 `LG_TOKEN_HISTORY_BUDGET=<预算>` 后再一轮（b.jsonl）；
4. 配对显著性：

```powershell
.\.venv\Scripts\python.exe evals/run_eval.py --compare evals\results\a.jsonl evals\results\b.jsonl
```

5. 从两轮 `UsageRecord` / 轨迹分别统计平均 token/会话；
6. 报告四段模板（环境 / 方法 / 数字 / 结论）落 `evals/results/` 目录下
   （文件名 cost_ab_report.md，属执行期产物、仓库中不预先存在）；
   显著性 CI 含 0 时**如实写"不显著（噪声区间）"**——工具已内建该标注，别挑数字。
