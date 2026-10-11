# 测试分层策略（P1 可测试收口）

> 目标读者：贡献者与评审者。本文回答"一个改动应该在哪一层补测试、各层怎么跑、CI 怎么卡"。
> 配套：`docs/runbooks/release-and-rollback.md`（部署验收）、`evals/README.md`（评估集与指标口径）。

## 1. 五层测试体系

| 层 | 覆盖物 | 依赖 | 运行方式 | CI |
|---|---|---|---|---|
| L1 单元 | domain/application 纯逻辑、策略、服务（fake 依赖注入） | 无外部服务 | `pytest tests -m "not integration"` | ✅ test 作业（py3.10 矩阵） |
| L2 API 集成 | TestClient 打到真实 FastAPI 应用、SSE 流式（`test_p37_http_endpoints.py`、`test_chat_api.py`） | fake LLM/存储 | 同上（随 L1 一起跑） | ✅ |
| L3 外部集成 | 需真实中间件/LLM 的用例（标记 `@pytest.mark.integration`） | Docker/LLM | `pytest -m integration`（本地带环境跑） | ⛔ CI 显式排除 |
| L4 架构守卫 | 分层依赖（p17）、观测口径（p22）、selfcheck（p35）、文档巡检（p41）等"防倒退"用例 | 无 | `pytest tests/test_p17_domain_layer.py tests/test_p22_obs_scope.py` | ✅ 独立作业（失败即阻断） |
| L5 端到端 | `scripts/e2e/e2e_phase4.mjs`（真实浏览器 + demo 后端）、`perf-baseline.yml`（p95 性能门禁） | 本机联调环境 | `node scripts/e2e/e2e_phase4.mjs` | ⛔ 手动/夜间 |

## 2. Marker 约定（`--strict-markers` 已启用，未注册 marker 会报错）

- `integration`：任何需要外部服务（真实 LLM、Milvus、Redis、Neo4j）的用例必须打标——CI 用 `-m "not integration"` 排除，漏打标会打红 CI。
- `slow`：单跑耗时 > 10s 的用例打标，便于本地快速回路 `pytest -m "not slow"`。
- 注册位置：`pyproject.toml` `[tool.pytest.ini_options].markers`。新增 marker 先注册再用。

## 3. 改动 → 测试对照表

| 改动类型 | 必须补的测试 |
|---|---|
| domain 策略/纯函数 | L1（无 IO，覆盖边界与 fail-open 语义） |
| 新 HTTP 端点 | L2（TestClient：正常路径 + 错误折叠 + trace_id 头） |
| 新配置项 | `.env.example` + selfcheck 合法省略清单同步（p35 会卡）；开关默认值必须"零行为变化" |
| 新跨层 import | 先跑 L4 守卫自查（p17 分层规则） |
| Agent 图结构变更 | L1/L2 全绿后，重录轨迹基线（见 §4） |
| 依赖新增 | 先过 `pip check` + ruff，再跑全量 |

## 4. 评估与回归（量化"行为没变"）

三件套职责边界（详细口径见 `evals/README.md`）：

1. **golden 离线评估**：`python evals/run_eval.py --offline`——对已录制的 `baseline_predictions.jsonl` 打确定性指标（路由准确率/引用命中率/拒答正确率/引用正确性），零外部服务。
2. **质量门禁**：`python evals/quality_gate.py --baseline-file evals/baselines/quality.json --metric <k> --current <v>`——"不低于基线 -2%"语义；基线缺失或指标首现时**只建档不阻断**，提交 `evals/baselines/quality.json` 后自动升级为真阻断。CI 的 `eval-offline` 作业已按此接线（未提交预测基线时优雅跳过）。
3. **轨迹回归**：`evals/trajectory_regression.py`——对比 Agent 执行轨迹（step 序列/工具选择）与 `evals/baselines/` 基线包。**重录时机**：LangGraph 图结构变更、工具注册表变化、prompt 结构性调整后；基线导出命令见脚本 docstring。

**录制预测基线**（需联调环境：LM Studio + Docker 中间件）：`python evals/run_eval.py --record` → 提交 `evals/baseline_predictions.jsonl` → 本地播种并提交 `evals/baselines/quality.json`（五指标；**CI runner 的建档写回不持久**）→ 此后 CI `eval-offline` 自动真阻断。轨迹包导出到 `evals/baselines/traces/` 子目录（勿与 `quality.json` 同级）。

## 5. 本地全量回归口径

本地只维护一个 **Python 3.12.13 标准 venv `.venv`**。在 Windows 上从仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

macOS/Linux 对应命令：

```bash
./.venv/bin/python -m pytest tests -q -p no:cacheprovider
```

CI 的 Python 版本由各 workflow 独立配置；本地单环境不改变 CI 覆盖口径。
全量测试不要并行运行：少数用例写**仓库相对路径**（轨迹 JSONL 轮转、上传限额落盘），
并行执行可能互相踩文件产生**假红**。

> ⚠️ **`--basetemp` 一定要用"本次新建"的空目录，且指向仓库之外。**
> 复用被中断过的 basetemp 会踩到两类问题：① 沙箱/守卫对仓库内 tmp 清理有干扰
> （2026-09 实测：复用一次被 kill 的 basetemp，Python 3.12 环境报 **100 个 teardown error**，
> 而单文件跑同一批用例时 12 条全部通过——**是验证方法的问题，不是产品问题**）；
> ② p17 守卫探针用例依赖临时目录不在仓库内。

**参考基线**：以 **README §11 的实测数字**为准（单一真源）。

⚠️ 本文档**刻意不复述**那个数字——此前这里写 1380 例通过 / 7 跳过，
而同一时点 README 写 1557、实测是 1784：**同一个事实写在两处，就一定会有一处先腐烂**。
（这也是本节的教训本身：不是"把数字改对"，而是"只留一处"。）

4 个 skip 是**设计内**的（集成 / 外部依赖用例），不是"跳过失败"：
2 条需 `LEGELBOT_RUN_INTEGRATION_TESTS=1`，2 条 LightRAG 端到端需 `LEGELBOT_RUN_LIGHTRAG_E2E=1`。
