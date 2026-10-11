# Memory 深度优化设计（Phase 32）

> 总纲约束见 `docs/migration/Phase32-34_Memory-RAG-MultiAgent优化总纲.md` §0–§5。上位定位：**Memory 是 Context Provider + Writer，不拥有控制流**。研读对照见 `docs/source-code-study.md` §3.5–§3.6、§5 P6。
>
> **状态（2026-10-02，v2 Stage B1 已完成；执行蓝图见 `docs/migration/架构重构_统一执行链方案_v2.md` §4）**：
> **P0 全部完成 ✓**——M-P0-1（双卡统一 Card Schema：`agentix/application/services/card_schema.py` 单源，冲突只标记不覆写）、M-P0-2（A2 经 Tool Gateway，守卫 p62）、M-P0-3（`redact_pii` 中英姓名/地址规则只增不减 + LTM/Semantic 写路径 + 读路径二次脱敏，privacy_leak=0）、M-P0-4（`resolve_conflicts` 全 tags 键 + 30 天时间窗分簇 + `meta.conflicts` 标记不静默）、M-P0-5（README 与服务 docstring 统一 3 scope）——守卫 `tests/test_p67_memory_p0_convergence.py`（p67），评测 `evals/memory_eval.py`（报告键 profile_stability / conflict_precision / privacy_leak，不进门禁）。
> 早期已完成 ✓：M-P2-1（/learn → create_skill_draft）、M-P2-3（/context → GET /ops/context）。已完成：M-P1-2（P6 规则版 memory_review + **F1b LLM 路由 `route_memory_item_llm` + turn-end 生产者 `_maybe_schedule_turn_review`**，fail-open 回规则）、M-P1-4（**F1b2 级联已做**：全局 `~/.agentix/` → 项目根 → 消息触达子目录三层 + `read_agents_file` 按需取回，`AGENTS_CASCADE_ENABLED` 默认关）、M-P1-3（**F1b2 已做**：`MEMORY_INDEX_ENABLED` 索引 + `obs://` 指针，正文外置 ArtifactStore，预算封顶截尾）、M-P1-1（F1b 向量召回）、M-P1-5（F1b 真分词计数 + **F1b2 obs:// 隔离接线**：`CONTEXT_ISOLATE_PERSIST_ENABLED` 让隔离引用真正可取回）——守卫 F1b `tests/test_p77_memory_p1_wiring.py`（p77）、F1b2 `tests/test_p78_index_cascade_isolate.py`（p78）。**F2a 已完成 ✓（2026-10-03）**：M-P2-2（`CONTEXT_PRECOMPACT_REINJECT_ENABLED` PreCompact 重注，见 §6 注记）、M-P2-4（`MEMORY_PROFILE_SCOPE_ENABLED` 画像作用域 + 授权边界 + 一键清除全链路，见 §6 注记）——守卫 `tests/test_p82_memory_p2.py`（p82）。P2 全部完成。

---

## 1. 现状实测（2026-10-01 代码级）

- 进程内记忆：`agentix/agent/memory/service.py` 的 `InMemoryMemory`（`recall/remember` + `redact_pii` + `resolve_conflicts`）为纯内存 list、无持久化/TTL/容量、无向量召回（子串匹配），注释自述“适配不重写”，实际未与 LTM 打通。
- 契约：`agentix/domain/agent/types.py`（`MemoryScope/Kind/Item`）与 `agentix/domain/agent/ports.py`（`MemoryPort/ContextPort/CompactorPort`）解耦良好，是保留基座。
- 长时记忆：`agentix/application/services/long_term_memory.py`（`LongTermMemory` + `fold_profile_card/derive_profile` + Redis 键 `ltm:*`）与 `agentix/application/services/semantic_memory.py`（规则抽卡 `extract_cards/merge_cards`）为**双轨**：画像卡（Phase 29）与语义卡（R3-8）旗标、生产者、消费者各自分离；`agentix/application/services/memory_consolidation.py`（`select_consolidation_groups/plan_*_eviction`）纯函数 + 归档先删后备。
- 上下文/压缩：`agentix/agent/context/engine.py`（`ContextEngine`）+ `agentix/agent/context/compaction.py`（五要素保真 + `maybe_compact`）+ `agentix/application/agents/compactor.py`（`LLMCompactor`）构成 L1–L4，但 token 估算 `len//4`、规则英文、`CONTEXT_AUTOCOMPACT` 默认关、主循环一次性构建（`agentix/orchestration/orchestrator.py` budget=0）。
- 指令文件：`agentix/agent/context/instructions.py`（`build_instructions` + hash 去重 + 12k 预算）已有 Codex 式预算雏形，但无级联（root 常驻 + 子目录按需）与豁免语义。
- 关键缺口：全部强记忆路径默认关（`MEMORY_CONTEXT_ENABLED / MEMORY_CONSOLIDATION_ENABLED / CONTEXT_AUTOCOMPACT / MEMORY_PROFILE_CARD_ENABLED`，见 `agentix/config/settings.py`）；`agentix/capabilities/` 零记忆工具（Agent 不能显式 remember/recall/forget）；`LongTerm/Semantic` 写路径无 PII 脱敏；语义抽取中文关键词 + 固定 confidence；`resolve_conflicts` 只看 `tags[0]`。

---

## 2. 对标结论（搬什么、不搬什么）

| 产品 | 可搬设计 | 落点 |
|---|---|---|
| Codex | AGENTS.md 级联合并（`~/.codex` → 各层目录，32KiB 封顶，越界截断+提示）；`build_prompt = 环境 + AGENTS + memory summary + 工具集`；三压缩后端 + JSONL rollout；`/memory` 显式追加 | 指令级联 + 预算去重强化 + 显式沉淀入口 |
| Claude Code | 记忆三分类：指令记忆（CLAUDE.md 常驻）/ 持久工作记忆（MEMORY.md 索引 + topic 文件存正文）/ 会话记忆（压缩用摘要）；`MEMORY.md 只做索引`防膨胀；root 常驻、子目录按需，压缩时重读；`PreCompact Hook` 把关键规则重注到尾部高注意力区；“prompt 是请求，Hook 才是强制” | MEMORY 索引制 + PreCompact 重注 + Hook 强制观 |
| Hermes | `MemoryManager` 多 provider 扇出 + ABC 边界；`USER.md（长期画像）vs MEMORY.md（工作记忆）` 双路由由后台异步评审决定（turn 后线程，不阻塞）；FTS5 fail-open（索引坏也照查）；`/learn` 显式沉淀成功经验为技能草稿 | 后台评审 + 双路由 + fail-open + /learn |
| 智谱系 | 用户画像分级授权（画像可见范围可配）；企业知识与个人记忆隔离；记忆引用必须可溯源、可删除 | 画像作用域 + 引用隔离 + 一键清除 |

不搬：Codex 服务端加密 compaction blob（不适合本仓自托管）；Claude API 侧 auto-memory 黑盒（本仓要白盒可审）。

---

## 3. 目标架构

```
Turn 上下文组装（ContextEngine）
├── L0 指令层：级联 AGENTS（全局→项目根常驻→子目录按需）+ 预算/去重 + 压缩时重读
├── L1 工作层：窗口消息 + 状态栏（常量键）+ 预算尾标
├── L2 记忆层：画像摘要 + 语义卡（向量召回 TopK，fail-open）+ 引用隔离（超长 obs://）
├── L3 会话层：轨迹摘要（压缩器，保真五要素）
└── L4 工具层：remember / recall / forget / learn 四工具（经 Tool Gateway）
        ↕
Memory 内核（统一五层模型，单 MemoryPort）
├── Working / Episodic / Semantic / Procedural / Profile
├── 存储：Redis（索引/画像/快照）+ 向量（语义召回）+ JSONL（审计）
└── 后台：异步评审（USER vs MEMORY 路由）+ 巩固（consolidation）+ /learn 沉淀
```

原则：读时融合（多源召回→排序→预算截断→标注来源）、写时分流（后台决定落哪层）、删时彻底（用户级一键清除 + 审计）。

---

## 4. P0 收敛（不加智能，先消双轨与裸奔）

> **B1（2026-10-02）已完成以下四项**：M-P0-1 / M-P0-3 / M-P0-4 / M-P0-5（M-P0-2 已在 A2 提前完成）——守卫 `tests/test_p67_memory_p0_convergence.py`，评测 `evals/memory_eval.py`。

- M-P0-1 合并双卡：画像卡与语义卡统一为一套 Card Schema（只增字段，不改旧序列化，沿 Phase 28–31 纪律）；`fold_profile_card` 与 `merge_cards` 冲突语义对齐为“同键冲突只标记不覆写”（已是画像侧语义，语义侧子串覆写改为标记）。
- M-P0-2 读写工具化（规划新建，不加反引号）：新增记忆工具组（remember / recall / forget 三 ToolSpec，经现有 Tool Gateway 治理链注册；默认 L1 风险，走 HITL fail-closed）。[path 写作纪律：规划新建路径在本文中均以 plain 文本给出，如 agentix/capabilities/tools/ 下新增 memory_tools 模块（规划新建）——终态已落地 agentix/tools/adapters/memory_tools.py（M1 顶层工具层），旧 capabilities/tools 路径 M6 已删。]
- M-P0-3 脱敏补齐：`redact_pii` 覆盖中英文姓名/地址/证件（规则只增不减），写路径全覆盖（含 LTM/Semantic），读路径对命中片段二次脱敏；原文零留存抽查进评测。
- M-P0-4 冲突键加固：`resolve_conflicts` 键从 `(kind,subject,tags[0])` 扩展为全 tags + 时间窗语义（同主体新值标记冲突而非静默覆写）。
- M-P0-5 写作纪律：本仓现状 `MemoryScope` 为 3 值（user/knowledge/task）而 README 写“2 scope”，以代码为准并在 README 另批修正，本文不新增并列口径。

---

## 5. P1 接线（灰度打开在库能力）

- M-P1-1 向量召回：Semantic 层接 embedding + 向量检索（复用检索域向量栈，不另建框架），`recall` 改为“关键词 × 向量”双路 + recency 衰减（沿用 `MEMORY_RECENCY_HALF_LIFE_DAYS`），TopK 可配；向量不可用时关键词 fail-open。【F1b 已落地：`MEMORY_VECTOR_RECALL_ENABLED` 默认关；进程内 provider 双路融合（纯策略 `agentix/domain/policies/memory_recall.py`），embedding 复用 `embed_texts_sync`（内部 1.0s 预算 < provider 1.5s 超时），失败超时回纯关键词】
- M-P1-2 后台评审：turn 结束后异步线程跑评审（写 USER 画像 vs MEMORY 工作记忆由 prompt 决定，不阻塞响应；评审失败只打点不影响主链，沿 Hermes `background_review` 语义）。【F1b 已落地：`route_memory_item_llm`（prompt 路由，fail-open 回规则标 `fallback-rule:` 前缀）+ turn-end 生产者 `_maybe_schedule_turn_review` 三接线点（ask 成功 / stream 成功 done / 兜底 done），gate 沿 `MEMORY_REVIEW_ENABLED`】
- M-P1-3 MEMORY 索引制：MEMORY 入口只保留索引（topic → 文件指针），正文进 topic 文件；常驻预算封顶，超限只截索引尾部 + 给模型“按需取”工具。【F1b2 已落地：`MEMORY_INDEX_ENABLED` 默认关（挂 `MEMORY_CONTEXT_ENABLED` 通道）；flag-on 记忆注入段落头保留、正文行经 `offload_content`（kind=`memory_topic`）外置后替换为 `- 预览 → obs://{artifact_id}` 索引行，常驻预算 `MEMORY_INDEX_MAX_CHARS=1500` 超限截尾附提示；取回走 `read_artifact`（`obs://` 前缀自动剥离），外置降级该行保留原文（fail-open 不丢记忆）】
- M-P1-4 AGENTS 级联：`instructions.py` 升级为“全局→项目根（常驻）→子目录（按需，触达才加载）”，单 4k/总 12k + hash 去重保留，压缩时重读根级。【F1b2 已落地：`AGENTS_CASCADE_ENABLED` 默认关；flag-on `build_instructions` 追加全局层（`~/.agentix/AGENTS.md`，`global_start` 可注入）与子目录层（`mentioned_instruction_dirs` 从消息解析触达目录，逐目录向上找最近约定文件，根级命中由 hash 去重丢弃），预算/去重沿既有机制；运行期按需走 `read_agents_file`（L0 只读，项目边界内向上查找）；生产接线 `agent_loop_adapter` 传 `subdirs=mentioned_instruction_dirs(message)`（flag-off 返回 () 与基线逐字节一致）】
- M-P1-5 真分词/真计数：token 估算换分词器（复用 LLM 侧 tokenizer，不新增服务）；超长记忆进 `obs://` 隔离引用并配取回工具（与渐进上下文共用）。【两半均已落地：计数半（F1b）`CONTEXT_REAL_TOKEN_COUNT_ENABLED` 默认关 ≡ `len//4`，flag-on 走 tiktoken cl100k_base、缺失退 CJK 启发式，接线 engine/simple/compaction 7 处估点；obs:// 半（F1b2）`CONTEXT_ISOLATE_PERSIST_ENABLED` 默认关 = `ContextEngine` 隔离引用与基线逐位一致（`obs://{step_id}` 裸引用），flag-on 超长观测正文经 `offload_content` 真正外置、引用落 `obs://{artifact_id}` 可经 `read_artifact` 取回，外置失败 fail-open 回裸引用；超长记忆的外置即 M-P1-3 的索引指针，取回工具与渐进上下文共用】

旗标（全部默认关，off 字节等价；键名落在 `docs/architecture/configuration.md` 统一维护，本文只列语义）：记忆上下文总开关、向量召回开关、后台评审开关、画像卡开关、自动压缩开关。P1 灰度顺序：工具化 → 向量召回（影子跑）→ 后台评审 → 级联/压缩。

---

## 6. P2 智能（新增能力）

- M-P2-1 /learn 沉淀：显式入口把一次成功经验沉淀为 Procedural 草稿（→ Skill 草稿，与 Skills 体系联动，见 Multi-Agent/技能侧规划）。**【早期已落地】** `/learn` → `skills/learn.py::create_skill_draft`。
- M-P2-2 PreCompact 重注：压缩前把关键规则/用户硬约束重注到上下文尾部（高注意力区），解决“长会话规则丢失”；规则来源可审计。**【F2a 已落地（2026-10-03）】** flag `CONTEXT_PRECOMPACT_REINJECT_ENABLED` 默认关（挂 `CONTEXT_AUTOCOMPACT` 通道）：`compaction.build_reinject_block` 纯函数——**约束优先**（原轨迹五要素 `constraint_key` token 去重按出现序）+ 规则行按声明序补满（`PRECOMPACT_REINJECT_MAX_CHARS=1500` 有界），行带 `[precompact:<source>]` 审计标记、`ContextSnapshot.meta["precompact_reinject"]` 记来源计数；`ContextEngine`/`SimpleContextBuilder` 在超预算触发时**压缩前**采集、块挂 `assemble` 尾部（不在 `traj_lines` 内 ⇒ 丢弃循环不删，规则必须幸存；计入 token 估算 ⇒ 预算照常收敛）；flag-off 块恒空与基线逐字节等价。
- M-P2-3 /context 只读诊断：分类用量表 + 网格可视（不发 provider 请求、不影响缓存/计费，沿 Hermes 只读契约）。**【早期已落地】** `GET /ops/context` → `observability.context_report`。
- M-P2-4 画像作用域：画像字段分级（长期稳定 vs 会话偏好），跨会话沿用需授权边界；用户一键清除画像 + 语义卡 + 摘要全链路。**【F2a 已落地（2026-10-03）】** flag `MEMORY_PROFILE_SCOPE_ENABLED` 默认关：① 分级单源 `profile_scope.PROFILE_FIELD_TIERS`（`user_constraints`/`legal_domain_prefs`/`conflicts`=stable、`usage_habits`=session、`schema_version`=meta；**未知字段 fail-closed 按 stable**），投影纯函数 `project_profile_card`/`project_profile_topics`；② 授权边界——`LongTermMemory.profile_scope_consent/set_profile_scope_consent`（键 `{prefix}:profile_scope:{user}`，读取异常 fail-closed），`get_context_for_query` flag-on 时稳定层（画像卡稳定字段/频繁领域/语义卡）仅授权后注入、未授权只注会话层并审计留痕，flag-off 投影为直通拷贝与基线逐位等价；③ 授权面 `GET/POST /ops/memory/profile-scope`；④ 一键清除 `POST /ops/memory/clear-user` → `clear_user_profile_chain`：画像（含 profile_card）+ 语义卡（`SemanticMemoryStore.clear_cards`）+ 摘要/索引（复活 `clear_user_memories`）分段尝试逐段计数、授权状态随画像一并撤销、`errors` 非空显式 500（删除面不静默吞错）。

---

## 7. 验收

- 单元/契约：双卡合并后序列化只增断言；冲突“标记不覆写”单测；PII 双向脱敏单测（含原文零留存）；`remember/recall/forget` 必经 Gateway 断言（复用总纲守卫 2）。
- 行为：画像稳定性（同一用户多轮一致率）、冲突标记准确率、压缩保真五要素（沿用 `compaction.py` 断言）、向量故障 fail-open（断向量服务仍可答）。
- 评测（只增报告键）：`profile_stability / conflict_precision / privacy_leak(必须为0)`；进 `evals/` 摘要，不进质量门禁。
- 守卫：Capability/记忆工具不得 import Orchestrator/Planner；在线只写记忆、不做 Evolution 晋升（总纲守卫 3/7）。
