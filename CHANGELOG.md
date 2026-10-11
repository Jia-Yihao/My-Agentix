# 更新日志 (Changelog)

本文件记录 My Agentix 的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本 SemVer 2.0.0](https://semver.org/lang/zh-CN/)。
版本策略详见 [`docs/VERSIONING.md`](docs/VERSIONING.md)。

变更类型：`新增 (Added)` / `变更 (Changed)` / `弃用 (Deprecated)` / `移除 (Removed)` / `修复 (Fixed)` / `安全 (Security)`

---

## [Unreleased]

### 统一知识库问答与智能助手工作台

- **[前端] 统一聊天入口**：根路径进入单一聊天工作台；输入框的知识库开关按需切换检索域与 Agent 助手，不再使用页面级「知识库问答 / 智能助手」模式切换。
- **[前端] 按模式保留上传语义**：知识库开启时上传 PDF 并写入所选知识库、提交索引；关闭时通过助手附件接口上传本轮文件，并随 Agent 流式请求发送。知识库未选择时禁用知识库上传。
- **[文档] 会话与入库边界**：当前两种模式复用 UI，但仍使用不同后端 API 与服务端会话历史；助手回答直接保存为知识库文档尚未实现。

### 本地真实检索评测（2026-10-11）

- **[评测/文档] 法条语料真实对照**：在隔离索引中使用 LM Studio BGE-M3 与 Infinity Cross-Encoder 对 4,865 条法条、23 条 `kb-query` 金标运行 BM25/Dense/RRF/Z-score/Rerank 对照；Z-score α=0.75 + 10 个精排候选的 Recall@5 为 0.7826、MRR 为 0.7283。Recall@5 与 Dense 持平（18/23），仅观察到排序提升，不能外推为召回覆盖或通用准确率提升；README §15.1 记录指标、延迟口径和复现方法。代码默认 flag 保持不变。

### RAG 质量与评测路线（2026-10-10）

- **[性能] 混合检索双通道并发**：Dense 与 BM25 候选检索并发执行，融合与后续精排仍等待两路完成；不改变 flag-off 路径、通道过滤或索引快照隔离。真实 KB 环境下的端到端收益仍需基准测量。
- **[召回] 法条编号跨格式规范化**：BM25 将“第148条”和“第一百四十八条”等相同编号映射为统一词项，并避免原始编号字符片段制造相似法条误匹配；补充双向编号形式与相邻条号干扰回归。
- **[性能/可观测] BM25 倒排查询与精排降级状态**：BM25 改为按词项 posting 累加候选分数，不再遍历整个语料；rerank 对 HTTP 200 错误对象/无效结果显式告警并 fail-open，混合链阶段报告将 `applied` 标为 false。
- **[修复] 混合检索 BM25 索引快照一致性**：BM25 进程缓存从仅按 `kb_id` 改为 `(kb_id, artifact_id)`；生产 sparse 检索读取活动 manifest 内且 `ready` / `indexed_artifact_id` 匹配的 chunks，避免索引重建后 TTL 内融合旧语料，或提前混入未激活的新语料。补充跨 artifact 缓存与 manifest 状态回归。
- **[文档] RAG 质量优化计划**：README、项目说明书、项目规格、运行时护栏、RAG 设计与评估说明同步梳理分阶段路线：人工金标、Recall@K/MRR 阶段归因、CJK/元数据召回、RRF/Rerank 校准及入库/索引一致性。明确当前模板数据和未录制 live 基线不构成法条准确率证据。

### 前后端契约审计修复 + 死代码清理（2026-10-09）

- **[修复] 文档缩略图/阅读器默认 401（审计 B1）**：后端 `GET /documents/{id}/thumbnail|file` 均要求 Bearer 鉴权（`get_current_user`），而 `<img src>`/iframe 无法携带 Authorization 头、`AUTH_COOKIE_SESSION_FALLBACK` 默认关——默认配置下缩略图全破图、PDF 阅读器整页 401。前端改为**经 axios（自动带鉴权头）拉 blob → `URL.createObjectURL`** 渲染（`documents.ts` 新增 `getDocumentThumbnailUrl`/`getDocumentReaderUrl`/`releaseDocumentThumbnails`/`releaseDocumentReaderUrl`；`PersonalKbView.vue` 换用并在列表刷新/组件卸载时回收 object URL 防泄漏；PDF fragment 参数保留）。同步删除旧直连封装 `documentThumbnailUrl`。
- **[修复] 知识库文档列表超 20 条静默截断（审计 B2）**：后端分页默认 `page_size=20` 且前端不传分页、丢弃 `total`——第 21 个文档起列表/重命名/删除全部不可达。`listKbDocuments` 改按上限 100/页循环拉全（以 `total` 为停止条件，100 页防御上限）；folders 接口后端无分页无此问题。
- **[修复] cron_create 无界 prompt 可落盘（清理批次发现）**：`install_cron_tools` 导入了 `MAX_CRON_PROMPT_CHARS` 却从未参与校验（spec description 声称 ≤4000 但 Field 与处理器均无约束）——`_create` 补 prompt 长度校验（与 name 同款语义）+ `test_p58` 新增超限拒绝/边界通过用例。
- **[修复] 反馈提交补 trace_id（审计 B3）**：meta 帧 `request_id` 经 `useChatStream` 落到助手消息（`StreamableMessage`/`ChatMessage`/`KbModalMessage`/`ChatModalMessage` 四类型 + `useConversationChat` 投影同步）→ `ChatMessageList.sendFeedback` 携带，反馈回流评测管道恢复 trace 归因列。
- **[修复] chat 域 401 对齐全局语义（审计 B5）**：`chatStream.ts` 流式与 `ChatWidget.vue` 非流式在 401 时 `clearToken()` + 广播 `tracerag:unauthorized`（原为静默失败；`V1_REQUIRE_JWT` 默认关无症状，开启后不再踩坑）。
- **[文档] `VITE_API_BASE_URL` 构建期陷阱（审计 B4）**：README §10 补约束说明——该变量仅限本地开发直连，配绝对地址会打进构建产物、容器部署下绕过 nginx 同源反代必然连接失败。
- **[清理] 死代码与冗余（静态扫描确认 + 全量回归）**：后端 `ruff --select F401` 人工复核后删除 23 处确认未用导入（typing 标准库 10 + 测试残留 13；注册副作用类逐一排除保留）；F841 3 处处理（2 删 1 显式弃值——`test_p89` 的"触发模块加载"副作用保留语义）；前端删除旧签名残留 `uploadProgressStatusLabel`（生产单源为 `uploadTaskStatusLabel`，测试改测生产函数）+ 死类型 `ErrorCodeValue` + `VectorDbView` 冗余手动 multipart Content-Type 头。`clearToasts` 为测试清理钩子保留。

### 增量优化批次：安全自评 · 评测 runbook · fallback 跨端点 · 反馈闭环 · Ragas/promptfoo/LangSmith 引入（2026-10-09）

- **[新增] OWASP LLM Top 10（2025）对照自评文档**：`docs/security/owasp-llm-self-assessment.md`——LLM01–LLM10 逐条三栏（风险 → 现有缓解含代码位置 → 残余风险），LLM03 供应链与 LLM07 系统提示泄露如实标注未专项覆盖；README §14 文档索引同步收录。
- **[文档] 模型快照固定纪律注释**：`.env.example` `LLM_MODEL` 上方新增注释——云 API 必须钉快照版本号、禁用 `latest`/浮动别名（理由：`--compare` A/B 与质量基线可比性依赖同模型快照）；LM Studio 本地模型名即快照无需处理。
- **[文档] 评测基线录制 runbook**：`docs/runbooks/eval-recording.md`——8 步 PowerShell 操作版（启动基础设施 → `--record` → 提交 predictions → 离线复打分 → 五指标 `evals/quality_gate.py` 首跑建档 → **手动提交 quality.json** → 推送验证 CI 门禁升级）；`evals/README.md` §9 武装顺序处加链接。
- **[文档] LangSmith 占位（默认关）**：`.env.example` 新增 `LANGCHAIN_TRACING_V2=false` / `LANGSMITH_API_KEY=`（留空）/ `LANGSMITH_PROJECT=my-agentix` 三行注释（免费档 5000 traces/月、14 天保留、fail-open、只接开发评测流量）；README §9 接入说明（生效边界：LangGraph/LangChain runnable 自动上报，自建体系全保留）；`requirements.txt` 注释补一句启用指引。不新增 langsmith 依赖钉版（websockets>=15 冲突，既有决策维持）。
- **[新增] promptfoo 回归主入口（P1-5）**：新建 `promptfooconfig.yaml`（纯 CLI 模式：结果落本地不上云；provider 用自定义 Python provider 持久 worker 直调 ChatService——与 `run_eval --record` 同一调用缝）+ `evals/promptfoo_provider.py`（`call_api` → `get_chat_service().ask`，结构化结果 JSON 序列化、单条失败如实上报）+ `evals/promptfoo_asserts.py`（`get_assert` **直接 import run_eval 的 score_route/score_citations/score_reject/score_must_contain**——打分口径单源，守卫测试断言无实现副本）+ `evals/golden_to_promptfoo.py`（legal_golden → promptfoo tests 机械映射、取前 N 可复现，默认 25 条试点）+ `evals/promptfoo_tests.jsonl`（转换产物）。CI 新增 `promptfoo-eval` job 并入 `ci-gate` needs——**常驻只跑契约守卫 `tests/test_p104_promptfoo_contract.py`（12 条：yaml 可解析/引用面存在/转换产物=golden 子集/期望零加工/口径单源棘轮/get_assert 行为），全离线不跑在线 eval**（M-6 修正：CI 无 LLM，在线 eval 无意义）；README §8 补「如何复现评测」PowerShell 步骤。`npx promptfoo eval` 全量在线跑留待活环境（T11 验收的本地联调项，见报告未尽事项）。
- **[新增] compose web 前端 service（P2-3）**：新建 `web/Dockerfile`（node:22-alpine 构建 → nginx:alpine 托管 dist；构建上下文 `./web` 绕开根 .dockerignore 排除规则）+ `web/nginx.conf`（SPA fallback + `/api` 同源反代 `backend:8000`，`proxy_buffering off`/HTTP/1.1/拉长 read timeout 的 SSE 配置；同源方案 ⇒ 后端 CORS 零改动）+ `web/.dockerignore`。`docker-compose.yml` 新增 `web` service（第 13 个，`profiles: ["full"]` 隔离——默认 up 不启动，开发模式叙述不变；`VITE_API_BASE_URL` 按实测真名，M-4 修正）；README 快速开始补 `--profile full` 变体。`docker compose config` 校验通过。
- **[新增] ragas 忠实度报告（P2-2，observe-only）**：新 `evals/ragas_report.py`（沿 hybrid_eval.py 先例：输入 golden+predictions、报告键族固定 `ragas_faithfulness`/`ragas_answer_relevancy`/`ragas_questions`/`ragas_unavailable`、缺依赖/缺 judge 端点/缺输入一律 exit 2 诚实降级、不进 ci.yml METRICS 与 quality.json 门键）+ `tests/test_p106_ragas_report.py`（9 条：build_samples 纯函数、exit 2 降级、报告键与 CI METRICS 五键交集为空 + METRICS 元组零变化源级钉死）。**依赖实测结论（requirements-dev.txt 钉死组合）**：`ragas==0.4.3 + datasets==4.4.0 + pyarrow==21.0.0`——ragas 0.4.3 硬依赖 datasets≥4，其默认解析链（datasets 5.x→pyarrow≥24→pyarrow 26 起要求 numpy≥2）与运行时 `numpy<2.0.0` 硬冲突；本组合 numpy 1.26.4 下 `import ragas` 实测通过、`pip check` 零冲突。**pip-audit 实测（36 漏洞/15 包）**：既有包（langchain 家族/langsmith/black/pytest 等）为已记录存量；本批新引入且**在 numpy<2 约束下无可达修复版**的余留：datasets 4.4.0（PYSEC-2026-3716，修复版 5.0.1 需 numpy≥2）、pyarrow 21.0.0（PYSEC-2026-113，修复版 23.0.1 同前）、ragas 0.4.3（PYSEC-2026-3046，暂无修复版）、diskcache 5.6.3（PYSEC-2026-2447，暂无修复版）——四者均为 dev 依赖（生产镜像只装 requirements.txt 不装 requirements-dev.txt），ragas 链路 observe-only 不接生产数据面；升级 numpy 后可解除。
- **[新增] 满意度反馈闭环（P2-1）**：新 flag `FEEDBACK_ENABLED`（默认关——flag-off 路由不注册=与基线等价）+ `FEEDBACK_PII_REDACT`（默认开）。后端新增 `agentix/interfaces/http/v1/feedback.py`（`POST /api/v1/feedback`：用户面鉴权 `require_v1_user` + `resolve_v1_caller` + 会话归属校验跨用户 404；事件 `message_feedback` 落 EventPort 三后端；reason 过 `redact_pii`；发布失败 fail-open 回 503 不影响主链路）并注册进 v1 聚合点；新指标 `feedback_total{rating}`（label `rating` 有界 up|down，已入 p100 白名单）。前端新增 `web/src/api/feedback.ts`（fetch 封装、404/网络异常静默失败）+ `ChatMessageList.vue` 消息操作区 👍/👎（`feedbackEnabled`+`sessionId` prop 控制，默认零渲染、一票一消息、失败回滚可重试）。**接线（自动发现）**：`GET /api/v1/feedback/status` 探活端点（挂在同一 flag 门 router 内，flag-off 即 404）+ `probeFeedbackEnabled()`（模块级 memo 只缓存成功，404/网络异常 fail-open 不缓存）+ 组件挂载自动探活（`feedbackActive = feedbackEnabled prop || 探活结果`；prop=true 跳过探活）——装配方无需手工传 prop，后端开 flag 后前端自动出现反馈按钮。导出脚本 `evals/feedback_export.py`：EventPort `message_feedback`(down) → `feedback_ingest.py` 输入格式（`error_code=user_feedback_down` 表达"用户判定的质量失败"，不伪造 `status=error`；question/route_type 留空交人工补全）。测试 `tests/test_p105_feedback_loop.py`（19 条）+ vitest `ChatMessageList.feedback.test.ts`（4 条）。
- **[文档] 性能与成本采数 runbook + README 骨架**：`docs/runbooks/perf-cost-measure.md`——TTFT P50/P95（`agent_ttft_seconds` 直方图 PromQL）、单请求平均 token（`llm_tokens_total` 聚合）、成本 A/B（`LG_TOKEN_HISTORY_BUDGET` 两轮 `--record` + `--compare`）全 PowerShell 步骤；README 新增 §15「性能与成本（待实测）」（数字全部 `<待实测>` 占位 + 测量方法）与 §16「已知限制」（集中收录散落诚实标注）。
- **[新增] fallback 跨 base_url 适配层（P1-1）**：新 Settings 键 `LLM_FALLBACK_MODEL_SPECS`（默认空 = 基线等价）——逗号分隔 `model|base_url|api_key_env` 三元组（`|` 分隔因 base_url 含 `://`；`api_key_env` 是环境变量名，密钥不落配置值、不与主 `LLM_API_KEY` 复用；`base_url`/`api_key_env` 段可空 = 仅换模型名回退主端点）。`model_gateway.py` 新增 `FallbackSpec` dataclass + `fallback_specs()`（解析语义与 `fallback_chain` 一致：去空段/保序/去重/剔除 primary；旧键旧语义零变化，specs 非空时消费点优先）；`chat_agent_tools.py` `_build_fallback_client` 按条目分支（跨端点直接构造 `ChatOpenAI`，仅模型名走 `create_chat_llm` 同构路径）。打点零新增（复用 `gateway_fallback_total{primary_model,fallback_model}`）。五处同步齐（settings/selfcheck/.env.example/configuration.md §5/p41 清单）。新测试 `tests/test_p103_fallback_specs.py`（10 条：解析去重/剔除 primary/空段、specs 优先旧键、两键皆空基线等价、api_key_env 缺失不抛错、消费点源级棘轮、五处同步棘轮）。生效面诚实边界：fallback 只接线检索域 `query_knowledge_base` 工具链路。
- **[文档] 降级演练 runbook**：`docs/runbooks/fallback-drill.md`——8 步 PowerShell 演练（配置三元组 → 停主端点 → 发 KB 检索请求 → 断言响应非主模型 + `gateway_fallback_total` 增长 → 恢复回滚），写明"必须带知识库检索才触发降级"的诚实边界。

### L2 生产链路上下文工程补齐：Prompt Caching 可观测 · 指令分层 · token 级历史窗口 · 状态尾注（2026-10-09）

> 背景：优化规划按投入产出排序的 A/B/C/D 四阶段——把已建好的上下文基础设施（指令级联、token 计数、预算台账）真正接进 L2 生产主链路，并补齐缓存命中率可观测。**三个新 flag 默认全关，flag-off 路径与基线逐位等价。**

- **[新增] A·Prompt Caching 可观测与纪律**：①general 角色的前缀指纹改取**模板常量**——`llm_factory` 新增 `_TEMPLATE_PREFIX_ROLES`，`general` 正文含 memory/指令动态注入不再逐请求上报 `prompt_prefix_fingerprint` 抖动噪声，模板变更仍是漂移信号（`router/planner/tool/summarizer/hallucination` 静态角色行为不变）；②provider 缓存命中量采集——`_TrackedChatOpenAI.ainvoke` 读 `usage` 的 `input_token_details.cache_read`（langchain 归一）与 `prompt_tokens_details.cached_tokens`（OpenAI 原生）双形态，`record_llm_tokens` 新增 `label_type` 显式覆盖参数落 `llm_tokens_total{type="cached"}`，命中/未命中打 `cache_{hits,misses}_total{cache_type="prompt_prefix"}`——**字段缺失（供应商不支持）不记账**，不把「不支持」误算成「未命中」；标签 `type`/`cache_type` 均在 p100 白名单内，零新标签名
- **[新增] B·指令分层接入 L2 生产链路**：新 flag `LG_INSTRUCTIONS_ENABLED`（默认 `False`）——`chat_service._maybe_instructions` 组装级联指令块（复用 `build_instructions`：settings → skills → 全局 → 项目 → 子目录，4k/12k 预算 + hash 去重），经 `configurable["instructions"]` 透传，`general_handler` 按「静态 system 常量 → 指令块 → memory_context」顺序拼接（越稳定越靠前，保护 KV Cache）；**进程级指纹缓存**（`_INSTRUCTIONS_CACHE`：相关 flag + 约定文件 stat + 触达目录任一变化才重读盘，读盘经 `asyncio.to_thread`，故障 fail-open 返回空串）；流式/非流式两路同写；新建仓库根 `AGENTS.md`（项目层指令来源 + 协作约定，约 50 行）
- **[新增] C·token 级历史窗口**：新 flag `LG_TOKEN_HISTORY_BUDGET`（默认 `0`）——`create_message_window_node` 在条数窗口（`MAX_HISTORY_MESSAGES`）之内再按 token 预算从最新消息**向前一次性累加** `count_tokens`（每条只计一次，无逐条重算的 O(n²)），超预算即停、至少保留最新一条（本轮输入不可裁）；主图与 supervisor 图同一工厂自动生效；`0` = 该分支完全不执行，与旧条数窗口逐位等价
- **[新增] D·状态尾注（Manus 式）**：新 flag `LG_STATUS_TAIL_ENABLED`（默认 `False`）——`general_handler._status_tail` 把「对话轮次 / 历史条数 / 预算余量（`BudgetLedger.snapshot_lines`，仅非流式 ask 有 ledger 时）」追加到**本轮 human 消息尾部**（`model_copy` 副本，瞬态只进本次 ainvoke，不写回 checkpointer 历史、绝不进静态前缀）；已知边界：流式路径无 ledger ⇒ 预算段缺席，轮次/历史量两段两路径一致
- **[变更] `record_llm_tokens`**：新增可选 `label_type` 参数（缺省仍按 direction 推导 `prompt`/`completion`），既有调用点行为不变
- **[文档] 同步面**：`README.md`（§3 架构 + §11 测试真源数）、`docs/PROJECT_SPEC.md`（§3.4 开关表 + §5.9.2 L2 消费 + §8.1 注入点）、`docs/architecture/configuration.md` §5（3 新键）、`docs/architecture/runtime-guardrails.md` §13（生产守护测试）、`docs/source-code-study.md` P3·3.3（缓存断点纪律勾选）、`.env.example`（3 新键）、`agentix/config/selfcheck.py`（合法省略清单）、`tests/test_p41_doc_consistency.py`（手写清单）
- **[测试] 新增 `tests/test_p102_lg_context_engineering.py`（29 条）**：①3 新键五处同步棘轮（settings/selfcheck/.env.example/configuration.md/p41 清单）②flag-off 等价（指令空串、条数窗口逐位等价、状态尾注空串）③flag-on 行为（指令块拼装、指纹缓存命中不重读盘/指纹变化重读、token 窗口裁老留新、每条只计一次、最新条必留、预算极小时仍保最新）④general_handler 注入顺序源级棘轮（指令块在 memory 之前）⑤A4 守护：`assert_static_prefix_stable` 对 general 组装序断言公共前缀恰好停在 `<logic>\n` 静态/动态边界 ⑥A2 单元：`_extract_cached_tokens` 双形态/缺字段返 None、`type="cached"` 指标落点、`cache_type="prompt_prefix"` 命中/未命中
- **[变更] 依赖一致性守卫（`test_dependency_consistency.py`）**：本机 `.venv312` 已按 requirements.txt **干净重建**（原增量环境从 Legelbot-develop 拷贝而来，p101 钉版本后实装与声明漂移 18 包）——28 条偏差全部自然消除后按守卫自身指示：①从 `_ENV_SNAPSHOTS["3.12"]` 删除已过期的 grpcio 条目；②按 `test_the_envs_are_actually_distinguishable` docstring 授权路径删除该测试（3.12 与 3.10-linux 档事实收敛为同型；分档机制与 `_ENV_KEY` 登记检查保留，防止「未登记环境 ⇒ 空表 ⇒ 全绿」）；旧环境改名 `.venv312_legacy/` 留盘并加入 .gitignore
- 回归：全量 `4233 passed / 14 skipped / 0 failed`（4204 + 29 = p102，Windows `.venv312` = Python 3.12.13 干净重建后实测；skip 较上口径 15→14 系 py3.12 下 p81 真 HITL E2E 具备执行条件不再跳过）；`ruff` 全绿、`mypy agentix/domain` 0 错；守卫全集 p41/p100/p17/p22/p35/p78 共 177 passed；`test_dependency_consistency` 27 条全绿

### 项目完整性审查：启动链 · 依赖声明 · 文档死链 · 入口与守卫（2026-10-06）

> 背景：要求「确保最终项目完整性，而不是仅仅一个小的 demo」——按**新克隆能否真跑起来**核查，发现十余处 CI 全绿也照样存在的静默缺口（含 2 处启动即崩/命令即炸、1 处跨域全拒、6 项直连依赖无声明）。

- **[修复] `scripts/backend_entrypoint.sh`**：`wait_for_mysql` 原无条件用 `pymysql`，而 `DATABASE_URL` 是 `postgresql+psycopg` ⇒ 容器拿 MySQL 驱动连 PostgreSQL 端口，180s 超时 `SystemExit` + `set -e` ⇒ **backend 每次启动即退出、进 restart 崩溃循环**。改为按 URL scheme 分支（mysql/mariadb→pymysql、postgres→psycopg、未知→提示后跳过）；同时删掉对**已删除**的 `scripts/init_kb_milvus.py` 的调用（phase5 起入库归 kb_ingest，原每次启动打印 "ingestion failed" 且哨兵永不写入）及随之失效的 `wait_for_milvus` / `KB_MILVUS_SENTINEL`
- **[修复] `pyproject.toml`**：`[project.scripts] agentix-mcp-server` 仍指向 p75 已删的 `agentix.mcp.server:main` ⇒ `pip install -e .` 装得上、命令也生成，**一执行即 ImportError**（CI 走 `--no-deps` 且从不调该命令，故长期全绿）；改指 `agentix.capabilities.mcp.server:main`
- **[修复] `.env.example`**：`INGEST_SERVICE_URL=http://localhost:8000` 会被 compose 拿去变量替换 ⇒ 容器内 backend 把请求发回**自己**、且与 kb_ingest 实际的 8100 不符；改为注释掉交由 compose 缺省 `http://kb_ingest:8000`（本机直跑再取消注释写 8100）
- **[修复] `docker-compose.yml`**：`CORS_ORIGINS=${CORS_ORIGINS}` 无缺省 ⇒ `.env` 未设时注入空串、覆盖 pydantic 默认 ⇒ `allow_origins` 变空、跨域全拒；补 `:-http://localhost:3000,http://localhost:5173`
- **[修复] `README.md` §10 快速开始**：原第 5 步 `docker compose up -d` 会启 backend 占住 8000、第 6 步本机 uvicorn 又要 8000（端口冲突），且本机后端连 `postgres:5432`（docker 内网名，本机解析不了；Postgres 宿主端口实为 **15432**）——改为只起基础设施 + 覆盖 `DATABASE_URL`/`INGEST_SERVICE_URL`，另附全容器路径说明
- **[修复] 直连依赖补齐（`requirements.txt` + `pyproject.toml` 同步）**：`aiohttp`（file_handler/image_handler/kb_retrieval/task_handlers）、`pandas`（cypher_tools/visualize）、`regex`（text2cypher 校验器）、`anyio`（chat.py）、`botocore`（s3_minio/documents）、`typing-extensions`（9 处）此前**只靠传递依赖**才恰好装上（上游一调即裸奔）；`tiktoken` 补进 pyproject（原只在 requirements，单跑 `pip install -e .` 会静默退化成 `len//4` 估算）
- **[修复] 文档死链与过期命令**：`docs/VERSIONING.md` 4 条悬空相对链接（根目录文件按同级链接、缺 `../`）；`docs/guides/QUICK_START.md` 3 条从未存在的引用（`INTEGRATION_SUMMARY.md`/`INTEGRATION_VERIFICATION.md`/`CLAUDE.md`）+ `server`/`milvus-standalone` 两个 compose 中不存在的服务名 + 补 8100/15432/9000/3000 四个端口
- **[移除] 两处死代码**：`agentix/retrieval/settings.py` 的 `CORS_ORIGINS` 属性（默认含全仓零引用的历史端口 **23416**，真正生效的白名单是 `config/settings.py::cors_origins_list`）；`text2sql/sql_execution/node.py` 的 `_map_db_type_to_driver`（把 postgresql 映射到 `postgresql+psycopg2`，而声明只有 `psycopg[binary]`，全新安装会 `NoSuchModuleError`；全仓无调用者）
- **[修复] 凭据回退名补进 example**：`GLM_API_KEY` / `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `VITE_PORT` 代码都会读（`DEEPSEEK_*` 设了还会**覆盖**主配置），根 `.env.example` 与 `kb_ingest/.env.example` 此前一概未提
- **[修复] `Makefile` `init-data`** 死目标 `data/chroma`（chroma 早非向量后端，R3.7 起为 Milvus）→ 建 `uploads`/`data/tiktoken_cache`/`data/lightrag`/`data/indexes`/`data/kb`；补 `.PHONY` 与 `help` 条目；`.gitignore` 补 `data/indexes/`；`docs/PROJECT_SPEC.md` 行号与内容同步
- **[新增] `tests/test_p101_integrity_guards.py`（5 条）**：① `[project.scripts]` 每个目标 import 得动且可调用 + MCP 入口必须落 `agentix.capabilities.mcp`；② README/CHANGELOG/docs 相对链接零悬空（>100 条基线防空真）；③ 凭据回退键必须出现在 `.env.example`；④ `INGEST_SERVICE_URL` 不得指回 backend 自身的 8000
- 回归：全量 **4204 passed / 15 skipped**（4199 + 5 = `test_p101_integrity_guards`）；`ruff`（venv 0.5.7）与 `mypy agentix/domain` 全绿；文档相对链接 0 悬空；`docker-compose.yml` YAML 解析通过；`scripts/backend_entrypoint.sh` heredoc 内 Python 70 行语法校验通过

### CI 跨平台修复：Linux 守卫路径 · p43 平台化载荷 · 前端 Node 22（2026-10-06）

> 背景：CI 自 `569acec`（2026-10-05，先于 Phase H 收尾）起 4 测试失败 + 前端 build 失败，最后绿为 `9c5361b`（2026-09-16）——均为**平台假设写死**问题，本地 Windows 全绿而 Linux CI 必红。

- **[修复] `tests/test_dependency_consistency.py`**：`_site_packages()` 由 Windows 专属 `Path(sys.prefix)/"Lib"/"site-packages"` 改 `sysconfig.get_paths()["purelib"]`（跨平台，Windows 解析结果与旧路径逐字相同）——旧路径在 Linux 不存在导致 RECORD 逐文件核对把**全部已装包**误判为"半删"；`_ENV_KEY` 非 win32 加平台后缀并新登记 `3.10-linux` 档（CI 全新安装 ⇒ `deviations`/`record_exempt` 按构造为空，经 pytest 7.4.4 与 matplotlib 3.10.9 两个 wheel 的 RECORD↔内容一致性实测核验）+ 新增 `test_ci_linux_tier_is_covered` 防该档被静默删除
- **[修复] `tests/test_p43_path_safety.py`**：`C:evil.xlsx` 期望文件名按平台（Windows `evil.xlsx` / POSIX `C_evil.xlsx`——冒号被 `_UNSAFE_FILENAME_CHARS` 换下划线，两者均落 temp_dir 内）；防空真载荷抽 `NAIVE_ESCAPE_PAYLOADS` 按平台取"本平台真能逃逸"者（两平台通用 `/etc/passwd.xlsx` + `../../evil.xlsx`，Windows 加 `C:/Windows/evil.xlsx`）——旧写法在 POSIX 上 `C:` 只是普通目录名、不逃逸，断言反噬
- **[修复] `.github/workflows/ci.yml`**：前端 job `node-version` 18 → **22**（vite 8 / rolldown 需 `util.styleText`，engines `^20.19.0 || >=22.12.0`；Node 18 上 build 必挂，run 37421099435 实测）；PROJECT_SPEC 两处 Node 18 → 22 同步
- 回归：`test_dependency_consistency` 28 passed（+1 新守卫）、`test_p43_path_safety` 29 passed（本机 Windows）；全量 **4199 passed / 15 skipped**（4198 + 1 = `test_ci_linux_tier_is_covered`）；`ruff` / `mypy` 全绿

### Phase H · 高性能运行时优化八波（W0–W7）（2026-10-05 ~ 2026-10-06）

> 规划真源 `docs/migration/PhaseH_高性能运行时优化规划.md`（§9 逐波回写实测数据与偏差）；全部 flag 默认关/基线等价，关掉一步回旧行为。

**W0 · 性能真值与基准设施**：新增 `agentix/performance/`（latency 计时 + budget 预算台账）与 `evals/performance/`（runner/fakes/perf_gate/queries.jsonl/baseline.json——首版基准实录，OVERALL p50 1.88ms）；`main.py` 请求计时 + endpoint 归一化 + 事件循环滞后采样，`run_event_stream` TTFT/终帧/耗时拆解入轨迹；新 flag `LATENCY_BREAKDOWN_ENABLED` / `LATENCY_TOTAL_BUDGET_MS` / `EVENT_LOOP_LAG_METRIC_ENABLED`。**用户可感知变化**：慢请求有了可解释的耗时拆解与预算告警。
**W1 · LLM 优化**：路由器确定性 fast-path（寒暄/法条直答免 LLM，`llm_calls_saved_total{reason}` 计数）、角色档位路由（`LLM_TIER_ROUTING_ENABLED`）、终答验证三旋钮预算、上下文段级配额 + `obs://` 外置引用、`prompt_prefix_fingerprint{builder}` 前缀稳定观测；新 flag ×7（含 `VERIFICATION_TOKEN_BUDGET` 执行中增补）。**用户可感知变化**：简单问题首字更快、验证不再无限烧 token。
**W2 · 检索优化**：singleflight 请求合流、分级升级级联（`RETRIEVAL_ADAPTIVE_ENABLED`）、rerank 高分动态跳过、Neo4j Cypher 成本护栏、Milvus nprobe 配置化、缓存键租户+融合指纹升级（旧键 600s 冷却）；新 flag 统一域 2 + 检索域 4。**用户可感知变化**：并发同问不再重复打检索链路，图查询风险被前置拦截。
**W3 · 运行时治理**：三态准入（30s 有界排队，超限 429）、跨层共享重试预算桶、工具超时与剩余预算取 min、七级负载阶梯甩载、四池隔离舱（检索/Neo4j/MCP/WebSearch）、共享 HTTP 连接池（`SHARED_HTTP_MAX_CONNECTIONS`，rerank/Redis 出口接线）；新 flag ×12。**用户可感知变化**：过载时平台礼貌拒绝而非雪崩，LLM/检索不再互相拖死。
**W4 · 持久化 + SSE 后端**：Redis Stream 事件批写（`EVENT_BATCH_MAX_SIZE`，默认 0=关——即时可见契约优先）、轨迹异步写、SSE 到期检查式合并 + producer 有界队列背压（`SSE_COALESCE_ENABLED`，`sse_dropped_frames_total{reason}` 计数、终态帧永不丢）；新 flag ×5。**用户可感知变化**：高扇出流式场景服务端 CPU 显著下降，帧合并窗口内呈现无感知。
**W5 · 多智能体**：Peer 结果契约面（`SUBAGENT_RESULT_CONTRACT_ENABLED`，证据条目外置、契约面过限整体外置可取回）、顺序拓扑早停（`MULTIAGENT_EARLY_EXIT_ENABLED`，跳过 reviewer 与后续轮次）；新 flag ×2。**用户可感知变化**：多智能体收敛轮次减少、token 消耗下降。
**W6 · 前端感知性能与实时动态 UI**（`web/`）：50ms 批窗流式缓冲（`useStreamBuffer`，渲染次数降至 ~20/s）、智能吸底与回底胶囊（`scrollBehavior`）、marked+DOMPurify 消毒渲染、StreamStepper 流式阶段步进器、工具执行卡/耗时芯片/复制+重新生成、useToast 全局 toast（替换 3 处 alert）、停止/重新生成全链（ChatPanel/Home/History/PersonalKb/KbChatModal/PdfReader）；vitest 126 → **198**。**用户可感知变化**：长答案滚动不再抖动、Markdown 即时渲染、流式进度与工具状态可视化、可随时停止/重问。
**W7 · 观测收口 + 全文档同步 + 交付**：OTel GenAI 语义属性（`genai_span` 网关 `chat {model}` + `gen_ai.*` 请求/用量属性，接入 `invoke_with_breaker` 与 `_TrackedChatOpenAI.ainvoke` 两 LLM 出口）+ Prometheus 指标标签基数白名单守卫（29 项有界白名单 + 高基数身份标签禁入）；`web/package.json` 补 `lint` 脚本（修 ci-gate 必红）；perf-smoke 从 `continue-on-error` 转 **CI 阻塞门禁**；新增 `tests/test_p100_genai_observability.py`。

**Phase H 汇总**：后端测试 3908 → **4198**（+290，六个新测试模块 p92–p99 + p100 + perf_smoke），前端 vitest 126 → 198；新 flag **35 枚全部默认关/基线等价**（五处同步完成，检索域 4 键沿 R-P1 纪律两处同步）；新模块 `agentix/performance/`、`agentix/infrastructure/{cache,http}/`、`agentix/reliability/{retry_budget,bulkhead}.py`、`agentix/orchestration/strategies/multi_agent/contract.py`、`agentix/application/services/stream_gate.py`、`evals/performance/`、`web/src/modules/knowledge/{composables,utils,components}/` 六新前端文件；设计文档 `docs/design/frontend-streaming-optimization.md`。

### S2 安全批：依赖升级 · 隐患加固 · 边界测试 · 全量门禁收口（2026-10-04）

- **[安全] 依赖两波升级（`requirements.txt` ↔ `pyproject.toml` 双声明同步，`test_dependency_consistency` 守卫全绿）**：Wave1 14 包——fastapi 0.109.1、setuptools<84、langchain 0.3.30、langchain-community 0.3.27、langchain-core>=0.3.85、langchain-text-splitters>=0.3.9、scikit-learn 1.5.0、pymysql 1.1.1、requests 2.33.0、PyJWT 2.15.0、python-dotenv 1.2.2、python-multipart 0.0.31、jinja2 3.1.6、lxml 6.1.0；Wave2 大版本——fastapi **0.142.2** + starlette **1.7.0**（新增显式钉 `starlette==1.7.0`、`websockets==14.2`）；`pip check` clean
- **[安全] pip-audit 残余 113 → 37 → 21（8 包）**：残余全部属 langchain 0.3.x 生态（langgraph-checkpoint 6 / langgraph 3 / langchain-core 3 / langchain 2 / langchain-openai 2 / langchain-text-splitters 2 / langgraph-sdk 2 / langsmith 1），清零需 langchain 1.x 大迁移（后续计划，本批不冒行为风险）；langsmith 0.8.5 的 1 项为已知取舍——0.8.18 需 websockets>=15，与 uvicorn 0.27 legacy WebSocket API 冲突，故不升
- **[安全] 弱哈希 3 处**：`redis_cache.py` md5 / `milvus_store.py` sha1 / `kb_ingest/kb_service/services/utils.py` md5 → `usedforsecurity=False  # nosec B324`
- **[安全] kb_ingest MySQL 注入护栏**：`kb_service/schemas/ingest.py` 新增 `table` 标识符白名单（`^[A-Za-z0-9_]{1,64}$`）+ `where` 拒 `;`/`--`/`/*` 的 field_validator，FastAPI body 校验自动 422
- **[安全] bandit medium+ 清零（16 → 0）**：3 弱哈希 + 13 处 `# nosec` 同行标注（migrate / migrate_metadata / mysql_ingest / verify_ingestion_pipeline / chaos_test / load_test / settings / scripts run ×2 / start_chatbot ×2 / embedding cache）；当前扫描（agentix+kb_ingest+evals+scripts）medium/high = 0，LOW 77 项为 try/except-pass 等信息性噪声保留
- **[测试] 新增 `tests/test_p91_final_boundaries.py` 50 条**：MySQL 注入护栏、PII/有害内容/幻觉检测边界、embedding cache FIFO/去重/失败降级（`tests/fakes/redis.py` 扩 hash 命令 + `_Pipeline` 支持）、CircuitBreaker 状态机、TokenBucket 容量/补充/突发上限
- **[变更] fastapi 0.142 路由内省适配**：`include_router` 改惰性 `_IncludedRouter`（运行时路由与 openapi 81 路径不变，`/api/v1/chat` 双斜杠别名实测在列）；新增 `tests/route_introspect.py`（`iter_effective_routes`/`effective_paths` 递归摊平，兼容旧平铺结构）修 5 处契约守卫——`test_compatibility_contracts` / `test_p37` / `test_p61` / `test_review_bugfixes` 的路由收集 + `test_p2` lifespan 接受 fastapi 合并包装名 `merged_lifespan`
- **[修复] `domain/policies/memory_recall.py` mypy 2 错**：`float ** float` 在本环境 typeshed 下推断为 `Any` → 显式 `float()` 包裹（零行为变化）
- **[文档] README §11 真源**：3858 → **3908 passed**（+50 = p91）
- **[文档] 交付前同步**：SECURITY.md 安全设计表补「SQL 注入护栏」「弱哈希治理」两行 + 新增「依赖安全现状」节（pip-audit 21 残余口径 / bandit medium+ 0 / 双声明同步纪律）；`docs/migration/current-tree.md` tests 179 → **181** 模块（+p91、route_introspect，口径 = 顶层 .py 不含 conftest）；`.gitignore` 补 `*.db-shm` / `*.db-wal`（SQLite 运行时伴生文件不入库）
- **[版本] 0.1.2 → 0.1.3（PATCH，VERSIONING「安全修复」递增位）**：`scripts/bump_version.py 0.1.3` 同步四处真源（pyproject / `agentix.__init__` / `web/package.json` / `.env.example APP_VERSION`），`--check` 一致；文档字面量同步——README 版本徽章与 FastAPI 徽章（0.109→0.142）、PROJECT_SPEC §1.2 版本真源行/包头行、VERSIONING 示意图、TECH_STACK 示例
- **[文档] PROJECT_SPEC §1.2 技术栈表实况对齐**：修 5 处陈旧——`fastapi 0.109.0 → 0.142.2`（行内补 `starlette==1.7.0`）、`langchain 0.3.7 → 0.3.30`、`langchain-community 0.3.4 → 0.3.27`、`langchain-core>=0.3.39 → >=0.3.85`（行内补 `langchain-neo4j==0.3.0`）、全部 `pyproject.toml:` 出处行号对齐现文件（Web 68–70 / 校验 71–72 / LLM 80–84 / 图编排 89–91 / pythonpath 306）；`docs/source-code-study.md` FastAPI 0.109 → 0.142；统一执行链方案 v2 执行状态行 `p61–p90 → p61–p91`（+p91 注记）
- 回归：全量 **3908 passed / 15 skipped / 0 failed**（2026-10-04 实测）；`ruff check .` 全绿；`mypy agentix/domain` 0 错（35 files）；覆盖率 **73.45%** ≥ 60；bandit medium+ 0；web vitest **126** + `vue-tsc` 0 错；pip-audit 残余 21 项已记录

### 文档守卫盲区扩展 · 旧版孤儿清理 · 性能优化路线图（2026-10-04）

- **[测试] p41 覆盖盲区扩展（33 → 37 条）**：扫描集并入根目录当前态文档（README / SECURITY / CONTRIBUTING / CODE_OF_CONDUCT / `evals/README.md`；CHANGELOG 历史记录刻意除外）与说明书 `docs/*.docx`（stdlib zipfile 逐段抽取——`w:tab`/`w:br`/段边界保留成分隔符防假粘连，首尾 lookahead 边界防 `legal_golden.jsonl` 被抽成 `.json` 假匹配）；路径前缀补 `docs`/`config`/`.github`/`skills`、扩展名补 `vue/ts/js/mjs/mermaid/png/csv`；新增有意留档豁免 `DOCX_INTENTIONAL_REFS`（≤5，当前 1 = release_gate 删除面留档句）+ 根文档贡献/docx 存在性/docx 防空真/豁免上限 4 条守卫
- **[文档] 新守卫当场拦截 4 处 `config/` 简写腐烂**：`docs/architecture/README.md`（单一事实来源行）、`docs/architecture/layers.md`（§4 横切表整列改全路径）、`docs/migration/module-inventory.md` ×2 → 一律 `agentix/config/{settings,ablation,selfcheck}.py`
- **[文档] 删旧版孤儿 `docs/design/TEXT2SQL_IMPLEMENTATION.md`**（零外部引用；text2sql 现状以 `docs/PROJECT_SPEC.md` 相关章节 + `docs/design/wren-text2sql-integration.md` + `agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/text2sql/` 代码为准；保守清理范围——仅删真正零引用孤儿，被 README/CHANGELOG/测试引用的历史记录一律保留）
- **[文档] 新增 `docs/design/optimization-roadmap.md`（性能与优化路线图总纲）**：§1 五层现有性能功能盘点 18 项（LLM 调用/编排/上下文/检索/负载帽，每项锚定真实代码符号与 settings 行号 + 关闭回退行为）；§2 新可扩展优化点 E1–E8——P0：轮内并发灰度、schema TTL 生产化、HTTP 出口连接池化 → P1：embedding 缓存观测与预热、最小延迟基准、prompt 静态前缀稳定化 → P2：语义结果缓存、出口连接池全景审计（每条含现状→方案与方法实现→可扩展方式→验收→风险回退，拟增项均标「拟增」）；§3 分域深钻索引（rag/memory/multiagent-optimization）；§4 分期依赖链 E8→E3→(E1∥E2∥E6)→E4→E5→E7；§5 守卫口径（诚实声明「无专用性能基准」，速度声明须带测量日期+环境）
- **[文档] README §11 真源**：3854 → **3858 passed**（+4 = p41 盲区扩展入链，2026-10-04 实测）
- 回归：全量 **3858 passed / 15 skipped / 0 failed**（2026-10-04 实测）；`ruff check .` 全绿；db-shm/wal 未入

### 文档缺口修复 · 说明书(docx)终态同步（2026-10-04）

- **[文档] md 真缺口 3 处**：SECURITY.md XSS 行 `web/src/components/ChatWidget.vue` → `web/src/modules/chat/components/ChatWidget.vue`；design-legal-domain 两处 `docs/{class,sequence}-diagram.mermaid` → `docs/design/`；其余扫描命中复裁为非缺口（PROJECT_SPEC 迁移出处表 / ADR-0002 已有终态勘误 / ADR-0003 背景更新注记 / `data/cron/jobs.json` 运行时产物 / hermes·wren·archive 外部引用 / `已不存在` 否定句）
- **[文档] 说明书 `docs/My-Agentix项目说明书.docx` 终态同步（原稿 2026-09-30，3395 → 3398 段）**：头部插入「终态同步修订」3 段（迁移映射 + 删除面 + 行号基线声明）；49 段级覆写（AgentLoop 类型表 → `orchestrator.py:32/46/88/94`、`__init__` 契约补 `context_budget_tokens/round_concurrency`、`classify_exception` → `executor.py:115-137`、`get_active_graph` → `lg_builder.py:396-408`、Run API 两段 → `interfaces/http/v1/runs.py` + `/api/v1/agents/runs` + `require_run_bearer`、shim 三行表头/行文 → M6 已删留档、`release_gate` 段 → M5 已删（现门禁 `evals/quality_gate.py`+`release.yml`）、kg single_agent chart/text2cypher 拓扑两行 → M5 已删 + components 现状、ToolExecutor `471→617 行`、loop `290→550 行`、hitl_bridge `640→642 行`、tools 目录表 → `tools/`+`gateway/`+`adapters/` 布局、`settings.py` 函数/属性错行 4 处 `:935→:1287`、`:22→:23`、`:651→:952`、`:870→:1188`）；95 段原位替换保格式（33 条旧路径映射全量 repoint + `settings.py:NNN` 按相邻 KEY 重锚 57 处 + `KEY（:NN）` 2 处 + AgentLoop 段短 `loop.py` → `orchestrator.py` 25 段）；验证 = python-docx 打开 OK + XML 合法 + 路径扫描 missing 归零（唯 `reliability/release_gate.py` 留档句有意保留）
- 回归：p41+p40+p17 = **69 passed**；`ruff check .` 全绿（db-shm/wal 未入）

### 文档细节完善 · 全仓「文档与终态一致性」66 条审计修复（2026-10-04）

- **[文档] README（6 条）**：Chat 示例 `question` → `message`（`ChatRequest` 实况字段）×2 + 删已不存在的「发布门禁」引、`GET /api/v1/ops/events`（旧 `GET /api/v1/events`）、tools 目录树注释改 `definitions.py / registry_init.py / selection.py` 单一注册面、multiagent 测试路径 → `orchestration/strategies/multi_agent/`
- **[文档] PROJECT_SPEC（24+ 条）**：§5 编排段整体改 `orchestration/orchestrator.py`（550 行、run docstring:142、`_run_state:88`、while:196、budget:197-206、回调窗:210-221、终态判定:313-333）+ §5.1 表行号（executor classify :266-277、StepExecutor :32、LoopResult :46）+ 主循环 15 行 loop.py → orchestrator.py 行号映射 + §6.4 事件表 → orchestrator emit 真源（`ON_STEP_END:282` / `ON_RUN_END:152` / `ON_ERROR:166,183,277,301`）+ §7 十组（`build_graph:329-361`、9 节点 :343-356、`get_active_graph:396-408`、supervisor 428-444、route_query 7 值 Literal、settings 键行号 9 处）+ 全文 `settings.py:NNN` 29 处核对清零（含 L2244 secret 段 → `resolved_secret_key:1201`、L2278/2354 KB 键 → settings:1022 起）+ §12 domain 44 → 30 .py、删 `mcp/` `infrastructure/{knowledge,tools}` 行 + §13 异常处理 `main.py:690-728`、`app:741`、v1 `:21-26` + 文件行数 7 处
- **[文档] architecture（15 条）**：README「终态顶层包归属」11 包表 + 代码分层地图整树重绘、layers.md 决策树与代表文件（domain 无 models/、infra 删 knowledge/tools）、runtime-guardrails「result_limiter 未接线」→ 已接线（`HITL_IN_LOOP_ENABLED:828` + `POST /api/v1/ops/hitl/in-loop`）、extension 绝对导入 `kg_tools_list`、interfaces 默认实现三分拆、configuration `GET /api/v1/ops/events`；`generate_architecture_diagram.py:219` 改 orchestrator 路径并重生成两 PNG
- **[文档] guides（16 条）**：QUICK_START 检索 curl → kb_ingest :8100 + X-API-Key、「常见操作/端点速查」按 kb_ingest 5 端点（`/ingest/excel[/upload]` `/ingest/mysql` `/search` `/search/hybrid`）与 `DELETE /api/v1/chat/session/{id}` 重写（KG/GraphRAG 标注「无独立 HTTP 端点」）；crawler_guide 整文件重写；TECH_STACK_GUIDE `knowledge_service/vector_store/reranker/embeddings/kg_neo4j_conn` 等 11 处文件名 → 现行路径（`retrieval/infra/*`、`kb_ingest/kb_service/services/*`、`kg_sub_graph/`）+ `knowledge_router` → `interfaces/http/v1/*`；testing L5 → `scripts/e2e/e2e_phase4.mjs`；routing quick-ref 死链 → `docs/guides/testing.md`；lightrag 语料 → `data/legal/legal_lightrag_corpus.json`
- **[文档] design（7 条）**：design-legal-domain M14/M15 前端路径 → `web/src/modules/chat/components/*`、M17 `data/kb/data.txt` → `legal_corpus.txt`、L3 工具三行全路径 + 旧 `knowledge_service` 已归档注；orchestration-rationale `<A>/lg_builder` → `orchestration/langgraph/lg_builder.py` 全量行号重核（A1/A2/A5/A6、附录 B router 161/160-164/170-184、utils.py 七段、summarize「generate_summary 死链已清」、validation 28-135）；rag-optimization 发布门禁 → `release.yml` + `evals/quality_gate.py`；phase4 前端 e2e 路径 → `scripts/e2e/`
- **[测试] p41 拦截 1 例**：crawler_guide 引用不存在的 `scripts/import_provisions.py` → 改文字表述（路径白名单机制正常工作）
- 回归：全量 **3854 passed / 15 skipped / 0 failed**（2026-10-04 实测，基线不动）；`ruff check .` 全绿；p41+p40+p17 = 69 passed；提交 `5b87532`

### Stage T · 统一执行链：终态收口（evals 报告键 + 全套文档终态化）（2026-10-04）

- **[新增] evals 退出契约报告键（总纲 §6 Multi-Agent 四键；只增报告键、零门禁接触）**：`run_eval` 新独立聚合 `_aggregate_multi_agent_exit`（与老聚合 `_aggregate_multi_agent` 分离——p88 零接触断言原样）产出 ① `multi_agent_exit_reason_dist`（按 `domain/runtime/lifecycle.py` 单源四值 `ExitReason` 计数）② `multi_agent_budget_exhaust_rate`（`max_iterations` 占比）③ `multi_agent_cancel_terminal_applicable` + `multi_agent_cancel_terminal_rate`（口径：分母 = `exit_reason=="interrupted"` 取消样本，分子 = status ∈ 明确终态集合；离线无取消样本 ⇒ `None`/`False` 诚实显示「—」不造假，运行时取消终态由 p61 断言）；meta 缺 `exit_reason` ⇒ 整组键缺席（「没有」不读成 0）；`evaluate()` 接线 + CI `METRICS` 五键门零交集（p90 源级断言）
- **[文档] README §11 真源**：基线 3838 → **3854**（2026-10-04 实测）+ Stage T 段入链（+16 = p90）
- **[文档] README 架构段终态重绘（T2）**：§3 新增「终态统一执行链（Stage A/B/M 收敛后）」ASCII（Run 入口 → 唯一循环 → Runtime → Tool Gateway → Capabilities → Infra）+ 运行时内核表落地路径（Agent Core 2.0 → `orchestration`+`policies`+`agent`+`tools`；MCP → `capabilities/mcp`）+ §4 导语（M2/M3 终态改写、p66 10 文件白名单棘轮）+ 端口契约 → `domain/agent/ports.py` + 依赖边界（`application/runtime` shim 已删）+ `services/` → `application/services/` + MCP Server 命令两处 → `python -m agentix.capabilities.mcp.server` + L1 路由 7 出口补 `text2sql` 与 `task-query` flag 注记 + §12 项目结构树全量重绘（+`orchestration/` `policies/` `tools/`，domain 五子包，删 `mcp/`、`infrastructure/tools`、`infrastructure/knowledge`、`application/runtime` 三簇）
- **[文档] PROJECT_SPEC 终态修订（T3）**：§0.2 SSE 双链路措辞（词汇已归一 type+seq/run_id A5/R5b，帧类型集合仍不同）+ §4.1 domain 文件职责表（35 .py、M0 拆入 `agent/` `runtime/` `tool/` 三子包，删 `agent_types.py`/`ports/agent_core.py` 行）+ 七端口引用三处 → `domain/agent/ports.py`（PlannerPort:53 / MemoryPort:92 / EventPort）+ §8 服务分组表（`control_plane`→M4 runtime、`hitl_bridge`→M3 policies、`llm_factory`→agents 已迁标注）+ §8.9 `hitl_bridge` 行标 M3 已迁 + §14.1 `agentix/mcp` 四转发壳 M6 删除 + 入口命令改写 + §22.2「帧类型集合」+ 对照表编排行 → capability 内部工具循环（M5 已剥离 langgraph ReAct）
- **[文档] 对照矩阵与 DoD**：v2 §5 升级「结构迁移对照矩阵（终态实测 2026-10-04）」+ 逐行新增第 4 列「终态状态」全 ✅（M0–M6/A4/A5 含证据引用）+ §4 执行状态行 + §9 DoD 五项全部勾选；总纲 §3 三方向状态 → ✅ 全部完成（唯 A-P2-3 按 D5 另期）+ §8 DoD 四项勾选 + 终态句
- **[文档] current-tree.md 全量刷新（T4b）**：数字同步 2026-10-04（`agentix/` 527 个 .py、`tests/` 179 模块、`.env.example` 264 活行）；仓库根/一级包/application/domain/infrastructure/retrieval 详树按终态重绘（+`orchestration` `policies` `tools` 一级包，删 `mcp/`，domain 三子包 + ports 再导出，infra 删 `knowledge/`+`tools/`）；§8 缺口清单补终态行（三簇 shim 清零 p66/p69 断言）
- **[文档] module-inventory.md 收口段（T4c）**：追加「统一执行链 Stage A/B/M/F/T」六条勾选（p61–p90 逐批映射 + 主题清单）+ 终态收口行（全量真源指 README §11、shim 空集、白名单 10 文件）
- **[文档] evals/README.md**：新增 §4.8（退出契约四键表 + 离线无取消口径 + 「只增报告键不进门禁」纪律）+ §5.4 输出注记补 §4.8
- **[测试] 新增 `tests/test_p90_eval_report_keys.py` 16 条**：dist 精确计数 / budget 率三态 / 取消口径四态（有样本有终态 / 有样本无终态 / 无样本 None+False / 有样本全终态=1.0）/ 老预测缺键整组缺席 / `evaluate()` 接线有 meta 出键·无 meta 正交 / 真实 golden 空预测键组缺席 / CI `METRICS` 与新键零交集源级 / 老聚合函数无新键回归
- 回归：全量 **3838 → 3854 passed / 15 skipped / 0 failed**（2026-10-04，`ruff check .` 全绿，p41 33 条全绿）

### Stage F2 · F2c-b 统一执行链：Multi-Agent 子代理隔离三件套（A-P2-1）（2026-10-03）

- **[开关] `MULTIAGENT_SUBAGENT_ISOLATION_ENABLED` 默认 false（五处同步：settings Field / selfcheck / `.env.example` 活行 / configuration.md 表行 / p41 手写清单）**：flag-off supervisor 现行为逐位等价（handler 收原 state/config 对象、meta 无 `isolation` 键、Manager 不收 `budgets`）
- **[新增] `orchestration/strategies/multi_agent/isolation.py`（纯基础设施，零 I/O 零 LLM）**：① `IsolationPolicy`（frozen）+ `build_task_face_prompt`——ephemeral 恒不拼父轨迹，只读快照段按 `memory_snapshot_readonly` 注入；② `take_memory_snapshot`——str 原样冻结、Mapping 深拷贝后 `MappingProxyType`（写入 TypeError，真只读非口头）、空值/未知类型 → None；③ `SubAgentSession`（session_id + 独立 `PeerBudget` 副本 + state/心跳/steering），`session_budgets` 产出 `{actor: budget}` 经 Manager `budgets` 单源注入；④ `LiveSubagentRegistry`——register/get/by_actor、`heartbeat`/`is_alive(ttl, at)` 时间可注入、pause/resume/kill（kill 终态幂等且审计保留，heartbeat/steer/resume 对其失效）、steer/take_steering 一次性、`status()` counts+records；⑤ `wait_while_paused`——resume 继续 / kill 短路 / 超时 `max_pause_wait_seconds` fail-open 放行（绝不悬挂）；⑥ `ephemeral_task_face_state`（浅拷贝 + messages 收敛最后一条，router 等任务态字段共享）+ `isolated_config`（摘除活 `memory_context` = skip_memory，只读快照改 `memory_snapshot` 键，原 config 不就地修改）；⑦ `session_gate`（心跳→pause 有界等待→kill 短路返回 `session=killed` PeerResult）+ `wrap_peer`（包装内层：steer 注入 `[steering]` 段+`steered` 标记、快照段追加、内层零执行）
- **[接线] `lg_supervisor`（仅 flag-on 分支）**：handler 子 agent 收任务面 state/config；Proposer/Reviewer 各持会话经 `wrap_peer` 包装（心跳在被调即刷新）；`_new_isolation` 组装 registry+双会话（budget 走 `_budget_factory` settings 单源镜像）；meta 仅 flag-on 追加 `isolation` = policy/sessions/snapshot_chars/registry 四键；模块与 `supervisor_dispatch` docstring 同步诚实化
- **[文档] multiagent-optimization.md** 状态行 A-P2-1 移入已完成（待实施清零，余 A-P2-3 按 D5 另期）+ §6 A-P2-1 完整注记；configuration.md 新增键表行
- **[测试] 新增 `tests/test_p89_subagent_isolation.py` 47 条**：policy 默认与 to_dict / 快照五态（str·空值三态·Mapping 冻结写入抛·未知类型·frozen 抛 `FrozenInstanceError`）/ 任务面 prompt 三态（ephemeral 丢父轨迹·非 ephemeral 含·快照段按 policy 门控）/ registry 九态（注册查找·TTL 时间旅行·pause-resume·kill 终态幂等审计·steer 一次消费·status 计数与字段·budgets 映射与独立副本）/ `wait_while_paused` 四态（直通·resume 解除·kill 解除·超时 fail-open·未知会话）/ 任务面 state/config 六态（截除·router 共享·空消息·摘记忆注入快照·原 config 未污染·非 dict 直通）/ `wrap_peer` 七态（直通+心跳刷新·steer 一次性+标记·快照段·kill 短路内层零执行·pause-resume 跑内层·pause 中 kill 短路）/ supervisor flag-off 两态逐位等价·flag-on 五态（任务面截除+摘记忆+快照键·isolation 块结构·无记忆零快照·budgets=会话预算·kill 闸·steer+心跳经 dispatch）/ 配置键四处同步
- 回归：全量 **3791 → 3838 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2c-a 统一执行链：Multi-Agent 退出契约 + 六失败模式打点（A-P2-2 余项）（2026-10-03）

- **[契约] `ManagerOutcome` 统一退出契约（无 flag——A-P0-3「先契约后功能」正确性修复）**：增 `exit_status/exit_reason/truncated` 三字段，`__post_init__` 按 `reason` 推导——新 helper `derive_exit_contract`：`converged`→completed/completed；`proposer|reviewer_budget_exhausted`/`max_rounds_exceeded`→failed/max_iterations（truncated=True）；`peer_cancelled:*`→interrupted/interrupted；`proposer_or_reviewer_not_wired`/`no_progress`/`contradiction_unresolved`/`proposer_accept_reviewer_reject`/深度超限/兜底→failed/error（ok=True 兜底 completed）；词表**单源复用** `domain/runtime/lifecycle.py` 的 `ExitStatus/ExitReason`（不另造平行词表），并强制契约等式 `truncated == (exit_reason == "max_iterations")` 按 reason 重算（显式传入也强制对齐）
- **[透传] `lg_supervisor` meta 增三键** `status/exit_reason/truncated`（成功与失败两分支都带）；Manager 异常路径（outcome=None）诚实 `failed/error/False` 不冒充完成态；`supervisor_dispatch` docstring 同步声明
- **[打点] `multiagent_failure_mode_total{mode}`**（`agentix/observability/metrics.py`）：prometheus Counter + 无依赖 `_NoopMetric` 双分支；`record_multiagent_failure(mode)`；`ManagerOutcome.__post_init__` 在 `failure_mode != NONE` 时恰打一次（含 `escalate` 深度超限路径），**NONE 不计**——成功收敛不进失败计数器
- **[评测面零接触]** `run_eval._aggregate_multi_agent` 只读既有键，meta 新增键不改变聚合结果（p88 断言双输入聚合相等）
- **[文档] multiagent-optimization.md** 状态行 A-P2-2 移入已完成 + P0（A-P0-1/2/3/4/5）标注已随 v2 Stage A1/A2/A3 完成 + §6 A-P2-2 完整注记
- **[测试] 新增 `tests/test_p88_multiagent_exit_contract.py` 55 条**：词表单源两断言 / 推导表 13 行 × 枚举入值域（16 组合）/ 落章四态 + 契约等式全谱 + 显式字段不覆盖等式仍重算 / 打点三态（恰一次·NONE 不计·escalate 路径）+ not_wired 映射 + metrics 面存在 / supervisor meta 三态（收敛·未收敛·manager 异常）/ 评测聚合零接触 / docstring 三键在案
- 回归：全量 **3736 → 3791 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-e 统一执行链：RAG P2 混合检索归因报告（R-P2-5）（2026-10-03）

- **[新增] `evals/hybrid_eval.py` 独立归因报告脚本（先例 = `memory_eval.py`，只增报告键、零门禁接触）**：`Recall@5/MRR × {BM25, Dense, RRF, +Rerank}` 四列归因表——同问题集、同 `--search-k`、同匹配函数，唯一变量是通道，回答「哪条通道把期望法条召回到 top-5、排多前」；四通道接线 = `sparse_search` 直调（bm25）/ `retrieve_docs_with_retry` 直调（dense）/ `retrieve_hybrid_docs` 强制 sparse 参与融合 + `RETRIEVAL_HYBRID_RERANK=false`（rrf）/ 同链 rerank 开（+Rerank），flag 调用即翻、finally 即还原
- **[口径] silver 匹配复用 `run_eval` 同源解析器**：金标 `expected_sources`（如《民法典》第148条）与检索文档（正文 + metadata 干草）比对——法规名先剥 `第X条` 再 `_extract_law` 归一（原函数为引用对引用设计、会吞条号段）后互相包含；条号统一整数按 `第X条` 三形匹配（`第148条` ≡ `第一百四十八条`，新写 `_int_to_cn` 0–9999 含空位补零 1005→一千零五）；解析失败退化子串（与 `_citation_matches` 同策略）
- **[口径] 分母与 fail-open**：仅金标非空题进分母（默认 `legal_golden.jsonl` 23 问 `kb-query` 小集，`--limit`/`--route` 可调，10–50 问带）；单题通道失败剔出该列分母，整列跑不通 = 表内 `n/a` + `unavailable` 诚实降级，绝不产出假数；`recall_at_5` = top-5 内首中题占比（rank>5 记未中），`mrr` = 首中位次倒数均值（全列表不截断），四舍五入 4 位
- **[门禁边界]** 报告键固定 `{columns, questions, table, unavailable}`，不进 `run_eval.evaluate()` summary / `CSV_FIELDS` / CI `METRICS` 五键门 / `quality_gate`（源级守卫钉死）；缺 `--kb-id`、`--search-k < 5`、索引 artifact 未就绪/索引文件缺失 ⇒ 诚实 exit 2，不降级出假表
- **[文档] evals/README.md** 目录树 + 新增 §4.7（匹配/分母/报告键/门禁边界）；rag-optimization.md 状态行 **P2×5 已全部完成** + §6 R-P2-5 完整注记
- **[测试] 新增 `tests/test_p87_hybrid_eval_report.py` 38 条**：`_int_to_cn` 十二例（含空位补零与超界退回）/ 匹配八态（法规名 content·metadata 标题、条号三形互转、错条/错法不中、无法规名退化子串、空文档）/ `first_rank` 四态 / 报告固定键集、指标数学与 rank6 口径、unavailable 诚实、四列 markdown 表 / 注入 runner 端到端（空金标不进分母、单题通道失败剔除）/ golden 小集装载（默认 23 问带 + 过滤/上限）/ CLI 诚实退出三态 / 门禁边界四条源级（run_eval·quality_gate·CI 零接触 + README 在案）
- 回归：全量 **3698 → 3736 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-d 统一执行链：RAG P2 Graph 动态化（R-P2-3）（2026-10-03）

- **[新增] schema 感知 Cypher 生成（键 `KG_SCHEMA_AWARE_GENERATION_ENABLED` 默认 false，五处同步）**：新 helper `components/utils/utils.py::resolve_generation_schema`——flag-on 时生成链与修正链（`cypher_tools/utils.py` 两处 ainvoke）的 `{schema}` 改用与校验同源的 `retrieve_and_parse_schema_from_graph_for_prompts`（法律域文档摘要 + 花括号清洗后的**发现态** `graph.schema`，标签/属性/关系来自库内发现而非静态中文标签描述），发现/渲染异常 **fail-open 回裸 `graph.schema`** 绝不打断生成；flag-off = 基线裸 `graph.schema` 逐位等价；孪生链（`text2cypher/generation/prompts.py`）静态中文标签冗余块改 `{legacy_schema_hint}` 占位 + `legacy_schema_hint()` 调用期取值——flag-off 拼接结果与基线**逐字节等价**，flag-on 置空（动态 `{schema}` 成为唯一权威源），孪生 node 同步接 `resolve_generation_schema`
- **[新增] 执行结果统一进证据池（键 `EVIDENCE_POOL_ENABLED` 默认 false，五处同步）**：`task_execution._to_observation` 成功分支携带 `meta["evidence"] = [{"content": 正文[:4000], "source": tool}]`——取**去重 stub 之前的原文**（grounding 不吃折叠占位文本），失败观测不携带；`LayeredVerifier._extract_evidence` 既有读取口（`meta.evidence`/`hit_chunks`）即刻可消费，喂 L2 `sources` 与 L3 grounding；flag-off = meta 无 `evidence` 键与基线逐位等价
- **[新增] L3 grounding 总闸（键 `VERIFIER_GROUNDING_ENABLED` 默认 false，五处同步）**：`task_execution` 与 `agent_loop_adapter` 两处生产调用点原硬编码 `require_grounding=False` 改读本键；与 `EVIDENCE_POOL_ENABLED` 同开 = 工具/图执行结果真正进 L3 支撑度核验（此前 L3 仅测试侧可达）
- **[裁定] 示例分类不在冻结设计范围**：`legal_retriever` 模块 docstring 与 `_dynamic_classify` 注释诚实化——R-P2-3 冻结条款为 schema 发现/感知生成/fast-path/证据池四件，示例分类保持显式 `NotImplementedError`（p68 契约钉住，warning 文案不变），few-shot 维持策展通用示例 fast-path + 词法相关性重排
- **[保留] 预定义模板 fast-path**：`predefined_cypher` 7 类模板与 `run_governed` 只读闸不动（p68 既有守卫覆盖）
- **[文档] rag-optimization.md**：状态行改「P2 余 1 项待实施（R-P2-1/2/3/4 已落地），R-P2-5 仍待实施」+ §6 R-P2-3 完整注记（三键、fail-open 语义、裁定与守卫指针）；configuration.md 特性开关分组行 + §5 详情三行
- **[测试] 新增 `tests/test_p86_graph_dynamic.py` 25 条**：三键默认值与五处同步（selfcheck/.env 活行/configuration.md 双落/p41 清单/rag 注记）/ schema 渲染（flag-off 恒等、flag-on 发现态+文档摘要、故障 fail-open、文档缺失仍动态）/ 生产链与孪生链接线源级 / 孪生静态块 flag-off 逐字节等价与 flag-on 置空 / 证据池六态（flag-off 无键、成功携带、失败不带、4000 截断、去重下保留原文、`_extract_evidence` 可消费）/ L3 两调用点接线 / fast-path 与 p68 分类契约未破
- 回归：全量 **3673 → 3698 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-c 统一执行链：RAG P2 Text2SQL 加固（R-P2-4）（2026-10-03）

- **[修复·correctness 无 flag] 重试耗尽显式失败，不把未执行 SQL 包装成答案**：`format_answer` 增 `is_valid` 分支（execute 节点仅 `is_valid=True` 可达 ⇒ 走到 formatter 即「重试耗尽从未执行」）——答案换成 `SQL_NOT_EXECUTED_ANSWER`（不再产出误导性「查询结果摘要」）+ 回写 `execution_error=SQL_NOT_EXECUTED_ERROR`，`text2sql_tool` 层既有 `if execution_error: errors.append` 消费 ⇒ 下游拿到显式失败而非成功样式的假答案；覆盖空 SQL/词法失败/dry-run 失败三类耗尽路径
- **[新增] dry-run 校验（键 `TEXT2SQL_DRY_RUN_ENABLED` 默认 false，unified 五处同步）**：`sql_validation` 节点词法+安全检查通过后跑 `EXPLAIN` 试跑（新模块 `sql_validation/dry_run.py`，先例 = Cypher `EXPLAIN` 试跑）——失败追加错误喂既有重试环（retry_count 照常递增）；**基础设施故障（连不上库/账号/库名等 `_INFRA_MARKERS`）fail-open 放行**不误杀合法查询；词法失败不重复试跑
- **[新增] schema 缓存（键 `TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS` 默认 0=关闭=基线逐位等价，unified 五处同步）**：`schema_retrieval` 节点按 `(DATABASE_URL, 归一问题)` 缓存 INFORMATION_SCHEMA 拉取（schema_context 含按问题筛表结果故键含问题）；失败不入缓存、128 条上限过期剪除；打点 `record_cache_hit/miss("text2sql_schema")`（`cache_hits_total{cache_type}`）
- **[修复] Wren 缺失显式降级打点**：`TEXT2SQL_BACKEND=wren` 但工厂返回 None（wrenai 未装/项目无效）此前只 `logger.warning`、steps 无痕——现同样留痕 `sql_execution_wren_degraded` + `record_degraded("text2sql_wren")`；既有 `WrenNotAvailable`/异常降级路径同打点（steps 此前被 tool 层丢弃，指标让降级在 Prometheus 可见）
- **[配置] 两新键五处同步**：settings 默认 false/0（Field+ge=0）/ selfcheck `DEFAULT_IGNORED_MISSING` +2 / `.env.example` 两活行 / configuration.md 特性开关分组行 + §5 详情表两行（p63 双落表口径）/ p41 手写清单 +2
- **[文档] rag-optimization.md**：状态行改「R-P2-1/2/4 已落地、余 2 项待实施」+ §6 R-P2-4 完整注记（四件套、fail-open 语义、守卫指针）
- **[测试] 新增 `tests/test_p85_text2sql_p2_hardening.py` 29 条**：键五处同步 / formatter 显式失败四态（耗尽/空 SQL/执行错误原样/有效路径逐位不变）+ tool 消费源级 / 路由器 execute·retry·end 契约 / dry-run（flag-off 绝不调、失败喂重试环、通过保持 valid、词法失败跳过、空 SQL 短路、`_is_infra_error` 分类、无连接串 fail-open、sqlite EXPLAIN 通/拒）/ schema 缓存（TTL=0 每问现拉、同题命中+归一入键、换题重拉、失败不入缓存、命中计数）/ Wren 三态（请求缺失降级打点、默认后端不误报、异常降级打点）
- 回归：全量 **3644 → 3673 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-b 统一执行链：RAG P2 查询改写/HyDE（R-P2-1）（2026-10-03）

- **[新增] 查询改写/HyDE（两独立键 `RETRIEVAL_QUERY_REWRITE_ENABLED` / `RETRIEVAL_HYDE_ENABLED`，均默认 false = 与基线逐位等价；检索域 env 键沿 hybrid 块注释示例三处同步）**：`agentix/retrieval/usecases/query_rewrite.py`——`REWRITE_PROMPT` 多轮改写（意图补全，结合历史对话补全省略主语/指代，只输出检索式）+ `HYDE_PROMPT` 假设性文档生成（200 字内知识库风格正文片段）；归一纯函数 `normalize_query_output`（取首行、去包裹引号/直角引号、截 512、去空白忽略大小写等价 ⇒ 判未改写）/ `normalize_document_output`（截 1200）；`rewrite_query`/`hyde_expand` 经 `invoke_with_breaker`（熔断+限流）+ 8s `asyncio.wait_for` 有界调用，**LLM 失败/超时/空输出/等价输出一律回退原 query（fail-open），绝不阻断检索主链路**
- **[接线] `retrieve_scoped_docs_cached` 单缝（预检/工具/端口适配三调用方全覆盖）**：flag-off 路径代码逐字未动（`effective_norm = question_norm`、`cache_suffix = ""`）；生效才替换 dense/hybrid 的检索文本并给缓存池加后缀 `|r1`（改写）/`|y1`（HyDE）——沿 hybrid `|h1` 先例隔离结果池，失败回退原 query ⇒ 与 flag-off 同池语义相同；rerank 仍按原用户查询打分；新增 `history` 参数（预检路径由 `_stream_retrieval_preflight` 传 `_prepare_context` 的 `history_text`，工具/端口适配缺省空 ⇒ 单轮意图补全）
- **[文档] rag-optimization.md**：状态行改「R-P2-1/2 已落地 F2b-b/F2b-a、余 3 项待实施」+ §6 R-P2-1 完整注记（键名、归一与 fail-open、单缝接线、缓存后缀、history 透传、守卫指针）；`.env.example` hybrid 块补两注释示例行
- **[测试] 新增 `tests/test_p84_rag_p2_query_rewrite.py` 27 条**：键同步 / 归一全谱（空回退、首行去引号、等价判未改写、512/1200 截断、多行文档）/ LLM 薄封装（成功带历史、异常 fail-open、空历史占位、超时 fail-open、空问题短路、HyDE 成败双态）/ 接线（flag-off 绝不调变换、生效改写/HyDE 检索文本、回退与异常保原查询、双 flag 组合改写→HyDE 序、缓存池 `|r1` 隔离与回退/基线同池无后缀、history 透传、预检源级接线）
- 回归：全量 **3617 → 3644 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-a 统一执行链：RAG P2 证据门禁（R-P2-2）（2026-10-03）

- **[新增] 证据门禁（键 `EVIDENCE_GATE_MODE`，检索域 env 键沿 `GROUNDING_*` 同族：rset 声明 + `.env.example` 活行 + selfcheck 白名单三处同步，不进 unified 五处）**：判定纯函数 `agentix/domain/policies/evidence_gate.py`——`normalize_gate_mode`（未知值 fail-safe 回 `observe`）+ `decide_evidence_gate`（触发信号任一即触发：grounding 低分 = 报告未跳过且 `score < GROUNDING_LOW_SCORE_WARN`（`0` 关该信号）∪ R1-2 法条引用未核验 `unverified_refs` ∪ 引用无法溯源被剥离 `stripped`）；三档动作：`observe`（默认，与基线逐位等价，触发仍如实记录）/ `warn`（答案尾追加一条「证据核验提示」警告，与 R1-2「引用核验提示」标注文案区分）/ `refuse`（答案整体替换为 `EVIDENCE_GATE_REFUSAL_MSG` 且 citations 清空，落库/推流一致）
- **[接线] `_post_process_and_persist` 两验证器之后**：门禁自身任何故障 fail-open 回基线交付（绝不因门禁拦答案）；审计 `tool_ctx.last_evidence_gate = {mode, action, triggers}` + Prometheus `evidence_gate_total{action}` 计数（`record_evidence_gate`，observe 交付不计）
- **[文档] rag-optimization.md**：状态行改「R-P2-2 已落地 F2b-a、余 4 项待实施」+ §6 R-P2-2 完整注记（键名、三档语义、触发信号、fail-safe/fail-open、审计与指标、守卫指针）
- **[测试] 新增 `tests/test_p83_rag_p2_evidence_gate.py` 25 条**：键三处同步 / 模式归一 fail-safe / 判定全谱（observe 记触发仍 deliver、阈值 0 关信号、分数等于阈值不触发、skipped_reason 不触发、refuse 零触发照常交付）/ 警告文案与 R1-2 头区分 / `_post_process_and_persist` 集成（observe 基线含 R1-2 照常标注、warn 尾部警告、refuse 替换+citations 清空+落库一致、干净答案 refuse 不触发、grounding 低分触发、审计字段落 tool_ctx）/ Prometheus 计数
- 回归：全量 **3592 → 3617 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2a 统一执行链：Memory P2（M-P2-2/4）（2026-10-03）

- **[新增] PreCompact 重注（flag `CONTEXT_PRECOMPACT_REINJECT_ENABLED` 默认关，挂 `CONTEXT_AUTOCOMPACT` 通道）**：`compaction.build_reinject_block` 纯函数——约束优先（五要素 `constraint_key` token 按出现序去重）+ 规则行按声明序补满，`PRECOMPACT_REINJECT_MAX_CHARS=1500` 有界；`ContextEngine`/`SimpleContextBuilder` 超预算触发时**压缩前**采集、块挂 assemble 尾部（不在 `traj_lines` 内 ⇒ 丢弃循环不删、规则必须幸存；计入 token 估算照常收敛），行带 `[precompact:<source>]` 审计标记 + `snapshot.meta["precompact_reinject"]` 来源计数；flag-off 块恒空与基线逐字节等价
- **[新增] 画像作用域（flag `MEMORY_PROFILE_SCOPE_ENABLED` 默认关）**：① 分级单源 `application/services/profile_scope.py`（`usage_habits`=session、`user_constraints`/`legal_domain_prefs`/`conflicts`=stable、`schema_version`=meta，**未知字段 fail-closed 按 stable**）+ 投影纯函数；② 授权键 `{prefix}:profile_scope:{user}` 仅 `granted` 视为授权、**读取异常 fail-closed**；`get_context_for_query` flag-on 扣下稳定层（画像卡稳定字段/频繁领域/语义卡）并审计留痕，flag-off 投影为直通拷贝逐位等价；③ 授权面 `GET/POST /ops/memory/profile-scope`；④ `POST /ops/memory/clear-user` → `clear_user_profile_chain` 分段尝试逐段计数、授权随画像一并撤销、`errors` 非空显式 500（删除面不静默吞错），复活 `clear_user_memories` 收摘要+索引+画像
- **[配置] 两新旋钮五处同步**：settings 默认 False / selfcheck `DEFAULT_IGNORED_MISSING` / `.env.example` `=false` 行 / p41 手写清单 +2 / configuration.md §2 记忆行 + §5 行
- **[文档] memory-optimization.md**：状态行「F2a 已完成 ✓ M-P2-2/M-P2-4」+ §6 两项完整注记（flag 名、fail-closed、端点、审计标记、守卫指针），P2 全部完成
- **[测试] 新增 `tests/test_p82_memory_p2.py` 43 条**：五处同步 / `build_reinject_block` 纯函数（约束优先序、去重、max_chars 两段截断、空源、空行跳过）/ engine+simple 双接线（flag-off 逐字节等价与规则单次出现、flag-on 尾部块挺过丢弃循环+meta 计数、通道门、预算门、无规则仍注约束、reader fail-safe）/ 分级投影全谱 / 授权读写与 `get_context_for_query` 五态（flag-off 不读授权仍全量、未授权扣稳定层、授权全量、读故障 fail-closed、无画像仅卡扣下）/ clear chain 全删计数+中段失败后续段照跑+无授权照删 / ops 端点 200·500
- 回归：全量 **3549 → 3592 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F · F1e 统一执行链：A-P1-4 HITL 进环（2026-10-03）

- **[新增] 进环挂起/恢复（flag `HITL_IN_LOOP_ENABLED` 默认关 = 与基线逐位等价，父闸 `HUMAN_REVIEW_ENABLED`）**：`lg_builder` 在 `add_node("execute_task")` 处包注入壳（langgraph import 留 p66 白名单文件；四条件 = py3.12+ × 本 flag × 父闸 × 非 stream ask 声明的 `configurable.hitl_in_loop` 意图，stream 恒 L2 永不声明），把 `langgraph.types.interrupt` 经 config 传 `task_handler → run_task_execution(review_interrupt=…)`；`task_execution` 在 `run_batch` **之外**消费首个 `review_required`（`task_retry.run_task` 把 BaseException 转 outcome 会吞掉 GraphInterrupt，中断必须在节点内、批外抛出），恢复值 `approve` ⇒ `gate.approve` 落批准后重试一次并把该步 outcome 换成成功，`reject`/未知值/批准失败 ⇒ 终止后续波次回 failed 终态（保持 run_task_execution「只返回 LoopResult 不抛」契约，fail-closed）；py3.10 async `interrupt` 必死锁（test_p32:429-451 特征化）⇒ 版本门控，3.10 诚实降级回 `review_required`（绝不注入绝不调用）；已知边界：LangGraph 节点重入 ⇒ 挂起点之前的步骤恢复后重执行（非幂等写须调用方幂等化或规划为单步）
- **[新增] ② 无 approver REJECT（沿 sidecar 语义）**：executor `_review_preflight` 终局失败收进 `_blocked`——flag-on 且闸开且无 approver 且无决策面 ⇒ `gate.reject` + `reason=no_approver_wired_fail_closed`（不留永远没人批的 PENDING）；有 HITL 桥决策面（新增公开读取点 `human_review.decision_surface_active()`）或 flag-off ⇒ 照旧 `review_required`（flag-off 返回与原字面量相同的结果，逐位等价）
- **[新增] ③ 裁决顺序契约锁定** `deny → hook → ask强制 → allow`（前置风险/guardrail 闸 = deny，hook 可代批但代批仍须复过 ask，allow = 执行点 `consume` 消费；flag-off 路径代码逐字未动）
- **[新增] 恢复面 `POST /ops/hitl/in-loop`**：惰性调 `lg_builder.resume_in_loop`（`Command(resume=…)` + 迟绑定 `graph`，langgraph import 不出 p66 白名单）；非 approve|reject ⇒ 400，未知 thread/无挂起 ⇒ 409；裁决落账由 `task_execution` 消费侧负责
- **[配置] 新旋钮五处同步**：settings 默认 False / selfcheck `DEFAULT_IGNORED_MISSING` / `.env.example` `=false` 行 / p41 手写清单 +1 / configuration.md §2 Agent 行为行 + §5 行
- **[文档] multiagent-optimization.md**：状态行 A-P1-4 改「F1e 已落 HITL 进环」、六缺口⑤ 标注已补、验收「高风险无 approver 必 REJECT」标注落点、灰度顺序 HITL 中途 ✓、§5 A-P1-4 完整注记（含三子句、节点重入边界与守卫清单）
- **[测试] 新增 `tests/test_p81_hitl_in_loop.py` 35 条**：五处同步 / 注入壳四条件门控（flag-off 透传原 config 对象、py3.10 真版本降级分支、3.12 假版本注入）/ Q3 REJECT 双态（flag-on 无通道 REJECT、有决策面 review_required、flag-off 基线逐位、低风险零登记、连续否决不漏 PENDING）/ ③ 顺序契约（hook 收物化请求、代批仍复闸、flag-off 原序）/ 进环管线（approve 落批准重试一次恰一产物、reject 快速失败零副作用、未知值 fail-closed、非 review 失败不挂起、缺省基线语义、链式两步各挂一次、图节点贯通）/ ops 端点（approve|reject 委托、400、409）/ 接线锁（chat_service 意图键、ops 零顶层 langgraph、lg_builder 壳与助手）/ 真图 e2e（skipif py3.12+：挂起 → `Command(resume)` 恢复返回裁决值，py3.12 实测 35 passed）
- 回归：全量 **3515 → 3549 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿；py3.12 并行环境 p81 35 passed）

### Stage F · F1d 统一执行链：A-P1-3 并发工具轮（2026-10-03）

- **[新增] 并发工具轮（flag `AGENT_TOOL_ROUND_CONCURRENCY` 默认 1 = 串行逐位等价）**：一把旋钮共管两面——L2 检索聊天 `chat_agent_runner._execute_tool_calls` 与 L3 `AgentLoop` 批路径（`round_concurrency` 组合根注入，orchestration 保持不 import config 纯编排）；flag-on 批路径顺序 = 授权门（逐调用 `round_gate.preflight_block` 执行点语义 `consume=True`，HUMAN_REVIEW 关闭零副作用恒放行、未获批 fail-closed 落 error 不派发）→ 危险强制串行（`is_dangerous`：有效风险 MEDIUM+ = L1+，含参数提权；危险绝不与同批任何调用并发）→ 剩余步数预算裁剪（并发不放宽 max_steps）→ 批前一次上下文重建 → Semaphore(N) gather（异常按单步路径同款 classify）→ 观测按声明序落位 + steps_used 单调 + ON_STEP_END → 每批至多一次 replan（取声明序首个失败观测）；L2 结果按输入序组装（ToolMessage 落位 = 输入顺序，事件序不变），危险先串行执行、safe 集限并发；依赖步不与前置同批（ready 谓词与 `_next_pending` 同款）；「家政工具不计进度」当前两面无家政在途（chat 5 工具全检索、L3 表 act），机制留口归 F2；flag-off 两面原路径代码逐字未动（守 test_p52 G6 / p74 事件序）
- **[新增] 共享派发闸 `agentix/tools/gateway/round_gate.py`**：`is_dangerous`（判定故障保守按危险串行）+ `preflight_block`（闸件不可用 fail-closed，与 ToolExecutor `_review_preflight` 同款）；两面均函数内惰性 import——retrieval→tools 新边（R6 已知）沿 orchestration/executor.py result_limiter 先例
- **[配置] 新旋钮五处同步**：settings 默认 1（ge=1）/ selfcheck `DEFAULT_IGNORED_MISSING` / `.env.example` `=1` 行 / p41 手写清单 +1 / configuration.md §2 Agent 行为行 + §5 行
- **[文档] multiagent-optimization.md**：状态行 A-P1-3 改「F1d 已落 L2/L3 并发工具轮」、六缺口③ 标注已补、§5 A-P1-3 完整注记（含家政留口与守卫清单）
- **[测试] 新增 `tests/test_p80_multiagent_p1_concurrent_round.py` 33 条**：五处同步武轮 / round_gate 危险判定与授权门双态 / Surface A（flag-off 串行基线、并发与 Semaphore 封顶、危险先串行零重叠、授权门拦截、缺工具与异常 error 回执、入参解析）/ Surface B（串行基线、独立批并发与封顶、依赖不入批、预算裁剪、危险单例批、拦截步 failed Observation、声明序落位、ON_STEP_END 单调、每批至多一次 replan、批前恰一次重建、批内异常分类）
- 回归：全量 **3482 → 3515 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F · F1c-1 统一执行链：A-P1-2 LLM 规划 + TodoWrite（2026-10-03）
- **[新增] LLM 规划器（flag `AGENT_LLM_PLANNER_ENABLED` 默认关，父闸 `AGENT_LOOP_ENABLED`）**：`agentix/policies/planner/llm.py::LLMPlanner` 经 `agent_loop_adapter._select_planner` 注入 AgentLoop——`with_structured_output(LLMPlanOutput)` 结构化输出（temperature=0，沿 `_llm_decompose` 同款）、执行表键集合白名单整步过滤（`tool`→`action` 解析，白名单外整步丢弃；A-P1-2「按任务类型白名单」的执行表交集校验注入口）、步数封顶 `MAX_ITERATIONS`、id 去重与依赖裁剪（未知依赖/自引用裁除）；LLM 调用失败/输出空/全被滤掉一律 fail-open 回 `SingleStepPlanner` 语义（flag-on 也绝不拖垮整轮）；`DeclaredSteps` 确定性 fast-path 原样保留；规划提示携带当前 TodoWrite 快照（`todo_provider` 注入，provider 故障 fail-open 无快照照常规划）；`AGENT_MAX_REPLANS>0` 时 LLM planner 同时注入 `RetryReplanner` 内层（`_select_replanner` 拆出，flag-off 分支与基线逐位等价——不传 `planner` kwarg 保持内层 None）；flag-off 选择器返回与基线同型同参的 `SingleStepPlanner`
- **[新增] `todo_write` 工具（同 flag）**：Codex/Claude TodoWrite 语义——**整体替换**当前清单、`status ∈ pending/in_progress/completed`、至多一条 `in_progress`、50 条/500 字封顶（schema pydantic 与 handler 双侧校验，直连调用同等把关）；`TodoStore`（application 层，`data/todos/current.json` 默认路径）JSON 原子落盘（tmp+`os.replace`）跨进程可恢复、文件损坏按空清单起步 fail-open、落盘失败只告警内存态保留（更新绝不因磁盘故障丢）；回执含全表+版本号即读通道；声明 `L2+control`（名称词表 `write` → 有效风险 HIGH，两层一致沿 `write_task_artifact` 先例）+ `side_effect=True` 禁 auto-retry；`registry_init` 声明与 `main.py` 装配成对 flag 门控（`install_todo_write_tool` 自身 belt-and-suspenders 再判一次 flag 并 `ensure_registered()`），`Plan 工具` = 既有 `enter/exit_plan_mode`（A-P2-4 已完成不重复立项）
- **[文档] multiagent-optimization.md**：状态行更新（A-P1-2 部分完成→F1c 已落 LLM 规划+TodoWrite，余按任务类型分型细化；A-P1-3/A-P1-4 分别标注 F1d/F1e 待施）+ §5 A-P1-2 完整注记 + 六缺口②标注；configuration.md §2 Agent 行为行 + §5 新增 `AGENT_LLM_PLANNER_ENABLED` 行
- **[修复] `test_notice_never_contains_config_values` 假阳性**：随机 `config_hash`（hex）偶发含数字子串（如 "21"）导致误报——断言改为剔除哈希字段后再做值子串检查（语义不变，仍精确命中 event/changed_keys 泄值）
- **[测试] 新增 `tests/test_p79_multiagent_p1_llm_plan.py` 36 条**：五处同步棘轮 / 选择器双态与 replanner 内层注入 / LLMPlanner 全谱（成功、白名单丢步、三类 fail-open、去重裁依赖、步数封顶、Todo 快照进提示与 provider 故障、adapter 端到端）/ 声明契约（L2+control+title）/ registry 双态与 main 源级门控 / executor 端到端回环与四类校验错误 / TodoStore（整体替换、版本、恢复、损坏、落盘故障、清空、直连 handler）
- 回归：全量 **3446 → 3482 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F · F1b2 统一执行链：M-P1-3/4/5 残余三项（2026-10-03）
- **[新增] M-P1-3 MEMORY 索引制（flag `MEMORY_INDEX_ENABLED` 默认关，挂 `MEMORY_CONTEXT_ENABLED` 通道）**：记忆注入段升级为「段落头保留 + 正文 `obs://` 指针索引」——正文行经 `offload_content`（kind=`memory_topic`，强制外置、rebuildable）落 ArtifactStore，入口只留 `- {预览≤100字} → obs://{artifact_id}` 索引行；常驻预算 `MEMORY_INDEX_MAX_CHARS=1500`（常量决策，与指令层 12k 同范式）超限截索引尾部附提示；外置降级该行保留原文（fail-open 不丢记忆），索引变换自身异常回原文；取回端 = `read_artifact`（`obs://` 前缀自动剥离），`MEMORY_INDEX_ENABLED` 单开（TASK_EXEC 关）也声明并装 read handler（registry gate 改为两 flag 任一、main.py 独立接线块、handler 拆出 `install_artifact_read_tool` 幂等复用）；flag-off 注入与基线逐位等价
- **[新增] M-P1-4 AGENTS 级联（flag `AGENTS_CASCADE_ENABLED` 默认关）**：`build_instructions` 升级为「settings → skills → 全局 `~/.agentix/`（常驻）→ 项目根（常驻）→ 子目录（消息触达才加载）→ 会话」六层——全局层 flag-on 才读（`global_start` 可注入，读失败给空）、子目录层 `mentioned_instruction_dirs` 从消息解析类路径 token（存在 + 项目边界内 + 去重 + 上限 8）、逐目录向上找最近约定文件（根级命中由既有 hash 去重丢弃），预算/去重/截断全沿既有 4k/12k 机制；生产接线 `agent_loop_adapter` 传 `subdirs=mentioned_instruction_dirs(message)`（flag-off 返回 `()` ⇒ 输出与基线逐字节一致）；配套 L0 只读工具 `read_agents_file`（规划新建已落地 `agentix/tools/adapters/agents_file_read.py` + `agentix/agent/context/install.py`，四步接线 = 声明/校验/组合根/`main.py`，项目边界内向上查找最近约定文件，未命中结构化 `found=false` 回执，正文沿 4k 截断）
- **[新增] M-P1-5 obs:// 隔离接线半程（flag `CONTEXT_ISOLATE_PERSIST_ENABLED` 默认关）**：`ContextEngine` 超长观测隔离从「字符串替换的悬空引用」升级为真正外置——flag-on 正文经 `offload_content`（kind=`agent_step`）落 ArtifactStore，`isolated_refs` 与截断标记落 `obs://{artifact_id}`（经 `read_artifact` 可取回，与记忆索引指针共用同一条取回通道）；外置失败/降级 fail-open 回 `obs://{step_id}` 裸引用（隔离绝不抛错）；flag-off 隔离引用与基线逐位一致（守卫 `test_agent_context_budget` 冻结语义不变）；`read_artifact` handler 增 `obs://` 前缀剥离 + 描述补指针取回语义
- **[守卫/配置] 3 新旋钮五处同步**（settings 默认关 / selfcheck `DEFAULT_IGNORED_MISSING` +3 / `.env.example` 3 活跃 `=false` 行 / p41 手写清单 +3 / configuration.md §2 记忆行 + §5 三行）；memory-optimization.md 状态行与 §5 三条注记（M-P1-3/4/5 均改「F1b2 已落地」并给出机制细节）
- **[测试] 新增 `tests/test_p78_index_cascade_isolate.py` 35 条**：3 键五处同步 / 记忆索引（flag-off 原文注入、嵌套闸零 LTM、段落头保留 + 3 topic 外置取回回环、长正文预览封顶、降级留原文、预算 130 截尾、端到端注入→`read_artifact` 指针取回、索引异常回原文、MEMORY_INDEX 单开也声明取回端）/ AGENTS 级联（flag-off 忽略两新参、六层序、子目录根命中去重、settings 读取、触达解析四态 + limit、title/校验契约、声明与 handler 闸 + 审计成对、边界内取回与结构化 miss、生产接线源断言）/ obs 隔离（flag-off 裸引用逐位、flag-on 外置可取回且头部保留、降级回裸引用、`obs://` 前缀取回与未知 ID fail-fast、源断言）
- 回归：全量 **3411 → 3446 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿，门禁 883 passed）

### Stage F · F1b 统一执行链：Memory P1 接线（M-P1-1/2/5，2026-10-03）
- **[新增] M-P1-1 向量召回（flag `MEMORY_VECTOR_RECALL_ENABLED` 默认关）**：进程内 provider `InMemoryMemory.recall` 升级为「关键词 × 向量」双路 + recency 衰减（半衰期沿 `MEMORY_RECENCY_HALF_LIFE_DAYS`，TopK 沿 `MEMORY_RECALL_LIMIT`）——纯策略落 `agentix/domain/policies/memory_recall.py`（`cosine_similarity`/`recency_weight`/`fuse_recall_scores`，稳定排序、双通道加权 `(0.5·kw + 0.5·vec) × recency`、融合分 0 不返回保持「只给命中」语义）；embedding 复用检索域 `embed_texts_sync`（懒 import + 1.0s 内部预算 < MemoryManager provider 1.5s 超时，防「向量慢把关键词一并拖降级」）；embedding 失败/超时/空向量 ⇒ 打点后落回纯关键词路径（验收「断向量服务仍可答」）；写路径 best-effort 挂向量、`forget` 随删清理；flag-off 召回与基线逐位等价（零 embedding 零 `_vectors` 开销）；读路径二次脱敏（M-P0-3）与 scope 隔离在向量路径同样生效
- **[新增] M-P1-2 后台评审 LLM 路由 + turn-end 生产者（gate 沿既有 `MEMORY_REVIEW_ENABLED` 默认关）**：`memory_review.route_memory_item_llm`（`REVIEW_ROUTE_PROMPT` 只认 `user_profile`/`episodic` 两词，LLM 不可用/解析失败 ⇒ 规则路由兜底 reason 标 `fallback-rule:` 前缀，honest 可审计）；`review_items` 增 `router` 可选参（缺省/路由器异常走规则，既有契约零变化）；`schedule_review_for_summary` 增 `use_llm`；新生产者 `ChatService._maybe_schedule_turn_review`（turn 文本 fire-and-forget，调度失败只打点）三接线点 = ask 成功 / stream 正常 done / stream 兜底 done（错误路径不评审）
- **[新增] M-P1-5 真分词/真计数半程（flag `CONTEXT_REAL_TOKEN_COUNT_ENABLED` 默认关）**：`agentix/agent/context/token_count.py` `count_tokens`——flag-off ≡ `len//4` 与全部既有估点逐字节一致；flag-on 走 tiktoken cl100k_base（进程内懒加载一次 + 负缓存，钉版 requirements `tiktoken==0.13.0`），tokenizer 缺失/编码失败退 CJK 感知启发式（永不打断上下文构建）；接线 7 处估点（`context/engine.py` ×3、`context/simple.py` ×3、`context/compaction.py` ×1）；obs:// 超长记忆接线与 M-P1-3/4 归 F1b2
- **[守卫/配置] 2 新旋钮五处同步**（settings 默认关 / selfcheck `DEFAULT_IGNORED_MISSING` / `.env.example` `=false` 两行 / p41 手写清单 +2 / configuration.md §2 记忆行 + §5 两行）；p74 `RETRIEVAL_BYPASS_ALLOW` 棘轮随迁 +1 对（`agentix/agent/memory/service.py → agentix.retrieval.infra.rag.dense`，批次评审入册，零僵尸）；memory-optimization.md 状态行与 §5 三项 F1b 注记
- **[测试] 新增 `tests/test_p77_memory_p1_wiring.py` 51 条**：flag 五处同步 ×2 键 + requirements 钉版 / 融合纯策略已知值（cosine、半衰期 0.5、双通道 > 单通道、recency 乘法、平分稳定序）/ 向量召回（flag-off 零 embedding、无子串语义召回、双通道排序、embedding 失败与空向量 fail-open、写失败仍存、PII 脱敏、recency 降序、forget 清向量、scope 隔离）/ LLM 路由（双目标解析、失败与乱输出回退规则、prompt 渲染、router 参数化与异常回退、`use_llm` 调度路径与原契约保持）/ turn-end 生产者（flag-off 零调度、flag-on 文本与 `use_llm=True`、调度异常吞掉、三接线点源断言）/ 真分词（flag-off ≡ `len//4` 参数化、flag-on ≡ tiktoken、tokenizer 与编码失败退 CJK、7 估点源断言、builder flag-off 不变）
- 回归：全量 **3360 → 3411 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿，90 条门禁全绿）

### Stage F · F1a 统一执行链：RAG P1 真 Hybrid（R-P1-1..5）（2026-10-03）
- **[修复] 检索后端元组解包（R-P1-5，无条件随行）**：faiss/milvus/qdrant 三个真实后端 `similarity_search` 返回 `(doc, score)` 元组，`_similarity_search_with_fetch` 的 else 分支曾整包当 doc——走真实后端且 fetch 未命中即 `AttributeError: 'tuple' object has no attribute 'metadata'`；新增 `_as_page_content_doc` + 元组解包、真实分数流入 scored pairs，连带 `RETRIEVAL_MAX_DISTANCE=1.2` 距离阈值由「从未生效」变为实际过滤（非元组元素仍 score 0.0 透传旧行为）
- **[新增] 融合纯策略与过滤谓词**：`agentix/domain/policies/rank_fusion.py`（`rrf_fuse` k=60 / `zscore_fuse` alpha，首通道加权、std=0 记 0、稳定排序、纯 stdlib 零 retrieval 依赖——Channel A/B 共用，规避 p74 application→retrieval 引用面）；`chat_scope.meta_matches_filter`（None/dict(值 list=IN)/callable，与 `build_faiss_filter` 同语义，异常 fail-closed）
- **[新增] 进程内 BM25 稀疏通道**：`retrieval/infra/rag/sparse.py`——`tokenize`（ASCII 词 + CJK 一元/二元）、`BM25Index`（Okapi k1=1.5 b=0.75、按 chunk_id 映射）、`_load_index`（kb_id TTL=600s 缓存、容量 8、锁外加载）、`sparse_search`（filter 复算同口径、min_score 过滤、过取 2x 截断、加载失败 fail-open 返回 []）
- **[新增] 四阶段 Hybrid 编排**：`retrieval/usecases/retrieval_hybrid.py` `retrieve_hybrid_docs`——rewrite→dense（`to_thread` 调 `retrieve_docs_with_retry`，channel_n=max(search_k, min(FUSION_TOP_N, search_k*4))）→sparse（子闸门 / 无 session 跳过 / filter_in_force 同口径）→融合（rrf/zscore 按 chunk_id 融合、dense 优先占位、融合分写回 `_retrieval_score`）→可选 rerank（候选截 `RETRIEVAL_RERANK_CANDIDATES`，双层 try/finally fail-open + 熔断状态记报告）；ContextVar 阶段报告（`set_stage_report`/`pop_stage_report`）+ 事后 `span("rag.hybrid_stages", **attrs)` 结构化轨迹；`retrieve_scoped_docs_cached` 增 `session` kwarg + hybrid 分支 + 缓存键 `|h1` 标记隔离
- **[变更] SSE 四阶段进度帧**：preflight 前清报告、preflight 后 pop，有报告即追加 `{type:progress, stage:"retrieval", detail:{step:2,total:3,stages:[...]}}` 事件（进度冻结测试三帧基线不受扰）；三调用点补传 session（`chat_service_helpers` preflight / `chat_agent_tools` `ctx.session` / `retrieval_port_adapter`）
- **[变更] Channel B 本地检索融合**：`local_search_node.fuse_local_channels`（rrf k=60 / zscore alpha=0.5，chunk 内容哈希去重、pg 侧优先），`KB_LOCAL_FUSION_ENABLED` 门控——关时走旧 postgres-first 逐位不变
- **[配置] 12 新旋钮全部默认关**：retrieval settings 10 键（`RETRIEVAL_HYBRID_ENABLED`/`RETRIEVAL_SPARSE_ENABLED`/`RETRIEVAL_SPARSE_MIN_SCORE`/`RETRIEVAL_SPARSE_CACHE_TTL`/`RETRIEVAL_FUSION_METHOD`/`RETRIEVAL_RRF_K`/`RETRIEVAL_FUSION_ALPHA`/`RETRIEVAL_FUSION_TOP_N`/`RETRIEVAL_HYBRID_RERANK`/`RETRIEVAL_RERANK_CANDIDATES`）+ 统一 settings 2 键（`KB_LOCAL_FUSION_ENABLED`/`KB_LOCAL_FUSION_METHOD`）；`.env.example` 熔断块后 12 行注释块、configuration.md §5 两行（仅 KB 键，retrieval 专属键不进文档纪律保持）、p41 手写清单 +2
- **[测试/文档] 新增 `tests/test_p76_rag_p1_hybrid.py` 27 条**：配置默认值 / rrf+zscore 已知值 / tokenize+BM25+作用域+缓存 / 后端元组解包与阈值生效 / 四阶段链路与融合顺序（flag 开关、缓存 `|h1` 隔离、rerank fail-open）/ ContextVar 报告交接 / SSE 四帧 / Channel B 融合与门控；`docs/design/rag-optimization.md` 状态行改「P1×5 已完成」+ §5 各项 F1a 注记
- 回归：全量 **3333 → 3360 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿，90 条门禁全绿）

### Stage M · M6 统一执行链：删三簇 shim + 全部旧路径清尾（2026-10-03）
- **[移除] 三簇 strangler shim 整批删除**（56 文件 + 空目录）：`agentix/infrastructure/tools/`、`agentix/mcp/`、`agentix/capabilities/tools/` 三簇 + `agent/` 执行件七目录（`core`/`executor`/`multiagent`/`planner`/`replanner`/`verifier`/`hitl`）+ `application/agents/lg_{builder,states,supervisor}.py` + `application/services/{control_plane,hitl_bridge}.py` + `domain/agent_types.py` + `domain/ports/agent_core.py` + M5 两件旧路径（`retrieval/domain/policies/legal_effectiveness.py`、`retrieval/domain/services/grounding_verify.py`）；全仓模块级 `_TARGET` 转发常量归零
- **[变更] codemod 全仓 import 改指规范位**：113 文件 / 384 处（`PREFIX_MAP` 按 p70 `SHIM_MAP` per-file 映射 + infrastructure/tools 链式 + `NAME_WISE` 生命周期/步骤/预算名收敛 + `capabilities.tools` 特例 `human_review→tools.gateway`、`guardrails→policies.guardrails`）；手工面：`domain/ports/__init__` 七端口改从 `domain.agent.ports` 再导出、`domain/status` shed `ExitStatus`/`ExitReason`（只余 `RequestStatus`）、`runtime/{state,steps,budget}` shed 对应名（`BudgetState` 保持规范位同一）、`capabilities/__init__` 去 tools 六名 re-export、`agent/__init__` 惰性门面 string 改指 orchestrator、phase12/20/30 `touched_paths`、ablation `WIRED_FLAGS` 斜杠路径、10 处 `from ...application.agents import lg_*` package-form 改指 `orchestration.langgraph`、2 处 hitl_bridge/control_plane package-form
- **[守卫翻转]**：p36 `_TARGET` 白名单条目删除（同名异值清单余 `GUARDRAILS_SYSTEM_PROMPT` 一条）；p66 `SHIM_DIRS_ALLOWED` → **空集终态**（守卫 #12「迁移期白名单相等 → 终态空集」达成）+ 守卫 #3 增 `TOOLS_GUARDRAILS_EDGE_ALLOWED = ("agentix.policies.guardrails",)` 窄豁免（`tools/gateway/executor.py` 删 shim 后直连规范位 guardrails，蓝图 §5 Tool Gateway 链含 Guardrails 步，PROJECT_SPEC §9.5 M3 注记的「M6 清尾时同步调整守卫与 import」落定）；p69 `OLD_PATHS` 存在性断言翻转为 `SHIM_PATHS_DELETED_M6` 负向 + 同一对象断言拆分为「shed 名已收敛 / 存活名规范位同一 / 已删模块 ImportError」三组 + `COMPLETED_STAGES` 登记 M6；p70/p71/p72/p73 逐文件断言翻转（shim `_TARGET`/别名式形态 → 文件删除 + import `ImportError` + 规范位单一入口 + monkeypatch 只落规范位 + 加载序子进程改「旧路径断、规范位通」）；p74 M5 口径已随 M5 翻转
- **[测试] 新增 `tests/test_p75_m6_shim_removal.py` 30 条**：零 `_TARGET` equality 棘轮（agentix/ 生产源 AST 模块级扫描 == 0）、20 个旧 dotted 前缀静态 import 面 == 0（`agentix/` + `tests/` + `scripts/` + `evals/` + `kb_ingest/` + 根入口 `*.py`；archive 冻结历史不入面）、19 个删除面 + 5 dotted `ImportError` 参数化、生产源零旧斜杠路径字符串（tests 留档清单不入面）、门面消失三断言（`capabilities` 无 `tools` 属性、`import agentix.agent` 不拖旧子包（子进程 `sys.modules` 检查）、`domain.status` 单一来源）
- **[顺手修] 生产源历史路径措辞**：`domain/agent/types.py`、`orchestration/langgraph/lg_builder.py`、`tools/{adapters,gateway}/__init__.py` 四处 docstring/注释的旧斜杠路径改 dotted/删除口径（保 p75 棘轮为零）；p69/p73 负向断言改 `importlib.import_module` 动态化（保静态 import 面为零）
- **[文档]**：docs sweep 14 文件——current 文档（architecture/design/guides/runbooks/PROJECT_SPEC）27 条 backtick 旧路径 repoint 规范位、历史文档（module-inventory、Phase21-26/28-31/32-34、蓝图、source-code-study）去反引号 + `（M6 已删，现见 new）` 注记；PROJECT_SPEC 手工 16 处：§2.4 shim 表改「M6 已删、留档迁移期形态」口径、`capabilities/__init__` 行（13 行 re-export → 7 行 docstring）、Strangler 链段、§9.5/§11.1/防御常量表/目录结构表时态、`PROTECTED_VERIFIER_PATHS` 表行（双护 → 终态 4 条）；README 测试真源 3305→3333
- 回归：全量 **3305 → 3333 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check .` 全绿，81 条门禁 + p36 共 90 条全绿）

### Stage M · M5 统一执行链：检索域收编（剥离 ReAct + 单门 + 共享域）（2026-10-03）
- **[变更] 执行链剥离改造**：`retrieval/usecases/chat_agent_runner.py`（349 → 391 行）剥离 langgraph（删 `create_agent` 版本自适应、`GraphRecursionError`、`_stream_agent_plan_e` 与 `stream_mode=["messages","updates"]` 消费），换 `_stream_native_rounds` 手写 capability 内部工具循环（`llm.bind_tools(...).astream` 聚合 `AIMessageChunk.tool_call_chunks` → `.tool_calls` + `tool.ainvoke(ToolCall)` → `ToolMessage`，工具异常转 error ToolMessage 回注继续）。**事件契约四 kind 一字不变**（`AgentStreamEvent` / `chat_service._stream_agent_and_persist` 消费零改动，p65 词汇单轨）；事件时序复刻旧路径：缓冲轮 `model_usage`（工具执行前，供输入预算检查）→ `tool_running`（`announced` 按工具名跨轮去重、仅 deep）→ 执行；已跑工具轮文本 delta 实时流出、轮末 `model_usage` 滞后对齐 updates 语义。**顺手修双调 bug**：首轮无工具 = 单个 `answer_delta` 一次模型调用（旧路径 messages 闸门关死 → fallback `agent.ainvoke` 二次调模型且 usage 计 0）；步数（模型步 + 工具步，复刻 langgraph super-step 粒度）对照 `AGENT_RECURSION_LIMIT`（默认 25，函数内读取）超限给 `_RECURSION_HINT` + `terminated(recursion_limit)`；流失败 fallback 改 `_ainvoke_rounds` 非流式多轮，终文单 delta **带真实 usage**（旧 fallback 从 state dict 取 usage 恒 (0,0)，预算检查此前实为死代码，现可核算）
- **[变更] Port 装配单门收敛**：`knowledge_gateway` 顶层增 `get_retrieval_port()`（应用侧取 `RetrievalPort` 的唯一装配入口，application 内唯一触碰 `retrieval_port_adapter` 的文件）；`file_handler` / `general_handler` / `kb_tools/node` 三处散点 import 改指网关（调用点同步 `create_retrieval_port()` → `get_retrieval_port()`）
- **[变更] 纯策略件迁共享域**：`retrieval/domain/policies/legal_effectiveness.py`（208 行）与 `retrieval/domain/services/grounding_verify.py`（201 行）迁 `agentix/domain/policies/`（两件纯 stdlib，零 retrieval 依赖）；旧路径 = `sys.modules` 别名式 shim（M2 定型，新旧同一模块对象，p42/r2/r3 旧路径测试零改动）；外部引用改指新家：`kg_sub_graph/cypher_tools/node`（1 处）、`policies/verifier/layered`（1 处）、`retrieval/usecases/chat_service`（5 处函数内延迟 import）、`retrieval/domain/policies/__init__` re-export 源
- **[变更] 进化保护顺手修**：`agentix/agent/evolution/loop.py` `PROTECTED_VERIFIER_PATHS` 此前仍指 M3 旧路径（`agentix/agent/verifier/*` shim），**真实 `agentix/policies/verifier/*` 未受防线 3 保护**——补齐 M3 实体 + M5 新家（`domain/policies/grounding_verify.py`）并保留旧 shim 双护（M6 拆 shim 时同步收缩）
- **[测试] 新增 `tests/test_p74_m5_retrieval_unification.py` 25 条**：原生循环金测（事件时序四场景、单 delta + 单次模型调用、deep/fast tool_running 语义与去重、冻结工具文案、输入/输出预算终态且工具不跑、轮次上限提示、流失败 fallback 带 usage、双失败错误兜底、工具异常回注、kind 词表冻结）、runner 与整个 retrieval 包 langgraph-free AST、单门三断言（装配单点 / 三调用方改指 / 返回 Adapter 实例）、**旁路清零 equality 棘轮**（包外 `agentix.retrieval.*` 静态 import 面 == 13 条 (file, module) 豁免：main 组合根 5 + 网关 2 + settings 配置面 4，零新增零僵尸）、共享域迁移四断言（同对象 / `_TARGET` / 零依赖 / PROTECTED 覆盖）
- **[守卫随迁]**：p66 `LANGGRAPH_ALLOWED` 删 chat_agent_runner 行（11 → 10，equality 断言）+ `SHIM_DIRS_ALLOWED` 增 `retrieval/domain/{policies,services}` 两目录；p69 `COMPLETED_STAGES` 登记 M5 + MIGRATION_MANIFEST 增 2 条 M5 迁移条目（参数化随清单 +2）；p36 `_TARGET` 注释补 M5 配对谱系
- **[文档]**：PROJECT_SPEC §9.5 runner 节按 391 行新实现重写（含三轮时序图式条目）、§22.1 生成步去 `create_agent`、L3 grounding 路径指新家、进化防线常量表更新（行号 +9）、knowledge_gateway 方法表行号刷新并记 `get_retrieval_port`、白名单计数 11→10；README 测试真源 3278→3305；dependency-map LangGraph 出现点表移除 runner 行（附 M5 注记）+ Verifier 簇 grounding 行指新家；`chat_service` 模块 docstring 去 `create_agent`；蓝图为规划口径、module-inventory/Agent_Core_2.0_规划/current-tree 按历史口径不动
- 回归：全量 **3278 → 3305 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check agentix tests scripts evals` 全绿，81 条门禁 + p36 共 90 条全绿）

### Stage M · M4 统一执行链：控制平面 → runtime/control_plane.py（2026-10-02）
- **[变更] 规范位落成**：`application/services/control_plane.py`（321 行）迁往 `agentix/runtime/control_plane.py`（蓝图 §5「Run registry / cancellation 归位」）——`ControlPlane`（spawn/bind/状态迁移/cancel 真打断 asyncio.Task/`check_admission` 并发与深度准入 fail-closed/有界 FIFO 淘汰）、`AgentRun`、`AdmissionDenied`、单例 `get_control_plane`/`reset_control_plane` + M0 `RunState`/`TERMINAL_STATES` 再导出（p69 契约经 alias shim 对象同一保持）。**唯一包内改写**：`TokenBudget` 模块级 import 收进 `if TYPE_CHECKING`（两处字符串注解 + `to_dict()` 鸭子类型，运行期零引用）——Runtime 簇首次纳入跨簇文件而不破「模块级 agentix 边只到 runtime/domain/config」自包含不变量（先例 `runtime/state.py` 对 `steps` 的 TYPE_CHECKING 回指）；`config.settings` 本就在 `get_control_plane()` 内延迟读取
- **[变更] 旧路径降级 shim**：`application/services/control_plane.py` = **`sys.modules` 别名式**转发（M2 定型形态；旧/新同一模块对象，phase25 `monkeypatch.setattr(cp_mod, "get_control_plane", ...)` 形态与 `from agentix.application.services import control_plane` 包形态皆零改动通过）；`runtime/__init__.py` **刻意不加汇总导出**（避免包面膨胀与 eager 加载序扰动），仅 docstring 记入 control_plane 条目与 shim 说明
- **[测试] 新增 `tests/test_p73_m4_runtime_control_plane.py` 21 条**：目标位/旧路径 alias 形态、7 符号同一（含 p69 RunState/TERMINAL_STATES 再导出契约）+ 模块同一 + 包形态 import 解析、monkeypatch 回滚契约（裸 sentinel + `get_control_plane` phase25 形态）+ 单例注册表跨路径共享、Runtime 自包含（全包模块级 AST 扫描仅许 runtime/domain/config + control_plane 的 token_budget 仅 TYPE_CHECKING + `AgentRun.budget` 惰性字符串注解可无 TokenBudget 构造）、双向加载序 3 子进程
- **[守卫随迁]**：p69 `COMPLETED_STAGES` 登记 M4（MIGRATION_MANIFEST 条目 M4 翻转验证）、p36 `_TARGET` 注释补 M4 配对；p66 `SHIM_DIRS_ALLOWED` 的 `application/services` 目录 M3 已在册，无需再增
- **[文档]**：PROJECT_SPEC 顶层包表 `runtime/` 行记 M4 迁入、§11.1 增 M4 段 + 规范位/shim 两表行、全文 3 处路径 replaceAll；README:107 Control Plane 行、`runtime-guardrails.md` 控制平面行指新家；dependency-map Runtime 簇表增 control_plane 行 + 「运行期自包含」结论改写（两处惰性跨簇边：runtime.py→observability 函数内延迟、control_plane→token_budget TYPE_CHECKING）；module-inventory §Plan 期 defer 行与 Phase21-26/蓝图按历史/计划口径不动
- 回归：全量 **3257 → 3278 passed / 14 skipped / 0 failed**（2026-10-02，`ruff check agentix tests` 全绿，81 条门禁全绿）

### Stage M · M3 统一执行链：策略件 → 顶层 policies/（2026-10-02）
- **[变更] 规范位落成**：7 个策略模块迁往 `agentix/policies/`——`planner/`（`simple.py` 自 `agent/planner/`、`retry.py` 自 `agent/replanner/` **归并**入本包，蓝图 §5 裁决；门面 4 导出 SingleStep/DeclaredSteps/RetryReplanner/failure_key）、`verifier/`（layered + structural，自 `agent/verifier/`）、`hitl/`（`sidecar.py` 自 `agent/hitl/`、`hitl_bridge.py` 自 `application/services/`，26 KB 执行件）、`guardrails/guardrails.py`（自 `capabilities/tools/`，`__all__` 14 名由 `policies/guardrails/__init__` re-export 保旧 import 面）。迁移文件内 import 改写仅 2 处（hitl_bridge 的 `agentix.agent.hitl`、layered 的 `agentix.agent.verifier.structural`），外部依赖逐字保留（hitl_bridge 的 `capabilities.tools.{definitions,human_review,executor}`、`application.services.breakpoint_resume`；layered 的 `retrieval/...`）。与 `agentix/domain/policies/`（纯契约值对象）分界：本包只放**有实现**的执行件，包门面零 import 防半初始化窗口
- **[变更] 旧路径降级 shim**：7 个模块 = **`sys.modules` 别名式**转发（旧/新路径同一模块对象，monkeypatch 打得到真实实现，M2 定型形态）+ 4 个包门面 PEP 562 惰性（`agent/{planner,replanner,verifier,hitl}/__init__.py`，`_EXPORTS` 与迁移前逐字一致——`agent/planner` 门面不因归并扩面，`agent/replanner` 门面继续只导 Retry 两件）；`guardrails` 的 `_TARGET` 指 `agentix.policies.guardrails.guardrails`（非包，与其余 6 件同为模块级别名）
- **[测试] 新增 `tests/test_p72_m3_policies_migration.py` 53 条**：目标位 12 文件 + 新家非壳断言 + `policies/__init__` 零 import + domain/policies 分界、7 shim `_TARGET`/别名式形态 + 7 组旧/新同模块对象、4 包门面惰性/导出面逐字一致 + 4 组包级导出符号同一、planner 归并面（新包 4 符号齐、旧门面 2 符号不扩面）、monkeypatch 经旧路径打到真实模块回滚契约（hitl_bridge + guardrails）、policies 自足 AST（禁引 6 个 M3 旧路径前缀；hitl_bridge 外部依赖显式允许）、executor 保留旧路径断言、双向加载序 4 子进程
- **[守卫随迁]**：p66 `SHIM_DIRS_ALLOWED` 增 5 目录（`agent/{planner,replanner,verifier,hitl}`、`application/services`；`capabilities/tools` 原已在）、p69 `COMPLETED_STAGES` 登记 M3、p36 `_TARGET` 注释补 M3 配对；**p70 三处 M3 期断言翻转**：`test_target_does_not_contain_guardrails` 注释更新、`TestGuardrailsStayedForM3` → `TestGuardrailsMigrated`（capabilities 侧 `_TARGET` 指 policies、新家为真实实现）
- **[顺手修]**：`tools/gateway/executor.py` 三处 delayed guardrails import 一度切新路径，**回退保留旧路径**——p66 守卫 #3 禁止 tools/（按能力层扫描）import 上层 policies；经 shim 转发行为等价，M6 清尾时同步调整守卫与 import（蓝图 L5 链含 Guardrails 步，届时一并裁决）；README:136 架构表顺手修正 M2 遗留的 `executor/` 旧路径行
- **[文档]**：PROJECT_SPEC 顶层包表增 `policies/` 行 + `agent/` 行再收缩、§2.4 shim 表增 M3 行（capabilities 行改 17 转发）、§2.5 L3 行改「M3 起策略件在 policies/」、§11.1 增 M3 段 + 规范位/shim 两表行、全文 6 处路径 replaceAll；README:136 架构表（规划/执行/验证/HITL 四行指规范位）、layers / research-agent-demo（含代码块 dotted import）/ interview 同步；module-inventory §1.3 历史清单与 multiagent-optimization（2026-10-01 快照）按既定口径不动
- 回归：全量 **3203 → 3257 passed / 14 skipped / 0 failed**（2026-10-02，`ruff check agentix tests` 全绿，81 条门禁全绿）

### Stage M · M2 统一执行链：编排件 → 顶层 orchestration/（2026-10-02）
- **[变更] 规范位落成**：9 个编排模块迁往 `agentix/orchestration/`——`orchestrator.py`（AgentLoop，自 `agent/core/loop.py`）、`executor.py`（MappingStepExecutor + `classify_exception`，自 `agent/executor/executor.py`，目录收敛为单文件）、`langgraph/`（lg_builder / lg_states / lg_supervisor，自 `application/agents/`；**业务 prompt `lg_prompts.py` 按 §5 裁决留 application**，builders 保留上行引用）、`strategies/multi_agent/`（bus / manager / peer，自 `agent/multiagent/`）。包内 import 全改新路径切断旧依赖（loop→executor 延迟 import、supervisor→multiagent/builder/states 重写；`lg_prompts`、handlers、`infrastructure.tools.registry_init` 等外部依赖逐字保留），三个空门面 `__init__` **刻意零 import**（lg_builder ↔ lg_supervisor 模块级互引环 + 经 `agentix.agent` 的回拉需惰性窗口）；`guardrails` 等余项仍属 M3
- **[变更] 旧路径降级 shim，形态分两种**：8 个模块 = **`sys.modules` 别名式**转发（旧/新路径 import 拿到**同一模块对象**——迁移中发现 M1 式代理 shim 上 `monkeypatch.setattr`/`patch.object` 会静默打不进真实实现，phase19 4 条 + p12 4 条直接跑真网络地址而红，改别名式后原测试零改动全绿）+ `agent/multiagent/__init__.py` PEP 562 惰性门面（16 导出符号同一）；`agentix.agent.__init__` 的 `AgentLoop/LoopResult` 再导出同步**改惰性**（orchestrator 先加载序下，其顶部 `from agentix.agent.hooks import ...` 触发包初始化会沿 shim 回拉半初始化 orchestrator 炸 AttributeError，p71 双向加载序子进程测试钉死）
- **[测试] 新增 `tests/test_p71_m2_orchestration_migration.py` 55 条**：目标位 13 文件 + 新家非壳断言 + 3 空门面零 import、8 shim `_TARGET`/别名式形态 + 8 组旧/新同模块对象、agent 包惰性（AST + 子进程不拖 orchestrator）、符号同一（loop/executor/multiagent 三组）、monkeypatch 经旧路径打到真实模块的回滚契约（phase19/p12 回归钉）、orchestration 自足 AST（禁引 6 个 M2 旧路径前缀，`lg_prompts` 上行引用显式允许）、lg_prompts 留守为真实文件、双向加载序 4 子进程
- **[守卫随迁]**：p66 LangGraph 白名单 3 路径 `application/agents/lg_*` → `orchestration/langgraph/*`（equality 11 文件不变，头注记 M2 批次刷新）、`SHIM_DIRS_ALLOWED` 增 4 目录（`agent/{core,executor,multiagent}`、`application/agents`）、p69 `COMPLETED_STAGES` 登记 M2、p36 `_TARGET` 注释补 M2 配对；**源码扫描测试随实体指新家**：p41 两枚图节点计数测试、p02 wiring 1 处、p24 deadcode 3 处（`LG_BUILDER_PATH` 等），其余外部 importer（router/handlers/chat_service 等 30+ 文件与 20+ 测试）经 shim 零改动
- **[顺手修复]** M1 16 个模块 shim 的 f-string 转义笔误（`{{_TARGET!r}}` 双花括号使错误消息输出字面花括号）；`agentix/application/agents/lg_builder.py:332` 等行号引用随逐字迁移保持有效
- **[文档]** PROJECT_SPEC：顶层包表新增 `orchestration/` 行 + `agent/` 行改写为 Context Provider 家、§2.4 shim 表补 capabilities/tools（M1）与 M2 两行、§2.5 两套编排辨析重写（L3 推进权两分家 + LangGraph 允许位置改 p66 白名单口径）、§11.1 增 M2 规范位/shim 段与两行、全文 21 处路径 replaceAll；README:136、layers.md、dependency-map、module-inventory（executor/lg_builder/符号表 4 处）、routing quick reference、research-agent-demo、interview 同步
- 回归：全量 **3148 → 3203 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁全绿

### Stage M · M1 统一执行链：capabilities/tools → 顶层 tools/（2026-10-02）
- **[变更] 规范位落成**：16 个工具模块迁往 `agentix/tools/`——`gateway/`（executor / result_limiter / human_review / kg_gateway）、`adapters/`（search / notify_user / memory_tools / cron_tools / artifact_read / task_writes / plan_mode / integrity / tool_search）、**顶层**注册面（definitions / registry_init / selection + `__init__` 注册门面与 `get_tool` 粗闸）。注册三件套刻意不放 `registry/` 子包——公开单例 `registry` 与子包名冲突会让 `import agentix.tools.registry.x` 属性链解析到单例上（影子化地雷；§4 路线表本就只写「顶层 tools/」）；包内 import 全部改指新路径（切断旧路径依赖），`guardrails.py` **留驻** capabilities（M3 条目，新 executor 在 M6 前仍从旧路径引用）
- **[变更] 旧路径降级 shim**：16 个 `_TARGET` 转发文件（p36 模板，p66 `SHIM_DIRS_ALLOWED` 登记 `agentix/capabilities/tools`）+ 包门面 `capabilities/tools/__init__.py` 改 **PEP 562 惰性转发**（刻意不用 eager star-import：避免 agentix.tools 半初始化窗口重入 ImportError；`__all__` 与 `get_tool`/`registry`/`list_tool_specs` 等对象同一）；`infrastructure/tools` 老链收敛为 infrastructure → capabilities(shim) → agentix/tools，M6 一并清尾
- **[测试] 新增 `tests/test_p70_m1_tools_migration.py` 66 条**：目标位 19 文件存在 + 16 shim `_TARGET` 指向与 `_mod is` 对象同一 + 7 组代表符号同一 + 包行为（`__all__`/`get_tool`/`registry` 同一、双路径注册副作用、新家独立 `import agentix.tools` 不反向拖 `agentix.capabilities`、旧路径优先进程注册完整）+ guardrails 留驻断言；**守卫随实体迁移**：p40 `agentix.tools` 守卫由「禁止引用不存在模块」翻转为「import 目标必须存在」、p66 守卫 #3 扫描面扩 `agentix/tools`、code_hygiene 三枚 fix-marker 指新家、p36 `_TARGET` 白名单注记 M1 配对、p69 `COMPLETED_STAGES` 登记 M1（存在性翻转验证）
- **[文档]**：PROJECT_SPEC §11.1 目录组织重写（规范位表 + shim 链三层收敛）、§11.2–§11.5 路径全部指新家（行号不变——迁移逐字保留）、§2.4/§12.1/§11.8/§12 漂移点 shim 链说明、顶层包表新增 `tools/` 行；module-inventory / configuration / failure-recovery 路径同步
- 回归：全量 **3082 → 3148 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage M · M0 统一执行链：契约拆分 + 逐文件迁移清单冻结（2026-10-02）
- **[新增] 三个纯契约子包（蓝图 §3 目标树 / §4 M0）**：`agentix/domain/agent/`（`types.py` = Task/Plan/Observation/Verification/Memory 等，拆自 `domain/agent_types.py`；`ports.py` = Harness 七端口，拆自 `domain/ports/agent_core.py`）、`agentix/domain/runtime/`（`runs.py` = `RunState`+`TERMINAL_STATES` 拆自 control_plane；`steps.py` = `StepStatus`+`StepType` 拆自 `runtime/state.py`+`runtime/steps.py`；`budget.py` = `BudgetState`+`Budget`+`BudgetExceededError` 拆自 `runtime/state.py`+`runtime/budget.py`；`lifecycle.py` = `ExitStatus`+`ExitReason` 拆自 `domain/status.py`）、`agentix/domain/tool/`（`spec.py` = `ToolSpec`+`RISK_LEVELS`+`FORBIDDEN_RISK_LEVEL`+`CONTROL_REQUIRED_FROM`+`_strip_control_chars` 拆自 `capabilities/tools/definitions.py`；`risk.py` = `RiskLevel` 拆自 `human_review.py`；`permission.py` = `PermissionMode` 拆自 `hitl_bridge.py`）——三包零第三方依赖（stdlib/typing only）
- **[变更] 旧路径全部降级 re-export shim（对象同一，M6 清尾）**：`domain/agent_types.py`、`domain/ports/agent_core.py`、`domain/status.py`（`RequestStatus` 留存）、`runtime/state.py`（改顶部导入，中段散落 import 删除、TYPE_CHECKING 注释同步）、`runtime/steps.py`、`runtime/budget.py`、`application/services/control_plane.py`（改导入 + 未用 `Enum` 清除）、`capabilities/tools/definitions.py`（`ToolRegistry`/`registry` 留存，typed 拆走）、`capabilities/tools/human_review.py`（`ReviewDecision` 留存）、`application/services/hitl_bridge.py`（`__all__` 的 `PermissionMode` 不变）——全仓 import 面零改动
- **[测试] 新增 `tests/test_p69_stage_m_manifest.py` 51 条**：M0 规范位 12 存在 + 旧路径 shim 10 在位 + 新旧对象 `is` 同一（8 组参数化 + 2 组直断）+ M1–M6 逐文件迁移清单冻结（`MIGRATION_MANIFEST` 16 条目结构校验 + 无重复 source；`COMPLETED_STAGES` 翻转机制：已完成批断目标存在/删除批断源消失、未完成批断源必须还在——后续每批只许按清单搬家）；纯度守卫随契约迁新家：phase1 types/ports AST 改指 `domain/agent/`（+旧路径 shim 同一性）、p22 status 纯度拆为 `status.py` ∈ {enum, agentix} + `lifecycle.py` ∈ {enum}（+shim 同一性）、p17 结构/导出断言扩 `agent/runtime/tool` 三子包
- **[文档]**：PROJECT_SPEC §11.2（ToolSpec 规范位 `agentix/domain/tool/spec.py`，常量/`__post_init__`/`ToolRegistry` 行号刷新）、§8.3/§10.8（`TERMINAL_STATES`/`RunState` 指 `agentix/domain/runtime/runs.py`:15/:18，control_plane 侧 `AgentRun`/`ControlPlane`/`check_admission`/单例行号刷新，顺手修正 `SPAWNING` 幽灵值为实际 `pending` 枚举）、§4.2 标题标注规范位；`docs/architecture/interfaces.md` §1.4 七端口标注规范位 + 旧路径 shim（保留 `agent_core` 对照锚点）
- 回归：全量 **3028 → 3082 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage B · B2b 统一执行链：33-P0 RAG 收敛后三项（R-P0-3/4/5，2026-10-02）
- **[变更] rerank 配置三态收敛（R-P0-3）**：`agentix/application/services/knowledge_gateway.py` 的 `RerankerCompatStub.enabled` 改 `None` 三态（None=未决，不再回落 True 假开）；`agentix/config/settings.py` 新增 `_deprecated_kb_rerank_score_threshold` validator——旧键 `KB_RERANK_SCORE_THRESHOLD` 映射到 `KB_POSTGRES_RERANK_THRESHOLD` 并告警（新键胜出，弃用键不再静默覆盖）；`.env.example` 弃用注释与 `docs/architecture/ARCHITECTURE_DETAIL.md` 同步
- **[变更] Cypher 只读闸 + Neo4j 硬化最小集（R-P0-4）**：`agentix/application/agents/kg_sub_graph/kg_neo4j_conn.py` 新增公共件 `query_readonly/assert_readonly_cypher/find_write_clauses`——写子句在触库**之前** `ValueError` fail-closed（`WRITE_CLAUSES` 子串保守匹配，`graph.query` 零调用），`NEO4J_QUERY_TIMEOUT_SECONDS<=0`（默认 0.0）同步内联、调用形状与原缝逐位等价（位置参/`query=`+`params=` 关键字），`>0` 走 `asyncio.to_thread + wait_for` 超时抛 `RuntimeError`；五处执行缝接线（`text2cypher/execution/node.py`、`cypher_tools/utils.py` 软闸 block、`predefined_cypher/node.py`、`kg_gateway_bridge.py` 两 handler 硬闸）；schema 快路径 TTL 刷新 `NEO4J_SCHEMA_REFRESH_SECONDS=300`（锁内刷新，失败保留旧 schema + warning + 时间戳推进防风暴，reset 同步清时间戳）；`legal_retriever.py` dynamic 分类显式 `NotImplementedError(R-P0-4)`、`get_examples` 降级通用示例 + warning（静默 `_classify` 与 `type_based_examples` 删除）；两配置键五处同步（settings / `.env.example` / selfcheck `DEFAULT_IGNORED_MISSING` / configuration.md 表 / p41 名单，根 `.env` 照既有惯例不动）
- **[变更] 打点不吞错（R-P0-5）**：`kb_retrieval._record_rag_retrieval` 返回 `bool`（True=记账成功 / False=埋点降级），失败不再 debug 静默——warning 留痕 + best-effort `record_degraded("rag_retrieval_{source}")` 计数（/metrics 亮 degraded 信号）且**永不抛出**（通道 degraded 位仍只由通道失败驱动，语义不混淆）；`chat_service.retrieve_scoped_docs_cached` 的 `record_retrieval/record_cache_*` 包裹同语义（此前抛出会让已成功的检索 500）
- **[测试] p68 追加 32 条（14→46）+ p02 打点契约改写（4→5）**：`TestRerankTriStateConvergence` 6 + `TestNeo4jSchemaRefreshTtl` 5 + `TestCypherReadonlyGateAndTimeout` 11（写子句参数化 / 调用形状基线 / 超时 / 五缝接线 / 桥直查归零 / 10 模板过闸）+ `TestDynamicExampleClassificationDegraded` 3 + `TestTelemetryDegradedSurfacing` 4 + `TestFinalizeDegradeEvidence` 3（双通道原文 + degraded 透传回归）；`tests/test_p02_wiring.py` 打点契约由「吞异常照常返回」改写为「照常返回**且** `record_degraded` 显式透出 + `_record_rag_retrieval` bool 契约」；`tests/test_kb_nodes.py` `_run` 自建事件循环，修 p47→kb_nodes pytest-asyncio 顺序依赖预存缺陷（干净树同样失败）
- 回归：全量 **2995 → 3028 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage B · B2a 统一执行链：33-P0 RAG 收敛（R-P0-1/2，2026-10-02）
- **[变更] LightRAG 单实现去重（R-P0-1）**：`agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/customer_tools/node.py` 的 `LightRAGAPI` 改为兼容门面，转调 `agentix/application/services/lightrag_service.py::LightRAGService`——平行实现（LightRAG 构造 / embedding / 查询 / 插入、硬编码 `OPENAI_*` 凭据、固定 dim）整体删除；门面保持历史外观：构造签名与属性（`working_dir/retrieval_mode/top_k/max_token_size`，显式参数 > settings）、`query` 返回 `{"response","mode","query"}`、`insert_documents` 返回 `{"total","success","error"}`（单实现响应 `failed` 映射为 `error` 失败计数，`scripts/init_lightrag.py` 依赖不变）、`LIGHTRAG_AVAILABLE` 软依赖守卫（延迟导入单实现）与 `GraphRAGAPI` 别名；单实现顺带收编门面专有参数 `max_token_size`（默认取 settings，存量调用行为不变）；行为随单实现升级为 dim 三级推断（`EMBEDDING_DIMENSION` → `vdb_*.json` → settings）与 `EMBEDDING_API_KEY/BASE_URL` 优先凭据；转调层与历史行为形状全兼容、无旧行为可回退，故不新增回退 flag，以 p68 契约测试代替
- **[变更] 租户 fail-closed（R-P0-2）**：`agentix/retrieval/usecases/retrieval_port_adapter.py` 三方法（`search/delete_by_doc/index_status`）新增 `_require_tenant`——空/空白/None `tenant_id` 在任何 `session_scope`/仓储访问**之前**抛 `ValueError`（校验不进 try，避免被宽泛异常处理吞成 degraded 假象）；基础设施故障仍走既有 `status="degraded"` 语义（「契约违规响亮失败 vs 故障降级」由 p68 对照测试钉死）；`agentix/domain/ports/retrieval.py` 协议 docstring 同步契约；行级权限（row-level filter 下沉检索层）另批，不在 P0 最小集
- **[测试] 新增 `tests/test_p68_rag_p0_convergence.py` 14 条**：源码级去重守卫（node 无平行实现指纹 + 全仓 `LightRAG(` 构造调用唯一在单实现、`GraphRAGAPI` 别名同对象）+ 门面契约（构造默认值与延迟导入、query dict 形状与构造参数逐项转调、异常前缀不双包、insert `failed`→`error` 映射）+ 租户（三方法空租户 ValueError 先于存储访问、有效租户放行进存储、infra 故障仍 degraded、`tenant_id` 关键字必传）
- 回归：全量 **2981 → 2995 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage B · B1 统一执行链：32-P0 记忆收敛（双卡合并 / 脱敏补齐 / 冲突键加固，2026-10-02）
- **[新增] 统一卡片 Schema `agentix/application/services/card_schema.py`（B1-1，M-P0-1）**：`CARD_SCHEMA_VERSION=1` 单源（画像卡 `PROFILE_CARD_SCHEMA_VERSION` 与语义卡 `MemoryCard.schema_version` 同取）+ `canonical_json` + `conflict_row/upsert_conflict_row`（纯函数、值按 canonical 去重排序、行只增键）——双卡冲突行统一 `{"key","values"}` shape，`fold_profile_card` 改走同一 helper（输出与旧逐位等价）
- **[变更] 语义卡冲突改「只标记不覆写」（M-P0-1）**：`MemoryCard` 只增 `schema_version/conflicts` 两字段（旧序列化与旧数据不动，`from_dict` 给默认）；`merge_cards` 子串冲突不再「新值覆写」→ 原 `content` 保留 + 冲突值并入同一 `conflicts` 行 + `version+1` 表示卡片修订；二次冲突累积进同行（同键只一行）
- **[变更] PII 脱敏补齐（M-P0-3，规则只增不减）**：`redact_pii` 基线 4 条（PHONE/EMAIL/ID/CARD）不动 + 新增 11 条——中文地址 5（行政区划起手/道路门牌/楼号室/栋室/小区房号）、英文地址 1（门牌+道路通名）、中文姓名锚点 2（`我叫…`、`姓名：…`——冒号锚点防法律文本误伤）、英文姓名锚点 3（`my name is/I'm/I am`，大小写敏感）；写路径全覆盖——`InMemoryMemory.remember`（原有）+ `LongTermMemory.store_summary`（summary/topics/key_facts）+ `SemanticMemoryStore.store_cards`（incoming + 存量 legacy 重写即清洗）；读路径二次兜底——`InMemoryMemory.recall`（正文 + `meta.conflicts` 标记行）、`get_context_for_query`（摘要/画像卡/语义卡区段）
- **[变更] 冲突键加固（M-P0-4）**：`resolve_conflicts` 键 `tags[0]` → **全 tags 排序集**（全集不同 = 不同事实不裁决）；同键按 `stored_at` **30 天时间窗**（`CONFLICT_TIME_WINDOW_DAYS=30.0`）分簇，窗外历史独立保留（各出胜者、互不覆盖）；窗内胜者（confidence 最高，同分取新、再同保首见）以 `meta["conflicts"]` **标记**被裁决值（item_id/content/confidence/stored_at——不覆写不删除不静默）；缺 `stored_at` 视为当下（兼容存量无时间戳数据）；纯函数 `dataclasses.replace` 不改输入
- **[变更] M-P0-5 3 scope 收口**：service 模块 docstring / README 记忆行 / PROJECT_SPEC §5.10 / agent_types docstring 统一「三 scope」口径（以 `MemoryScope` 3 值为准）
- **[新增] 验收：p67 守卫 30 条 + 离线评测**：新增 `tests/test_p67_memory_p0_convergence.py` 30 条（序列化只增/双卡冲突行同构、标记不覆写与同行累积、脱敏基线不减+中英姓名地址+零误伤+幂等+写读零留存、冲突键全 tags+时间窗+标记+纯函数、3 scope 文档一致、evals 报告键）+ `evals/memory_eval.py`（离线确定性零 LLM，报告键 `profile_stability=1.0 / conflict_precision=1.0 / privacy_leak=0`，不进门禁）；改写 `test_r3_memory_layers.py::test_merge_conflict_bumps_version` 为标记语义
- 回归：全量 **2951 → 2981 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage A · A6 统一执行链：死代码批（51 文件）+ 分层守卫定稿 p66（2026-10-02）
- **[移除] 死代码批（A6-1，51 文件 + 15 空目录）**：全仓可达性扫描（AST import + 点号字符串提及 + 祖先包标记防伪死）零引用直删——text2sql/components 10 个 `__getattr__` 转发器；agentic_rag_agents 31 件（gather_cypher/gather_visualizations 4、summarize/prompts、text2cypher/models、tool_selection/models、visualize/{models,schema}、embeddings/{__init__,embedder_protocol}、exceptions.py、utils/{__init__,config}、ingest/ 与 cypher_examples 子包死件、dynamic_schema_retriever、neo4j_vector_example_retriever、ui/ 4、workflows 多 agent 两死子图、workflows/single_agent 整目录）；kg_sub_graph/{kg_builder,multi_tools}.py；infrastructure/reliability/retrieval 8 件（core/{hashing,middleware}、**tools/executor 死 shim（零 importer）**、reliability/release_gate、conflict_arbitration、infra/llm/{__init__,chat_client}、vectorstore/migration）；**排除清单显式不动**：agent/{observer,policy}×4（保留决策）、kg_states.py、kg_sub_graph/planner/{__init__,planner_node}、components/planner/unified.py（p63 活）、crawler、graph/ 占位、application/agents/main.py、KNOWN_BYPASS_TOOLS 五项
- **[变更] 文档与导出同步（A6-2）**：p41 三红清零——design-legal-domain M3 行改 plain path + A6 删除注、PROJECT_SPEC §7.5 single_agent 表删 chart/text2cypher 两行 + 注、§16.4 shim「8 个文件」→7 且示例改 definitions.py、§16.7 release_gate 改「A6 已删」；failure-recovery L1 路径改 capabilities；module-inventory 三处死 shim 陈述；current-tree 五处陈旧行；p40 `KNOWN_MISSING_DEPS` 清空；`application/__init__` 删 `prompts` 再导出、`infrastructure/__init__` 删 `knowledge` 再导出与 docstring
- **[新增] 分层守卫定稿 p66（A6-3，12 守卫测试化收敛）**：新增 `tests/test_p66_arch_guards.py` 9 条——**#11 LangGraph 白名单棘轮**（equality 断言，A6 后冻结 11 文件，M 批次迁图随评审收缩）、**#12 `_TARGET` shim 目录棘轮**（equality，2 目录，终态空集）、**#3 能力层零反依赖**（capabilities 对 agent/orchestration/policies 零 import，无白名单）、**#6 应用核心零客户端直连**（pymilvus/neo4j/langchain_neo4j/langchain_community/langchain_mcp/mcp；显式豁免前缀 `application/agents/` = M2 迁移对象、随迁收缩）、**#7 promote 只在离线进化**（`.promote`/`.run_regression` 调用点只在 agent/evolution/）、**#4 适配层无业务 DB**（agents/ 子树零 persistence/core.database import + SessionLocal 名零出现）**+ runtime 零 langgraph**、**#9 replan 有界步预算兜底**（RetryReplanner 反复触发但 max_steps=2 先触顶 → `budget_exceeded` 终态、`asyncio.wait_for` 防挂起）、**#1 跨路径单一平面**（chat + retrieval 双流同平面、agent_name 各异、run_id 互异、跨路径重复 spawn 拒绝）；#2/#5/#8/#10 分别由 p62/p65/p31+p61/p32+p33 承载（docstring 守卫映射表）
- 回归：全量 **2942 → 2951 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage A · A5 统一执行链：SSE 全量统一 R5a–R5d（2026-10-02）
- **[变更] R5a run-scoped emitter（守卫 #5 测试化）**：新增 `agentix/application/services/run_event_stream.py::RunEventEmitter`——`attach` 原地补 Run 内单调 `seq`（从 1）+ `run_id`（登记成功时，降级为 `None`）；chat 侧戳记**单点在 `stream_events` wrapper**（holder 默认 emitter，impl 注册成功后换装带 run_id 的，impl 直驱不戳记），retrieval 侧在 `_stream_answer_impl` 的 async for 内 `yield emitter.attach(payload)`；SSE 帧可回溯到统一 Run/Event stream（v2 守卫 #5），由 p65 测试断言
- **[新增] retrieval 流式运行登记（无新 flag）**：`_stream_answer_impl` 登记 `"retrieval_chat"`（meta: channel=stream, domain=retrieval, conversation_id）→ `bind` 当前 task → `mark_running` → 终态收口 `_finish_run`（幂等）：`usage_error` 非空→FAILED、`GeneratorExit` 且未交付 finished→CANCELLED(client_disconnected)、CancelledError→CANCELLED、Exception→吞掉并 yield 已戳记 error 帧（router 契约）；`AdmissionDenied` fail-closed 上抛，其他登记故障降级不断流（帧带 `seq` 无 `run_id`）
- **[变更] R5b SSE 词汇归一（拍板：保留 `type` 删 `event`）**：删 28 处 yield 双写（application chat_service 12 + retrieval chat_service 14 + helpers 1 + router 1）与 router backstop 兜底块；`ChatStreamChunk` 删 `event` 字段、加 `seq`/`run_id`，`_to_sse` 同步；**前端两 parser 按风险规则 D1 L187 保留两词容错不动**（`event ?? type` / handleBlock 归一化），弃 `event` 不致白屏；范围裁定：`config_watch` ops 事件、LangGraph 原始词汇、lightrag raw-text 端点不在 D1 范围
- **[变更] R5c 前端 framing 收口共享层**：新建 `web/src/api/sse.ts`（`extractSseDataLines` / `readSseData` / `SSE_MAX_BUFFER=1MB`，溢出先 `cancel()` 再抛 `stream buffer overflow`，`finally` `releaseLock()`，onData 抛错向外传播）+ `sse.test.ts` 5 条；chat / knowledge 两 parser 改用共享层——JSON 容错、词汇归一、业务分发留在各模块；vitest **121 → 126**，`vue-tsc` 0 错
- **[变更] R5d 流式轨迹桥通电（chat 侧，无新 flag）**：nested `_record_stream_completion()`（`stream_failure` 非空则跳过；`validate_completion` 恒执行，route="legal" 等非 citation 路由 ≥ `MIN_ANSWER_CHARS=6` 且带 sources → ok → `_record_completion_step`）+ 三处失败 `_record_error_step`（idle/wall 超时 `stream_timeout`、异常路径 `e.code.value`/`internal_error`）；**失败路径不记 completion**；state 块新增 `route_type=None`、`answer_parts=[]`；流式轨迹 jsonl 持久化 → `export_trajectory_packages` 评估门测试
- **[测试] 新增 `tests/test_p65_unified_sse.py` 13 条**：emitter seq/run_id 与降级戳记、retrieval 登记/FAILED/CANCELLED 终态、全端点单词汇（`event` 不出现）、流式 completion/error 轨迹、jsonl→eval 导出评估门；p14 `test_frames_dual_write_event_and_type` 改写为 `test_frames_single_vocabulary_type_only`，retrieval progress 两处双写断言改为 `"event" not in` 归一断言
- 回归：全量 **2929 → 2942 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；p41 33 条、p35 26 条、code_hygiene 12 条、p46 10 条 = 81 条门禁全绿；vitest 126 + `vue-tsc` 0 错

### Stage A · A4 统一执行链：HITL 生产审批决策面 + application/runtime 直删 + 死转移处置（2026-10-02）
- **[新增] HITL 生产审批决策面（A4-1，无新 flag）**：`ops_router` 补两端点——`GET /ops/hitl/approvals`（`bridge.pending()` 待审面 + `bridge.stats()` 观测面）与 `POST /ops/hitl/approvals/{sidecar_request_id}`（四裁决 approve/reject/edit/resume → `bridge.decide` 回写闸门；未知 id 404、非法 action / EDIT 缺参等裁决错误 400 不落判定）——修复 `bridge.decide` **零生产调用方**造成的 WAITING_APPROVAL 挂起不可解（A2 留口：HITL 生产审批端点属 A4）；惰性 import 与 `/ops/events` 同模式（测试可 reset 单例），router 级 `verify_api_key` 照旧；PROJECT_SPEC §13.4 ops 路由清单同步
- **[移除] application/runtime 直删（A4-2）**：6 文件（budget/lifecycle/runtime/state/steps/`__init__`）全仓 0 importer（v2 §1 已核实）整目录删除 + p64 防回潮断言；文档同步——PROJECT_SPEC（§2.1 目录表、§2.4 shim 表行标「已删除」）、source-code-study 两处旧路径改纯文本 + 已删除标注（p41 反引号红线）、dependency-map / current-tree / module-inventory / agent-core-layers / multiagent-optimization 的「为 shim」现时态陈述改为「A4 已删」
- **[移除] 死转移处置（A4-3）**：`AgentStatus.PAUSED_FOR_APPROVAL` 状态成员与 `ALLOWED_TRANSITIONS` 连边（RUNNING→、EXECUTING→ 及其出边行）整体移除——Phase 8 起审批语义由 `WAITING_APPROVAL` 承担，生产零进入 = 死转移（重复语义）；审批流收敛单轨 `EXECUTING/RUNNING → WAITING_APPROVAL → RUNNING`；PROJECT_SPEC §10.2（12 → 11 值 + 行号实测刷新）/ §10.5 状态机表同步；`test_r3` 审批流用例改走 `WAITING_APPROVAL`；`PAUSED`（Phase 9 长任务）与 `PLANNING`（M2 编排前瞻）为不同语义状态，不动
- **[测试] 新增 `tests/test_p64_hitl_ops_and_dead_transitions.py` 11 条**：路由注册、空待审面、挂起 → 列示 → approve → 闸门放行 roundtrip、reject fail-closed、未知 id 404、非法 action 400（仍可合法消费）、EDIT 缺参 400 且闸门不落判定、`PAUSED_FOR_APPROVAL` 成员与值移除（11 值断言）、审批单轨 roundtrip + WAITING_APPROVAL 出边全集、application/runtime 目录不存在 + import `ModuleNotFoundError` 防回潮
- 回归：全量 **2918 → 2929 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；p41 33 条、p35 26 条、code_hygiene + p46 随全量通过

### Stage A · A3 统一执行链：图内规划收敛（KG planner 影子对拍 → 切流）+ TEXT2SQL 死 flag 处置（2026-10-01）
- **[移除] `TEXT2SQL_ENABLED` 死 flag 处置（A3a）**：代码级核对确认零读取点（仅 settings 声明 + selfcheck 合法省略清单 + `.env.example` 声明），四面清除——`settings.py` 声明块（R-11 占位注释一并删除）、`selfcheck.DEFAULT_IGNORED_MISSING` 条目、`.env.example` 行、`PROJECT_SPEC` 配置键表行；**活键不受影响**（`TEXT2SQL_BACKEND`/`WREN_*` 由 `wren_adapter` 真实读取，原样保留）；`route_query` 的 `text2sql-query` 分支与图内 text2sql 节点不涉此 flag，行为零变化
- **[新增] 两个 flag（默认关，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+详情+p41 名单）**：`KG_PLANNER_SHADOW_ENABLED=False`（影子对拍）、`KG_PLANNER_UNIFIED_ENABLED=False`（切流）——flag-off 图内私有 LLM 规划链逐位不变、统一 Planner 零触达
- **[新增] 统一 Planner 适配与影子对拍报告 `components/planner/unified.py`（A3b，对齐 v2 §4 A3 与 §8 风险对策「影子报告先行，golden 零抖动才切流」）**：`get_unified_planner()` 取用点（现役 `SingleStepPlanner` 零 LLM 确定性，与 `agent_loop_adapter` 生产口径一致；未来 LLM 规划器换装口，测试可 monkeypatch）+ `unified_decompose()`（`Plan` → 图内 `Task`，每 plan step 一个任务单元，step 空时与图内空回退同构单任务兜底）+ `PlannerShadowReport`（seq/question/driver/in_graph/unified/agree/error 冻结数据类）+ 有界报告队列（deque maxlen=200，淘汰后序号仍严格单调）+ `record_report`（strip 归一对拍；影子侧异常记 `error` 且 `agree=False`；不一致/异常/一致三档结构化日志）——**报告只记不改**
- **[变更] 图内 planner 节点三分支（`components/planner/node.py`）**：默认双 flag 关 = 图内私有链原样（含 ignore_node 空分解单任务兜底语义）；`UNIFIED` 切流 = 统一 Planner 驱动、私有链零 LLM 调用、驱动侧异常诚实抛出（与图内链失败口径一致，不静默降级）；`SHADOW` = **非驱动侧**并行运行落报告（in_graph 驱动时统一侧跑、unified 驱动时私有链反向跑，双向复用同一报告机制），异常只记不抛绝不影响输出；`ignore_node` 优先级最高（任何 flag 下不调 Planner）
- **[测试] 新增 `tests/test_p63_planner_convergence.py` 22 条**：flag 默认关与五处同步（含 TEXT2SQL_ENABLED 四面清除 + 活键不受影响）、flag-off 基线（图内链唯一调用方/统一零触达零报告/空分解回退/ignore_node 跨 flag 语义）、影子（驱动不变/不纠偏/影子侧异常不破输出）、切流（私有链零调用/空回退/驱动异常诚实抛/双开反向对拍/next_action 契约）、报告存储（归一对拍/error 强制不一致/有界淘汰 + 序号单调/clear 归一）、`unified_decompose` 取用点可替换
- 回归：全量 **2896 → 2918 passed / 14 skipped / 0 failed**（2026-10-01）；`ruff check agentix tests` 全绿；p41（含 2 新键）33 条、p35 26 条、code_hygiene + p46 随全量通过

### Stage A · A2 统一执行链：三闸有效化 + 五项署名旁路收编 + 记忆三工具（2026-10-01）
- **[新增] 三闸有效化（A2a，无新 flag）**：`get_tool_executor()` 构造点在 `TOOL_SELECTION_ENABLED` 打开时注入 `selection.build_tool_policy()` 与新函数 `selection.tool_permission_check()`——此前生产路径 `policy` 与 `permission_checker` **零注入**（Gate A/B 空转，v2 §1 出处）；flag-off `build_tool_policy()` 返 `None` ⇒ `ToolExecutor()` 零注入基线，注入故障按零注入回落不打挂单例；权限兜底 = `DISABLED_TOOLS` 执行侧（声明层裁剪的同名配置兜底）+ 策略黑名单/白名单/能力标签，checker 故障 fail-closed 语义不变；**Gate C 决策面**：`human_review` 新增 `mark_decision_surface()/clear_decision_surface()`、`get_review_gate()` 告警条件加 `not _decision_surface`——`hitl_bridge.install_executor_hook` 先宣告决策面再建闸门（HITL 桥 `bridge.decide` 即审批决策通道），消除「开了却无人可批」的误导性启动告警；`reset_review_gate`/`reset_hitl_bridge` 归零，test_p32「无决策通道必告警」两条契约原样通过；HITL 生产审批端点属 **A4** 本批不做
- **[新增] 两个 flag（默认关，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+分组行+p41 名单）**：`TOOL_GATEWAY_ENFORCED=False`（五项旁路收编闸）、`MEMORY_TOOLS_ENABLED=False`（记忆三工具）——flag-off 节点直连/不声明不注册，与基线逐位等价
- **[新增] 五项署名旁路收编（A2b）**：新执行缝 `capabilities/tools/kg_gateway.py`——`run_governed(tool_name, args, direct=)`：flag-off 直连（同步/awaitable 均透传，行为逐位等价）、flag-on 经 `ToolExecutor.execute`（失败抛 `GatewayDenied`，节点既有 `except` 分支按普通执行失败处理）、flag-on 无 handler 告警回落直连（接线缺失不打挂节点）；六处执行缝接入：`kb_tools` search、`cypher_tools/utils` 与 `text2cypher/execution` cypher_query（执行侧写子句二次闸门前置不变）、`predefined_cypher`、`customer_tools` microsoft_graphrag_query、`text2sql_tool`（workflow 构造抽为 `build_text2sql_workflow()`，节点/handler 共用防漂移）；`application/services/kg_gateway_bridge.install_kg_gateway_handlers()` 装齐五 handler（graph/lightrag/工作流均 handler 内懒加载，`ensure_registered()` 先补 spec），`main._startup` flag-on 安装；`KNOWN_BYPASS_TOOLS` **保持五项不变**（flag-off 默认仍旁路、豁免继续说真话；flag-on 装齐后审计 `stale_exemptions` 亮起属预期灰度信号）+ 棘轮测试钉死只减不增；声明超时放宽 `text2sql_query=120s`、`microsoft_graphrag_query=60s`（收编后治理链超时闸按 LLM 工作流时长，cypher 两族保持 30s）
- **[新增] 记忆三工具（A2c）**：`capabilities/tools/memory_tools.py` 三个 schema（类名==工具名 title 契约）+ spec——`remember`/`forget` L1 + side_effect（`HUMAN_REVIEW_ENABLED` 时走 MEDIUM 审批半）、`recall` L0 只读，`capabilities=("memory",)`；`memory_tools_bridge.install_memory_tools()` 三 handler（缺 content / forget 无匹配键 → ValueError → executor failed 诚实；**无通配删除**：item_id/content 二选一必填）；`MemoryManager.forget()` duck-typed 扇出——**`MemoryPort` Protocol 不扩容**（runtime_checkable isinstance 契约不动），无 `forget` 方法的 provider 静默跳过，scope 不支持跳过、单 provider 失败 warning 继续；`LongTermMemoryProvider.forget`（hdel 精确 field/内容匹配，比对原文与脱敏文本）+ `InMemoryMemory.forget`（scope+subject 隔离，双键皆空返 0 绝不误清）；`registry_init` flag-on 追加三声明、`main._startup` flag-on 安装
- **[测试] 新增 `tests/test_p62_unified_gateway.py` 18 条**：三闸（flag-off 零注入基线 / flag-on 注入 policy+checker / 非法 `TOOL_SELECTION_MAX_RISK` 回落零注入 / 权限兜底全真值表 / `DISABLED_TOOLS` 执行侧 `permission_denied`）、决策面（标记抑制启动告警、安装置位/复位清零）、执行缝（flag-off 直连等价、无 handler 回落、flag-on 治理链路由 + `GatewayDenied`、`KNOWN_BYPASS_TOOLS` 棘轮基线冻结）、bridge（flag-off 零安装、flag-on 五 handler + 审计 `ok` + `stale_exemptions` 信号）、记忆三工具（flag-off 双排除、title 契约 + 风险分级 + capability、remember→recall→forget roundtrip、无通配契约、未声明 forget 的 provider 跳过）；flag-on 注册的 L1 spec 测试完按 p50/p60 模式撤出全局注册表（防 r1「注册表全 L0」顺序红）
- 回归：全量 **2878 → 2896 passed / 14 skipped / 0 failed**（2026-10-01）；`ruff check agentix tests` 全绿；p41（含 2 新键）33 条、p35 26 条、code_hygiene + p46 随全量通过

### Stage A · A1 统一执行链骨架：流式运行登记闭环 + 退出契约三件套 + Run API 硬迁移 v1 + 元数据死重剔除（2026-10-01）
- **[新增] 流式 run 登记与终态闭环（A1a，无新 flag，登记失败降级不断流）**：`control_plane.spawn()` 支持显式 `run_id`（缺省 uuid4，重复 ID 抛 `ValueError`）、`bind()` 单 owner（活任务改绑返回 False + warning，同 task 幂等，`done()` 后可改绑）；`chat_service._stream_events_impl` **首帧前**登记（spawn `"kg_graph"` + meta `{channel: stream, session_id}` + `bind(asyncio.current_task())` + `mark_running`；`AdmissionDenied` 透传 fail-closed，其余异常降级 `plane, run_id = None, None`）；新增闭包 `_finish_run(state, error)`（懒解析 `RunState` → `finish/cancel`）与 `stream_failure` 状态位；metadata 首帧**移入 try 内**（防首帧前断连泄漏 RUNNING）；尾部 `except asyncio.CancelledError → finish("cancelled","cancelled")` + re-raise、`except GeneratorExit → finish("cancelled","client_disconnected")` + re-raise（其间不 yield）、`except Exception → finish("failed", f"{type}: {e}")` 再产 error/done 帧；`finally` 兜底：`stream_failure` 非空 → `finish("failed", stream_failure)` 否则 `finish()`（默认 SUCCEEDED），并释放信号量——正常/断连/取消/异常/挂起五条路径均落终态，无 RUNNING 泄漏。
- **[新增] 退出契约三件套（A1b）**：`domain/status.py` 新增 `ExitStatus{COMPLETED="completed", INTERRUPTED="interrupted", FAILED="failed"}` + `ExitReason{COMPLETED, MAX_ITERATIONS="max_iterations", INTERRUPTED, ERROR="error"}`（str-Enum，可与裸串比较）；`LoopResult` 增只读属性 `exit_status/exit_reason/truncated`（`budget_exceeded → max_iterations + truncated=True`，`context_build_failed` 只是 reason 前缀、terminal 仍为 `failed`）；`TaskOutcome` 增同名三字段 + `__post_init__` 派生（**不覆盖显式传值**；ok→completed、retryable 耗尽→failed/max_iterations+truncated、非可重试→failed/error）+ `to_dict` 三键落地。
- **[变更] Run API 硬迁移 v1（A1c，无双路径别名）**：删除 `agentix/retrieval/api/routers/agent_runs.py`，新家 `agentix/interfaces/http/v1/runs.py`，include 于 `v1/__init__.py`（无前缀 → `/api/v1/agents/runs`，tag "Agent Runs"，L19-24）；鉴权改 router 级 `require_run_bearer`（严格 Bearer JWT + `safe_decode_access_token`，**不查库、无 Cookie 回退、默认拒绝 401**，与旧 `get_current_user` 口径一致，避免鉴权倒退）；错误信封保持顶层 `{request_id, error_code, message, user_hint?}`（handler 内直接返回 JSONResponse）；`agent_executors` 新增 `chat` 执行器（ask→`{output, meta:{executor/session_id/route/status/error_code/sources}}`，session 缺省 `uuid4().hex`）+ `register_executor("chat", ...)`；400 hint 改动态 `executor_names()`。文档八处同步：README §11 表、PROJECT_SPEC（§9.2 表删行/§10.6 段/§13.4 表/§13.5 对照）、`docs/runbooks/failure-recovery.md`（新路径 + Bearer 头）、`docs/migration/module-inventory.md:114`、`docs/design/generate_architecture_diagram.py`（p41 约束：旧路径在文档中仅纯文本或带删除表述）。
- **[变更] 会话元数据死重剔除（A1d，`ask()` 返回值不变）**：`chat_service.py` 模块常量 `METADATA_EXCLUDE_KEYS = frozenset({"agent_state", "runtime", "budget", "completion"})`；`save_message` 持久化过滤扩展至该集合；`v1/chat._public_metadata` 出口过滤改用**同一集合**——存/出两口同口径。
- **[新增] 守卫用例 `tests/test_p61_unified_run_chain.py` 31 条（A1e）**：spawn 显式 run_id/重复拒绝、bind 单 owner（跨 task 改绑拒绝/同 task 幂等/done 后改绑）、流式登记五态（正常 SUCCEEDED、`aclose`→CANCELLED+client_disconnected、图异常→FAILED 且 error 含原消息、idle 超时→FAILED（`monkeypatch settings.STREAM_IDLE_TIMEOUT`）、双流恰两记录）、LoopResult/TaskOutcome 退出契约 + `to_dict` 三键 + `METADATA_EXCLUDE_KEYS` 内容、Run API 四路由在 v1 + 裸 client/非法 Bearer 双 401 + retrieval 聚合不再含 runs、执行器 registry（probe/chat 双注册 + chat_executor 映射与自动 session）、元数据过滤（save_message 落库 + `_public_metadata` 出口）。
- 回归：全量 **2847 至 2878 passed / 14 skipped / 0 failed**（2026-10-01）；`ruff check agentix tests` 全绿；p41 文档一致性 33 条、p35 配置治理 26 条全过。

### P6 · 长时记忆：MemoryPort 扇出 + 后台评审 + Skill 草稿沉淀 + 分段 fail-open（2026-10-01）
- **[新增] 两个 flag（默认关，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+记忆分组行+p41 名单）**：`MEMORY_FANOUT_ENABLED=False`（6.1 扇出召回，**挂在 `MEMORY_CONTEXT_ENABLED` 通道之下**——与 `MEMORY_CARDS_ENABLED` 同族）、`MEMORY_REVIEW_ENABLED=False`（6.2 摘要后台评审）；6.3 草稿工具**复用既有 `SKILLS_ENABLED` 门控（零新 flag）**；6.4 分段 fail-open 无 flag（仅 service 内部逐段兜底）；flag-off 读路径/handler 报告与基线逐位等价
- **[新增] 记忆扇出 `memory_manager.py`（借鉴 hermes MemoryManager 多 provider 扇出 + provider ABC，6.1/§3.5）**：`MemoryManager`——ctor `runtime_checkable` isinstance 边界 fail-fast（空 ValueError/非 port TypeError）；`recall` 每 provider `asyncio.wait_for` 独立超时（`PROVIDER_TIMEOUT_S=1.5`）+ `return_exceptions` **失败 provider 记 warning 跳过（fail-open 永不抛，全败返回 `[]`）**，合并按 (scope,subject,content) 去重保最高置信 → `resolve_conflicts` 裁决 → confidence 降序稳定排序截断；`remember` 扇出——`MemoryScopeUnsupported`=跳过不计失败、≥1 成功返回首个 id、全败 `RuntimeError`（honest 不静默丢写）；`last_degraded` 诊断视图 + `get_memory_manager/reset_memory_manager` 懒加载单例
- **[新增] Redis LTM 适配器 `LongTermMemoryProvider`（implements MemoryPort，6.1/§5 表 6.1 落点）**：**独立键空间 `{prefix}:port:{subject}`**（JSON hash，field=item_id——不复用 `summary:*`，避免把记忆条目算进 `profile.total_sessions`/污染摘要注入区，kind/confidence/tags 全保真）；`recall` scope 隔离（非 user → `[]`）+ 坏 JSON 条目 granular fail-open 跳过 + 排序 `(-confidence, item_id)` 完全确定（不依赖 hash 迭代序）；`remember` scope≠USER 抛 `MemoryScopeUnsupported`、`redact_pii` 脱敏、无 id 按脱敏内容 **sha256 前缀确定性 id（同内容幂等覆盖）**、超 `max_items=200` 按 `stored_at` 裁剪最旧（删-写语义同 `SemanticMemoryStore`）；Redis 异常不吞（fail-open 归扇出层）
- **[新增] chat 扇出接线（6.1）**：`_maybe_memory_context` base 段照旧 → `MEMORY_FANOUT_ENABLED` 开才追加「长期记忆片段（扇出召回）」段（subject=default_user 与 base 同语义，limit=`MEMORY_RECALL_LIMIT`）；**扇出失败只丢该段保 base、base 失败仍返回空串、空结果不追加空行**；flag-off 路径与基线逐位等价
- **[新增] 后台评审 `memory_review.py`（借鉴 §3.5 异步评审线程，6.2/§5 表 6.2）**：`route_memory_item` 纯规则零 IO（优先级：scope≠user → 置信地板 0.5（先于 kind，防低置信污染画像）→ kind=user 晋升 → SEMANTIC+≥0.6+标签 preference/instruction 晋升 → 过程性条目归 Skill 域评审不代劳），reason 诚实进报告；`review_items` **逐项 fail-open**（晋升失败只计数不中断）；`schedule_background_review` fire-and-forget + done-callback 只记日志（同步上下文返 None 并告警，不伪装成功）；`items_from_summary` 复用 `extract_cards`（零 LLM）→ kind=SEMANTIC/scope=USER 条目
- **[新增] 摘要 handler 接线（6.2）**：`handle_conversation_summary` 落库后 flag-on 才 `schedule_review_for_summary(summary)`（旁路失败仅 warning，摘要落库不受影响）；flag-off 报告形状与基线完全一致
- **[新增] 6.4 分段 fail-open（§5 表 6.4）**：`get_context_for_query` 摘要段与画像段**各自** try/except 兜底（任一段失败只丢该段记 warning，其余段照常拼装部分存活；卡片区本就 fail-open）
- **[新增] Skill 草稿沉淀工具 `create_skill_draft`（6.3，§5 表 6.3 + §3.6 /learn）**：`agent/skills/learn.py` 写 `data/skills_drafts/{name}/SKILL.md`——**隔离于 `SKILLS_DIR`**（loader 扫不到 ⇒ 草稿不会被当活技能加载，人工评审搬入后才生效）；校验复用 loader 单一真源（必填三键/描述 120/反例 240/正文 8000/`_NAME_RE` 防路径穿越投毒命名/单行 frontmatter 约束）+ **同名拒绝覆盖（never overwrite）** + 写后 `parse_skill_file` 复验失败即清理不留残目录；工具层永不抛（`created=False + errors` 诚实回执）；声明 **L1 + side_effect=True + capabilities=skills + timeout 10s**（side_effect 禁 auto-retry）、schema 类名==工具名（title 契约，`_spec_for_schema` 回查不 miss）；`install_skill_draft_tool` 与 `install_read_skill_tool` 同 `SKILLS_ENABLED` 双 gate（registry 声明 + handler 安装双侧，flag-off 不声明不注册）
- **[测试] 新增 `tests/test_p59_memory_fanout.py`（37 条）+ `tests/test_p60_memory_learn_review.py`（49 条）**：manager 边界/recall fail-open（超时/全败/去重/裁决/确定排序/limit）/remember 三态语义；适配器 scope 隔离/脱敏/幂等 id/roundtrip 保真/关键词过滤/坏条目跳过/确定排序/上限裁剪；单例；chat 扇出 flag 两态（flag-off 零触达 manager+逐位等价、flag-on 段落格式、扇出失败保 base、嵌套 CONTEXT 闸门）；6.4 三段降级部分存活；路由 10 规则参数化+优先级；评审计数/fail-open/报告；摘要→卡片→条目映射与调度（含 done-callback 吞异常、同步上下文返 None）；handler flag 两态+调度失败不破 handler；learn flag 治理/声明契约/audit 干净/9 校验路径不落盘/垃圾入参不抛/写后可解析/拒绝覆盖/隔离断言/executor E2E 双向/源级接线；5 处同步断言
- **[文档] 阶段三同步（P1–P6 终态）**：README §11 测试真源 2463 → **2847**（2026-10-01 实测 + 逐批 delta 链 2463→2496→2542→2594→2668→2761→2847）；`current-tree.md` 数字刷新（.env 241 键 / agentix 559 py / tests 150 模块 + services 补 7 文件）；`module-inventory.md` §6 新增「优化批次 P1–P6」六批收口记录；`source-code-study.md` §4 对照矩阵更新为终态（职责 1/5、要求 1/2/3/5 升级，职责 7 加分项 Proactive/长期记忆 ❌/⚠️ → ✅ 默认关）+ §5 借鉴落点具体化到新文件 + 章节头标注已落地；`PROJECT_SPEC.md` §3.4 行号全量实测刷新 + 附 P1–P6 十五个新键独立表
- 回归：全量 **2761 → 2847 passed / 14 skipped / 0 deselected**（2026-10-01）；`ruff check agentix tests` 全绿（`ruff format` 非本仓门禁）；p41（含 2 新键）33 条随全量通过

### P5 · Proactive：cron 定时任务分半调度 + 自愈四件套 + 交付账本 + Cron* 工具族（2026-10-01）
- **[新增] 两个 flag（默认关/60s，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+Agent 分组行+p41 名单）**：`CRON_ENABLED=False`（调度主开关）、`CRON_TICK_SECONDS=60`（ge=1，仅 flag-on 生效）——fail-fast flag 治理，flag-off **不启动循环、不注册工具、不建 `data/cron/jobs.json`**（声明清单与基线逐项一致）；超时/卡死窗口是代码内常量不进配置面（`CRON_JOB_TIMEOUT_S=120`、`CRON_STALE_MS=600000`、`MAX_DELIVERY_ATTEMPTS=3`）
- **[新增] 存储/计划半 `cron_jobs.py`（借鉴 hermes `cron/jobs.py` 分半架构，5.1）**：**零依赖 cron 解析**（requirements 无 cron 库——不假设库可用自研最小子集：5 段 UTC、`*`/`N`/`A-B`/`A-B/S`/`*/S`/逗号、dow 7≡0、**dom/dow 双受限标准 OR**）+ `next_cron_time` 严格晚于 after（日/时/分三层跳跃不扫分钟、366 天无 fire ⇒ `CronExprError` fail-fast——排不出来拒绝入库而非静默永不执行）；`JobStore` 单文件原子写（tmp+`os.replace`）、**损坏/未来版本 fail-open**（改名 `.corrupt` 留证空库起，不拖垮启动）；**先推进 next_run_at 再执行**（推进即落盘，执行期崩溃只丢这一轮不 tight-loop）；inflight 簿记（在跑/停用拿不到、`>=CRON_STALE_MS` reap）；runs LRU 200 + incidents LRU 100 有界
- **[新增] 交付账本 + 事故单（§5.3 durable delivery ledger + 5.2 自愈四件套之③）**：`accept_run`（受理）≠ `complete_run`（回执）——stale/崩溃的 run 停在 accepted 即 unacked，由 `pending_runs()` 诚实暴露且**不自动补跑**（自动重放有双副作用风险）；通知状态机 pending→delivered，重投封顶 `MAX_DELIVERY_ATTEMPTS` 次转 `dead`（告警不静默丢也不无限重试）；事故单按 job+message **同因节流**（进行中只累加 count 不刷屏）、成功自动销案、销案后再失败 ⇒ 新单
- **[新增] 执行半 `cron_scheduler.py`（借鉴 hermes `cron/scheduler.py`，5.1/5.2）**：`tick()` 次序契约——① revision 不等 ⇒ `yielded` 让位（默认 provider 读 `AGENTIX_CODE_REVISION` env，未配置两侧空串恒等不误杀；滚动部署旧进程让位新代码）→ ② reap stale+事故单 → ③ advance 先行（runner 内可见已推进值）→ ④ acquire 防双跑 → `_execute` 受理→runner（`asyncio.wait_for` per-job 超时）→成功销案/失败开单，**`CancelledError` 透传但先 release**（取消不是失败；账本停在受理=unacked，重启按计划恢复）→ ⑤ `unacked` 汇总进 `TickReport` + 通知按账本重投（`user_notification` 事件，走既有通知管线）；`run_job_now` 与 tick 同款簿记（缺 id/停用/在跑 ⇒ 诚实失败）；`start/stop_cron_scheduler` 幂等循环（先睡后跑、单轮异常不杀环、`_shutdown` 取消）
- **[新增] Cron* 工具族 `cron_tools.py`（5.4，对齐 hermes Task/Cron 工具）**：`cron_create`（name/expr/prompt|tool 二选一互斥/deliver）/`cron_list`/`cron_delete`/`cron_run`——create/delete/run **L1 + side_effect=True + capabilities=control**（配置级副作用禁 auto-retry；L1⇒MEDIUM⇒`HUMAN_REVIEW_ENABLED` 时人工审批半），list L0 只读；schema 类名==工具名（title 契约）；`cron_run timeout_ms=130000` > `CRON_JOB_TIMEOUT_S`（**漂移守卫测试**钉死——工具等待上限<任务超时=必然假失败）；**安全边界：不提供任意 shell**——任务仅 agent 提示词/已注册工具两型，tool 型经 `ToolExecutor.execute` 全闸门（风险/权限/HITL 不旁路），非法 expr/互斥冲突/未知工具/非 JSON 入参全部诚实失败
- **[新增] 组合根接线（分层守则）**：`main._build_cron_runner()` **定义在 main.py**（tool 型过完整闸门；agent 型每轮独立会话走 `chat_service.ask`——DB 会话属 composition root 职责，application 层不 import infrastructure，p58 源级守卫钉死模块头）；`_startup` flag-on 才装配（CronScheduler→install→start，接线失败不阻断启动，同 plan mode 模式）、`_shutdown` 先停环；`registry_init` CRON 块在 P4 块后 `return declarations` 前（flag-off 声明清单逐项一致）
- **[测试] 新增 `tests/test_p57_cron_jobs.py`（54 条）+ `tests/test_p58_cron_scheduler.py`（39 条）**：解析器（star/范围/步进/列表/dow7、13 非法式 fail-fast、严格晚于/fire 点取下一个、UTC weekday、OR 语义三向、Feb30、确定性）；存储（roundtrip/原子无 tmp/损坏+未来版本 fail-open、advance 先落盘、acquire 冲突/停用、stale 阈值 `>=` 边界 100ms、reap 不伪造完成）；账本（accept≠complete、pending→delivered、封顶 dead、LRU×2、事故单节流/销案重开/截断）；tick 契约（让位两态、推进先于 runner 可见、跳过在跑、成败/销案/节流、超时、取消释放+unacked、Ghost run 报告）；投递（deliver 两态、3 次重投 dead 后停）；run_job_now 四态；E2E（flag 治理声明 title 风险、create/list/run/delete roundtrip、6 校验路径、tool 型 roundtrip、timeout 漂移守卫）；循环 start/stop 幂等；源级接线（main/`.env.example`/configuration.md/selfcheck 五处 + 分层 import 守卫 + 无 shell 边界）
- 回归：全量 **2668 → 2761 passed / 14 skipped / 0 deselected**（2026-10-01）；`ruff check agentix tests` 全绿（`ruff format` 非本仓门禁）；p41（含 2 新键）33 条随全量通过

### P4 · 异常闭环：三计数器护栏 + 重复折叠 + 计划态回滚 + 失败边界（2026-10-01）
- **[新增] 三个 flag（默认关，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+分组行+p41 名单）**：`TOOL_GUARDRAILS_ENABLED=False`（4.1+4.2）、`AGENT_ROLLBACK_ENABLED=False`（4.3）、`FAILURE_BOUNDARY_ENABLED=False`（4.4）——fail-fast flag 治理，flag-off 全接线**零调用、逐位等价**
- **[新增] 工具失败三计数器护栏 `guardrails.py`（借鉴 hermes tool_guardrails，4.1）**：按工具聚合 `same_args_failures`/`tool_failures`/`identical_result_repeats`，判级 **同参 2/3/5、工具级 2/5/10** → warn（附恢复建议）/block（拒**同参**再执行）/halt（拒该工具全部）；**mutation 成功全清**（计数/拦截/熔断归零）、只读成功只打断同参链（tool_failures 保留）；`recovery_advice` 按错误文本确定性分型（超时/权限/名称/参数/上游/默认），建议**刻意不含 `is_retryable` ASCII 提示词**（防拼接污染可重试性分类，测试逐词钉死）；LRU 上限 128 防长跑泄漏
- **[新增] executor 三接点（执行前闸 + 成功/失败记账）**：前置 `check()` 命中 ⇒ 风险闸门**之前**拒绝（`error="guardrail {tier}: {advice}"`、`reason=guardrail_rejected`、handler 不执行）；失败出口 `record_failure` ⇒ 建议进 `metadata["guardrail"]` 而 **error 文本保持原样**（`is_retryable`/`failure_key` 指纹不受影响）；成功出口 `record_success(mutation=side_effect)`；`task_retry` 循环头前置 skip（`skipped_reason=guardrail_{tier}`、attempts=0、runner 零启动）+ **终局回喂**（retry_predicate 判定后追加「恢复建议：」）；`RetryReplanner(guardrails=)` rationale 拼建议（未注入/无记录/引擎故障逐位等价原样）
- **[新增] 重复结果折叠 stub（借鉴 hermes 结果出层，4.2）**：`ResultDeduplicator` 记录字节级哈希——第 **2 次**重复起折叠为 `[重复结果已折叠] tool=…` stub（**轮询不豁免**），首次/第 1 次重复保留原文；两接点均 flag 门控：`MappingStepExecutor` 成功观测与 `task_execution._to_observation` 成功分支（flag-off 引擎状态存在也绝不观察）
- **[新增] 计划态权限切换 `plan_mode.py` + `rollback.py`（借鉴 claw-code 保存/恢复语义，4.3）**：`enter_plan_mode` **幂等红线**（二次进入复用原 checkpoint，绝不二次快照——否则 L0 被快照成「原策略」、退出形同没退）→ 快照 `executor.policy` 后收紧 **L0 只读**；`exit_plan_mode` 按 checkpoint 恢复（`None` 也是合法恢复值；未进入/未知 id **诚实失败**不静默降级）；两工具声明 **L0 + side_effect=True + capabilities=control**，schema 类名==工具名（title 契约）、不命中 HITL 词表 ⇒ 有效 LOW 免审批；`registry_init`/`main._startup` 双 flag 门控（flag-off 声明清单与基线逐项一致）
- **[新增] `RollbackPort` 生产实现 `InMemoryRollbackStore`**：uuid4 checkpoint + **deepcopy 双向隔离**（改源/改返回均不污染存储）、restore 缺失抛 `KeyError`（协议约定不静默降级）、**per-checkpoint LRU**（restore 命中即 move_to_end——修实现以符文档契约）+ 单 run 上限 8 / run 数上限 64 有界；`reset_rollback_store` 只清存储与计划态标记**不动 executor.policy**（残留 L0 是可见状态，不藏副作用）
- **[新增] 失败 turn durable 边界（借鉴 hermes 先持久化后发布，4.4）**：`failure_boundary_content`（`context_overflow` 排除码返 None 不补写；未知 code ⇒ `unknown_error`）+ chat 两路径接线——非流式 ask 失败在 **re-raise 之前**补写（route=error、metadata 含 `failure_boundary`/`error_code`/`http_status`），流式 error-only 在 **yield error 帧之前**补写（`boundary_saved` 防多帧重复；正文已产出则走 finally 落 partial **不叠加**，一条 turn 一条 assistant 消息）；补写自身失败绝不抛出
- **[测试] 新增 `tests/test_p54_tool_guardrails.py`（39 条）+ `tests/test_p55_rollback_plan_mode.py`（20 条）+ `tests/test_p56_failure_boundary.py`（15 条）**：判级阈值/拦截语义/mutation 清零/ASCII 提示词防污染；executor 三接点 flag 两态 + task_retry skip/终局回喂 + replanner rationale + dedup 引擎与两接点；计划态 flag 治理/声明契约/title/HITL 免审批/存储四契约/生命周期幂等与诚实失败/E2E 经 executor 进出；边界 helper 格式与排除码/非流式先持久化后 re-raise/流式 error-only、防重、partial 不叠加/flag-off 两路径逐位等价
- 回归：全量 **2594 → 2668 passed / 14 skipped / 0 deselected**（2026-10-01）；`ruff check agentix tests` 全绿（`ruff format` 非本仓门禁）；p41（含 3 新键）33 条随全量通过

### P3 · 渐进上下文：指令预算 + 压缩阶梯 + 逐步重建 + 写读闭环 + 只读诊断（2026-10-01）
- **[新增] 两个 flag（默认关/0，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+Agent 分组行+p41 名单）**：`CONTEXT_COMPACT_LADDER=False`（L4 keep-last 窗口阶梯）、`AGENT_CONTEXT_BUDGET_TOKENS=0`（AgentLoop 逐步重建预算）
- **[新增] 指令层预算纪律（借鉴 claw-code 3.1，4k/12k 两级）**：`instructions.py` 注入前 **sha256 内容 hash 去重**（保首次出现 ⇒ 前缀稳定）+ **总预算 `TOTAL_INSTRUCTION_CHARS=12000`**（从尾部高优先级：会话层向前保配额，低优先级块先截后弃并附 `…[指令块已截断]` 提示）；**零新 settings 键**（延续本模块零键决策）；不重复、合计预算内的输入两种机制均 no-op——生产输出与既有行为**逐字节一致**（单文件 4k 上限先截再过总预算，与 claw-code 4k/12k 同源）
- **[新增] L4 渐进压缩重试梯（借鉴 claw-code 3.2）**：`CONTEXT_COMPACT_LADDER=true` 时 `maybe_compact` 单次尝试升级为 keep-last 窗口阶梯 **4→2→1→0**——每轮只压缩头部、尾部行原样保留（最新上下文不进压缩面），空输出/五要素被拒 ⇒ 换更小窗口再试，**压缩器异常立即回退**（provider 故障换窗口无意义），四轮耗尽/仍超预算回退 Phase 18 丢弃循环（确定性终止）；flag-off = Phase 22 单次语义逐字节不变（`TestL4On` 的 `calls==1` 契约原样保留）
- **[新增] 主循环渐进上下文重建（补对照矩阵行 3「一次性构建 budget=0」缺口）**：`AgentLoop` 新增 `context_budget_tokens`（默认 0 = 只在启动时构建一次，trajectory=()/budget=0 与基线逐位等价）；>0 = 每步执行前按累计观测重建上下文并按预算确定性裁剪，重建异常 ⇒ 与初始构建同款 `context_build_failed` 终态（emit 后返回不 hang）；值由组合根 `agent_loop_adapter` 读 settings 注入——`AgentLoop` 保持不 import config 的纯编排约束
- **[新增] `read_artifact` 取回工具（补对照矩阵行 3「隔离正文无取回工具」+ P1 写→读闭环）**：`artifact_read.py`（L0 只读 `side_effect=False`，名称不命中风险词表 ⇒ 有效 LOW 任何模式免审批；schema 类名==注册名 title 契约——`_spec_for_schema` 回查不 miss，延迟检索/tool_policy 过滤均可见）；与写工具同 `TASK_EXEC_ENABLED` gate 挂声明+handler（`task_tools_bridge`，flag-off 零注册）；未知 ID fail-fast、`max_chars` 默认 4000/≤16384 超长截断+提示（防整库倾倒进上下文）；可经 `TOOL_DEFERRED_TOOLS` 延迟并由 `tool_search` 召回（P2 机制对新工具成立）；任务管线可执行菜单自动收录
- **[新增] 诊断只读契约（借鉴 claw-code 3.6）**：`observability.context_report()` 纯函数——预算常量/flag 现值/状态栏信任源/阶梯轮次快照，契约（测试钉死）= 不发 provider 请求、不走 `get_observability`（不碰 Redis）、两次调用逐字节一致、JSON 可序列化；`GET /ops/context` 只读端点暴露
- **[测试] 新增 `tests/test_p52_progressive_context.py`（29 条）+ `tests/test_p53_read_artifact.py`（23 条）**：指令去重/尾部优先预算/边界 no-op/4k 先截；阶梯 flag 治理/换窗序列（4→2 行入参实测）/异常立即回退/仍超预算走满四轮/四轮耗尽/引擎丢弃循环收敛；逐步重建单次基线/逐观测增长/重建失败终态/预算裁剪/组合根注入两态；read 声明与对账/title 契约/roundtrip/只读不改 store/HITL 免审批/截断与非法入参/延迟召回
- **[修复] p49 夹具卫生**：`_TASK_TOOLS` 补 `read_artifact`（flag-on 声明缓存清理与注册表摘除成对，防跨测试泄漏）
- 回归：全量 **2542 → 2594 passed / 14 skipped / 0 deselected**（2026-10-01）；`ruff check agentix tests` 全绿（`ruff format` 非本仓门禁）；p41（含 2 新键）随全量通过

### P2 · Skills/Registry/动态调度：注入方 + 延迟召回 + 快照热重载 + 对账（2026-10-01）
- **[新增] 四个 flag（默认关/空，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+工具分组行+p41 名单）**：`TOOL_SELECTION_ENABLED=False`、`TOOL_SELECTION_MAX_RISK=L1`、`TOOL_DEFERRED_TOOLS=""`、`TOOL_RESULT_CONTRACT_ENABLED=False`
- **[新增] `tool_policy` 注入方（双层闸 L1 从死读变活，2.4/2.5）**：`selection.build_tool_policy()`（flag-off 返 None；非法 max_risk 记 error 不抛不注入）+ `chat_service._inject_tool_policy`（flag-off **不触碰 config**，逐位等价）接入 **ask 与 stream 两条图路径**（源码计数守卫断言 2 处）；注入 → `tool_selection` 节点既有读取点 → `select_tool_schemas` 声明级过滤生产可用；与执行层参数感知有效级判定分工（selection.py 分工说明不变）
- **[新增] 延迟工具面 + `tool_search` 召回（2.2，借鉴 claw-code ToolSearch）**：`TOOL_DEFERRED_TOOLS` 非空 ⇒ `get_active_tools()` 剔出常驻 schema（`DISABLED_TOOLS` 两层裁剪可组合、注册表 spec 永不删）+ 声明清单挂 `tool_search` + `main._startup` 装配；**召回面 = 声明全集（含延迟工具）**，返回名/描述/风险级/入参 schema；`tool_search` 自身不可被延迟（机制自锁=配置笔误，静默剔除）；空列表 = 不声明不装配，声明清单与基线逐位等价
- **[新增] Tool Ranking 层（selection.py，补齐选择链第五层）**：`score_tool_match`（ASCII 词 + **CJK bigram** 分词、name×2 权重、按 query 数归一、确定性无 I/O）+ `rank_tool_schemas`（降序+声明序稳定截断 top_k，空查询/零分 fail-open 不丢工具）
- **[新增] Skills manifest 快照缓存 + 热重载（2.3，借鉴 hermes 快照比对/`reload_skills`）**：`loader.scan_fingerprint`（只 stat 不读不解析，posix 路径跨平台可比）+ `SkillRegistry.maybe_refresh`（指纹变才重扫、双检锁、`_skills` 原子替换、读取后取指纹留收敛余地）；`get_registry()` 每次访问附带指纹检查——目录增删改**无需 `reset_registry`** 即被下次访问看见，指纹未变**零重扫**（扫描调用计数守卫）
- **[新增] 规范即测试（2.3，借鉴 hermes test_authoring_standards）**：`skill_standards_errors` 单一真源（description ≤120 / anti_when ≤240 / 正文 ≤8000 不被 read_skill 截断 / name==目录名）+ `MAX_DESCRIPTION_CHARS`/`MAX_ANTI_WHEN_CHARS` 上限；shipped `skills/**` 全量过规范的守卫测试
- **[新增] 三注册表对账 `audit_tool_registries`（2.1，借鉴 hermes dispatch 契约）**：声明清单 ⇄ spec 注册表 ⇄ handler 表漂移分类（declared_no_handler / handler_no_spec / dynamic_handlers / stale_exemptions），`KNOWN_BYPASS_TOOLS` **逐项署名理由**（search 组件直连 + 4 KG 工具节点直连）；只读不改表，`main._startup` 每次启动审计（仅漂移时 warning，不阻断）；新增 `ToolExecutor.handler_names()` 只读视图
- **[新增] 固定裁决顺序全链序列守卫（2.5）**：既有测试只逐闸单测，现断言 **risk → permission → ask → observe → execute** 完整序列（本地 executor + 权限/审批/观测探针），三个拒绝路径各自证明「后序一个都不许发生」
- **[修复] 执行契约三处（2.1）**：非 mapping 入参 `dict()` 崩溃 → 转 failed `invalid_arguments`（不再打穿调用栈）；docstring 澄清「不外抛」唯一例外 = `CancelledError`（吞取消是更严重违规，测试钉死传播）；返回值契约 flag-on 时 handler 成功值必须 JSON 可序列化，否则**立即** failed `invalid_result` **不重试**（检查放 try/else 分支——契约违规不落回 except 被当 handler 故障），flag-off data 原样透传
- **[测试] 新增 `tests/test_p50_skills_registry.py`（23 条）+ `tests/test_p51_tool_policy_search.py`（23 条）**：快照热重载（零重扫计数/增删改免 reset）/规范即测试/对账四态+只读性/裁决序列/契约三面；flag 治理/注入方/延迟拆分/召回含延迟工具+schema/排名确定性+CJK+top_k
- 回归：全量 **2496 → 2542 passed / 14 skipped / 0 deselected**（2026-10-01）；`ruff check agentix tests` 全绿；p41（含 4 新键）/p35 随全量通过

### P1 · 任务执行链路：task-query 路由 + 声明式任务管线（2026-10-01）
- **[新增] 两个 flag（默认关，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+p41 名单）**：`TASK_EXEC_ENABLED=False`（任务执行主开关）、`AGENT_MAX_REPLANS=0`（ge=0，>0 才注入重规划器）
- **[新增] task-query 路由三件套**：`lg_states.TaskRouter`（pydantic Literal 扩展 + `task-query`）、`lg_prompts.TASK_EXEC_ROUTE_BLOCK`（仅 flag 开追加到路由提示，flag-off 提示词与基线**字节等价**）、`router.analyze` flag-conditional `router_cls`/`router_prompt`/`allowed_types` + `route_query` Literal 加 `"execute_task"`（flag-off 撞到 task-query 降级 `create_kb_query` 并告警）
- **[新增] 执行节点 `execute_task`（`task_handler.py`）**：flag-off 返回 `{}` 零副作用；flag 开调 `run_task_execution` → `bridge_observations_to_trajectory` → `map_loop_result`，回写 messages（AIMessage tool_calls 补 id/type）/保留 prev 字段的 `TaskRouter`/sources；图节点 8 → **9**（`lg_builder.add_node` + `ROUTE_HANDLERS`），p24/orchestration-rationale/PROJECT_SPEC 三处节点数 claims 同步
- **[新增] 声明式任务管线 `task_execution.py`**：可执行菜单 = **has_handler ∩ registry（治理链路边界，有意设计）**；拆解（注入步骤优先，否则 LLM temperature=0/role=planner 可 monkeypatch）→ 规整（不按 MAX_ITERATIONS 截断——超预算交给执行侧 `budget_exceeded` 终态，与 AgentLoop 语义一致）→ `plan_waves` 分波 + 预算 chunking → `run_batch`（逐波、依赖串行）→ `LayeredVerifier` 校验 → 终态 `completed/verify_failed/decompose_failed/budget_exceeded/task_exec_disabled`
- **[新增] 任务写工具 `task_note`/`task_artifact`**（`task_writes.py` 2 schema + 校验：控制字符剥离/4000/200 上限；`task_tools_bridge.py` 双 flag gate + `main._startup` 装配）：side_effect=True、timeout 10s、必填 `control`，注册即入 HITL review_required（`CITATION_ROUTES` 不含 task-query，拦截链路复用既有 `get_review_gate`）
- **[新增] 重规划接线**（`agent_loop_adapter.py`）：`AGENT_MAX_REPLANS>0` → `RetryReplanner(max_replans=n)` 并传 `max_replans`/`replanner`；=0 一律不传（字节等价）
- **[测试] 新增 `tests/test_p49_task_execution.py`（33 条）**：flag 治理（默认 off/零注册/提示词字节等价 fake-LLM/降级路由）、flag-on（TaskRouter schema、非法类型不派发、图节点、ROUTE_HANDLORS、Literal）、写工具 roundtrip/validation/控制字符/HITL spy、管线 happy 2/2+并行波+LLM 拆解+未知工具+空拆解+预算+全失败+HITL+disabled、节点回写与诚实降级、重规划 0/2 两态
- **[文档] 说明书/规划同步**：`orchestration-rationale.md`、`PROJECT_SPEC.md` 节点清单 8 → 9 并刷新 `lg_builder.py` 行号引用（build_graph :273 / add_node :287-298 / edges :300-303）
- 回归：全量 **2463 → 2496 passed / 14 skipped / 0 deselected**（2026-10-01）；`ruff check agentix tests` 全绿（`ruff format` 非本仓门禁）；p41 文档守卫随全量通过

### 项目说明书 · 调研全量输出 + Word 图文版（2026-09-30）
- **[新增] `docs/PROJECT_SPEC.md`**（297,972 字节 / 2,702 行 / §0–§22 共 23 章，p41 守卫 33 条全绿、154 处路径引用零缺失）：面向新人的实现级说明书——技术栈与分层架构、两套并行编排（L2 LangGraph 默认开 / L3 AgentLoop `AGENT_LOOP_ENABLED=False`）、AgentLoop 主循环与四终态、LangGraph 扁平主图拓扑、**双 SSE 链路辨析**（`/api/v1/retrieval/chat/stream` 检索域 RAG vs `/api/v1/chat/stream` LangGraph 图）、五层中间件、文档入库与索引、质量与发布门禁、端到端双链路对照；章节级 `文件:行号` 定位，事实基准 = 2026-09 源码实测
- **[新增] Word 图文版 `docs/My-Agentix项目说明书.docx`**（1.4MB）：pandoc 3.8 从说明书转出，YAML 标题页 + 两级目录 + **9 张 matplotlib 自绘架构图**（总体分层 / 双编排总览 / AgentLoop 主循环 / LangGraph 拓扑 / 检索域 SSE 链路 / 五层中间件 / 入库链路 / 质量门禁 / 端到端对照）按章插入（§1/§2/§5/§7/§9/§13/§18/§20/§22）；解包校验 media 9/9、图注 9/9、章节 22/22，Markdown 源保持纯文本不动
- **[文档] README §14 文档索引**新增「项目说明书」首行（指向 md 源 + docx 图文版）

### 穿插批次 B/C · 审计遗留收口（2026-09-28）
- **[新增] B1 流式 idle 看门狗**（`chat_service.py`）：新 key `STREAM_IDLE_TIMEOUT`（默认 60s，settings/selfcheck/`.env.example`/configuration.md/p41 五处同步）；`async for` 循环体墙钟改**手动迭代**——每次取事件包 `asyncio.wait_for(stream_iter.__anext__())`，静默挂起（非 LLM 节点卡死/网络黑洞）→ error 帧 + 既有「恰好一次 done」兜底 + `_AGENT_SEM` 槽位归还 + 循环后 `aclose()` 回收上游；原墙钟检查保留为第二层
- **[新增] B4 跨标签页会话同步**（`knowledge/utils/conversationSync.ts`）：`BroadcastChannel('agentix:conversations')` 广播 `create/delete/rename` 三事件——useConversationChat 发新建（草稿落库/首条消息）与删除并**合并接收**（upsert/改标题/移除，保留 local 草稿与分页、删目标时才清选中），useKbModalChat 发 rename/delete（弹层只发不收）；无 BroadcastChannel 环境静默降级为单标签页
- **[变更] B3 auth 解耦**：token 实现提升共享层 `web/src/api/token.ts`（与 `api/config.ts` 同层）；knowledge `auth/token.ts` 改 re-export（存量零改动），chat 两处 import 改共享层——**chat → knowledge 反向依赖清零**
- **[变更] B2 SSE 词汇双写（单列小批）**：chat（application 12 处）+ retrieval（usecases 15 处含 thinking_chunk 前奏 + router err 帧）所有 yield 同时输出 `event` 与 `type`；`_to_sse`/`ChatStreamChunk` 补 event 透传、retrieval router 去 `pop("type")` 双键并行；前端两解析器**优先 `event` 回退 `type`**（knowledge 在 `readChatSse` 规范化、chat 在 `dispatchChunk` 分发）——**不合并解析框架**，双稳定两个发布周期后弃 `type`（另立项）
- **[新增] C1 消息列表窗口化**（`ChatMessageList.vue`）：>200 条折叠中段（首 50 + 尾 150 + 可点击折叠条「已折叠 N 条」，展开后保持全量、新消息不重折叠）
- **[变更] C2 tests lint 清零并入门禁**：一次性清偿 55 处（49 F401 `--fix` + 6 F841 手工**保留有副作用调用**改裸调用 + 1 处 B017 noqa 修正）；**删除 `tests/**` per-file-ignores 豁免**（tests 与生产代码同一严格 select）；门禁命令纳入 tests：文档三处（README §11 / audit §4 / 规划 §7）+ 工具链两处（`make lint` / ci.yml lint job 的 ruff 范围同步扩 `tests`，p46 平价守卫兼容；本地与 CI 同为 ruff 0.5.7，范围一致）
- **[移除] C3 死导出**：`PROGRESS_STAGE_LABELS`（全仓无引用，stage 文案由后端 `progress.message` 直接渲染）；**重复空态组件评估后不立项**——三处空态语义/样式/所属模块均不同，跨模块抽共享将重建 B3 刚解除的反向依赖（理由落档 audit §1）
- **[文档] 基线同步（2458 → 2463 passed / 14 skipped；vitest 113 → 121）**：README §11 / current-tree（137 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头 + §8 三行状态 / module-inventory §6 勾选；audit §2 B1-B4+C1-C3 移入 §1 已修复表（C4 保留）+ §3 批次 B/C 标 ✅
- **[文档] README 能力矩阵同步（Phase 28–31 + 批次 A/B/C 入册）**：§2 评估能力补声称一致性/敏感性/隐私；§4 记忆补画像卡（`profile_card`）、演化回路补两载体/三状态落盘/睡眠学习；§6 智能路由补澄清决策（`CLARIFY_ROUTING_ENABLED`）、对话管理补跨标签页同步；§7 新增流式看门狗（`STREAM_IDLE_TIMEOUT`）与预算意识（`budget_notice`）两条可靠性；§8 评测表补分层 golden + 评估二期行；§11 安全面补归属校验/上传同口径鉴权/限流口径 + 工具链口径注明 ruff 含 `tests`；§14 文档索引新增 Phase 28–31 规划行并更新审计行描述
- 回归：全量 **2463 passed / 14 skipped / 0 deselected**（2026-09-28）；扩展守卫批 162 绿；`ruff check agentix scripts evals kb_ingest tests`（含 tests 新门禁）全绿；`mypy agentix/domain` 0 错；前端 `vue-tsc` 0 错 + vitest **121** 全绿

### Phase 29 · 记忆结构化二期：JSON Cards + User as Code（2026-09-28）
- **[新增] 结构化画像卡 `profile_card`**（`agentix/application/services/long_term_memory.py`，Ch3 §3.5）：`UserProfile` 只增 `profile_card` 字段——`to_dict()` 仅在 `MEMORY_PROFILE_CARD_ENABLED` 开时写入该键（flag-off 与基线 7 键字节等价，旧键一个不动）；`from_dict` 恒读、旧数据无卡 ⇒ `None` 回退旧字段；卡片形状 `{schema_version, legal_domain_prefs, usage_habits, user_constraints, conflicts}`（`PROFILE_CARD_SCHEMA_VERSION=1`、领域列表按 `(-count, topic)` 截断 20）
- **[新增] User as Code 派生纯函数**（Ch3 §3.6）：`fold_profile_card(card, event)`（单事件折入——usage 极值/消息累加、领域计数（事件内主题去重）、可选 `preferences` 观测；**同键冲突只标记 `conflicts`、原值保留不覆写**，与 R3-8「新版本胜出」语义刻意不同）为增量与重放**共用唯一核心**；`derive_profile(events)` 按 `(created_at, session_id)` 排序重放 ⇒ 派生幂等（replay ≡ 增量、重放确定性）、旧字段沿用首见序/容量 50 语义、空日志返回无卡（不伪造）
- **[新增] 触发器接线**：`LongTermMemory.refresh_profile_card`（只更新卡片不动旧计数器、无事件/flag-off 零写入）由 `memory_consolidation` handler 在 **flag 开且非 dry_run** 时调用并写回报告 `profile_card`（flag-off 报告形状与基线一致；dry_run 零写入约定不被破坏）；**读路径不隐式重放**（读零成本）；`get_context_for_query` 卡片区段仅 flag 开拼装（Gotcha 3：开启时点已文档化于 configuration.md）
- **[配置] `MEMORY_PROFILE_CARD_ENABLED: bool = False` 五处同步**（settings/selfcheck/`.env.example`/configuration.md 表 + p41）；**执行注记**：原案 flag 名 `MEMORY_CARDS_ENABLED` 已被 R3-8 语义卡片占用（默认 true、不同特性）⇒ 按 Phase 28 正交开关先例改独立新名
- **[测试] 新增 `tests/test_agent_phase29_memory_cards.py`（20 条）**：flag-off 7 键精确等价/存储有卡不写盘/prompt 无卡片区段；roundtrip + 旧数据回退；冲突三值只标记不覆写 + fold 不突变输入 + 事件内去重计数 + 次序无关 + 增量 ≡ 重放幂等 + 空日志不伪造 + 容量 50 重放；handler flag-off 报告形状/dry_run 零写入/空日志无键/归档红线 flag 开用例；flag-on prompt 卡片段
- **[修复] `tests/fakes/redis.py`**：`zrange`/`zremrangebyrank` 负 stop 区间语义对齐真实 Redis（原实现 `stop=-201` 在小集合上误删全部索引，`store_summary` 真实调用链首次覆盖到）
- **[文档] 基线同步（2438 → 2458 passed / 14 skipped）**：README §11 / current-tree（137 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头 + §8 批三状态 + DoD 勾选（含 flag 撞名执行注记）/ module-inventory §6 勾选
- 回归：全量 **2458 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 干净 + 新文件严格 select 全过；`mypy agentix/domain` 0 错；Phase 24/R3-8/单测相邻记忆套件 72 条全绿

### Phase 30 · 进化闭环：knowledge/program 载体 + 提案三状态落盘 + 睡眠学习（2026-09-28）
- **[新增] 两载体扩展**（`agentix/agent/evolution/loop.py`）：`EvolutionCarrier` +`knowledge`/`program`（六值，Phase 12 断言同步）；门元组复用既有门键——KNOWLEDGE `("citation","scope_isolation")`、PROGRAM `("param_fidelity","verifier_logic_unchanged")`；payload 白名单校验 fail-closed：KNOWLEDGE 仅 `prompt_fragment/top_k/threshold/notes`（top_k/threshold 须数值）、PROGRAM 仅 `rules/notes` 且每条 rule 须声明式 `{id,when,then}`——越界即拒（`*_payload_keys_not_allowed:[...]`/`*_payload_type:*`/`program_rules_not_declarative`），触碰 `PROTECTED_VERIFIER_PATHS` 拒绝语义不变
- **[新增] 提案三状态落盘**：`proposal_sink` ctor 注入 + `_sink_record(proposal, state)` 五落盘点（校验拒/staged/回归拒/applied），去重键 `(content_hash, state)` append-only 幂等；`jsonl_proposal_sink(path)` 工厂（反斜杠归一、含 `evals/baselines/` 相邻段或空路径构造即 `ValueError`，fail-closed 不写盘）；无 sink 时内部状态与基线完全一致；导出 `agentix.agent.evolution`（+`KNOWLEDGE_PAYLOAD_ALLOWLIST`/`PROGRAM_PAYLOAD_ALLOWLIST`/`PROGRAM_RULE_KEYS`）
- **[新增] 睡眠学习 handler**（`task_handlers.handle_evolution_sleep`，`EVOLUTION_SLEEP_ENABLED` 默认关，五处同步）：flag 关 → `skipped disabled`；门槛（`min_signals=20`/`min_error_rate=0.1`）未达 → `no_op threshold_not_met`；dry_run 不占窗；端点预检（OPENAI_API_KEY/OPENAI_API_BASE 空 → `skipped no_endpoint`，先于 claim，no-op 路径不消耗窗口）→ 进程内 monotonic 幂等 claim（`force` 默认 False、window 86400，镜像 consolidation 语义）→ LLM temp 0.0 合成 → carrier 限 `knowledge|program` → `loop.propose` → `proposed/rejected` + staged/proposal_id/carrier + sink 记录；**绝不 `promote`/`run_regression`**；非法 JSON → `invalid_proposal_json:...` 不写档案；`EVOLUTION_SLEEP_PROPOSAL_PROMPT` 外置 `services/prompts.py`（静态指令在前、占位符在后，p23 合规）
- **[新增] `evolution_run.py --proposal-log`**（默认空=不落盘，stdout 与基线逐键一致）：设置时 sink 接线 + `report["proposal_log"]`；路径非法 → stderr + exit 2
- **[配置] 两 key 五处同步**：`settings.py`（`EVOLUTION_PROPOSAL_LOG=data/evolution/proposals.jsonl`、`EVOLUTION_SLEEP_ENABLED=false`）/ selfcheck `DEFAULT_IGNORED_MISSING` +2 / `.env.example` +2 行 / `configuration.md` 表 +2 行
- **[测试] 新增 `tests/test_agent_phase30_evolution_carriers.py`（41 条）**：载体门/knowledge/program 白名单与拒绝 reason/sink 三状态 + `content_hash` 幂等 + 无 sink 等价/jsonl 路径守卫（正反斜杠与 `evals/baselines` 拒）/settings-selfcheck-env 四方一致/sleep handler 12 条（flag/门槛/dry_run/端点预检/claim 幂等/非法 JSON/禁晋升）/`--proposal-log` 3 条（默认 stdout 逐键等价/接线/坏路径 exit 2）；Phase 12 四载体断言改写六值
- **[文档] 基线同步（2397 → 2438 passed / 14 skipped）**：README §11 / current-tree（136 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头 + §8 批二状态 + DoD 勾选 / module-inventory §6 勾选
- 回归：全量 **2438 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 干净 + 新文件严格 select 全过；`mypy agentix/domain` 0 错；守卫批（p41/dependency/ci_gate/p35/p47/p40/p36/hygiene）162 条全绿

### Phase 31 · 评估二期：分层 golden + 声称一致性 + 敏感性/隐私 + Best@k（2026-09-28）
- **[新增] 声称一致性检查**（新 `evals/claim_check.py`，纯 stdlib 无时钟，p42 合规）：条号正则族（`第X条` 阿拉伯数字 + 中文数字 + `民法典/刑法/宪法…` 法名归一）抽取答案声称，与「实际 `sources` ∪ golden `expected_sources`/`expected_citations`（人工真值并入证据池）」比对 → `ok/mismatch/no_claims/missing_prediction`；无 trajectory 行诚实 `applicable=false` 不计分；报告含 `checked/ok_count/mismatch_count/mismatch_ids/note`（**只标记不判分，mismatch 需人工复核**）；与 `run_eval.py` 互不反向 import（脚本直跑顶层名 / pytest 走 `evals.claim_check` 双路径导入）
- **[新增] 分层 golden 汇总**（31.1 决策：**golden 文件不动，拒伪造**）：新行写 `difficulty`/`source` 可选字段，旧行缺省由 runner 兜底（`unknown`/`legacy`）；`_aggregate_by_difficulty` 按组出 9 键、组键净化；`feedback_ingest.build_candidate` 增 `source="feedback"`
- **[新增] 隐私口径**（`privacy_*`）：PII 离线检测（手机号/身份证/银行卡纯正则），命中行 `privacy_flagged`——**只记 id 与类型、原文零留存**；citation/cc/mc/reject applicable 类池与 reject 两 count 排除 flagged（`privacy_applicable_excluded` + `privacy_note`），全量口径与 `free_hit`（无要求行数不过滤）不变
- **[新增] Best@k 分项**（`best_at_k*`）：`BEST_AT_K_COMPONENTS` 5 分项（route/citation_hit/citation_correctness/must_contain/reject）每任务取 k 次 max 后跨任务均值；无预测 → `no_predictions_for_any_golden_row`、`repeat=1` → `n_a_single_run` 诚实标 n/a；`pass_k/pass_at_k/mean_task_success` 口径不动
- **[新增] 敏感性微扰**（`--sensitivity`，仅 `--live`/`--record`）：四确定性变体（original/prefix_formal/suffix_source/polite_opening）只包指令不改问题本体，`compute_sensitivity_ranges` 按 `sensitivity_variant` 分桶出 5 指标极差（<2 变体不出键）；run_live 行加变体标记 + session_id 插 `variant_seg`，关时与旧版逐字节一致；**离线/`--compare` 带 flag → 提示 + exit 2**；run_offline 自动检测出键
- **[测试] 新增 `tests/test_agent_phase31_eval_layering.py`（40 条）**：改前快照 `phase31_pre_snapshot.json` 旧键逐位字节等价 / 新键前缀白名单（`difficulty_*`/`privacy_*`/`best_at_k*`/`sensitivity_*`/`claim_consistency`，只增不进门禁）/ claim 两例（说对做对 ok、说对做错 mismatch + `mismatch_ids`）/ 无预测诚实 skip / PII 命中从 applicable 分母排除且原文零留存 / 检测器命中与不命中 / best_at_k repeat=1 n/a / 敏感性范围跨变体出数 + 缺省无键 / 离线带 flag exit 2 / `feedback_ingest` source=feedback
- **[文档] 基线同步（2357 → 2397 passed / 14 skipped）**：README §11 / current-tree（135 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头 + §8 状态 + DoD 勾选 / module-inventory §6 勾选 / `evals/README.md`（§2 分层可选字段 + §4.6 五族报告键 + §5.3 `--sensitivity` + §5.4 输出键）
- 回归：全量 **2397 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 干净 + 严格 select 全过；`mypy agentix/domain` 0 错；`test_ci_gate_contract` 门键零变化原样通过

### Phase 28 · 自主护栏：需求澄清决策 + 预算意识（2026-09-28）
- **[新增] 需求澄清决策**（`CLARIFY_ROUTING_ENABLED` 默认关，五处同步）：新 `agentix/application/agents/clarify.py`（`should_clarify` 纯函数：口语过短 / 法域词表冲突 / 主体实体缺失 / 路由 confidence < 0.5 首命中，零新增 LLM + `clarify_reply` 结构化应答含候选项）；`general_handler` 两入口 `_maybe_clarify` 前置（`AgentState` 新通道 `clarify_armed` 单轮约束——连续澄清至多 1 次、第二轮强制路由，判定明确时回写解除；不入 HITL、不计失败、不改路由终态；flag-off 入口与基线字节等价）
- **[新增] 预算意识**（`BUDGET_NOTICE_ENABLED` 默认关，五处同步）：状态栏白名单 `STATUS_BAR_TRUSTED_KEYS` +常量键 `max_iter`（`agent_loop_adapter._budget_status_bar` 构造侧 flag-gated 写入，同配置前缀哈希一致）+ 新 `agentix/application/services/budget_notice.py`（剩余 ≤2 步轨迹 append `budget_notice`，append-only 拒进状态栏——KV Cache 纪律）；接线：AgentLoop 支路 `loop.py` 注入 `budget_observer`（:145 检查处，observer 未注入零调用）+ 图主循环流式支路按 `langgraph_step` 恰一次（子图 `langgraph_checkpoint_ns` 过滤；非流式 ainvoke 无观测点不插，防双写）；`budget_exceeded`/`MAX_ITERATIONS` 终止语义不变
- **[测试] 新增 `tests/test_agent_phase28_autonomy_guards.py`（28 条）**：flag-off 字节等价 / 歧义三信号澄清 / 单轮约束与解除 / 状态栏白名单+前缀哈希 / `budget_notice` 区边界与前缀不变 / observer 区内回调 / `budget_exceeded` 语义回归 / observer 异常不破环
- **[文档] 基线同步（2329 → 2357 passed / 14 skipped）**：README §11 / current-tree（134 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头 + §8 状态 + DoD 勾选 / module-inventory §6 勾选
- 回归：全量 **2357 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 干净 + 严格 select 全过；`mypy agentix/domain` 0 错

### 审计批次 A · 前置安全收口（2026-09-28）
- **[安全] A1 会话/历史归属校验（IDOR 收口）**（`v1/deps.py` + `v1/chat.py` + `v1/sessions.py` + `chat_service`）：新增 `resolve_v1_caller`（JWT regime 身份 = token sub；匿名 regime 取 `X-Device-ID` 头；**无声明 = 现状不变**，strangler 纪律）与 `session_visible_to` 归属判定——history/clear/session 详情/删除/重命名/messages/snapshot 持身份跨用户一律 **404**（防存在性枚举）；chat 流式/legacy GET 写路径先于业务链路拦截；create 强制 `user_id=身份`、list/count 跨身份 404、PATCH 以身份覆写 `user_id`；前身份期 `default_user` 匿名桶对声明身份保持可见（迁移不破坏）；新增 `ChatService.get_session_owner`
- **[安全] A2 上传读/删鉴权（写读不对称收口）**（`v1/upload.py`）：GET `files/images` 补 `require_v1_user`（flag 开无凭证 → 401，与写同口径——原「渲染面读豁免」契约翻转，见 `audit-2026-09-28.md` §1/§3 实施注记）；上传落 owner sidecar（`UPLOAD_DIR/.owners/<name>.owner`，独立目录防被读走）——读/删比对归属，跨身份/匿名读有主文件 → 404，无 sidecar（历史/匿名文件）放行；DELETE 跳过 sidecar 目录并同步清理
- **[安全] A3 限流口径统一**（`infrastructure/core/ratelimit.py` + `retrieval/api/routers/chat.py`）：`rate_limit_dependency` 停写共享 `limiter.config`（原并发互踩地雷），改 `_limiter_for` per-params 派生缓存（同参数复用、异参数隔离、flag 关零开销）；retrieval `_chat_limiter` 由 import 期绑定改惰性热重建（`rate/burst` 签名变化即重建，运行期改配置即时生效）
- **[测试] 新增 `tests/test_p48_ownership.py`（21 条离线归属契约）**：跨 user 404 / 同 user 200 / 匿名现状不变 / header 覆盖 user_id / 匿名桶可见 / JWT regime（401、sub 身份、头不可伪造）/ 写路径先拦 / 上传 sidecar 读门禁与删除隔离——会话 CRUD 走内存替身、chat 走脚本 stub、上传走 tmp 目录，零 DB/零网络；`test_rate_limit_middleware.py` 扩 4 条 A3 用例；`test_v1_endpoint_auth.py` 上传读契约翻转 + 补 JWT 过鉴权进归属层用例
- **[文档] 基线同步（2303 → 2329 passed / 14 skipped）**：README §11 / current-tree（133 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头+§8 状态 / module-inventory §6 勾选 / `audit-2026-09-28.md` §2 P0 移入 §1 + §3 批次 A 标记完成
- 回归：全量 **2329 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 干净 + 严格 select 全过；`mypy agentix/domain` 0 错

### 优化迭代 Phase 28–31 立项（2026-09-28）
- **[文档/立项] `docs/migration/Phase28-31_优化规划.md`（v1.0）**：依据《Agent 全书笔记》对照 + `audit-2026-09-28.md` 分期拍板四主题——**批一** = 前置审计批次 A（A1 会话/历史归属 IDOR 校验、A2 上传读鉴权、A3 限流口径；安全修复默认开）+ Phase 28 自主护栏（`CLARIFY_ROUTING_ENABLED` 确定性澄清决策挂 handler 函数体不加图节点 + 状态栏 `max_iter` 常量键/轨迹 append `budget_notice` 预算可见性）∥ Phase 31 评估二期（golden 只增 `difficulty`/`source` 分层 + 新 `evals/claim_check.py` 声称一致性 + `--sensitivity` 提示敏感性 + PII 离线 `privacy_flagged` + `best_at_k`，全部报告键只增不进门禁）；**批二** = Phase 30 进化闭环（`EvolutionCarrier` +`KNOWLEDGE`/`PROGRAM` 载体与回归门、提案三状态 append-only 落盘 `data/evolution/proposals.jsonl`、`EVOLUTION_SLEEP_ENABLED` 睡眠学习 handler 只产提案不晋升）；**批三** = Phase 29 记忆结构化（`MEMORY_CARDS_ENABLED` JSON Cards + `derive_profile` User-as-Code 派生幂等）；缺口 10–12 另期；批次 B/C 随批穿插
- **[文档]** `module-inventory.md` §6 新增「优化迭代 Phase 28–31」立项勾选节 + `Agent_Core_2.0_规划.md` §9 头注/状态表同步

### 接口审查批次：前后端同步审查 + 渲染/契约修复（2026-09-28）
- **[修复] 流式主路径助手消息落库**（`v1/chat.py`）：原 `/api/v1/chat/stream` 只落库用户消息，助手回答仅存在于响应流 → 刷新即丢回答/引用；现 `event_stream` 缓冲 `answer_parts/final_route/final_sources`，`finally` 中用独立 `SessionLocal()` 落库（含 `sources` metadata），异常/断连路径同样保留已产出 token；非流式响应 metadata 过滤 `agent_state`（`_public_metadata`，与落库口径对齐）
- **[修复] 历史回放窗口**：`history` 端点从 `get_by_session`（**最旧 N 条** → 会话超长丢最新）改为新 `get_recent_by_session`（最新 N 条、正序）；`limit` 加 `ge=1, le=100` 下上界（原 `limit=-1` 在 SQLite 等价全量、PG 直接 500，`limit=0` 返回空数组让前端静默不恢复）；sessions 分页 `skip ge=0 / limit le=200`；`count_by_user` 默认 `active_only=True`（与 `get_by_user` 口径对齐）
- **[修复] 流式限流对齐**：`_stream_events_impl` 补 `await _AGENT_SEM.acquire()`（原 `ask` 有限流、流式主路径绕过信号量）+ `sem_acquired` 标志保证 `finally` 只释放自己持有的配额
- **[修复] SSE 终态解析**（chat 模块 `chatStream.ts`）：`ParserState.terminated` —— `error` 帧后的 `done` 帧（后端「恰好一次 done」兜底契约）不再把 outcome 翻转成 delivered；`credentials: "include"`；截断（`!done`）在 `useChatStream` 返回 `'failed'` 并追加中断标注，不再计为成功
- **[修复] 会话同步机制**（`useConversationChat.ts`）：`selectToken` 竞态守卫（await 后校验 + 切会话/新建时递增）、切会话先 `abortChatStream()`、`resolveConversationForSend` 统一草稿创建（history 分支防幽灵会话）、`openConversationById` 未命中本地列表时回源 `getConversation` 补齐、消息落位前校验 `userTempId`
- **[修复] 渲染展示**：`ChatMessageList` 进度条 `v-if` 顺序提到 pending 占位**之前**（原占位抢占 → fast 模式下进度条/阶段文案永远不可达）；`HomeView` 流式期间禁发送 + 错误横幅改 `!!errorText`；`ComposerBox`/`ChatWidget` 输入 `maxlength=5000` 与后端 `QUERY_MAX_LENGTH` 对齐；`serverResponded` 门控——已应答的流式失败不回退非流式（防重复答案），仅未触达业务才 `postChat` 兜底（knowledge 弹窗同步去 2 次重试循环）
- **[修复] 其他**：knowledge SSE `readChatSse` 残留尾帧派发 + CRLF 帧边界 + `finally releaseLock`；`retrieval` SSE `sse_generator` 异常结构化收口（原 500 中途断流无帧）；`agent_runs` 日志 `{}` 占位符改 `%s`（stdlib logging 格式错）；共享 API 配置层 `web/src/api/config.ts`（knowledge/chat 两套 `API_BASE_URL` 计算收敛为 re-export）
- **[测试] `tests/test_chat_api.py` 全量重写**：原文件零 assert + 字段名全错（`is_user`/`message` vs 契约 `message_type`/`content`）+ 关闭后用 client + 依赖 `localhost:8000`，2 条 `integration` 用例必然假通过；重写为 mini-app + stub ChatService 的 **12 条离线契约测试**（流式落库/error 路径部分回答/history 窗口与 limit 422/agent_state 过滤/legacy 长度上限/CRUD 窗口真实 SQLite/count_by_user 软删口径），`integration` 标记清零
- **[测试] 前端 +20 用例**：chat SSE 解析 7（终态双向丢弃/CRLF/跨 chunk 拼接/坏 JSON 跳过）、knowledge `readChatSse` 5、`parseHttpError` 6（422 detail 数组/AppError hint/非 JSON 兜底）、`ChatMessageList` 进度条优先渲染 2
- **[文档] 基线同步**：README §11 / current-tree / Agent_Core 头+阶段表 / Phase15-20 头+验证段 / Phase21-26 头 / module-inventory §6；新增 `docs/architecture/audit-2026-09-28.md` 架构审计（可优化点 + 下一步方案）
- 回归：全量 **2303 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 主检 + 严格 select 全绿；`mypy agentix/domain` 0 错；`vue-tsc` 0 错 + vitest **113** 全绿

### Phase 27 执行：死代码清理 + 文档收尾（2026-09-27）
- **[移除] `search_service` 全家**（evolution-assessment §4「标记 → 删除」两步流程的删除步，同批执行经拍板）：删 `agentix/application/services/search_service.py`（`SearchService` 类 + `generate_stream`）、`agentix/application/prompts/search_prompts.py`、空包 `agentix/application/prompts/`——依据：`SearchService` 全仓零 importer（含动态引用）、`generate_stream` 零生产调用、5 类事件 `search_start/search_results/direct_answer/direct_content` 零前端/测试引用；**只删 `generate_stream` 会留下零调用的 `_call_with_tool`/`search_tool`/prompt 孤儿（制造新死代码），故整体删除**
- **[测试] 防复活守卫**：`test_p40_import_health.py::test_deleted_search_service_stays_deleted`（同 `text2cypher` 先例：删除是审计结论，恢复前必须先给出调用方）；`test_previously_broken_modules_now_import` 守卫范围随删收缩（docstring 记录去向）
- **[同步] 引用清零**：`services/__init__.__all__`、`settings.py` DISPLAY_TZ 理由注释（删 cur_date 示例弹）、`.pre-commit-config.yaml` trailing-whitespace 排除表、`docs/architecture/extension.md` Prompt 位置、`current-tree.md` 目录树、`dependency-map.md` 工具簇消费者、`evolution-assessment.md` §4（处置列 + 保留项判定）/§8（Phase 24–26 完成、死代码已清理）
- **[文档] Phase15-20 陈旧勾选同步**：L4 压缩/工具序稳定+config_watch/Skills+Hooks → 已由 Phase 21–22 完成；附录 A 清单 LLM-judge → 已由 Phase 26 完成（record/quality.json/轨迹 baseline 仍 blocked）
- 回归：全量 **2291 passed / 14 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select 全绿；守卫 p40/p42/p24_deadcode/p17/code_hygiene 全过

### Phase 26 执行：评估三期——judge 去 stub + 配对显著性（2026-09-27）
- **[变更] `judge_similarity` 去 stub**（`evals/run_eval.py`）：惰性接 `LLMClient`（温度 0）+ `ModelGateway.resolve_model("standard")` 分档解析；`_judge_preflight` 端点预检——`OPENAI_API_KEY`/`OPENAI_API_BASE` 任一空 → `NotImplementedError`（CLI 退出码 3，提示含「校准」）；评委输出解析不出 0–1 → `JudgeError`（`main` 补捕获，同样退出码 3）**绝不给假分**；空回答确定性 0.0 不触网；`evaluate` 开头预检 fail-fast（不产半截结果）
- **[变更] judge 标注进 summary**：仅新增 `judge_model` / `judge_count` / `judge_mean`（`judge_*` 前缀只增不改——**五 metric 门结构零变化，judge 不进门禁**）+ `format_summary_line` 辅助指标行（含模型标注 +「仅参考、不进门禁——需人工抽样校准」）
- **[新增] `--compare A B` 配对显著性**（Ch7 §7.14）：两份预测各自离线打分 → 按 id 配对（交集排序、同 id 多行取首行、各边独有数如实报告）→ 五指标 percentile bootstrap 95% CI（**纯 stdlib** `random.Random` 固定 seed=1729 可复现，不引 numpy）→ CI 含 0 →「不显著（噪声区间）」；Δ = A − B；纯离线不写文件；golden 缺失/无配对/与 `--live` 同用 → 退出码 2
- **[文档] `evals/README.md`**：§4.4 重写（真实端点出分/诚实失败 + 校准四步：≥30 条抽样 → 双人背对背标注 → 一致性阈值 → 双 Judge 对照）+ 新增 §4.4.1（`--compare` 口径）+ §9 状态「已接入」
- **[测试]** 新增 `tests/test_agent_phase26_eval_judge.py` **19 用例**（DoD 全项：伪 client 出分带 model/温度 0 标注、无端点诚实失败、不可解析 `JudgeError`、空回答零触网、summary 键集断言、默认路径零构造、同分布不显著、构造差异显著、`run_compare` 端到端/缺文件/空 golden、`--live` 同用拒绝）；`test_p21_eval_harness.py` 两处桩函数测试改端点语义（**本地 `.env` 带真实端点凭据，原测试在新实现下会触网**——显式清空 + 伪 client）
- **[文档]** 规划 Phase 26 DoD 勾选 + §7 状态 + 落地注；module-inventory §6 勾选；基线同步 README §11 / current-tree（132 模块）/ Agent_Core 头+§9 / Phase15-20 / 规划头部；`test_ci_gate_contract` 全过（`quality.json` 门行为零变化）
- 回归：全量 **2290 passed / 14 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select 全绿；`mypy agentix/domain` 0 错

### Phase 25 执行：multi-agent 运行时防线——六失败模式（2026-09-27）
- **[准入] `ControlPlane` 并发/深度防线**：`check_admission(depth=)` + `max_concurrency`（0=不限=接线前行为）+ `spawn(..., depth=0)` 写 `meta["depth"]`；`get_control_plane()` 每次读 settings；`chat_service._invoke_tracked` 对 `AdmissionDenied` **fail-closed**（`coro.close()` 后上抛），注册故障仍降级 `return await coro`
- **[新增] `InProcessBus` 进程内消息总线**（`agentix/agent/multiagent/bus.py`）：subscribe/unsubscribe/discover/send（同步 `put_nowait`，未注册/已取消/满 → False+dropped）/recv/cancel（清在途）/resume/status/stats + `BusStats`——纯 asyncio，**零 Redis/Docker**；**不复用 Redis Stream 为 bus**（事件≠agent 通信）
- **[变更] `Manager` 注入化**：构造注入 `budget_factory`/`bus`（默认 None=直连路径字节等价）；`detect_artifact_conflicts` 纯函数（同 key 不同值 + 极性 accept/reject 并存）**只标记不改写**；`_record` 先 `bus.send` + run 起点为双方建信箱（幂等）；`_peer_cancelled` 早返回 `peer_cancelled:{id}`；所有返回经 `_outcome` 收 meta（conflicts/conflict_marked/bus status）；`lg_supervisor` `_budget_factory`（`MULTIAGENT_PEER_MAX_STEPS/MESSAGES`）/`_make_bus`（`MULTIAGENT_BUS_ENABLED` 默认关 → None）+ `defense_meta` 合入两处 `multi_agent` meta
- **[修复] `echo_overlap` 漏传**：轮次耗尽的最终 `detect_failure_mode` 此前恒传 `echo_overlap=0`——echo_chamber 永远退化为 livelock（六模式之三形同虚设）；现传末轮 `echo` + 预置 `echo=0.0`（`test_echo_chamber_high_overlap` 驱动发现）
- **[配置] 5 新 key 五处同步**（settings/selfcheck/`.env.example`/configuration.md/p41）：`CONTROL_PLANE_MAX_CONCURRENCY/DEPTH`=0、`MULTIAGENT_PEER_MAX_STEPS/MESSAGES`=10/20、`MULTIAGENT_BUS_ENABLED`=False——全部默认关/不限=接线前行为
- **[测试]** 新增 `tests/test_agent_phase25_multiaagent_guards.py` **26 用例**（DoD 全项：并发/深度越限拒绝、预算熔断、取消 fan-out、逻辑冲突/级联只标记、六失败模式运行时离线 fake peer、bus 原语、flag-off 直连等价、settings 镜像）
- **[文档]** 规划 Phase 25 DoD 勾选 + §7 状态 + 落地注（echo_overlap 修复入注）；module-inventory §6 勾选；基线同步 README §11 / current-tree（131 模块）/ Agent_Core 头+§9 / Phase15-20 / 规划头部
- 回归：全量 **2271 passed / 14 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select 全绿；`mypy agentix/domain` 0 错

### Phase 24 执行：记忆/知识深化——整理遗忘生产化 + Agentic RAG（2026-09-27）
- **[淘汰红线] `LongTermMemory.consolidate` 归档改造**：原状直接 DEL 淘汰摘要（违规）→ 改为「先归档正文 `ltm:archived:summary:{sid}`（与正文同 TTL，含 `source_message_ids` 证据链）再删原键」，report 新增 `archived_session_ids`（dry-run 报计划零写入；被合并掉的条目不另归档——并集语义已全文并入合并结果）；`try_claim_consolidation` 新增（`SET NX EX` 幂等窗，`force`/`window<=0` 旁路）
- **[新增] `memory_consolidation` task handler**（flag `MEMORY_CONSOLIDATION_ENABLED` 默认关，五处同步 settings/selfcheck/`.env.example`/configuration.md/p41）：flag-off `skipped` 零存储触碰；非 dry_run 走幂等窗，窗口内重复触发 `already_consolidated`；成功返回 `consolidated + report`
- **[新增] 重要性三因素纯函数**（`memory_consolidation.py`）：`access_frequency_score` / `recency_decay_score`（指数半衰期，`MEMORY_RECENCY_HALF_LIFE_DAYS` 接线）/ `uniqueness_score`（主题稀有度）+ `importance_sort_key` 字典序 `(频率, 衰减, 独特性)`——一/二键与旧 `(access_count, last_accessed)` 同序严格兼容，独特性仅三级 tie-break；接进 `plan_summary_eviction` 超容淘汰
- **[新增] Agentic RAG 查询改写重试**（flag `AGENTIC_RAG_ENABLED` 默认关，五处同步）：挂 `general_handler.create_kb_query` 函数体（扁平/supervisor 双拓扑收敛点，不动 L2 图结构）；空检索 → 改写重试 **≤2 次**（总 workflow 调用 ≤3）→ 仍空保留 finalize 诚实文案；每次尝试进 trajectory（`kind=retrieval, name=agentic_rag_rewrite`，懒 import + 全吞）；改写 prompt 外置 `lg_prompts.KB_QUERY_REWRITE_PROMPT`（过 p23 三断言）；`tests/fakes/redis.py` 扩 `set(nx=)`
- **[测试]** 新增 `tests/test_agent_phase24_memory_rag.py` **25 用例**（归档+证据回溯、dry-run 零写入、handler flag 门/幂等窗/force/关窗、读路径不淘汰、三因素纯函数 + 淘汰排序、flag-off 单 invoke 零轨迹等价、flag-on ≤2 改写+轨迹计数+步数有界+诚实声明、改写 LLM 失败降级、轨迹失败吞、prompt 外置三断言）
- **[文档]** 规划 Phase 24 DoD 勾选 + §7 状态 + 落地注（挂点偏离决策表说明）；module-inventory §6 勾选；基线同步 README §11 / current-tree（130 模块）/ Agent_Core 头+§9 / Phase15-20 / 规划头部
- 回归：全量 **2245 passed / 14 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select 全绿；`mypy agentix/domain` 0 错；`vue-tsc` 0 错 + vitest 93 全绿

### Phase 23 执行：交互二期——出站通知 + Redis Stream + 事件矩阵（2026-09-27）
- **[新增] `notify_user` 出站通知 capability**（Ch6 §6.2；flag `NOTIFY_USER_ENABLED` 默认关，五处同步 settings/selfcheck/`.env.example`/configuration.md/p41）：`capabilities/tools/notify_user.py`（`user_notification` 事件类型、priority 白名单、4000 字符上限 + 控制字符剥离 fail-fast、L1→MEDIUM、`side_effect=True`、capability 标签 `interaction`）；注册经 `registry_init._build_declarations` flag-gated；**组合根注入** `application/services/notify_bridge.py::install_notify_user_tool`（capabilities 不反向 import agent）→ handler 经 `get_event_port().publish` + trace_id 落盘 → `main.py` flag 装配（HOOKS 块后）
- **[新增] `EVENT_PORT_BACKEND=redis_stream` 第三实现**：`agentix/agent/events/redis_stream.py::RedisStreamEventBus`（键布局 `agentix:events:{stream:|rec:|dedup:|index}`；XADD 前 SET NX 占真实 event_id 防投递竞态、XDEL 原子认领、LRANGE 倒序 `list_recent`、async 面 `async_get`/`async_pending_count`；**懒连接**——构造不连库）；`build_event_port` 分派第三值（init 异常回退 memory + 告警）；`EVENT_PORT_BACKEND` description 扩三值 + `.env.example`/configuration.md 注释同步；**分层偏离落地注**（落 `agent/events/` 而非 infrastructure，理由入 docstring + 规划 §3）
- **[新增] `GET /ops/events` 事件查询端点**：`ops_router` 惰性 import + `inspect.isawaitable` 鸭子扩展面（memory 按插入序倒序 / jsonl 委托 / redis async）；`InMemoryEventBus`/`JsonlEventBus` 补 `list_recent`
- **[测试]** 新增 `tests/test_agent_phase23_interaction.py` **47 用例**（DoD 全项：flag 门三面零注册、校验/HITL MEDIUM 拦截、execute→ops 可查、backend 分派、三 backend 参数化契约、去重/超时/取消/并发/cross-restart 矩阵、redis 连接错误向上传播不伪造）；37 过 / **10 条 redis 诚实 skip**（本机 6379 关，非伪造）
- **[文档]** 规划 §3 DoD 勾选 + §7 状态 + 落地注；基线同步 README §11 / current-tree（129 模块）/ Agent_Core §9 / Phase15-20 / 规划头部
- 回归：全量 **2220 passed / 14 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select 全绿；`mypy agentix/domain` 0 错；`vue-tsc` 0 错 + vitest 93 全绿

### 前后端契约显示同步 + 文档清理 + 演进评估（2026-09-27）
- **[同步] knowledge 模块**：`ChatMessage` 补 `progress/answer_id/trace` 三字段透传；`useKbModalChat` 回放映射补 `answer_id`（`KbModalMessage`/`ChatPostEnvelope` 类型补齐）；`PROGRESS_STAGE_LABELS` 删 4 个后端不产的死键；`traceTimeline` 加 `progress` 分支 + 用例；`ChatStreamPayload` 补 `request_id/message_id/terminate_reason/question_message_id`，finished 帧 `terminate_reason` 上屏提示
- **[接线] chat 模块**：`ChatWidget` 接 `streamChat` 流式主路径（`SendOutcome` 三态，网络失败回退既有非流式保降级、error 帧不回退防二次执行、`activity` 打点 + 滚底）；`formatSource` 字符串/对象双形态；`restoreHistory` 读 `meta.sources`；后端 `v1/chat.py` 助手消息 `metadata["sources"]` 持久化
- **[修复] 引用链字段**：`CitationData`/explanations chunks/`get_citations_from_docs`/`citation_hydrate`/Answer persist 白名单补 `effective_status/replaced_by`
- **[文档] 旧版文档删除 8 份**：`CONFIG_SUMMARY` / `OPERATIONS` / `OPERATIONS_RUNBOOK` / `DEPLOYMENT` / `README_CHATBOT` / `README_DEV`（`VITE_API_KEY` 待并内容并入 `web/README.md` 环境变量表）/ `ROUTER_PROMPT_FIX` / `design-p26-schema-layering`——删除前全仓引用扫描清零；**ADR 重编号** `0003-infrastructure-criticality` → `0006`（消除撞号，索引补行 + CHANGELOG 引用同步）；6 处路径 rot 修复（oncall 告警 6→8 补映射、secrets/release-and-rollback/bug_report/configuration 链接、`utils.py` schema 文档路径补 `design/` 段——修运行期恒空串 bug）；`web/README.md` 按域分列两套 SSE 词汇 + 真实 `VITE_*` 键名
- **[文档/新增] `docs/migration/evolution-assessment.md`**：依赖方向/分层偏离/双通道 SSE/零调用面（`search_service.generate_stream` 0 调用、`/v1/sessions` 前端 0 调用）/事件矩阵/框架候选「均不动」/遗留优先级
- 守卫复跑：p41+p35+code_hygiene+p36+p17+p40 = 114 passed

### Phase 22 执行：上下文二期——缓存纪律 + L4 auto-compact（2026-09-27）
- **[新增] 工具序稳定（KV Cache 纪律，Ch2 §2.4）**：`ToolRegistry.freeze()` 注册序快照（幂等、`delete` 不受限、`unfreeze` 供测试）——冻结后 `register` **告警拒绝**（Gotcha：运行期动态注册属违例，写测试锁定）；`schema_prefix_hash()` 测试辅助（canonical JSON over schema 序列 + sha256，同配置两次一致；`CONTEXT_AUTOCOMPACT` 开关不进哈希/前缀）；**生产自动触发放延后**（`build_graph`/`_startup` 锚点会跨测试冻结全局 registry），显式 API + 测试先行，落地注见规划 §3
- **[新增] 配置变更 append `system_notice`**（Ch2 §2.4）：`application/services/config_watch.py` — 非 secret 配置 hash（键名含 key/secret/token/password/credential/dsn 即排除）首调记基线 → 运行中变更向轨迹 `record(kind="notice", name="system_notice")`，**detail 只含事件/变更键名/哈希前缀、不含任何配置值**（Gotcha 3 无 PII）；接线 `_ask_impl` 与 `_stream_events_impl` 入口（自吞异常），**静态前缀字节不变**
- **[新增] L4 语义压缩**（Ch2 §2.7 分层压缩最后一档）：`domain/ports` 新 `CompactorPort`（零第三方 Protocol）+ `agentix/agent/context/compaction.py`（**五要素机制断言** `missing_elements`：trace_id/错误记录/约束 key/来源引用/任务状态字段 token 级保留，缺一弃用；`maybe_compact` 单次尝试 + 任何失败返回 None）；接入 `ContextEngine`（`trim_policy` 同层 `compactor` 参数）与 `SimpleContextBuilder`——L1–L3 后仍超预算才触发，**flag off 与 Phase 18 逐字节等价**，压缩失败/断言拒绝/仍超限 ⇒ 确定性丢弃循环回退；生产实现 `application/agents/compactor.py::LLMCompactor`（`llm_factory` 角色 `summarizer` 零温度）经 `agent_loop_adapter` 组合根 **flag-gated** 装配（默认 None 零行为变化；离线 record/eval 永不构造）
- **[测试]** 新增 `tests/test_agent_phase22_context_cache.py` **19 用例**（DoD 全项：schema 哈希两次一致 + flag 不进前缀/指令、冻结后注册被拒+告警+unfreeze 恢复、config 变更 `system_notice` 仅键名无值 + 前缀字节不变、L4 off 双 builder 逐字节等价且压缩器零调用、L4 on 五要素保留/坏压缩器拒绝回退/异常回退/仍超限终止、`missing_elements` 每类丢失检出 + 空真通过）
- **[变更] p17 守卫 `ports.__all__` 清单增 `CompactorPort`**（domain 端口集合唯一同步点）
- **[文档]** `Phase21-26_规划.md` §3 DoD 勾选 + §7 状态 + 落地注（freeze 生产触发延后决策）；`module-inventory.md` §6 Phase 22 勾选；基线同步 README/current-tree（128 模块）/Agent_Core（§9 加 Phase 22 行）/Phase15-20/规划头部
- 回归：全量 **2183 passed / 4 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select（源码）全绿；`mypy agentix/domain` 0 错（前端未触及）

### Phase 21 执行：Skills 渐进披露 + Hooks 生命周期（2026-09-27）
- **[新增] Skills 两段式加载**（书 §2.6/§4.7；flag `SKILLS_ENABLED` 默认关 + `SKILLS_DIR="skills"`，三 key 五处同步 settings/selfcheck/`.env.example`/configuration.md/p41）：`agentix/agent/skills/` — `loader` 扫 `skills/**/SKILL.md` 扁平 frontmatter（name/description/anti_when **三键必填**，缺项告警跳过、重名 first-wins、控制字符名拒绝）；`registry` 懒加载单例 + `read_skill`（**name 不拼路径**，只读扫描期存下的 path + `relative_to(root)` 纵深防穿越，正文 8000 字符截断、未知名字回友好回执+可用清单）；`build_instructions` flag-on 注入元数据目录（**正文永不进前缀**；flag-off 字节等价且不 import skills 包，`skills_enabled` 参数供测试覆盖）；`read_skill` 声明进 `registry_init`（flag-gated，进 `bind_tools`）+ handler 组合根 `install_read_skill_tool()`；示例 `skills/release-checklist/SKILL.md`
- **[新增] Hooks 5 事件**（flag `HOOKS_ENABLED` 默认关）：`agentix/agent/hooks/` — `events`（on_tool_before/after、on_step_end、on_run_end、on_error）+ `registry`（**仅同步 callable**、注册序触发、逐个 try/except 隔离、`emit` 首行 gate settings 永不抛）；`ToolExecutor.set_observation_hooks` 中性回调口（capabilities 零反向依赖，事件语义在 agent 侧绑定）——**闸门全放行后才 before**（Gotcha 3：与 HITL hook 正交有序，被拒/unknown 尝试不观察）；`loop.py` 四相位 `on_error`（context/plan/step/verify）+ `on_step_end`（观测入列后）+ `on_run_end`（`run→_run_inner` finally 恰好一次）；`main.py` 组合根 flag-gated 接线（同 Phase 16 HITL 模式）
- **[新增] `ToolRegistry.describe_groups()`**：按首个 capability 标签分组（无标签归 `general`），组序/组内均为注册序、两次输出稳定——供 planner/文档的元数据目录第一层
- **[变更] `ensure_registered()` 改逐项补缺**：原「首项已注册即整体 return」会漏掉 flag-on 后增量声明的 read_skill；现按 spec 逐个补注册（幂等语义更强）
- **[测试]** 新增 `tests/test_agent_phase21_skills_hooks.py` **17 用例**（DoD 全项：flag-off 指令区/工具表字节等价、目录无正文、缺 anti_when 跳过并告警、穿越拒绝（名字层 + 伪造注册项纵深）、hook 顺序+异常隔离+flag-off 零调用、闸后 before/after 配对+故障不改结果、loop 四相位/step/run 事件、describe_groups 稳定完整）
- **[文档]** `Phase21-26_规划.md` §3 DoD 勾选 + §7 状态 + 决策表/清单措辞对齐落地事实；`module-inventory.md` §6 新增 Phase 21–26 小节；基线同步 README/Agent_Core/current-tree/Phase15-20/规划头部
- 回归：全量 **2164 passed / 4 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select（源码）全绿；`mypy agentix/domain` 0 错；`vue-tsc` 0 错 + vitest 92 全绿（前端未触及）

### 下一版规划立项 + 项目定位表述统一（2026-09-27）
- **[文档/新增] `docs/migration/Phase21-26_规划.md`（v1.0）**：基于《Agent 全书笔记》分章版 11 文件对照立项——Phase 21 Skills+Hooks / 22 上下文二期（缓存纪律+L4）/ 23 交互二期（出站通知+Redis Stream）/ 24 记忆知识深化 / 25 multi-agent 运行时防线 / 26 评估三期+Phase 17 解阻清单；分批已拍板（批一 = 21+22），`Agent_Core_2.0_规划.md` §9 加指向行
- **[文档] 项目定位表述统一**：统一口径为「**以 Agent 内核 + RAG 等多路检索为核心的综合服务平台**（领域无关，法律仅为参考领域之一）」——修 README §1 概览句、`architecture/README.md` 一句话架构（原「可迁移垂直领域智能客服框架」陈旧）、`extension.md` 定位句、`prd-legal-domain.md` 产品目标（原误把 PRD 领域等同项目整体）、`Phase21-26_规划.md` §4；CHANGELOG 历史条目与 ADR 0001 决策上下文保持原样（历史记录不改写）

### 审查项收尾：逐端点鉴权 + 契约接线 + progress/命名收敛（2026-09-27）
- **[安全/新增] v1 逐端点鉴权**：新配置 `V1_REQUIRE_JWT`（默认关；settings / selfcheck / `.env.example` / configuration.md / p41 反向清单五处同步）→ `interfaces/http/v1/deps.py::require_v1_user` 接 **chat / sessions 全部路由** 与 **upload 三个写端点**（GET 文件/图片读取豁免——渲染面取不到 Authorization 头）；**events / ops** 接线既有 `verify_api_key`（`REQUIRE_API_KEY` 管辖，为未挂全局中间件的装配面兜底）；两开关正交；`tests/test_v1_endpoint_auth.py` 12 用例（轻量 app 无中间件直测端点级依赖：缺/坏/好 token、错/对 x-api-key、豁免面、正交性）
- **[修复/UX] ChatWidget 消费 `ChatResponse.status/error_code`**：新增 `chat/api/chatContract.ts`——`extractChatError` 覆盖三种错误体形状（FastAPI `detail` 字符串/对象、AppError `{error_code,message,user_hint}`、APIKey 中间件 `{error,message}`）+ 422 数组 + 网络错误；`decorateDegraded` 给非 ok 状态追加可见提示（含 ErrorCode 中文文案与 trace_id）——降级兜底不再被当正常答案、4xx/5xx 不再一律固定文案；vitest 14 用例
- **[接线] sessions/history 契约**：ChatWidget 持久化 `session_id`（localStorage）→ 刷新后 `GET /api/v1/chat/history/{id}` 恢复历史（服务端 404 则自动弃旧 id 重开）；「重置会话」接 `DELETE /api/v1/chat/session/{id}` 清服务端消息——先前前端对 v1 sessions/history 契约**零调用**，刷新即丢历史
- **[接线] SSE progress 事件**：retrieval 编排层三处**真实边界**补发 `type=progress`（`loading` 准备会话 → `retrieval` 检索预检 → `llm_generate` 出答，带 `stage/message/detail{step,total}`，router `type→event` 直通）——前端 progress 分支从死代码变实链；**不虚报** sparse/dense/rrf 阶段（后端无此划分，标签表补 `retrieval`）；`test_retrieval_progress_events.py` 2 用例（含拒答路径只发 loading）
- **[收敛] executor 命名**：`module-inventory.md` §2 新增 **Executor 对照表**（`MappingStepExecutor` / `ToolExecutor`+shim / `Executor` 别名 / `StepExecutorPort` / 已删 `AgentExecutor`，并钉死 `classify_exception` 归属）；修 `interview-10-questions.md` 把 `classify_exception` 张冠到 capabilities 侧的史实错误；4 个测试文件统一改走 `capabilities.tools.executor` 规范 import 路径（`infrastructure` shim 保留）；**删除死代码 `AgentExecutor`**（`services/breakpoint_resume.py`，137 行，全仓零调用）+ inventory/dependency-map 符号锚点同步（原同名三件套混淆源清除）
- 回归：全量 **2147 passed / 4 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select 全绿；`vue-tsc` 0 错 + vitest 92 全绿

### 接口审查第二轮：安全就绪 + 契约补齐（2026-09-27）
- **[安全] `GET /api/v1/sessions/` 的 `user_id` 改必填**：原可缺省 → 无鉴权下任何人可无条件枚举**全量用户**的会话时间线；现缺参 422（FastAPI 校验先于 DB，离线可测），带参行为不变
- **[修复] `DocumentData` 补 `updated_at`**：`Document` 模型有列但 DTO 漏带 → 前端 `updated_at` 恒 `undefined`；`_to_doc_data` 单点补齐 + schema 加可选字段，与前端 `documents.ts` 的 `updated_at?: string` 对齐
- **[新增] 前端 `x-api-key` 就绪**：`web` 新增 `API_KEY`/`apiKeyHeader()`（读 `VITE_API_KEY`，未配置时零行为变化），注入全部请求触点——knowledge `http` 拦截器、chat/knowledge 两路 SSE fetch、`ChatWidget` 上传 + chat POST、`useKbPersonalUpload` 跨域直连兜底两处（顺带回补缺失的 Bearer）；`Vite` 代理下 VectorDbView 走 `http` 实例自动携带。开 `REQUIRE_API_KEY=true` 前的前端前置就绪（键打进产物，仅作门禁钥匙，机密仍由 JWT 承载）
- **[决策] 死代码保留不删**：`agentix/agent/observer/`、`agentix/agent/policy/`（`InMemoryObserver`/`LoopBudget` 全仓零调用方）按 `module-inventory.md` §1.1/§1.3 记录的 Phase 2 规划骨架保留——文档引用其路径，删除会破坏 p41 路径守卫与规划史；待对应 Phase 启动时接线或正式移除
- **[测试]** `test_review_bugfixes.py` +2（sessions 缺参 422、`updated_at` 透传）= 9 用例；`test_p37` 500 折叠用例补 `user_id` 带参（适配契约变更，仍验证 DB 故障收敛 500 不泄栈）
- 回归：全量 **2133 passed / 4 skipped / 2 deselected**（2026-09-27 终值）；ruff 主检 + 严格 select 全绿；`vue-tsc` 0 错 + vitest 78 全绿

### 接口审查 + 前后端契约修复（2026-09-26）
- **[修复] `AgentLoop` 异常分支 import 错包**：`agent/core/loop.py` except 内从 `capabilities.tools.executor` import 不存在的 `classify_exception` → 自定义 executor 抛错时 except 内再抛 `ImportError` 逸出 `run()`（flag-on 崩溃）；改从 `agent.executor.executor` 导入
- **[修复] 轨迹镜像 `begin_step` 误用**（两处）：`capabilities/tools/executor.py` 与 `runtime/runtime.py` 的 `_mirror_trajectory` 丢弃 `begin_step` 返回值再 `steps.pop()` 弹出上一条已完成步 → 新步不入轨迹 + 历史步被覆写（空序列时 failure_stage KeyError 整步静默丢失）；改为 `step = begin_step(...)` → `end_step(step, ...)`
- **[修复] `EventPort` 协议对齐 webhook 调用面**：`domain/ports/agent_core.py` `publish` 补 `event_key/occurred_at/ttl_s/max_retries` kwargs、`wait` 补 `poll_s`（严格按协议实现的新后端不再在 webhook 路径 `TypeError`）
- **[修复] `POST /api/v1/chat` 无斜杠别名**：前端直发 `/api/v1/chat`（无斜杠）原先靠 307 跳转（Vite 代理/反代下偶发丢 body）；补直接命中路由，带斜杠路径保留
- **[修复] 事件/进化 runner 坏输入**：`aggregate_signals` 缺 `success`/`cost_usd` 非数的坏行跳过（原先 `TypeError`/`ValueError` 崩 runner）；空轨迹基线判 `not_enforced` fail-closed（原先空目录空真通过）；citation 基线值非数值 → `not_enforced`、当前值非数值 → `fail`；webhook `event_key` 加 256 字符上限（原先可绕过 payload 8192 上限撑爆事件日志）
- **[修复] 前端**：knowledge SSE 读流补 1MB 缓冲上限（与 chat 模块同口径，防异常服务端撑爆内存）；Vite 代理 `/healthz` → `/health`（后端只有 `/health`，旧配置恒 404）
- **[测试]** 新增 `tests/test_review_bugfixes.py` 7 用例（loop 异常分层、双镜像历史保全、协议契约、chat 双路径）；`test_agent_phase20_events_evolution.py` 补 6 用例
- **[文档]** 接口现状报告：v1 chat/sessions/ops/upload/events 无逐端点鉴权（仅全局 `REQUIRE_API_KEY` 默认关）；前端 `x-api-key` 全缺（开 `REQUIRE_API_KEY` 前须补）；sessions/history 契约未接线；TS 类型漂移项（`DocumentSummary.updated_at`、`progress` 事件、`ChatResponse.status/error_code` 未消费）——待产品决策
- 回归：全量 **2131 passed / 4 skipped / 2 deselected**（2026-09-26 终值）；ruff 主检 + 严格 select + `mypy agentix/domain` 全绿；`vue-tsc` 0 错 + vitest 78 全绿

### Phase 16/18/19/20 执行（2026-09-26）
- **[新增] Phase 16 HITL 三栈库级桥**：`application/services/hitl_bridge.py`（`HitlBridge` / `PermissionMode` 三模式 default·accept_edits·bypass / `BridgeSuspend`）；executor 拦截 hook **默认 None、flag 关不接线**（flag-off 字节等价）；`main.py` 组合根仅在 `HUMAN_REVIEW_ENABLED=true` 时 `install_executor_hook()`；fail-closed：拦下必挂 `ReviewerSidecar`、approver 否决落 REJECT、EDIT×approver 编辑合并 + digest/risk 重算、`to_persistence/from_persistence` 重启恢复仍落闸门；误拒可观测 `stats()`（拦截数/裁决分布/`rejection_rate`）；图内 `interrupt()` 另期
- **[新增] Phase 18 上下文分层**：L1 executor `_limit_result` 接 `result_limiter`（仅 `truncated`/`has_more` 改写并记 `meta.limited`，未触限字节不变，capabilities 缺失降级无界）；L2 `ContextEngine(trim_policy="tail"|"head")`（非法值 `ValueError`）；L3 `ManagerConfig.max_result_chars=4000` + `bound_result_face()` 结果面显式截断；状态栏 `STATUS_BAR_TRUSTED_KEYS` 白名单（保序）+ `rules`；指令分层 `agent/agent/context/instructions.py`（settings → 项目 `AGENTS.md`/`CLAUDE.md` 向上查找 → 会话，超长截断）→ `SimpleContextBuilder(rules=build_instructions())` 组合根接线；L4 Auto-compact / Skills / Hooks / 工具序+配置 append 缓存纪律 = **另期可选项**
- **[新增] Phase 19 multi_agent 接线**：supervisor 拓扑经惰性 `graph_getter` 切换（`AGENTIX_ABLATION_MULTI_AGENT` 消融开关），默认扁平零变化；`variants.json` wiring_status 翻转；结构审阅收敛 + meta 信息增量/失败模式 → `run_eval` 摘要聚合 → Benchmark 附录
- **[新增] Phase 20 事件 + 进化**：`EVENT_PORT_BACKEND`（settings/selfcheck/`.env.example` 三处同步，默认 `memory`）；`agent/events/jsonl.py` `JsonlEventBus`（append-only JSONL、重启恢复、跨重启 event_key 去重、损坏行容忍）；`agent/events/port.py` `build_event_port()/get_event_port()/wait_external_event()`（WAITING_EXTERNAL_EVENT→EXECUTING 自吞回切）；HTTP **`POST /api/v1/events`**（202 / 422 控制字符与空类型 / 413 payload>8192 / 同 event_key 去重回同一 id）；`evals/evolution_run.py` 离线 runner（信号聚合 → 提案 → 门 → staging → canary；verifier 受保护路径 propose 即拒；缺基线 `not_enforced` **fail-closed**；退出码 0/1/2）；`quality_gate.evaluate_metric` 抽纯函数与 CI 同真源；进化门对齐 `GATE_CHECKERS={golden_prefix, citation}`（未接入门 `not_wired` fail-closed）；Redis Stream 另期
- **[测试]** `test_agent_phase16_hitl_bridge.py` 27 + `test_agent_phase18_context_layering.py` 37 + `test_agent_phase20_events_evolution.py` 31 用例（Phase 19 见 `test_agent_phase19_multiagent.py`）
- **[文档]** 规划 §4–§11 勾选/执行状态 + module-inventory §6；Phase 17 全栈标 **blocked**（本机无 Docker/LM Studio，**拒绝伪造 baseline**）；configuration.md + p41 反向清单补 `EVENT_PORT_BACKEND`
- 回归：全量 **2118 passed / 4 skipped / 2 deselected**（2026-09-26）；p17/p22/p35/p41/code_hygiene 全绿；ruff 主检 + 严格 select 全过；`mypy agentix/domain` 0 错

### Phase 15–20 深化规划 + Phase 15/17 执行（2026-09-24）

- **[文档]** 新增 `docs/migration/Phase15-20_深化规划.md`：对照《Agent 全书笔记》+ Claude Code/Codex 公开架构，定义 Phase 15（点亮 Harness）–20（进化点亮）范围、依赖、DoD 与风险；已拍板：flag `AGENT_LOOP_ENABLED` 默认关、stream 仍走 L2（决策 A）、拒绝手工伪造 eval baseline
- **[新增] Phase 15 点亮 Harness**：`AGENT_LOOP_ENABLED`（settings/selfcheck/`.env.example` 三处同步）+ `application/services/agent_loop_adapter.py`（TaskSpec / 工具桥 / LoopResult→图 dict / observations→轨迹）；`ChatService._ask_impl` 在 flag 开时于同一 `_AGENT_SEM`/`AGENT_TIMEOUT`/`runtime.astep` 包裹内跑 `AgentLoop`；**stream 恒 L2**；flag-off 与原路径等价
- **[新增] Phase 17 在线信号（离线部分）**：`OnlineLoopRecorder` 单例 + `get_online_loop_recorder`；`chat_service` 成功 / AgentError / 内部错误三处 `record_request` 漏斗旁路零写 `record(...)`；轨迹包导出强制 `evals/baselines/traces/` 子目录（避开 `quality.json` 碰撞）；`evals/README` §9 / runtime-guardrails §12.5 / testing.md §4 写明武装顺序（全栈 record → 本地播种并 **手动 commit** quality.json——CI 建档不持久）
- **[测试]** `tests/test_agent_phase15_wiring.py` 18 用例（flag-off 等价、flag-on smoke、stream 仍 L2、三漏斗在线信号、轨迹桥、零写签名）
- **[变更] 文档数字**：README §11 / current-tree / 规划 §9 基线对齐 **2000 passed / 4 skipped**（2026-09-24）；module-inventory §6 勾 Phase 15 + 17 离线
- 回归：全量 **2000 passed / 4 skipped**；p17/p22/p35/p41/code_hygiene 全绿；ruff + mypy 全过

### Agent Core 审查纠错 + README 深化（2026-09-24）

- **[修复] `FailureMode.Livelock` → `LIVELOCK`**：枚举成员命名规范 + 去掉事后 `_value_` hack；`stop_on_contradiction=False` 时 `max_rounds` 终态不再丢失 `contradiction` 标记（`reason=contradiction_unresolved`）
- **[修复] Event 总线持久化**：`InMemoryEventBus.to_persistence` 的 dedup 表由 tuple 键改为可 JSON 序列化字符串键，补 `from_persistence` 往返（历史 `json.dumps` 直接 TypeError）
- **[修复] HITL Sidecar**：`EDIT` 裁决在注入 approver 时同样先问 approver（fail-closed，防「改参放行」绕过审批）
- **[修复] Replanner**：自定义 planner 返回与已观测同 id 的 step 时重映射，避免被 `_next_pending` 跳过导致空转
- **[修复] Event 虚拟身份往返**：`InMemoryEventBus.from_persistence` 此前忽略 blob 内 `identity`，每次恢复都铸新身份（`identity_id`/scopes/`credential_ref` 静默丢失）；现从持久化还原，显式 `identity=` 参数仍优先
- **[修复] 清理**：`agent/` 与若干遗留 F401/F841 死代码；`domain/evals` 补 `aggregate_run_costs`/`SIGNIFICANCE_Z_*` 进 `__all__`；`research-agent-demo.md` 修正 `SimplePlanner` 错误引用
- **[变更] README**：新增 §4 Agent Core 2.0（Harness/HITL/事件/多智能体/演化/端口）；测试真源 **1982 passed**（2026-09-24，Python 3.10.20）；HITL 口径对齐 L1+（MEDIUM+）；目录重编号 §1–§12；文档表补 agent-core-layers / migration / demo
- **[文档]** 迁移清单与 runbook 中「README §10」引用改为 §11；`current-tree`/`module-inventory` 基线数字对齐 1982；规划 §9 执行状态收口为 Phase 0–14 完成；Phase 10 消融口径按 `variants.json` 记为 A–G（原文 A–H 的 H 无定义）
- **[清理]** `--select F401,F811,F841` 全仓清零（evals/kb_ingest/scripts 死 import 与未用赋值）
- 回归：Event 持久化 JSON + identity 往返、EDIT×approver、contradiction 终态、自定义 planner step 重映射；全量 **1982 passed / 4 skipped**

### 求职收益优化批次（Agent Runtime/MCP/评测/Trace UI/README，2026-09-22）

- **[#P1] 任务级 Agent Run API**（`16252c5`）：Control Plane HTTP 化——`POST/GET /api/v1/retrieval/agents/runs`、`GET .../runs/{id}`、`POST .../runs/{id}/cancel`；执行器注册表（内置 probe 回显执行器）；超时统一 `asyncio.wait_for`、取消幂等、步数预算挂 TokenBudget；错误码 `AGENT_RUN_NOT_FOUND`/`UNKNOWN_AGENT`；9 用例
- **[#MCP] MCP 一等公民**（`871f393`）：零依赖 stdio server（MCP 2024-11-05 子集：initialize/ping/tools/list/tools/call）+ 客户端（握手/调用/超时保护）+ ToolExecutor 桥接适配（远端工具 L0 只读建模入统一执行器，`mcp_` 前缀防撞名）；E2E 子进程测试；18 用例
- **[#评测] Ablation 脚手架**（`469d419`）：`evals/ablations/`——A-G 七变体定义（裸 LLM → Full Agentix，开关单调叠加）+ 在线编排器（`AGENTIX_ABLATION_*` 环境注入 run_eval --live）+ Benchmark 表聚合（离线可跑，缺数据变体占位）；3 用例
- **[#P0] Trace UI**（`0a9a3f1`）：前端执行轨迹时间线——流式事件折叠（工具/阶段/生成/完成/异常，相邻去抖），消息内「执行轨迹」折叠面板实时呈现；纯函数 + vitest 5 用例；vitest 全量 78 / vue-tsc 0 错
- **[#文档] README 减肥**（`43d29d0`）：762 → 253 行，按 What/Why/Runtime/Knowledge/Reliability/Evaluation/Observability/Quick Start/Engineering 重构首页；架构细节迁 `docs/architecture/ARCHITECTURE_DETAIL.md`、数据准备迁 `docs/DATA_PREPARATION.md`；定位调整为「Agent Platform · 法律领域参考实现」
- **仓库文件夹重命名**：`Legelbot-develop` → `My-Agentix`（本地路径与品牌一致；源码零硬编码路径依赖，仅外部辅助脚本路径更新）
- 测试基线：**1418 passed / 7 skipped / 0 failed**（+30）+ vitest 78 / vue-tsc 0 错

### My Agentix 迁移批次（P0-P2，2026-09-21/22）

- **[#P0] 重命名迁移**（`b0b7e08`）：`legelbot` → `agentix` 包名全量迁移（344 个 .py + pyproject/Makefile/compose/Dockerfile/CI/kb_ingest/evals/web），品牌文案升级「My Agentix」；`STATIC_PREFIX_VERSION/PROMPT_VERSION` 经 content_version 动态滚动。保留：前端 localStorage 键 `legelbot:access_token`（避免强制全部用户重登）与存量本地库名。644 文件，独立 commit 可 revert
- **[#P1] 可部署收口**（`bcebc6e`）：Dockerfile 多阶段重构（builder 编译工具链与运行时分离）；compose healthcheck 补齐 13/13（retrieval-worker=celery ping / etcd=etcdctl / kb_ingest=/health）；`LIGHTRAG_INIT_LIMIT` 缺省修复；`.env.development/staging/production.example` 三模板；发布与回滚 Runbook（`docs/runbooks/release-and-rollback.md`，五层验收链 + 四级回滚）
- **[#P1] 可测试+可评估收口**（`0d71a3f`）：CI 新增 `eval-offline` 作业（离线评估 + 四指标质量门禁，预测基线缺失优雅跳过）；`docs/testing.md` 五层测试体系；`evals/baselines/quality.json` 建档
- **[#P2] 可观测+可解释收口**（`b8c95b8`）：业务指标 `grounding_low_score_total` / `gateway_fallback_total`（grounding 低分与 Gateway fallback 接线）；**Explanation API v1**（`GET /api/v1/retrieval/answers/{id}/explanation`，聚合问题/证据/引用/模型，未持久化块显式 not_recorded，零 schema 变更）；告警 +2（grounding 低分突增 / fallback 连锁）；SLO 目录（`docs/runbooks/slo.md`）
- **[#遗留] AppError 全局转换器**（`1ab5d3c`）：修复检索域业务错误一律 500 的平台缺口——`AppError` 现按 `ERROR_CODE_TO_STATUS` 映射语义化状态码（401/404/409/429…），响应体与 `response.error()` 标准结构一致；非 AppError 异常仍收敛 500（契约不变）
- **[#遗留] X3 过程折叠面板**（`1813a1c`）：前端 assistant 消息下「过程」面板——懒加载 Explanation API 展示检索证据（相关度 + 效力徽标）/模型/未记录块占位；`not_recorded` 不编造
- **文档与整理**：README 全面刷新（Milvus 统一口径、测试分层、项目结构、GitHub 链接修正）；品牌资产更名（agentix-logo/icon.png）；清空遗留 `backend/` 空壳目录
- 测试基线：**1388 passed / 7 skipped / 0 failed**（后端）+ vitest 73 / vue-tsc 0 错（前端）

### Round 3 续：Semantic Memory 卡片层（P2，2026-09-21）

- **[#R3-8] Semantic Memory**：`application/services/semantic_memory.py`——§29-33 四层收口：Working→AgentState（R3-1/R3-3）、Episodic→MemorySummary（既有）、Semantic→**本模块**、Procedural→留待（无真实场景，决策记录）。卡片管线全链（§32）：规则式 Extractor（零 LLM，从**会话摘要**提取偏好/指示卡——遵守"不把每句对话存进去"纪律）→ Dedup（同内容 confidence 取 max）→ Conflict（同类型互为包含 → version+1 覆盖）→ Redis hash Store。接线 `LongTermMemory`：`store_summary` 时提取（生产者）、`get_context_for_query` 注入"用户稳定偏好"区段（消费者）；`MEMORY_CARDS_ENABLED` 默认开但挂 `MEMORY_CONTEXT_ENABLED`（默认关）之下——整体默认零行为变化。16 用例

### Round 3 续：Model Gateway（P2，2026-09-21）

- **[#R3-7] Model Gateway**：`infrastructure/llm/model_gateway.py`——补齐 §42-44 三缺口（Strangler Pattern 叠加在 llm_factory 之上，不推翻）：① **Model Registry/Router**（§43）：任务分档 fast/standard/reasoning → `resolve_model`（`LLM_MODEL_FAST/STANDARD/REASONING` 显式配置优先，空 = 回退主模型——单模型部署零行为变化）；② **Fallback 执行器**（§44）：`ainvoke_with_fallback` 按 primary → 链逐模型尝试，`LLM_FALLBACK_MAX_ATTEMPTS` 硬上限防成本爆炸，GatewayResult 携带 model_used/attempts/errors 归因；**分层纪律：Gateway 不构建 client 不发起调用**——`build_client`/`invoke` 由消费点注入，保证 fallback 客户端与主链路同构（同 base_url/extra_body/计量/熔断）。接线 `query_knowledge_base`（`LLM_FALLBACK_MODELS` 空 = 默认零行为变化；非空时 primary 失败 → 链内兜底，熔断/重试语义不变）。vision/embedding 不重复建档（已有 RERANKER_MODEL/向量库 embedding 独立配置）。14 用例

### Round 2 收口：效力过滤覆盖 Neo4j 路径（2026-09-21）

- **[#R2-2b] KG 记录级效力后过滤**：`legal_effectiveness.filter_records_in_force`——`List[Dict]` 记录变体（与 `filter_in_force` 同语义：fail-open、日期别名、失效当天仍有效）；**无效力字段的记录原样保留且不注入字段**（存量图数据/统计聚合查询字节级零变化）；有效记录回写 `effective_status` 供下游展示。接线 `cypher_tools` 节点 records 产出点（text2cypher 执行结果进摘要前过滤），过滤器异常不阻断 KG 主链路；开关复用 `EFFECTIVENESS_FILTER_ENABLED`。**决策**：LightRAG/GraphRAG 返回纯文本无结构化效力字段，记录级后过滤不可判，维持现状不接线。10 用例

### Round 3 续：Evidence Layer 补 GroundingValidator（2026-09-21）

- **[#R3-6] GroundingValidator v1**：`retrieval/domain/services/grounding_verify.py`——句子级证据锚定校验（架构文档 §104 四件套的最后一个缺口；与 CitationValidator 互补：后者管"引用对不对"，前者管"每句话有没有证据支撑"）。纯函数零 LLM 调用：中文句子切分（短句/纯数字过滤）→ 字符 bigram 包含度比对 → grounding score + 未锚定句预览；无证据 fail-open 不判定（与 citation_verify 同哲学）；`GroundingValidator` 类封装为语义级（NLI）演进留接缝。**观测模式**：只产出报告（`AgentToolContext.last_grounding`）+ 低分 WARN，不改答案、不拦截交付；`GROUNDING_VALIDATOR_ENABLED/SUPPORT_THRESHOLD/LOW_SCORE_WARN` 三配置。15 用例

### Round 3 续：Context Engine（2026-09-21）

- **[#R3-5] Context Engine**：`retrieval/domain/services/context_engine.py` 四能力——① 静态前缀版本化（`STATIC_PREFIX` + `content_version` 求版号 `ctx-*`，不变性测试锁定"同版本号 ⇒ 同前缀"）；② 法律 authority 排序（§26 五档：法律正式文本>司法解释>法院案例>二级资料>普通网页；读 `source_authority_level` 及入库别名，缺失档位 0 稳定排序——存量数据顺序零变化）；③ Context Budget（§27 token 级贪心装配，超预算低档截断/丢弃，`CONTEXT_BUDGET_TOKENS=0` 默认不限额）；④ 大文档隔离通道（指南 L1⑦，复用 artifact_store，超阈值全文外置、Prompt 留预览+句柄，`CONTEXT_ISOLATE_THRESHOLD=0` 默认关闭）。`assemble_context` 统一装配管线（Ranking→Isolation→Budget→拼接）接入 `query_knowledge_base`；`build_prompt` 增 `status_fields` 状态栏注入（【对话状态】单行，默认不传零变化）；`AgentToolContext.last_context_meta` 携带装配统计。18 用例
- **[修复] 工具注册表测试顺序污染**：`ToolExecutor.register_handler` 会把 spec 自动登记进全局 registry，R3 测试的 L2 夹具泄漏会打红 `test_r1` 的"全部 L0"断言（仅自定义顺序触发）。`ToolRegistry.delete_tool` 新增 + `test_r3_agent_runtime`/`test_r3_tool_selection` 夹具快照恢复
- **[事故] git 对象库损毁与重建**：2026-09-21 发现 `.git` 对象全部丢失（refs 目录、.pack、loose objects 均被外部删除，成因未明）。工作区源码经逐项验证完整无损；从 GitHub 只读 fetch 回上游基线，本地 78 个提交以单一恢复提交 `d581981` 重建（提交消息归档于 `D:\workbuddy工作目录\2026-09-15\My Agentix_recovery_backup\git_reflog_HEAD.txt`，工作区完整备份 `worktree_2045.tar.gz`）。下文历史 commit 哈希仅存于本日志文字记录

### Round 2 + 3：《技术变更指南 v2.0》落地 + Agent Runtime 架构（2026-09-21）

> 依据《技术变更与架构演进指南 v2.0》（L1-L3 P0 项）与《生产级 Agent 系统架构与实现设计》
> （Strangler Pattern，LangGraph 降级为 Execution Backend）。共 5 个独立 commit，每项配测试。

- **[#R2-1] Python 3.10→3.12 升级**（`c37de42`）：pyproject `requires-python >=3.12,<3.14`、Dockerfile×2 `python:3.12-slim`、CI/README 同步；钉版校准 langgraph-sdk `>=0.1.42,<0.2.0`、pyahocorasick `2.1.0`（cp312 wheel）、pydantic `2.13.5`（对齐实测基线）；特征化测试按解释器版本分支（3.12 contextvar 隐式传播 config / async interrupt 限制解除）。`.venv312` 全量 **1296 passed / 0 failed**
- **[#R2-2] 法条效力过滤**（`99609e6`）：`domain/policies/legal_effectiveness.py` 四字段契约（effective_status/effective_date/expire_date/replaced_by），存量默认 in_force + fail-open；检索后过滤（三后端一致）+ fetch_k 有界补偿 + citations/hit_chunks 带 status + 答案"检索截至"标注 + kb_ingest 入库透传与显式标注入口；双开关配置。25 用例
- **[#R2-3] golden 扩充 + 引用正确性评分**（`5df20d4`）：golden 22→**102 条**（7 路由全覆盖、6 条效力感知样例）；`score_citation_correctness`（答案文本真实引用 + sources 可归因，中文条号归一）。60 用例
- **[#R3-1/R3-2] Agent Runtime 骨架 + 统一 ToolExecutor**（`2f66d24`）：`application/runtime/`（AgentState 九态上下文 / ExecutionStep 类型化轨迹 / 六维 Budget / 生命周期状态机 / AgentRuntime 包装既有图执行）+ `infrastructure/tools/executor.py`（权限 fail-closed→风险闸门→超时→指数退避重试→ToolResult 统一协议）+ ToolSpec 执行治理字段。34 用例
- **[#R3-3] Runtime 接入编排主链路**（`702e7c1`）：ChatService.ask 每请求挂 AgentRuntime（观测模式 Budget 全 0），整图执行成 EXECUTING 步骤，`metadata["runtime"]` 携带 AgentState 快照（含失败轨迹）；错误链/超时行为零变化。5 用例 + 108 回归零变化
- **[#R3-4] 工具选择过滤链**（`acacb35`）：`infrastructure/tools/selection.py`（max_risk_level/白黑名单/capabilities 三层过滤，LLM 不可见无权限工具，§17）+ `tool_selection` 节点经 `configurable["tool_policy"]` 逐请求接策略（静态构图不动）。11 用例

### 企业级 P0 专项：可观测性 + 限流（2026-09-21）

> 对应《My Agentix 企业级优化评估》P0 路线图，四项独立 commit，每项配测试。

- **[#P0-1] `/metrics` 端点暴露**（`46200c1`）：main.py 挂 Prometheus 指标端点（`get_metrics()` 助手），纳入 APIKey 中间件豁免清单；`tests/test_observability_metrics.py` 3 用例
- **[#P0-2] 监控栈**（`5bbd2cd`）：`docker/monitoring/` 新增 prometheus（15s 抓取 backend:8000/metrics）+ alertmanager（BackendDown/5xx>1%/P95>10s/限流风暴 4 条规则）+ grafana（provisioning 自动注入数据源）；compose 三服务含 healthcheck 与持久化卷
- **[#P0-3] 全局限流中间件**（`64ef95d`）：`GlobalRateLimitMiddleware` 纯 ASGI 中间件挂最外层，令牌桶复用 `reliability/rate_limiter.py`，按客户端 IP（X-Forwarded-For 首跳优先）限流；429 + Retry-After + `rate_limit_triggered_total` 指标；`RATE_LIMIT_ENABLED/GLOBAL_RPM/GLOBAL_BURST` 配置项 + 超限追踪键防内存放大（上限 10000）；6 用例
- **[#P0-4] OTel 转硬依赖**（`1414868`）：requirements.txt 钉版 `opentelemetry-sdk/exporter-otlp-proto-grpc==1.44.0`；main.py startup/shutdown 接线 `init_tracing`/`shutdown_tracing`；测试断言硬依赖 + 真实 tracer 冒烟，防"生产静默禁用"
- 配套治理：`.env.example` 补限流/监控/OTEL 变量；selfcheck 合法省略清单同步

> 架构优化批次 11 + 12 + 13：
> **批次 11–12**：清单缺口收口 + 开源工程化 + 前沿 Agent 框架标准；
> 架构健康度清单（40 条）严格 ✅ 由 **21/40 → 40/40**，⚠️ 与 ❌ **双双清零**（S6 目标 ≥32 达成并超出）。
> **批次 13**：剩余批次（P2-4 / P2-5 / P2-6 T7）收口 + 代码结构整理与去重 + 实现细节优化。
> **融合前优化批次**（2026-09-20）：提交此前未入库的生产化优化工作，为 Tracerag 融入建立干净基线（tag `pre-tracerag-merge`）。

### 融合前优化批次（2026-09-20）

- **工具调用预算硬上限**：`BudgetLedger` 新增 `max_tool_calls`/`record_tool_call`，`chat_service` 工具调用处计入预算，超限返回 `TOKEN_BUDGET_EXCEEDED`；`TOOL_CALLS_PER_REQUEST`（默认 0=不限额）
- **法律语料元数据规范化**：`legal_metadata` helper（`effective_from/to`、`authority_level`），kb_ingest 接入法律元数据透传
- **请求执行图 + 基础设施关键性 ADR**：`docs/architecture/request-execution-graph.md`、`adr/0006-infrastructure-criticality`
- **运营手册与运维脚本**：`OPERATIONS*.md`、`backup/restore_databases.ps1`、`load_test.py`、`chaos_test.py`
- **融合基线**：tag `pre-tracerag-merge`，`.gitignore` 排除 `archive/` 与迁移中间产物

### Phase 1：Tracerag 融合 — 检索能力域迁移（R1.1–R1.14）

- **R1.1** 迁移 Tracerag domain/usecases/infra → `agentix/retrieval/`（89 文件，10382 行）
- **R1.2–R1.4** 上提 observability/security/reliability 至 `agentix/` 顶层共享（14 文件）
- **R1.5** 退役 `tracerag` 包名，重写 80 文件 352 行 import 路径 → `agentix.retrieval.*`
- **R1.6** 定义 `RetrievalPort` Protocol（`search`/`delete_by_doc`/`index_status`）+ TypedDict 结果类型
- **R1.7** 实现 `RetrievalPortAdapter` 本地适配器（编排检索链路，满足 Protocol）
- **R1.8** 归档旧 RAG 至 `archive/agentix-knowledge/`，保留 `KnowledgeService` 兼容 shim
- **R1.9** `kb_tools/node.py` 改写为经 `RetrievalPort` 检索（编排侧无 `retrieval.infra` 直接 import）
- **R1.10** 统一挂载 12 个 retrieval API 路由（`/api/v1/retrieval`），退役 `knowledge_router`，新增 `/liveness`+`/readiness`
- **R1.11** 配置收敛：`retrieval/settings.py` 委托 `agentix.config.settings` 统一入口
- **R1.12** 依赖收敛：新增 sqlmodel/celery/boto3/faiss-cpu 等 10 个检索依赖
- **R1.13** CI 新增 `retrieval-gate` 质量门禁 + 8 个 RetrievalPort 单元测试
- **R1.14** 双跑验证：12 关键导入路径验证通过，8 测试通过，git status 干净

### Phase 2：认证与租户统一（R2.1–R2.7）

- **R2.1** 新增 `Tenant` 领域模型（双侧 ORM：retrieval SQLModel + 编排 SQLAlchemy），`tenant_id` 全局唯一不可变、`name ≤128`；Tenant CRUD usecase + API 路由（`/api/v1/retrieval/tenants`）；迁移自动播种默认租户；15 单元测试全部通过
- **R2.2** `User` 扩展 `tenant_id`/`role`/`status`，`KnowledgeBase` 扩展 `tenant_id`；`owner_user_id` 保留为创建者但不再作隔离依据；迁移回填存量 User/KB 至默认租户；`list_kbs` 按 `tenant_id` 过滤；新注册用户自动归属默认租户；禁用用户不可登录；10 单元测试全部通过
- **R2.3** 统一 JWT 认证：新增 `agentix/security/jwt.py`（`create_access_token`/`decode_access_token`/`UserContext`，HS256）；`deps.py::get_current_user` 改为解析 `Authorization: Bearer <token>`；`auth` 路由返回 `access_token`；Cookie Session 保留为 `AUTH_COOKIE_SESSION_FALLBACK` 回滚开关；新增 `PyJWT` 依赖；13 单元测试全部通过
- **R2.4** 逐层归属校验升级：`authz.py` 新增 `require_kb_in_tenant`/`require_doc_in_tenant`/`require_session_in_tenant`/`require_index_job_in_tenant`，404（不存在）与 403（跨租户）分离；旧 `require_kb_owned`/`require_document_readable`/`require_index_job_readable` 保留为兼容包装委托到新函数；8 单元测试全部通过
- **R2.5** 租户内 RBAC：新增 `agentix/retrieval/api/rbac.py`（`require_admin`/`require_admin_or_member`/`can_delete`/`can_upload`/`can_manage_tenant`）；tenant 写操作要求 admin（`get_current_admin` 依赖）；KB 删除要求 admin；17 单元测试全部通过
- **R2.6** 高权限操作审计日志：新增 `AuditLog` 模型 + `audit_service.write_audit`，在 tenant/KB 增删改事务内写审计记录；敏感字段（password/token/api_key）自动过滤；结构化日志 `extra` 携带操作者/对象/结果；12 单元测试全部通过
- **R2.7** 存量 `tenant_id` 回填脚本（`scripts/migration/backfill_tenant_id.py`，幂等可重跑，支持 `--dry-run`）+ 阶段二端到端验证（回填幂等性、无匿名访客 401、跨租户 403/404、禁用用户不可登录、JWT/RBAC/审计/authz 工具齐备）；14 验证测试全部通过；**Phase 2 共 97 测试全部通过**

### Phase 3：存储收敛 + 向量回填 + 移除 pgvector（R3.1–R3.9）

- **R3.1** 统一 PostgreSQL：共享 engine + schema 隔离（编排域 `public` / 检索域 `retrieval`，SQLite 下 no-op）；`migrate.py` schema 感知；startup 补检索域建表；11 测试
- **R3.2** Redis-Stack 单实例 DB 分片：db0 缓存 / db1 Celery / db2 checkpoint；Celery 保持「显式配置才启用」契约（空默认=同步回退）；4 测试
- **R3.3** MinIO 统一：bucket `documents`（业务）/ `milvus`（Milvus 内部）；`ensure_bucket()` 幂等 + 进程内防重入；凭据与 compose 对齐；6 测试
- **R3.4** Milvus 统一：服务端 v2.3.3→v2.4.17、pymilvus 2.4.x；集合 `retrieval_kb`、`tenant_id` partition_key、`MILVUS_METRIC_TYPE=IP`（归一化 ≡ cosine）；修复 `filter` 未下推 Milvus search 的潜伏缺陷；milvus 后端强制归一化校验；8 测试
- **R3.5** 元数据迁移脚本 `scripts/migration/migrate_metadata.py`（sorted_tables 拓扑序、PK 幂等跳过、tenant_id 填充、datetime 归一化、JSON 报告、非零差异退出）；e2e 验证 dry-run→迁移→重跑零差异
- **R3.6** 蓝绿向量回填 `scripts/migration/backfill_vectors.py`（FAISS→影子集合→计数校验→rename 切换+失败回滚，RetryPolicy 批次重试）；7 测试；`e1b2dd6` 修复版本目录按目录 mtime 排序不稳定的缺陷（改按 index.faiss mtime）
- **R3.7** kb_ingest 后端 pgvector→Milvus `legal` 集合：确定性主键 `sha1(source_table|source_id)[:32]`、L2 归一化+IP、JSON 字段 expr 下推、company 超量召回 k*3+子串过滤；`VectorStoreWriter` 兼容 shim；移除 psycopg2/pgvector 依赖、`docker/pgvector`、`kb_postgres` 服务与 entrypoint PG 等待逻辑；修复 `DatabaseConfig` slots dataclass 类属性默认值坑；15 测试（`7ab5af6`）
- **R3.8** docker-compose 收敛至 6 类存储：新增统一 postgres:16 与 retrieval-worker（Celery，独立 replicas/CPU/内存）；6 类存储 readiness healthcheck 全覆盖；`0fb9139` 测试基建修正（DATABASE_URL 兜底上移 conftest、p35 契约清单随 R3.7 更新）
- **R3.9** 阶段三验收记录 `docs/storage_convergence_r3.md`：部署清单/探针覆盖/能力不退化/pgvector 零残留核查/回滚点

### Phase 4：前端合并（R4.1–R4.6）

- **R4.1** 归并双前端为单一仓库双模块：Tracerag frontend/src（99 文件）→ `web/src/modules/knowledge/**`；原 web/src → `web/src/modules/legal/**`（git mv 保留历史）
- **R4.2** 统一入口 main.ts + 根 App（auth gate + 共享导航双 tab）+ 根 router（`/`=法律智能助手，`/knowledge/**`=知识库问答）；knowledge 模块路由表前缀化、不再自建 router 实例
- **R4.3** 统一 JWT 会话：`auth/token.ts` 单一 token 存取（免循环 import）、axios 拦截器注入 Bearer、legal 模块 SSE/上传通道接入同一 token；单登录态贯通两模块
- **R4.4** 收敛单一 package 与构建链（Vue3+Vite8）：`build = vue-tsc --noEmit && vite build`；vue-tsc 0 错误、vitest 64 用例全过、build 成功；BatchPayload 类型显式化
- **R4.5** 旧入口 URL（/kb /history /vector-db）重定向收敛，旧书签无 404
- **R4.6** 端到端验收 `scripts/e2e_phase4.mjs` 6/6 通过（登录 gate/统一 shell/token 持久化/无重复登录切换/旧 URL 重定向/切回保持）；验证记录 `docs/phase4_frontend_merge.md`
- **连带修复**：TraceIdMiddleware 补设 `request.state.request_id`（此前所有 `/api/v1/retrieval/**` 请求 500 的融合缺口）；knowledge 前端 API 路径适配统一 `/api/v1/retrieval` 前缀（13 文件 43 处）

### Phase 5：品牌与对外切换（R5.1–R5.4）

- **R5.1** APP_NAME/版本单一事实来源 = My Agentix：登录页标题、knowledge 欢迎区、`web/index.html` 标题、3 处 LLM prompt 品牌自称统一为 My Agentix；`web/package.json` 版本 1.0.0→0.1.2（恢复 `agentix.__version__` 单一来源，`bump_version.py --check` 通过）（`ac7d41e`）
- **R5.2** 下线临时双跑入口：删除 `infrastructure/knowledge` DEPRECATED 兼容 shim 与未挂载的 `knowledge_router`；新增非弃用 `KnowledgeGateway`（委托 RetrievalPortAdapter，编排层唯一检索入口，6 个调用方改接）；移除 `batch_embedding` 死处理器与 TXT/MD/JSON 死分支；删除过期脚本与桩测试；前端移除零引用 `@deprecated overallUploadPercent`；全量 1146 passed / 7 skipped / 0 failed（`30eaae7`）
- **R5.3** 文档更新：新增 `docs/phase5_brand_switch.md` 验证记录；CHANGELOG Phase 5 小节；复核 README/OPERATIONS/TECH_STACK_GUIDE 品牌口径（Tracerag 仅存于历史记录）
- **R5.4** 最终回归验证：见 `docs/phase5_brand_switch.md` §5
- **收官后补强（2026-09-21）**：修复 venv lint 工具链（ruff/black 安装损坏）并清零 `ruff check` 全部 11 项——含两处真实缺陷修复：`vector_db.py` 文档分页接口 `result[start:end]` NameError 残留、`embedding/client.py` 缺失 `settings` 导入；black 全仓格式化（291 文件）标记为可选整改未执行

### 新增 (Added)

- **尝试并撤回：运行中人工审批中断**（批次 13，`LangGraph interrupt`）。实测结论：
  - **async 节点不可用**：`interrupt()` 抛 `Called get_configurable outside of a runnable context`
  - **sync 节点可用**（最小复现：暂停 → `Command(resume=…)` 恢复并取回裁决值）
  - 本仓库工具节点均为 async ⇒ 包装式接线不成立；可行做法需改图拓扑（影响主路由），故撤回。
  - ⚠️ **勘误**：最初归因于 `langgraph 0.2.60` 与 `langgraph-checkpoint 2.0.26`「版本不匹配」是**错的** ——
    langgraph 0.2.60 自身即声明 `langgraph-checkpoint>=2.0.4,<3.0.0`，该 pin 正确。已在代码注释与文档同步修正。
- **框架限制用测试固化（批次 13）**：`tests/test_p32_tool_guards.py::TestLangGraphInterruptCharacterization`
  把「`interrupt()` sync 可用 / async 不可用」写成**刻画测试**而非只写在文档里 —— 文档会腐，
  测试会在 LangGraph 升级后**主动提示边界已变**，避免后人继续回避一个其实已经可行的方案
- **人工裁决归一化（批次 13）**：`human_review.normalize_decision()` —— 把人工裁决归一为
  `APPROVED` / `REJECTED`，**fail-closed**：看不懂的裁决（`None` / 空串 / 意外结构）一律 REJECTED。
  支持字符串（中英文）、布尔值、dict 三种写法；15 例测试
- **文档一致性守卫（批次 13）**：新增 `tests/test_p41_doc_consistency.py`（25 例）——
  ① 配置文档表格里的键必须在 `Settings` 存在（实测 25 个真实键全部存在）
  ② 文档引用的仓库内路径必须真实存在
  ③ **分组标题声明的达标数必须与表格实测标记一致**（批次 13 真实踩到：40 行表格全改成 ✅、汇总表也改了，却漏改六个分组标题，仍停在 21/40 —— 文档内部自相矛盾。已修正并经对抗性验证：把 A 组标题改回 3/6 → 守卫如期失败）。
  **实测发现并修正的文档腐烂**：
  `agentix/services/lightrag_service.py`→`application/services/…`、
  `agentix/api/lightrag_router.py`→`interfaces/http/…`、
  `kb_ingest/core/config.py`→`kb_ingest/kb_service/core/config.py`、
  `tests/test_agent_routing_questions.md`（从未存在）→`tests/test_agent_routing.py`
- **覆盖率门禁达标（批次 13，S6 验收标准 ②）**：53% → **61%**（`fail_under = 60` 现已启用）
  - 新增 `tests/test_p38_search_tool.py`（14 例）：`SearchTool` 用 mock httpx 覆盖成功/失败路径与**下游结果限量**
  - 新增 `tests/test_p39_redis_cache.py`（26 例）：`redis_cache.py` **0% → ~90%**，含「Embedding 故障不打断聊天主链路」、容量淘汰、损坏条目跳过
  - 新增 `tests/fakes/redis.py`：内存版 Redis 替身，只实现本项目实际用到的 9 个命令，**零新增依赖**（不引入 fakeredis）
  - 新增 `tests/test_p40_import_health.py`：遍历导入 `agentix.*` 全部模块，自动捕获断链 import
- **P2-2 核心链路单测收口（批次 13）**
  - `test_chat_api.py` 的 2 个用例加 `integration` + `asyncio` 标记（此前**被收集后协程从未 await，静默不执行**）
  - `test_router_smoke.py` 移入 `scripts/router_smoke.py`（无可测函数却躺在 `tests/` 被导入）
  - 新增 `tests/test_p37_http_endpoints.py`（12 例）：TestClient **离线** HTTP 层端到端——健康检查 / 根路由 / 路由清单 / trace_id 中间件 / 404 / 外部服务失败收敛为 500 / **上传路径遍历防护**
- **token 预算真正限额（批次 13，此前只计量）**
  - 新增 `ErrorCode.TOKEN_BUDGET_EXCEEDED`，归入 `DEGRADABLE`（预算耗尽是**控制决策**而非服务故障 ⇒ 礼貌拒答，不进 `HTTP_FATAL` 抛 5xx）
  - `token_budget`：ContextVar 作用域（`set/get/clear_current_ledger`）+ `budget_exhausted_error()`（「先判断再调用」）
  - `chat_service`：请求开始绑定台账、`finally` 解绑（防 ContextVar 跨请求串台）
  - `_TrackedChatOpenAI.ainvoke`：调用前预检
  - **默认 `TOKEN_BUDGET_PER_REQUEST=0` ⇒ 完全 inert，零行为变化**；实测未启用→None、10/1000→放行、1009/1000→AgentError、解绑后恢复 inert
- **记忆整理执行侧（批次 13）**：`memory_consolidation.py` 的纯函数此前**没有任何调用方**。
  新增 `LongTermMemory.consolidate(user_id, *, max_gap_days, max_groups, ttl_days, dry_run)`——
  读出全部摘要 → 交纯函数分组/合并/淘汰 → 写回并重建索引。支持 **dry-run**（整理会改写历史，
  建议先观察）；合并后溯源仍为**并集**，保证可重建判据在整理后依然成立。
  `tests/fakes/redis.py` 同步补上有序集合命令（`zadd` / `zrange` / `zrevrange` / `zremrangebyrank`），长期记忆索引依赖它们
- **轨迹持久化（批次 13，让 F1/F5/F6 真正可用）**
  - `JsonlTrajectorySink`：追加式 JSONL 轨迹后端（每行一个步骤，跨进程/重启保留，按大小轮转，损坏行跳过）；`all_traces()` 供分析
  - `export_trajectory_packages()`：把轨迹导出为 `{trace_id}.json` 轨迹包，**可被 `evals/trajectory_regression.py` 直接消费**
  - `build_trajectory_sink()`：按 `TRAJECTORY_SINK`（`memory` 默认 / `jsonl`）选择后端，任何异常降级为内存 sink
  - 新增配置 `TRAJECTORY_SINK` / `TRAJECTORY_PATH`
  - `evals/trajectory_regression.py` 新增 `--export-from` / `--export-to`：从落盘轨迹一键建立回归基线（`-b`/`-c` 改为按模式手工校验，否则导出模式会被「缺少必填参数」挡住）
- **剩余批次收口（批次 13）**
  - **P2-5 路由挂载收敛**：`knowledge_router` 由条件挂载改为**默认挂载**；`agentix/infrastructure/core/lazy.py` 提供 PEP 562 惰性导出的共享实现，消除手写 `__getattr__` 的重复与递归陷阱
  - **P2-6 T7**：删除 `domain/models/`（违反 R3 的 re-export 代理，全仓零生产引用）；`domain/__init__.py` 移除其惰性导出；`test_p17` 豁免收紧为**零豁免**（新增 AST 判定的 `test_no_exemption_remains_in_guard`）
  - **P2-4 配置治理收尾**：新增 `agentix/config/selfcheck.py`（比对 `.env` 与 `.env.example` 的**字段集合**：只比键不比值、只告警不抛异常），`main.py` 启动时调用；可独立运行 `python -m agentix.config.selfcheck`
  - 新增 `agentix/infrastructure/core/lazy.py`：PEP 562 惰性导出的**共享**实现（`lazy_submodule` / `lazy_attribute`，仅 stdlib），消除手写 `__getattr__` 的重复与递归陷阱

- **任务分解显式化（批次 12，A 组）**
  - `application/services/task_plan.py`：**声明式任务依赖**——`TaskSpec(name, depends_on, payload, meta)` + `plan_waves()`（Kahn 拓扑排序产出**执行波次**：同波次可并行、波次间串行）+ `validate_plan()`（缺失 / 自依赖 / 环形 / 重名四类结构校验）+ `summarize_plan()`（`max_parallelism` / `serial_depth`）
  - `application/services/task_retry.py`：**子任务局部重试与失败隔离**——`run_task()`（局部重试；耗尽后**不抛异常**而是返回 `TaskOutcome`，把失败变成**数据**）+ `run_batch()`（并发 + **部分成功**语义 + 并发上限）；`asyncio.CancelledError` 一律透传
  - `domain/policies/completion.py`：**确定性终态校验**（零 LLM 调用）——只看环境事实（错误是否消解 / 答案是否实质为空 / 引用类路由是否真的带来源 / 是否有工具失败），三档 `complete`/`degraded`/`incomplete`；已接 `chat_service`（结论进 `metadata.completion` 与轨迹），默认只记录不改判定（`COMPLETION_VALIDATOR_STRICT=False`）
- **Prompt 静态前缀框架级保证（批次 12，C 组）**
  - `domain/policies/prompt_envelope.py`：`PromptEnvelope`（frozen）把「静态前缀 + 动态后缀」变成**类型**，拼接顺序由 `render()` 唯一决定；`assert_static_prefix_stable()` **不变性验证**（用多组不同动态输入渲染，断言公共前缀 ≥ 声明常量——检查的是**输出**而非代码，故隐蔽的条件分支也会被抓住）；`common_prefix_len()`
- **角色差异化（批次 12，D 组）**
  - `application/agents/role_capabilities.py`：六角色的**工具集 + 上下文策略**声明表——`RoleCapability` + `ContextPolicy(max_history_messages, include_memory, include_sources, max_retrieved_docs, include_tools)`；`router`/`summarizer`/`hallucination` 零工具，`tools_for_role()` 与真实可用集求交（尊重 `DISABLED_TOOLS`），未知角色保守返回空；`roles_sharing_tools()` 暴露死工具
- **开源社区规范**
  - `CONTRIBUTING.md`（开发环境 / 架构约束 / Conventional Commits / PR 流程）
  - `CODE_OF_CONDUCT.md`（Contributor Covenant 2.1 中译 + 四级执行阶梯）
  - `SECURITY.md`（漏洞报告流程、响应时间线、10 项安全设计概览）
  - `CHANGELOG.md` + `docs/VERSIONING.md`（SemVer 规则、单一版本事实来源、分支模型、兼容性承诺）
  - `scripts/bump_version.py`（三处版本号同步 + `--check` 供 CI）
  - `.editorconfig`、`.pre-commit-config.yaml`（含**本地架构守卫钩子**）
- **CI/CD**
  - `.github/workflows/ci.yml`（7 作业：版本一致性 / lint / test / **架构守卫** / eval-smoke / frontend / `ci-gate` 汇总门禁）
  - `.github/workflows/release.yml`（版本校验 → 构建 sdist+wheel → 从 CHANGELOG 提取说明发布）
  - `.github/workflows/codeql.yml`（Python `security-and-quality`，每周 cron）
  - `.github/dependabot.yml`（pip / npm / github-actions / docker 四生态，分组更新）
  - `.github/ISSUE_TEMPLATE/*` + `.github/PULL_REQUEST_TEMPLATE.md`
- **架构文档体系**（`docs/architecture/`）
  - `README.md`（导航 / 五条设计原则 / 运行时架构图 / 代码分层地图）
  - `layers.md`（各层职责 / **4 条强制依赖规则** / 棘轮语义 / 新增文件归属决策树）
  - `interfaces.md`（端口 Protocol / Payload TypedDict / 错误码四层分类 / HTTP 公共 API）
  - `extension.md`（扩展点总览表：新增工具 / Provider / Agent 子图 / 领域适配）
  - `configuration.md`（单一事实来源 / 优先级 / **只读 property 陷阱** / 特性开关回滚表）
  - `runtime-guardrails.md`（上下文隔离 / 控制平面 / 目标仲裁 / 工具限量 / 人工审批）
  - `adr/0001`–`0003`（分层与端口 / 注册表可插拔 / 声明式配置，含备选方案否决理由）
- **可插拔扩展机制**
  - `infrastructure/registry.py`：通用 `PluginRegistry[T]`（`register` / `provider` 装饰器 / `factory` / `get` / `maybe_get`）
  - `infrastructure/llm/provider_registry.py`、`infrastructure/knowledge/{embedding,reranker}_providers.py`
  - **差异化失败语义**：LLM / Embedding fail-fast；Reranker 降级
  - `agentix/py.typed`（PEP 561）
- **可观测性（F 组缺口）**
  - `application/services/trajectory.py`：`TrajectoryStep` / `InMemoryTrajectorySink`（有界）/ `TrajectoryRecorder` / `locate_first_error`
  - **F1 完整回放**：`llm_factory._record_llm_trajectory()` + `chat_service._record_tool_step()` 落 prompt / response / 工具入参摘要，经 `redact_and_truncate()` 脱敏 + 截断；受 `OBSERVABILITY_PAYLOAD_CAPTURE` 门控（默认关）
  - **F5 轨迹前缀回归**：`trajectory_prefix_signature()` / `compare_trajectory_prefix()` / `compare_trajectory_packages()` + CLI `evals/trajectory_regression.py`（退出码可进 CI；空基线显式标注，不冒充通过）
  - **F6 步骤级失败定位**：`locate_first_error()` + `replay()`；工具 start/end 与两条错误路径均已接线
- **评估（F 组缺口）**
  - **F3 Pass^k**：`evals/run_eval.py` 新增 `_task_success()` / `compute_pass_k()`（τ-bench 口径；分母只统计跑满 k 次的任务）+ CLI `--repeat N` / `--k K`
  - **F7 回流自动化**：`evals/feedback_ingest.py`（确定性失败分类 → 候选 golden 行；**只读 golden，绝不写入**；期望值刻意留空待人工填写）
  - `evals/README.md` 补 §4.5 Pass^k、§7.1.1 回流自动化、§8 轨迹前缀回归
- **上下文管理（C 组缺口）**
  - `application/services/artifact_store.py`：**上下文隔离通道**（`ArtifactRef` 句柄 + 有界 LRU + **外置失败降级内联** + `evicted_unrebuildable` 计数）；`ARTIFACT_INLINE_THRESHOLD=2000`
  - `application/services/memory_consolidation.py`：**遗忘/整理机制**（`select_consolidation_groups` / `merge_summaries` / `plan_summary_eviction`，纯函数）
  - **可重建性接进淘汰路径**：`plan_message_eviction()` 用 `is_rebuildable` 决定物理删除；保守默认（无证据即保留）
- **控制平面（D 组缺口）**
  - `application/services/token_budget.py`：**token 预算**（三档判定 OK/WARN/EXCEEDED + `would_exceed` 预检 + 请求级/子 Agent 两级台账）；`chat_service` 接线，默认 `TOKEN_BUDGET_PER_REQUEST=0` 不限额
  - `application/services/control_plane.py`：**控制平面**（spawn / bind / status / finish / **cancel 真正打断 asyncio.Task** / per-run 预算）；`chat_service._invoke_tracked()` 接线
  - `domain/policies/goal_arbitration.py`：**目标优先级与冲突仲裁**（硬优先级 安全>合规>准确>有用>时延>成本 + 8 组冲突规则 + 确定性全序；显式提权不可越过硬优先级）
- **工具护栏（B 组缺口）**
  - `infrastructure/tools/result_limiter.py`：**感知工具下游结果限量**（行分页 + 字符截断 + `PageMeta.notice()` 显式告知模型）；`search.py::_bounded()` 接线
  - `infrastructure/tools/human_review.py`：**高风险动作人工审批旁路**（确定性风险分级 + 异步 `decide()` / 同步 `preflight()` 双通道 + **fail-closed** + 「一次批准只放行一次」）；`get_tool()` 接线，默认关

### 变更 (Changed)

- **CI 作业命令逐个本地实测通过**（批次 13）：`bump_version --check` / `ruff` / `pytest` /
  架构守卫 / `test_p21` 冒烟 / `npm run lint` + `npm run build`（`vite build` 846ms）。
  并回查 `vite@5.4.21` 的 `engines`（`^18.0.0 || >=20.0.0`）确认 CI 固定 Node 18 不会导致构建失败
- `pyproject.toml`：补齐 `[project.dependencies]`（原**缺失**）、`classifiers`、`keywords`、`license`、`[project.urls]`、可选依赖组；`requires-python` 由 `>=3.9` 统一为 `>=3.10,<3.11`；新增 `[tool.ruff]` / `[tool.coverage.*]`；pytest 增加 `--strict-markers`
- `requirements.txt`：`lightrag-hku` 补上界 `<2.0.0`；新增 `ruff`、`types-PyYAML`、`types-requests`
- `agentix/__init__.py`：`__version__` 0.1.0 → 0.1.2（与 `pyproject.toml`、`web/package.json` 对齐）
- **代码结构整理（批次 13）**
  - `GUARDRAILS_SYSTEM_PROMPT` 消歧义：该名曾在 `lg_prompts.py` 与 `kg_prompts.py` **同名异值**（主图应用范围检查 vs KG 图谱范围判定）。各新增语义化正式名 `GUARDRAILS_APP_SCOPE_SYSTEM_PROMPT` / `GUARDRAILS_GRAPH_SCOPE_SYSTEM_PROMPT`，旧名保留为**显式别名**（`is` 同一对象）
  - `NO_CYPHER_RESULTS` 统一：原有一份英文（模块级）+ 一份中文（函数内局部）同语义双份，统一到 `constants.py` 单一来源（中文）
  - `kb_ingest/.env.example` 从「根目录 `.env.example` 的整份拷贝」（含 Milvus/Neo4j/LightRAG/CORS 等主应用专有项）重写为 **kb_ingest 真实契约**（44 个变量）
- **ruff 规则冻结为分类忽略清单**（每类写明理由）：`typing.List/Dict` 风格（~2200 处）刻意保留；`W291/W293` 因位于 prompt 多行字符串内（删除会改变送给模型的字节、击穿逐字节断言）而忽略，`.pre-commit-config.yaml` 的 `trailing-whitespace` 钩子加同源排除
- black 由**阻断**降级为**非阻断**（CI `continue-on-error` + pre-commit `stages: [manual]`），全量迁移后可升级为门禁
- 修复 104 处零风险卫生问题（尾随空白 / 冗余 f-string / 多语句行 / `dict()` 字面量）

### 修复 (Fixed)

- **版本号单一事实来源被破坏（批次 13）**：`settings.APP_VERSION` 硬编码 0.1.2，而 `.env` / `.env.example` 写 `APP_VERSION=0.1.1` → 运行期被覆盖，**健康检查对外报 0.1.1**，而 `bump_version --check` 只比对三处代码文件，完全看不见该漂移。修复：`settings.APP_VERSION` 改为 `default_factory` 惰性读 `agentix.__version__`；`.env.example` 纳入 `--check`（现为四处）；同步 `.env.example` 与本地 `.env` 至 0.1.2
- **断链 import（批次 13，共发现 3 处）**：模块引用**根本不存在**的目标，导致文件**永久无法导入**、永远 0% 覆盖却挂在覆盖率分母里。已修两处，第三处按 P2-5 标注
  - `ps_genai_agents/components/guardrails/prompts.py`：`from ..utils.utils import ...`（无 `utils` 包，且符号从未使用）→ 删除；修复后该家族 0 个模块导入失败
  - `application/services/search_service.py`：`from agentix.tools import ...`（无 `agentix.tools`，真实路径是 `agentix.infrastructure.tools`）→ 改为正确路径
  - `.../workflows/multi_agent/text2cypher_with_viz_and_follow_ups.py`：`from .edges import viz_mapper_edge`（`edges.py` 无此符号）→ **刻意不猜测语义**，标注 `DEAD-CODE(P2-5)` 并在 `test_p40` 登记；删除需授权
- **覆盖率分母治理（批次 13）**：`[tool.coverage.run/report]` 的 `omit` 只补**默认配置下不可达**的代码——Streamlit UI 组件与一次性 ingest 脚本。`agentic_rag_agents` 其余部分（约 3069 stmts，确有 47% 覆盖）**刻意保留**在分母内，不因"是内嵌框架代码"就整体豁免
- **边界条件（批次 13）**：`offload_content` 在 `store.put` 返回 `None` 时原会构造「外置成功但无句柄」的不一致态，导致内容**既不在 state 也取不回来**（静默丢失）。现降级为内联并记录 `degraded_reason`；`OffloadResult.value()` 不再抛异常
- **`test_p17::test_probe_import_would_trip_the_guard` 长期失败**：根因是对抗性探针**写进真实源码树**（`agentix/infrastructure/_p26_probe_interfaces_tmp.py`），清理在受限文件系统下失败并**残留文件**，进而毒化同组的 `test_no_infrastructure_file_interfaces_imports`（本次实测确实残留）。修复：`_infrastructure_interfaces_offenders(root=None)` 参数化扫描根，探针改写到临时目录（扫描函数与真实守卫完全相同）；新增 `test_real_infrastructure_tree_is_clean` 守护真实树
- `evals/run_eval.py::format_summary_line` 在多次采样时因格式化参数缺失抛 `KeyError: 'max_runs'`
- **mypy 门禁不可用**：`mypy agentix/domain` 会跟随 import 并检查整个依赖图（67 个错误全在 `infrastructure` / `interfaces`，耗时 24 分钟）。修复：`[tool.mypy]` 增加 `follow_imports = "silent"`（读取被导入模块类型但不上报其错误）+ CI 缓存 `.mypy_cache`（首次 12 分钟 → 增量 14 秒）；同时补齐 `domain/policies/retry.py` 缺失的 2 处类型标注（`aretry` 返回类型、`wrapper` 的 `*args/**kwargs`）

### 测试 (Tests)

- 新增 356 例：`test_plugin_registry.py`（25）、`test_observability_trajectory.py`（53）、`test_p30_context_artifacts.py`（40）、`test_p31_control_plane.py`（45）、`test_p32_tool_guards.py`（49）、`test_p33_task_planning.py`（90）、`test_p35_config_governance.py`（22）、`test_p36_structure_hygiene.py`（8）等
- 全量：**876 passed / 9 skipped / 0 failed**（批次 11 前为 520 passed / 1 failed / 9 skipped）

---

## [0.1.2] - 2026-09-11

> 架构优化批次 1–10：从「可运行原型」升级为「具备生产级可靠性的 Agent 系统」。

### 新增 (Added)

- **P0 可靠性**
  - Redis Checkpointer（`langgraph-checkpoint-redis`）替代 `MemorySaver`，会话状态跨进程持久化，含三级降级
  - LLM 调用加固：分级超时 / 指数退避重试（`aretry`）/ 并发信号量 / 错误分类
  - 领域错误分类（`domain/errors.py`）：四层故障分类 + 可降级集 + HTTP 状态映射
  - 可观测性接线：`trace_id` 中间件、LLM 调用埋点、请求级指标落库
  - 鉴权与密钥固化：生产环境缺失 `SECRET_KEY` 启动失败
- **P1 架构质量**
  - 拆分 630 行 God Function → `kb_nodes/` 5 个独立节点 + 格式化纯函数 + prompt 外置
  - Neo4j 连接单例 + 子图编译 `lru_cache` 复用
  - 工具注册表落地（`ToolRegistry` + 声明式清单 + 描述含「何时不该用」反例）
  - 真流式响应（`astream_events` + 节点级白名单过滤，防内部编排 token 外泄）
  - 检索并发化（`asyncio.gather` + 部分成功语义）
  - 业务逻辑从 API 层下沉（`ChatService`），`chat.py` 653 → 322 行
  - 补齐 `domain/` 层骨架（`models` / `ports` / `policies`）并清零既有依赖违规
- **P2 工程化**
  - 法律领域评估集（`evals/`）：22 条 golden 覆盖 7 类路由 + 3 个确定性指标
  - 编排替代性论证（`docs/orchestration-rationale.md`）
  - 核心链路单测（`kb_nodes` / 路由 / 流式 / 检索 / 服务层）
  - Prompt 外置与静态前缀排序
  - 终态验证器（`final_answer_guard`，默认关）
  - 可重建压缩判据（`is_rebuildable` + `source_message_ids` 溯源）
  - 记忆上下文接线（`get_context_for_query`，默认关）

### 变更 (Changed)

- `multi_tool.py` 862 → 225 行
- 主图新增 `message_window` 入口节点，历史消息窗口化（默认 20 条，防无界增长）
- `role=` 分级模型路由接线（router / hallucination / summarizer / general）

### 安全 (Security)

- Cypher 注入防护：写子句黑名单补全 + 执行前只读闸门 + 标识符白名单
- 上传路径遍历防护、分块写入防 OOM、`file_id` UUID 校验
- 错误信息不泄露内部异常详情

---

## [0.1.0] - 2026-07-27

### 新增 (Added)

- 首个可运行版本：三层 Multi-Agent 架构（主路由层 → 多工具子图层 → 原子工具层）
- LangGraph 多智能体编排 + GraphRAG 图谱检索 + Text2SQL + 多源知识融合
- 法律法规知识图谱（4 部法规 / 1953 法条 / 67 司法解释）与向量语料（4865 块）
- FastAPI 后端（RESTful + SSE）与 Vue 3 前端聊天组件
- Docker Compose 一键部署（Neo4j / Milvus / PostgreSQL / MySQL / Redis / LightRAG）

---

[Unreleased]: https://github.com/Jia-Yihao/Legelbot/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/Jia-Yihao/Legelbot/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/Jia-Yihao/Legelbot/releases/tag/v0.1.0
