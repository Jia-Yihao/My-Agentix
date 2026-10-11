# Phase 0 · current-tree.md — 现有目录树与新建缺口

- 生成时间：2026-09-23（数字同步：2026-10-04 · Stage T 终态收口）
- 范围：仓库根与 `agentix/`（不含 `__pycache__`、`.venv`、`node_modules`）
- 依据：`docs/migration/Agent_Core_2.0_规划.md` v1.1；结构迁移对照见
  `docs/migration/架构重构_统一执行链方案_v2.md` §5（终态矩阵）
- 状态：统一执行链 Stage A/B/M/F/T 全部落地；三簇 shim 清零（旧路径一律
  `ModuleNotFoundError`，p66 `SHIM_DIRS_ALLOWED` 空集断言）

## 1. 仓库根

```text
.github/          CI / release / perf-baseline
.env.example      运行时密钥样例（295 活行，2026-10-06 实测）
agentix/          Python 包（541 个 .py，不含 __pycache__，计数以脚本为准）
archive/
data/
docker/
docs/             architecture / design / guides / runbooks / migration（本文）/ assets / VERSIONING
                  + 项目说明书（PROJECT_SPEC.md + 图文版 docx）
evals/            legal_golden.jsonl（102 行）+ run_eval / trajectory_regression /
                  quality_gate / claim_check / evolution_run / memory_eval /
                  hybrid_eval（R-P2-5 四列归因）+ ablations/ baselines/
kb_ingest/
scripts/
skills/           Agent Skills 目录（Phase 21 示例 release-checklist，SKILLS_ENABLED 默认关）
tests/            191 个测试模块（顶层 .py 不含 conftest）；基线见 README §11（唯一真源，带实测日期）
uploads/
web/              Vue 前端（vue-tsc 必须过）
```

## 2. `agentix/` 一级包

```text
agentix/
  __init__.py
  main.py                 # FastAPI / 组装入口
  application/            # 业务规则与图接线（agents/ + services/）
  config/
  crawler/
  domain/                 # 领域层：agent/ runtime/ tool/ ports/ policies/ 纯契约（零第三方）
  infrastructure/         # core/ llm/ persistence/ text2sql/ registry.py
  interfaces/             # HTTP v1 + models + ops_router
  observability/
  orchestration/          # ★ Stage M·M2：orchestrator / executor / langgraph/ / strategies/
  policies/               # ★ Stage M·M3：planner/ verifier/ hitl/ guardrails/
  reliability/
  retrieval/              # 独立检索/RAG 子系统（domain/api/infra/usecases，自带 ReAct 已随 M5 剥离）
  runtime/                # 真源：state/steps/budget/lifecycle + control_plane（M4 迁入）
  security/
  tools/                  # ★ Stage M·M1：gateway/ adapters/ definitions/ selection（唯一工具入口）
  agent/                  # Context Provider 家：context/memory/events/observer/policy/skills/hooks/evolution/graph（占位）
  capabilities/           # 仅 mcp/（M6 后 infrastructure/tools 与 capabilities/tools 已删）
```

## 3. `agentix/application/` 详树

```text
application/
  agents/
    clarify.py, compactor.py, constants.py, file_handler.py,
    general_handler.py, guardrails.py, image_handler.py, kb_tools/,
    lg_prompts.py,            # 业务 prompt 留 application（M2 裁决）
    llm_factory.py, main.py, role_capabilities.py, router.py,
    task_handler.py, utils.py,
    text2sql/               # StateGraph 工作流
    kg_sub_graph/
      planner/planner_node.py          # 子图规划器（已有）
      kg_states.py, kg_tools_list.py, kg_neo4j_conn.py
      prompts/, ps_genai_agents/
      agentic_rag_agents/
        components/   # planner, tool_selection, validate_final_answer,
                      # final_answer, guardrails, text2cypher/*, text2sql/*,
                      # visualize/*, errors/tool_selection, summarize,
                      # predefined_cypher, cypher_tools, customer_tools, utils
        workflows/
          multi_agent/    # kb_nodes, prompts, edges, kb_workflow, multi_tool, ...
        retrievers/
  services/                # ★ 应用服务（Memory/Task/Trajectory/网关 等；control_plane → runtime/ M4，
                            #   hitl_bridge → policies/hitl/ M3，llm_factory → agents/ 已迁）
    agent_executors.py, agent_loop_adapter.py, artifact_store.py,
    breakpoint_resume.py, budget_notice.py, card_schema.py, chat_service.py,
    config_watch.py, cron_jobs.py, cron_scheduler.py,
    kg_gateway_bridge.py, knowledge_gateway.py, lightrag_service.py,
    llm_client.py, long_term_memory.py, memory_consolidation.py,
    memory_manager.py, memory_review.py, memory_tools_bridge.py,
    notify_bridge.py, observability.py, profile_scope.py, prompts.py,
    redis_cache.py, request_scope.py, rollback.py, run_event_stream.py,
    semantic_memory.py, task_execution.py, task_handlers.py, task_plan.py,
    task_queue.py, task_retry.py, task_tools_bridge.py, todo_write.py,
    token_budget.py, trajectory.py, __init__.py
```

> `application/runtime/` 已随 A4 直删（p64 断言目录不存在 + import 失败）。

## 4. `agentix/domain/`（接口先行落点，零第三方）

```text
domain/
  errors.py, status.py, tracing.py, evals.py
  agent/                  # ★ M0 拆入：types.py（核心类型）+ ports.py（七端口规范位）
  runtime/                # ★ M0 拆入：budget.py / lifecycle.py（退出契约）/ runs.py / steps.py
  tool/                   # ★ M0 拆入：spec.py / risk.py / permission.py 纯契约
  policies/
    completion.py, error_classification.py, evidence_gate.py,
    goal_arbitration.py, grounding_verify.py, ingest_auth.py,
    legal_effectiveness.py, memory_recall.py, prompt_envelope.py,
    rank_fusion.py, retry.py, upload_paths.py
  ports/
    __init__.py           # 七端口自 agent/ports.py 规范位再导出 + 本包 graph/payloads/repository/retrieval
    graph.py        # GraphProviderPort（LangGraph 可替换接缝）
    payloads.py     # Session/Message TypedDict
    repository.py   # SessionRepositoryPort / MessageRepositoryPort
    retrieval.py    # RetrievalPort
```

守卫：`tests/test_p17_domain_layer.py` — domain 禁 `infrastructure`/`interfaces` 与 FORBIDDEN_THIRD_PARTY（含 langgraph/pydantic/fastapi/redis/openai…），**零豁免**。
旧路径 `agent_types.py` / `ports/agent_core.py` 已随 M6 删除（p69 `SHIM_PATHS_DELETED_M6` 断言）。

## 5. `agentix/infrastructure/`（工具与基建）

```text
infrastructure/
  core/            # logger, database, security, ratelimit
  llm/
  persistence/     # repositories, checkpointer, crud/, db/models/
  text2sql/        # wren_adapter
  registry.py
```

> `knowledge/` 与 `tools/` 已随 M 批清理：工具唯一入口为顶层 `tools/`（M1），
> 向量/Reranker/Embedding 归检索域 infra。

## 6. `agentix/retrieval/`（RAG 子系统，部分上提为 Agent Core 底座）

```text
retrieval/
  api/            # routers/（含 approvals.py）+ schemas/
  domain/
    models/ policies/
    services/
      context_engine.py    # ★ Phase 5 底座：assemble_context / build_status_bar / 状态栏
      citation_verify.py, chat_scope.py, manifest_diff.py
      # grounding_verify.py 已上提 domain/policies/（与 L1/L3 纯函数同居）
  infra/          # vectorstore (milvus/qdrant/faiss), llm, embedding, rag,
                  # tasks (celery), storage, cache, auth, parsers, repos
  usecases/       # chat_service, chat_agent_*（M5 剥离 langgraph ReAct，改 capability 内部循环）,
                  # kb_*, approval_service, conversation_*, document_*, index_*, feedback_*, ...
  settings.py
```

## 7. 其余一级包

```text
orchestration/    # ★ M2：orchestrator.py, executor.py, langgraph/（lg_builder/lg_states/lg_supervisor）,
                  #        strategies/multi_agent/（Peer + Manager + 子代理隔离）
policies/         # ★ M3：planner/（simple+retry）, verifier/, hitl/（sidecar + hitl_bridge）, guardrails/
tools/            # ★ M1：gateway/, adapters/, definitions.py, selection.py, registry_init.py
observability/     trajectory.py (运行时旁路 Recorder), tracing, spans, metrics,
                   logging_config, context, versions
reliability/       熔断/重试等
interfaces/http/   v1 路由（chat/sessions/upload + deps/events）+ models + ops_router（引用 lg_builder.graph）
security/          jwt, output_filter, prompt_injection
config/            settings, selfcheck, ablation
crawler/
capabilities/mcp/  protocol, client, 零依赖 stdio server（python -m agentix.capabilities.mcp.server）
```

## 8. 相对目标架构的缺口清单（Stage T 终态复核）

| 目标路径 | 现状 | 动作 | Phase |
|----------|------|------|-------|
| `agentix/agent/**` | Context Provider 家（编排/策略件已交出：M2/M3 迁 orchestration/ + policies/） | 完成 | 2–5 / M2–M3 |
| `agentix/runtime/**` | 真源（state/steps/budget/lifecycle + control_plane M4 迁入） | 完成 | 14 / M4 |
| `agentix/capabilities/**` | 仅 mcp/（工具已迁顶层 tools/，M1） | 完成 | 6 / M1 |
| `domain/agent` + `domain/runtime` + `domain/tool` 契约 | M0 拆分规范位；旧路径 M6 删除 | 完成 | M0 |
| `docs/migration/*` 三件套 | 本目录新建 | 与 current-tree 同批落盘 | 0 |
| 全局唯一 trajectory | 已收敛（Phase 10：observability 为 services 薄适配 `as_service_steps` 桥） | 完成 | 10 |
| 全局唯一风险词表 | 单处 L0–L3↔RiskLevel 映射（规范位 `domain/tool/risk.py`，M0 拆入） | 完成 | 6 / M0 |
| 三簇 shim 清零 | application/runtime（A4）、infrastructure/tools、agentix/mcp 及 M1–M3 过渡 shim（M6）全删 | 完成（p66/p69/p75 断言） | A4 / M6 |

## 9. 明确不动（本批）

- `retrieval/` 存储/向量/Celery 实现细节
- `application/agents/kg_sub_graph/**` 业务图（仅在 Phase 2+ 通过 L2/L3 分工接入，不重写）
- `web/`、`Dockerfile`、`.env` 密钥轮换（遗留用户决策）
