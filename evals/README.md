# 评测框架与回归（P2-1）

> 依据 `My Agentix_架构优化方案.md` P2-1（第 1159–1180 行）建立。
> 目标：**先定义成功，再谈优化**。为 P0/P1 改动提供确定性的回归门槛。

> ⚠️ **评测集正在换代，尚未完成**：旧的 KG/RAG 法律金标（102 条，`evals/legal_golden.jsonl`）
> 与录制基线（录制预测基线与门禁基线文件）已于 2026-10-26 停用删除，CI 的 `eval-offline`
> 门禁机位随之暂停。**这不是放弃评测**——本目录的评测代码全部保留：确定性打分口径、门禁四态、
> 轨迹前缀回归、失败回流、消融框架都还在原地；新题集 `evals/ceval_golden.jsonl`
> （C-Eval 公开 MCQ，156 题）也已就位，只差基线录完即可重新武装。
> **历史数字（102 条基线、Recall@5 等）是历史测量记录，不再作为门禁基准，也不再重录。**
>
> ✅ **公开 MCQ 评测集已建、**A/B 全量未跑**（2026-10-26 只跑过 3 题冒烟，见 §10.5）**：为量化多 agent 系统的性能，
> 用公开数据集 **C-Eval**（`ceval/ceval-exam`，许可 **CC BY-NC-SA 4.0 = 非商用**）
> 转换出 156 行金标（52 个学科 × 3 题）`evals/ceval_golden.jsonl`；
> 转换脚本 `scripts/eval/ceval_to_golden.py` 带**回源逐行校验**，
> 出处 / 许可 / 抽样口径落在 `evals/ceval_golden.manifest.json`。
> 这套集**只作人工性能测量用**：**没有 CI 门禁、没有质量基线、A/B 全量未跑**，
> 跑法见本文件 §10；3 题冒烟的真实读数与"跑 A/B 前要先修的口子"见 §10.5。

---

## 1. 目录用途

```
evals/
│                                # （旧 KG/RAG 金标集 2026-10-26 停用删除；新题集见下）
│                                # （录制基线随旧集停用删除；新基线未录）
├── ceval_golden.jsonl            # 公开 MCQ 评测集（C-Eval 转换而来，156 行；仅人工性能测量用）
├── ceval_golden.manifest.json    # 上者的出处 / 许可 / 抽样口径 / 校验结论
├── run_eval.py                   # 批量跑 + 打分 + 输出 CSV / summary.json（judge / --compare / --sensitivity）
├── recording_env.py              # 本机录制档：宿主机环境变量装配 + 录制前自检（--local-llm 用）
├── write_quality_baseline.py     # 由 summary **生成** quality.json（分母为 0 或值为 0.0 的键不建档）
├── quality_gate.py               # 质量/性能门禁（对基线 -2% CI 阻断；缺基线/缺键 NOT-ARMED 硬失败，建档要 --allow-bootstrap）
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

* **评测集（曾为 `legal_golden.jsonl`）**：**唯一事实来源**，人工维护，禁止由模型自动生成后
  直接合入。2026-10-26 随知识图谱 / RAG 评测下线一并删除；本文件其余部分描述的是**评测框架**
  （schema 约定、评分口径、门禁语义），换一份评测集即可继续用。
* **`ceval_golden.jsonl`（公开 MCQ 集）**：与上面那份**不是同一条线**——它是**从公开数据集
  机器转换**来的（`scripts/eval/ceval_to_golden.py`），事实来源是 C-Eval 本身而不是人工标注；
  用途是**人工跑一次多 agent 性能测量**，不接 CI 门禁、不建质量基线。详见 §10。
* **录制基线（曾为 `baseline_predictions.jsonl`）**：由 `--record`（在线）在该机器上的真实输出
  落盘，用于离线环境复现打分；同样已于 2026-10-26 删除。
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

> ⚠️ 这三个数据文件目前是**模板/骨架**（`docs/design/测试数据域-本体Schema.md` 第 5 节明确说明
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
* **分母**：仅金标非空题（默认取 `legal_golden.jsonl`〔该集 2026-10-26 已删，分母随在用题集现值〕的 23 问
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
./.venv/Scripts/python.exe evals/run_eval.py --record --local-llm   # 本机录制档（推荐）
```

调用真实 `agentix.application.services.chat_service.ChatService.ask`
（`from agentix.application.services.chat_service import get_chat_service`），
逐条提问、落盘 `baseline_predictions.jsonl`、再就地打分。**逐行 flush 落盘**：
一次录制是上百条分钟级请求，跑完才写盘等于「第 80 条崩掉就颗粒无收」，
中断也留下一份可核对的部分产物（行序与字段集合与一次性写盘相同）。

`--local-llm`（本机录制档，缺省关闭 = 录制路径与此前逐字节等价）按序做三件事，
本体见 `evals/recording_env.py`：

1. **装配环境变量**——优先级 `进程环境 > .env.local > 录制档默认`，`.env` 只在
   「值里没有 compose 内网名」时被采纳（`NEO4J_*` 凭据即由此采纳）。为什么需要它：
   `.env` 是**容器视角**写的（`DATABASE_URL` 指向 `postgres:5432`、
   `NEO4J_URI` 指向 `neo4j:7687`、LLM 指向远端 API），而 `--record` 在宿主机跑；
   内网名解析不到 ⇒ 每条 kb 问题都在观测面撞 `Error 11001`，录出来是一份
   「全是兜底答案」的基线。装配项含：模型一律指向 LM Studio、知识库指向
   `local_kb_demo` seed 过的 SQLite+FAISS、`TEXT2SQL_DATABASE_URL` 从 compose 的
   `mysql` 服务推导、主机端口从 compose 的 `ports` 推导（**不抄表**，纪律 7）、
   以及 `KB_SCOPE_DESCRIPTION` 的**语料换域声明**（出厂 scope 是「测试数据集」口径，
   KB 子图 guardrails 会按旧域把真法条问题判成越界并 0 引用）。
2. **录制前自检**——端点模型是否存在、知识库行数、Neo4j 节点数、Text2SQL
   `articles` 数、Redis ping、kb_ingest 通道可达性；不齐即退出码 `2`。
   自检只花几秒，而跳过它的代价是两小时后一份废基线。
3. **本地模型适配**——结构化输出方式按能力探测 + router 角色超时抬到 60s，
   复用 `scripts/demo/local_kb_demo.py`，不新造第二套口径。

装配清单**逐条打印且脱敏**（连接串里只留 user，密码替换为 `***`）——不留暗改。
本开关不写 `.env`，机器私有的值（模型 id、Neo4j 口令等）放 `.env.local`
（已被 `.gitignore` 覆盖）。

> ⚠️ 需要 **LM Studio + Docker（Redis / Neo4j / MySQL）** 可用；
> Milvus 与 `kb_ingest` 不在也可录，见 §5.4 的 degraded 口径。
> 完全离线的开发环境（未启动 LM Studio / Docker）仍**无法**跑在线模式。
> 录制前的数据准备（图谱 CSV 导入、Text2SQL 目标库回填）见
> `docs/guides/数据准备指南.md`。

### 5.3 从录制到门禁：基线是生成物，不是抄本

```bash
# ① 录制 + 打分（产出 evals/baseline_predictions.jsonl 与 evals/results/<stamp>.summary.json）
./.venv/Scripts/python.exe evals/run_eval.py --record --local-llm

# ② 由那次 summary 生成 committed 基线（先 --dry-run 看一眼）
./.venv/Scripts/python.exe evals/write_quality_baseline.py \
    --summary evals/results/<stamp>.summary.json --dry-run
./.venv/Scripts/python.exe evals/write_quality_baseline.py \
    --summary evals/results/<stamp>.summary.json

# ③ 之后每次 CI 的 eval-offline 做的是：离线打分 → 五个键逐个与 quality.json 比对
./.venv/Scripts/python.exe evals/run_eval.py --offline
./.venv/Scripts/python.exe evals/quality_gate.py --baseline-file <基线文件> \
    --metric route_accuracy --current 0.7843
```

`write_quality_baseline.py` 的三条规则（口径见模块 docstring，
该守卫随旧评测集停用删除于 2026-10-26 一并撤下，规则本身仍由 `write_quality_baseline.py` 的 docstring 说明）：

* **落盘值为 0.0 的键不写**（分母为 0 或分母不为 0 都算，**先 `round(…, 4)` 再判**——
  恒 PASS 的形态由写进文件的那个数决定，`0.00004` 折成 `0.0` 同样是常绿灯）。
  `*_applicable` 在本轮没有
  适用样本时给出 `0.0`；录制中断时可评测样本还会**全落在缺预测区间**，于是分母 34
  的 `citation_correctness_applicable` 也是 `0.0`（2026-10-25 实测）。把 `0.0` 当
  基线写进去等于宣布该指标恒 PASS（`0.0 ≥ 0.0 × 0.98` 恒真），而且这条**连
  `--force` 都越不过**——它与录制完整与否无关，是「这个键没有可把关的下界」。
  宁可让这个键**红**：2026-10-26 起门禁对缺键是 `NOT-ARMED` + 退出码 2（旧口径是
  NOT-ENFORCED + 建档 + 绿灯），所以「本轮有键归档不了」会直接体现在 CI 上，处置只有两条
  可评审的路——补录一份五键都能归档的 summary，或把该指标从 `ci.yml` 的 `METRICS` 里显式摘掉。
* **缺预测不为零时拒绝建档**（`--force` 才能覆盖）：录制未跑完时缺预测按失败计，
  基线被系统性压低，此后真劣化会藏在噪声里。
* **写入的是生成物 + 溯源**：`_meta` 记下来源 summary、题数与各指标分母；
  门禁按键取值，多余键忽略。

> **生成器只保证「写出的键是对的」，拦不住「已入库的键被人删掉」——这条 2026-10-26 已由门禁侧封住。**
> 旧口径下 `quality_gate.py` 对「基线里没有这个键」的处理是 NOT-ENFORCED + **把本轮值写回去建档**
> + 退出码 0：单独看这一步是"响亮地没把关"，但配合删键就成了**静默降级**——那一轮不比对，
> 下一轮起拿本轮值当新地板。2026-10-25 审查按此复现（删
> `citation_correctness_applicable` ⇒ `rc=0` 并把它写了回去）。现在的三条钉子：
>
> 1. **缺输入即阻断，且拒绝路径上不写文件**（`evals/quality_gate.py`）：基线文件不存在、
>    或基线里没有该键 ⇒ `::error` + `NOT-ARMED` + 退出码
>    `NOT_ARMED_EXIT_CODE = 2`（**刻意不等于 FAIL 的 1**：1 意思是"指标劣化了"，这里是
>    "什么都没比对"，混在一个码上日志里就分不开）。首跑建档退成必须显式声明的
>    `--allow-bootstrap`（保留旧 NOT-ENFORCED 语义 + 写回 + rc 0），`ci.yml` 的 `eval-offline`
>    **不带**这一行，只有 `perf-baseline.yml` 的 nightly 带——那里"本轮无基线"是常态。
> 2. **基线值本身**：`test_committed_baseline_is_a_real_gate_not_an_empty_archive` 把下界收严成
>    **开区间**（`0.0 < value ≤ 1.0`，0.0 基线直接红），`test_committed_baseline_covers_every_ci_gate_metric`
>    要求 committed 基线**覆盖** ci.yml 的 `METRICS` 每个键（键清单复用
>    `test_ci_gate_contract._ci_gate_metrics` 从 ci.yml 抽，不另抄一份），并要求生成器的
>    `CI_GATE_METRICS` 与之双向相等。
> 3. **劣化预算写死在 workflow 里**：`test_ci_pins_the_regression_budget_instead_of_inheriting_the_default`
>    从 ci.yml 门禁段落正则抽出 `--max-regression` 的字符串值并断言 `== 0.02`。过去它靠
>    `quality_gate.py` 的 argparse 默认值，而既有用例全部**自己显式传值** ⇒ 往 ci.yml 加一行
>    `--max-regression 0.99` 就静默解除五个指标的门禁；现在放宽必须在这份文件上留下一行可评审的改动。
>    同族还有一条 `test_workflow_bootstrap_posture_is_explicit_where_it_is_needed`：守 perf-baseline
>    真的显式带着 `--allow-bootstrap`，否则默认值翻成硬失败会把 nightly 首跑打成一堆假红。
>
> 三条降级路径的实测（2026-10-26，打在**真树**上：备份 → 变异 → `test_p124` + `test_ci_gate_contract`
> + `test_p41` 三份合跑 → 还原 → sha256 复核逐位一致）：
>
> | 变异 | 红在哪 | 条数 |
> | --- | --- | --- |
> | 删 committed 基线的一个键 | `test_committed_baseline_covers_every_ci_gate_metric` | 1 |
> | 删门禁基线文件 | p124 三条（真门 / 覆盖每个键 / README 数字一致）+ p41 两条文档路径守卫 | 5 |
> | 删 `evals/baseline_predictions.jsonl` | `test_committed_predictions_cover_the_golden_set_exactly` | 1 |
>
> 删预测文件那一条是**本轮新增的内容守卫**：过去它「192 条一条都不红」——p41 的 `_PATH_EXT`
> 刻意不含 `jsonl`，`test_ci_gate_contract` 又只断言 ci.yml **正文**提到这个文件名。现在守卫判的是
> 存在 · 非空 · **题数与金标同源**（不与 `legal_golden.jsonl` 行数相等就红，不写死 102）·
> id 唯一 · id 集合与金标**双向相等**。为什么值得单独一条：CI 的 `eval-offline` 在预测文件缺失时
> 走「响亮跳过」，job 依旧绿而门禁是关的——那是**唯一的静默解除路径**，光靠 workflow 侧的
> `::warning` 拦不住合入。机理与取舍见 `docs/项目实现手册.md` §20.5。

### 5.4 录制机位的已知限制（读基线数字前必看）

* **`kb_ingest` / Milvus 通道未起**：KB 子图的两条本地通道里，
  `postgres` 通道是 HTTP POST 到 kb_ingest（后端 Milvus `legal` 集合）。本机
  Milvus 镜像未拉、服务未起 ⇒ 该通道每条都失败 ⇒ 子图判定「通道失败 ≠ 查不到」，
  走 `milvus`（本地 RetrievalPort，FAISS + SQLite）兜底并给
  `status=degraded`。打分口径**容忍**这一点（`score_reject` 明文：
  「单纯的 `degraded` 不算误拒，因为降级仍可能给出实质性回答」），
  但 `--record` 前自检会如实打印这一条，避免后人重新考古。
  注意端口：`.env` 的 `INGEST_SERVICE_URL` 是 `localhost:8000`，而本机 8000 上
  跑的是**另一个项目**的服务（实测 `/openapi.json` 报 TicketPilot）——录制档把它
  换成 compose 推导出的 kb_ingest 映射端口（默认 8100），宁可连接拒绝，
  也不要跨项目串台拿到一个语义不明的 404。
* **本地 8B 模型的路由残差（2026-10-25 全量录制实测，38/102 不符）**：
  `graphrag-query → kb-query` **17 条（该路由全量误判）**、`additional → kb` 6、
  `general → additional` 5、`general → kb` 5、`text2sql → kb` 2、
  `text2sql → 报错` 1、`file → kb` 1、`additional → graphrag` 1。
  两个后果必须写清：① 图谱子图整轮**只执行了 1 次**（还是一条 additional-query
  问题被误判过去），所以 `route_accuracy=0.6275` 里含着一整块结构性失分，该机位下
  天花板是 (102−17)/102 = 0.833；② 因此**基线不能读成「图谱检索质量」**——
  图谱路径根本没被度量到。`route_accuracy` 在这里的意义只是「同机同口径可比」。
* **1 条 `status=error`（L080）**：Text2SQL 路径在 live 调用里抛 `KeyError: 'code'`，
  由 `run_eval` 如实记成 error 行（`route_type=null`）并按失败计入，**没有**为了
  数字好看把它剔掉。这是一个待定位的产品缺陷，不是评估口径问题。
* **单条耗时（n=102 实测）**：min 11.0 s / median 52.8 s / p95 76.8 s / max 148.5 s /
  mean 48.0 s；整轮 15:50:21 → 17:11:55 = **81.5 分钟**。
  因此录制适合挂后台（`Start-Process` 脱离，见 runbook 第 4 步），进度行
  （`[n/102] id route=… status=… src=… ms`）就是为这个写的。

### 5.5 其他参数（下方示例里的 `evals/legal_golden.jsonl` 已于 2026-10-26 下线：未显式换成在用题集时，入口按 `evals/golden_source.py` 响亮退出 2 并列出可用集——示例不代为改指）

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

### 5.6 输出

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

### 7.1.1 自动化候选生成（`feedback_ingest.py`；下方 `--golden` 示例指向已停用删除的 `legal_golden.jsonl`，须显式换成在用题集）

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

* ⚠️ **评测集换代中**：旧的 102 条法律金标（覆盖全部 7 类路由与 6 个出口节点，含 13 条越界）
  与录制基线已于 2026-10-26 停用删除；下面的框架能力全部保留，新题集基线录完即可重新武装。
* ✅ **公开 MCQ 评测集已建、**A/B 全量未跑**（2026-10-26 只跑过 3 题冒烟，见 §10.5）**：`ceval_golden.jsonl`（C-Eval 转换，
  156 行，非商用许可）+ `scripts/eval/ceval_to_golden.py`（带回源校验）+
  `scripts/eval/mcq_report.py`（MCQ 判分 + 多 agent 性能报告）已就位；
  **跑法见 §10；A/B 全量未跑**（2026-10-26 只跑过 3 题冒烟，读数见 §10.5），不接 CI 门禁。
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
* ⚠️ **基线与门禁的历史记录（2026-10-25 录制，2026-10-26 随旧集停用删除）**：`evals/baseline_predictions.jsonl`
  是 102 条**真实录制**（机位：本机 LM Studio `qwen/qwen3-8b` + `text-embedding-bge-m3`；
  容器 Redis/Neo4j/MySQL 起，Milvus 与 `kb_ingest` 未起 ⇒ 46 条 `status=degraded`、
  55 条 `ok`、1 条 `error`）。门禁基线由
  `write_quality_baseline.py` 从那次 `--offline` 的 summary 生成（生成物，勿手抄）：

  | 指标 | 基线 | 分母 | −2% 阈值 | 还能掉几条样本 |
  |---|---:|---:|---:|---|
  | `route_accuracy` | 0.6275 | 102 | 0.6150 | 1 条 |
  | `citation_hit_rate_applicable` | 0.6268 | 46 | 0.6143 | **0 条** |
  | `citation_correctness_applicable` | 0.5833 | 34 | 0.5716 | **0 条** |
  | `must_contain_applicable` | 0.9098 | 61 | 0.8916 | 1 条 |
  | `reject_accuracy_applicable` | 1.0000 | 13 | 0.9800 | **0 条** |

  双向取证（2026-10-26 对着门禁新默认值复跑过一遍）：同值五个指标逐个走**真比对 PASS**
  （输出里既没有 `NOT-ENFORCED` 也没有 `NOT-ARMED`——后者是本轮新增的"缺输入即阻断"态，
  见上），注入 −5pp 五个全部 FAIL、退出码 1。**这段是历史测量记录**：输入文件已于
  2026-10-26 删除，CI 的 `eval-offline` 机位同步下线，门禁当前处于「未武装」状态。
* ⚠️ **武装后的门禁把关的是「打分口径 + 基线文件」，不是线上运行时质量**。
  `--offline` 是 `committed 预测 × golden × 打分代码` 的纯函数——实测两份离线打分
  （`20261025_171228` / `20261025_172925`）summary **逐键完全相同**；与录制那轮的就地打分
  （`20261025_155021`）相比，差的只有 `best_at_k_requested` /
  `pass_k_requested` / `pass_k_tasks_with_full_runs` 三个 **pass@k 记账键**（录制轮知道每条
  是第几次尝试，离线重打分只见单 attempt ⇒ 102→0、1→None），**五个门禁指标逐键相同**。
  所以 CI 这一步变红说明**有人放宽了打分口径
  或动了基线预测**；要回答「这次代码改动有没有让答案变差」，仍得在全栈机位重新
  `--record` 一次，而那一步是非确定性的、人工的。CI 里真正跑产品代码的是
  另一个 job（`retrieval-gate`），别把两者的覆盖面混着讲。
* ⚠️ **三个指标的 2% 阈值比一条样本还细**（分母 46/34/13 ⇒ 单样本 2.17pp/2.94pp/7.7pp）。
  意味着「任何一条适用样本退化 = CI 红」，重录一次就可能因与改动无关的路由抖动变红。
  这不是 bug，是分母小的必然结果——届时读的是逐条 diff（`evals/results/*.csv`）
  与路由分布，而不是绿/红本身。想让门禁松一个样本的粒度，得先扩样，而不是调
  `--max-regression`。
* ⚠️ **基线数字的形态限定**（详见 §5.4，读数字前必看；§5.x 编号已于 2026-10-27 与文档序对齐）：17 条 `graphrag-query`
  金标全部被本地 8B 路由判成 `kb-query`，图谱子图本次只执行了 1 次（还是一条
  `additional-query` 问题被误判过去）⇒ `route_accuracy` 在该机位下的天花板是
  (102−17)/102 = 0.833，**这份基线不能读成「图谱检索质量」**；另有 1 条
  `status=error`（L080，Text2SQL 路径 `KeyError: 'code'`）如实按失败计入，未做修饰。

  > 分步 PowerShell 操作版见 [docs/runbooks/评测基线录制.md](../docs/runbooks/评测基线录制.md)；
  > 换模型 = 换基线口径，重录后必须重新跑 `write_quality_baseline.py` 而不是手改数字。
* ✅ LLM-as-a-Judge 语义相关度**已接入**（Phase 26 去 stub）：真实端点出分、
  `judge_model` 标注、无端点/不可解析诚实失败；**默认关、不进门禁**，
  启用前仍需按 §4.4 人工抽样校准。`--compare` 配对 bootstrap 显著性同步接入。
* ⚠️ 轨迹包导出必须用子目录（如 `evals/baselines/traces/`），勿与 `quality.json` 同级。
  生产侧 `TRAJECTORY_SINK=jsonl` 机制已完备（见 `docs/architecture/运行时护栏.md`
  §12.5）；跨进程回放需配置 `TRAJECTORY_PATH`（默认 `memory` sink 重启即丢）。

---

## 10. 公开 MCQ 评测集 C-Eval

> 用公开数据集 C-Eval 转换出的一份 MCQ 集，**用来量化多 agent 系统的性能**
> （正确率 / 延迟 / 模型调用次数 / supervisor 出口契约）。

### 10.1 它是什么、不是什么

| | 说明 |
|---|---|
| **事实来源** | 公开数据集 **C-Eval**（HF `ceval/ceval-exam`，revision `617524a0…`），**不是**人工标注 |
| **许可** | **CC BY-NC-SA 4.0 —— 非商用**。商用前须另行取得授权（见 `evals/ceval_golden.manifest.json`） |
| **规模** | 156 行 = 52 个学科 × 3 题（分层抽样，种子固定 ⇒ 可复现）；**题数的唯一真源是 `evals/ceval_golden.manifest.json` 的 `rows` 字段**（由 `scripts/eval/ceval_to_golden.py` 生成，用例 `test_manifest_records_license_and_provenance` 钉 `rows>0` 且 `verification.rows_checked == rows`）。除本行（唯一声明处）外，对 `evals/README.md`、`README.md`、`docs/migration/现有目录树.md`、`docs/migration/PhaseJ_*.md` 跑 `grep -n "156"` 的**每一处命中都是本行的抄本快照**：本文件卷首 ✅ 横幅 `:16`、§1 目录用途块内的目录行 `:30`、§9 当前状态说明 `:683`、§10.5 跑批预算 `:819` 与 `:831`；`README.md:378`、`docs/migration/现有目录树.md:21`、`docs/migration/PhaseJ_评测活证据与叙事收口规划.md` 的 `:16`/`:17`/`:78`/`:97`/`:98`/`:112`/`:158`。登记面即上述四个文件的 `grep -n "156"` 命中；`CHANGELOG.md` 内的「156」是既有条目或行号引用，不属本登记面（该文件 `:35` 已就地说明）。**唯一例外**：本文件 `:103` 的「第 156–159 行」是 `agentix/application/agents/router.py` 的**行号引用**，不是题数抄本。对账跑 `wc -l evals/ceval_golden.jsonl` 并与 manifest 的 `rows` 比对（纪律 7：手抄数不留成第二基准） |
| **用途** | 跑一次**多 agent 系统的性能测量**：正确率 / 延迟 / 模型调用次数 / supervisor 出口契约 |
| **不是** | 不是 CI 门禁、不是质量基线、**A/B 全量未跑**（3 题冒烟见 §10.5）、不替换 §1 那条已停用删除的旧 KG/RAG 评测线 |

### 10.2 重新生成（数据集变了就重跑，别手改金标）

```bash
python scripts/eval/ceval_to_golden.py --per-subject 3
```

产出 `evals/ceval_golden.jsonl` + `evals/ceval_golden.manifest.json`。
脚本**自带回源校验**：写完重新打开文件、重新加载源数据，逐行比对题干 / 四个选项 /
答案字母 / 路由 / `must_contain`，不一致直接**退出码 2**。

### 10.3 跑一次测量（多 agent 开 / 关各来一次）

```bash
# A：多 agent 拓扑（supervisor）
AGENTIX_ABLATION_MULTI_AGENT=true python evals/run_eval.py \
    --local-llm --live --record \
    --golden evals/ceval_golden.jsonl \
    --predictions .tmp/preds_ma_on.jsonl --results-dir .tmp/results_ma_on --no-judge

# B：扁平拓扑（基线对照）——去掉上面那行环境变量，其余逐字相同
python evals/run_eval.py --local-llm --live --record \
    --golden evals/ceval_golden.jsonl \
    --predictions .tmp/preds_ma_off.jsonl --results-dir .tmp/results_ma_off --no-judge

# 各自出报告
python scripts/eval/mcq_report.py --golden evals/ceval_golden.jsonl \
    --predictions .tmp/preds_ma_on.jsonl --label "多 agent 开" --out .tmp/ma_on.md
```

多 agent 开关是 `AGENTIX_ABLATION_MULTI_AGENT`（默认 **关** = 扁平图，
接线点 `agentix/orchestration/langgraph/lg_builder.py::get_active_graph`），
**每请求读一次、不缓存** ⇒ 两组跑在同一个进程里也互不污染。

### 10.4 判分口径（别把宽松当准确）

* **MCQ 正确率用「选项字母抽取」**，不是子串命中。抽取优先级：
  ① "答案/选 X" 类显式标注 → ② 行首 "X." → ③ 四个选项原文里出现了哪一个
  （**只在抽不到字母时启用**）→ ④ 抽不出记 `none`。
  ②③④ 的命中条数分别统计在报告的「抽取方式分布」里 ⇒ 宽松口径占比看得见。
* **多 agent 指标只统计带 `multi_agent` 元数据的行**；扁平拓扑恒无 ⇒ 该组显示「不适用」
  而不是 0（把「没有」读成 0 是这类报告最常见的撒谎方式）。
* **性能读数**取自预测行的 `perf` 段（墙钟 / LLM 步数 / token），缺就显示「未采集」，
  不补 0。
* **路由准确率单列**：C-Eval 期望路由恒为 `general-query`（纯文本知识问答，
  无附件 / 无图 / 无表 / 无 DB / 无知识图谱在评测范围内）。路由判错**单算一项**，
  不混进 MCQ 正确率。

### 10.5 冒烟实测（2026-10-26，3 题）与 A/B 未跑

跑法同 §10.3，只把金标集换成 3 行切片，模型走**本机** LM Studio（`--local-llm` 录制档默认
`qwen/qwen3-8b` @ `127.0.0.1:1234/v1`，不外送提示词）。**A/B 全量（156 题 × 多 agent 开 / 关）未跑**——
维护者裁决本轮不跑评测，本节只登记冒烟读数与跑之前必须先补的口子。

- **接线通了**：`--local-llm` 的录制前自检（`evals/recording_env.py::preflight_local_recording`）
  全条 OK——两个模型 id 在列、知识库 / Neo4j / Text2SQL / Redis 可达；`run_eval.py --live --record`
  落预测、`mcq_report.py` 出报告两条路径都跑通。
- **路由准确率 0**：3 题期望恒 `general-query`，实测路由器给出 `kb-query`（2 条）/
  `text2sql-query`（1 条）。这是被测系统的真实行为，不是脚本问题。
- **MCQ 正确率「无法判分」而非 0%**：3 题答案全部抽不出选项字母（抽取方式分布 `none` 3/3）。
  真因是进了 KB / Text2SQL 管线后被 guardrails 判"不在本知识库问答范围内"而**拒答** ⇒
  答案文本里没有选项字母。报告如实显示「—（0/0 可判分）」，不折成 0%。
- **单题墙钟 29.3s / 40.5s / 45.5s**（那一轮同时在与全量回归抢 CPU ⇒ 只当量级看）。按 40s/题
  外推，156 题 × 2 臂**串行**约 3.5–5 小时；两臂不得并行——LM Studio 单实例并行会互相抢 CPU，
  把延迟读数污染成不可比。
- ⚠️ **跑 A/B 前必须先修的一处**（**措辞修正**：此处此前记的「录制侧把 `perf.llm_steps` / `perf.llm_latency_ms`
  写成了**常量 0**」**不准确**——本轮沿代码复核证明**没有任何一处写死 0**，真缺口是三层接线：
  ① 整条 ask 被包成**一次** Runtime 步骤，类型 `EXECUTING`（`agentix/application/services/chat_service.py:869`–`870`，
  `agentix/runtime/runtime.py:185` / `:199` 同样只用 `StepType.EXECUTING`）⇒ 轨迹里没有 LLM 粒度的步骤；
  ② `StepType` 里**没有 LLM / MODEL 成员**（`agentix/domain/runtime/steps.py:21`–`31`；`GENERATE = "generate"`
  定义在 `:29` 但在 `agentix/` 全包**零代码引用**）；③ 报告侧判据只认带 `llm` 字样的类型
  （`evals/run_eval.py:656`–`658`：`if "llm" not in step_type: continue`）。
  后果形态也与"填 0"不同：`evals/run_eval.py:644`–`648` 的「缺就返回缺键、不补 0」在本次冒烟里**并没有被绕过**——
  `.tmp/smoke_preds.jsonl` 三行的 `llm_steps` 键**存在**，值 0 表示「步骤列表取到了、其中没有一条算 LLM 步」，
  是"真的没采到"而非"填了个 0"。**结论不变**：「模型调用次数」这项指标当前**不可用**，跑 A/B 前要先修仪表接线
  （在执行链里登记 LLM 步 + 报告侧判据与真源同源），照现在跑 A/B 会产出一份"两臂每题都调用了 0 次模型"的报告。
- 非商用许可（CC BY-NC-SA 4.0）不变；本轮 3 题只作本地测量，未再分发题目。
