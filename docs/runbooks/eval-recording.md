# Runbook：录制评估基线并武装质量门禁（P0-1）

> 状态：**待环境就绪后执行**（需 LM Studio + Docker 全栈可用）。
> 本 runbook 是 `evals/README.md` §9「武装顺序」的分步 PowerShell 版本。
> 纪律：**拒绝手工伪造 baseline**——所有数字必须来自真实录制。

## 前置条件

- LM Studio 已加载主模型（或任意 OpenAI 兼容端点可用）；
- `.env` 已配置 `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY`；
- **模型快照已记录在案**（云 API 钉快照版本号，见 `.env.example` 中
  `LLM_MODEL` 上方注释；模型不同则基线不可比）；
- **温度设置已确认**：温度 0 下 Pass^k 无区分度（见 `evals/README.md` §4.5 口径陷阱）。

## 八步录制流程（PowerShell）

### 第 1 步：启动基础设施

```powershell
docker compose up -d postgres redis neo4j mysql milvus kb_ingest
```

### 第 2 步：确认环境变量与模型快照

```powershell
# 记录当前模型快照与温度（誊进录制记录，呼应模型快照固定纪律）
Get-Content .env | Select-String "LLM_MODEL|LLM_BASE_URL|TEMPERATURE"
```

### 第 3 步：在线录制 102 条 golden 的预测

```powershell
.\.venv\Scripts\python.exe evals/run_eval.py --record
```

逐条提问真实端点，落盘 `evals/baseline_predictions.jsonl`，并就地打分写
`evals/results/*.summary.json`。

### 第 4 步：提交基线预测文件

```powershell
git add evals/baseline_predictions.jsonl
git commit -m "evals: record baseline predictions (P0-1)"
```

### 第 5 步：离线复打分（与 CI 同款路径）

```powershell
.\.venv\Scripts\python.exe evals/run_eval.py --offline
```

### 第 6 步：五指标逐个首跑建档

对五个门禁指标逐个执行（首次为 NOT-ENFORCED 并**写回**
`evals/baselines/quality.json` 建档）。注意门禁脚本在 `evals/` 根
（不是 `evals/baselines/` 下）：

```powershell
.\.venv\Scripts\python.exe evals/quality_gate.py --baseline-file evals/baselines/quality.json --metric route_accuracy --current <第5步summary中的值>
.\.venv\Scripts\python.exe evals/quality_gate.py --baseline-file evals/baselines/quality.json --metric citation_hit_rate_applicable --current <v>
.\.venv\Scripts\python.exe evals/quality_gate.py --baseline-file evals/baselines/quality.json --metric citation_correctness_applicable --current <v>
.\.venv\Scripts\python.exe evals/quality_gate.py --baseline-file evals/baselines/quality.json --metric must_contain_applicable --current <v>
.\.venv\Scripts\python.exe evals/quality_gate.py --baseline-file evals/baselines/quality.json --metric reject_accuracy_applicable --current <v>
```

指标名以 `.github/workflows/ci.yml` 中 `eval-offline` job 的 `METRICS` 元组为准
（守卫测试 `tests/test_p90_eval_report_keys.py` 与 `tests/test_ci_gate_contract.py`
钉死了这五个键）。

### 第 7 步：手动提交 quality.json（关键）

```powershell
git add evals/baselines/quality.json
git commit -m "evals: enforce quality gate baselines (P0-1)"
```

> **为什么必须本地提交**：CI runner 上的建档写回不落盘、下次运行即丢，
> 门禁会永远停留在 NOT-ENFORCED。`evals/README.md` §9 已明文此机制。

### 第 8 步：推送并验证 CI 门禁升级

```powershell
git push
```

推送后在 GitHub Actions 确认 `eval-offline` job 从"警告 + 跳过"升级为
"真跑分 + 逐指标门禁"；此后指标相对回退超过 2% 时该 job exit 1 阻断合并。

## 执行留痕

执行完成后，在本节下方追加执行记录（日期、模型快照、五指标数值、
录制环境差异），并同步 `CHANGELOG.md`。
