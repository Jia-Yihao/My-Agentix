# 法律领域评估集与回归（P2-1）

> 依据 `My Agentix_架构优化方案.md` P2-1（第 1159–1180 行）建立。
> 目标：**先定义成功，再谈优化**。为 P0/P1 改动提供确定性的回归门槛。

---

## 1. 目录用途

```
evals/
├── legal_golden.jsonl            # 黄金评估集（ground truth），≥20 条
├── baseline_predictions.jsonl    # 系统实际输出（录制产物，默认不入库）
├── run_eval.py                   # 批量跑 + 打分 + 输出 CSV / summary.json（judge / --compare / --sensitivity）
├── quality_gate.py               # 质量/性能门禁（对基线 -2% CI 阻断；缺基线 NOT-ENFORCED 首跑建档）
├── claim_check.py                # 声称一致性（答案声称 vs 实际来源，只标记不判分，Phase 31）
├── feedback_ingest.py            # F7：生产失败样本 → 候选 golden 行（人工评审后合入）
├── trajectory_regression.py      # F5：轨迹前缀回归（基线轨迹 vs 候选轨迹逐位置比对）
├── evolution_run.py              # 进化闭环离线提案（17 门 + --proposal-log 三状态落盘）
├── hybrid_eval.py                # R-P2-5 混合检索归因报告（Recall@5/MRR × {BM25,Dense,RRF,+Rerank} 四列，只增报告键不进门禁）
├── ablations/                    # 消融实验（run_ablation / aggregate / variants.json）
├── baselines/                    # 门禁基线落盘（quality.json；traces/ 子目录随 --record 约定）
├── __init__.py                   # 包标记（pytest 走 evals.* 导入路径）
├── results/                      # 每次跑分产物：<timestamp>.csv / <timestamp>.summary.json
└── README.md                     # 本文件
```

* `legal_golden.jsonl`：**唯一事实来源**，人工维护，禁止由模型自动生成后直接合入。
* `baseline_predictions.jsonl`：由 `--record`（在线）在该机器上的真实输出落盘，
  用于离线环境复现打分。
* `results/`：跑分产物，可随时重新生成，不参与版本控制（见仓库 `.gitignore` 策略）。
* `feedback_ingest.py` / `trajectory_regression.py`：**只读 golden / 只读轨迹**，
  从不修改任何既有基线文件（见第 7、8 节）。

---

## 2. golden schema 逐字段含义

每行一个 JSON 对象（JSONL，UTF-8，无空行），字段如下：

| 字段 | 类型 | 必需 | 含义 |
|---|---|---|---|
| `id` | string | ✅ | 稳定唯一标识，形如 `L001`；**回流时不改已有 id**，新增用新号 |
| `question` | string | ✅ | 用户提问原文（喂给系统的输入） |
| `route_type` | string | ✅ | 期望路由类型，取值见第 3 节 7 类 |
| `expected_sources` | string[] | ✅ | 期望被引用的法条，如 `["《民法典》第577条"]`；无引用要求则为 `[]` |
| `expected_citations` | string[] | ⭕️ | R2-3 引用正确性要求：期望在**答案文本中真实出现**且可归因到 sources 的法条，如 `["《民法典》第577条"]`；缺省 = 无正确性要求 |
| `must_contain` | string[] | ✅ | 回答应包含的关键片段（辅助人工审阅，当前不参与硬打分） |
| `expect_reject` | bool | ✅ | 是否为**越界**问题（应拒答/降级） |
| `notes` | string | ⭕️ | 设计意图说明，便于后续扩充与评审 |
| `difficulty` | string | ⭕️ | 分层字段（Phase 31）：`easy` / `medium` / `hard`；缺省由 runner 兜底为 `unknown`。**新行才写，旧行不动** |
| `source` | string | ⭕️ | 分层字段（Phase 31）：行来源，如 `seed` / `feedback`；缺省兜底为 `legacy`。`feedback_ingest.py` 生成的候选行自动带 `source=feedback` |

> 分层字段**只用于分组汇总报告**（`difficulty_*` 键），不参与任何打分与门禁；
> 因此新增字段不需要回填旧 102 行，也不允许为凑分层而改动/删除既有行（拒伪造纪律）。

### 2.1 `expected_sources` 的依据来源

`expected_sources` **必须来自仓库内真实存在的法律种子数据**，不允许凭空编造法规名：

* `data/legal/legal_seed.json` —— 顶层 `regulations` / `causes` / `interpretations` / `authorities`；
  法规含嵌套 `chapters → articles`（`条号` 为阿拉伯数字，如 `577`）。
* `data/kb/legal_corpus.txt` —— 按「`# 《法规》第X条`」组织的 kb 向量库语料（4865 条）。
* `data/legal/legal_lightrag_corpus.json` —— LightRAG 社区摘要语料（`title` = 条款标题）。

例如 `《民法典》第577条`、`《劳动合同法》第87条`、`《宪法》第35条` 均可在
`data/legal/legal_seed.json` 中逐条核对到原文。

> ⚠️ 这三个数据文件目前是**模板/骨架**（`docs/design/legal_domain_schema.md` 第 5 节明确说明
> 「不含真实法律条文，正式上线前需由用户填入官方发布文本」）。因此本评估集验证的是
> **系统行为契约**（路由、引用透传、拒答），而非法条内容的法理正确性。

---

## 3. 路由覆盖矩阵（必须全覆盖）

`route_type` 共 **7 类**（`agentix/application/agents/router.py` 的 `ALLOWED_TYPES`，
第 91–99 行），映射到 `route_query`（第 156–159 行）的 **6 个出口节点**：

| route_type | 出口节点 | 语义 |
|---|---|---|
| `general-query` | `respond_to_general_query` | 闲聊 / 常识 / 越界拒答 |
| `additional-query` | `get_additional_info` | 信息不足，需追问 |
| `image-query` | `create_image_query` | 图片理解 |
| `file-query` | `create_file_query` | 文件理解 |
| `kb-query` | `create_kb_query` | 知识库语义检索（单条文命中） |
| `graphrag-query` | `create_research_plan` | 图谱多跳关联 / 归纳 |
| `text2sql-query` | `create_research_plan` | 问数 / 统计（text2cypher） |

注意 `graphrag-query` 与 `text2sql-query` **共用** `create_research_plan` 出口，
所以 7 类路由只对应 6 个出口节点。评估集需同时覆盖 7 类路由与 6 个出口。

当前分布（102 条，R2-3 扩充后）：`general-query` 26（越界拒答样本**全部**集中在此类）、`additional-query` 11、
`image-query` 6、`file-query` 6、`kb-query` 23、`graphrag-query` 17、`text2sql-query` 13；
其中 `expect_reject=true` 共 **13 条**，带引用要求（`expected_sources` 非空）**46 条**，
带 `expected_citations` 引用正确性要求 **34** 条；含 **6 条效力感知样例**
（旧法已废止 → 应返回现行替代条文，如婚姻法 → 民法典第1079条，联动 R2-2 效力过滤）。

> `image-query` / `file-query` 条目**不需要真图/真文件**——只断言路由结果即可
> （`expect_reject=false`，`expected_sources=[]`）。

---

## 4. 指标定义与判定口径（确定性优先）

评分函数均为**纯函数、零外部依赖**，位于 `run_eval.py`，可单测：

### 4.1 路由准确率 `route_accuracy`

`score_route(expected, actual) -> bool`：两边 `strip().lower()` 后严格相等。
`route_accuracy = 命中条数 / 总条数`。

### 4.2 引用命中率 `citation_hit_rate`

`score_citations(expected_sources, actual_sources) -> float`：

* 期望为空 ⇒ 返回 `1.0`（无引用要求，vacuous truth；适用于闲聊/拒答/text2sql）。
* 否则返回 `命中条数 / 期望条数 ∈ [0,1]`。

单条命中判定：法规名互相包含 **且**（若期望带条号）条号相等。兼容三种来源形态：
字符串 `"《民法典》第577条"`、graphrag dict `{law, article, source}`、
kb dict `{document_id, source}`。

> 汇总值 `citation_hit_rate` 为**逐条命中率的宏平均**（而非微平均），
> 使单条文与多条文条目权重一致。
>
> ⚠️ **空真（vacuous truth）口径**：期望为空的条目在宏平均中贡献免费满分。
> R2-3 扩充后 102 条 golden 中 56 条无引用要求 ⇒ 全量均值地板 = 56/102 ≈ 0.549。
> 自批次 7 起，`summary.json` 额外暴露以下字段用于区分两个口径（逐条打分不变）：
>
> | 字段 | 含义 |
> |---|---|
> | `citation_expected_count` | 有引用要求（`expected_sources` 非空）的条数——真实分母 |
> | `citation_free_hit_count` | 无引用要求而自动计满分的条数（空真条目） |

### 4.3 引用正确性 `citation_correctness`（R2-3 新增，指南 §2.3.7②）

`score_citation_correctness(expected_citations, actual_sources, answer_text) -> float`：

在 `citation_hit_rate`（检索层命中）之上进一步要求，每条期望法条须**同时**满足：

1. **可归因**：出现在实际 `sources` 中（复用 §4.2 的命中口径）；
2. **真实引用**：答案文本中真实出现《法名》第X条（中文数字条号归一为整数比对，
   `第148条` ≡ `第一百四十八条`；法规名剥离"中华人民共和国"前缀后互相包含）。

任一不满足 ⇒ 该条引用记 0（防伪造引用 / 防检索命中但答案未实际引用）。

聚合口径与 §4.2 相同（宏平均 + 空真可见性）：`summary` 暴露
`citation_correctness`（全量均值）、`citation_correctness_expected_count`
（`expected_citations` 非空条数——真实分母）、`citation_correctness_applicable`
（真实分母上的均值）。

中文条号解析覆盖一~九千的标准组合（含 零/两），非法形态 fail-open 返回 None
（评分按不命中处理，不抛错）。
> | `citation_hit_rate_applicable` | 仅在真实分母上的宏平均；分母为 0 时取 `0.0`（不得再制造空真） |
> | `reject_non_reject_count` | 非越界（不要求拒答）条数——`reject_accuracy` 的构成项 |
>
> 汇报时建议**优先引用 `citation_hit_rate_applicable`**，或同时给出两个口径；
> 逐条 CSV 亦新增 `citation_expected` 列标记该条是否有引用要求。

### 4.3 拒答正确率 `reject_accuracy`

`score_reject(expect_reject, status, message, sources) -> bool`：

* `expect_reject=true`（越界）：满足任一即正确
  1. 状态显式拒答（`rejected` / `refused`），或回答文本含拒答信号；
  2. 状态为 `degraded` **且未携带任何引用**（降级兜底也算正确）。
* `expect_reject=false`（非越界）：**不得**出现拒答信号即为正确
  （单纯 `degraded` **不算**误拒，因为降级仍可能给出实质回答）。

拒答信号词表见 `run_eval.py:REFUSAL_MARKERS`。为避免系统性误判，**刻意不收录**
「专业律师」等词——统一免责声明（`lg_prompts.LEGAL_DISCLAIMER`）每条回答都会带。

### 4.4 语义相关度（LLM-as-a-Judge，辅助、默认关闭）

`judge_similarity(question, answer, *, model=None)`（Phase 26 去 stub）经
**真实 LLM 端点**出分：端点/模型走既有 `OPENAI_API_KEY` / `OPENAI_API_BASE`
与 `ModelGateway.resolve_model("standard")`（温度 0），实际使用的模型随
summary 的 `judge_model` 标注输出；评委输出只允许一个 0–1 数字
（`JUDGE_SYSTEM_PROMPT` 硬约束）。

* **诚实失败，绝不给假分**：无端点 → `NotImplementedError`（CLI 退出码 3）；
  输出解析不出 0–1 数字 → `JudgeError`（同样退出码 3）；空回答 → 确定性 0.0。
* 默认 `--no-judge`，**不参与主分数、不进门禁**（`quality.json` 五 metric
  门结构零变化；summary 仅新增 `judge_model` / `judge_count` / `judge_mean`）。
* **启用前必须人工抽样校准**（Ch7：评委有系统性偏好）。校准流程：
  1. 抽样 ≥30 条离线跑分（`--judge`），导出逐条 `judge_similarity`；
  2. 人工独立标注同批样本的 0–1 相关度（双人背靠背，分歧条目复议）；
  3. 计算评委—人工一致性（Spearman / 绝对差 ≤0.1 占比），未达阈值前
     **不得**把 judge 写进任何报告结论；
  4. 双 Judge 对照（换模型再标一批）检查评委间稳定性后方可常开参考。
* 只作为**参考**，不作为 CI 回归门槛。

### 4.4.1 配对比较与显著性（`--compare`，Ch7 §7.14）

差 1–2pp 可能只是波动。`--compare A B` 把两份预测各自离线打分后按 id
**逐条配对**，对五个指标做 percentile bootstrap 95% CI（纯 stdlib、固定
seed 可复现），CI 含 0 → 标注「不显著（噪声区间）」：

```bash
./.venv/Scripts/python.exe evals/run_eval.py \
    --compare evals/results/a.jsonl evals/results/b.jsonl
```

* Δ = A − B（正数 = A 更好）；配对集为两边 id 交集，各边独有条目数如实报告；
* 纯离线、不写结果文件；golden 缺失或无配对 id → 退出码 2（不给结论）。

### 4.5 可靠性指标 Pass^k（多次采样）

单次跑分回答的是「**这一次**对不对」；真实系统需要回答「**每次都**对不对」。
`Pass^k` 采用 τ-bench 口径，衡量**重复 k 次运行时的稳定性**：

| 指标 | 定义 | 含义 |
|---|---|---|
| `pass_k` | **全部 k 次都成功**的任务占比 | 严格可靠性（**主指标**） |
| `pass_at_k` | **至少一次成功**的任务占比 | 能力上界（乐观口径） |
| `mean_task_success` | 单次运行的逐题宏平均成功率 | 与 `pass_at_k` 对照可看出不稳定性 |

「任务成功」的判定 `_task_success`：**路由正确 且 拒答正确 且 引用全中**（三项同时满足）。

> ⚠️ **口径陷阱**：
> * `pass_k` 的**分母只统计「跑满 k 次」的任务**（`pass_k_tasks_with_full_runs`），
>   采样次数不足的任务不参与——避免把「没跑够」当成「跑失败」。
> * 温度 0 的确定性模型下 k 次结果相同，`pass_k == pass_at_k == mean_task_success`，
>   **该指标无区分度**；只有温度 > 0（或存在非确定性检索/工具）时才有效。
> * `pass_k` 显著低于 `pass_at_k` 说明系统**偶发失败**——这是单次跑分完全看不见的
>   风险，应优先排查。

---

### 4.6 Phase 31 报告键（分层 / 声称一致性 / 敏感性 / 隐私 / Best@k）

以下五族键**全部只增不进门禁**（`test_ci_gate_contract` 门键契约零变化），
缺省行为与既有指标完全正交：

| 键族 | 含义 |
|---|---|
| `difficulty_*` | 按 golden `difficulty` 分组的路由/引用/拒答等指标（缺省组 `unknown`）；组键经 `[^a-z0-9_]` 净化 |
| `claim_consistency` | 声称一致性（`claim_check.py`）：抽取答案中的《法名》第X条声称，与实际 `sources` ∪ golden `expected_sources`/`expected_citations` 比对 → `ok` / `mismatch` / `no_claims` / `missing_prediction`（无 trajectory 诚实 `applicable=false`）。**只标记不判分**，mismatch 需人工复核 |
| `privacy_*` | PII 离线检测（手机号/身份证/银行卡，纯 stdlib 正则）：`privacy_flagged_count` / `privacy_flagged_ids` / `privacy_flagged_kinds`（**只记 id 与类型，原文零留存**）+ `privacy_applicable_excluded`（命中样本从 applicable 类分母排除）+ `privacy_note`；全量口径不变 |
| `best_at_k*` | Best@k 五分项（route / citation_hit / citation_correctness / must_contain / reject）：每任务取 k 次 max 后跨任务均值；无预测或 `repeat=1` 以 `*_reason` 标注 n/a，不出假数 |
| `sensitivity_range_*` | 指令包装微扰敏感性（`--sensitivity`，仅 `--live`/`--record`）：四变体（original / prefix_formal / suffix_source / polite_opening）各跑一遍，报告 5 指标跨变体极差；**离线带该 flag 直接提示并 exit 2**（预测文件是录制固定值，扰动无意义）。缺省关 = 无 `sensitivity_*` 键，输出与现状字节等价 |

> 敏感性变体**只包裹指令前后缀，不改写问题本体**——`must_contain` / `expected_sources`
> 等语义断言不参与扰动，变体间指标可比。会话 id 插入 `variant_seg` 段以便分桶聚合。

---

### 4.7 混合检索归因表（`hybrid_eval.py`，R-P2-5）

**只增报告键，不动质量门禁**（独立脚本，先例 = `memory_eval.py`）：
`Recall@5/MRR × {BM25, Dense, RRF, +Rerank}` 四列归因表——同问题集、
同 `--search-k`、同匹配函数，唯一变量是通道，回答「哪条通道把期望法条
召回到 top-5、排多前」。

* **匹配口径**：golden `expected_sources`（如《民法典》第148条）与检索
  文档（正文 + metadata 干草）比对——法规名经 `run_eval._extract_law`
  归一后互相包含，条号统一转整数按 `第X条` 三形匹配
  （`第148条` ≡ `第一百四十八条`）；与 `citation_hit_rate` 同源解析器；
* **分母**：仅金标非空题（默认取 `legal_golden.jsonl` 的 23 问
  `kb-query` 小集，`--limit` / `--route` 可调，10–50 问带）；
  单题通道失败剔出该列分母，整列失败 = 表内 `n/a` + `unavailable`
  （诚实 fail-open，绝不产出假数）；
* **报告键固定** `{columns, questions, table, unavailable}`（守卫
  `tests/test_p87_hybrid_eval_report.py`），不进 `evaluate()` summary /
  `CSV_FIELDS` / CI `METRICS` 五键门 / `quality_gate`；
* 需索引环境（Milvus + PG sparse 索引）：缺 `--kb-id` / 索引未就绪
  ⇒ exit 2，不降级出假表。

```bash
python evals/hybrid_eval.py --kb-id <KB>                    # 默认 23 问 kb 小集
python evals/hybrid_eval.py --kb-id <KB> --limit 40 --json-out out.json
python evals/hybrid_eval.py --help
```

---

### 4.8 Multi-Agent 聚合与退出契约报告键（`evaluate()` summary）

**只增报告键，不进门禁**（`test_ci_gate_contract` 门键契约零变化；新键不在
`ci.yml` 的 `METRICS` 五键内，由 `tests/test_p90_eval_report_keys.py` 断言）。
两组键都**只在预测行带 `multi_agent` meta 时出现**（扁平拓扑/空预测 ⇒ 整组
缺席，不把「没有」读成 0）：

| 键族 | 含义 |
|---|---|
| `multi_agent_meta_count` / `multi_agent_ok_rate` / `multi_agent_rounds_mean` / `multi_agent_incremental_ratio_mean` / `multi_agent_failure_modes` | Phase 19 既有聚合（信息增量 / 六失败模式 / 任务完成率）。聚合函数只读既有键——meta 新增退出契约键不影响其输出（p88「评测面零接触」钉住） |
| `multi_agent_exit_reason_dist` | 退出原因计数（`exit_reason` 词表 = `domain/runtime/lifecycle.py` 单源四值：`completed` / `max_iterations` / `interrupted` / `error`） |
| `multi_agent_budget_exhaust_rate` | 预算/轮次耗尽率 = `max_iterations` 占比（`truncated` 等式由 p88 钉住，此处不重复推断） |
| `multi_agent_cancel_terminal_applicable` / `multi_agent_cancel_terminal_rate` | 取消终态到达率：分母 = 发生过取消/打断的运行（`exit_reason == "interrupted"`），分子 = 其中 `status` 落在明确终态集合（`completed`/`interrupted`/`failed`）的占比；**离线 golden 不注入取消** ⇒ 无样本时 `applicable=false` 且 rate 为 `null`（诚实显示 —；运行时取消真正打断 Task 后到达终态由 `tests/test_p61_unified_run_chain.py` 断言） |

> 口径与来源：总纲 §6「Multi-Agent：任务完成率、exit_reason 分布、预算耗尽率、
> 取消终态到达率」；实现 = `evals/run_eval.py::_aggregate_multi_agent_exit`
> （与 `_aggregate_multi_agent` 分离，互不污染）。

---

## 5. 两种跑法

### 5.1 离线模式（默认，零外部服务）

```bash
./.venv/Scripts/python.exe evals/run_eval.py --offline
```

读取 `evals/baseline_predictions.jsonl`（schema：
`{id, route_type, sources[], message, status}`）与 golden 对齐打分。

**若基线文件不存在**：打印中文提示（引导到有完整服务的机器跑 `--record`），
并以**非零退出码**（`2`）失败——不会静默跳过。

### 5.2 在线模式 / 录制基线

```bash
./.venv/Scripts/python.exe evals/run_eval.py --record    # 等价于 --live
```

调用真实 `agentix.application.services.chat_service.ChatService.ask`
（`from agentix.application.services.chat_service import get_chat_service`），
逐条提问、落盘 `baseline_predictions.jsonl`、再就地打分。

> ⚠️ 需要 **LM Studio + Docker（Redis / Neo4j / 向量库）** 全链路可用。
> 当前仓库的离线开发环境（未启动 LM Studio / Docker，无 Redis/Neo4j/Milvus）
> **无法运行在线模式**——因此本仓库目前**尚未录制基线分数**（见第 8 节）。

### 5.3 其他参数

```bash
./.venv/Scripts/python.exe evals/run_eval.py --help
./.venv/Scripts/python.exe evals/run_eval.py --golden evals/legal_golden.jsonl \
                                     --predictions evals/baseline_predictions.jsonl \
                                     --results-dir evals/results
./.venv/Scripts/python.exe evals/run_eval.py --judge    # 语义相关度（需端点；无端点诚实报错退出码 3）
./.venv/Scripts/python.exe evals/run_eval.py --compare a.jsonl b.jsonl   # 配对显著性（离线）
```

**可靠性指标（Pass^k）**：

```bash
# 在线：每题重复提问 3 次，自动算 Pass^3（需温度 > 0 才有区分度）
./.venv/Scripts/python.exe evals/run_eval.py --live --repeat 3

# 离线：预测文件已含同 id 多行（attempt=1/2/...）时，限定只取前 2 次
./.venv/Scripts/python.exe evals/run_eval.py --offline --k 2
```

* `--repeat N`：仅在线模式生效，每题跑 N 次，预测行多出 `attempt` 字段。
* `--k K`：限定 Pass^k 的 k 值；离线模式下用于对「同 id 多行」截取前 K 次。
* 未指定 `--k` 时，`k` 取每任务**实际观测到的最大采样次数**。
* 汇总行仅在观测到多次采样（`max_runs > 1`）时追加「可靠性指标」行，
  单次运行不输出该行（避免噪声）。
* `--sensitivity`：敏感性微扰（仅 `--live`/`--record` 生效，见 §4.6）；
  离线/`--compare` 附带该 flag → 中文提示 + 退出码 `2`（拒做假扰动）。

### 5.4 输出

* `evals/results/<timestamp>.csv`：逐条明细，列为
  `id, question, expected_route, actual_route, route_ok, citation_hit_rate, reject_ok, status`
* `evals/results/<timestamp>.summary.json`：`{timestamp, summary{...}}`；
  多次采样时额外含 `pass_k` / `pass_at_k` / `mean_task_success` /
  `pass_k_tasks` / `pass_k_requested` / `pass_k_max_runs_observed` /
  `pass_k_tasks_with_full_runs`；Phase 31 起额外含 §4.6 五族键
  （`difficulty_*` / `claim_consistency` / `privacy_*` / `best_at_k*`，
  `sensitivity_range_*` 仅敏感性开启时出现）；预测行带 `multi_agent` meta
  时额外含 §4.8 聚合与退出契约键
* 终端汇总行（中文）。时间戳格式 `%Y%m%d_%H%M%S`，取自 `datetime.now()`；
  仅当 `privacy_flagged_count > 0` / 声称 mismatch / 敏感性开启时才追加对应说明行
  （常态下汇总行与 Phase 31 之前字节等价）。

---

## 6. 如何扩充评估集

1. **先定位真实依据**：从 `data/legal/legal_seed.json` / `data/kb/legal_corpus.txt`
   中挑选真实存在的法规名与条号，写入 `expected_sources`。
2. **补路由矩阵**：新增条目应尽量填补上表覆盖不足的 `route_type`，
   并保证 6 个出口节点仍全覆盖。
3. **写清 `notes`**：说明这条考察什么（路由？引用？拒答？），便于评审。
4. **勿改已有 id**：`id` 一旦发布即稳定；删除条目也保留 id 不复用。
5. **跑测试**：`./.venv/Scripts/python.exe -m pytest tests/test_p21_eval_harness.py -q`
   会自动校验条数、路由覆盖、拒答覆盖与 schema。

---

## 7. 评估集随生产失败轨迹回流（关键约束）

方案第 7 章明确：**评估集随生产失败轨迹回流生长；训练/测试隔离防泄漏；
不要把评估集同时当训练集。**

### 7.1 失败样本从哪里来

* `ChatService.ask` 的降级/异常路径：`status != "ok"`、`route_type` 为空、
  `status == "error"` 的记录；
* `evals/results/<timestamp>.csv` 中 `route_ok=false` / `reject_ok=false` /
  `citation_hit_rate=0` 的条目；
* 线上用户点踩 / 人工抽检发现的事实性错误的问答对；
* `sources` 引用错误（张冠李戴：法条引用与问题不匹配）的样本。

### 7.1.1 自动化候选生成（`feedback_ingest.py`）

回流流程的**机械化部分**由脚本承担，**评审部分仍然人工**：

```bash
./.venv/Scripts/python.exe evals/feedback_ingest.py \
    --input  prod_failures.jsonl \
    --out-dir evals/candidates \
    --golden  evals/legal_golden.jsonl
```

* **输入**：生产侧落盘的问题记录 JSONL（字段形如
  `{question, route_type, status, error_code, sources, trace_id, ...}`）。
* **判定**：`classify_failure()` 用**确定性规则**分三类——
  `status_failure`（`status ∈ {error, degraded, missing_prediction}`）、
  `error_code`（带 `error_code`）、`missing_route`（`route_type` 为空）。
  规则是**纯函数**，可单测、可复现，不含模型判断。
* **产出**：`evals/candidates/<timestamp>.candidates.jsonl`，每行含
  `_needs_review: true`、`_source.failure_kind`、`_review_hint`。
* **刻意留空**：`expected_sources` 置 `[]`、`expect_reject` 置 `false`——
  **脚本不得自证答案**，期望值必须由人工依据法条原文填写。
* **绝不改 golden**：脚本只读 `--golden`（仅用于计算下一个可用 id 前缀，
  避免 id 复用），**永不写入**。此约束有单测守着
  （`test_run_never_modifies_golden`）。
* id 前缀沿用 golden 既有约定：候选行以 `C<序号>` 编号，
  `next_candidate_index()` 扫描既有 `C###` 取最大值 +1。

**人工步骤（不可省）**：脱敏 → 依据真实法条填写 `expected_sources` /
`expect_reject` → 复核 `notes` → 追加进 `legal_golden.jsonl` → 跑
`tests/test_p21_eval_harness.py` 校验。

### 7.2 如何脱敏

* 去除/替换 `session_id`、`user_id`、trace id、IP、手机号、身份证号等标识符；
* 去除当事人真实姓名、具体地址、具体金额等可识别信息，改为 `某甲`、`某地`、`XX 元`；
* **只保留问题文本 + 期望路由 + 期望法条 + 是否越界**这类行为契约，
  不落盘任何原始模型输出中的敏感片段；
* 脱敏后经人工复核，才能进入 `legal_golden.jsonl`。

### 7.3 如何防止把评估集当训练集（防泄漏）

* `legal_golden.jsonl` **只用于评测/回归**，**禁止**作为 few-shot 示例、
  提示词示例、微调语料、RAG 语料或图谱种子数据；
* 若某条失败样本被用于改进提示词/规则，则该条应**迁移出 golden**，
  同时新增**同类型但不同表述**的新条目——避免“对着答案改”导致指标虚高；
* 训练/测试隔离：训练侧（提示词、规则、语料）与测试侧（`evals/`）由不同文件维护，
  代码中不出现 golden 内容；
* CI 门槛只认确定性指标，且 golden 变更需走人工评审（避免“改题过关”）。

---

## 8. 轨迹前缀回归（双层回归的第二层）

端到端跑分（第 4 节）对**整题**打分，只能回答「**答对了没**」。重构（换模型、
改 prompt、调拓扑）之后，即使答案侥幸正确，**走过的路径**也可能已经漂移——
这在整题打分下完全不可见。轨迹前缀回归回答的是「**走的还是不是同一条路**」。

| 层 | 工具 | 回答的问题 |
|---|---|---|
| 端到端（整题） | `run_eval.py` | 答案对不对（route / citation / reject） |
| 轨迹前缀 | `trajectory_regression.py` | 路径变没变（节点 / 工具 / 状态序列） |

### 8.1 轨迹包从哪来

`agentix/application/services/trajectory.py` 的 `TrajectoryRecorder` 按
`trace_id` 记录步骤（`kind` / `name` / `status` / `latency_ms` / 可选 payload），
`replay(trace_id)` 产出可直接落盘的轨迹包：

```json
{
  "trace_id": "q1",
  "step_count": 4,
  "first_error_step_index": null,
  "steps": [
    {"step_index": 0, "kind": "node", "name": "guardrails", "status": "ok", "...": "..."},
    {"step_index": 1, "kind": "llm",  "name": "router",     "status": "ok"},
    {"step_index": 2, "kind": "tool", "name": "cypher_query", "status": "ok"},
    {"step_index": 3, "kind": "llm",  "name": "summarize",  "status": "ok"}
  ]
}
```

把基线轨迹落到 `evals/baselines/<trace_id>.json`（目录内每个 `.json` 为一个 trace）。

### 8.2 跑法

```bash
# 基线 vs 候选：全量比较
./.venv/Scripts/python.exe evals/trajectory_regression.py \
    --baseline  evals/baselines --candidate out/traces

# 只锁定上游前 5 步（已知下游会变动时）
./.venv/Scripts/python.exe evals/trajectory_regression.py -b base -c cand --length 5

# 只比 name+status（忽略 kind 差异）
./.venv/Scripts/python.exe evals/trajectory_regression.py -b base -c cand --fields name,status

# 报告落盘
./.venv/Scripts/python.exe evals/trajectory_regression.py -b base -c cand --out report.json
```

退出码：`0` 全部匹配；`1` 存在分歧；`2` 参数/文件错误。可直接接进 CI。

### 8.3 比较语义（关键）

基线被当作**参考前缀**——候选必须**以基线前缀开头**：

* ✅ 候选**更长**：不算回归（下游多走几步是允许的）。
* ❌ 候选**更短**：`reason = candidate_shorter`（在比较区内就断了）。
* ❌ 比较区内任一步不一致：`reason = step_mismatch`，报出**首个分歧位置**
  与两侧签名 `[kind, name, status]`。
* ⚠️ **空基线**：`reason = empty_baseline`——显式标注，**不得**让「没有基线」
  冒充「回归通过」（空真陷阱）。
* 基线有、候选无该 trace：`missing_candidate`，判失败。
* 候选有、基线无该 trace：只计数，**不判失败**（便于增量接入）。

签名默认取 `kind, name, status` 三个字段——**刻意不含** `latency_ms` /
`started_at` 等易变字段，避免时序噪声造成假阳性。

---

## 9. 当前状态说明

* ✅ golden 评估集已建立（**102 条**，覆盖全部 7 类路由与 6 个出口节点，含 **13 条**越界）。
* ✅ `run_eval.py` 离线/在线双后端、**5 个确定性打分函数**（`score_route` /
  `score_citations` / `score_citation_correctness` / `score_must_contain` /
  `score_reject`）、CSV/summary 输出、`--judge` 开关（Phase 26：真实端点出分 +
  `judge_model` 标注 + 诚实失败）与 `--compare` 配对显著性均已就绪。
* ✅ **Pass^k 可靠性指标**已接入（`--repeat` / `--k`，含口径陷阱防护）。
* ✅ **失败样本回流自动化**（`feedback_ingest.py`）已接入，输出候选行供人工评审，
  **绝不自动修改 golden**（有单测守着）。
* ✅ **轨迹前缀回归**（`trajectory_regression.py`）已接入，补齐「双层回归」的第二层。
* ✅ **`must_contain` 已参与硬打分**：`score_must_contain()` 做**确定性关键词召回**
  （答案文本中命中比例，大小写不敏感），并已接进 `_task_success` ⇒ 影响 `pass_k`。
  注意口径边界：它只做**字面包含**，不做文本蕴含/同义改写判定——例如要求「撤销」
  时，答「可以请求人民法院予以撤销」算命中，答「可以主张该行为无效」不算命中。
  若将来需要语义级判定，那是 LLM-judge 的职责，不应放宽本函数。
* ✅ 空真口径已显式化：`citation_hit_rate_applicable` / `citation_correctness_applicable` /
  `must_contain_applicable` / `reject_accuracy_applicable` 均只在「真有该项要求」的
  样本上求均值；另附 `reject_false_refusal_rate`（不该拒却拒了的比例）。
  `pass_k` 在**无任何可评测预测**时返回 `null` 而非 `0.0`（配套 `pass_k_applicable`
  与 `pass_k_unavailable_reason`），避免把「没跑」读成「全错」。
* ⚠️ **尚未录制基线分数**：当前环境无 LM Studio / Docker / Redis / Neo4j / Milvus，
  `--live` 无法执行，`baseline_predictions.jsonl` 尚未生成。
  即 `evals/results/` 目前为空，**没有任何真实分数可比对**。
  连带后果：CI 的 `eval-offline` job 会整体跳过（已改为显式 warning + job summary，
  不再静默变绿），且 `evals/baselines/quality.json` 仍是空 `{}` ⇒ 质量门禁处于
  「首次建档」状态，**尚未真正阻断过任何一次退化**。

  **武装顺序（Phase 17；需全栈机器，拒绝手工伪造 baseline）**：
  1. 全栈环境跑 `python evals/run_eval.py --record` → 提交 `evals/baseline_predictions.jsonl`；
  2. `python evals/run_eval.py --offline` → 从最新 `evals/results/*.summary.json` 读 5 个指标；
  3. 本地对每个指标跑一次 `quality_gate.py --current <v>`（首次 NOT-ENFORCED 并写回
     `evals/baselines/quality.json`）→ **手动提交 quality.json**（CI runner 上的建档写回会丢）；
  4. 此后 CI `eval-offline` 在 >2% 相对回退时 exit 1 真阻断。

  > 分步 PowerShell 操作版见 [docs/runbooks/eval-recording.md](../docs/runbooks/eval-recording.md)。
* ✅ LLM-as-a-Judge 语义相关度**已接入**（Phase 26 去 stub）：真实端点出分、
  `judge_model` 标注、无端点/不可解析诚实失败；**默认关、不进门禁**，
  启用前仍需按 §4.4 人工抽样校准。`--compare` 配对 bootstrap 显著性同步接入。
* ⚠️ 轨迹包导出必须用子目录（如 `evals/baselines/traces/`），勿与 `quality.json` 同级。
  生产侧 `TRAJECTORY_SINK=jsonl` 机制已完备（见 `docs/architecture/runtime-guardrails.md`
  §12.5）；跨进程回放需配置 `TRAJECTORY_PATH`（默认 `memory` sink 重启即丢）。
