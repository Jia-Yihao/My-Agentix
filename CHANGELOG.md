# 更新日志 (Changelog)

本文件记录 My Agentix 的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本 SemVer 2.0.0](https://semver.org/lang/zh-CN/)。
版本策略详见 [`docs/版本管理策略.md`](docs/版本管理策略.md)。

变更类型：`新增 (Added)` / `变更 (Changed)` / `弃用 (Deprecated)` / `移除 (Removed)` / `修复 (Fixed)` / `安全 (Security)`

---

## [Unreleased]

- M3-4b 卡2（拆 `llm_factory` 三条反向边，4 → 1）：三条 `llm_factory` 边（`lg_builder.py` 顶层 import、`lg_supervisor.py` `_llm_review`/`_llm_revise` 函数体内延迟 import）收到既有能力注入位——装配根 `agentix/application/wiring.py` 新增第三键 `capabilities.register("get_llm_client", lambda: llm_factory.get_llm_client)`（模块形态 thunk，测试对 `llm_factory.get_llm_client` 的 monkeypatch 照常打中），消费点改经 `capabilities.resolve("get_llm_client")` 每次现取真身；`supervisor_dispatch` 入口在任何 `try:` 之前早取 `capabilities.provider("get_llm_client")`（只取 thunk 不调用，已装配态每次 dispatch 的行为逐位不变——只多一次 dict 存在性查询，不构造客户端）。**行为变化只在装配缺失面，且分两态**：完全未装配进程改前已响亮失败（`NodeNotRegistered`，抛点在 `resolve("route_query")`），本卡把首个抛点前移到 `supervisor_dispatch` 入口并换成 `CapabilityNotRegistered` + `WIRING_HINT` + 键名（失败更早、指名缺哪枚键）；「节点面已装配 + 能力面缺 `get_llm_client`」这半装配态才真是由静默变响亮，但它生产上不可自然产生（四个装配根都 import wiring，一处写三键），现实里只由测试清键构造。`orchestration → application` 反向边由 4 条收到 **1 条**（终态保留 `lg_builder.py` 的 `lg_prompts`，那条字面被 p71 钉住、归 M3-6 收口）。测试同步：p181 能力清单两键改三键、残留边分布/总数冻结改 1（两条用例改名维持诚实——逐字点名：`test_capability_names_are_exactly_the_registered_two → test_capability_names_are_exactly_the_registered_three`、`test_orchestration_residual_edges_are_exactly_four → test_orchestration_residual_edges_are_exactly_one`，实测均在代码批改名；另 p180 同口径改名 `test_application_import_statements_exactly_two → test_application_import_statements_exactly_one`，在返工批）并新增未装配 supervisor 探针；p179 supervisor 侧延迟 import 守卫由「恰 2 条 llm_factory」改为 0 条反向守卫；p180 `lg_builder` 白名单 2→1；p182 核销一般项 G-2（`_state` 改复用 phase19 实现）；G-1（三闸生产可达）**不按核销登记**——入口早取使「装配缺失」的首个抛点前移到 `supervisor_dispatch` 入口、先于体外首个 `try`，故新增用例证的是「缺键即响亮失败（端到端）」，不是「三闸生产可达」；三处闸的 `CapabilityError` 分支降级为「将来在这三个 `try` 块内新增 capability 取源」的保险，保留不删。
- M3-4b 卡1（治理闸先行，不拆边）：`agentix/orchestration/langgraph/lg_supervisor.py` 三处宽 `except Exception`（proposer 修订轮 / reviewer fail-open / 外层 `manager.run`）前各加一条 `except capabilities.CapabilityError: raise` 治理闸——注入位装配缺失属 fail-closed，不再被吞成「审阅通过 approve」或「回退草稿」；普通 LLM 故障仍照旧 fail-open。新增 `tests/test_p182_supervisor_governance_gate.py`（3 条用例逐闸钉住）。三条 `llm_factory` 反向边（`lg_builder.py:42` 顶层、`lg_supervisor.py:281`/`:293` 函数体内）本卡**未拆**，orchestration → application 残留边数仍为 4。
- M3-1（编排层去具体化第①步）：新增 `agentix/orchestration/node_registry.py` 节点注册表底座与 `tests/test_p178_node_registry.py` 编译图等价守卫；零行为变化，orchestration → application 反向边仍 27 条，默认注册表为空。

### 架构 · M3-2 编排层去具体化第②步（handler 改由 application 装配期注册，2026-10-29 落地）

- 新增 `agentix/application/wiring.py`：10 条节点 provider 的唯一注册落点（全部模块形态 thunk），由 `agentix/main.py`、`agentix/application/agents/main.py`、`tests/conftest.py` 三个装配根显式 import。
- `agentix/orchestration/langgraph/lg_supervisor.py` 删 7 条顶层 application import，`ROUTE_HANDLERS` 改为注册表背书的惰性映射（静态键域 + 覆盖表 + 每次现取 `resolve`），保 17 处 `monkeypatch.setitem` 与 `is` 同一性断言零改动。
- 反向边 orchestration → application import 语句 **27 → 20**；lg_supervisor 顶层 0，余 2 条 `get_llm_client` 归 M3-4。
- 新增 `tests/test_p179_supervisor_wiring.py`（23 条：AST 双面守卫、fail-closed、monkeypatch 兼容、编译图节点/边等值）。

### 架构 · M3-3 编排层去具体化第③步（lg_builder 节点面改走延迟解析壳，2026-10-29 落地）

- 新增 `tests/test_p180_builder_shells.py`（49 条：AST 双面守卫（application import 白名单 8 条冻结 + 壳名位零顶层 import）、9 对签名同源、转发保真（含 HITL 包裹与 `safety_guardrails` 内部调用点）、未装配态 fail-closed、干净解释器 subprocess 建图三探针 P1/P2/P3、编译图节点/边与 p178 基准等值、键≠壳名（`create_code_query` → `code_agent`）、以及 `_RegistryBackedHandlers`「非静态键可写不可读」的已声明行为守卫）。
- `agentix/orchestration/langgraph/lg_builder.py` 删 7 条 application import 语句（6 条 handler 模块位 + 1 条 router re-export 块）与 4 个零消费者的 re-export 私有别名（`_heuristic_router`、`_extract_configurable`、`_coerce_to_bool`、`AdditionalGuardrailsOutput`；reviewer 独立确认全仓无任何调用点在 import 期依赖它们），改为 9 个**延迟解析壳**（壳与被解析真身签名逐字相同，在**调用期**才向 `agentix/orchestration/node_registry.py` 解析）；机制理由是本模块在 import 期就执行 `graph = build_graph(...)`，把解析点放到建图期会炸掉一切不经装配根的直调进程。
- 反向边 orchestration → application 按 AST 全树（函数体内延迟 import 也计）实测 **20 → 13**（lg_builder 单文件面 15 → 8；lg_supervisor 2 条归 M3-4、contract 3 条与 lg_builder 的 lg_prompts 等残差归 M3-5/M3-6）——终态判据是「只剩 `lg_prompts` 一条」，由后续批次收口。
- 新增第 4 个装配根挂线位 `agentix/application/services/chat_service.py`（覆盖 `evals/`、`bench/` 这类不经 FastAPI/CLI 装配根的直调链），`agentix/application/wiring.py` 的挂线位清单由 3 条同步为 4 条。
- 闭合 M3-2 遗留的一般项 S1（挂线位缺正向守卫）与 S2（非静态键可写不可读的不对称未声明）：前者由 p180 的正向挂线守卫 + subprocess 探针 P3 钉住，后者由 docstring 明文声明 + p180 用例钉住，`agentix/orchestration/langgraph/lg_supervisor.py` 该处零行为改动。
- 未新增任何开关；与基线的行为差异只有一条：不经装配根直 import `lg_builder` 再执行图的入口，从「隐式可用」变为首个执行点 `NodeNotRegistered`（fail-closed）——该类入口经全仓核查为空，且 `node_registry` 口径本就禁止静默兜底。

### 架构 · M3-4 KG 面去具体化（`create_research_plan` 真身迁 application，lg_builder 只留延迟解析壳，2026-10-29 落地）

- 新增 `agentix/application/agents/research_handler.py`：主图最后一个真身仍住编排层的节点
  `create_research_plan` 迁入此，连同它**独占**的两个私有辅助函数
  （`_extract_legal_sources` / `_get_compiled_multi_tool_workflow`，含 `@lru_cache(maxsize=4)`
  的 P1-2b 编译缓存）。函数体逐字符照搬，唯一一处刻意不等价：消融分支
  （`ablation.tools_enabled=False`）的退回调用从同层直调 `respond_to_general_query` 改为
  `node_registry.resolve("respond_to_general_query")`，延续 M3-3 的既有不变式「这条分支同样
  经注册表解析、装配缺失时响亮抛 `NodeNotRegistered`」——直接同层调用会让它不经装配也能跑，
  等于静默改掉一个已审过的失效面。
- `agentix/orchestration/langgraph/lg_builder.py` 删该节点的三条 KG import
  （`kg_neo4j_conn.get_neo4j_graph` / `legal_retriever.LegalCypherRetriever` /
  `multi_tool.create_multi_tool_workflow`）、`constants.SCOPE_DESCRIPTION`、函数体内的
  `legal_cypher_dict` 延迟 import，以及随之失去全部消费者的 `lru_cache` / `AIMessage` /
  `get_active_tools` / `Optional`；改为第 10 个**延迟解析壳**（与其余 9 个同形态、同样不写
  返回注解）。`lg_builder` 的全树 application import 由 **8 条降到 3 条**（AST 全树口径实测；
  余下三条的归口分别是：`lg_prompts` 由 `tests/test_p71_m2_orchestration_migration.py`
  字面钉住、`llm_factory` 的消费者是 `check_hallucinations`（DEAD-CODE(P2-5) 有意不接线）、
  `router._ensure_router` 归 M3-5 的 T3）。`SCOPE_DESCRIPTION` 一条提前随本批收掉——它的
  唯一消费点就在被迁走的函数里，同一条 import 的两个消费点不可能分两批改。
- `agentix/application/wiring.py`：该键的 provider 由 `lambda: lg_builder.create_research_plan`
  改为 `lambda: research_handler.create_research_plan`（注册键、`awaits=True`、
  `takes_config=True` 一个字未改）；`lg_builder` 这个模块 import 在 wiring 里再无消费者，
  随之一并删除；上方那段「provider 指向 orchestration 侧」的注释按新事实改写。
- 口径镜像同步：`agentix/config/ablation.py` 的 `WIRED_FLAGS["tools_enabled"]` 接线点、
  `docs/项目实现手册.md` §7.6 表内那一格，均改指
  `agentix/application/agents/research_handler.py::create_research_plan`。
- 测试侧**只改打点/读源码的目标，不改判据**：`tests/test_p12_reuse.py`（12 处
  `patch.object` + 2 处 `cache_clear()` + 模块 docstring）、`tests/test_p14_streaming.py`
  的源码级转发守卫（壳里也含 `config=config`，只改判据不改目标会「绿而测不到」）、
  `tests/test_ablation_wiring.py`（`monkeypatch.setattr` 目标与两处直调真身）。
  `tests/test_p180_builder_shells.py` 的冻结基准随事实更新：`SHELL_TO_KEY` 10 项、
  `SHELL_SOURCE_MODULES` 8 个模块、application import 白名单 **8 → 3**
  （`lg_prompts` / `llm_factory` / `router`）与同名计数常量、以及两处「9 个壳」的措辞——
  守卫逻辑本体零改动，`tests/test_p178_node_registry.py`（注册键全集、节点名/边基准）与
  `tests/test_p179_supervisor_wiring.py`（lg_supervisor 恰 2 条）零改动，本批不增不减注册键、
  不改图结构。**本批刻意不新增任何用例**。
- 未新增开关、未改提示词/路由/图结构与节点名；编译图的节点名集合与 18 条边逐位不变
  （由 p178/p180 的等值守卫与 `tests/test_p24_deadcode_marking.py` 的节点绊线共同钉住）。

### 架构 · M3-5a 非节点能力注入位（`contract.py` 的 3 条 `artifact_store` 反向边改经注入位解析，2026-10-29 落地）

- 新增 `agentix/orchestration/capabilities.py`：与 `agentix/orchestration/node_registry.py`
  同层的**非节点**能力注入位（`register` / `provider` / `resolve` / `require` / `names`，
  `override` 默认关；进程级单例 `DEFAULT_CAPABILITIES` + 同名模块级转发函数）。形态照抄
  节点注册表的三条口径（fail-closed / 零参 thunk 每次现取 / 同一个判定单源），但**不登记
  `awaits` / `takes_config`**——能力面的消费者是普通调用点，形态由被调真身决定，再存一份
  就是第二个判据源。它刻意**不进 `DEFAULT_REGISTRY`**：注册成第 11 个节点键会同时破三条
  「注册表全集相等」守卫（`tests/test_p178_node_registry.py:527`、`test_p179` 的
  `test_names_exactly_the_wired_ten`、`test_p180` 的 `test_p3_chat_service_wires_ten_keys`，
  三条一律不许放宽），故两把键集互不相交。
  未注册即抛 `CapabilityNotRegistered`，提示串**复用** `node_registry.WIRING_HINT`
  （`AGENTS.md` 纪律 6 ④），本模块源码不出现那份字面量。
- `agentix/orchestration/strategies/multi_agent/contract.py` 删 3 条函数体内的
  `agentix.application.services.artifact_store` import（`offload_content` 两处、
  `get_artifact_store` 一处），改按键名经注入位解析（`capabilities.resolve(...)`）；
  入参、返回值处理、两处既有降级分支（`try_offload_face` / `resolve_evidence_ref` 的
  `except Exception`）与 `store is not None` 的短路**逐字保持**；模块上方「application
  依赖全部函数内延迟 import」的架构注记按新事实改写，不留与代码不符的注释。
- `agentix/application/wiring.py`：追加 `offload_content` / `get_artifact_store` 两键，
  provider 一律是**每次现取的模块属性 thunk**（`lambda: artifact_store.offload_content`）——
  thunk 语义由 `tests/test_p181_capability_seam.py::TestConsumerBehaviorEquivalence::test_offload_text_observes_module_attr_patch`
  钉住；`tests/test_p78_index_cascade_isolate.py` 两处 `monkeypatch.setattr(store_mod, ...)`
  是应用侧直接 import 路径（`agentix/application/services/chat_service.py:376` /
  `agentix/retrieval/domain/services/context_engine.py:204`）的降级用例，与本注入位无依赖
  关系，至多作为「模块属性 patch 惯用法同形」的旁证，**不是**前提；docstring 的落点说明
  随之补「本文件同时是非节点能力注入位的唯一写入点」。
- 反向边 orchestration → application 按 AST 全树（函数体内延迟 import 也计）实测
  **8 → 5**：余 `agentix/orchestration/langgraph/lg_builder.py` 3 条
  （`lg_prompts` / `llm_factory` / `router._ensure_router`）与
  `agentix/orchestration/langgraph/lg_supervisor.py` 2 条（`get_llm_client`），分别归
  M3-5b / M3-4b / M3-6，本批一律不动。
- 新增 `tests/test_p181_capability_seam.py`（33 条：两把键集互不相交、能力面全集相等
  （两键清单，注释指向 §8 台账 M3-4b 的扩键指针）、真身同源、fail-closed 与 thunk 语义、
  `WIRING_HINT` 单源静态守卫、AST 三面守卫（`capabilities.py` 零 `application` 零
  `langgraph`、`contract.py` 0 条、全仓恰 5 条且**分布**逐文件冻结）、消费者行为等价
  （模块属性 patch 生效 / 真外置成 `obs://` 并可取回 / 显式 store 短路 / 未装配态 +
  显式 store 仍可解析）、未装配态 subprocess 探针）。AST 取数复用
  `tests/test_p178_node_registry.py` 的 `_imported_modules`，
  不另写第二份判据。
- 未新增任何开关（`CODE_EXEC_*` 之类一律未动）；与基线的行为差异按调用点分**三种形态**
  （都只发生在「不经装配根直 import `contract`」的进程里）：
  ① `_offload_text`——由「隐式可用（函数体内延迟 import 直接命中 application）」变
  `CapabilityNotRegistered` **响亮失败**（该函数不吞异常）；
  ② `try_offload_face`——由「可用」变「返回 `None`，走既有降级分支」：分支本身没改，
  但**触发频率**变了，改前几乎不可触发，改后未装配态必然触发；
  ③ `resolve_evidence_ref`——「未装配 + 显式传入 `store`」与改前一致（`store` 短路在前、
  `capabilities.resolve` 折在 `else` 支，这条路不依赖任何装配）；「未装配 + 缺省 `store`」
  由「可用」变 `None`，仍在该函数「绝不抛，`None` = 不可解析」的既有契约内。
  装配态（FastAPI / CLI / `tests/conftest.py` / `chat_service` 四条挂线位）行为逐字不变。

### 架构 · M3-5b 拆除 `lg_builder → router` 反向边（`route_query` 注解取源改经节点注册表 + import 顺序契约钉死，2026-10-29 落地）

- `agentix/orchestration/langgraph/lg_builder.py` 删 `agentix.application.agents.router
  import _ensure_router` 这条反向边（连同随之变成死 import 的顶层 `import sys`；函数体内
  的 `import sys as _sys` 是另一处延迟 import，不受影响），注解取源块改为
  `node_registry.resolve("route_query")` 经注册表现取真身。**刻意不包 try/except、不做
  `registered()` 兜底、不回退具体 import**：未装配即 `NodeNotRegistered`（带 `WIRING_HINT`）
  原样抛在 import 期——静默兜底等于把「响亮塌缩」换成「连一次响亮失败都不留」。
- 反向边 orchestration → application（AST 全树口径，函数体内延迟 import 也计）实测
  **5 → 4**：分布为 `lg_builder.py` 3 → 2（余 `lg_prompts` / `llm_factory`）、
  `lg_supervisor.py` 仍 2 条，判据常量在 `tests/test_p181_capability_seam.py`。
- 等价性论证：`wiring` 的 provider 是 `lambda: router.route_query`，返回的就是函数对象
  本身，`get_type_hints` 按函数自己的 `__globals__` 求值 ⇒ 拿到的 8 分支 `Literal` 与改前
  逐字相同；p178 的 18 条边等值守卫仍绿，另由新增 P5 探针直接断言出边集合不塌缩。
- **import 顺序契约（本批行为变化的本体）**：langgraph 在 `add_conditional_edges` 那一刻
  读 `Literal`，取源动作只能在 import 期完成 ⇒ `lg_builder` 自此 **import 期就要求装配已
  加载**。`agentix/application/services/chat_service.py` 的挂线位整体前移到它 import
  `lg_builder` 之前（evals/bench/独立脚本这些不 import 装配根的进程的共同咽喉点）；顺序
  由 P4 探针（`sys.meta_path` 在 `lg_builder` 模块体即将执行的那一刻取样注册表）钉住——
  改前先跑红（读数 `ROUTE_QUERY_WIRED_BEFORE_LG_BUILDER: False`）、改后跑绿（`True`）。
- 新增用例 **2 条**（都在 `tests/test_p180_builder_shells.py`）：P4
  `TestWiringPrecedesBuilderImport::test_p4_registry_ready_before_lg_builder_body`
  （顺序契约）、P5 `TestCleanInterpreterImportContract::test_p5_conditional_edges_do_not_collapse`
  （无声塌缩防线；期望分支集从 p178 `EXPECTED_GRAPH_EDGES` 按源节点筛出，不在本文件抄
  第二份字面量）。同时 P1/P2 的「未装配」前提**反号**：P1 原断言「干净解释器只 import
  `lg_builder` 也建图成功且注册表为空」——该前提自本批起不再成立，剩下的正确事实是
  「未装配时 import 即失败，stderr 含 `NodeNotRegistered` 与 `WIRING_HINT`」（设计口径
  fail-closed，不是回归）；P2 守卫判据不变（调用期 fail-closed），探针改为先挂线建图、
  再清空 `DEFAULT_REGISTRY`（`_entries`，p179 `_unwire` 同法），把「未装配」从 import 期
  挪到调用期。**未删除任何既有断言来让红变绿。**
- 代价面（不经装配根的进程必须自行挂线，与 M3-3「未装配→首个执行点抛」同一漂移、只是
  再前移到 import 期）：
  ① `bench/adaptive_ab.py::_apply_window` 改前实测抛 `NodeNotRegistered: node(s) not
  registered: 'route_query' — application wiring not loaded`（该臂不 import chat_service），
  本批在它 import `lg_builder` 之前补一行自行挂线；
  ② **在干净解释器里 import `lg_supervisor`（当时其顶层 import `lg_builder`）的进程同样受
  影响**——已发现一处连带回归：
  `tests/test_p71_m2_orchestration_migration.py::TestLoadOrderSafety::test_supervisor_new_path_works_old_path_gone`
  以 subprocess 起干净解释器 import `lg_supervisor`，现改红（1 failed）。它在简报允许改动
  文件之外，executor 未越界修改，**待 organizer 裁决**（修法是一行：该探针脚本在 import
  `lg_supervisor` 前补 `import agentix.application.wiring`；该建议未采纳，实际裁决与复绿见下方「同批返工（2026-10-29）」条目）。
  ③ `lg_builder.py` 模块注释里「建图不依赖注册表 / 干净解释器 `names()` 为空」的旧断言
  自本批起与事实不符，已按新事实改写，不留与代码不符的注释。
- 计数与白名单同步：p180 `APPLICATION_IMPORT_BASENAME_ALLOWLIST` 删 `router`（3 → 2）、
  `EXPECTED_APPLICATION_IMPORT_COUNT` 3 → 2、守卫改名
  `test_application_import_statements_exactly_two`、两处过期 docstring 措辞（顶层零 import
  的模块数 7 → 8、例外放行口子描述）随事实更正；p181 `EXPECTED_RESIDUAL_EDGES`
  `langgraph/lg_builder.py` 3 → 2、`EXPECTED_RESIDUAL_TOTAL` 5 → 4、守卫改名
  `test_orchestration_residual_edges_are_exactly_four`、沿革措辞保留原句并带
  2026-10-26 / 2026-10-29 时点括注；p181 docstring 里两处会漂移的行号锚（p180:544、
  chat_service.py:376）改为函数名/位置描述锚。`EXPECTED_CAPABILITY_NAMES` 仍两键未动；
  未新增任何 Settings 开关、未新增任何宽 `except`。
- **同批返工（2026-10-29，修上面②的 p71 连带回归）**：裁决为**改 `lg_supervisor.py`、
  p71 一字不动**——`lg_builder` 的顶层 import（原 `:58`）下移到唯一消费函数
  `build_supervisor_graph()` 体内。理由：`lg_supervisor` 自己 import 期不需要
  `lg_builder`，顶层 import 它等于把 `lg_builder` 自 M3-5b 起的 import 期装配依赖
  转嫁给每一个 import supervisor 的进程（p71 `TestLoadOrderSafety` 在干净解释器里
  import `lg_supervisor` 正是这么砸红的），而编排层不许 import 装配根（p179 静态
  守卫），supervisor 无法自救，只能下移。p71
  `test_supervisor_new_path_works_old_path_gone` 一字未动地恢复绿；p179 新增一条
  静态守卫（用例总数 +1）：`lg_supervisor.py` 模块体顶层指向 `lg_builder` 的
  import 恰 0 条。代价面收窄：需自行挂线的入口从 `{lg_builder, lg_supervisor,
  chat_service, bench, 独立脚本}` 缩到 `{lg_builder, chat_service, bench, 独立
  脚本}`；调用期 fail-closed 不弱化（未装配时 `build_supervisor_graph()` 仍经
  `lg_builder` 响亮抛 `NodeNotRegistered`）。两个 `__init__.py` 里「lg_builder ↔
  lg_supervisor 模块级互引」的旧表述按「原句保留 + 时点括注」更正，两文件维持
  零 import。

### 架构 · M1 把归属表变成可执行断言（闭包矩阵守卫，纯测试与文档批，零产品代码改动）（2026-10-29 落地）

路线图 §7 M1（口径：`docs/architecture/分层架构与依赖规则.md` §7.3 与 `.qoder/rules/task-order.md` §5）：
新建 `tests/test_p177_arch_closure.py`——把 §1「终态顶层包归属」表的依赖方向列逐行照抄成机器可读允许集
（17 个顶层包；config/domain/performance 三个缺行补录带注明），闭包计算器与 p66 扫描器同源（AST，
函数体内延迟 import 与 TYPE_CHECKING 块内 import 一律计边；装配根 `__root__` 不进层矩阵），
录两条**相等棘轮**基线：直接违例边 **53** 条（每条带实质理由）与 17 包传递闭包。六条守卫分别钉住：
①实际违例边集与基线键集相等（新增即红、登记过而实际消失亦红，防白名单腐化）；②每条登记边必须有
实质理由（占位符禁词判红）；③实际闭包与闭包基线双向相等（变宽挡新债、变窄逼同步收紧）；④合成用例
自证两个方向的漂移都能被抓 + `domain` 回归钉（`domain→config` 必判违例——§2.3「零外部依赖」优先于
§4「配置全层可读」；`tools→config` 必判放行——普适维照常生效）；⑤跨包相对导入实测为零（把 §7.1 的
前提从口径升级为实测）；⑥解析 `docs/architecture/分层架构与依赖规则.md` §7.1 实测快照块三行计数与
计算器实测核对（**fail-closed**：抽不到即红，判据与正文脱钩必须可见）。4 条边由主会话裁决**保留为
基线例外、待拆除/待收敛、不排期**（属裁决不是修复）：`agent→application`、`interfaces→orchestration`、
`policies→infrastructure`、`retrieval→infrastructure`。M1 批次**不产出任何性能/成本指标**，产出的是
「改一处不会悄悄炸另一处」这个性质（路线图 §7.1 原话）——闭包断言连间接越界一起暴露，白名单没法
悄悄变宽。配套文档同步：§7.1 两处旧数字（9 对双向边 / 12 包同环）标历史值并插入与其同源核对的实测
快照块（现行实测：6 对 / 8 包 / 零出边 3 包）、表内两行陈旧计数改 `→ **0**（M2 已拆）`、§5 增「包级
例外真源是基线常量而非手抄清单」指针。沿革与处置见台账 §8 的 M1-1…M1-3 行与 §8.2 表 P。

### 架构 · M2 拆三条最便宜的架构反向依赖边（纯结构重构，判跳/取值语义零变化；.env 文件基准有一处已登记口径差异）（2026-10-28 落地，2026-10-29 复验与登记）

路线图 §7 M2（口径：`docs/architecture/分层架构与依赖规则.md` §7.4）点名的三条边全部拆除：
`grep --include="*.py" "agentix\.application" agentix/tools/`、同式判 `agentix/runtime/`、
以及 `grep "agentix\.retrieval" agentix/observability/` 现均为空。**三条都不改术语与接口字段、
不改判跳与取值语义，只改定义规范位与 import 源**；**唯一已登记的口径差异在边1 的 `.env` 文件基准**
——工作目录 ≠ 仓库根时两个方向都有差异（stray CWD `.env` 会被读到；仓库根 `.env` 的值会丢、回落默认），
指向台账 M2-1 行偏离列①②。M2 等量验收：后置 `5847 passed / 40 skipped / 6 deselected / 91 warnings`
（主会话独立复跑，退出码 0），前置同为 `5847 / 40 / 6 / 91`；**两次读数均在含 T3/评测批次在飞改动
（`evals/**` 19 处、其中 `evals/golden_source.py` 未跟踪）的同一工作树态取得**，等量结论只在
「该批次两侧恒定」这一限定下归因给 M2（详见台账 M2-5 行）。

- **[边1 `observability → retrieval`]** `LOG_JSON_ENABLED` / `LOG_LEVEL` / `TRAJECTORY_DIR`
  三键真源从 `agentix/retrieval/settings.py` 上提到 `agentix/config/settings.py`（通用可观测性
  配置不属检索域；检索侧删行不留第二源，经 `_RetrievalSettingsCompat.__getattr__` 委托仍可读）。
  两个 `field_validator(mode="before")` **逐位复刻旧 env 值语义**（旧 `_bool_env` 字符串只认
  `1/true/yes`，`on`/`y`/`t` 判 False；`LOG_LEVEL` 无条件 `.upper()`；`TRAJECTORY_DIR` 默认路径与
  旧检索域 `_BASE_DIR` 上溯三级同源）。该复刻限于 env **值**语义；**`.env` 文件基准随真源上提
  变为工作目录口径**：旧实现在仓库根绝对路径 `load_dotenv` 之后导入期现算，新实现由统一
  Settings 按 `env_file=".env"`（CWD 相对）解析，故工作目录 ≠ 仓库根时存在两个方向的差异
  （stray CWD `.env` 会被读到；仓库根 `.env` 的值会丢回落默认），已按规划 §8 台账 M2-1 行
  登记偏离（彻底等价须改 `model_config` 的 `env_file` 为仓库根绝对路径，影响 200+ 个键、
  越出本批范围）。`logging_config.py` / `trajectory.py` 的函数内延迟 import 改指 config。配置键按纪律 4 五处同步；`.env.example` 里
  `TRAJECTORY_DIR` 取**注释行**形态（写空串会让 `Path("")` 把落盘根变成 CWD，不等价），故
  `selfcheck.py` 的 `DEFAULT_IGNORED_MISSING` 只新增 `LOG_JSON_ENABLED`。同批删除 `test_p74`
  两条僵尸豁免（相等棘轮要求「边消失则豁免消失」，属收紧，不放宽任何守卫）。
- **[边2 `tools → application`]** 纯作用域类型 `RequestScope` / `_scope_var` / `get_current_scope`
  下沉新建 `agentix/domain/request_scope.py`（纯 stdlib，与 `domain/tracing.py` 的 ContextVar
  同构）；`application/services/request_scope.py` 改为**对象同一**的再导出，写入实现与模块导入期
  `set_remaining_provider(...)` 注册留在 application；`tools/gateway/executor.py` 的延迟 import
  改指 domain。另一半：4 个 KG 工具参数模型（`cypher_query` / `predefined_cypher` /
  `microsoft_graphrag_query` / `text2sql_query`）规范位落 `agentix/tools/kg_arg_schemas.py`，
  `kg_tools_list.py` 保留为再导出 shim。**落位裁决：tools 层而非 domain**——
  `tests/test_p17_domain_layer.py` 对 `agentix/domain/**` 零豁免禁第三方依赖（含 pydantic），
  参数模型进 domain 必红该守卫；且参数 schema 是工具契约数据，与 `ToolSpec` 同源同层，
  `registry_init.py` 本地早有同类参数模型（`create_skill_draft`）先例，不为拆一条边把它挪进不该去的层。
- **[边3 `runtime → application`]** `DEFAULT_WARN_RATIO` / `BudgetVerdict` / `TokenBudget`
  下沉新建 `agentix/domain/runtime/token_budget.py`（纯 stdlib；`to_dict()` 键序不变、类名未改），
  `application/services/token_budget.py` 再导出，`BudgetLedger` 与 ContextVar 台账读写留在
  application；`runtime/control_plane.py` 的 TYPE_CHECKING 引用改指 domain（保持惰性形态）。
  同批把 `test_p73:164` 断言指向 domain 落位并新增「application 路径不得进 TYPE_CHECKING」
  反向断言（判据强度只增不减）。
- **[窗口内零新增用例]** M2 等量验收窗口（`task-order.md` §3 第 2 条）内未加任何测试：
  `tests/` 被改的三个文件（p73 / p74 / p41）HEAD/工作树 `def test_` 计数全等
  （14=14、22=22、40=40），`git diff HEAD -- tests/` 中增删 `def test_` 行数为 0。

### 路由层 · T30 三层路由收敛为单层（flag 门控，默认关）（2026-10-28）

T29 实测显示一次 kb-query 请求要做**两次**独立的模型路由：L1 意图分类只产意图，KB 检索通道
（`route` / `tools`）还要在 KB 子图的 `router` 节点（A3）再判一次。这一轮立一条**默认关闭**的
单层通道：L1 在同一次调用里一并产出检索通道参数，A3 直接消费。**本条只写机制**——跳数差值的
前后取证属 T30-3（本批未跑 bench），不在此声称任何实测降幅。

- **[新增开关] `ROUTING_SINGLE_LAYER_ENABLED`（默认 `false`）**：flag-off 与基线**逐项等价**
  （纪律 1）——路由提示词（`_router_prompt_text` 不拼 `SINGLE_LAYER_ROUTE_BLOCK`）、结构化输出
  schema（`_router_class` 返回与现网同一批四个基线类，基类 `Router` 的 `Literal` 一字未动）、
  `sanitized_kwargs`（`single_layer_on` 为假时连 `kb_route`/`kb_tools` 两个键都不加）三处逐位等价；
  三个判定只有 `_exec_flags` 一个源，缓存换代令牌复用同一组函数（纪律 6 ④）。配置键按纪律 4
  五处同步（`settings.py`、`selfcheck.py`、`.env.example`、`docs/architecture/配置管理.md` §5、
  `tests/test_p41_doc_consistency.py` 手写清单）。
- **[单层机制，flag-on]** L1（`agentix/application/agents/router.py::analyze_and_route_query`）在
  `type` 为 `kb-query` 时额外产出 `kb_route`（`local`/`external`/`hybrid`）与 `kb_tools`（元素仅
  `postgres`/`milvus`）；`general_handler.create_kb_query` 经 KB 子图输入面以 `route` / `kb_tools`
  两键转发；子图 `router` 节点消费转发参数时**0 次模型调用** ⇒ 单次 kb-query 请求少一次模型调用。
  兜底出口（异常 / 白名单外 / fast-path / 缓存命中）**一律不产参数**，此时 A3 照常自判
  （跳数回到基线，**不伪造默认通道**，不算错也不省）。
- **[三段安全钳同源]** A3 的三段钳制——`allow_external_search` 关时把 `external`/`hybrid` 降级
  `local`（`KB_ENABLE_EXTERNAL_SEARCH` 是安全边界，不因决策者是谁而放宽）、tools 与
  `{milvus, postgres}` 取交集、`route != external` 且 tools 为空时回落 postgres 优先默认——对
  **转发参数与子图自判两个来源共用同一段代码**，不按来源各写一份（纪律 6 ④）。
- **[输入面扩键（必要落点）]** `KBInputState` 新增两个 **`NotRequired`** 键（`route` / `kb_tools`）：
  框架只把 `input=` schema 认识的键写进状态通道——不声明则转发键被输入面**静默丢弃**（不报错）
  ⇒ 单层化「永不生效」。不传两键时与改动前行为相同，flag-off 的等价性是结构性的而非靠约定。
- **[已登记能力缺口（规划 §8.2 表 M，实名）]**：① `agentix/application/agents/routing_cache.py` 的
  `_CACHED_FIELDS` 仍只有 `type/logic/decision/confidence` 四个基线字段 ⇒ **缓存命中不携带单层
  参数**，同时开 `ROUTING_CACHE_ENABLED` 时该次请求由 A3 自行判路由（跳数回到基线）；扩缓存值面
  会改 T8 的缓存语义，本批刻意不做。② `general_handler._retry_kb_query_with_rewrite`（Agentic RAG
  空检索重写重试）**未接转发**，该路径 flag-on 仍有子图模型调用（G-1）。③ 模型回传越域 `kb_route`
  值时在 `router_cls.model_validate` 抛 `ValidationError` 使节点失败——**刻意不回退**，与 `type`
  字段越域的既有口径一致（fail-closed，G-2）。②③ 已同步登记为代码注释。
- **[回滚]** 置回 `ROUTING_SINGLE_LAYER_ENABLED=false`：提示词退回基线段、schema 退回基线四类、
  转发键不加、A3 恢复自判；无残留任务、无新增指标序列。

### 流式审批 · T23 流内审批帧下发（默认关）（2026-10-28）

流式链路此前**没有审批车道**：`astream_events` 里遇到需要审批的工具，`preflight` 抛
`ReviewRequired` 就直接变成一次失败，用户没有「在这条流上点批准」的入口，也不会走到
`STREAM_IDLE_TIMEOUT` 的判定。这一轮补上**默认关闭**的流内审批：需要审批时下发 `approval`
帧，裁决落在既有 ops 审批 API 上，裁决后流继续跑到治理终局。

- **[新增开关] `STREAM_HITL_ENABLED`（默认 `false`）+ `STREAM_HITL_HEARTBEAT_S`（默认 `15`）**：
  flag-off **逐字节等价**（纪律 1）——`build_stream_hitl()` 返回 `None` 且**不触碰** `config`，
  `chat_service` 仍走原来那句 `asyncio.wait_for(..., STREAM_IDLE_TIMEOUT)`，帧序列里不会出现
  `approval` / `heartbeat`，`configurable` 不新增 `hitl_in_loop` / `stream_hitl` 键。
  flag-on 的前置链：`HITL_IN_LOOP_ENABLED` 且 `HUMAN_REVIEW_ENABLED` 为真，否则告警一次并退回
  flag-off 行为（不另造开关语义）。
- **[为什么不挂起图]** 一个 SSE 响应只能下发一次：`langgraph.types.interrupt` 的恢复需要
  **第二个**请求来 `Command(resume=)`，流式面拿不到。因此等待发生在 `execute_task` **节点内部**
  （`agentix/application/services/stream_hitl.py`，回调允许 async），由 `chat_service` 的取事件
  循环把帧写进同一条流；`lg_builder._wrap_execute_task_in_loop` 按 `configurable["stream_hitl"]`
  选「节点内等待」而不是 `interrupt()`。
- **[帧契约]** `{"type":"approval","metadata":{status,request_id,sidecar_request_id,tool,
  risk_level,reason,decide_url?,decision?}}`：`status ∈ {"pending","decided"}`，
  `decision ∈ {"approve","reject"}` 仅 `decided` 帧带；`decide_url` 由服务端给出
  （`API_V1_PREFIX` 是前缀唯一真源），桥里查不到映射时**不带**该字段，不伪造 id。
  `{"type":"heartbeat","metadata":{status:"waiting_approval",request_id,tool,waited_s,
  heartbeat_s}}` 只用于占位（带 `waited_s` 是为了排障时能区分「心跳在走」与「心跳停了」）。
- **[时限口径]** 新增四层尺子分家（详见 `docs/architecture/运行时护栏.md` §16）：
  `STREAM_IDLE_TIMEOUT` 判的是「图有没有产出事件」，审批等待期间由心跳占位 ⇒ **不算空闲**；
  `AGENT_TIMEOUT` 仍是单次调用墙钟，**包住**审批等待（粒度 = 一个心跳周期）；T19 的
  `HITL_APPROVAL_DEADLINE_S` 数的是 pending 存活时长，先到者生效、归因不同；节点内另有
  `wait_cap_s = AGENT_TIMEOUT` 兜底，到期交出 **fail-closed `reject`** ⇒ 节点侧
  `gate.reject(by="in_loop_resume", note="in-loop reject")` **闭合**那条 pending（终态
  `REJECTED`），归因是「流内等待上限触发的拒绝」——这条**不**计入 `stats()["auto_reject_timeout"]`、
  也**不**打超时指标，与 T19 的 `decided_by=system:deadline` 是两个不同来源；库行不由流内路径
  写（仍是 `pending`）：`HITL_APPROVAL_DEADLINE_S>0` 时由到期扫描继续处理——扫描在**持闸门记录的
  进程**里按既有裁决补偿闭合（`in_loop_resume`，不重复计数），在**另一个 worker** 里则重建出未决
  闸门记录后按 T19 口径裁决（库行落 `system:deadline` / `auto_reject_timeout`），跨 worker 时
  同一条 pending 因此**两个面两个归因**；`=0`（默认）时扫描不跑、库行一直停在 `pending`。
  「把 `STREAM_IDLE_TIMEOUT` 调大来等审批」是错的解法。
- **[裁决单一真源]** `task_execution` 的批准 / 拒绝只读闸门账本（`ReviewGate.get().decision`），
  `approval` 帧里的 `decision` 是同一值的投影（纪律 6 ④）。桥先落批准、节点随后
  `gate.approve()` 返回 `False`（已存在 / 已消费）时按「闸门已是 `APPROVED`」继续重试，不判成
  「批准未落地」。
- **[拥塞]** `stream_gate._CRITICAL_TYPES` 收 `approval`（丢帧会把运行困在等待里），**不收**
  `heartbeat`（丢一次只是少一次占位，下个周期补上）。
- **[前端]** **未交付**：`web/` 零改动（`grep -rn approval web/src` 与
  `grep -rn heartbeat web/src` 均零命中）；`chatStream.ts` 的 `ChatStreamChunk.type` 仍是
  六值、`dispatchChunk` 对这两型走 `default` 直接丢弃。这两帧目前只在后端产出，
  浏览器端渲染另立排期。
- **[跨 worker]** 裁决真源是 T18 的库行（本地闸门是快路径）：等流侧判定顺序为
  「本地闸门优先、未决时按 `sidecar_request_id` 回捞库」——`--workers 2` 下裁决落在
  另一个 worker 也能被这条流看见；回捞粒度即既有 `DECISION_POLL_S`（不新增配置键），
  读库失败 fail-open（当作未命中、本地闸门照旧轮询 + warning 留痕，不抛进流、不提前收口）。
  守卫：`tests/test_p174_stream_hitl.py::TestCrossWorkerDecisionLookup`。
- **[与 T18 落库的关系]** 流内审批**复用** executor 拦截钩子那条链（`open_from_gate`）：
  需审批的工具在执行点被拦下 ⇒ 钩子登记 sidecar 请求 ⇒ `HITL_PENDING_PERSIST_ENABLED=true`
  时照旧写 `hitl_pending_approvals`，重启与跨实例裁决仍吃这一行。本卡没有新增库面。
- **[已知限制]** ① 流内审批的 `edit`（改参数后重试）复用不到「改后的参数」——重试走的是
  节点里原始 `payload`；② 跨实例裁决靠**回捞库**看见，延迟上界是一个 `DECISION_POLL_S`
  轮询窗口，且**要求 `HITL_PENDING_PERSIST_ENABLED=true`**（关着时库面装配判据返回 `None`，
  流侧只有本地闸门这一个来源）；③ 钩子本身有 **2 秒**超时（`executor._REVIEW_HOOK_TIMEOUT_S`），
  落库慢于该超时时节点侧仍按 `review_required` 继续等，此时 `approval` 帧可能先不带
  `sidecar_request_id`（下一拍查到映射后补上），不伪造 id。
- **[回滚]** 置回 `STREAM_HITL_ENABLED=false`：立即退回「遇到需审批即失败」的基线，
  `stream_hitl.py` 不再被 `chat_service` 调用，无残留任务、无指标新增序列。

### 路由层 · T8 路由结果跨请求缓存 + fast-path 扩面（默认关）（2026-10-28）

意图分类每来一个问题都要构造一次 `router` 角色客户端并发一次分类请求；同一问句在缓存 TTL 内
被反复问（前端重试、同一 FAQ 被多人问、评测集回放）时这部分成本是纯重复。这一轮加一条**默认
关闭**的进程内缓存，并把确定性 fast-path 从「寒暄 / 单条法条」扩到五类封闭 reason。

- **[新增开关] `ROUTING_CACHE_ENABLED`（默认 `false`）**：flag-off **逐位等价**（纪律 1）——缓存
  模块**不 import**（`sys.modules` 里查不到）、`routing_cache_total` 三个取值**零增量**、路由节点
  日志与提示词**逐字节相同**；命中才省掉**一整次模型调用**（零客户端构造、零请求）。
- **[可观测]** 命中计数 `routing_cache_total{result}`（`result` 封闭 `hit` / `miss` / `error`，已进
  p100 标签白名单），并在 trajectory 落一步 `route.cache_hit`（`cached=True` + 有界 `route_type`）
  ——轨迹因此能证明「这次真的没调模型」，不必只信指标。
- **[数据口径]** 键 = `sha256(归一化问句)` + 换代令牌（8 位十六进制）；值**只存** `type`（封闭枚举）/
  `logic` / `decision`（模型生成的短文本）/ `confidence`（数值），**不存问答原文、不存 `reasoning`**
  （后者是模型叙述文本，可能整段复述问句，且生产侧没有消费点）。键只存归一化问句的 sha256，
  不含问句原文；命中时用**当前这次请求的原文**回填 `question`。
- **[失效]** 换代 = router 提示词**实际生效文本** / 实际使用的 Router 类 / `LLM_MODEL` /
  `router_enabled` 消融开关任一变化（四项复用既有判定的唯一来源，不另立快照——纪律 6 ④），
  另加 TTL 300 秒到期与进程重启；语料换代**不**失效（Q1 未裁决时的保守口径）。
- **[边界·只服务单轮请求]** 查、写两处**同一判据**：仅当 `state.messages` 恰好 1 条时才查/写。
  键只覆盖末句，而分类器输入是 `[system] + state.messages`（含窗口历史）——两段历史不同、末句
  相同的请求共用一条结论会把上一轮的 `logic` 拼进这一轮的 system prompt；只加查侧判据不够，
  多轮请求照样会写脏缓存让后面的单轮请求命中它。
- **[fast-path 扩面]** 确定性规则从「寒暄 / 单条法条直取」扩到 **5 类封闭 reason**：`chit_chat`
  （寒暄/问候，以及 `谢谢` / `收到` / `好的` / `你是谁` 一类短句——它先于 capability / identity / ack 分支求值）、
  `statute_direct`（短句单条数据条目直取，无推理词）、`capability_meta`（助理能力："你有哪些功能"）、
  `identity_meta`（助理身份："你是AI吗"）、`ack_meta`（收尾确认与致谢："明白了 / 谢谢你的解答"）。
  新增的三类元问题**一律只落 `general-query`**，`statute_direct` 仍落 `graphrag-query`；且每条都要求
  **整句**落在封闭句式内（`fullmatch`），"元问题 + 实质内容"的混合句与 >60 字输入一律落回 LLM；
  reason 词表由 `router.FASTPATH_REASONS` 单源给出。
- **[回滚]** 置回 `ROUTING_CACHE_ENABLED=false`：立刻退回「每个问句都走分类器」的基线，
  缓存模块不再加载；无残留任务、无指标新增序列。

### 前端 · T14 消息列表虚拟滚动（默认关，未接线）（2026-10-28）

长会话的消息列表一直是全量渲染（超过 200 条时只有「折叠中段」这一种减负）。这一轮加一条**默认关闭**的虚拟滚动通道，并把它拆成「纯函数算窗口 + 组件只渲染」两段，让可判定性落在 vitest 上而不是浏览器手感上。

- **[新增 prop] `ChatMessageList` 的 `virtual`（默认 `false`）**：为 `false` 时消息循环仍以 `displayMessages` 的**同一引用**为数据源、折叠条下标恒等于 `FOLD_HEAD_KEEP`、两个高度占位是空 `v-for`（零 DOM 节点，避开 `v-if` 留下的 `<!--v-if-->` 注释节点），监听器在关闭态首调即短路 ⇒ 渲染结果与基线逐字节一致（纪律 1 的前端对应物）。**未接入任何生产装配方**。
- **[窗口计算]** 抽成纯函数 `computeVirtualWindow({total, scrollTop, viewportHeight, rowHeight, overscan})` → `{start, end, topPad, bottomPad}`（`web/src/modules/knowledge/composables/useVirtualWindow.ts`）；`topPad + 渲染条数 × 行高 + bottomPad` 恒等于 `total × 行高`。非法参数（负数 / `NaN` / `Infinity` / 行高 0）一律收敛到安全值并返回空窗，不抛异常。
- **[诚实边界]** 按固定估算行高（72px）+ `overscan=4` 开窗，滚动中**不做逐条实测修正**：可变高度消息（思考区 / 工具卡 / 引用 / 证据展开）会让滚动条长度与真实内容不符，快滚时窗口边缘可能露白。真实布局在 happy-dom 下不可得 ⇒ **未验证真机滚动平滑度**，本轮也不声称验证过。窗口外节点的 `details` 展开态会随节点销毁而丢失，这是虚拟化的固有语义，未处理。
- **[新增守卫] `useVirtualWindow.test.ts`（6 条）+ `ChatMessageList.virtual.test.ts`（4 条）**：纯函数的窗口边界与守恒式、`total=0`/非法参数、默认关渲染全量条数（若默认值被改成 `true` 必然变红）、虚拟模式只渲染窗口子集且顶/底占位存在、折叠条随窗口滚进滚出。既有 `ChatMessageList.test.ts` / `.feedback.test.ts` **逐字节未改**。
- **[文档同步]** 本文件。`README.md` §11 测试真源数需由收口批跑全量实测后 re-cite（本轮前端新增 10 条只在前端套件内验证）。CI 是否把前端测试改成阻断取决于未决的 Q3，**本轮未动 `.github/**`**；`.env.example` 的 `VITE_API_BASE_URL` 条目由同一批另行补入。

### 队列 PEL 认领 · T26 消费 worker 接线与优雅停机收口（2026-10-28）

`agentix/application/services/task_queue.py` 一直是「能入队、没人消费」：`TaskQueue.start_worker()` 全仓**没有生产调用点**，而 consumer group 的语义是「读走即进 PEL，处理完才 `XACK`」——任何崩在 `xreadgroup` 与 `xack` 之间的消费者都会把那条消息永久留在 PEL 里（`>` 通道不再派它），任务从此静默卡死。这一轮同时补上**消费**与**认领**，并把 worker 的收口接进 `_shutdown`。

- **[新增开关] `TASK_QUEUE_WORKER_ENABLED`（默认 `false`）**：flag-off **逐位等价**（纪律 1）——`main._start_task_queue_worker()` 第一道判定直接返回，**不建后台任务、不建 Redis 连接、不打一行日志、不 import 队列模块**；`_shutdown` 无任务可取消，关停链与基线逐字相同。flag-on 才惰性 import（含 `task_handlers` 的 `@queue.register` 注册——不先注册就起消费，每条消息只会走「No handler for task type」并被 ack 成 FAILED，比不启动更误导）并 `create_task(TaskQueue.start_worker())`；构造/启动失败 fail-open：ERROR 留痕（带异常类型）后照常启动（纪律 2）。
- **[PEL 认领]** 消费循环每 `_CLAIM_INTERVAL_S=30s` 最多一轮 `XAUTOCLAIM`（`time.monotonic()` 排程，启动即做一轮以接手上一个进程留下的 PEL；认领失败时**先推进计时再重试**，不把一次 Redis 抖动放大成每轮重试）；只认 idle ≥ `_CLAIM_IDLE_MS=60s` 的消息（阈值不能太小——handler 非幂等，`file_import` 会打外部 ingest 服务，抢走"正在处理还没 ack"的消息等于造重复执行）；单轮最多 `_CLAIM_MAX_PER_ROUND=16` 条，认领不完的下一轮继续。**有界口径** = 单次运行内次数 ≤ `⌈运行时长/30⌉`、条数 ≤ 次数×16；**刻意不设**跨进程生命周期的累计次数上限（用尽之后 PEL 又永久无人接手）。三个参数是**模块常量不进配置面**（不进纪律 4 的五处同步）。认领到的消息交给**同一份** `_process_message`（ack / 重试 / dead letter / 甩负荷四条分支不复制，纪律 6 ④）；返回值形状归一化只有一处 `_normalize_xautoclaim_response`——形状由**服务端** Redis 版本决定（6.2 两段 / 7.x 三段，redis-py 只解析第二段后原样透传），第三段丢弃、值为 `None` 的已删除条目剔除。
- **[可观测]** 新指标 `task_queue_claimed_total{result}`（`result` 封闭 `ok`/`empty`/`error`，已登记 p100 白名单，`_NoopMetric` 兜底表同步一行）；认领失败 `logger.warning` 带异常类型与原因并记 `result="error"`，**异常不上抛**——一次认领失败不许打死消费循环（下一轮继续）。
- **[停机口径]** `_shutdown` 在 drain **之后**、其它清理**之前**收口：`stop_worker()`（只翻 `_running`）→ `cancel()` → `suppress(asyncio.CancelledError)` 后 `await` → 句柄归 `None`。顺序理由：drain 管「已经在跑的图执行」，worker 管「还会接新活」，反了会在 drain 期间继续接新任务。**`RUN_DRAIN_TIMEOUT_S` 与 worker 收口互不占用**：`stop_worker()` 之后那次 `xreadgroup(block=5000)` 的自然到期等待不进 drain 预算，紧随的 `cancel()` 直接打断该等待，故 worker 收口没有第二个上限。`start_worker` 的 `CancelledError` 分支由 `break` 改为 `raise`：吞掉取消时关停只看得到「正常返回」，`Task.cancelled()` 恒假，测试与排障都失去判据。
- **[版本依据]** `XAUTOCLAIM` 需 Redis ≥ 6.2；本仓 compose 用 `redis/redis-stack-server:7.4.0-v2`（`docker-compose.yml` 的 `redis:` 服务），满足。**本轮未改 compose**，故不涉及 `scripts/gen_staging_matrix.py --check`（纪律 7）。
- **[新增守卫] `tests/test_p171_queue_claim.py`（20 条）**：2 元组/3 元组两种 `XAUTOCLAIM` 形状归一化（含 `None` 值条目剔除、意外形状降级留痕）、被认领消息走**同一份** `_process_message`、单轮不超过上限、无消息计 `empty`、认领异常 ⇒ warning 带异常类型与原因 + `result="error"` 且**不上抛**、连续失败后消费循环仍继续读消息、三个模块常量的取值、指标封闭枚举与 `_NoopMetric` 兜底行、flag-off（返回 False / 句柄 None / `get_task_queue` 未被调用 / 零日志）、flag-on 起任务且 `_shutdown` 后 `task.cancelled()` 为真且两个句柄归 None、构造失败 fail-open、以及三条静态单一源守卫（`start_worker` 不读 flag / 认领不复制处理逻辑 / `_startup` 起、`_shutdown` 收）。**不连真 Redis、不 sleep 长时间**（Redis 用假客户端，循环节奏由临时压到毫秒级的 `_CLAIM_INTERVAL_S` 驱动）。
- **[回滚]** 关 `TASK_QUEUE_WORKER_ENABLED`：立刻退回"无人消费"的基线（PEL 也就不再被认领——这正是原状），无残留任务。注意 worker 一旦真跑起来，重复消费的语义边界仍受 handler 幂等性约束（本卡不动 handler）。
- **[文档同步]** 本文件；`docs/architecture/配置管理.md` §5 新增该键一行（含三个模块常量的取值与「有界」口径）；`docs/项目实现手册.md` §8.8 新增 T26 段、§7.2 的 LG-3-6 drain 段补一句「worker 收口不占 drain 预算」。`README.md` §11 测试真源数需由收口批跑全量实测后 re-cite —— 本轮只跑了定向守卫文件，**不在此处填数字**。

### 断点续跑 · T22 checkpointer 降级后的升级重试与两条语义分家（2026-10-28）

《审批与中断恢复优化规划》T22 的**剩余**三条验收（①「待恢复线程由 checkpointer 派生」已由 LG-2-1 落地，本轮只做只读核实，未重做）。此前 Redis 不可用降级 `MemorySaver` 后单例是**粘的**——`init_checkpointer()` 第一行就返回既有单例，同一进程内永不重试；降级与"有没有从降级里回来"也没有区分得开的观测面。

- **[新增开关] `CHECKPOINTER_RETRY_INTERVAL_S`（默认 `0` = 关）/ `CHECKPOINTER_RETRY_MAX_ATTEMPTS`（默认 `3`）**：flag-off **逐位等价**（纪律 1）——`main._start_checkpointer_upgrade()` 第一道判定直接返回，**不建后台任务、不打日志、不 import checkpointer 模块**；`init_checkpointer` 的降级结果与改动前逐字一致。`>0` 才起一条一次性后台循环（`create_task`，`_shutdown` 取消），升级成功一次即退出。节奏是**定间隔**、**不做指数退避**（重试目标是同进程的一个 Redis 连接，退避换不来成功率，只多一套要记的口径）；次数上限防重试风暴，用尽后打一条含「已尝试次数 + 最后一次失败原因」的 WARNING 并复位标记（避免日志刷屏）。
- **[可重试的判定只有一个源]** 三条降级原因里**只有「Redis 连接 / `asetup()` 失败」可重试**：显式 `CHECKPOINTER_TYPE=memory` 是人配的不是故障、「`langgraph-checkpoint-redis` 未安装」运行期不会变。标记是 `checkpointer._upgrade_retryable`（只在 Redis 失败分支置真），升级入口 `retry_upgrade_checkpointer()` 与 `main` 的重试循环都只读 `is_upgrade_retryable()`，**不按配置键或降级原因字符串重判**（纪律 6 ④）。建连 + db0 不变式校验 + `asetup()` 抽成 `_connect_redis_saver`，启动初始化与升级重试共用同一段；图装配抽成 `main._wire_graph_checkpointer(ckpt)`，启动与重试共用同一段（不写第二份 `build_graph` / `build_supervisor_graph`）。
- **[可观测]** 新指标 `checkpointer_upgrade_total{result}`（`result` 封闭 `ok`/`fail`，已登记 p100 白名单）：`degraded_total` 记"进了降级态"，这条记"有没有从降级态回来"。**「升级成功」的判定包含图重编译成功**：建连 + `asetup()` 成功 ⇒ 经唯一装配点 `main._wire_graph_checkpointer` 重编两张图（先在局部变量里编好、全部成功后才一起落赋值 ⇒ 要么都换、要么都不换）⇒ **重编译成功之后**才提交 `_saver` / `_degraded_reason=None` / `_upgrade_retryable=False` / INFO + `result="ok"`；失败 ⇒ `_saver` 保持原值（不提交新 saver）、`_degraded_reason` 刷新为最后一次异常（装配失败时写明「Redis 已就绪 + 图重编译失败」两条事实，下一轮与"次数用尽"告警都读得到）、WARNING + `result="fail"`、标记保持真让下一轮再来。反过来（先换 saver 再装配）在装配抛错时会留下「自称已恢复、服务图仍挂旧 saver」的中间态——`/ops/active-threads` 的线程 id 来自新 saver、「未完成」判据来自旧图 ⇒ 静默返回空列表，而指标已记 ok。
- **[语义分家]** 「同 thread 重发起」（业务再发一次请求，`thread_id = session_id` **口径不变**，图从 START 重跑）与「真断点续跑」（`ainvoke(None)`，只存在于 `/ops/threads/resume`、`/ops/threads/{tid}/retry-task`、启动恢复、进环 HITL 四处）在**谁触发 / 从哪开始 / 谁判可不可续 / 失败时用户看到什么 / 幂等承诺**五维上成文对照，落 `docs/项目实现手册.md` §7.2。口径要点：续跑 404 的准确含义是「当前 checkpointer 里没有该 thread 的快照」，**不**等于"业务会话不存在"。
- **[新增守卫] `tests/test_p170_checkpointer_upgrade.py`（16 条）**：字段默认值、间隔 0 不建任务（即使降级可重试）、`init_checkpointer` 降级行为不变、显式 memory / 包未安装都不可重试、fail-closed 抛出后没有可升级的降级态（标记为假、起停函数起不了任务）、升级成功四面到位（saver 替换 / 原因清空 / 装配点被调用一次 / 指标 ok+1 / 成功即退出）、**装配抛错 ⇒ 不算成功**（`ok` 不增、`fail` 每次 +1、`_saver` 仍是降级单例、原因写明两条事实、用尽后复位）、连续失败恰好达上限并复位标记（`fail` 计数等于尝试次数、最后原因被记录、只打一条 exhausted 告警）、`MAX_ATTEMPTS=0` 显式告警、起停句柄可取消、`_shutdown` 取消任务、以及三条静态单一源守卫（启动经唯一装配点且起停调用**在 AST 上直接挂在函数体**而不是字符串下标 / 重试循环不重判配置 / 三件写入都在 checkpointer 模块内）。**不连真 Redis、只用毫秒级真实 sleep（不注入时钟，理由已述）**（毫秒级间隔 + 既有 `_try_import_async_redis_saver` / `redis.asyncio.Redis` 桩）。
- **[回滚]** 两个键置回默认（`0` / `3`）：立刻退回"降级即粘死"的基线，无残留任务、无指标新增序列。
- **[文档同步]** 本条 CHANGELOG；`docs/项目实现手册.md` §7.2 升级段与 `docs/architecture/配置管理.md` §5 的 `CHECKPOINTER_RETRY_INTERVAL_S` / `_MAX_ATTEMPTS` 行已按「升级成功含图重编译成功、重编译失败保持降级态并继续重试」改写。**待收口批补**：`README.md` §11 的测试真源数与口径链需由收口批跑一次全量实测后 re-cite 并追加 T22 一段——本轮只跑了定向守卫文件，**不在此处填任何测试数字**（未实测的数字比空着更糟）。

### 运行态落库 · T21 agent_run_states（2026-10-28）

《审批与中断恢复优化规划》T21：`GET /agents/runs` 的权威面一直只是进程内存（有界 FIFO 500），重启后「跑过什么」整体清零，运维只能翻日志。这一轮把控制平面运行态落 Postgres：默认关，开了才有库面，且**权威面仍是内存**——库只负责「重启后还查得到」。

- **[新增开关] `RUN_STATE_PERSIST_ENABLED`**（默认 `false`）：flag-off **逐位等价**（纪律 1）——`control_plane.resolve_run_state_store()` 恒 `None` ⇒ 连 `run_state_store` 模块都不 import、不触 sqlalchemy、不建池、`agent_run_states` 表**零创建**、写队列与后台写线程**零注册**、`stats()` 键集合与基线逐字相同（不预先塞 0 占位）、响应体不出现 `meta.thread_id`。
- **[新表 `agent_run_states`]** 一行 = 一次运行的逐字段投影：`run_id` 主键、`agent_name`、`state`（与 `RunState.value` 同词表，写前显式校验、词表外直接抛）、`trace_id`、`meta` JSON、`run_depth`、`created_at` / `updated_at`，另加 `started_at` / `ended_at` / `error` / `cancel_reason` 四列（`AgentRun` 同一生命周期的投影——没有它们，重启后只看得到终态取值，看不到何时结束、为什么失败）。建表沿用本仓无 alembic 的 `checkfirst` 幂等惯例（`RunStateStore.ensure_ready()` 只建这一张表，**刻意不走** `Base.metadata.create_all` 全量）；模型**刻意不挂** `db/models/__init__.py`——挂进去就会被 `init_db(create_all=True)` 全量建出来，flag-off 也建表，纪律 1 作废。
- **[写路径]** `spawn` / 状态迁移 / `finish` / `cancel` 把快照投进**有界队列（256）+ 单后台写线程**（同步 SQLAlchemy 绝不在事件循环里跑，纪律 6 ②）；队列满**丢最旧并计 `persist_dropped`**（观测面不能把进程内存撑穿）。装配判定**唯一源** `control_plane.resolve_run_state_store()`——`get_control_plane` 与 `runs.py` 的「库里有没有面」判定都只认这一处（纪律 6 ④）。`finish` 之后的 meta 补写经 `persist_run()` 再投一次——但补投**只发生在成功路径**（`runs.py::_execute_run` 在 `finish(SUCCEEDED)` 之后才把 `result` 写进 `meta` 再投一次），FAILED / 超时 / 取消路径没有第二次投递；因此**正常路径下（写成功且未被丢最旧）**响应面看到的 `meta` 才与库行一致。
- **[写失败 fail-open，与 T18 是两套语义]** 写库失败只 `logger.warning` + `stats().persist_errors` 计数，运行照跑、内存态不回滚、绝不阻断主链（纪律 2）。**不得与 T18 互相援引**：T18 的审批 pending 是**治理真源**（不落库等于审批证据丢失，故失败要 `persist_error` 回执 + ERROR 日志 + 计数，显式可观测）；T21 的库面是**运行态快照**（权威仍是内存面；每次写都是带全量快照的 upsert ⇒ **中间迁移**丢一次写不会留半行，下一次状态迁移会整行覆盖，这一段才算可自愈）；但**终态那次写失败之后没有后续写入**——终态（`finish` / `cancel`）之后控制平面对该 run 一律 `return False` 不再入队，写线程失败只 `errors += 1` 后丢弃、**无重试**，队列满丢最旧的那条也无人补写 ⇒ 库行可能停在 `running` 或整行缺失，这就是「重启后查得到终态」的边界——一个是「批了没有」，一个是「跑过没有」，所以一个必须响亮失败、一个只需告警降级。
- **[读路径]** `GET /agents/runs`：内存列表与库列表按 `run_id` **去重合并、内存优先**（库只补历史，避免「列表只剩本次进程的 run」与单查各说一套）；`GET /agents/runs/{run_id}`：内存 miss 才回落库，同步 repo 经 `asyncio.to_thread` 下放（纪律 6 ②）。两处读库失败都 fail-open：warning 后回内存面（单查 miss ⇒ 404），坏行跳过不放大故障。启动**不**把库记录灌回内存（回落即可，免得历史 run 变成可 cancel 的活动对象）。
- **[响应形状变更]** flag-on 且 `options.session_id` 非空时，运行 `meta` 多一个 `thread_id` 键（取值 = `session_id`，也是图形的 checkpoint thread）；取不到就**不写该键**，不拿空串占位。flag-off 响应体与基线逐字节相同。契约登记在 `docs/项目实现手册.md` 的 Run API 段。
- **[回滚]** 关 `RUN_STATE_PERSIST_ENABLED` + `DROP TABLE agent_run_states`：前者立刻退回纯内存基线（表在也不读写），后者清掉已落的历史行。
- **[新增守卫] `tests/test_p167_run_state_persistence.py`（16 条）**：生命周期落库（创建 / 运行 / 终态）、取消终态落库、`finish` 后 meta 补写落库、同主键 upsert 更新同一行、`state` 词表外拒写、列表倒序 + 按状态过滤、重启后单查回落终态、列表去重合并且内存优先、写库失败不阻断运行（`persist_errors` + warning、内存态不变）、读库失败回落内存面、有界队列丢最旧并计数、`resolve_run_state_store()` 为唯一读取点且 flag-off 时不 import builder、flag-off 平面无 store 且 `stats()` 键集合与基线相同、flag-off 响应形状基线、`thread_id` 只在有 `session_id` 时出现。**条数取法**：该文件是 class 形式（`class Test*` + 方法），`grep -c "^def test_"` 对它返回 `0`，正确读数是 `grep -c "    def test_"` = 16，与 `--collect-only` 一致（无 parametrize、无 skip）。
- **[一般项登记（不阻塞通过，按细则跟踪）]** ① `main._shutdown` **未** flush 持久化队列：写线程是 daemon，进程退出时队列里最后几条可能丢——按现在的事实，`flush_persist()` 在全仓**只被测试调用**、`close_persist()` 也只由 `reset_control_plane()` 触发，而 `reset_control_plane()` 本身同样只被测试调用（`agentix/**` 无生产调用点）⇒ 关停链上没有任何一次冲刷，丢的可能是**终态那条写**（下一进程读到的是旧状态或整行缺失），不属「可自愈」，但也不阻断主链，故关停链应补一次 `flush_persist()`；② `RunStateStore.ensure_ready()` 的 `_ready` 标志**非线程安全**（无锁）：并发面不只有写线程——读路径（`list_runs` / `load_run`）同样会在首次进入 `_session()` 时调 `ensure_ready()`，且它们经 `asyncio.to_thread` 下放 ⇒ 同一进程里多个线程可能同时首建。结论不变：`CREATE TABLE` 走 `checkfirst` 幂等，并发首建最多多执行一次 DDL，失败自愈；③ 响应里 `thread_id` 与 `session_id` 的语义区别（前者是图形 checkpoint 线程、后者是会话面标识，当前两者取值相同）未在文档写明。
- **[文档同步]** 本文件；`README.md` §11 基线与口径链 re-cite（收口批全量实测）；`SECURITY.md` 已知边界补 `agent_run_states` 的业务内容与无 TTL 口径；`docs/工程口径日志.md` 追加本轮读数。配置键与 Run API 契约已在实现轮落地：`docs/architecture/配置管理.md` §5、`docs/项目实现手册.md` 的 Run API 段。

### 性能门禁 · T16 perf-baseline 扩关键业务端点（2026-10-28）

《审批与中断恢复优化规划》T16：夜间 `perf-baseline.yml` 此前只打 `/liveness`。这一轮**另起**一个 job 压三条关键业务端点，而不是往 liveness 链路里塞端点——挂载面、阻断强度、基线三件事都不同，混在一起等于顺手改动既有那条真阻断链路。

- **[新增 job `business-p95`]** `if: github.event_name == 'schedule'` ⇒ **只**跑夜间 cron，PR 与手动 dispatch 都不跑（本 workflow 顶层本就没有 `pull_request`，这一行是把"不挂 PR"**显式**钉死，不靠"恰好没写"）。
- **[三个端点，清单唯一]** `metrics`（Prometheus 抓取面，在 `public_paths.PUBLIC_PATHS` 里 ⇒ 开 `REQUIRE_API_KEY` 也免鉴权）、`sessions`（会话列表读路径，只过 `require_v1_user`，`V1_REQUIRE_JWT` 默认关 ⇒ 夜间无凭据可达）、`chat_routes`（路由/能力清单，纯静态无 DB）。刻意不选 `/readiness`（每次调用在线探 Redis/Neo4j/Milvus，外部抖动会被记成"服务劣化"）、`/api/v1/agents/runs`（router 级 `require_run_bearer` 严格验签，夜间无凭据恒 401，测到的是鉴权闸）、`/api/v1/chat`（要 LLM，夜间必假阳性）。选点注释只留**可由代码核对**的判据，不写实测读数（那种数字没有可复现落点，写进工作流只会腐烂）。
- **[阈值未改]** 仍 `0.20`：`PERF_MAX_REGRESSION="0.20"` 与既有 `--max-regression 0.20` 逐字相等，全文件只有这一个值，由守卫钉住（改阈值属行为变更，另批）。
- **[首周不阻断，到期转真阻断]** `PERF_ENFORCE_FROM=2026-11-04`：之前同一判定只 `::warning`（作业仍绿），到期起 `sys.exit(1)` 真阻断。切档靠日期，**不靠** `continue-on-error`——后者会让"到期该阻断"这件事也一直是绿的。合取 `if regressions and enforcing:` 与"首周只告警分支排在写基线之前"由守卫**按文本/按位置**钉住（子串存在性钉不住谁来当闸门：拆掉 `and enforcing` 后原有子串断言全绿）。
- **[失败样本判红，且不推进基线]** `scripts/load_test.py` 把超时/异常记进 `errors` 且**不追加延迟样本**（`_Worker.run` 只在拿到响应后 append），`success` 只是成功样本数 ⇒ "200 个请求里 60 个超时"时 p95 只由那 140 个存活样本算出。只读 `status_codes` 会把这个**低报值**判成 PASS 并写成下一版参照（静默低报 + 污染基线）。比对脚本现在把 `errors > 0`（等价 `success != requests`）计入失败，失败分支 `continue` 在写基线**之前** ⇒ 判红的 slug 不推进基线；失败行也在 step summary 表里占一行，不再只出现在折叠日志里。
- **[基线独立]** cache 前缀 `perf-business-baseline-`、文件 `perf-cache/business-baseline.json` 按 slug 分键，与 liveness 的单键 `perf-baseline.json` 互不恢复。压测前清空 `perf-cache/business/*.json`——缓存恢复会把上一轮报告带回来，而比对是 `glob("*.json")`，陈旧报告会被当成"本轮当前值"（清目录而不是在比对脚本里再抄一份 slug 白名单：白名单等于把端点清单拆成两个源）。
- **[新增守卫] `tests/test_p169_perf_baseline_endpoints.py`（7 条）**：三条验收各一条（端点在夜间清单里且清单行数与端点数对齐 / 不挂 PR 且 job 钉死 schedule / 首周只报告、到期真阻断）+ 阈值未改（全文件只有 `0.20`）+ 失败样本阻断（判据在、且按位置排在写基线之前并以 `continue` 收口）+ 基线持久化且独立 + liveness 链路形状仍在（压测参数、方向、`--allow-bootstrap` 建档声明各在原位）。负控实测：拆合取、删 `errors > 0`、失败分支去掉 `continue` 三种变异各自转红。
- **[文档同步]** 本文件；`docs/项目实现手册.md` §20.5（`quality_gate` 调用方改为覆盖现状：p95 CLI 比对 + business job 内联 `evaluate_metric`）与 §21.2 工作流形态清单（补 business job 的挂载面与 bootstrap 到期日）。`README.md` 未改（属另一任务范围）。

### 控制面取消 · T11 取消传播、FIFO 边界与 `BudgetExceededError` 处置（2026-10-28）

《审批与中断恢复优化规划》T11：取消此前只有「置终态」这一层语义——没人钉住它与底层任务的真实关系，也没有读数说明「取消多久生效」；`BudgetExceededError` 落哪条路同样只有口头说法。这一轮把三件事一起收口：**取消语义、有界 FIFO 的溢出行为、预算异常的处置现状**。行为零变更，只补用例、读数与口径。

- **[取消的语义：置终态 + 真正打断底层任务]** `ControlPlane.cancel(run_id, *, reason="user_cancelled")`（`agentix/runtime/control_plane.py`）先置 `CANCELLED` / `cancel_reason` / `ended_at`，把终态快照投进持久化队列，**再** `task.cancel()` 并 `await task`——后者正是「已报取消但仍在跑」这个假象的消解点：返回 `True` 时被绑 Task 已经退出。终态先投递再等退出是刻意的：执行器忽略取消时那个 `await` 可能很久，而「已取消」这个事实不该跟着一起等。
- **[幂等与乱序]** 已终态的 run 再取消返回 `False`，不会被第二次取消覆盖 `cancel_reason` 或 `ended_at`；取消顺序不影响 `list_runs` 的登记序（那是 spawn 序，不是取消序）；取消一个 run 不波及无关 run 及其绑定的 Task。
- **[并发]** 同一 run 被 N 个并发 cancel 打 ⇒ **恰好一个 `True`**（判定与置终态之间没有 `await`，胜者是确定性的「第一个」，不靠时序运气）；不同 run 并发取消互不影响；cancel 与 `finish` 竞争 ⇒ 恰好一方成功。
- **[有界 FIFO 与溢出钳位]** `max_runs` 溢出只淘汰最旧**终态**行，活动运行宁可超容也不被淘汰（丢掉的不能是还在跑的活）；被淘汰的 `run_id` 不能被 cancel / finish / bind 复活；`max_runs` 钳位下界 1；`max_concurrency` 槽位随取消释放；`max_depth` 边界含等号。
- **[基准读数]** `bench/cancel_propagation.py` 走 `ControlPlane.cancel` **原接口**，测的是**传播往返**：从调用进入到被绑 Task `task.done()` 的墙钟毫秒（不含 spawn / bind，不含事件循环启动）。分位数、指纹、落盘全走 `bench` 骨架既有实现（跨轮池化 + `nearest_rank_quantile`），`--repeats` 低于 5 直接拒绝；`cancel` 返回 `True` 却 `task.done()` 为假时**拒绝产出读数**（那种样本会把传播时延伪装成 0 附近）。本机读数 p95 约 **0.067–0.072 ms**（出处：`docs/design/审批与中断恢复优化规划.md` §8 台账行；`bench/results/` 下当前**没有**该 target 的结果文件，引用前先按文件里的用法自己重跑一次取本指纹内读数）。**不带任何阈值门禁**：读数不构成「合格 / 不合格」判定。
- **[`BudgetExceededError` 的处置现状（事实陈述，不是建议）]** 生产代码**零处**专用 `except BudgetExceededError` ⇒ 它落到 `chat_service.ask` 末尾的兜底 `except Exception`：默认回执 500 `internal_error`，`LEGACY_ERROR_MODE=True` 时 200 + 兜底文案；而 `is_retryable` 是对异常**文本做子串匹配**，用量数字（`used=500` / `used=429`）会把一次预算超限判成「上游瞬断」⇒ 503 `LLM_UNAVAILABLE`（现状缺陷，本节只如实记录）。生产装配的 Runtime 预算六维**全 0**，而每个抛点都有 `limit > 0` 前置 ⇒ 该异常在生产链路上当前**不可达**；`0` 是「不限额」而不是「额度用完」。它与编排域的 `ErrorCode.TOKEN_BUDGET_EXCEEDED`（`AgentError`，可降级）是两个机制，不得混称。
- **[新增守卫] `tests/test_p166_control_plane_cancel.py`（33 条）**：乱序取消 / 并发取消 / `max_runs` FIFO 与溢出钳位 / 准入边界（含等号与全 0 不拒）/ `BudgetExceededError` 现状钉住（含「生产代码不得新增专用 catch」「生产预算六维全 0」两条静态守卫）/ bench 入口冒烟（样本形状、骨架复用、`--repeats` 低于下限拒绝）。**不**在任何用例里断言时延阈值——时延没有门禁。
- **[文档同步]** 本文件；`bench/cancel_propagation.py` 的模块 docstring 自带同卡口径。

### 检索基准探针 · T12 向量库构建与 embedding client 构造（2026-10-28）

T12：检索链「每请求重建一次 vectorstore / embedding 客户端」此前没有任何读数落点，容量讨论全靠猜。这一轮**只加两个 bench target**，不改被测路径、不加开关。

- **[两个新 target（`bench/targets.py`）]** `retrieval_vectorstore_build`：`get_or_build_retriever(index_path)` 一次调用的完整重建（`RETRIEVAL_RETRIEVER_CACHE_ENABLED=false` 默认值下内部直调 `build_retriever → create_vector_store → FAISSVectorStore.load → create_embeddings`）；`retrieval_embedding_client`：`create_embeddings(get_default_config())` 的单次构造（faiss 在 load 时走的**同一个**生产函数）。
- **[走原接口，不复制逻辑]** 计时只包这一次原接口调用；「计数壳」只统计生产实现被调用了几次（`patch.object` 包一层、只多一次函数调用），不重写构造语义、不另立第二个实现。分位数与指纹走 `bench.aggregate` / `bench.fingerprint` 既有实现，本文件不写秩算式。
- **[构造次数断言：前提不成立就作废读数，而不是发一个错数字]** 一次 vectorstore 构造必须**恰好**新建 1 个 embedding client，否则 `RuntimeError`；flag 关时不得复用实例（构造次数必须等于按 `id()` 去重后的实例数）；`create_embeddings` 每次必须返回带 `embed_query` 的新实例。
- **[入口参数（`bench/run.py`）]** 新增 `--samples`（retrieval 类 target 每轮样本数，默认 **10**，`<1` 直接报错）与 `--index-path`（`retrieval_vectorstore_build` 的 FAISS 索引目录，缺省=运行时现场造小 fixture）。
- **[已知局限（与结果文件 `notes` 同文，引用读数必须连着这些一起引）]** ① 只测 **faiss** 档：milvus / qdrant 构造期只建连接、`_get_embeddings` 是惰性调用点，需外部服务 ⇒ **未实测**，跨栈差异不得由本读数推断成结论；② 默认 fixture 是小索引（20 文档 / 8 维）⇒ 读数主要反映客户端新建 + 索引反序列化的固定开销，真实索引越大反序列化占比越高；③ 每个 target 采样前预热 1 次，样本不含首次 import / 首次反序列化 / 证书栈初始化；④ 零网络，不含检索与嵌入请求 ⇒ 不是端到端检索延迟，也不是线上结论；⑤ 读数只对本指纹 + 同一索引规模可比。
- **[未落盘的读数]** `bench/results/` 当前只有 `kb_ingest_flatten-*.json` 与 `worker_replica_stress-*.json` 两份结果文件，这两个 target 的产物**没有落盘**；复跑：`python -m bench.run --target retrieval_vectorstore_build --repeats 5 --samples 10`（要读 embedding 构造则换 `--target retrieval_embedding_client`）。台账登记过的本机读数 p50 14.596 / p95 15.765 / p99 16.281 ms（出处：`docs/design/审批与中断恢复优化规划.md` §8 台账行；该行未注明是哪一个 target 的池化分位，引用前先按上面的命令重跑取自己的读数）。
- **[文档同步]** 本文件；target 元数据里的 `entry` / `notes` 与读数同处一文件（判据只有一个源）。

### 队列积压告警 · T15 两条 Celery 队列规则与阈值推导（2026-10-28）

T15：`docker/monitoring/alert_rules.yml` 原有 8 条告警全是「服务自己暴露的指标」，broker 侧积压零告警——队列堵住时 `up` 仍是 1、5xx 也不涨，运维看不到。这一轮**只新增**两条规则，不删不改既有 8 条。

- **[新增规则（规则数 8 → 10）]** `My AgentixCeleryQueueBacklog`：`sum(celery_queue_length) > 20`，`for: 10m`，`warning`；`My AgentixCeleryQueueNotDraining`：`sum(celery_queue_length) > 0`，`for: 30m`，`critical`。两条挂在新分组 `agentix-queue`（队列面挂的是外部 exporter，与 `agentix-availability` 分开）。
- **[前置：指标不在本仓暴露面——未部署 exporter 时这两条恒为「无数据」]** `celery_queue_length` 由 broker 侧的 **celery-exporter** 导出；`agentix/observability/metrics.py` **不**暴露任何队列长度指标，`docker/monitoring/prometheus.yml` 现有三个抓取目标（`agentix-backend` / `prometheus-self` / `alertmanager-self`）里也**没有** exporter ⇒ 不部署 exporter 时这两条规则**不触发、也不报错**，是「规则在、指标不在」的静默态，不要读成「已经能看到积压」。可评估的三件前置（部署 exporter 且 `BROKER_URL` 指向 worker 用的同一个 broker / `prometheus.yml` 加抓取目标 / worker 以 `-E` 启动或改用直读 Redis 的 exporter）写在**规则文件末尾的前置说明**里，且用例钉住那段注释不得被删——判据与说明绑在一起，杜绝「编个指标名加规则、注释里什么也不说」。
- **[阈值不是线上分位数，是从声明过的容量推来的]** `20 = 10 ×（RETRIEVAL_WORKER_REPLICAS 默认 1 × compose command 的 concurrency 2）`，即 10 个串行波次；作业若打满 `INDEX_JOB_TIMEOUT_SECONDS=1800s`，队尾最坏等待 = 10 × 1800s = 5h，远超「一次批量入库能等」的量级。第二条的窗口直接取作业硬上限本身（1800s = 30m）。改副本 / 并发 / 超时后阈值必须按同一式子重算，不要凭感觉调。
- **[成因不唯一，别把第二条写成单一结论]** 「连续 30 分钟非空」只能推出**持续积压**，推不出成因：消费停摆（入队照旧、出队为 0）与持续饱和（入队速率长期高于吞吐）在同一个表达式下完全一样，区分动作写在 `description` 与值班手册里。同理刻意**不带** `{queue="celery"}` 选择器：exporter 的队列标签名可能是 `queue` 或 `queue_name`，选错时 Prometheus 拿到空向量 ⇒ 规则形状正确但永不触发（静默失效比不写更难发现）；当前 `celery_app` 未配 `task_routes`、任务装饰器也未指定 queue ⇒ 全部进默认队列，求和与按队列拆分在此部署下等价（后续 exporter 上线并配 `task_routes` 后需重估，见规划文档 §8.2 表 A 的 G8）。
- **[读数口径（读码可得，不是实测）]** `celery_queue_length` 读的是 **broker 队列键**长度，而本仓 `task_acks_late=True`：已被 worker 预取但未 ack 的作业在 Redis 的 PEL 里、**不在**队列键里 ⇒「长度 0」不等于「没有作业在跑或卡住」，worker 卡在一个长作业上时长度可以一直是 0，直到 `visibility_timeout`（≥2×1800s）到期重投才回升。
- **[容量基线读数（环境参照，不进告警门禁）]** `bench/worker_replica_stress.py` 1 / 2 / 4 副本 = **299.5 / 601.8 / 961.4 tasks/s**（`bench/results/worker_replica_stress-20261028T054042Z.json`）；`bench/kb_ingest_throughput.py` = **55126.8 rows/s**，p50 **3.608 ms**（`bench/results/kb_ingest_flatten-20261028T054023Z.json`）。前者的任务体是 no-op + 一次 Redis INCR，读的是 broker / 调度面的扩容曲线，**不是**索引作业吞吐。
- **[新增守卫] `tests/test_p168_alert_rules_queue.py`（12 条）**：新规则存在且既有 8 条一条都没被删 / 改名；每条新规则的 `expr` / `for` / `severity` / `annotations` 齐全，`for` 是合法 Prometheus 时长；`expr` 里每个指标名都有出处（本仓真实暴露 / Prometheus 内置 `up` / `EXTERNAL_METRICS` 署名登记），登记项必须能在规则文件的注释里找到出处；阈值数字必须能在 `description` 里找到（「为什么是这个数」不能只活在提交信息里）；无高基数 label；外部指标的前置说明必须在规则文件本身里。**钉形状与口径，不钉阈值对不对**（阈值合不合理属观测结论）。
- **[值班手册同步]** `docs/runbooks/值班响应手册.md` 从 8 条扩到 10 条，新增两条的排查动线，第一句就是「先确认这条规则是不是无数据」。随通过登记的一般项 G2–G9 见规划文档 §8.2 表 A。
- **[文档同步]** 本文件；`docs/runbooks/值班响应手册.md`。

### 评测入口 · T3 默认题集缺失时响亮退出（2026-10-28）

T3：`evals/legal_golden.jsonl` / `legal_golden_ext.jsonl` 已于 2026-10-26 随评测集下线删除，但六个评测入口的 `--golden` 缺省串还指向它们。缺省指向一个不存在的文件，最坏的收法不是报错而是**静默跑空**：题集读成 0 条 → 指标 0 或整表空白 → 退出码 0 → 看的人以为「评测跑过了」。这一轮把这条路径统一成响亮失败，并补上此前零覆盖的自动化守卫（本卡落地时**零自动化覆盖**，用例由收口批补齐）。

- **[统一入口 `evals/golden_source.py`]** `ensure_default_golden(explicit, *, entry, default, example_flags="", exit_code=MISSING_GOLDEN_EXIT)` → `(生效的题集参数, 退出码)`：未传且缺省缺失 ⇒ 打印「默认题集不存在」+ **真扫出来的**可用题集清单（带行数）+ 改道示例，返回 `(None, 2)`；调用方必须先看退出码，不许拿 `None` 继续跑。退出码取 `2`（`MISSING_GOLDEN_EXIT`），与各入口既有的「参数 / 输入不对」用法一致，不复用 `3`（`run_eval` 里专指「缺端点 / 未实现」）也不复用 `0`。
- **[六个入口接线形状相同]** `run_eval.py` / `hybrid_eval.py` / `ragas_report.py` / `golden_to_promptfoo.py` / `feedback_ingest.py` / `scripts/demo/local_kb_demo.py`（后者的 `--golden` 只挂在 `eval` 子命令上）的 `--golden` argparse 缺省值**全为 `None`**，再由 `ensure_default_golden` 一处判——同一个判定不许两个源（纪律 6 ④）。此前任一入口丢掉这次调用都**不会自己报错**：`ragas_report.py` / `feedback_ingest.py` 的读集函数对缺失文件返回 `[]`，直接退化成「空样本 + 退出 0」。
- **[清单来自目录扫描，不是硬编码]** 可用题集 = `evals/` 下**文件名含 `golden`** 的 `.jsonl`（扫描根是缺省路径的父目录，跳过隐藏目录与 `__pycache__`）；写死一份「可用题集」清单就是下一个会腐烂的抄本（纪律 7 同一个毛病）。
- **[不误伤显式调用]** 显式传 `--golden`（哪怕文件不存在）**原样返回、退出码 0**——「响亮退出」只管「没给且缺省不存在」这一种情形；显式传值且文件存在时各入口行为逐位不变。
- **[保守边（规划文档 §7 Q6，待维护者追认）]** 只改提示与退出码——**不**新建任何题集，**不**把缺省改指到别的题集（那是 Q6 的另一边，未裁决）。
- **[新增守卫] `tests/test_p146_golden_loud_exit.py`（8 条）**：缺省缺失且扫描到可用集 ⇒ `(None, 2)` 且 stderr 含真扫出来的文件名与改道示例关键片段；显式传值原样返回 + 退出码 0；六个入口的 `--golden` 缺省全为 `None`（实造 parser 再 `parse_args`，**不**在源码文本上做字符串匹配）。用例用 `tmp_path` 现场造题集，结果与仓库里现存的题集无关。
- **[文档同步]** 本文件；`evals/golden_source.py` 模块 docstring 记着同一条保守边。

### 审批与中断恢复 · T19 审批等待 deadline，到期自动拒绝（2026-10-28）

《审批与中断恢复优化规划》T19（Q9 已裁决：到期自动拒绝）：此前挂起只受外层 `AGENT_TIMEOUT`（单次图调用墙钟）约束，没有任何「审批躺太久」的收尾——一条没人处理的 pending 会一直挂在待审面上。这一轮给它一个可配的时限，到期按 fail-closed 自动拒绝。

- **[新增配置] `HITL_APPROVAL_DEADLINE_S`**（默认 `0`，`>=0`）：**0 = 关 = 现行为**——不建扫描任务、**扫描链**一次库都不读（T18 的启动 restore 不属本卡新增读取）、`stats()` 键集合与基线逐字相同（纪律 1）。`>0` 才启用：后台扫描任务周期读 `hitl_pending_approvals`（周期 = deadline/4，夹在 1–30 秒），把 `status=pending` 且 `created_at <= now - deadline` 的行拒绝掉。
- **[前置链]** 到期拒要真跑起来，三件事都得成立：`HUMAN_REVIEW_ENABLED=true`（否则没有桥、也没有 pending）、`HITL_PENDING_PERSIST_ENABLED=true`（否则没有库可扫）、`HITL_APPROVAL_DEADLINE_S>0`。任一不成立 ⇒ **告警一次且不建任务**（hook 安装失败导致桥缺失时同样告警不建），四道判定按序收口在 `main._start_hitl_deadline_sweeper` 一处（纪律 6 ④）。
- **[治理路径唯一]** 到期拒**复用** `HitlBridge.decide(REJECT)`（闸门 reject + sidecar 收口 + 清内存 + 断点解除 + 库闭合同一条路径），不另写一条 reject 路径（纪律 6 ④）。落地的库行是 `status=rejected`、`decided_by=system:deadline`。
- **[超时拒与人工拒可区分]** 四类可见面：`BridgeDecision.reason == "auto_reject_timeout"`、库行 `decided_by`/`decision_reason`、`stats()["auto_reject_timeout"]`（该键只在 deadline>0 时存在）、指标对 `retrieval_approval_decisions_total{decision="rejected"}` + `retrieval_approval_timeouts_total{target_type="tool"}`（与检索侧审批同一形态：裁决一条指标、超时再补一条）。**指标词汇不动**：`decision="rejected"` 是六值封闭集之一（p134 不许改），超时**不另造** decision 值。
- **[计数口径：超时是 `rejected` 的子集]** `rejected` 含人工拒**与**超时拒（到期自动拒绝也是拒绝）；`auto_reject_timeout` 只是其中按归因拆出的子集，**不进** `decided` 求和（否则同一次裁决数两遍）；`rejection_rate = rejected/decided` 因此**不因超时拒被稀释**（一条纯超时拒 = rejected 1 / decided 1 = 1.0）。子集计数在 `decide` 的**同一次写入**处完成（`attribution` 参数），不做"先 +1 再 -1"的事后回改。
- **[与 AGENT_TIMEOUT 的关系]** 两把尺子、量纲不同：`HITL_APPROVAL_DEADLINE_S` 管「pending 存活时长」，`AGENT_TIMEOUT` 管「单次图调用墙钟」；审批挂起本来就不让图停住（拦截钩子超时后 executor 已返回 blocked），两者同时可达时**先到者生效**，归因不同（一个留请求级超时错误，一个留可查询的 `rejected` 审批行）。口径见 `docs/architecture/运行时护栏.md` §8.1。
- **[本卡不恢复图、不重开执行]** 到期拒是治理层终局：执行点拿到的是"没批准"（fail-closed），不重试、不重开执行。**进环车道不在扫描面内**：`HITL_IN_LOOP_ENABLED` 的图内 `interrupt()` 挂起不写这张表，其暂停由 `POST /ops/hitl/in-loop` 恢复。
- **[失败姿态]** 读库失败只 `logger.warning` 并等下一轮（fail-open，纪律 2）；单行失败（坏载荷 / 闸门侧异常 / `decide` 落 not_found）跳过并告警，不阻断其余到期行，且**未落地的到期拒不计数、也不打超时指标**（不谎报成功）。
- **[并发与去重（第 2 轮返工）]** `--workers 2` 意味着每个 uvicorn 进程都起一个扫描任务，而 `expire_overdue` 原先存在三条「同一条 pending 被重复裁决/重复计数」的路径（两个扫描者同时扫到同一行；闸门已终局时 `gate.reject` 返回 `False` 却照常计数；`close_decision` 失败后库行仍 `pending`、内存已 pop，下一轮立即重算）。这一轮补两层 CAS：① **扫描租约**（`hitl_sweep_leases`，命名 `hitl_approval_deadline`，TTL 120 秒，条件更新抢占、持有者崩溃后由其他 worker 接管、`_shutdown` 显式让出）保证同一时刻只有一个扫描者，拿不到租约的那一轮**连库都不读**；② **行认领**（`hitl_pending_approvals.claim_owner/claim_until`，列级 CAS，**不改 status 词表**）保证**多 worker 下同一行只被一个扫描者裁决**（同刻其他 owner 抢不到，同一 owner 在 TTL 内也不许重复认领）。③ 库闭合失败时**保留认领**，认领 TTL 到期后按**闸门里那条既有裁决**补偿闭合库行（不重新计数、不打超时指标）。配套：`decide(..., require_gate_pending=True)`（仅 `expire_overdue` 传）——闸门里已无待决记录时这次拒绝**没新落地**，只报警、不计数、不闭合；`attribution` 只对 `REJECT` 有效，其余动作传它抛 `ValueError`。
- **[关停]** `_shutdown` 取消扫描任务并 `contextlib.suppress(asyncio.CancelledError)` 收口，句柄置回 `None`，并让出扫描租约（句柄存的是起任务时那台桥，不重新装配单例）；扫描是 `create_task` 后台跑，**不阻塞启动**。
- **[跨实例与闸门记录缺失（第 4 轮返工）]** 扫描租约只让**一个** worker 扫（`list_expired` 是全局查询），于是"进程内状态"与"库真源"的错位必须逐条收口：① **库行 status 是待审面的判据**——「挂起在 A、被 B 拒掉」是 `--workers 2` 下的常态，旧实现里 A 的 `_requests` 没人清理，审批列表继续把已超时拒绝的行当待审列出，运维点 approve 还会把库行从 `rejected/system:deadline/auto_reject_timeout` 改写成 `approved/ops/…`。现在 `pending()` 经新增的 `HitlPendingStore.statuses_for()` 剔除「库里明确存在且非 pending」的陈旧项并**回收**本实例的 `_map`/`_requests`（库里没有该行 = 从未落库成功，照旧列出）；`decide()` 内存命中也会先核一次库行 status，已闭合 ⇒ `not_found`（`reason="pending_already_closed"`）、读失败同样 fail-closed（`reason="pending_status_unreadable"`）。② `HitlPendingStore.close_decision()` 对已决行**不覆盖**（返回 `False`，一个闭合列都不写、不清认领）——裁决只写一次，`decided_by`/`decision_reason`/`status` 不被后手改写。③ **闸门记录被 FIFO 200 逐出时按 sidecar 载荷补建后再拒**：`HumanReviewGate._register` 对最老记录不区分是否 pending 直接逐出，"记录没了"不等于"已决"；旧实现下 `gate.reject` 恒返回 `False` ⇒ 该行永久停在 pending、每轮空转（`did_not_land` + 释放认领）、永久占一个 `max_unresolved` 槽位、还因为按 `created_at` 升序取数而挡住后面的到期行。
- **[新增守卫] `tests/test_p165_hitl_approval_deadline.py`（61 条）**：配置默认 0 与 `ge=0`；`list_expired` 取数口径（只 pending / 含等号 / 升序 / `limit`）；到期行被闭合为 `rejected` 且归因字段齐备、未到期行不动、闸门 reject 真落地、二次扫描幂等；人工拒与超时拒同批可区分（`rejected==2`、`auto_reject_timeout==1`、`decided==2`、`rejection_rate==1.0`）与纯超时拒的子集口径（`rejected==1`/`decided==1`/`rejection_rate==1.0`）；指标面可区分（到期拒打一次 `record_approval_timeout("tool")`，未落地不打）；`AGENT_TIMEOUT` 取值不影响到期判定（含 `AGENT_TIMEOUT < deadline` 的先到者形态）；`resolve_approval_deadline_s` 为唯一读取点；deadline=0 / store=None 返回空且键集合不变（扫描链不读库）、单例装配读配置；单行失败不阻断、读库失败 fail-open、未落地不计数；扫描周期三个边界；`HUMAN_REVIEW_ENABLED` 关 / `HITL_PENDING_PERSIST_ENABLED` 关 / 桥缺失时各自告警且不建任务、齐备时建任务且可取消；`_startup` 接线；扫描循环抛错后继续；`_shutdown` 取消任务、清句柄并让出租约；**并发去重**：扫描租约（独占/续租/到期接管/释放即取）、行认领（一 row 一 claimer、同 owner TTL 内不得重复认领、已决行不可认领、闭合与重挂均清认领）、已被他人认领的行不被裁决、拿不到租约不读库、`close_decision` 连续抛错时第二轮不增 `rejected`/`auto_reject_timeout`/超时指标且 TTL 后对账补闭合、闸门已决时 `require_gate_pending` 报 `gate_request_not_pending` 且不计数、`attribution` 非拒绝动作抛 `ValueError`；**跨实例与闸门补建（第 4 轮返工）**：对端实例拒掉后本实例待审面剔除该行且再裁返回 `not_found`、库行归因与 `status` 不被改写，闸门记录被逐出（`gate.clear()`）后按 sidecar 载荷补建再拒（`rejected`/`auto_reject_timeout` 各一次、超时指标恰好一次），`close_decision` 已决行不覆盖（第二次返回 `False`），`decide` 核库行 status 读失败 fail-closed（`pending_status_unreadable`；不计入裁决桶——`not_found` 计数 +1，库行仍 pending）。
- **[配置五处同步]** `settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `docs/architecture/配置管理.md` §5 / `test_p41_doc_consistency.py` 手写清单。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加 T19 读数（5580 / 40 / 4 / 0，+61）；`docs/architecture/运行时护栏.md` §8.1（两把尺子与先到者生效 + 先核后写窗口）+ §9 速查；`docs/architecture/配置管理.md` §5（`HITL_APPROVAL_DEADLINE_S` 与两张表的回滚口径）；`docs/项目实现手册.md` 补审批等待时限一段。

### LangGraph 原生对齐 · LG-3-6 优雅关闭（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P3 第 6 条：此前收到 SIGTERM 是直接杀进程，在跑的图要么被硬切断、要么整轮 500，只能靠下次启动去猜哪些线程没跑完。这一轮接上框架原生的**协作式停摆**：信号到达后，在跑的图在**超步边界**停下、留下可恢复 checkpoint，然后进程才退出。

- **[新增开关] `RUN_CONTROL_ENABLED`**（默认 `false`）：开 = `_startup` 安装 SIGTERM/SIGINT 处理器、`chat_service` 每次流式运行新建 `RunControl` 并 `astream_events(..., control=ctl)`；默认关 = **不装任何信号处理器**、**不传 `control` 形参**，调用形状与基线逐位等价（纪律 1）。
- **[新增配置] `RUN_DRAIN_TIMEOUT_S`**（默认 `30`，`>=1`）：drain 的**上限**。信号处理函数起一条 daemon `threading.Timer`，到点仍有运行在跑 ⇒ `logger.error` 后 `os._exit(1)` 强制退出（checkpoint 已在超步边界落盘，恢复走启动链）；`_shutdown` 里排队清空即撤销计时器。容器里走 uvicorn CLI，`__main__` 的 `timeout_graceful_shutdown` 管不到，所以硬上限由进程自己持有。
- **[drain 是调用级的] 每次运行新建 `RunControl`**：框架源码注释与 1.2.18 实测都明确——复用已 `request_drain()` 的实例等于**永久 drain**（第一个超步边界就抛，一个节点都不跑）。`policies.py::DrainCoordinator` 因此维护「在跑控制面」登记簿：`begin()` 新建 + 登记，`end()` 注销（正常/异常/断连都走），信号到达时统一置位；关停期间新运行拿到**预置位**控制面（第一超步即退出，不接新任务）。
- **[信号必须串接]** 包装的处理器先 `request_drain()`，再把信号交给**原处理器**（uvicorn 的 `should_exit` 才是进程退出的推手）。覆盖而不串接，进程就再也不响应 SIGTERM——「优雅关闭」会从「停不下来」变成「根本不停」。
- **[流式外层如实收口]** `GraphDrained` 在 `chat_service` 被单独捕获（与 `interrupt` 同层）：按 `cancelled` 收口运行记录、发 `error` 帧「服务正在重启，本轮任务已保存」、仍发唯一 `done`——不落进通用 `except Exception` 的「处理请求时出错」。
- **[恢复链路衔接]** `GraphDrained` 抛出时 checkpoint 已保存（1.2.18 实测：`alist()` 能列出该线程、`aget_state().next` 指向未跑的下一步），与 LG-2-1 的启动恢复 / 续跑入口直接接上。
- **[守卫#11 不增行]** `RunControl` / `GraphDrained` 从 `orchestration/langgraph/policies.py`（既在白名单内的适配层汇聚点）转出，`chat_service` 不直接 import langgraph ⇒ `test_p66_arch_guards.py` 的等式棘轮**未改一行**。
- **[新增守卫] `tests/test_p164_run_drain.py`（25 条）**：开关默认关、上限 `>=1`；协调器每运行新建控制面、注销幂等、统一置位、关停期预置位、`wait_for_drain` 有界；真图 drain 在超步边界停摆（`trail == ["a","b"]`、`alist` 非空、`next` 非空）；不传 `control` 正常跑完；复用已 drain 的控制面一个节点都不跑；信号处理器先 drain 再串接原处理器、安装/卸载可逆、flag 关不装；强制退出计时器到点真调 `os._exit` 且不叠计时器；`_shutdown` 的等待有界、没排空不撤计时器；`chat_service` flag 关不传 `control`、flag 开每请求新建且运行期登记、`GraphDrained` 如实报告。
- **[配置五处同步]** `settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `docs/architecture/配置管理.md` §5 / `test_p41_doc_consistency.py` 手写清单。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十九段读数；`docs/项目实现手册.md` 补优雅关闭一段。

### LangGraph 原生对齐 · LG-3-5 节点级错误补偿（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P3 第 5 条：`execute_task` / `code_agent` 是**有副作用**的节点，一旦抛错整轮 500，「哪些副作用已经发生」就丢给调用方猜。这一轮用框架原生 `add_node(error_handler=)` 接住失败：写一条可查的补偿记录，并把如实的失败回执交给用户，而不是让整轮静默失败。

- **[新增开关] `NODE_ERROR_HANDLER_ENABLED`**（默认 `false`）：开 = 主图给 `execute_task` / `code_agent` 挂原生 `error_handler`；默认关 = 注册处**完全不传**该形参，编译图里没有 `__error_handler__*` 节点，与基线逐位等价（纪律 1）。
- **[触发语义] 重试耗尽后才进 handler**：langgraph 1.2.18 实测——同节点挂 `retry_policy` 时节点跑满 `max_attempts` 次才调用 handler；无 `retry_policy` 时「耗尽」即首次失败。`GraphBubbleUp`（`interrupt` 同族）**不交给** handler，HITL 挂起语义不变。
- **[失败出口] `Command(update={"messages": [...]}, goto=END)`**：失败回执写进 `messages`（含节点名与异常类型），图以明确终态结束而不是抛 500；出口可用 `exit_node` 改。
- **[补偿记录复用 `hitl_pending_approvals`（Q7 真源），不新建存储]**：写一条**已闭合**记录——`status=rejected`、`decided_by=system:node_error_handler`、`decision_reason` 含节点与异常。刻意**不用** `pending`：未决行会被启动恢复重新装配成审批请求，节点失败记录混进去就是幽灵审批。
- **[幂等主键] `nodeerr:<sha256(thread|node|异常类型|异常文本)[:32]>`**：节点失败会被重入（恢复 / 重试 / 同轮重放）重复触发，主键收敛保证「一条失败一条记录」，不让真源表变成事件流水。
- **[不重跑节点]** handler 只记录 + 收尾；`execute_task` / `code_agent` 开关打开后仍无 `retry_policy`（整节点重跑会重放工具副作用，LG-1-1 铁律）。
- **[fail-open]** 写库失败只 `logger.warning`，失败回执与路由照常；且回执如实改口「补偿记录写入失败」（handler 自身抛错会让整轮失败，补偿动作必须自带兜底）。
- **[新增守卫] `tests/test_p163_node_error_handler.py`（20 条）**：配置默认关与工厂导出；`Command` 改 state / 路由 / 自定义出口 / 回执内容如实；记录落既有表且 `list_unresolved` 为空（反幽灵审批）、同因幂等、异线程/异因分行；写库失败降级并 warning；真实图里「重试耗尽才进 handler」「无 retry_policy 首次失败即进」`Command` 目标节点真被执行；`GraphBubbleUp` 穿透；主图关时零注册 / 开时只挂两个有副作用节点且不新增 `retry_policy`；`**node_error_handler_kwargs` 只出现在两个 add_node（AST，补 p147 对 `**` 的豁免）与实参来自 `policies`。
- **[配置五处同步]** `settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `docs/architecture/配置管理.md` §5 / `test_p41_doc_consistency.py` 手写清单。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十八段读数；`docs/项目实现手册.md` 补节点错误补偿一段。

### LangGraph 原生对齐 · LG-3-4 恢复期幂等（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P3 第 4 条：`execute_task` 是节点重入语义——in-loop HITL 挂起后恢复会**重跑挂起点之前已经成功过的步骤**，非幂等写工具的副作用因此发生两次。checkpoint 记得「图走到哪」，不记得「哪一步的业务副作用已经发生过」；这一轮把后者落成显式清单。

- **[新增开关] `RECOVERY_STEP_LEDGER_ENABLED`**（默认 `false`）：开 = `execute_task` 收到的 `step_scope` 与 `thread_id` 齐备时，已完成步骤落 `run_step_ledger`，同一作用域恢复时跳过并复用落账返回值重建观测；默认关 = 连表都不建、零读写，与基线逐位等价（纪律 1）。
- **[新增表] `run_step_ledger`**（`agentix/infrastructure/persistence/db/models/run_step_ledger.py`）：主键 `(thread_id, scope, step_key)`，另有 `tool` / `value_json` / `attempts` / `created_at`。建表走 `create(checkfirst=True)`（本仓无 alembic，与 `hitl_pending` 同款）；模型**不**进 `db/models/__init__` 聚合，避免 flag-off 时 `create_all` 顺带建表。回滚：`DROP TABLE run_step_ledger`。
- **[身份不取模型 id] `step_key = sha256({"tool", 规范化 args})[:24] + ":" + 同指纹序号`**：恢复会重新拆解，id 与顺序都不保证复现，按 id 记清单等于恢复后全部失配；同一计划里完全相同的步骤按出现序号区分，不把两次合法调用并成一次。
- **[作用域取 `checkpoint_ns`]**：同一次挂起的续跑拿到同一个 ns、新一轮对话换新 ns（langgraph 1.2.18 实测，`tests/test_p162` 用 MemorySaver 钉住这条事实）。用它区分「同一轮的重入」与「下一轮」，避免把上一轮的成绩当新任务的清单。
- **[接线] `task_execution.run_task_execution(step_scope=)`**：拆解后读本作用域清单、波内已完成的步骤直接复用（`logger.info` 记跳过数）、执行后把**成功**步骤 upsert 进清单；失败步骤不落账（下次仍重试）。HITL 批准重试成功的那一路，落账取替换后的 outcome。
- **[失败姿态]** store 只显式抛错、不吞；降级只在调用方一处：读失败按「本次无恢复保护」继续跑，写失败只告警（后续恢复可能重放这几步），均 `logger.warning` 留痕（纪律 2）。
- **[新增守卫] `tests/test_p162_recovery_step_ledger.py`（26 条）**：store roundtrip / `merge` 幂等 / 坏载荷按未命中 / 空入参 no-op / 建表幂等 / `attempts` 下限 / scope 隔离；步骤身份跨构建稳定、不认 id、重复步骤按序号区分、args 键序无关；flag-off 零接触 store 且两次真实执行；**计数桩证明恢复后副作用只发生一次**（全完成 / 部分完成 / 换 scope / 换会话 / 换 id / 重复步骤 / 缺作用域）；读写故障降级并留 warning；`execute_task` 透传 `checkpoint_ns`；langgraph 续跑 ns 语义。
- **[配置五处同步]** `settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `docs/architecture/配置管理.md` §5 / `test_p41_doc_consistency.py` 手写清单。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十七段读数；`docs/项目实现手册.md` 补恢复期幂等一段。

### LangGraph 原生对齐 · LG-3-3 四个业务指标（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P3 第 3 条：三层的重试与四个恢复入口此前只有日志，没有可聚合的读数——「哪一层在重试、退避睡了多久、重试链什么时候被判耗尽、图是怎么被续起来的」都只能翻日志。

- **[四个指标] `retry_attempts_total{layer}` / `retry_sleep_seconds_total{layer}` / `retry_exhausted_total{layer}` / `graph_resume_total{kind}`**：定义在 `agentix/observability/metrics.py`（含 `_NoopMetric` 降级桩）；便捷函数 `record_retry_attempt` / `record_retry_sleep` / `record_retry_exhausted` / `record_graph_resume`。
- **[label 封闭] `layer` / `kind` 都是枚举**：`RETRY_LAYER_VALUES = {tool, task, retrieval}`（与 `RetryBudget.try_consume(layer=...)` 同源）、`GRAPH_RESUME_KIND_VALUES = {startup, ops, retry_task, hitl_in_loop}`（四个生产入口各占一个值）。枚举外的值一律落 `"other"`，**不引入 thread_id / user_id 等高基数 label**。
- **[三层接线]** 检索 `retrieve_docs_with_retry`（同步循环）、task `run_task`、工具 `ToolExecutor`：只在**真的重试/真的退避**时落账（预算拒批/期限截断的路径不记 attempts 与 sleep）；`retry_exhausted` 只数「不会再有下一次尝试」的失败——首次即判不可重试的错误从未进入重试，不算耗尽。
- **[四个续跑入口]** `CheckpointRecovery.resume_thread(kind=)` 新增关键字：启动恢复 `startup`、`/ops/threads/resume` `ops`、`/ops/threads/retry-task` `retry_task`；进环 HITL 的 `lg_builder.resume_in_loop` 记 `hitl_in_loop`。计数点是**向图发起续跑**（含随后抛错的），无 checkpoint 直接返回的路径不计数——指标不在成功路径上自夸。
- **[新增守卫] `tests/test_p161_retry_resume_metrics.py`（22 条）**：指标名/label 名逐项、不越 p100 白名单与禁用集、越界值落 `other` 且不新开时间序列、生产调用点字面量与两个枚举**双向相等**（AST）、三层各自真实重试的增量（attempts/sleep/exhausted）、不可重试与预算拒批两种边界、四个续跑入口各自的增量、无 checkpoint 不计数。
- **[白名单]** `test_p100_genai_observability.py::ALLOWED_METRIC_LABELS` 登记 `layer` / `kind` 并注明有界理由。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十六段读数；`docs/项目实现手册.md` 补四个业务指标一段（并入 §15.6 指标族清单）。

### LangGraph 原生对齐 · LG-3-2 主图节点的原生超时（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P3 第 2 条：原方案的「`asyncio.wait_for` 壳」换成框架原生`add_node(timeout=TimeoutPolicy(...))`——超时由图执行器按**每次节点尝试**计时，超时点抛`NodeTimeoutError`，不再由调用方在节点外面再包一层。

- **[新增配置] `NODE_TIMEOUT_SECONDS`**（`float`，默认 `0.0`，`ge=0`）：主图节点的每次尝试墙钟上限（秒）。`0` = 关：注册处**完全不传** `timeout` 形参（不是传 `None`），编译出的主图与基线逐位等价（纪律 1）。
- **[单一构造点] `policies.node_timeout` → `lg_builder._node_timeout_kwargs()`**：非正一律 `None`；辅助函数只在策略非空时返回 `{"timeout": policy}`，10 个 `add_node` 全部 `**` 展开该 dict（关时是空 dict）。`lg_builder` 内不内联 `TimeoutPolicy(...)`（LG-0-3 策略集中）。
- **[重试不放大] `nodetimeouterror` 进不可重试分类**：`NodeTimeoutError` 故意不继承内置 `TimeoutError`，框架默认把它当可重试——同节点挂 `timeout` + `retry_policy` 会变成「超时→重试→再超时」。业务分类 `is_retryable` 把它判为不可重试（验收第 ④ 条）：慢节点只执行一次，异常冒泡后可由原生 `error_handler` 接住，整轮不会静默失败。
- **[边界] 协作式取消，不是抢占**：`run_timeout` 只在节点**让出事件循环**的时刻判得出来。三条用例钉住：整段不让出的协程照常跑完（0.05s 上限下 0.4s 阻塞仍返回）、阻塞后让出的协程能杀但耗时等于阻塞时长、同步节点在 `compile().validate()` 阶段直接 `ValueError`（框架不接受同步节点超时）。
- **[新增守卫] `tests/test_p160_node_timeout.py`（26 条）**：策略工厂五种「关」取值、设置项默认值与 `ge=0`、关时 10 个节点 `timeout is None`、开时 10 个节点都挂同一策略、10 个 `add_node` 全部 `**node_timeout_kwargs`（AST，防空漏接）、辅助函数的值只来自 `policies.node_timeout`（AST）、`NodeTimeoutError` 原生抛出与字段、业务 `retry_policy` 不重试（验收 ④）、`error_handler` 收到 `NodeError` 并落地 `Command` 更新、两条协作式取消边界、分类层四条断言。
- **[配置五处同步]** `settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `docs/architecture/配置管理.md` §5 / `test_p41_doc_consistency.py` 手写清单。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十五段读数；`docs/项目实现手册.md` 补节点超时一段。

### LangGraph 原生对齐 · LG-3-1 退避停等的 deadline 预算感知（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P3 第 1 条：三层重试（检索 / task / 工具）的退避停等此前只认「第几次失败」，不认「还剩多少时间」——预算只剩 1s 时仍会先睡 1s 再开一次注定越界的尝试，用户要等到最后一次尝试之后才看到超时。

- **[单一算术] `domain.policies.retry.budgeted_pause(delay, remaining)`**：返回 `(实际停等秒数, 是否应放弃重试)`。无 deadline ⇒ `(delay, False)`；预算够 ⇒ 原样；预算不够 ⇒ `(remaining, True)`——只等剩余预算即放弃，不再开下一次尝试。
- **[deadline 来源唯一] `RequestScope.deadline_at`**：domain 是依赖链最底层（`test_p17` 守着方向），故由 application 的 `request_scope` 在导入期把读取函数**注册**进 domain（`set_remaining_provider`）；`remaining_seconds()` 读不到 provider / 读不到 deadline / provider 故障 ⇒ `None`（= 回基线，纪律 2）。
- **[三层接入]** 检索（`aretry`，余量不足时给抛出的异常打 `retry_truncated` 标，类型与消息不变）、task（`run_task`，返回的 `TaskOutcome.truncated=True`）、工具（`ToolExecutor`，新增 `ToolResult.truncated` 字段，与「输出被截断」不是一回事）。
- **[新增守卫] `tests/test_p159_retry_deadline_budget.py`（16 条）**：`budgeted_pause` 五种取值、deadline 来源单一与故障降级、三层各自「剩余不足时不进入下一次尝试 + truncated 可见」、三层各自「无 deadline 时跑满 max_attempts（基线等价）」。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十四段读数；`docs/项目实现手册.md` 补预算感知一段。

### LangGraph 原生对齐 · LG-2-7 启动期崩溃恢复的并发与总预算（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P2 第 7 条：启动期的崩溃恢复此前是**串行 for 循环**——N 个待恢复线程逐个 `ainvoke`，单个卡死就把启动拖到天荒地老，恢复总耗时随 N 线性增长。

- **[并发上限] `RECOVERY_MAX_CONCURRENCY = 2`**：恢复用 `asyncio.Semaphore(2)` 限流，线程多时分批推进，不把所有线程同时压进图执行（每个恢复是一个完整图跑，同时起 N 个等于把启动期 CPU/Redis 连接打满）。
- **[总预算] `RECOVERY_TOTAL_BUDGET_S = 30.0`**：整段「扫待恢复清单 + 逐个恢复」包在 `asyncio.wait_for` 里；超预算即告警收口，剩余线程留给下次启动或 `POST /ops/threads/resume`——**启动不因恢复慢而失败**。
- **[单线程预算] `settings.AGENT_TIMEOUT`**：每个线程的 `resume_thread` 单独 `wait_for`，一个卡死只丢它自己（`logger.warning` 留痕），不拖住同批其他线程；`ThreadInterruptedError` 仍按 LG-2-2 口径跳过（等人工裁决的线程不硬续）。
- **[保留原有口径]** 待恢复清单仍由 `get_active_threads(lg_builder.graph, min_idle_seconds=60)` 派生（LG-2-1），图仍**动态读** `lg_builder.graph` 属性（P0-1：值绑定会在 Redis 图覆盖前固化旧图）；恢复失败/扫描异常仍只告警不阻断启动。
- **[新增守卫] `tests/test_p158_startup_recovery_bounds.py`（11 条）**：3 线程 2 并发分批（峰值并发恰为 2、总耗时显著小于串行）、耗时随批次而非线程数增长、单线程卡死不拖累其他、总预算封顶整段恢复且未完成线程留给下次、被中断线程与「无产出」不计入 recovered、扫描异常不破启动、恢复读到的是模块属性而非导入期快照、常量数值与文档一致、`_startup` 确实调用恢复入口（AST）、三个 helper 必须是模块级（AST，供 p47/p153 的静态守卫口径复用）。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十三段读数；`docs/项目实现手册.md` 补启动恢复限界一段。

### LangGraph 原生对齐 · LG-2-6 `durability` 参数化（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P2 第 6 条：checkpoint 何时落盘此前由框架默认档决定（`async`：每步后台写、下一步不等），运维无法要求「每步强制等落盘」。

- **[新增配置] `GRAPH_DURABILITY`**（`Literal["sync", "async", "exit"]`，默认 `async`）：`sync` = 每步写完 checkpoint 才走下一步；`async` = 每步后台写、下一步不等（**框架默认**，与升级前行为一致，**不是可靠性升级**）；`exit` = 只在退出 / 中断 / 报错时写。非法取值在 Settings 实例化（启动）即抛 `ValidationError`，不静默回落。
- **[单一透传点] `graph_config.durability_kwargs()`**：默认档返回空 dict——框架在 `durability=None` 时按同一默认值解析，调用形状与基线逐位相同（纪律 1）；「默认值」只取自 Settings 字段自己的默认，不另写字面量。
- **[四处调用点]** chat 的非流式 ask（`ainvoke`）与流式（`astream_events`，经 `**kwargs` 下传给 `astream`）、`lg_builder.resume_in_loop`、`breakpoint_resume.resume_thread`（定点重跑复用后者）。
- **[实测口径]** 1.2.18 下 `durability="exit"` 让同一条两节点图的历史从 4 条降为 1 条（`astream_events` 同样生效）；慢速 saver（每次写延迟 50ms）下，`sync` 档第 3 个节点能读到本轮前两步的 checkpoint，`async` 档读不到——两种读数同一条用例钉住。
- **[新增守卫] `tests/test_p157_graph_durability.py`（18 条）**：默认 async 与默认值单一来源、四种非法取值启动即报错、默认档空 kwargs、非默认档透传、四处调用点实际收到 kwargs、默认档不发参数、`sync`/`async` 落盘可见性双读数、`exit` 原生生效、三处调用点不得内联 `durability=` 字面量（AST）。
- **[配置五处同步]** `settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `docs/architecture/配置管理.md` §5 / `test_p41_doc_consistency.py` 手写清单。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十二段读数；`docs/项目实现手册.md` 补 durability 一段。

### LangGraph 原生对齐 · LG-2-5 历史回看端点（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P2 第 5 条：线程走到哪一步、每步 `next` 是谁，此前只能直接读 checkpoint 或翻日志。

- **[新增] `GET /ops/threads/{tid}/history?limit=50`**（`limit` 1–500）：数据源是 checkpointer 自己的 `get_state_history`，返回步骤序列（新→旧），每步投影为 `checkpoint_id` / `step` / `source` / `created_at` / `next` / `tasks`（节点名）/ `interrupts`（计数）——不携带 `values` 与 interrupt 载荷。
- **[只读] 不写任何状态**：方法只调读接口；用例断言调用前后 `get_state` 的 `checkpoint_id` 不变、历史首条即当前快照、步骤条数不增。
- **[口径] 步骤序列与图实际走过的超级步同源**（纪律 6 ④）：不读 Redis 元数据、不另造轨迹；线程无 checkpoint ⇒ 404；历史读取失败向上抛出（500），不把「读失败」报成「没有历史」。
- **[服务层] `breakpoint_resume`**：新增 `list_thread_history(thread_id, graph, *, limit=50)` 与 `_history_summary` / `_state_history`；后者与 `_state_snapshot` 同一取舍——优先 `aget_state_history`，同步专用桩退回 `get_state_history`。
- **[新增守卫] `tests/test_p156_thread_history.py`（11 条）**：步骤条数与原始历史逐条对齐（含 input 那条 `step=-1`）、只读性、`limit` 切片、投影字段集合、未知线程 None、同步桩回退分支、路由注册、端点 200/404/limit 上下界 422。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十一段读数；`docs/项目实现手册.md` 补历史回看一段。

### LangGraph 原生对齐 · LG-2-4 按 task 的定点查看与重跑（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P2 第 4 条：失败超级步此前只能整步续跑，运维看不到「哪条 task 失败、能不能只重跑它」。

- **[列表] `GET /ops/threads/{tid}/tasks`**：从图快照 `snapshot.tasks` 投影 task 摘要（`task_id` / `name` / `path` / `error` / `interrupts` 计数 / `has_result`），不携带 interrupt 载荷（避免 PII 下发）；线程无 checkpoint ⇒ 404，已完成线程 ⇒ 空表。
- **[定点重跑] `POST /ops/threads/{tid}/retry-task`**（body `{"task_id": ...}`）：目标 task 必须是当前待执行项；已完成（有结果且无 error）⇒ **409 `task_not_retryable`**；未知线程/未知 task ⇒ **404 `pending_task_not_found`**；挂起中断 ⇒ **409 `thread_interrupted`** 并指向 `/ops/hitl/in-loop`；成功 ⇒ 200 `{"status": "retried", ...}`。
- **[只重跑失败的那条] 机制取自 checkpointer 自己的台账**：同一超级步里已完成兄弟 task 的写入已落盘（`pending_writes`），`ainvoke(None)` 只重跑没有落盘写入的 task ⇒ 兄弟不重放。计数桩用例实测：首次失败跑 `good, bad`，重跑后为 `good, bad, bad`，`good` 只执行 1 次。
- **[与指导文档的偏差，附实测]** 文档建议用 `update_state(task_id=...)` 做这件事。实测 1.2.18 对失败超级步调 `aupdate_state(..., task_id=<失败 task>)` 会把整步收敛成这一条 update：兄弟 task 已落盘结果连同 `next` 一起被丢弃（`done` 从 `["good"]` 变空、`next` 变 `()`）。拿它做「重试」会损坏状态，因此实现走原生 resume，并把偏差与证据写进服务层 docstring。
- **[服务层] `breakpoint_resume`**：新增 `list_pending_tasks` / `retry_task` 与两个异常 `PendingTaskNotFoundError` / `TaskNotRetryableError`；重跑复用 `resume_thread`（沿用 LG-2-2 的挂起 409 与 LG-2-3 的统一 config）。
- **[新增守卫] `tests/test_p155_task_point_retry.py`（9 条）**：快照投影、未知线程、重跑只重跑失败 task（计数桩）、已完成 task 拒绝重放且状态不变、未知 task/线程 404、路由注册、端点列表→409→200→404 全链、挂起线程 409 指向 hitl。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第十段读数；`docs/项目实现手册.md` 补定点重跑一段。
- **[验证]** 定向 `test_p155 + p152 + p153 + p37 + p64` → **48 passed**；守卫批 `p81/p136/p66/p101/p22/p147/p154` → **149 passed**；`ruff check agentix tests` → `All checks passed!`；`mypy agentix/domain` → `Success: no issues found in 38 source files`；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5344 passed / 40 skipped / 4 deselected / 0 failed**（415.72s；相对 LG-2-3 的 5335 恰好 **+9**，即本条新用例数，既有 5335 条零翻转）。

### LangGraph 原生对齐 · LG-2-3 `recursion_limit` 统一（2026-10-28）

《LangGraph 原生对齐与恢复链优化》P2 第 3 条：`recursion_limit` 是 LangGraph 的**顶层** config 键，此前只有 `chat_service` 两条入口显式写出；恢复路径（`lg_builder.resume_in_loop` / `breakpoint_resume.resume_thread`）只带 `configurable.thread_id` ⇒ 落到框架默认 **25**。同一张图，主链路按 `MAX_ITERATIONS`（默认 10）截断，恢复请求却能多跑一倍多——两条路径的轮数预算各说一套。

- **[单一构造点] 新增 `agentix/orchestration/langgraph/graph_config.py`**：`thread_config(thread_id, **configurable)` 产出 `{"configurable": {"thread_id": ...}, "recursion_limit": int(settings.MAX_ITERATIONS)}`；轮数预算每次调用现读配置（运行期改值即生效），config 形状只在该模块定义。
- **[三处接入]** `lg_builder.resume_in_loop`、`breakpoint_resume`（`get_thread_snapshot` / `get_active_threads` / `resume_thread` 三处同形状构造一并收口）、`chat_service`（`ask` 与 `stream_events` 两条入口）全部改走 `thread_config()`。`configurable` 里的其余键（`image_path` / `file_path` / `incremental` / `hitl_in_loop` 等）仍由调用方按需追加，行为不变。
- **[行为变化]** 恢复请求的轮数上限从框架默认 25 回到 `MAX_ITERATIONS`（默认 10）。这不是"恢复变窄"而是与主链路同预算：需要超过 10 步才能完成的恢复，本来在主链路上也跑不完。无挂起/正常恢复路径逐位等价（p152/p153 同批全绿）。
- **[既有守卫按新口径更新]** `tests/test_p133_decision_span_attributes.py` 原来断言 `resume_in_loop` 的 config **恰好**是 `{"configurable": {"thread_id": "t-1"}}`；config 现在合法地多出顶层 `recursion_limit`，改为断言 `configurable` 恰为 `{"thread_id": "t-1"}` **且** `recursion_limit == settings.MAX_ITERATIONS`（意图不变、判据不弱化）。
- **[新增守卫] `tests/test_p154_recursion_limit_unified.py`（10 条）**：① 形状与顶层键；② 现读 `MAX_ITERATIONS`（7/3 两次）；③④⑤⑥ 四处调用方下发的 config 均带统一预算；⑦ 真图行为：动态 `interrupt` 恢复后自环，`MAX_ITERATIONS=3` 时抛 `GraphRecursionError` 且 spin 次数远小于 24（证明不是 25）；⑧⑨⑩ AST 守卫：三个调用方都经 `thread_config`、不再有把 `recursion_limit` 当 dict 字面量键的写法。
- **[文档同步]** `README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第九段读数；`docs/项目实现手册.md` 补 config 单点一段。
- **[验证]** 定向 `test_p154 + test_p133 + test_p02` → **46 passed**；恢复/端点/架构批 `p152/p153/p37/p64/p81/p136/p66/p101/p22/p147` → **178 passed**；`ruff check agentix tests` → `All checks passed!`；`mypy agentix/domain` → `Success: no issues found in 38 source files`；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5335 passed / 40 skipped / 4 deselected / 0 failed**（422.20s；相对 LG-2-2 的 5325 恰好 **+10**，即本条新用例数，既有 5325 条零翻转）。

### LangGraph 原生对齐 · LG-2-2 续跑前判挂起，命中即 409（2026-10-27）

《LangGraph 原生对齐与恢复链优化》P2 第 2 条：`/ops/threads/resume` 此前对「挂着未决中断」的线程照旧 `ainvoke(None)`，结果是 **200 但图又挂回去**——把「需要人裁决」报成「已恢复」。

- **[判据] `agentix/application/services/breakpoint_resume.py`**：新增 `ThreadInterruptedError(thread_id, pending)` 与唯一判据 `_pending_interrupts(snapshot)`（`snapshot.interrupts` 非空）。`resume_thread` 在拿到快照后、落 span 与 `ainvoke` 之前抛出该异常；`except ThreadInterruptedError: raise` 先于通用 `except`，**不写 failed 元数据、不动图状态**（治理层等待 ≠ 崩溃）。
- **[状态码] `agentix/interfaces/http/ops_router.py` 的 `/ops/threads/resume`**：捕获后回 **409**，`detail` 带 `error=thread_interrupted` / `pending_interrupts` / `resume_endpoint=/ops/hitl/in-loop`；404（无 checkpoint）与 500（续跑失败）口径不变。
- **[启动恢复] `agentix/main.py`**：`_startup` 的恢复循环逐线程捕 `ThreadInterruptedError`，`logger.info` 跳过并继续——否则一条待裁决线程会让整轮恢复中断（该线程仍是 `/ops/active-threads` 里的可见项）。
- **[不动静态暂停]**：`interrupt_before=[...]` 的暂停 `snapshot.interrupts` 为空、只有 `next` 非空 ⇒ 照旧可续跑（有回归用例钉住，409 不误伤）。
- **[新增守卫] `tests/test_p153_resume_interrupted_conflict.py`（5 条）**：① 节点内 `interrupt()` 造挂起 ⇒ `resume_thread` 抛异常、`checkpoint_id` 不变、后续节点未执行、Redis 无 failed 元数据；② 端点 409 且指向 `/ops/hitl/in-loop`；③ 静态 `interrupt_before` 线程照旧 200 完成；④ 未知线程仍 404；⑤ `main.py` 的 except 接线有 AST 守卫。
- **[行为变化]** 唯一变化是「挂着未决中断的线程」从假的 200 变成 409；无挂起路径逐位等价（p152 的真 `MemorySaver` 续跑用例同批全绿）。
- **[文档同步]** `docs/项目实现手册.md` 补 409 口径一段；`README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第八段读数。
- **[验证]** 定向 `test_p153 + test_p152 + test_p133` → **29 passed**（5 + 11 + 13）；端点/启动/架构批 `p37/p64/p81/p136/p66/p101` → **109 passed**；`ruff check agentix tests` → `All checks passed!`；`mypy agentix/domain` → `Success: no issues found in 38 source files`；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5325 passed / 40 skipped / 4 deselected / 0 failed**（429.52s；相对 LG-2-1 的 5320 恰好 **+5**，即本条新用例数，既有 5320 条零翻转）。

### LangGraph 原生对齐 · LG-2-1 待恢复线程改由 `alist()` 派生（2026-10-27）

《LangGraph 原生对齐与恢复链优化》P2 第 1 条：断点续跑此前**恒为空转**——`active_threads` 这个 Redis 集合在生产上没有写入点（唯一写方 `save_checkpoint_metadata` 只在恢复路径被调用，自举不成立），于是启动恢复与 `/ops/active-threads` 一直返回空。本轮把「哪些线程未完成」交回 checkpointer 自己回答。

- **[换源头] `agentix/application/services/breakpoint_resume.py`**：`get_active_threads()` 改为 `async def get_active_threads(graph, *, min_idle_seconds: float = 0.0)`，线程清单由 `checkpointer.alist(None, before=..., limit=THREAD_SCAN_PAGE_SIZE)` **有界翻页**枚举（200 条/页 × 5 页上限），「未完成」由图自己的快照 `next` / `interrupts` 判定——判据与执行链同源（纪律 6 ④）。**不读 checkpoint 元数据的 `writes`**：2026-10-27 实测「跑完」与「断在中途」两类线程的最新 checkpoint 都是 `pending_writes=[]`，用它判会把两类全判成完成。
- **[删除死代码] `active_threads` 集合**：`save_checkpoint_metadata` 里的 `sadd` / `srem` 与旧 `get_active_threads` 里的 `smembers` 删除；Redis 元数据写入保留（仅簿记，供 `resume_thread` 日志补 `last_node`）。
- **[续跑判据] `resume_thread` 不再要求 Redis 元数据**：前置检查换成新增的 `get_thread_snapshot()`（无 checkpoint / 空状态 ⇒ `None`；读失败降级为 `None` 并 warning）；`/ops/threads/resume` 的 404 同步改由快照判定——原先按元数据判 404 会把 alist 派生出的**所有**线程挡在门外。元数据读失败降级为空字典，只影响日志里的 node 名（纪律 2）。
- **[启动恢复] `agentix/main.py`**：改为 `get_active_threads(lg_builder.graph, min_idle_seconds=60)`；60 秒空闲闸的时间轴从 Redis 元数据 `updated_at`（无生产写入点）换成 checkpoint 快照自带的 `created_at`，意图不变（不打断此刻仍在跑的线程）。
- **[fail-open] `alist` 与单线程快照失败**：`alist` 异常捕获后 `logger.warning` 并返回已扫到的部分（首页失败即空列表 + 告警），**不静默返空**（验收③）；单线程快照读失败只跳过该线程；checkpointer 未装配（`init_checkpointer` 未调用）同样告警后降级为空。
- **[新增守卫] `tests/test_p152_pending_threads.py`（11 条）**：①②④ 用真 `MemorySaver` + `interrupt_before` 造「1 个已跑完 + 1 个停在中途」，断言只列后者、续跑完成后不再列出；③ 桩 saver 的 `alist` 抛错 ⇒ 空列表 + warning；⑤ 450 条 checkpoint ⇒ 恰好 3 页且每次 `limit=THREAD_SCAN_PAGE_SIZE`、第 2/3 页 `before` 指向上一页末条，翻页到 `THREAD_SCAN_MAX_PAGES` 即停；⑥ AST 守卫：模块里不再有 `sadd` / `srem` / `smembers` 调用、不再拼 `active_threads` 键（解释性注释不算犯例）；⑦ `min_idle_seconds=60` 过滤刚写过的线程；另钉「无元数据可续跑」「未知线程返回 None」「单线程快照失败不影响其余」「元数据写入保留」。
- **[行为变化与回滚]** 变化是**恢复面从「恒空」变为「有候选」**：`/ops/active-threads` 开始返回真实线程，启动恢复开始真正尝试续跑（受 60 秒空闲闸约束）。无新开关——这是把死代码接回执行链，不是可选能力；回滚为 `git revert` 本条提交。
- **[文档同步]** `docs/项目实现手册.md` 补恢复链来源一段；`README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第七段读数。
- **[验证]** 定向 `test_p152 + test_p133` → **24 passed**（11 + 13）；端点/启动/架构批 `p37/p64/p81/p136/p126 + app_error_handler + v1_endpoint_auth + phase23 + p52/p82 + p66` → **242 passed / 10 skipped**；`ruff check agentix tests` → `All checks passed!`；`mypy agentix/domain` → `Success: no issues found in 38 source files`；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5320 passed / 40 skipped / 4 deselected / 0 failed**（459.53s；相对 LG-1-4 的 5309 恰好 **+11**，即本条新用例数，既有 5309 条零翻转）。

### LangGraph 原生对齐 · LG-1-4 打开预算桶（部署面）（2026-10-27）

《LangGraph 原生对齐与恢复链优化》P1 第 4 条：把 Phase H·W3 已有的 run 级重试预算桶推进到部署面。**代码零行为变更**——`RETRY_BUDGET_ENABLED` 的 Settings 默认仍是 `False`（纪律 1），本轮只让 `.env.example` 与 staging override 说出推荐值。

- **[部署面·推荐值] `.env.example`**：`RETRY_BUDGET_ENABLED=false → true`，注释写明「代码默认仍是 false；这里写 true 是部署推荐值——桶只收紧重试放大、不新增任何调用，耗尽即停；额度由 `RETRY_BUDGET_TOKENS` 定（首 attempt 不占额度）」。这不是把开关默认开：`Settings` 字段默认值未动，不设环境变量的基线路径逐位等价。
- **[部署面·预发] `docker/compose.override.staging.yml`**：backend 显式钉 `RETRY_BUDGET_ENABLED: "true"` 并署名理由（LG-1-4）；**不另钉 `RETRY_BUDGET_TOKENS`**——它没有被 compose 转发，容器拿的就是字段默认 6，两处写反而多一份口径。
- **[生成物] 配置矩阵多一行**：`docs/runbooks/预发环境与配置矩阵.md` 的 dev=`false`（compose 未转发 ⇒ 回落字段默认）、staging=**`true`**（加粗 = 预发真的设了）、production 列手写推荐与理由；`python scripts/gen_staging_matrix.py --check` 退出 0，读数从「16 行由 override/Settings 生成，1 行登记为手填」变成 **17 + 1**。
- **[新增守卫] `tests/test_p151_retry_budget_deployment.py`（4 条）**：① 读 `Settings.model_fields`（不实例化，避免宿主机 `.env` 污染）断言默认仍 `false`、令牌默认仍 6；② `.env.example` 写 `RETRY_BUDGET_ENABLED=true` 且上方注释含「推荐」「不新增」两条理由；③ staging override 真设 `"true"` 且注释署名 LG-1-4；④ 配置矩阵该行由 `generated_rows()` 算出（dev=false / staging=true / 加粗）且 `check()` 干净。**行为面不重复断言**：flag-on 下三层共享 token、耗尽即停、分类审计由 `tests/test_p97_runtime_control.py::TestRetryBudget` 既有用例覆盖（42 条，本轮同批实跑全绿）。
- **[flag-off 等价]** 本轮不改任何执行路径；`RETRY_BUDGET_ENABLED=false` 时 `ensure_retry_budget()` 不被调用（`agentix/reliability/retry_budget.py` 的懒建口径），p97 的 `test_task_layer_flag_off_keeps_full_attempts` / `test_tool_layer_flag_off_retries_any_error` / `test_retrieval_layer_flag_off_keeps_retries` 仍按基线读数（3 次 / 3 次 / 2 次）通过。
- **[文档同步]** `docs/architecture/配置管理.md` §5 该行补「代码默认仍 false / 部署面推荐 true / 回滚即改回 false」；`docs/项目实现手册.md` 补部署姿态一段；`README.md` §11 基线与口径链 re-cite；`docs/工程口径日志.md` 追加第六段读数。
- **[验证]** 定向 `test_p151 + p97 + p115 + p41` → **144 passed**（4 + 42 + 44 + 54）；`ruff check agentix tests` → `All checks passed!`；`mypy agentix/domain` → `Success: no issues found in 38 source files`；`python scripts/gen_staging_matrix.py --check` 退出 0（`配置矩阵与部署物一致：17 行由 override/Settings 生成，1 行登记为手填`）；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5309 passed / 40 skipped / 4 deselected / 0 failed**（457.79s；相对 LG-1-3 的 5305 恰好 **+4**，即本条新用例数，既有 5305 条零翻转）。

### LangGraph 原生对齐 · LG-1-3 三层自研退避与原生默认同源（2026-10-27）

《LangGraph 原生对齐与恢复链优化》P1 第 3 条：tool / task / retrieval 三层各自一套退避数值收敛到同一份真源，与 `RetryPolicy` 默认逐项相同。

- **[新增开关] `RETRY_BACKOFF_ALIGNED`（默认 `False`）**：对齐是**行为变更**（工具层 0.2s→0.5s 且加抖动、task 上界 8s→128s、检索换成原生默认），按 `AGENTS.md` 纪律 1 走「新 flag + 默认关」；关时三层返回各自历史数值，flag-off 与基线逐位等价。五处同步齐全（`settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `配置管理.md` §2 开关清单 + §5 表 / `test_p41_doc_consistency.py` 手写清单）。
- **[真源] `agentix/domain/policies/retry.py`**：新增 `RETRY_INITIAL_INTERVAL=0.5` / `RETRY_BACKOFF_FACTOR=2.0` / `RETRY_MAX_INTERVAL=128.0` / `RETRY_MAX_ATTEMPTS=3` / `RETRY_JITTER=True` 与 `RetryBackoff`（`delay(n)=min(initial·factor^n, max_interval)`，抖动系数 ≤1 ⇒ 抖动后不越上限）；`aretry` 的默认值改由这些常量给出。domain 零第三方依赖仍是硬约束 ⇒ 数值是**抄本**，失真由用例逐项比对 `RetryPolicy()` 钉住（框架改默认值时先红）。`agentix/orchestration/langgraph/policies.py` 只**转出**同一批对象（`is` 同一性有用例），不另抄一份——langgraph 导入文件 equality 棘轮不扩容。
- **[接线·工具层] `agentix/tools/gateway/executor.py`**：删掉无上限的 `await asyncio.sleep(0.2 * (2**attempt))`，改走 `backoff.delay(attempt)`。128s 上限在两种开关状态下都生效，但 `ToolSpec.max_retries` 默认 2（仓内无工具覆盖）⇒ 可及等待 ≤0.8s、上限**不绑定**，flag 关实得 sleep 序列仍是 `0.2 / 0.4`（有用例实测，不是只读参数）。
- **[接线·任务层] `agentix/application/services/task_retry.py`**：`run_task` 的 `base_delay` / `max_delay` / `jitter` 改为 `Optional[...] = None`，缺省取 `task_retry_backoff()`；显式传参仍然优先（`run_batch` 透传语义不变），`max_attempts` 仍由调用方决定。`DEFAULT_MAX_ATTEMPTS` 改为引用 `RETRY_MAX_ATTEMPTS`（数值仍 3）。
- **[接线·检索层] `kb_retrieval.py`**：两个通道的 `@aretry(...)` 改为 `@aretry(**_RETRIEVAL_RETRY_PARAMS)`，装饰期快照由 `retrieval_retry_backoff()` 产出。**开开关时 `KB_RETRIEVAL_MAX_ATTEMPTS` / `KB_RETRIEVAL_RETRY_BASE_DELAY` 不再生效**（整组取原生默认，含 `max_attempts=3`）——这是「与原生默认逐项相同」的直接后果，已在配置文档与函数 docstring 里写明，不是静默忽略。
- **[新增守卫] `tests/test_p150_retry_backoff_alignment.py`（19 条）**：① 常量与 `RetryPolicy()` 默认逐项相同、`RetryBackoff()` 即原生默认形状、编排层转出的是**同一对象**；② 三处参数快照——开时逐项等于原生，关时逐项等于历史值（检索层用 monkeypatch 固定 `KB_RETRIEVAL_*`），并反向断言开关确实让三层**都**变了（防空转）；③ 工具层源码不再含 `0.2 * (2**attempt)`；真实 `ToolExecutor` 的 sleep 序列关时 `[0.2, 0.4]`、开时落在原生 0.5/1.0 的抖动区间；④ task 层真实 sleep 序列：开时 `[0.5, 1, 2, 4, 8, 16]`（越过旧 8s 上界），关时 `[0.5, 1, 2, 4, 8, 8]`，显式传 `base_delay=0.01` 仍生效；⑤ 检索层：AST 断言两个装饰器都展开模块级快照，并在打开开关后 `importlib.reload` 证明装饰期实参确实重算为 4 项原生默认。
- **[文档同步]** `README.md` §11 基线 re-cite 到本轮读数；`docs/项目实现手册.md` §7 补三层退避口径；`docs/工程口径日志.md` 追加第五段读数；`docs/architecture/配置管理.md` §2/§5 与 `.env.example` 补键（后三份含上一轮文档中文化产出的**未跟踪新文件**，本轮只在其内就地补行）。
- **[验证]** 全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5305 passed / 40 skipped / 4 deselected / 0 failed**（434.82s；相对 LG-1-2 的 5286 恰好 **+19**，即本条新用例数，既有 5286 条零翻转）；专项 `test_p147 + p148 + p149 + p150 + p41 + p17 + llm_retry + p02` → **187 passed**；`test_p33 + p54 + p97 + p15 + p66 + p101` → **209 passed**；`ruff check agentix tests` → `All checks passed!`；`mypy agentix/domain` → `Success: no issues found in 38 source files`；`python scripts/gen_staging_matrix.py --check` 退出 0；`pip check` → `No broken requirements found.`。既有守卫零删除、零判据放宽。

### LangGraph 原生对齐 · LG-1-2 `is_retryable` → `retry_on`（2026-10-27）

《LangGraph 原生对齐与恢复链优化》P1 第 2 条：让「哪些异常值得重试」只有一份判定，框架按业务口径而不是框架默认口径重试。

- **[接线] `readonly_retry_policy()` 的 `retry_on` 换成业务分类**：`agentix/orchestration/langgraph/policies.py` 从 `agentix.domain.policies.retry` 导入 `is_retryable` 直接作为回调（**同一份实现**，不是包装副本），数值参数仍全部由 `RetryPolicy` 自身给出。`agentix/domain/policies/retry.py` 把 `is_retryable` 列入 `__all__` —— 转出点在此，方向是编排层依赖领域层，领域层不知道 LangGraph 存在。显式传 `retry_on=` 仍可覆盖（`setdefault` 语义），其余字段覆盖不受影响。
- **[实测差异表] 原生 `default_retry_on` 与业务口径有四处不一致**（全部钉成参数化用例）：

| 异常 | 原生默认 | 业务 `is_retryable` | 业务理由 |
|---|---|---|---|
| `httpx.HTTPStatusError(429)` | `False`（只认 5xx） | **`True`** | 限流是瞬时故障 |
| `TimeoutError` | `False`（它是 `OSError` 子类，落在原生「不重试」清单） | **`True`** | 超时是最典型的瞬时故障 |
| `openai.AuthenticationError`（401） | `True`（未知即重试） | **`False`** | 重试换不来鉴权，只会放大故障 |
| 未知异常（自定义 `Exception` 子类） | `True` | **`False`** | 重试换不来缺陷修复 |

- **[新增守卫] `tests/test_p149_retry_on_business.py`（14 条）**：① 9 条参数化「逐条同值」——`is_retryable(exc)`、`readonly_retry_policy().retry_on(exc)` 与写死的期望列三方一致（期望列写死而非取自被测函数，避免判据给自己当基准）；② `retry_on is is_retryable`（同一份实现）；③ 覆盖其它字段不丢 `retry_on`、显式传 `retry_on` 才替换；④ 真图（flag-on）6 个只读节点的 `retry_on` 都是业务分类且**不是** `default_retry_on`（对应验收「断言实际生效的是自定义 retry_on」）；⑤ **行为证明**：`httpx 429` 在原生默认下 1 次即抛、在业务策略下耗尽 3 次尝试，未知异常反之（1 次 vs 3 次）——分类确实驱动框架行为。
- **[既有守卫随事实更新，非放宽]** `test_p147` 的「与原生默认逐项相等」改为「换回原生 `retry_on` 后逐项相等 + `retry_on is is_retryable`」——例外被钉死在明面上，数值反抄本守卫不变；`test_p148` 把「登记 401 会被原生重试 3 次」的缺口用例改为**双读数对照**（原生 3 次 / 业务 1 次），断言强度不变，缺口的另一侧从"待 LG-1-2"变成"已收口"。
- **[行为面]** 默认开关 `NODE_RETRY_ENABLED=false` ⇒ 生产链路零变化；只有显式打开时，重试判定才从框架默认切换为业务分类（这正是本条的目的）。
- **[验证]** `pytest test_p149 + test_p147 + test_p148` → **44 passed**；`ruff check agentix tests` → `All checks passed!`；`mypy agentix/domain` → `Success: no issues found in 38 source files`；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5286 passed / 40 skipped / 4 deselected / 0 failed**（432.12s；相对 LG-1-1 的 5272 恰好 **+14**，即 p149 的用例数，既有 5272 条零翻转）。

### LangGraph 原生对齐 · LG-1-1 只读节点接 `retry_policy`（2026-10-27）

《LangGraph 原生对齐与恢复链优化》P1 第 1 条：把「瞬时故障由框架原地重试」这件事交给原生 `add_node(retry_policy=)`，而不是让整轮对话失败。

- **[新增开关] `NODE_RETRY_ENABLED`（默认 `False`）**：指导文档没点名 flag，但接线是**行为变更**，按 `AGENTS.md` 纪律 1 必须「新 flag + 默认关」。关时 `readonly_retry_kwargs = {}` ⇒ 注册处**完全不传**该形参，与基线逐位等价。五处同步齐全（`settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `配置管理.md` §2 开关清单 + §5 表 / `test_p41_doc_consistency.py` 手写清单）。
- **[接线] `lg_builder.py` 节点注册段**：恰好 6 个只读节点挂 `**readonly_retry_kwargs`——`analyze_and_route_query` / `respond_to_general_query` / `create_research_plan` / `create_kb_query` / `create_image_query` / `create_file_query`；策略实参来自 LG-0-3 的集中点 `policies.readonly_retry_policy()`（逐项等于 `RetryPolicy()` 默认），本文件不内联构造。**`execute_task` / `code_agent` 无论开关都不挂**（整节点重跑会重放工具副作用，属指导文档铁律）；`message_window` / `get_additional_info` 不在 LG-1-1 清单内，维持不挂。
- **[新增守卫] `tests/test_p148_node_retry_wiring.py`（12 条）**：① flag-off 编译后 **10 个业务节点**读到的策略全为 `None`（逐位等价）；② flag-on 时上述 6 节点读到 1 元组 `(RetryPolicy(),)`，`message_window` / `get_additional_info` / `execute_task` / `code_agent` 仍为 `None`（对应验收②）；③ 真图行为：注入 `openai.RateLimitError` 后节点**恰好执行 2 次**并成功（对应验收①）；④ 一直 429 ⇒ 共 3 次尝试后抛 `openai.RateLimitError`（`max_attempts=3` 含首跑）；⑤ 策略是原生默认而非抄来的一组数字（`retry_policy == RetryPolicy()` 且 `retry_on is default_retry_on`）；⑥ 旧形参名 `retry=` 不在已安装 `add_node` 签名里（与 p147 同源）。
- **[登记一条不利事实] 原生默认与业务分类对同一批异常判定不一致（LG-1-2 的动因）**：`default_retry_on` 对 `httpx.HTTPStatusError(429)` **不重试**（源码只认 `500 <= status < 600`），但 `openai.RateLimitError` 不是它的子类 ⇒ 落到「未知即重试」分支被重试；反过来 `openai.AuthenticationError`（401）**会被重试 3 次**，而业务 `is_retryable` 判 False（重试换不来鉴权）。两条漂移都钉成用例（`TestRetryOnSourceOfTruthGap` 与 `test_native_default_retries_auth_error_this_is_the_gap_lg_1_2_closes`），LG-1-2 把 `retry_on` 换成 `is_retryable` 后后者读数应变成 **1 次即抛**，用例注释里写明届时如何改读数而**不是**把红改绿。
- **[文档同步]** `README.md` §11 基线 re-cite 到本轮读数；`docs/项目实现手册.md` 与 `docs/architecture/配置管理.md` §5 各补该键一行（后两份是上一轮文档中文化产出的**未跟踪新文件**，本轮只在其内就地补行）。
- **[注册一处未做项]** 编译图谱警告 `LangGraphDeprecatedSinceV05: input is deprecated … use input_schema`：指导文档 §4.1 明确本轮只用 `retry_policy` / `error_handler` / `timeout`，`input_schema` 属「其余留白」⇒ 本轮不改，告警保留。
- **[验证]** `pytest tests/test_p148_node_retry_wiring.py` → **12 passed**（11.01s）；`test_p41 + test_p101 + test_p132 + test_p66` → **72 passed**；`ruff check agentix tests` → `All checks passed!`（首次因两条 `pytest.raises(Exception)` 触 B017，改为精确异常类型后复跑通过）；`mypy agentix/domain` → `Success: no issues found in 38 source files`；`python scripts/gen_staging_matrix.py --check` 退出 0（`配置矩阵与部署物一致：16 行由 override/Settings 生成，1 行登记为手填`）；`pip check` → `No broken requirements found.`；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5272 passed / 40 skipped / 4 deselected / 0 failed**（430.31s；相对 LG-0-3 的 5260 恰好 **+12**，即本条新用例数，既有 5260 条零翻转）。既有守卫零删除、零判据放宽。

### LangGraph 原生对齐 · LG-0-3 策略集中（2026-10-27）

《LangGraph 原生对齐与恢复链优化》P0 第 4 条：把「节点策略从哪来」收敛到唯一入口。**零运行期行为变更**——本轮只立集中点与守卫，注册处一行策略都没挂（接线分属 LG-1-1 / LG-3-2 / LG-3-5）。

- **[新增] `agentix/orchestration/langgraph/policies.py`**：`readonly_retry_policy(**overrides)`（默认值全部由 `RetryPolicy` 自身给出，刻意不抄数字 ⇒ LG-1-3「与原生默认同源」由构造保证）、`node_timeout(run_timeout, idle_timeout, refresh_on)`（未启用返回 `None`，调用方据此**完全不传** `timeout`，这是 LG-3-2 口径①「flag-off 逐位等价」的接口前提；非正值一律按「关」处理）、`NATIVE_RETRY_DEFAULTS` 快照、`POLICY_KWARG_NAMES` / `LEGACY_POLICY_KWARG_NAMES` 两条形参名口径。
- **[新增守卫] `tests/test_p147_policy_centralization.py`（18 条）**：① 生产目录（`agentix/`、`scripts/`）不出现**由 `langgraph*` 导入而来**的 `RetryPolicy(` / `TimeoutPolicy(` 字面构造，唯一豁免是 `policies.py` 自身，且反向断言 `policies.py` 确实构造这两个类（否则豁免会退化成"把集中点掏空照样绿"）；② `add_node(...)` 的策略实参必须引用从 `policies.py` 导入的名字，`**` 展开除外（LG-3-2 用它实现"关时不传"）；③ 0.2 线旧形参名 `retry=` 出现即红（写了不生效、只会静默不重试）；④ 形参名口径与**已安装** `langgraph 1.2.18` 的 `add_node` 签名当场核对；⑤ 反空转：把判据跑在刻意违规 / 刻意合规 / 别名调用 / 同名自研四类样本源码上，必须一红一绿。**同名消歧是这条守卫的关键**：仓里另有自研 `agentix/retrieval/infra/tasks/reliable.py::RetryPolicy`（检索索引退避，消费者 `index_worker.py`、`scripts/migration/backfill_vectors.py`），判据按导入来源而不是字面名，未误伤既有代码。**编号避让**：`p146` 已被并发批次预留（`docs/design/审批与中断恢复优化规划.md:745` 的 `test_p146_golden_loud_exit`），本守卫取下一个空号 `p147`。
- **[守卫随批刷新，非弱化] `tests/test_p66_arch_guards.py` #11**：policies.py 导入 `langgraph.types` ⇒ langgraph 白名单 equality 棘轮按它自己注释写明的路径刷新（10 → **11 文件**），增行处署名理由与日期；棘轮本身仍是 equality，未加豁免。
- **[文档同步] 白名单计数与目录地图**：`README.md` §3（两处「仅登记在册 10 文件」→ 11）、§12 目录地图补 `policies 策略集中`；`docs/项目实现手册.md` §依赖边界（10 → 11 并写明 LG-0-3 增行理由）、`orchestration/` 模块清单与 §7 本体清单各补一处指针。`lg_builder.py` 注册段加 4 行口径注释（策略实参只能来自 `policies.py`）。
- **[验证]** `pytest tests/test_p147_policy_centralization.py` → **18 passed**；`test_p66 + test_p147 + test_p17 + test_p101 + test_p41` → **113 passed**（改白名单前 `test_p66` 的原始红：`新增=['agentix/orchestration/langgraph/policies.py']`）；`ruff check agentix tests` → `All checks passed!`；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5260 passed / 40 skipped / 4 deselected / 0 failed**（430.31s；相对 LG-0-2 的 5242 恰好 **+18**，即本条新守卫 `test_p147_policy_centralization.py` 的用例数，既有 5242 条零增删零翻转）。既有守卫零删除、零判据放宽。

### LangGraph 原生对齐 · LG-0-2 依赖迁移落地（2026-10-27）

《LangGraph 原生对齐与恢复链优化》P0 第 3 条：`langgraph` 0.2.60 → **1.2.18**，`langchain` 家族整族跟着走（1.2 线要求 `langchain-core>=1.4.7`）。只动依赖声明与因此翻转的守卫判据，**未动任何业务代码**。

- **[变更·依赖真源]** `requirements.txt` ↔ `pyproject.toml` **双声明同步**（`tests/test_dependency_consistency.py` 钉逐字一致；指导文档只点名 `requirements.txt`，但不同步 pyproject 就是让这条守卫红）：`langgraph 0.2.60→1.2.18`、`langgraph-checkpoint 2.0.26→4.3.1`、`langgraph-checkpoint-redis 0.0.8→0.5.2`、`langgraph-prebuilt 0.2.3→1.1.1`、`langgraph-sdk ≥0.1.42,<0.2.0 → ==0.4.10`、`langchain 0.3.30→1.4.7`、`langchain-core ≥0.3.85,<0.4.0→==1.7.0`、`langchain-community 0.3.27→0.4.2`（**无 1.x，0.4.2 是最后一版**，改依赖 `langchain-classic`）、`langchain-openai 0.2.14→1.7.3`、`langchain-neo4j 0.3.0→0.11.0`（其 METADATA 反向要求 `langgraph>=1.0.2`，正是「必须进 1.x」的硬约束）、`langchain-text-splitters ≥0.3.9,<0.4→==1.1.3`、`pydantic-settings 2.4.0→2.16.1`、`neo4j 5.27.0→6.4.1`、`numpy <2.0.0→>=2.0.0,<3.0.0`、`redis 5.0.1→>=5.2.1,<6.0.0`（**上界自己钉**，saver 只给下限）、`httpx 0.26.0→0.28.1`、`websockets 14.2→>=15,<17`、`pyyaml 6.0.1→6.0.3`、新增显式 `google-genai>=1.75.0,<2.0.0`（lightrag-hku 硬依赖，此前只靠传递）。import 路径零改动（LG-0-0 实测 11 个 `langchain_core` 子模块在 1.7.0 全部保留）。
- **[偏离指导文档 §3.3，三条，均有实测依据]** ① **`openai` 停在 2.x（实装 2.54.0），不进文档目标的 3.39.0**：`openai 3.39` 要 `jiter>=0.16`，而 dev/评测链的 `ragas 0.4.3 → instructor` 全系锁 `jiter<0.15`，整表 `pip install --dry-run -r requirements-dev.txt` 直接 ResolutionImpossible——铁律「评测不碰」优先于「追平 dry-run 清单」；`langchain-openai 1.7.3` 只要求 `openai>=2.45`，2.54 满足。② **`websockets >=15,<17`（实装 16.1.1），不是文档的 17.3**：`langsmith 0.14.13` 要 `>=15`，而 `google-genai` 只有 1.x 线（1.75.0）放宽到 `<17`，进 17 就得跨大版本换 Gemini 传输。③ `langsmith` 仍不显式钉（由 `langchain-core 1.7.0` 约束到 `>=0.14.9,<1.0`，实装 0.14.13，与文档「补丁号以当次实测为准」一致）。
- **[守卫同步，非弱化]** 全量首次 `2 failed / 5240 passed`（总数仍是 5242，即两条既有用例翻转），逐条归因后同步判据的**数据源**，断言内容一字未减：
  - `test_p24_deadcode_marking.py`：1.2 线 `get_graph()` 除 `__start__` 外还会画出 `__end__` 伪节点 ⇒ 新增 `FRAMEWORK_PSEUDO_NODES = {"__start__", "__end__"}`，把原先写死 `{"__start__"}` 的剔除集扩到两个；「业务节点恰好等于 `EXPECTED_NODES`」的等式判据不变，AST 那条（`_add_node_first_args(...) == EXPECTED_NODES`）未动。
  - `test_p0_s1_fixes.py`：1.2 线**不再画出「静态无入边」节点的出边**。`error_tool_selection` 只被 `tool_selection` 运行期 `Command(goto=...)` 命中，而该目标没写进它的 `Command[Literal[...]]` 注解 ⇒ 画法上整条链消失（0.2 线会画出来）。最小复现读数 `{'v': 1011}` = 1 + 10 + 1000 证明**运行期仍沿 `add_edge` 收敛**（不会挂起）⇒ 判据改读编译期路由表（`add_edge(a,b)` 落为 a 的 `ChannelWriteEntry(channel="branch:to:b")`），新增 `_compiled_destinations()` 助手，断言的不变量原文保留。
  - `test_dependency_consistency.py`：`langgraph-sdk` 见证版本 `0.1.74 → 0.4.10`，消费者约束文案随实装元数据改为 `langgraph==1.2.18 要求 (>=0.4.6,<0.5.0)`——这是该用例 docstring 自己写明的同步路径（「若未来升级 langgraph 且放开了 sdk 上界，本测试会红…请同步 pin 与见证版本」），元数据交叉核对那一条仍然照常判，未加豁免。
- **[能力回退·评测链，登记不修]** `ragas 0.4.3` 顶层 `from langchain_community.chat_models.vertexai import ChatVertexAI`，而 community 0.4.2 已移除该模块 ⇒ **`import ragas` 现在直接 ModuleNotFoundError**。`evals/ragas_report.py:137` 的前置检查按 `except Exception` 兜住 ⇒ 诚实降级 `exit 2`，不出假分数（`evals/` 与三个评测钉 `ragas/datasets/pyarrow` **一行未动**）；`pyarrow 21.0.0 + datasets 4.4.0` 在 numpy 2.5.4 下 import 实测通过。恢复 ragas 需动评测链，属本轮明确不做。
- **[环境修复，不入提交]** ① `langgraph-prebuilt 1.1.1` 装完是**半删状态**（RECORD 列 4 个 .py 不在盘上，命中 `TestNoEnvironmentStructuralDamage`），按守卫写明的方式 `pip install --no-deps --ignore-installed` 修好（不用 `--force-reinstall`：其卸载阶段会触发沙箱批量删除守卫）。② `agentix` 的 editable 元数据原指向**不存在**的 `D:\Code\practice_project\My-Agentix`，`pip check` 因此报 18 条冲突；`pip install -e . --no-deps` 重新注册后指向本仓，`pip check` → `No broken requirements found.`
- **[待办登记，本条不动]** ① `StateGraph(..., input=/output=)` 在 1.2 线是 `LangGraphDeprecatedSinceV05`（V2.0 移除），落点两处：`agentix/orchestration/langgraph/lg_builder.py:370`、`.../workflows/multi_agent/multi_tool.py:141` ⇒ 改 `input_schema/output_schema` 属后续任务。② `tool_selection` 的 `Command[Literal[...]]` 注解漏了运行期真实 goto 的 `error_tool_selection`（静态路由声明不完整，画法与真实可达不一致）⇒ 后续接原生 `destinations=` 时一并补。
- **[验证·全量]** `pip install -r requirements-dev.txt` 成功（`Successfully installed ... langgraph-1.2.18 ... openai-2.54.0 neo4j-6.4.1 numpy-2.5.4 redis-5.3.1 websockets-16.1.1 pydantic-settings-2.16.1 google-genai-1.75.0 ...`）、`pip check` 退出 0；`tests/test_dependency_consistency.py` **27 passed**、`test_p132_checkpointer_fail_closed.py` 4 passed、`test_p41_doc_consistency.py` 54 passed、`test_p101_integrity_guards.py` 5 passed、`test_p66_arch_guards.py` 9 passed；`ruff check agentix tests` → `All checks passed!`；`mypy agentix/domain` → `Success: no issues found in 38 source files`；`python scripts/gen_staging_matrix.py --check` 退出 0（`配置矩阵与部署物一致：16 行…1 行登记为手填`）；全量 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5242 passed / 40 skipped / 4 deselected / 0 failed**（425.04s，Python 3.12.13，单机离线）。与本轮起点基线 5242/40/4/0 逐位相同，用例总数未增未减。

### 文档分层收敛 · README 成为唯一「现行总览」（2026-10-27）

三份「总览」曾并存（`README.md` / `docs/项目实现手册.md` / `docs/architecture/overview.md`）：同一套架构讲三遍，改一次要同步三处，读者也无法判断哪句话还算数。本轮收敛为一层一份——**README 讲「现在是什么」，PROJECT_SPEC 讲「具体怎么实现」，`docs/archive/` 只留快照**。

- **[结构] README 吸收 overview 的独有内容**：§3 新增「三条端到端数据流」（三域 mermaid + 对照表）与「默认开关与能力矩阵」（默认开 / 默认关 / 五步判据）；§12 新增「关键代码索引」（「想找什么 → 看哪个文件」）。其余章节内容未动，只改指向。
- **[归档] `docs/architecture/overview.md` → `docs/archive/项目全景-归档快照.md`**：顶部加「已归档」横幅写明归档原因与内容去向；兄弟目录相对链接改指 `../architecture/`；尾部「维护约定」改为不再承担维护义务。新增 `docs/archive/README.md` 声明冻结快照规则：**只许改失效链接与归档说明，不更新事实内容**。
- **[定位] PROJECT_SPEC 降为「实现细节手册」**：标题由「项目说明书」改为「实现细节手册」，头部新增文档层级声明（与 README 冲突时以 README 为准）；§0.7 文档地图里指向 overview 的三处改指 README §3 / §12。
- **[纪律] `AGENTS.md` 新增纪律 8「文档分层不可回退」**：README 唯一总览 / PROJECT_SPEC 实现细节 / `docs/archive/**` 冻结；新规划文档必须带「文档状态」四选一（现行 / 进行中 / 已完成 / 历史）、不复述 README 已声明的事实、交付后转「已完成」、禁止新增第四份总览。
- **[状态] 27 份规划与优化文档加统一状态横幅**：现行 5（`optimization-roadmap` / `orchestration-rationale` / `design-legal-domain` / `prd-legal-domain` / `legal_domain_schema`）、进行中 1（`approval-recovery-optimization`）、已完成 7（`frontend-streaming-optimization` / `memory-optimization` / `multiagent-optimization` / `rag-optimization` / `wren-text2sql-integration` / `PhaseH` / `PhaseK`）、历史 14（两份阶段验证记录、四份 Phase 规划、`Agent_Core_2.0`、`current-tree`、`dependency-map`、`module-inventory`、`evolution-assessment`、`架构重构_统一执行链方案_v2`）。状态判据取各文档自述（「全部落地 / 蓝图存档 / 验证记录」），**未新增或改写任何事实陈述**。
- **[修复] 文档路径守卫首次全绿**：`tests/test_p41_doc_consistency.py` 从 1 failed / 53 passed → **54 passed**。两条被点名的引用（`evals/performance/results/` 下的 last_run.json、预留用例编号 `test_p146_golden_loud_exit`）在原文里本就写着「目录当前不存在 / 实测 MISSING」，是**事实陈述**而非腐烂；本轮把它们改述成不带路径反引号的形式，守卫不再误判，**没有删守卫、没有加豁免**。
- **[守卫调整] p41 的章数断言只扫现行文档**：`docs/archive/**` 是快照，其中「共 25 章」不随现行章节数变化，故从扫描清单移除；docx 快照里的旧路径按既有口径进 `DOCX_INTENTIONAL_REFS`（第 3 条，前两条同理：改 docx 等于改快照）。
- **[连带修复] 3 处悬空相对链接**：`docs/architecture/README.md` 两处、`README.md` §4 一处，由 `tests/test_p101_integrity_guards.py::TestMarkdownRelativeLinksResolve` 抓到，均改指 README §3。
- **[口径] 测试基线 re-cite**：全量重跑 `.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5242 passed / 40 skipped / 4 deselected / 0 failed**（421.16s，Python 3.12.13，单机离线未开并行）；`ruff check agentix tests scripts evals kb_ingest` 全绿、`mypy agentix/domain` 38 文件 0 错、`gen_staging_matrix.py --check` 退出 0。**这是本仓第一次全量 0 failed**；旧口径 5239 / 1 failed 已移入 `docs/工程口径日志.md` 的历史链。

### PhaseK · HITL 与恢复链断点收口（2026-10-27，PhaseK）

本批收口四个断点（checkpointer 静默降级无 fail-closed、进环审批裁决不打指标、`model_used` 产了没人取、路由/恢复决策在可观测面上不存在），连同本批新发现的 `decision` 值域腐烂一并收口，拆解与逐项验收口径的分工指针是 `docs/migration/PhaseK_HITL与恢复链断点收口实施计划.md`。**执行事实**：任务 1、2 由本会话按 `organizer→executor→reviewer` 流程落地并复审通过；任务 3–6 的落地与复验由**同一工作树上的并发会话**完成，本会话逐条读代码核验其落点与下列锚点相符。

- **[新增·配置键与 fail-closed] checkpointer 拒绝降级开关**（`agentix/infrastructure/persistence/checkpointer.py:28`/`:114`/`:132`/`:164`）：新增 `Settings.CHECKPOINTER_FAIL_CLOSED`（`agentix/config/settings.py:1316`，默认 `False`）。Redis 路径两条失败分支——包未安装（`checkpointer.py:129`–`:134`）、连接与 `asetup()` 失败（`checkpointer.py:162`–`:166`）——在开关为真时改抛 `RuntimeError("checkpointer fail-closed: <原因>")`，**且抛之前不给 `_saver` 赋值**（否则单例被粘成 `MemorySaver`，模块 docstring 的例外开关句已写明）；显式 `CHECKPOINTER_TYPE=memory`（`checkpointer.py:122`–`:127`）不受影响，默认关时与基线逐位等价（纪律 1）。配置键按纪律 4 五处同步：`agentix/config/settings.py:1316` / `agentix/config/selfcheck.py:83`（`DEFAULT_IGNORED_MISSING`）/ `.env.example:32`–`:33` / `docs/architecture/配置管理.md:120`（§5 表）/ `tests/test_p41_doc_consistency.py:373`（手写清单）。测试：`tests/test_p132_checkpointer_fail_closed.py`（覆盖默认关降级、开关开抛且单例不粘、显式 memory 恒不抛、包未安装分支）。
- **[新增·打点] 进环（`interrupt()` 恢复）审批裁决补打指标**（`agentix/application/services/task_execution.py:317`）：新增模块级私有函数 `_record_in_loop_decision()`，经 `record_approval_decision(target_type="tool", ...)`（`task_execution.py:330`）与桥路径同源；**五个裁决出口各打一次**——放行 1 条（`task_execution.py:537` `approved`）+ 拒绝语义 4 条（`task_execution.py:540`/`:547`/`:553`/`:565` `rejected`）。此前 `interrupt()` 恢复链的裁决完全不打点。测试：`tests/test_p81_hitl_in_loop.py:621` `TestInLoopDecisionMetrics`。
- **[破坏性·可观测变更] 审批指标 `decision` label 值域收敛**（**本批新发现的腐烂**，规格事实表第 3 行；不在提案原 4 处断点之列；`agentix/policies/hitl/hitl_bridge.py:424`/`:241`、`agentix/observability/metrics.py:165`/`:646`）：这不是"新增打点"，而是**破坏性可观测变更**——`retrieval_approval_decisions_total{target_type,decision}` 此前在写入点各写一套词汇（检索侧 `approved`、桥侧人工裁决写**枚举原值** `approve/reject/edit/resume`、自动放行写 `auto`），本批收敛为**六值** `{approved, rejected, edited, resumed, auto, bypass}`。桥侧人工裁决经 `bucket` 映射取映射值（`hitl_bridge.py:418`，映射先于打点定义），自动放行 `auto_label` 分 `bypass`（`hitl_bridge.py:229`，`PermissionMode.BYPASS` 模式级豁免）/ `auto`（`hitl_bridge.py:233`，逐请求自动放行），`_record_decision` 落点在 `hitl_bridge.py:241`；Counter help（`metrics.py:165`–`:167`）与 `record_approval_decision` docstring（`metrics.py:646`–`:651`）逐字列出六值。**登记的代价**：历史序列里 `decision="approve|reject|edit|resume"` 在新查询中查不到（本仓无看板，维护者 2026-10-27 知情裁决）。静态守卫：`tests/test_p134_approval_decision_domain.py`（`test_every_decision_write_site_is_within_allowed_domain` 起 `:336`——用源码文本/AST 判每个写入实参只能是六值之一，禁止 `result.action.value` 这类开放取值重新出现，不靠"跑一遍才有值"）。
- **[新增·span 属性] `GatewayResult.model_used` 终于有人取**（`agentix/retrieval/usecases/chat_agent_tools.py:298`/`:320`）：新增 `agentix/observability/spans.py:160` `record_model_used()`，在 `ainvoke_with_fallback` 成功后的两个分支写 span 属性 `gen_ai.response.model`（常量 `GENAI_RESPONSE_MODEL` 定义在 `agentix/observability/tracing.py:235`，两处调用共用，不重复字面量——纪律 6 ④）。此前 `model_used`（兜底链实际选中模型）无人取用。
- **[新增·span 属性] 路由决策与恢复点上 span**（`agentix/observability/spans.py:142`/`:179`）：`record_route_decision()` 在 `analyze_and_route_query`（`agentix/application/agents/router.py:224`）得到白名单校验通过的最终 route 后调用**唯一一处**（`router.py:333`，写 `agentix.route.decision`）；纯 Python 分派的 `route_query()`（`router.py:339`）不调，避免两个源。`record_resume_from_checkpoint()` 在 `Command(resume=...)` 分支（`agentix/orchestration/langgraph/lg_builder.py:357`）与 `resume_thread`（`agentix/application/services/breakpoint_resume.py:309`）两处调用，判据只取 `config["configurable"]["thread_id"]` 是否非空。`agentix/retrieval/api/routers/explanations.py:42` 里 `"route_decision"` 那条 `not_recorded` 占位文案改准为「路由决策值已进 span（`agentix.route.decision`），理由文本仍未持久化」，且该键**留在** `_NOT_RECORDED_BLOCKS`（`explanations.py:38`）。测试：`tests/test_p133_decision_span_attributes.py`（含 `test_resume_paths_mark_checkpoint_attr` `:318`）。
- **[验证·全量] 任务 8 基线重跑**（2026-10-27）：`.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5239 passed / 40 skipped / 4 deselected / 1 failed**（419.53s，Python 3.12.13 + ruff 0.5.7，单机离线未开并行）；`ruff check agentix tests scripts evals kb_ingest` 退出 0、`mypy agentix/domain` 38 文件 0 错、`python scripts/gen_staging_matrix.py --check` 退出 0。本批可点名的新增 30 条（`test_p132` 4 / `test_p133` 13 / `test_p134` 13，逐文件全跑通过、0 skip）。**这一轮不是 0 failed**：唯一失败是文档路径存在性守卫点名两条不存在的引用，出处与归因见 `docs/工程口径日志.md` 的 PhaseK 条（并发批次「已规划未落地」的引用，本轮按边界未改该文档、未动守卫）；相对上一口径 5084 的 +155 不做逐项对账——同一棵树含并发批次落点，只是两端实测相减；deselected 3 → 4 的 +1 可点名到 `tests/test_p144_redis_stream_bounds.py:476` 那条 `integration` 用例，skipped 仍是 40。

自我限定：本轮不删任何既有层（提案的三层串联前提经实测不成立）；多中断配对与动作级幂等属非目标，理由见本计划 §非目标；span 属性只落判定值，理由文本与高基数内容仍不落。

### 口径对齐 · 往期文档的旧基线复述 + 评测默认题集死引用 + PhaseJ 决策点现状（2026-10-27）

本轮**零用例增删**（实测 `tests/test_ci_gate_contract.py` 改前后同为 **21 passed**、`tests/test_p41_doc_consistency.py` 同为 **1 failed / 53 passed**）⇒ `README.md` §11 声明的那一轮基线不因本轮改动，本轮不 re-cite。改动面是**四份**往期文档的陈述口径（`docs/design/审批与中断恢复优化规划.md`、`docs/migration/PhaseJ_评测活证据与叙事收口规划.md`、`docs/migration/PhaseK_HITL与恢复链断点收口实施计划.md`、`docs/migration/现有目录树.md`）+ `evals/` 里一处脚本缺省标签 + 评测侧文档的过期现在时陈述（见本小节末条 bullet）。

- **[口径] 把「旧读数 5084 当现值」的三处改为当时读数 + 唯一真源指针**（`docs/design/审批与中断恢复优化规划.md:56`/`:753`、`docs/migration/PhaseJ_评测活证据与叙事收口规划.md:19`/`:55`/`:137`–`:138`）：原句「与 `README.md:578` 记录逐位一致」在 2026-10-27 PhaseK re-cite 后不成立。改法按「旧数字不删、只标历史」——数字留在原处并写明是 2026-10-26 建档读数，现值一律指向 `README.md` §11「测试真源」；P1 的验收行改成「与**开工前** README 声明的那一轮逐位一致」，**刻意不抄数字**（纪律 7：同一事实写两处就是下一个腐烂点）。三处均"一行换一行"，该设计文档总行数 755 不变，`docs/工程口径日志.md` 按行号引用的第 701/703 行逐字未动。
- **[现状核对] PhaseJ §8 五个决策点逐条对上现树**（该文件 §8 表后新增段，文件 173 → 182 行）：**P0 已落地**（纯文档纠偏，未动代码）；**P4 已落地**（README 首屏三条评测线已各自主谓；本文件原引裸行号 `:138`/`:153`/`:377` 现树实测为 `:140`/`:155`/`:379`，已 +2 漂移 ⇒ 改章节指认）；**P1 未落地**（实测 `grep -rn "RUNTIME_LLM_STEP_RECORDING_ENABLED\|LLM_STEP_TYPES" agentix evals tests .env.example` → **0 命中**，`evals/run_eval.py:670` 仍是 `if "llm" not in step_type:` ⇒ 步骤登记与判据同源都没做，P3 的硬前置未解除）；**P2 未裁**（路由口径 A/B 仍待维护者选型，本轮不擅自选）；**P3 维持「本轮不跑」**（维护者 2026-10-26 与 2026-10-27 两次明令）。同段登记该文件 §2/§3 引用的 `evals/run_eval.py:656`–`:658` 已漂移为 `:670`——行号这一维没有守卫守着。
- **[非目标核对] 「三层路由转一层」未做，且它本就是非目标**（`docs/migration/PhaseK_HITL与恢复链断点收口实施计划.md:60`–`:68`，文件 350 → 356 行）：三层结构在文档与代码里都仍在（`docs/architecture/README.md:11`「主路由层 → 多工具子图层 → 原子工具层」、`README.md` 的「三层 Multi-Agent 请求链路」节、L1 入口 `agentix/application/agents/router.py:224` 的 `analyze_and_route_query` 未删），本批只加观测点、零删除；`agentix/config/settings.py:561` / `:599` 两个闸经复测锚点未漂移、默认仍 `False`。同时把该条里**没给判据的手抄数**换成带命令的可复现读数：原文「11 个测试文件与 `route_type` 的 29 个消费面」→ 实测四条 grep 为 **17 / 36 处（9 个文件）/ 13 / 2**。
- **[评测入口] 默认题集"死引用"的现状取证 + 唯一残留处置**（`evals/write_quality_baseline.py:182`–`:188`、`docs/design/审批与中断恢复优化规划.md:99`）：五个 `DEFAULT_GOLDEN`（`evals/run_eval.py:82`、`evals/hybrid_eval.py:75`、`evals/ragas_report.py:39`、`evals/golden_to_promptfoo.py:36`、`evals/feedback_ingest.py:40`）与 `scripts/demo/local_kb_demo.py:69` 的 `GOLDEN_PATH` 仍写着已删的 `evals/legal_golden.jsonl`——**这是刻意保留**（保守边见 `evals/golden_source.py` 模块 docstring：不新建题集、不把缺省改指别的题集），六入口实测已全部经 `ensure_default_golden()` 收口为「非零退出码 2 + 目录扫描出的可用题集清单 + 改道示例」，**不再静默跑空**（五个直接跑即退出 2；`scripts/demo/local_kb_demo.py` 要走 `eval` 子命令——裸跑撞的是 argparse「command required」，不是本兜底）；设计文档 §3.4 第 4 行现已把「建档锚点」与「现树实测锚点」两套并列（缺省声明被 T3 落地整体下移）。唯一没走该兜底的是 `write_quality_baseline.py` 往生成物 `_meta.golden` 里写的缺省标签：改为**不猜路径**（`"unrecorded（source summary 未记 golden 字段；默认题集已下线，不猜路径）"`），`summary` 里 `golden` 有非空值时取值与改前逐位一致（缺省串涵盖「未记该键」与「值为空」两种情形）。全仓无任何用例钉过该缺省串（实测 `grep -rn "write_quality_baseline" tests/` 只在 `tests/test_ci_gate_contract.py:178` 的 docstring 命中）⇒ 按纪律 5 只做最小改动、**未新增测试**。同轮改准的评测侧过期现在时陈述另有两处：`evals/quality_gate.py:34`–`:37`（原句「本检查**早已**在 `ci.yml` 的 `eval-offline` job 中被调用」对现树为假——现测 `grep -nE "quality_gate|eval-offline" .github/workflows/ci.yml` 0 命中，CI 侧现役挂载只剩 `.github/workflows/perf-baseline.yml:99`，另有 `evals/evolution_run.py:52` 复用纯函数不算挂载）与 `evals/feedback_export.py:8`（链路图把已删集写成在用集）；`docs/migration/现有目录树.md` 的 `evals/` 块两处各自改准——题集行改指在用集并注明 legal 两集 2026-10-26 已删、目录行的 `baselines/` 标注工作树已无该目录（HEAD 仍持有 committed 的 evals/baselines/quality.json、删除未提交），处置标注已回写 `docs/design/审批与中断恢复优化规划.md` §3.7 第 3 条；`evals/README.md` 其余仍点名 `legal_golden.jsonl` 的行（:450/:488/:565/:585/:594/:598）由该文件 :6–:12 的下线横幅与 §1「本文件其余部分描述的是评测框架」兜住，本轮只在 §5.3、§7.1.1 两个标题与 §4 分母行就地加限定（标题限定直接盖住其下的 :488/:565 示例命令）、**未增删行**（844 行不变，:833 的行号引用未漂移）。**收口补记（同轮，reviewer 第 3 轮判通过后）**：其一般项 G4-① 的标注不对称随即就地补齐——`docs/migration/现有目录树.md` 头部「范围」行声明**基准=工作树**（该文档此前未声明以哪棵树为准，正是第 2 轮严重项的根因），题集行同步补上「前者 HEAD 仍持有 committed 档、删除尚未提交，后者从未入库」；文件仍 186 行、`:21` 未位移（`docs/design/审批与中断恢复优化规划.md:227`/`:228` 按行号引用它）。**同轮收口 reviewer 登记的四个一般项**：G1 `CHANGELOG.md:576` 的裸行号 `quality_gate.py:102-105` 改文件级指针（历史结论不回改）；G2 `evals/feedback_ingest.py:8`/`:131`/`:204`/`:215` 四处面向操作员的文案就地加下线限定（259 行不变，`:40`/`:110` 未位移）；G3 `evals/README.md` 第五章编号与文档序对齐（旧 §5.5/§5.6 → §5.3/§5.4，旧 §5.3/§5.4 → §5.5/§5.6；内容不搬块，「录制→门禁」「已知限制」仍紧跟录制基线；活引用六处随改：本文件 :374/:737、`docs/runbooks/评测基线录制.md:12`、`docs/项目实现手册.md:2715`/`:2941`、`docs/migration/Phase15-20_深化规划.md:160`、`docs/migration/模块清单.md:241`（后两处属历史记录行，只给处置指针加「旧 §5.6 ⇒ 现 §5.4」标注，行内的历史陈述不回改）；`CHANGELOG.md:537`/`:577`/`:580`/`:850` 四条历史条目按当时编号书写，**不回改**）；G4 C-Eval 题数收敛为单一真源（`evals/ceval_golden.manifest.json` 的 `rows`，§10.1 规模行为唯一声明处并给 `wc -l` 对账命令，四个文件（`evals/README.md`、根 `README.md`、`docs/migration/现有目录树.md`、`docs/migration/PhaseJ_*.md`）`grep -n "156"` 的其余命中逐条点名登记为抄本快照，并就地声明 `evals/README.md:103` 的「第 156–159 行」是 `router.py` 行号引用、不属抄本；`CHANGELOG.md` 自身四处 `156` 不列入抄本登记并就地说明——`:163`/`:383` 是本轮之前的既有条目（同在 `## [Unreleased]` 下，按当时读数保留、不回改）、`:35` 是本条自身的字面复述、`:550` 是 `cypher_tools/node.py:128-156` 的行号引用（非题数）；本轮**未新增用例**——「文档数字 == 文件行数」至今无人钉，补守卫属测试基线变更，待维护者点头）。**复验随手补的同类腐烂两处**：`docs/项目实现手册.md:2941` 那条【历史】行的处置指针由 §5.6 改准为 §5.4（旧编号已就地标注，历史陈述本身不回改）；`docs/migration/Phase28-31_优化规划.md:22`/`:245` 引的 `evals/feedback_ingest.py:82` 现树实测是**空行**，`"source": "feedback"` 实际写在 `:110` ⇒ 两处改准并标注腐烂。**同轮扩展 · 规划文档与配置面的现状同步（2026-10-27，全部行内替换、只加限定不删历史陈述）**：评测/门禁侧的「现在时」腐烂按现树逐处改准——`docs/design/性能与优化路线图.md:137`（CI job 清单标为 2026-10-26 前读数，现测 7 个并行 job + `ci-gate`，复现 `grep -n "^  [a-z-]*:$" .github/workflows/ci.yml`）、`:287`（「这一步现在是真门禁」⇒ **不是门禁**：缺基线走 `NOT-ARMED` 退出码 2，实测 `evals/quality_gate.py:59`/`:67`；代码与 `--offline` 路径保留）、`:305`（τ 调优的语料行注明两集已删 ⇒ 该验收口径当前无输入，题数真源指 `evals/ceval_golden.manifest.json` 的 `rows`）；`Phase15-20_深化规划.md`（:20/:134/:135/:136/:140/:163/:164/:172/:299/:301）、`Phase28-31_优化规划.md`（:22/:43/:236/:242/:278）、`Phase32-34_Memory-RAG-MultiAgent优化总纲.md:96` 同一改法；其中 :135 的复现指针按实测换成**文件存在性**判（`baseline_predictions.jsonl` **从未入库**，`git status --porcelain evals/` 对它不显 ` D`），并登记内容守卫 `test_committed_predictions_cover_the_golden_set_exactly` 已随守卫批次退掉（现树只在 `tests/test_p41_doc_consistency.py:87` 的注释里命中 ⇒ 「删基线 1 红」这条**不再成立**）。裸行号按现树改准：`PhaseK_…:253` 词表 `router.py:180`–`188` ⇒ `:181`–`:189`，并写明 flag-on 时 `:278` 并入 `task-query`、`:280` 并入 `code-query`；`PhaseK_…:56` 给「async 节点里 `interrupt()` 不可用」补限定（只适用 py3.10 包装式接线；`HITL_IN_LOOP_ENABLED` 已用注入壳落地，`settings.py:1044`）；`PhaseJ_…:18` 三处 README 引文按 `grep -n "质量门禁四态" README.md` 的**逐字命中**改准（`:155` 逐字、`:140` 同义、`:17` 卷首）、`:81`–`:82` 的 `run_eval.py:102` ⇒ `:115`；`架构演进评估.md:81`/`:86` 与 `Agent_Core_2.0_规划.md:6`、`:266`、`:268`–`:276` 补「历史读数，现值只认根 `README.md` §11」（数字本身一律不删不改）。**`PhaseK_…:62` 的「三层路由转一层 = 非目标」刻意不动**：`docs/design/审批与中断恢复优化规划.md:614`（§4.4 裁决登记）已由维护者 2026-10-27 推翻为必做项（T29 前置实测 + T30 收敛，状态待分派）并明写「PhaseK 该行不改、冲突以该节为准、实测数字仍有效」⇒ 该非目标行属另一会话的计划所有权，本轮只登记不重写。配置面补一处同源交叉说明（三处文案均取自 `.qoder/rules/code-exec.md` §3.1①，错误串与实测落点：`agentix/tools/gateway/executor.py:784` 注入 `build_tool_policy()`、`:398`–`:408` 按**有效风险**拒绝，报「risk level L2 exceeds policy max L1」）：`docs/architecture/配置管理.md:152`/`:189`、`docs/项目实现手册.md:371`、`.env.example:789` 现写明 `TOOL_SELECTION_ENABLED=true` 时须把 `TOOL_SELECTION_MAX_RISK` 提到 `L2`/`L3` 才跑得动 code exec；`:152` 原句「执行层…不受影响」同按实测收窄为**仅 flag-off 成立**。`docs/已知限制.md` 末尾新增两条诚实边界（165⇒177 行，本轮唯一允许增行处，该文件无被外部按行号引用）：「文档题数与金标行数之间**无守卫**」与「裸行号引用会腐烂、`.py` 侧按纪律 5 刻意未动」，均**只登记不实施**。守恒与守卫读数：除 LIMITATIONS 外全部 `wc -l` 与改前逐字节相同（roadmap 310 / Phase15-20 301 / Phase28-31 343 / Phase32-34 121 / PhaseK 356 / PhaseJ 182 / evolution 89 / Agent_Core 286 / configuration 347 / PROJECT_SPEC 3866 / .env.example 903 / README 1119）；`ruff check agentix tests` All checks passed；`pytest tests/test_p41_doc_consistency.py` 仍只有路径存在性那一条红、缺失清单**恰 2 条**（本轮没点亮新腐烂）。**返工第 1 轮（2026-10-27，reviewer 判不通过后的最小修正）**：`docs/design/性能与优化路线图.md:287` 与 `docs/migration/Phase15-20_深化规划.md:135` 把 `evals/baseline_predictions.jsonl` 的「入库」改准为「2026-10-25 本地录制落盘、**从未进入任何 git 提交**、2026-10-26 从工作树删除」（证据：`git log --all -- <该路径>` 空 · `git cat-file -e HEAD:<该路径>` fatal · `git rev-parse --is-shallow-repository` false · 全仓仅两个提交且最新是 2026-10-23 的 `ee2bad8`）；`Phase15-20:135` 原「`git status` 的 ` D` 只对 `legal_golden.jsonl`／`baselines/quality.json` 成立」的排他句**漏举 `promptfoo_tests.jsonl`**，已补全为现测三条；「守卫名 `grep -rn` 只在 p41 注释里命中」的判据**限定在 `tests/`**（全树另有 `CHANGELOG.md:35`/`:531`、`docs/项目实现手册.md:2729`/`:2750`、`docs/runbooks/评测基线录制.md:259`/`:282`、`evals/README.md:446`）；同类扫描另把 `docs/项目实现手册.md:2743` 的「两份 committed 文件」改准为「两份基线文件（`quality.json` 曾提交、预测档从未提交）」；`docs/已知限制.md:172` 错字「既风」⇒「既有」。全部行内替换：`wc -l` 读数不变（roadmap 310 / Phase15-20 301 / PROJECT_SPEC 3866 / LIMITATIONS 177 / CHANGELOG 1728 / ELOG 76），`ruff check agentix tests` All checks passed，`pytest tests/test_p41_doc_consistency.py -q` 仍 1 failed / 53 passed 且缺失清单恰 2 条。**同轮收口 · 文档全量刷现状（2026-10-27 第 1 轮返工 + 现状补扫）**：① 返工 reviewer 判不通过的三类问题——`docs/design/性能与优化路线图.md:287` 与 `docs/migration/Phase15-20_深化规划.md:135` 把 `evals/baseline_predictions.jsonl` 的「入库」改准为「2026-10-25 本地录制落盘、从未进入任何 git 提交、2026-10-26 从工作树删除」（证据：`git log --all -- <该路径>` 空、`git cat-file -e HEAD:<该路径>` fatal、`git rev-parse --is-shallow-repository` false、全仓仅两个提交且最新是 2026-10-23 的 `ee2bad8`；`Phase15-20:135` 的 ` D` 排他句补全为现测三条含 `promptfoo_tests.jsonl`；守卫名判据限定 `tests/`）；② 现状补扫三处——`docs/migration/PhaseH_高性能运行时优化规划.md:28` 的「角色→档位未接线」改准为「已接线、默认关」（`LLM_TIER_ROUTING_ENABLED` + `llm_factory.py::_apply_tier_routing`）、`docs/architecture/配置管理.md` §5 表后补检索侧召回缓存三键的散文条目（`SEMANTIC_CACHE_ENABLED`/`_TAU`/`_TTL_SECONDS`，配置面在 `agentix/retrieval/settings.py`，此前文档完全缺；**刻意不进表格**——该表受 `TestDocumentedConfigKeys` 按 `agentix/config/settings.Settings` 字段比对，写进表会让守卫转红、且解除须改测试基线，按纪律 5 不动测试）、`docs/migration/Phase21-26_规划.md:5` 的 2463 基线加历史指针；③ `docs/项目实现手册.md:2743`「两份 committed 文件」改准、`docs/已知限制.md:172` 错字「既风」⇒「既有」。复验：`wc -l`（`配置管理.md` 347 ⇒ 348，其余不变），`ruff check agentix tests` All checks passed，`pytest tests/test_p41_doc_consistency.py -q -m "not integration"` 仍 **1 failed / 53 passed**、缺失清单恰 2 条（`evals/performance/results/last_run.json`、`tests/test_p146_golden_loud_exit.py`，均出自并发批次的设计文档）。

### T6 · TTFT 混采拆分与兜底路径单独计量（2026-10-27，approval-recovery-optimization T6）

- **[指标] 混采拆分**（`agentix/observability/metrics.py`、`agentix/application/services/run_event_stream.py`）：
  新增直方图 `agent_ttft_fallback_seconds`（桶边界**逐字复制**既有 `agent_ttft_seconds`——Q2 未决不细分，
  主直方图定义零改动、diff 可证），走过 Model Gateway 兜底链的 SSE 流其首内容帧样本进兜底口径，
  `agent_ttft_seconds` 自此**只记主口径**。选「独立指标」而非「加 label」：不新增任何 label
  （纪律 3，`ALLOWED_METRIC_LABELS` 无需扩容）。判定同源（纪律 6 ④）：「是否兜底」由回退链自身
  判定点经 `record_gateway_fallback` 置位本流事实槽（ContextVar 共享引用，并发工具轮的子任务亦可见），
  发射器只读事实不再判定。观测 fail-open（纪律 2）：事实槽/记录异常 `logger.warning` 留痕、帧流不受影响。
  测试 `tests/test_p142_ttft_attribution.py`（含桶边界 le 集合钉死守卫）。
  已知未同步面：`docs/runbooks/性能与成本采数.md` 的 TTFT PromQL 现在查的是**主路径**样本，
  全量口径需 `agent_ttft_seconds + agent_ttft_fallback_seconds` 相加——本任务边界内 docs/** 只读，待下一轮文档批处理。

### T18 · HITL 审批 pending 落库（规划 approval-recovery · Q7 裁决：Postgres 为真源；2026-10-27）

新开关 `HITL_PENDING_PERSIST_ENABLED`（默认**关**）。关 = pending 维持进程内存（FIFO 封顶 200），
新表不创建、不读写，行为与基线逐位等价（纪律 1）；开 = 挂起落库 `hitl_pending_approvals`、
裁决（approve/reject/edit/resume）同库闭合、启动期 `install_executor_hook` 回捞未决 pending 进
内存可裁决、跨实例裁决从库判定不再 404（消灭「重启丢审批」事故面）。落库失败显式可观测
（error 日志 + 回执/ops 响应 `persist_error` 标记 + `stats.persist_errors/restore_errors` 计数），
不静默降级成纯内存宣称成功。库侧未决**同样封顶 200**、溢出显式拒绝不静默挤行（口径见
`agentix/infrastructure/persistence/hitl_pending_store.py` 模块注释）。既有 `to_persistence/
from_persistence` 序列化面被接通为逐字段投影（格式未改）；治理层拒绝语义不动（fail-closed）。
新增 `tests/test_p136_hitl_pending_persistence.py`（sqlite 内存库，不依赖真 Postgres）。
回滚：关 flag + `DROP TABLE hitl_pending_approvals`。

### T18 口径修正 · 节点错误补偿的记录写不写由 `HITL_PENDING_PERSIST_ENABLED` 决定（2026-10-28）

**只改口径，不改行为**：`6ad80f4` 把补偿记录的 store 装配统一到
`agentix/policies/hitl/hitl_bridge.py::resolve_pending_store`，`policies.py::node_error_handler`
不再兜底建表。于是 `NODE_ERROR_HANDLER_ENABLED=true` 而 `HITL_PENDING_PERSIST_ENABLED=false`
（**后者默认 false**）时，补偿记录**一条都不写**，只留 `logger.warning` 与「补偿记录未写
（补偿存储未装配）」失败回执。此前三处文档都写「开 = 向 `hitl_pending_approvals` 写一条已闭合
补偿记录」，**漏了这个前置键**——按字面打开 `NODE_ERROR_HANDLER_ENABLED` 的部署会以为自己在留
补偿证据，实际只有回执。

- **[口径]** 补偿记录的存储装配**唯一判据**是 `hitl_bridge.resolve_pending_store()`（纪律 6 ④，
  handler 内不另判 flag、不兜底自建表）⇒ **需 `HITL_PENDING_PERSIST_ENABLED=true` 才写记录**；
  该键关或装配失败时只给失败回执 + `logger.warning`，失败收尾照常（fail-open 不变）。
  `NODE_ERROR_HANDLER_ENABLED` 的承诺因此收窄为「失败不再整轮 500」，**不**包含「一定有记录」。
- **[文档同步]** `.env.example` 的 `NODE_ERROR_HANDLER_ENABLED` 注释块、
  `docs/architecture/配置管理.md` §5 该键行、本文件。
- **[未改]** 代码与默认值零改动，不新增配置键。

### code exec 加固批次 · 独立审查一般项 N1–N7 收口（2026-10-26）

上一节的加固批次经**独立审查（只读不改）判通过**（无阻塞项、无严重项），随结论登记 7 条
一般项。本轮把 7 条**全部同轮闭合**，改的是**措辞、守卫取值来源与文档面**——
**没有新增配置键、没有新增 Settings 字段、没有改变任何生产行为**（`parent_death_signal_supported()`
仍是纯探测，生产路径零调用者；`_set_parent_death_signal` 的降级行为**刻意不动**，改成 fail-closed
属口径变更，要先征得维护者同意——纪律 5）。`tests/test_p125_subprocess_sandbox.py` 与运行时
三个文件（`agentix/tools/sandbox.py`、`agentix/tools/subprocess_worker.py`、
`tests/subprocess_probe_handlers.py`）**零改动**。

- **[修正·措辞与代码相反] N1**（`agentix/code_exec_runtime.py:238`）：探测原来说
  "PR_SET_PDEATHSIG **can be set**"，而执行点**丢弃 `prctl` 的返回值**（它跑在 fork 之后、
  exec 之前，抛异常会让整次执行失败）、异常被 `contextlib.suppress` 吞掉 ⇒ 探测为真只保证
  "落得下去"，不保证那一次调用真成功。**改文字不改行为**：函数与 reason 都收窄成
  "有没有这个能力 / can be requested"，并把"运行时失败仍静默降级、由 worker 侧清理兜底"写明。
  这条必须改而不是补代码：下游所有"取证"都建立在这句声称上，声称比实现宽就会让人拿它当证据放行
  真正没落下的那一档（同一形态在本仓第 5 轮审查里已经翻过一次车）。
- **[修正·边界措辞] N2**（`agentix/code_exec_runtime.py:119`、`.qoder/rules/code-exec.md` §5）：
  `libc.so.6` 是 **glibc 的 soname**，musl 镜像（Alpine）上取不到 ⇒ `_LIBC is None` ⇒ 这条承诺
  **静默降级**，而口径只写"Linux 才有"。现在探测的 reason 与口径 §5 都注明"glibc 的 Linux；
  musl 上探测保守判不支持"。**没有顺手把探测改成按符号在任意 libc 里查找**：那会在 musl 部署上
  实际开启一条此前不成立的承诺，属行为变更。
- **[修正·判定不同源] N3**（`tests/test_p131_code_exec.py:504`/`:524`/`:554`）：
  `_declared_risk_levels()` 原先"声明清单优先、注册表补齐"，而**被守卫的 MCP 导出面读的是注册表**
  ⇒ 两源一旦漂移，守卫会读到 MCP 根本不读的那一份并**绿着**，而它守的承诺在导出面上已经破了
  （纪律 6 ④）。改为**注册表优先**，并新增 `_risk_level_conflicts()` 把"两源同名字但等级不一致"
  **显式报出来**而不是静默挑一个。今天 `code_execute` 不在声明清单里 ⇒ 两源必然无漂移，这条断言
  不会误伤，它咬的是将来。
- **[修正·诊断落空] N4**（`tests/test_p131_code_exec.py:927`/`:943`）：墙提前退出时原来只读
  **stderr** 尾巴，但 worker 的失败信封走 **stdout**（`emit()` 写 stdout，stderr 通常是空的）
  ⇒ "请求写错 / 闸没落下 / 协议版本对不上"这些真因会被盖成只剩一个裸 exit code。现在
  `_wait_for_user_code_pid` 同时取 stdout 里的**哨兵信封行**与两侧尾巴，缺信封行本身也算诊断。
- **[修正·与 docstring 相反] N5**（`tests/test_p131_code_exec.py:919`）：`/proc/<pid>/stat`
  读到**空串**时原判"已死"，与同函数 docstring 的"读不到 ⇒ 保守当成还活着"取向相反；
  空内容就是"读不到状态"的一种 ⇒ 改回判活（宁可误报不可漏报：判死会把"没读到"说成"已死"，
  而那正是这条用例最不该伪造的结论）。
- **[文档·补平台] N6**（runbook 第 3.5 步的变异表）：表里只写"1 failed"不标平台，而同一变异在
  两台机器上下场不同——B 行 Windows 上只咬静态守卫（行为用例按设计 skip），Linux 容器里静态守卫
  与行为用例**同时**变红（2 failed）。表格补两列平台读数。
- **[新增·条数守卫] N7**（`tests/test_p131_code_exec.py:1109`–`:1248`，最终树行号）：套件总数有
  `TestTestCountHasSingleSource` 盯着，**单文件条数没有** ⇒ p131 从 21 涨到 38 这一路，
  README / CHANGELOG / runbook / PROJECT_SPEC 里的手抄数字会同时腐烂而全量回归一个字不红。
  新增 `test_docs_declaring_this_files_case_count_match_the_live_collection`：数字由**本轮真实采集**
  的 `session.items` 现算（与终端那行 passed/skipped 同一份清单），再核所有写
  `p131 共 N 条` 的文档；采集被 `-k` / nodeid / `--lf` / `--deselect` 缩窄时**响亮 skip**
  （实测：按 nodeid 单跑该守卫 ⇒ `1 skipped`，理由点名"session.items 不是全量基准"）。
  **本条新增时还没有 `-m` 这一维判据**，它是第 4 轮一般项 ③ 补的（见上面两条）。
  身份源取采集侧 `nodeid` 而**不是** `item.location[0]`——后者派生自编译期写进 `.pyc` 的路径，
  容器复用宿主 Windows 编的字节码时会让本文件被数成 0 条（p125 那条同源守卫第 4 轮的严重项 S1）。
  **不含 `docs/工程口径日志.md`**：那是历史流水账，按"当时那轮"的读数保留，不当当前声明去核。
- **[验证·同轮实测]**：**p131 共 39 条**（本轮真实采集，与 README §11 / runbook / PROJECT_SPEC
  同短语逐处对齐，受 N7 条数守卫核）；Windows `tests/test_p131_code_exec.py` → **39 collected /
  35 passed / 4 skipped**（0.96s）；Linux 容器（`p125_linux` / `python:3.12-slim` / Python 3.12.15 /
  pytest 7.4.4，跑前做字节码卫生）**root 轮 39 passed / 0 skipped**（5.60s；重跑 5.96s 计数逐位一致）、
  **非 root 轮（uid=1000）39 passed / 0 skipped**（6.43s）——`-rs` 一条 skip 都没打印；
  同容器 `tests/test_p125_subprocess_sandbox.py` → **91 passed / 2 skipped**。
  `ruff check agentix tests scripts evals kb_ingest` 全绿（仓库 pin 的 ruff 0.5.7）。
- **[返工·独立审查第 4 轮]**（2026-10-26，审查者只读代码不跑测试）：判**通过**（无阻塞、无严重），
  随结论登记 3 条一般项，**本轮全部闭合**：① 覆盖面与措辞不符——README §11 与 CHANGELOG 此前**不写**
  `p131 共 N 条` 这个短语（实测各 0 处），守卫扫这两份是**空集**，而 runbook 的说明文写成"核 README /
  CHANGELOG"；修法是四处都落该短语（本轮 README §11、CHANGELOG 上面那条实测 bullet 各加一处），
  并把 runbook 那句改准成"守卫只认这个短语，没写它的位置不受保护"；② `_wait_for_user_code_pid`
  原先在**尾部 2000 字符**里找哨兵信封行，stdout 一长就会把早期信封的前缀切掉、把"墙拒了请求"
  误报成"墙连信封都没写" ⇒ 改成**全文找行、尾部只作展示**；③ 条数守卫的缩窄判据漏了 `-m` ⇒ 补进
  skip 条件（**初版口径**：本文件当时没有参与 `-m` 选择的 marker，`-m` 缩窄不了条数，补它是为了
  将来挂上筛选 marker 时不把人为缩窄报成文档腐烂 ⇒ **该初版把 `-m` 无条件列入，下一条登记它自己
  引入的回归**）。复验读数：`test_p131` + 三份文档守卫合跑 **124 passed / 4 skipped**（8.45s）；
  `ruff check tests/test_p131_code_exec.py` 全绿；本文件 sha256 由 `35b9fbd7…` 变为 **`f6835f12…`**。
- **[修正·闭合时自己引入的回归]**（第 4 轮一般项 ③ 的初版修法是错的）：把 `-m` **无条件**列入
  条数守卫的缩窄判据 ⇒ 门禁口径就是 `-m "not integration"`，于是这条生成物守卫**每一次全量都
  自我 skip**——第四次全量实测 **5083 passed / 41 skipped**（比第三次多 1 skip、少 1 passed），
  多出的那条 skip 就是它；守卫在最常用的那条命令上不承重，比不写还糟（"文档核过"会变成假话）。
  改成 **`-m` 只在"表达式点名了本文件声明过的 marker"时才算缩窄**：`_this_file_marker_names()`
  用 AST 取本文件装饰器里的 marker 名，`_markexpr_names()` 拆表达式，两者取交集。本文件当前挂的是
  `asyncio` 与两条 skipif 别名（`POSIX_ONLY` / `PDEATHSIG_SUPPORTED`），`not integration` 里没有
  任何一个名字命中 ⇒ 标准口径照常判定。
  复验：`tests/test_p131_code_exec.py` 带 `-m "not integration"` 全文件跑 **35 passed / 4 skipped**
  （守卫在 passed 里，不是 skip）；单跑该守卫 nodeid ⇒ **1 skipped**（人为缩窄时仍响亮跳过）；
  **承重反证**：把 README §11 的 `p131 共 39 条` 改成 `98` ⇒ **同一条命令**下 **1 failed / 34 passed /
  4 skipped**；还原后 README sha256 `327c01f4…` 与变异前逐字节一致，复跑转绿。
  **最终树全量复跑**（第五次实测，`.venv/Scripts/python.exe -m pytest -m "not integration" -q`）→
  **5084 passed / 40 skipped / 3 deselected / 0 failed**（405.40s）⇒ 那条多余的 skip 被清掉，
  计数回到本轮声明的基线并与前三次逐位一致；**这条回归是靠全量计数差抓出来的**：闭合那轮的定向
  复验用的命令**不带 `-m`**，守卫照样执行、照样绿，只有按门禁口径带上 `-m "not integration"`
  的全量才暴露"每一次都自我 skip"这件事。
- **[验证·第六次全量与行号对账]**（同日，本轮最后一批文字改动之后）：四处文档的 `-m` 判据说明、
  守卫 docstring 与行号/指纹登记改完后，同一棵树再跑全量 → **5084 passed / 40 skipped /
  3 deselected / 0 failed**（406.07s），计数与第五次逐位一致（本轮**没动用例**，条数仍是 39，
  四处 `p131 共 39 条` 由那条生成物守卫当场核过）。**同轮用脚本把文档里引用的行号逐条对现树读了一遍**：
  `:504` / `:524` / `:900` / `:927` / `:943` 仍准，而 `:1065`（实为 **`:1069`**）、`:1105`–`:1181`
  （实为 **`:1109`–`:1248`**）、`:1114`（**`:1118`**）、`:1122`（**`:1166`**）、`:1136`（**`:1180`**）
  五处已腐烂并当场改准。⚠️ **行号与指纹这一维没有任何守卫守着**（条数有，行号没有），本轮靠人工对账——
  登记在这里是为了让下一次改动前先跑对账，而不是相信引用。文档守卫复验：`test_p131` + 三份文档守卫
  合跑 **124 passed / 4 skipped**，`ruff check tests/test_p131_code_exec.py` 全绿。
- **[实测·评测冒烟（A/B 全量未跑）]**（2026-10-26，按 `evals/README.md` §10.3 同一条命令把金标
  换成 3 行切片，模型走**本机** LM Studio `qwen/qwen3-8b`，不外送提示词）：录制前自检
  （`evals/recording_env.py::preflight_local_recording`）全条 OK，`run_eval.py --live --record`
  与 `scripts/eval/mcq_report.py` 两条路径跑通。读数：**路由准确率 0**（3 题期望恒 `general-query`，
  实测 `kb-query` ×2 / `text2sql-query` ×1）；**MCQ 正确率「无法判分」而非 0%**（3 题全部抽不出
  选项字母，`none` 3/3——真因是进了 KB / Text2SQL 管线后被 guardrails 判"不在本知识库范围内"而**拒答**）；
  单题墙钟 29.3s / 40.5s / 45.5s（按 40s/题外推 156 题 × 2 臂串行约 3.5–5 小时）。
  ⚠️ **顺带抓到一处跑 A/B 前必须先修的口子**（**本条目此前记为"录制侧把 `perf.llm_steps` / `perf.llm_latency_ms`
  写成常量 0"，属误诊，本轮按代码复核改准**——没有任何一处写死 0，真缺口是三层：① 整条 ask 被包成**一次** `EXECUTING`
  类型的 Runtime 步骤（`chat_service.py:869`–`870`）；② `StepType` 里根本没有 LLM / MODEL 成员，`GENERATE`（`steps.py:29`）
  在 `agentix/` 全包零引用（`steps.py:21`–`31`）；③ 报告侧判据只认带 `llm` 字样的类型（`run_eval.py:656`–`658`）
  ⇒ 轨迹里没有一条算 LLM 步。冒烟预测档三行的 `llm_steps` 键**存在**、值 0，是"真的没采到"而非"填了个 0"，
  「缺就说缺、不补 0」的纪律并未被绕过 ⇒ 照现在跑 A/B，
  报告会写"两臂每题都调用了 0 次模型"。**A/B 全量按维护者裁决本轮不跑**；读数与口子登记在
  `evals/README.md` **新增的 §10.5**，另把八处"一次都还没跑过 / 尚未开跑"的当前口径改为
  "A/B 全量未跑 + 3 题冒烟"（本文件下面那条 MCQ 批次的旧句按**当时读数**保留，只加括注）。
- **[修正·评测「模型调用次数」缺口的一处误诊改准 + 规划文档落地]**（2026-10-26，**本轮不跑评测**——维护者裁决；未做 git 提交）：
  此前记为「录制侧把 `perf.llm_steps` / `perf.llm_latency_ms` 写成**常量 0**」属**误诊**，本轮按代码复核改准——没有任何一处写死 0，真缺口是三层：
  ① 整条 ask 被包成**一次** `StepType.EXECUTING` 步骤（`agentix/application/services/chat_service.py:869`–`870`；
  `agentix/runtime/runtime.py:185` / `:199` 同样只用 EXECUTING）；
  ② `StepType` 无 LLM / MODEL 成员，
  `GENERATE = "generate"`（`agentix/domain/runtime/steps.py:21`–`31`，定义在 `:29`）全包零代码引用；
  ③ 报告侧判据只认带 `llm` 字样的类型（`evals/run_eval.py:656`–`658`：`if "llm" not in step_type: continue`）。
  **后果形态要区分**：`evals/run_eval.py:644`–`648` 的「缺就返回缺键、不补 0」并未被绕过——
  `.tmp/smoke_preds.jsonl` 三行 `llm_steps` 键**存在**、值 0
   ⇒ 是"真的没采到"而非"填了个 0"，两者修法不同：前者要在执行链补步骤登记，后者只需删一行常量。
  **结论保留**：该指标当前不可用，跑 A/B 前先修仪表接线——照现在跑会产出"两臂每题都调用了 0 次模型"的报告。
  同步新增规划文档 `docs/migration/PhaseJ_评测活证据与叙事收口规划.md`（P0 误诊纠正 / P1 让 `llm_steps` 有值 / P2 路由口径 /
   P3 跑批分档 / P4 措辞 / §7 STAR 简历口径 / §8 待裁决项 / §9 非目标）：**P0 已由本条闭合**，P1–P4 **未开工**，
  §8 留三处需维护者裁决（路由口径 A/B、跑批何时解锁、P1 是否新增 flag）。⚠️ 诚实限定：`docs/migration/PhaseJ_*` 是**规划文档不是完成记录**——
  `code-query` 仍**不进**评测词汇表（`evals/run_eval.py:102` 的 `ALLOWED_ROUTE_TYPES` 保持 7 类）、
  C-Eval 集仍是 **CC BY-NC-SA 4.0 非商用**、A/B 全量**未跑**（只跑过 3 题冒烟）。
  轮次读数登记：独立审查第 5 轮不通过（1 严重 2 一般）→ 第 6 轮不通过（3 严重 2 一般）→ 第 7 轮小范围复验**通过**（5 项全闭合，
  执行者自报的 3 项偏离全部接受）；文档守卫合跑 **124 passed / 4 skipped**，本批全量六次实测中**五次计数逐位一致（5084 / 40 / 3 / 0）**、
  第四次夹过一次 **5083 / 41** 回归（成因：返工一般项时把 `-m` 无条件列入条数守卫的缩窄判据 ⇒ 生成物守卫在门禁口径下每次全量自我 skip）。
- **[登记·行号与指纹随本轮漂移]**（引用旧值的地方要连这句一起引用）：本轮改了
  `agentix/code_exec_runtime.py` 的措辞与 `tests/test_p131_code_exec.py` 的实现 ⇒
  探测函数从 `:234` 漂到 **`:238`**、执行点 `preexec_fn` 从 `:381` 漂到 **`:395`**、
  行为用例的断言从 `:969` 漂到 **`:1069`**（本轮对现树逐行核对：`assert not _process_alive(user_pid)`
  在 1069、断言消息跨 1070–1071，上一句登记的 `:1065` 与现树不符，按现树改准）；`agentix/code_exec_runtime.py` 的 sha256 由
  上一轮取证时的 `a920f2b9…` 变为 **`6b2a3f94…`**。**上一轮条目里的旧行号与旧指纹是当时那棵树的状态**，
  不回头改写（历史读数按当时保留），当前口径以本轮为准。
  全量套件的当前口径只登记在 **README §11**（唯一真源），本节不复述。

### code exec 加固：等级交叉守卫（L0 不变式）+ 墙被杀时的孙进程回收（2026-10-26）

上一轮把「不进 LLM 可见工具面」的判定收进了唯一真源，但那条判定只管住了**名字集合**这一侧。
本批补两处此前无人看守的地方：一个是**声明等级与导出面的交叉处**，一个是口径 §5 早已写下、
却没有任何用例承重的隔离承诺。

- **[新增·守卫] 「非 LLM 可见工具」不得声明成 L0**
  （`tests/test_p131_code_exec.py::TestNonLlmVisibleToolsAreNotL0`）：LLM 可见面靠
  **名字集合**拦（`agentix/tools/definitions.py::NON_LLM_VISIBLE_TOOL_NAMES`），
  而 MCP 导出面（`agentix/capabilities/mcp/tool_source.py`）**只按 `risk_level == "L0"` 过滤**、
  完全不认识那个集合 ⇒ 等级一旦写成 L0，同一个工具在两个导出面上给出的答案互相矛盾，
  会从 MCP 导出面漏出去。两条用例：① 集合里每个名字都必须**解析得出一条声明**，
  否则报「解析不出声明」——守卫**不许空转**（往集合里加一个没人声明的名字，
  它就会悄悄从守卫的视野里消失）；② 单独钉 `code_execute` 必须是 `L2`
  （集合被清空时 ① 会空转，② 仍然咬得住）。
- **[新增·守卫] 「墙被杀 ⇒ 孙进程随之被杀」这条承诺现在有用例承重**
  （`tests/test_p131_code_exec.py::TestParentDeathSignal`）：口径 §5 早写了
  `PR_SET_PDEATHSIG`，但此前**没有一条用例答得出"它落没落下"**——而这条承诺一旦失效，
  机器上会留下一个跑着用户代码的孤儿（资源闸挡不住无人回收的进程）。三条用例分工：
  ① **静态 AST 守卫**（任何平台都跑）钉住执行点真的把 `preexec_fn=_set_parent_death_signal`
  接上了（`agentix/code_exec_runtime.py:381`）；② 真墙的协议 v2 请求必须过**墙自己的**解析器
  （`agentix/tools/subprocess_worker.py::read_request`）——否则在有 `prctl` 的平台上真跑时，
  "请求写错了"会长得跟"墙起不来"一模一样，不好归因；③ **行为用例**（真起墙、真起用户代码进程，
  **只 SIGKILL 墙那一个 pid、不组杀**，断言该 pid 在 15 秒内消失）——只杀一个 pid 是因为
  组杀顺带带走孙进程的话，这条用例测的就不是 `PR_SET_PDEATHSIG` 了。
  用例里有一处**刻意的放宽并已写进 docstring**：**僵尸态算"已死"**（`/proc/<pid>/stat` 状态 `Z`）——
  墙被杀后孙进程被内核重挂到 PID 1，那个祖先不回收它时它就停在僵尸态，`os.kill(pid, 0)` 仍成功，
  但它已经不占 CPU、不是"留在机器上跑的孤儿"。
- **[新增·能力探测] `parent_death_signal_supported()`**
  （`agentix/code_exec_runtime.py:234`，已进 `__all__`）：返回 `(bool, 理由)`，
  判据取自执行点**自己**模块加载期取好的 `_LIBC`（有没有 `prctl`）——**刻意不另立平台判点**
  （纪律 6 ④）：守卫的 skip 判据必须与执行点**同源**，判点一分家就会出现
  "守卫在容器里被数成 0 条"那种形态。
- **[验证·Linux 容器真机]**（2026-10-26，容器 `p125_linux` / 镜像 `python:3.12-slim` /
  Python 3.12.15 / pytest 7.4.4，bind-mount `/repo` → 宿主仓库根，跑前做过字节码卫生）：
  p131 **root 轮 39 passed / 0 skipped**（5.60s；重跑一次 39 passed / 5.96s，计数逐位一致），
  **非 root 轮（uid=1000）39 passed / 0 skipped**（6.43s）——`-rs` **一条 skip 都没打印**
  ⇒ 没有出现"墙自己的故障"类 skip；同容器附带 `tests/test_p125_subprocess_sandbox.py`
  → **91 passed / 2 skipped**（11.74s，两条 skip 的理由都是"本机是 POSIX ⇒ Windows 的拒绝路径测不出"，
  与 [subprocess 沙箱验证 Runbook](docs/runbooks/子进程沙箱取证.md) 第 3 步的既有记录一致）。
- **[验证·变异反证]**：① 把 `agentix/tools/adapters/code_exec.py` 里 `code_execute` 的
  `risk_level="L2"` 改成 `"L0"` ⇒ p131 **3 failed**（既有 `test_spec_meets_the_declaration_guards`
  + 上面两条新的 L0 守卫），失败信息形如「`{'code_execute': 'L0'}` 声明成了 L0 ⇒
  MCP 导出面会把它放出去」；改回后 sha256 与改前逐字节一致。
  ② 把 `agentix/code_exec_runtime.py` 的
  `preexec_fn=_set_parent_death_signal if _IS_POSIX else None` 改成 `preexec_fn=None`
  ⇒ p131 **1 failed**（那条静态 AST 守卫咬住），信息是「没有把 `preexec_fn=_set_parent_death_signal`
  接上 ⇒ 墙被杀时孙进程会变成孤儿」；改回后 sha256 `a920f2b9…` 与改前逐字节一致。
- **[验证·行为级反证]**：② 同一变异在 Linux 容器里**真跑** ⇒ 行为用例变红，失败信息原文
  「墙被 SIGKILL 之后，用户代码进程 401 还在跑 ⇒ PR_SET_PDEATHSIG 没生效（孤儿进程留在机器上）」
  （`tests/test_p131_code_exec.py:969`，等待窗口 15s 走满后失败）；还原后同一条用例重跑
  **1 passed**（2.09s）⇒ 那条承诺不是"文档里的漂亮话"。
- **[诚实标注·平台]**：Windows 主机同树对照是 **38 collected / 34 passed / 4 skipped**
  （Python 3.12.13；3 条是 POSIX 隔离档探测、1 条是 PDEATHSIG 行为用例因无 `prctl` 跳过）。
  ⚠️ **那是本批 N7 条数守卫落地之前的 Windows 读数**：上面两轮容器取证跑在 N7 之后，所以那里是
  39；同一棵最终树的 Windows 复跑是 **39 collected / 35 passed / 4 skipped**，
  当前口径见本节开头与 README §11。
  **Windows 上这条行为用例恒 skip 是按设计，不是漏测**（口径 §5：这条承诺只在 Linux 成立，
  macOS 没有 `prctl`）；守卫的 skip 判据取自**同源**的 `parent_death_signal_supported()`，
  不是另写的一份平台判断。

### code exec：运行时写代码执行任务（沙箱隔离，POSIX 专属，默认关）

Agent 在运行时自己写 Python 代码来完成任务，而不是把每一步都翻译成既有工具调用。
**默认关**，flag-off 与基线**逐位等价**。

- **[新增·机制] 运行时写代码执行任务**：模型产出 Python 代码 → 交给 `code_execute` 工具
  → 工具声明 `sandbox="subprocess"`，走 **subprocess 档**（ADR-0007 阶梯第三档）在
  独立进程里执行。**该档 POSIX 专属**：非 POSIX 平台 fail-closed **拒绝执行**，
  永不做无隔离降级。
- **[新增·路由/节点] `code-query` 意图与 `code_agent` 图节点**：路由在 flag-on 时产出
  `code-query`，图里新增 `code_agent` 节点承接。flag-off 时路由不产该意图、节点不可达，
  与基线逐位等价。
- **[新增·工具] `code_execute`**：声明 `risk_level="L2"` / 有效风险 HIGH，**HITL 可审**
  （`HUMAN_REVIEW_ENABLED` 开时走审批通道）；**不进 LLM 可见工具面**，由 `code_agent`
  节点直接调用——它是执行面而不是"让模型自己挑的工具"。
- **[新增·开关] `CODE_EXEC_ENABLED`（默认 `false`）/ `CODE_EXEC_MAX_ROUNDS`（默认 `3`）/
  `CODE_EXEC_TIMEOUT_S`（默认 `20.0`）**：真正跑起来还需 `EXECUTION_SANDBOX_ENABLED=true`
  + `TOOL_SANDBOX_SUBPROCESS_ENABLED=true` + 平台是 POSIX，任一不满足 ⇒ 该次调用
  **失败并说明原因**，**绝不静默退回进程内执行**。⚠️ 若开了 `TOOL_SELECTION_ENABLED`，
  还要把 `TOOL_SELECTION_MAX_RISK` 提到 **`L2`**——它默认 `L1`，而 `code_execute` 声明
  `risk_level="L2"`，默认上限下会被执行器的风险闸**拒掉**（错误是「风险等级超过策略上限」，
  不是沙箱问题、也不是代码问题；该拒绝属**治理层终局**，不进自愈循环）。
- **[沿用·资源闸] 三道闸沿用 subprocess 档的既有闸**：地址空间 512MB（`RLIMIT_AS`）/
  CPU 30s（`RLIMIT_CPU`）/ 文件 32MB（`RLIMIT_FSIZE`）——不另造一套。
- **[诚实标注·边界] 这一档不隔离文件系统与网络**（ADR-0007 登记的残留：仍可出网、
  仍可读宿主可读文件），且**仅 POSIX 可用**。它是墙，不是笼子。

### code exec 返工：独立审查 9 项处置（2 严重 + 7 一般）

上一轮落地后由独立审查（只读不改）判定**不通过**：2 个严重项 + 7 个一般项。本轮全部闭合。

- **[修复·严重] `code_execute` 不再泄漏进 LLM 可见的任务拆解工具菜单**
  （`agentix/application/services/task_execution.py::_executable_menu`）：菜单按
  **全局注册表 ∩ 有 handler** 构造，flag-on 时会把 `code_execute` 塞进菜单、再写进
  拆解提示词的 `{menu}` ⇒ 模型看得见就能点名调用，与"不进 LLM 可见工具面"冲突。
  改为在菜单侧**按工具名无条件排除**（用 `CODE_EXEC_TOOL_NAME` 常量，**不依赖任何
  flag 组合**）。**判据取工具名而不是 `capabilities == ("code",)`**：后者一旦有别的
  工具也声明 `"code"`，就会连带把它踢出菜单——那正是"连带改变其它工具菜单内容"。
  `registry_init._build_declarations()` 里的缺席**管不到这条路**（声明清单 ≠ 拆解菜单）。
- **[修复·严重] 工具级超时不再可能小于代码级超时**
  （`agentix/tools/adapters/code_exec.py::build_code_execute_spec`）：原来
  `timeout_ms = (当前配置值 + 30) * 1000`，而 `timeout_s` 入参允许取到 120 秒 ⇒
  调用方传 `timeout_s=120` 而配置是 20 秒时，工具侧会先于代码超时掐断整次调用，
  用户看到的是"没有理由的失败"。改为按**代码超时的取值域上界**算：
  `timeout_ms = (MAX_TIMEOUT_S + 30) * 1000` = **150 000 ms**（覆盖 1–120 秒全区间）。
  **取舍**：选 (a) 恒定覆盖最坏情形，而不是 (b) 按本次 `args.timeout_s` 动态确定——
  `timeout_ms` 是 `ToolSpec` 上的**声明字段**、由治理链按它执行，动态化要改治理链
  （`agentix/tools/gateway/executor.py`），那超出本轮边界。同款口径见 `cron_run`
  （`timeout_ms` 覆盖 `CRON_JOB_TIMEOUT_S` 上界 + 余量）。随之**收窄 API**：
  `build_code_execute_spec()` 的 `timeout_s` 形参已无法影响结果（留着会误导下一个
  调用方以为"传 5 就得到 35 秒的工具超时"），故删除——唯一的生产调用方本就无参调用。
- **[修复·一般] 代码正文的量纲统一为「字符」**：入口闸判的是 `len(code)`（**字符数**），
  注释与口径文件却写成「64 KiB」（**字节**）——同一句"64 KiB"指向两个量。
  **取舍**：保持**字符**语义不变（常量名 `MAX_CODE_CHARS` 与判定逻辑一字未动），
  措辞统一成「65 536 个字符」，并说清协议层的**字节**闸另有其人
  （`agentix/tools/sandbox.py` 的 `MAX_REQUEST_BYTES` = 1 MiB）——两道闸各管各的量纲
  （纪律 6 ①：闸只覆盖它声明过的那个维度）。
- **[修复·一般] `CODE_EXEC_MAX_ROUNDS` 的措辞对齐实现**：实现是
  `for _round in range(max_rounds)` ⇒ 它是**总尝试轮数（含首次执行）**，而各处
  （adapter 注释/文档串、`settings.py` 字段注释与描述、`.env.example`、
  `.qoder/rules/code-exec.md` §3/§7、`docs/architecture/配置管理.md`、
  `docs/项目实现手册.md`）写的是"自修轮数"⇒ 按字面读会以为首轮之外还能自修 N 次。
  **实现不动**，措辞统一成「总尝试轮数（含首次执行）」。
- **[修复·一般] LLM 可见 schema 的 `timeout_s` 默认值改为配置驱动**
  （`default_factory=code_exec_timeout_s`）：原来写死 `20.0`，配置成 60 秒时模型看到的
  仍是"默认 20"并照着填 ⇒ 配置被静默忽略。描述同步改成
  「缺省取配置 `CODE_EXEC_TIMEOUT_S`（缺省 20 秒）」。
- **[修复·一般] `code_handler.py` 改用 `CODE_EXEC_TOOL_NAME` 常量**：去掉
  `"code_execute"` 字面量（同一概念在项目内只有一种叫法）。
- **[修复·一般] `agentix/tools/sandbox.py` 的 import 组不再被注释劈开**：那段
  "资源闸以 `code_exec_runtime` 为唯一来源"的说明**与它描述的那行 import 一起**
  提到 import 组最前面，import 组恢复连续（其余 import 的相对顺序未变）；
  `agentix.tools.sandbox` 从 `agentix.code_exec_runtime` 反向 import 三个 RLIMIT 常量
  仍是刻意的单一来源。
- **[修复·一般] 口径 §2「孙进程不能伪造成功信封」改成有条件表述**：原来的绝对措辞
  不成立——**墙自己**就是合法产出成功信封的一方；孙进程造不出父侧认可的信封，
  是因为父侧只认 **nonce 匹配 + 严格布尔 `ok`** 的信封（协议 v2，见
  `agentix/tools/subprocess_worker.py`），而孙进程的 stdout/stderr 被重定向到
  临时文件、压根不在协议通道上。
- **[测试·既有断言改写] `test_tool_timeout_must_exceed_the_code_timeout`**：原断言
  `spec.timeout_ms == int((5.0 + 30) * 1000)` 把基准钉在"当前配置值"上，与新口径冲突
  ⇒ 改写为"基准取取值域上界 + 余量"。**这是本轮唯一被改写的既有用例**。
- **[测试·新增] 4 条**：① 工具级超时覆盖 `timeout_s` 的全区间（1/20/60/120）；
  ② schema 默认值随配置走（`CODE_EXEC_TIMEOUT_S=60` ⇒ 模型看到的缺省也是 60）；
  ③ flag-on 且声明/handler 都在时 `code_execute` 进不了任务拆解菜单；
  ④ 排除只摘掉 `code_execute`，其它工具的菜单内容逐项不变。

### code exec 补漏：LLM 可见工具面的判定收进唯一真源（2026-10-26）

上一轮返工把 `code_execute` 从任务拆解菜单里摘掉了，但**判定散在两个互不相认的地方**
（菜单本地按工具名排除、声明清单靠"不在显式清单里"），而按**注册表全集**导出的两个
OpenAI schema 拼装点仍会把它带出去（实测 flag-on 时两者都返回 `True`）。本轮收口。

- **[修复·严重] 按注册表全集导出的 OpenAI schema 拼装点不再泄漏 `code_execute`**
  （`agentix/tools/__init__.py::list_tool_specs()` / `list_openai_tools()`）：两者按
  全局注册表全集导出，而 `register_handler(..., spec=...)` 会把 spec 顺带登记进注册表
  ⇒ flag-on 时 `code_execute` 会出现在它们的输出里。两处改为过筛。
- **[修复·严重] 单点查询 `get_tool_spec()` 同样受约束**：拿一条不进 LLM 可见工具面的
  工具的 OpenAI schema 也是漏口 ⇒ 直接抛 `KeyError`（fail-closed，不返回 schema
  让调用方自己拼进工具清单）。
- **[变更·口径] 「不进 LLM 可见工具面」的判定收进唯一真源**：
  `agentix/tools/definitions.py::NON_LLM_VISIBLE_TOOL_NAMES` + `is_llm_visible_tool()` /
  `llm_visible_specs()`；任务拆解菜单改为走同一判定（原来在本地按工具名 `continue`）。
  **纪律 6 ④**：同一个判定不许两个源——两处判据漂移时，改一处漏一处。
- **[文档·口径] `.qoder/rules/code-exec.md` §4 补上「判定在哪、哪些拼装点受约束」**，
  `docs/已知限制.md` 的 ③ 同步改成覆盖全部受约束的拼装点。
- **[测试·新增] 5 条**（`tests/test_p131_code_exec.py`）：① 真源集合含
  `CODE_EXEC_TOOL_NAME`（工具改名即红；字面量从声明位 import，不在测试里抄）；
  ② `llm_visible_specs()` 按真源过滤；③ 两个 OpenAI schema 拼装点不导出 `code_execute`；
  ④ `get_tool_spec()` 拒绝该工具；⑤ **同源守卫**——把真源改成放行/排除，两个拼装点
  必须同步跟着变（本地另写判据 ⇒ 红）。
- **[测试·改写] 同文件的 `_CODE_TOOL` 改为 import `CODE_EXEC_TOOL_NAME`**（值不变；
  工具名字面量在仓内只留两处：声明位与唯一真源）。

### 公开 MCQ 评测集：量化多 agent 系统性能（2026-10-26）

KG/RAG 评测集下线后另起一条线：**从公开数据集转换出一份 MCQ 评测集**，用于跑一次
**多 agent 系统的性能测量**。**没有 CI 门禁、没有质量基线、一次都还没跑过**（⚠️ 该句是本批当时的读数；
2026-10-26 已跑过 3 题冒烟，A/B 全量仍未跑，读数与口子见本节顶部与 `evals/README.md` §10.5）——
本批只把「输入 + 工具」备齐，跑分等架构改完再单独做。

- **[新增·数据] `evals/ceval_golden.jsonl`（156 行）+ `evals/ceval_golden.manifest.json`**：
  事实来源是公开数据集 **C-Eval**（HF `ceval/ceval-exam`，revision `617524a0…`），
  分层抽样 52 个学科 × 3 题（种子固定 ⇒ 可复现），期望路由恒为 `general-query`。
  **许可 CC BY-NC-SA 4.0 = 非商用**——商用前须另行取得授权。manifest 里登记出处、
  许可、抽样口径、校验结论与文件 sha256。
- **[新增·工具] `scripts/eval/ceval_to_golden.py`**：公开数据集 → golden 转换器。
  **自带回源校验**：写完重新打开文件、重新加载源数据，逐行比对题干 / 四个选项 /
  答案字母 / 路由 / `must_contain`，不一致**退出码 2**（不是打条日志继续）。
  抽样用「按学科固定种子洗牌取头部」⇒ 同一 seed 每次结果一致。
- **[新增·工具] `scripts/eval/mcq_report.py`**：MCQ 判分 + 多 agent 性能报告。
  判分用**选项字母抽取**（显式标注 → 行首字母 → 选项原文反推 → `none`），
  **不混进子串命中**；四种抽取方式各自计数，宽松口径占比看得见。
  报告同时给：正确率 / 逐学科正确率 / 路由准确率 / supervisor 出口契约
  （`exit_reason`、`status`、`failure_mode`、协调轮数、信息增量比）/
  延迟与模型调用（墙钟 p50·p90、LLM 步数、token）。**扁平拓扑恒无多 agent 元数据 ⇒
  该组显示「不适用」而不是 0**（把「没有」读成 0 是这类报告最常见的撒谎方式）。
- **[新增·观测] 预测行带 `perf` 段**（`evals/run_eval.py`）：单题墙钟、LLM 步数、
  token 计数，取自 `metadata.runtime` 的步骤快照。**缺就显示「未采集」，不补 0**。
- **[修复·工具] `evals/run_eval.py` 在 Windows 控制台上崩**：`run_live` 的预检行含
  `⇒`，GBK 编不了 ⇒ `UnicodeEncodeError` 直接打断录制。stdout 改 UTF-8 +
  `errors="replace"`（编不了的字符降级不抛）。
- **[文档·口径] 「评测集已下线」收窄为「KG/RAG 评测集已下线」**：`README.md` §8 与
  能力表、`evals/README.md`（新增 §10 写清这套集是什么 / 不是什么 / 怎么跑 / 判分口径）、
  `docs/项目实现手册.md` §20 与术语表、`docs/architecture/overview.md` §12.2、
  `docs/guides/测试分层策略.md` §4。**原 KG/RAG 那条线的下线事实一字未改**，
  只是新增了一套**不接 CI 门禁**的公开 MCQ 集，两条线不再互相冒充。

### 评测集下线：删除知识图谱 / RAG 评测资产，撤除 CI 质量门禁机位（2026-10-26）

项目方向调整：**不做知识图谱与 RAG 方向的评测**。本批只做「删评测资产 + 停评测」，
**生产代码一行未改**（`agentix/` 全部未动），评测工具链（`evals/run_eval.py`、
`evals/quality_gate.py`、`evals/trajectory_regression.py`、`evals/ablations/`）**原样保留**，
换一份评测集即可重新武装。

- **[移除·数据] 评测集与录制基线**：法律金标集（102 条）与扩样集（27 题）、
  录制预测档、门禁质量基线文件全部删除；`evals/baselines/` 现为空目录。
  随之删除的还有两份只服务该金标集的守卫：`tests/test_p120_golden50_retrieval_summary.py`、
  `tests/test_p124_quality_baseline.py`（后者守的是"预测档与金标题数/id 同源"）。
- **[移除·CI] `eval-offline` 机位**（`.github/workflows/ci.yml`）：「检查门禁输入 → 跑离线评估 →
  按五个 `*_applicable` 指标比对质量基线」整段撤除；`ci-gate` 汇总的 `needs` 相应从 8 个 job
  收到 **7 个**（version-consistency / lint / test / architecture-guards / eval-smoke /
  retrieval-gate / frontend）。**`eval-smoke` 保留**——它只跑单测（`test_p21_eval_harness.py`），
  不需要任何评测集。`perf-baseline.yml` 未动（它 pin 的是自己的 0.20 预算，与本批无关）。
- **[守卫·改造] 退掉只服务金标集的数据形状断言，保留代码契约断言**：数据集形状类守卫
  （题数、id 集合、每条金标的必含字段）整段删除；**打分口径与门禁语义的契约守卫改为挂在
  一份最小内联 fixture 上**（`tests/test_p21_eval_harness.py` 的 7 路由 + 拒答/无来源行、
  `tests/test_p90_eval_report_keys.py` 的 3 行内联、`tests/test_agent_phase31_eval_layering.py`
  的 20 行冻结快照、`tests/test_ci_gate_contract.py` 的 3 行适用口径对照）⇒
  **`run_eval` 与 `quality_gate` 的行为仍被钉住**，只是不再依赖任何仓内评测集。
- **[文档·口径] 统一改写为「评测集已下线，代码保留」**：`README.md` §8、`evals/README.md`、
  `docs/项目实现手册.md` §20、`docs/已知限制.md`（四条评测条目全部标为**历史**）、
  `docs/源码研读笔记.md`、`docs/guides/测试分层策略.md` §4、`docs/architecture/扩展机制.md`、
  `docs/runbooks/评测基线录制.md`（转历史留档）、`docs/design/检索增强深度优化.md`。
  **历史数字一个没改**（0.8696 / 0.6957 / 0.6275 / 五指标基线 / 0.7826 天花板全部保留），
  只在前面加「历史·已下线」标记——删数字等于伪造历史，改数字等于为了好看。
- **[文档守卫·判据] `tests/test_p41_doc_consistency.py` 引入「历史陈述 vs 现时态」的划线**：
  已删除的路径在**历史类文档**（`docs/migration/`、`docs/design/`、`docs/工程口径日志.md`、
  `docs/runbooks/评测基线录制.md`）里允许保留（那是事实陈述），在**现时态文档**里照红
  （必须改写成「已下线」）。说明书 docx 是 2026-10-22 快照，走既有的 `DOCX_INTENTIONAL_REFS` 豁免。
- **[未做·如实]** CI 里**现在没有任何「这次改动让答案变差」的自动信号**——质量门禁连同评测集一起没了。
  这不是缺陷掩饰，是方向调整的直接后果：重测需人工另建评测集并按
  `docs/runbooks/评测基线录制.md` 重录。生产代码与检索链路的**能力本身没有任何变化**。

### G14 · 同名工具重复注册：默认点名告警 + 可选严格模式（2026-10-26）

G14 的形态很窄但很贵：`ToolExecutor.register_handler` 只在 registry **没有**同名条目时登记 `ToolSpec`，于是第二份声明一个字都不落地，handler 却被覆盖——执行时 `execute` 读到的仍是旧声明。⑮ 之后丢的不再只是描述文本，而是 `sandbox`：一条工具声明"在墙里跑"、注册被静默忽略 ⇒ 它照旧在进程内跑，正是 ADR-0007 拒绝的「以为有墙」。本批**不改既有 winner 语义**（那会影响全部既有注册路径，属行为变更），只做两件事：让它**可观测**，并提供一个默认关的 fail-closed 选项。

- **[新增·诊断] 两个注册入口默认点名告警，且告警没有开关**：`executor.py::register_handler`（第一次赢：后一份 spec 丢弃、handler 照旧替换）与 `definitions.py::ToolRegistry.register`（最后一次赢：spec 与 factory 一起覆盖）都打印统一前缀 `TOOL DUPLICATE REGISTRATION:`，点名工具与**差异字段**，并说清执行时读的是哪一份（executor 侧还回写存活声明的 `sandbox` / `risk_level` 取值）。**只列字段名、不搬运文本值**——`description` 可能是远端 MCP 塞进来的长文本；executor 侧的回显仅限存活声明的 `sandbox` / `risk_level` 两个枚举白名单值。注册结果与基线逐位一致，多出来的只有诊断日志；这也是本批唯一的"flag 关也有可见变化"，理由即任务本身：静默丢弃正是要根除的形态。
- **[新增·开关] `TOOL_DUPLICATE_REGISTRATION_STRICT`（默认 `False`）**：打开后同名且 spec 不同 ⇒ 在改动 registry / handler **之前**抛 `ValueError`（拒绝要么完整、要么不发生）。registry 冻结期本来就拒绝一切注册并告警，排在严格模式之前、不再叠一层；**幂等重注册**（同一份 spec 重绑 handler）与"只重绑 handler"都不算冲突——既有路径里重建 handler 是常态，且没有两份声明可比。
- **[新增·口径] 判定单源 `domain/tool/spec.py::diff_spec_fields`**：两个入口各写一份"什么算不同"就是下一次「声明了什么 / 执行读到什么」各说一套的起点（纪律 6 ④）。纯函数、只读 dataclass 字段，不返回值。
- **[新增·同步] 纪律 4 五处齐备**：`settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example`（`=false`）/ `配置管理.md`（§2 键组 + §5 行）/ `test_p41_doc_consistency.py` 手写清单；基线 compose 转发 `${…:-False}`；staging override **显式钉 `false`**（宿主机 `.env` 不许决定预发是否 fail-closed，G13 同一课），runbook 配置矩阵按生成器补齐并 `python scripts/gen_staging_matrix.py --check` 退出 0（16 行生成 + 1 行手填）。该键**不进** `_INSECURE_GATE_FLAGS`：关着时与基线逐位相同，列进去只会稀释那条告警（与 `TOOL_HANDLER_CONTRACT_ENABLED` 同口径）。
- **[新增·守卫] `tests/test_p129_tool_registry_duplicate.py`（p129 共 27 条；其中 1 条是后续返工补的条数守卫）**：判定单源（差异字段全集 / 相同声明空元组）、executor 默认告警 + 第一次赢（用 `execute` 真跑，handler 确实换成后者）+ 字段值不回显、幂等 / 仅重绑 handler 静默、严格模式「改动前抛」（旧 handler 仍执行、registry 不变）、registry 默认最后一次赢、严格模式保首份（factory 也不半改）、多字段差异全列、冻结期优先、设置默认值、五处 / compose / staging / PROJECT_SPEC 四个同步面。
- **[验证·定向]** p129 **26 passed**（当时的条数；后续 N1–N4 返工补条数守卫后为 27）（首轮 RED 是 `ImportError: diff_spec_fields`，实现后 24 passed / 2 failed 只剩两份文档未同步，补完转绿）；连同 `p41 + p115 + p109 + test_code_hygiene` 复跑（179 passed）、工具/沙箱簇 `p107 + p114 + p125 + p32 + p62` 复跑（311 passed / 22 skipped）；`scripts/gen_staging_matrix.py --check` 退出 0。
- **[验证·变异]** 守卫不是"照抄实现"的两条反证（改完即恢复并核对 sha256、复跑转绿）：① 把 `diff_spec_fields` 改成恒返回 `()` ⇒ p129 **9 failed / 17 passed**（两个入口的告警与严格模式全哑）；② 把 executor 的严格分支改成恒假 ⇒ **2 failed / 24 passed**（"改动前抛"与"旧 handler 仍执行"两条同时响）。恢复后 **26 passed**（当时 26 条；N3 补条数守卫后为 27）。**全量回归（最终树）**：`.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **5061 passed / 36 skipped / 3 deselected / 0 failed**（427.38s，Python 3.12.13；文档收尾前同一功能树 419.00s，计数逐位一致）；相对上一口径 5034 的 +27 全部落在 p129（含 N1–N4 返工新增的条数守卫），skipped / deselected 零变化；口径与逐项加法见 README §11。
- **[格式]** `definitions.py` 与 p129 在 baseline 是格式干净的，本轮已 `ruff format` 收口；`spec.py` / `executor.py` **baseline 本就未全量格式化**（HEAD 上 `black --check` 已红），故只把本轮新增的代码块按格式化器结果手写收齐（black 差异块 14 → 13，无新增块），不借机重排既有代码。`ruff check` 全绿、`mypy agentix/domain` 38 文件 0 错。全量套件的当前口径只登记在 **README §11**（唯一真源），本轮全量在收尾任务里跑完后回写，本节不复述。
- **[文档] 同步面**：`README.md` §16（G14 段落：默认不是"不可能"而是"可见"）、`SECURITY.md`（安全设计概览 + 部署建议）、`docs/项目实现手册.md` §24.2 G14（修复状态 + 历史处置与已放弃候选）、`docs/architecture/运行时护栏.md`（flag 表 + 新增 §15.12）、`docs/architecture/adr/0007-执行沙箱端口.md`、`docs/design/性能与优化路线图.md` §4.1 台账（从未落地移出，并加"本批收尾"结论）、`README.md` §7（与 `TOOL_HANDLER_CONTRACT` 同属"排障前移到启动日志"那一层）、`docs/项目实现手册.md` §11.2（`ToolRegistry.register` 的同名再声明语义）、`docs/architecture/接口与契约.md`（注册幂等契约的 G14 补充）、`docs/architecture/adr/0002-注册表驱动的插件机制.md`（区分 `PluginRegistry` 报错语义与 `ToolRegistry` 告警语义）、`docs/源码研读笔记.md`（治理链补 G14）、`.env.example`、`docs/runbooks/预发环境与配置矩阵.md`。
- **[未做·如实]** 默认路径下这条**仍然是"第一次赢"**：本批买到的只是"它不再静默"，要真正消除"声明了却没生效"得在部署里打开严格模式——本批不替部署做这个决定（预发刻意钉 `false`）。没有新增指标：字段名做 label 会让差异组合进高基数面，而这条信息的受众是启动日志与 CI 的 grep。
- **[返工·独立审查第 1 轮] 判通过；随结论登记的 4 条一般项同轮闭合（N1–N4）**：审查员独立复现了 winner 语义、严格模式「改动前抛」、字段值不回显、冻结期优先、五处同步、变异读数（9/17 与 2/24）与全量计数，并确认审查期间未改动仓库文件。四条登记项：① **N1 文档口径冲突**——executor 告警刻意回显存活声明的 `sandbox` / `risk_level`（枚举白名单值），而多处文档写成"只列字段名、不回显值"；改为"只列字段名、不搬运文本值；executor 侧另回显这两个枚举值"，同步 p129 docstring / SECURITY / configuration / PROJECT_SPEC / runtime-guardrails / ADR-0007 / roadmap。② **N2 p129 吃宿主环境**——默认路径用例直接读 live settings，宿主 `.env` 打开严格模式会 7 failed；加 autouse fixture 运行期一律钉 `False`，"默认关"改读 `Settings.model_fields[...].default`（真源），`TOOL_DUPLICATE_REGISTRATION_STRICT=True` 下真跑 **27 passed**。③ **N3 单文件条数是手抄面**（G6 同类项重演）——`p129 共 27 条` 固定短语 + 采集侧 `nodeid` 现算，`-k` / nodeid / `--lf` / `--deselect` 缩窄时响亮 skip；把 README 的 27 改成 99 真跑 → **1 failed**，恢复转绿。④ **N4 G14 文档守卫近乎恒真**——此前只断言全文出现 `已落地`（该串在文件里二十多次）；改为只在 `**G14 ·` 到 `**G9 勘误` 的小节内断言 `已落地收口`；把状态改回"未落地"真跑 → **1 failed**，恢复转绿。
- **[复验·独立审查第 2 轮] 判通过；随结论登记的 3 条措辞类一般项（N5–N7）同轮闭合**：N5 本条目自己的诊断句仍写"只列字段名、不回显值"，与下一句及 p129 的取值断言自相矛盾——改为"只列字段名、不搬运文本值；executor 侧仅回显 `sandbox` / `risk_level` 两个枚举白名单值"；N6 "四处手抄"实为五处（漏 `runtime-guardrails`），p129 注释与 PROJECT_SPEC 各一处一并改为五处；N7 变异段"恢复后 26 passed"与同段"p129 共 27 条"并存——注明"当时 26 条，N3 补条数守卫后为 27"。审查员独立跑出：宿主 env 打开严格模式 **27 passed**、四种缩窄（`-k` / nodeid / `--deselect` / `--lf`）均响亮 skip、临时副本把 README 的 27 改成 99 与把 G14 状态改回"未落地"各 **1 failed**、全量 **5061 passed / 36 skipped / 3 deselected / 0 failed**（419.34s，与本节 419.00s 只差机器抖动，计数逐位一致）。

### 沙箱 T2 ⑰ 子集 · `RLIMIT_NPROC` 进程数闸（2026-10-26）

⑰ 规划四件（只读根、指定可写目录、出网默认拒绝、进程数闸）里**只做了最后一件**，其余三件仍在 ⑰/T3。先做它的理由不是"最安全"，而是它**最容易被误判成已生效**：`setrlimit` 调用成功、回读也是那个数，闸却可能一次都没拦。三条"不落"的理由全部在容器里量出来之后才写进代码，不是照内核文档抄的。

- **[新增·加固] 进程数闸只在降权之后落**：`subprocess_worker.apply_gates` 在 uid 降权**之后**才谈 `RLIMIT_NPROC`，上限 `SubprocessSandbox.RLIMIT_NPROC = 32`（**任务数**，线程也算，不是进程数）；`limits.payload` 恒带 `nproc`（不是"配了才有"）；落闸前用 `_scan_uid_ledgers` **同一次扫描**读两本账（进程数 / 任务数）。判定单源 `nproc_gate_plan(...)` 返回固定 6 键 `applies / cap / uid_process_count / uid_task_count / no_headroom / reason`——父进程的 `probe_subprocess_caps()` 与子进程的落闸共用这一份（纪律 6 ④：诊断口径与执行链判定同源）。
- **[新增·口径] 三条不落，每条都有实测依据**：`uid<0`（共用父 uid 时计数含整个登录会话的几百个进程 ⇒ 设小会假拦、设大不再是约束）、`uid=0`（内核跳过 `CAP_SYS_RESOURCE` 持有者：soft 写 5、回读 5、随后连起 **12** 个进程一次不阻塞）、`cap<=0`。`applies` **不依赖** `/proc` 可不可读（否则同一配置在两台机器上得到两种结论）；两个计数只影响 `no_headroom` 与措辞。
- **[新增·观测] 没落要报得出来，且只报一次**：`probe_subprocess_caps()["nproc_gate"]` 用同一判点、计数留 `None`；这一项**不进 `missing` / `runnable`**——"没配独立 uid"从来不在这一档的承诺里，混进去会让"开了 flag 但没配 uid"的机器整档被拒，那是行为变更而不是加固。未落时 `caps.nproc_gate.applies=false` 照常回传，`_log_caps` 对同一 `reason` **只告警一次**、不阻断调用（纪律 2）。
- **[取证·容器] 两本账是量出来的**（`p125_linux` / `python:3.12-slim` / 3.12.15，uid 60000）：`(cap, 预起线程, fork 成功) = (8,0,7) / (8,4,3) / (8,7,0) / (16,10,5)`，落闸时**进程数始终是 1**、任务数是 1/5/8/11 ⇒ fork 预算 = `cap − 任务数`；同一 uid 下"一个进程起 5 个线程"回读**进程 1 / 任务 6**。uid 0 豁免另跑一条：soft=5、回读 5、12 次 fork 全成功。`/proc` 只是**下限**：uid 65534 在本命名空间里数到 **0** 个任务，soft=8 下**第 0 次 fork 就 EAGAIN**、连线程都起不来（线程同样计费）⇒ 文档里的"独立 uid"必须是**整个内核上独占**的 uid。探针、原始输出与读法见 `docs/runbooks/子进程沙箱取证.md` **第 4.8 步**（本轮新增）。
- **[坑·取证] 残留任务会把下一轮预算静默清零**：`RLIMIT_NPROC` 按整个 uid 计费，上一轮留下的僵尸哪怕一个，下一轮读出来就是"这道闸每次都立刻咬死"。所以探针每轮先 `drain()` 到该 uid 一个任务都不剩再测；fork 出来的孙进程由子进程自己 `waitpid` 收回——容器里 PID 1 通常不 reap，孤儿僵尸会**永久**占额度。这一条是撞出来的，不是推的。
- **[变更·测试] p125 80 → 93 条**（+13：**8 条跨平台真跑** + **5 条 `POSIX_ONLY`**）。Windows 侧 **通过 63 → 71 / 跳过 17 → 22**；容器两轮 **root 159 通过 / 2 跳过**、**非 root 157 / 4**（p125 91/2 与 89/4；p114 68 条全过）。非 root 轮多出的两条 skip 是"要能降权到独立 uid"与"要 root 腿"，**不是"没测这道闸"**：`test_the_process_count_gate_bites_once_the_uid_is_dedicated` 在非 root 腿真跑，断言的是 `[limits] … uid drop` 的终局失败。
- **[文档] 同步面**：README §7 与 §16、`SECURITY.md`（安全设计概览 + 部署安全建议）、`docs/项目实现手册.md` §24.3（⑮⑯ 行补记 + T2 表）、`docs/architecture/配置管理.md`、`docs/architecture/运行时护栏.md`（§15 两处）、`docs/architecture/adr/0007-执行沙箱端口.md`、`.env.example`、以及 runbook 的第 3 步（两轮复跑读数）与新增的第 4.8 步。**没有新 Settings 键**（复用 `TOOL_SANDBOX_SUBPROCESS_UID`），所以 selfcheck / 五处同步清单没有增行——这是刻意的：给"进程数闸"单开一个布尔，会让"开了却没配 uid"变成一种新的半开状态。
- **[验证] 定向真跑**：Windows `p125 + p114` = **139 passed / 22 skipped**；容器两轮见上（含字节码卫生，先清 `__pycache__` 再跑）；`ruff check agentix tests` 全绿；`mypy agentix/domain` 38 文件 0 错；`black --check` 对沙箱相关五个 py 文件 unchanged。全量套件的当前口径只登记在 **README §11**（唯一真源），本节不复述。
- **[修正·基线] 补登 E4 漏掉的一行旁路豁免**：全量回归抓到 `tests/test_p74_m5_retrieval_unification.py::TestBypassZero::test_outside_import_surface_equals_frozen_allowlist` 红——P4（E4）给 `agentix/main.py` 加了 `from agentix.retrieval.infra.embedding.prewarm import run_prewarm_from_settings`（**函数体内惰性导入**，为的是"依赖缺失/循环导入也要 fail-open"），却没把这条 `(file, module)` 登记进 `RETRIEVAL_BYPASS_ALLOW`。该棘轮是 **equality** 判据（新增须登记、僵尸豁免也报），报错原文就写着"新增须入 RETRIEVAL_BYPASS_ALLOW"。修法是**只补清单一行 + 一句理由**（把"启动预热接线"与"新旁路"区分开），不放宽判定、不动产品代码、不删用例；p74 单文件 **25 passed**。这是本轮**唯一的测试基线改动**（纪律 5 的边缘），单独点名以免混进"零改动"的叙述里。
- **[收尾] README §11 已回写新口径**：`.venv`（3.12.13）全量 `-m "not integration"` → **5034 passed / 36 skipped / 3 deselected / 0 failed**（471.62s）；相对上一口径 4911/31 的 **+123 passed / +5 skipped（采集 +128）** 逐项闭合到五处（p125 80→93、p126 13、p127 26、p128 67、`test_ci_gate_contract` 20→29，13+13+26+67+9=128）。P4 / P5 / P5 补三条里"README §11 的套件总数仍是上一口径 4911"是**当批状态**，按"历史条目留原文"的口径不逐条追改；当前数以 README §11 为准。
- **[未做·如实]** 只读根、指定可写目录、出网默认拒绝三件仍在 ⑰（T3）；`container` 档仍未实现；把 `PYTHONDONTWRITEBYTECODE` 加进 `OS_ENV_ALLOWLIST` 未做（行为变更，纪律 5 未点头），所以**双向 pyc 污染仍在**，取证前必须清 `__pycache__`，清完马上跑、跑完还会再长回来——见 runbook 第 3 步那段 ⚠️。

### 优化批次 P5 补 · 规划文档的"落地态"复核与 §5.1 灰度登记（2026-10-26）

写 roadmap §5.1 这张表（P5 的 §2 E7 条目自己指向它，不补就是文档指向不存在的登记）时，把 §2 的每条重读了一遍，结果抓到 **6 处文档与代码/CI 各说一套**。共同形态很一致：**代码落地那一批改了状态行（§4），没改提案正文（§2）；或者落地那一批写对了，后来的重建把它抹掉了**。这类漂移的代价不是难看——照旧名设环境变量会静默无效，照旧 job 名找门禁会找不到那一格。

- **[修正·文档 1] §2 E5 与自己 §4 状态行互相打脸**：§2 写着"现状：仓库**没有**专用性能基准……拟建「tests/test_perf_smoke.py」"，而 §4 的 E5 行写着"已落地（Phase H W0 + W7）"。实测：`tests/test_perf_smoke.py` + `tests/test_p93_perf_benchmark.py` 共 **20 条**（`-m "not integration"` 下采集 20、跑通 20，25.68s），`evals/performance/` 五件齐全（`runner.py`/`fakes.py`/`queries.jsonl`/`perf_gate.py`/`baseline.json`），`agentix/performance/` 四件齐全，`LATENCY_*` / `EVENT_LOOP_LAG_METRIC_ENABLED` 三键在统一配置层。§2 E5 因此按实际锚点重写（含 gate 的 `DEFAULT_RATIO = 3.0`、committed 基线 overall p50 **1.882ms**、以及**边界**：它量的是时延数量级，不是命中率/串答率）。**"harness 自身稳定"这条验收本批真跑了**：`runner.py --iterations 3 --gate` 三连跑，overall p50 **1.645 / 1.140 / 1.388 ms**（极差比 1.44×，距 3× 阈值还有两倍余量）、p95 80.6 / 97.4 / 61.4 ms（±30%，与 README §15.2 口径⑥"分位数不逐轮一致"同形），三轮退出码全 0；输出写 `.tmp/`，**committed `evals/performance/baseline.json` 全程未参与**（`git status` 该文件干净——`--record` 会重写入库基线，那属数据面变更，纪律 5，本批没碰）。
- **[修正·文档 2] "独立 perf-smoke job" 是三份文档里的一个幽灵**：`docs/migration/PhaseH_高性能运行时优化规划.md` 的 W7 条与 CHANGELOG 的 W7 条都记"perf-smoke 摘 `continue-on-error` 转阻塞（job 改名「性能冒烟」+ ci-gate needs 增补）"，而今天 `ci.yml` 里**没有**任何叫 `perf-smoke`／`perf_smoke`／`性能冒烟` 的 job，`ci-gate` 的 needs 也没有性能项。出处查得到：`.github/` 整目录曾在快照里缺失并按 **8 个 job** 重建（CHANGELOG 的重建条自己写着那个数），重建时没恢复它。**效果没变、出处变了**：`tests/test_perf_smoke.py` 不带 integration 标记 ⇒ 被 `test` job 的 `pytest -m "not integration"` 收走 ⇒ 超预算即红 ⇒ `ci-gate` 阻断。本批把 §2 E5、§4 E5 行、PhaseH W7 三处措辞改成按实际路径描述，并在 PhaseH W7 补一段带日期与实测的「现状复核」。
- **[新增·守卫] `tests/test_ci_gate_contract.py` 28 → 29 条**：`test_perf_smoke_blocks_through_the_test_job_it_actually_runs_in` 把上面那句话钉成机器判据，四条**合取**——① `test` job 真跑 `pytest -m "not integration"`；② smoke 文件没有 integration 标记（**AST 扫** `pytestmark` 赋值与全部装饰器，不用"文本里没出现过这个词"：注释里写过一次就会永远读成"标了"，这是纪律 6 ④ 在 P4/p127 之后第三次按同一个教训写）；③ `ci-gate.needs` 含 `test`；④ `ci.yml` 里确实没有独立 perf job。**第 ④ 条刻意过严**：它要的不是永远绿，而是"独立 job 一旦被恢复，文档措辞必须被同一次改动带走"。**四组变异实测各红 1 条，红的正是本条**（A1 给 smoke 文件加 `pytestmark = pytest.mark.integration`／A2 换掉 test job 的选择器／A3 从 needs 撤掉 `- test`／A4 在 ci.yml 加一行含 `perf-smoke` 的注释；`.tmp/e5_ci_mutation.py`，还原后 `ci.yml 7bba71fd97b5f8f1`、`tests/test_perf_smoke.py 51a9b1a617e93091` 逐位一致）。
- **[修正·文档 3] §2 E3 挂着一个从未存在的键名**：正文写"拟增 `HTTP_CLIENT_POOL_ENABLED`（默认 false 灰度）"，而全仓（除 `.tmp` 快照副本）**零命中**该键；实际落地的是 `SHARED_HTTP_MAX_CONNECTIONS`（默认 **0 = 关**，统一配置层，`selfcheck.py` 与 `配置管理.md` 都有它）+ `agentix/infrastructure/http/shared_client.py` 的 `get_shared_async_client`／`acquire_http_client`→`(client, owns)`／`aclose_shared_async_client`（`agentix/main.py:871` lifespan 调用，失败只 warning）／`reset_shared_async_client`。消费点实测两处（`rerank.py:62`、`redis_cache.py:296`），不是 §2 原写的 `agentix/infrastructure/core/`。这条最危险：`HTTP_CLIENT_POOL_ENABLED=true` 会安静地什么都不做。
- **[修正·文档 4] §2 E8 说"尚未核验"，而核验表就在它下面 6 行**：附录 11 行已把 pg／milvus／neo4j／aiohttp／engine／embedding 客户端全部核完并分三类（已池化／"重建但已 `dispose`"／每请求新建无托管）。措辞改成落地态，并把取证边界写明：**静态核到构造点与关闭点，未压测**（握手成本与 fd 峰值没有实测数）。
- **[修正·文档 5] `docs/architecture/配置管理.md` 把共享池化接到了错误的缝上**：那行写"rerank / embedding 两缝复用"，实测共享工厂的调用方是 **rerank + Redis 语义缓存**，embedding 侧根本不接它（它走 `RETRIEVAL_RETRIEVER_CACHE_ENABLED` 的检索器复用路径）。按实测改，并加一句"两件事别混"——运维拿这行当接线表用，指错缝就是白调一个键。
- **[修正·文档 6] P5 自己的散文中留了已被实测推翻的判据**：§2 E7 的验收句与 `tests/test_p128_semantic_recall_cache.py` 的模块 docstring 都还写着"flag 关 ⇒ `create_embeddings` 换成'一调用就 raise'的桩"，而 P5 的 M1 变异恰恰证明**那个桩证伪不了这句话**（fail-open 把 raise 咽成 `None`）。两处同改为实际判据：**peek 侧数构造次数**（`built == []` 且 `queries == []`），**链路侧才用 raise 桩**（那一段没有 fail-open 包装）——并把"两边判据不同，是因为两边的失败形态不同"写进 docstring。
- **[新增·登记] roadmap §5.1「灰度/默认值类条目的达标条件与取证口径」**（**本批不改任何默认值**）：五行 × 四列（现状锚点／达标条件／取证口径／未达标时禁止），覆盖 §4 依赖总结里剩下的全部——**E1 轮内并发**（要求串行与并发各 ≥3 轮、改善幅度大于抖动区间、错误率与 `router_fallback_total`、HITL 拦截不升，尺子是 `--repeat` 池化而非单轮 p95）、**E2 schema TTL**（时延收益实测之外，必须先有 runbook 那条"改表后必调失效端点"的承接，否则 TTL 只是把陈旧写进默认值）、**E6 prompt 前缀**（逐角色 + 每步过 `--offline`；指纹一致用现成的 `prompt_prefix_fingerprint{builder}`；**provider 侧 cached-token 收益在 LM Studio 取不到** ⇒ 本地只能证"布局对了"，不配写"省了多少"）、**E7 τ 调优**（要一张新的命中率/串答率表，判据固定"串答率≈0 且命中率显著>0"，并点名 `test_perf_smoke.py` **不是**这层的验收尺子）、**E7 答案级接入**（四个前置：判定面先并一份、`scope` 从可选改构造期必填、12h TTL 与知识版本失效钩子同批、指标取值继续分开）。表尾两条全表纪律：默认值变更**必须单独成批**（同批不带第二个行为变更，否则回退时拆不开）、引用即连**测量日期 + 环境**。
- **[未做·如实]**：① **§5.1 的达标条件一条都没跑**——本批不动默认值，也就没有触发门槛；那张表的价值是把"下一次顺手改默认值"变成要交证据的动作，不是宣称已经有证据。② `evals/trajectory_regression.py` 不比时长（轨迹上确有 `duration_ms`/`ttft_ms` 戳记，比对侧没有消费点），E5 原方案"轨迹时长字段口径"那一半仍未接线——补它要改已入库轨迹基线的语义，属数据面变更（纪律 5）。③ **CHANGELOG 的 W7 历史条目没改**：它记录的是当批声称做了什么（错的正是那条声称），本批选择改"现状文档 + 加一条守卫"，历史条目留原文并在此处点名，避免用今天的树去重写昨天的账。④ PhaseH W0 计划文本里的"continue-on-error: true（首版非阻塞）"保留（那是当时的计划，不是现状声明）。⑤ E1/E6 表里引用的 `HITL 拦截计数`、`prompt_prefix_fingerprint` 是既有观测面，本批**没有**为 §5.1 新增任何指标或脚本。
- **[验证]**：`tests/test_ci_gate_contract.py` **29 passed**（0.13s）；文档/门禁面合跑（p41 62 + p128 67 + `test_ci_gate_contract` 29 + `test_code_hygiene` 14 + p36 9 + p127 26 + p124 15）**222 passed**（11.85s）；本轮改完文档后复跑（p41 62 + `test_code_hygiene` 14 + p35 26）**102 passed**（3.66s）；性能基准两套（`test_perf_smoke` + `test_p93`）**20 passed**（25.68s）；`ruff check` 对新改的 `test_ci_gate_contract.py` 全绿，`black --diff` 对本次新增区（485–561 行）零建议（该文件更早批次留下的几处 `assert X, (\n "…")` 形态仍未合 black，与 P4/P5 同口径：不顺手重排，避免无关 diff）。⚠️ 收尾任务 #54 的全量回归仍未跑，README §11 的套件总数仍是上一口径 4911。

### 优化批次 P5 · 路线图 E7：召回级语义结果缓存（2026-10-26）

E7 是路线图里唯一一个"命中即可能答错"的条目：它省的是检索段的钱，代价是**把另一个问法的检索结果当作本题的结果**。所以本批的口径不是"能不能命中"，而是"什么必须不命中"——默认关、τ 保守、权限隔离由结构而非措辞保证。落地形态与路线图原方案有两处差异，都写在 roadmap §2 E7 里（接入点与存储层），不是遗漏，是取证后的选择。

- **[新增·能力] `agentix/retrieval/infra/rag/semantic_cache.py`**（392 行，纯新增，零既有函数改写）。六条口径写在模块 docstring 里：**默认关**（纪律 1）、**判定面是纯函数**（`best_match(vectors, payload_vectors, tau)` 不碰时钟、不碰配置、不碰网络，τ 边界因此可被确定性断言）、**隔离面同源于精确键**（纪律 6 ④）、**TTL 在读时判**（写时判 ⇒ 一条常青记录被反复续命）、**有界**（每 bucket `MAX_ENTRIES_PER_BUCKET=64`，常量而**不占配置面**——逐出策略没有可被安全调大的值，调它等于换语义）、**fail-open**（纪律 2，任何异常退化为"未命中"并 `logger.warning`，`_count()` 把打点自身也包进 except ⇒ 观测面坏了不影响答题）。相似度**复用** `agentix.domain.policies.memory_recall.cosine_similarity`，没有第三份实现：本仓已同时存在两份同名余弦（记忆侧与未接线的 `RedisSemanticCache._cosine_similarity`），再写一份就是"三条链三种判定"。
- **[新增·开关] 三个键，只在检索域配置面存在**（与 E4 同口径，纪律 4 的"五处同步"不适用，理由与守卫都与 P4 那条一致）：`SEMANTIC_CACHE_ENABLED=false` / `SEMANTIC_CACHE_TAU=0.95` / `SEMANTIC_CACHE_TTL_SECONDS=120`。τ 取路线图风险段给的保守值——**0.95 是出厂值，不是调优值**（调优要命中率/串答基准，见"未做"）。TTL 刻意比精确键的 `RETRIEVAL_CACHE_TTL_SECONDS=600` 短一个量级：语义键复用的是"另一句话"的结果，陈旧窗口必须更小，且**没有主动失效钩子**（E2 的失效面按 schema/artifact 走，语义 bucket 里没有可失效的依据）。启动自检那两条（`τ ∉ (0,1]`、`TTL<=0` ⇒ error）**不以 flag 为前提**，与 E4 的 `_MAX` 同口径：`τ=0.5` 是配错，flag 关着时把它咽掉，"改回开"的那天就是"无关问法也算命中"的串答现场，而且安静得没有任何日志。
- **[新增·装配] `agentix/retrieval/usecases/chat_service.py`**：接入点是 `retrieve_scoped_docs_cached` 里**精确键之后、miss 主体之前**的那一段（`docs is None and settings.SEMANTIC_CACHE_ENABLED`），不是 roadmap 原写的 `retrieval_hybrid.py`——理由是取证结果：`search_k` / 融合参数 / top-k 全都参与精确键，混合链内部再挂一层就变成"两个地方各判一次权限"，而 flag 关时那段代码在 hybrid 里根本没有稳定的"精确命中短路"位置可放。语义作用域键由**同一个** `_retrieval_cache_key` 派生：`_compose_key(scope_only=True)` 走 `question_norm=None`，digest 槽位落字面量 `"sem"`——它**不可能是任何问法的摘要**（真实摘要恒为 24 位十六进制），因此"语义键撞上某个精确键"这一类跨权限命中在结构上不成立，而不是"概率很低"。bucket 再带 `|m:<model>`：不同模型的向量空间不共池，否则换了 embedding 模型仍可能读到旧向量。两次调用都经 `asyncio.to_thread` 下放（同步 sqlite-free 但同步 http + 同步锁）；回写条件是 `semantic_peek is not None and docs`——**必须由 peek 背书**，否则"检索空手而归"会被写成一条命中，把空结果粘在整个 TTL 里。坏载荷按未命中处理并照常检索（`_deserialize_docs_from_cache` 抛了只留 warning）。
- **[新增·观测] 零新 label**（纪律 3）：hit/miss 走既有 `record_cache_hit/miss(CACHE_TYPE)`，`CACHE_TYPE="semantic_recall"` 是 `cache_hits_total{cache_type=…}` 的一个**有界新取值**，与既有但**从未被打过**的 `"semantic"`（答案级，`RedisSemanticCache` 那条未接线孤岛）刻意不同名——两种缓存打进同一条时间序列，读的人会把"从没发生过的答案级命中"当成"语义命中为零"。守卫 `test_module_declares_no_new_metric_label_names` 断言本模块发出的 label 名 ⊆ `{cache_type, component}`。
- **[守卫] `tests/test_p128_semantic_recall_cache.py` 新增 67 条**（八组：判定面纯函数 / 存储面（TTL、去重刷新、有界逐出、拒写）/ 配置面（三键默认值、单配置面、非法值不以 flag 为前提）/ 观测面 / embeddings 复用与 fail-open / peek+remember 端到端 / `chat_service` 接线的 **AST 结构守卫** / 真链路行为等价 + 文档同步面）。三处值得留档的判据设计：① **反例必须真反**——只测"相似命中"等于没测 τ，所以用一对**浮点可精确表示**的向量做边界（`cosine([4,3],[3,4]) = 24/25 = 0.96` 与字面量 `0.96` 是同一个 double），"τ=0.96 命中 / τ=0.97 不命中"不依赖任何容差；② 权限隔离用**键差分**证（比对两个键的段序列，只许 digest 槽位不同），不靠"看起来带了租户"；③ 接线用 **AST** 判（peek 调用必须落在以该 flag 为 test 的 `ast.If` 体内、且反向检查该体外没有别处调用、`to_thread` 与 `asyncio.to_thread` 两种写法都认），这是纪律 6 ④ 在 P4 之后**第二次**按同一个教训写：子串判据会被注释里的键名满足。
- **[实测·变异] 十四组单点变异**（`.tmp/p128_mutation.py`，备份字节 → 真树改一处 → 跑 p128 → 按字节还原 → sha256 复核；四个被碰文件还原前后逐位一致：`semantic_cache.py 769d658ccc7b3d05`、`chat_service.py 7857d9c3393b559e`、`settings.py 2dbfaa669854b71d`、`memory_recall.py 1424f8b0407c7235`）。**十三组红，且各自红的用例不重复**：M1 flag 不生效 ⇒ 2 failed（含 `test_flag_off_returns_none_before_any_embedding_is_constructed`）；M2 τ 判据被摘 ⇒ 5 failed；M3 空白载荷照样写 ⇒ 1；M4 维度不符按短边比 ⇒ 1（`test_dimension_mismatch_never_hits`，这条同时钉住策略面）；M5 装配不看 flag ⇒ **7 failed**；M6 peek 不下放线程 ⇒ 2；M7 空载荷也算命中 ⇒ 1；M8 回写不看 peek ⇒ 3；M9 语义作用域退化成按问法分桶 ⇒ **8 failed**；M10 bucket 不带模型名 ⇒ 1；M11 embeddings 每次重建 ⇒ 1；M13 复用签名不看 `base_url` ⇒ 1；M12 自检放过非法 τ ⇒ 1。
  ⚠️ **两处是变异实测逼出来的返工，不是写完就过的**：① **M1 第一轮全绿**——原守卫用"一调用就 raise 的 `create_embeddings` 桩"，而 `semantic_recall_peek` 的 fail-open 会把 raise 咽成 `None`，于是"flag 关"与"embedding 挂了"两种结局在断言眼里长得一模一样。改成**数构造次数**（`built: list` + 计数桩，断言 `built == []` 且 `queries == []`）——要证伪的是"没发生"，不是"没报错"，这句话现在是本仓写等价性守卫的通用口径。② **M11 连绿两轮**——先摘外层快路径、再摘内层判据，两轮都绿；第二轮是真漏（补 `test_concurrent_first_use_constructs_exactly_one_instance`：8 线程 + 0.05s 慢工厂，摘掉双检锁**内层**判据即红，本轮实测 M11 红在该条）；**第一轮是等价变异**：外层快路径摘掉后锁内条件仍挡住重建，构造次数、返回实例、命中判定全不变，唯一变化是"每次多抢一次锁"——为它写断言等于给锁竞争写断言（不可靠），所以 `M11b` 登记在脚本的 `EQUIVALENT` 集合里并**允许它全绿**（本轮实测：67 passed，判词"绿 ⇒ 等价变异（已论证，不写守卫）"）。
- **[同步] 文档面**：`docs/design/性能与优化路线图.md`（§2 E7 条目改写为"✅ 召回级已落地"，含两处与原方案的差异及其取证理由、三条不可让的口径、键与校验、计数面新增取值、验收清单点名 p128、风险与残余项；§4 状态表 E7 行翻 ✅；依赖顺序总结改"E8/E3/E5/E4/E7 已落地，E2 落前半段，剩下 E1、E6、E2 的 TTL 默认值、E7 的 τ 调优与答案级接入"）、`docs/项目实现手册.md`（参数有效性那条 error 补 `SEMANTIC_CACHE_TAU ∈ (0,1]`、`SEMANTIC_CACHE_TTL_SECONDS>0` 并写明"不以 E7 flag 为前提"及三种失败形态；"其它"配置行补三键默认值与模块指针）、`.env.example`（注释形态登记，不设值 ⇒ 不产生自检噪声）。
- **[未做·如实]**：① **命中率与串答率都没有数字**——默认关 ⇒ 本批没有任何生产流量，"τ=0.95 会不会把'第188条诉讼时效'和'第194条最长保护期'判成同一个问题"目前只有代码路径可查；E5 的 `tests/test_perf_smoke.py` 量的是**时延数量级不回退**，不是命中率，别把它当这层的验收依据。② **答案级未接入**——`agentix/application/services/redis_cache.py::RedisSemanticCache` 仍是未接线孤岛，它的 `_cosine_similarity` 与记忆侧那份的**语义差异**（是否 clamp 负值、维度不符怎么退）本批只在文档里点名，没有合并；并之前先接等于把三份判定变四份。③ **多 worker 不共享**：进程内分桶 ⇒ 每个 worker 各自一份，跨进程既不同步也不同步失效；实测形状是"命中率被 worker 数摊薄"，不是正确性问题（权限面在键里）。④ **本批未跑金标**：flag 默认关，链路根本不进语义层，跑一遍只会重新测上一批的检索数字。⑤ 逐出是"超上限逐出最旧"，没有按 bucket 的 LRU；`stats()` 只给 `buckets/entries`，没有可导出的分位。⑥ 没有新增任何指标 label ⇒ 观测面只多一个取值，其余零变化。
- **[验证]**：`tests/test_p128_semantic_recall_cache.py` **67 passed**（变异脚本对照轮 1.38s，`.venv` 单跑 3.98s）；八套合跑（p41 62 + p47 26 + p95 42 + p127 26 + p128 67 + `test_code_hygiene` 14 + p100 17 + p77 51 = 305）**305 passed, 2 warnings in 12.15s**；`ruff check` 对四个新/改 py 文件全绿；`black --check` 对 `semantic_cache.py`／`test_p128` 两文件 unchanged（`chat_service.py`、`retrieval/settings.py` 的既有超长行属历史债，本批没有顺手重排——那会把无关改动混进 diff）。⚠️ **取证环境的坑，值得留一句**：本批第一次合跑用的是宿主 `D:\Anaconda\python.exe`（缺 `lightrag`／`langchain_neo4j`），p47 当场 3 failed + 7 errors，数字与上一批口径完全不同；改用 `.venv/Scripts/python.exe` 才是上面这组。**引用任何毫秒/用例数之前先 `sys.executable` 打一眼**，README §15.2 口径③已经为同一件事写过"栈敏感"。⚠️ **本批没重跑全量** `-m "not integration"`（收尾任务 #54 统一跑），README §11 的套件总数仍是上一口径 4911，P4 与 P5 两批都未提前改写。

### 优化批次 P4 · 路线图 E4 的后半段：embedding 缓存启动预热（2026-10-26）

E4 在路线图里原本只剩半段：hit/miss 计数面早已接线（`record_cache_hit/miss("embedding")` ⇒ `cache_hits_total{cache_type="embedding"}`），缺的是**启动预热**。本批把它落地，并顺手把"计数面已具备"这句话从散文变成可证伪的钉子。

- **[新增·能力] `agentix/retrieval/infra/embedding/prewarm.py`**（纯新增，零既有函数改写）。四条口径写在模块 docstring 里：**默认关**（纪律 1）、**fail-open**（纪律 2，异常只进 `report["error"]`，绝不递给启动路径）、**没有缓存就不预热**（`EMBEDDING_CACHE_ENABLED=false` 时预热只会白打端点，直接跳过并说明原因）、**不新造 label**（纪律 3）。三个入口各有分工：`parse_prewarm_queries()` 纯函数（分隔符只认 `;`／`；`／换行——**半角逗号刻意不在其内**，查询本身含逗号是常态，这条由 `test_parse_prewarm_queries_never_splits_on_a_comma` 单独钉住）、`prewarm_embeddings()` 冷/热两结局的执行体、`run_prewarm_from_settings()` 读配置并给出三种 `skipped` 标签（`disabled` / `cache_disabled` / `empty_list`）。
  两个刻意的实现选择：① **去重在截断之前**——先截断会让重复项吃掉配额，`"a;a;b;c"` + `MAX=2` 得到 1 条而不是 2 条，这条形态由参数化用例与变异各自钉住；② 预热**走 `embed_documents`，不自己写 sqlite**——绕开缓存层直接塞向量等于把 key 计算规则复制成第二份（纪律 6 ④），而 key 里含模型名，抄错就是"换了模型仍读到旧向量"。为此给 `CachedEmbeddings` 加了只读探测 `cached_count_for()`：不发网络、不写缓存、探测异常退化为 0 命中并 `logger.warning`。
- **[新增·开关] 三个键，只在**检索域配置面**存在**：`EMBEDDING_CACHE_PREWARM_ENABLED=false` / `EMBEDDING_CACHE_PREWARM_QUERIES=""` / `EMBEDDING_CACHE_PREWARM_MAX=32`（`agentix/retrieval/settings.py`，与既有 `EMBEDDING_CACHE_*` 家族同处）。**刻意不进 `agentix/config/settings.py`**，理由不是省事：同族的四个缓存键本就住在检索域，再加一份统一层入口就是"同一个开关两个源"；且 `docs/architecture/配置管理.md` 是 p41 `_config_keys()` 的扫描面，把检索域键写进去会当场红 `test_every_documented_key_exists_in_settings`。这条决定有守卫兜着——`test_the_keys_live_on_exactly_one_settings_surface` 断言三键在统一配置层源码里**一次都不许出现**、在检索域源码里必须出现，且**读取端**也只许经 `from agentix.retrieval.settings import settings` 取值；后人想补"统一层同名键"会先撞到它。启动自检那条 `EMBEDDING_CACHE_PREWARM_MAX <= 0 ⇒ error` **不以 flag 为前提**：`MAX=0` 是配置写错，而 flag 关着时把它咽掉，会让"改回开"的那天炸在一个没人核过的值上；关闭的语义已经由 `_ENABLED=false` 表达，不需要 `MAX=0` 兼职。`.env.example` 只用注释形态登记整族键（不设值 ⇒ 不产生自检噪声）。
- **[新增·装配] `agentix/main.py`**：模块级 `_embedding_prewarm_task: asyncio.Task | None = None`（不存句柄 ⇒ asyncio 只持弱引用，任务可能被 GC）+ `_embedding_prewarm_worker()` 经 `asyncio.to_thread` 下放（同步 sqlite + 同步 http，绝不许在事件循环里跑），`_startup` 里紧随配置校验之后按 flag 派发 `create_task(name="embedding-cache-prewarm")`，`_shutdown` 里取消它。派发点在校验之后是因为预热要读 `EMBEDDING_*` 端点配置，配置本身非法时先让校验那条日志说话。**flag 关 ⇒ 不构造 embeddings 实例、不发一条请求**，这一句不是措辞：等价性由"把 `create_embeddings` 换成一调用就 raise 的桩"来证伪。
- **[守卫] `tests/test_p127_embedding_cache_prewarm.py` 新增 26 条**（七组：解析 / 真实缓存两结局 / fail-open / 三种跳过 / 新键默认值与自检与 `.env.example` 与 label / AST 装配 / 文档面）。真结局那组用的是**真 `EmbeddingCache` + 真 `CachedEmbeddings`**（tmp_path 里的 sqlite），不是 mock：冷缓存 ⇒ 一次批量 + 落盘、热缓存 ⇒ 后端 0 次调用、重复项按后端条数计、换模型不共用向量。
- **[实测·变异] 五组单点变异全打在真树上**（备份字节 → 变异 → 跑 p127 → 按字节还原 → sha256 复核；`prewarm.py 09c0b76a…`、`main.py d64f001b…` 还原前后逐位一致），**每一组红的都不是同一条用例**：M1 flag 不生效 ⇒ `test_flag_off_constructs_nothing_and_says_why`；M2 截断提到去重之前 ⇒ `test_parse_prewarm_queries_applies_the_limit_after_dedupe`；M3 命中探测恒报 0 ⇒ `test_warm_cache_prewarm_touches_the_backend_zero_times`；M4 装配不看 flag ⇒ `test_startup_creates_the_prewarm_task_inside_the_flag_branch`；M5 退出不取消 ⇒ `test_shutdown_cancels_the_prewarm_task_itself`。
  ⚠️ **M4/M5 第一版没红，是本批最值得留档的一次自查**：那两条守卫当时是文本子串判据（`"EMBEDDING_CACHE_PREWARM_ENABLED" in startup` + `index()` 比先后），把 `if _retrieval_settings.EMBEDDING_CACHE_PREWARM_ENABLED:` 改成 `if True:` ⇒ **25 条全绿**，因为键名还留在上一行注释里、两个判据都由**散文**满足；把 `_shutdown` 里的 `.cancel()` 换成 `pass` ⇒ **也全绿**，因为同一个函数里滞后采样那段也有 `.cancel()`，子串分不清"取消的是谁"。改成 AST 判据（赋值必须落在**以该 flag 为条件的 `ast.If` 体内**，且分支外不许有该赋值；取消调用的接收者名字必须正好是那个句柄）后，同一对变异各 **1 failed**（复测见上表）。这正是纪律 6 ④ 第二次在本仓咬人：判据必须与它守护的**结构**同源，不能与"文件里出现过这个词"同源。
- **[同步] 文档面**：`docs/design/性能与优化路线图.md`（§1.4 缓存行补预热模块与三键、§2 E4 条目改写为已落地含验收清单、§4 状态表 E4 行翻 ✅、依赖顺序总结改成"E8/E3/E5/E4 已落地，E2 落前半段，关键路径收敛为 （E1 ∥ E6）→ E7"）、`docs/项目实现手册.md`（三键登记进"其它"配置行 + 参数有效性那条 error 补 `EMBEDDING_CACHE_PREWARM_MAX>0` 并写明"刻意不以 flag 为前提"）、`.env.example`。
- **[未做·如实]**：① **预热效果未压测**——默认关 ⇒ 没有任何生产流量，"第一次查询快多少"只有代码路径可查，没有 p95 数字；② 预热清单是**静态配置**，没有从金标/语料自动派生（那是"用一批查询替全体用户预热"的假设，需要 E5 基准背书才谈得上）；③ 多 worker 部署下每个进程各自在启动时预热一轮，**同时启动会不会各打一遍端点**本批未取证（sqlite 文件共享 ⇒ 最坏是重复劳动，不是正确性问题）；④ 没有新增任何指标 label，因此**观测面本批零变化**——预热到底命中了多少，今天只能从启动日志的 `requested/cached/embedded/elapsed_ms` 读。
- **[验证]**：`tests/test_p127_embedding_cache_prewarm.py` **26 passed**（0.81s）；装配/配置/文档面合跑（p41 62 + `test_code_hygiene` 14 + `test_ci_gate_contract` 28 + p124 15 + p66 9）**128 passed**（13.36s）；检索与配置链合跑（p127 + p47 + p35 + p115 + p121）**134 passed**（8.10s）；`ruff check` 对本次四个新/改 py 文件全绿；`black --check` 对 `prewarm.py`／`cache.py`／`test_p127` 三文件 unchanged（`main.py`、`retrieval/settings.py` 的既有超长行属历史债，本批没有顺手重排——那会把无关改动混进 diff）。⚠️ **本批没重跑全量** `-m "not integration"`（收尾任务 #54 统一跑），README §11 的套件总数因此仍是上一口径 4911，未提前改写。

### 质量门禁姿态收口 · 「关掉自己」的三条路径全部封住（2026-10-26）

本批闭合的是 P2-A 末尾以「待点头」形态登记的三条遗留：缺键 ⇒ 建档放行、预测档无人核、`--max-regression` 只活在脚本默认值里。三者共同形态是**不动打分代码、只动门禁的输入或参数，就能让绿灯重新什么都不代表**。授权依据是维护者 2026-10-26 的连续指令（"继续完善实施并落地规划文档中提到的所有涉及的优化""不要停下来，持续工作直到完成"），其中第 1 条确实翻转了 `quality_gate.py` 缺输入时的**既有语义**（原口径把它登记为"待维护者点头"）。

- **[变更·门禁] `evals/quality_gate.py`：缺输入从"建档放行"翻成"拒绝放行"**（三态 ⇒ 四态）。`NOT_ARMED_EXIT_CODE = 2` + `_refuse()`：基线文件不存在、或文件里没有这个指标键，默认打 `::error` 注解 + step summary + 退出码 2，且**基线文件在拒绝路径上不被改写**（模块说明里写死"没有第三种形态"）。`--allow-bootstrap` 保留旧的建档能力，但要调用方**显式**声明（help 文本点名"默认不开——缺输入现在是硬失败"）。`NOT-ARMED` 与 `FAIL` **刻意不同码**（2 ≠ 1）：1 意思是"指标劣化了"，而这里什么都没比对——混在一个码上，日志里就分不开"质量退步"与"没人把关"。`--max-regression` / `--direction` 的代码默认值一个没动（0.02 / higher），改变的只是"线上门禁强度由谁决定"。
  实测（同一份 committed 基线的**副本**，committed 文件未参与）：删 `citation_correctness_applicable` ⇒ `rc = 2`、`::error … 该键未被把关，拒绝放行且不改动基线`、副本 sha256 前后同为 `cf82238b…`、那个键**没有被写回来**——这正是 2026-10-25 独立审查复现过的降级形态（旧写法 `rc=0` + 键被写回，从此本轮值成新地板）。
- **[变更·CI] `.github/workflows/ci.yml`：劣化预算钉进 argv，跳过分支文案纠正**——① 门禁调用段显式传 `"--max-regression", "0.02"`，容差不再继承 argparse 默认（旧形态：往 workflow 加一行 `--max-regression 0.99` 即可静默解除五个指标的门禁，本地与 CI 都不会红）；② 两个跳过分支的 `::warning` 与 step summary 不再宣称"首跑建档"，改用门禁的真标签 `NOT-ARMED` 并各指一条真出处（预测档 ⇒ 内容守卫那条用例名；基线档 ⇒ `write_quality_baseline.py` 是生成物 + 门禁不替你建档）；③ `perf-baseline.yml` 的 nightly **显式**带 `--allow-bootstrap`（缓存里的 p95 基线首跑本就常缺，默认值翻硬失败后不留这条通道就会打成一堆假红——`test_workflow_bootstrap_posture_is_explicit_where_it_is_needed` 守的就是这一行还在）。**`eval-offline` 的行为面零改动**：`skip=true` 判定、五指标循环、以及"它没有 `needs`、与 `test` 并行跑"这三件事都没动，动的是文案与一行参数。
- **[新增·守卫] `tests/test_ci_gate_contract.py` 20 → 28 条**（净增 8 条 + 1 条按语义反转改名：`test_missing_baseline_file_is_loud_but_not_blocking` ⇒ `..._and_blocks_by_default`）。新增的承重项：缺文件默认阻断 / 旗标后仍可建档 / **缺键阻断且 committed 基线逐字节不变** / 空 `{}` 基线同样算缺输入 / 空基线在旗标下仍可建档 / NOT-ARMED 也写 step summary / `test_committed_predictions_cover_the_golden_set_exactly` / `test_ci_pins_the_regression_budget_instead_of_inheriting_the_default` / `test_workflow_bootstrap_posture_is_explicit_where_it_is_needed` / `test_ci_offline_never_bootstraps_and_says_so`。
  内容守卫选在**档面**而不是把 `jsonl` 塞进 p41 的 `_PATH_EXT`：更现实的退化形态是"文件在、被换成空档或短档"，那改写的是**分母**，路径存在性完全拦不住；而 `jsonl` 进白名单会把 73 份文档里所有数据文件名与命令行示例一并纳入扫描，把"数据没录制"和"文档腐烂"混成同一条红。守卫四项：存在 · 非空 · **题数与金标同源**（与 `legal_golden.jsonl` 行数相等，不写死 102，扩样即红）· id 唯一且与金标**双向相等**。
  `test_ci_offline_never_bootstraps_and_says_so` 两次红在自己头上才写对，两条教训留在注释里：① 切片锚点必须带上行首缩进（`text.split("eval-offline:")` 会停在 `::warning title=eval-offline::` 上，切片里没有后面任何断言对象＝假通过形态），故改用 `"\n  eval-offline:"` / `"\n  retrieval-gate:"` 并加一条防空真的 `startswith`；② `--allow-bootstrap` 的断言范围**只覆盖门禁调用那一段**——跳过分支的 summary 文案正是要向读者解释"建档要显式加这个旗标"，那处提及是文档，不是接线。
- **[变更·守卫强度] `tests/test_p124_quality_baseline.py`（仍 15 条，两处收紧措辞与判据）**：空建档那条的失败信息改成"每个指标都会撞 NOT-ARMED（退出码 2）"；真门禁闭环那条现在**两个"未武装"字样都要查**（`rc == 0` 只排除了 NOT-ARMED，没排除"显式建档后返回 0"，PASS 必须由真比对产生）；`test_committed_baseline_covers_every_ci_gate_metric` 的 docstring 按新姿态重写——它管**推送之前**，门禁的 rc 2 管**推送之后**。
- **[实测·变异] 五组变异全部打在真树/副本上**（备份 → 变异 → 合跑 → 还原 → `sha256sum` 复核；三份合跑 `test_p124` + `test_ci_gate_contract` + `test_p41` = 105 条，对照全绿）：删 committed 基线的一个键 **1 红**（昨天 1 红，但旧红只在 pytest 侧、运行时是 rc 0 + 写回）／删 `evals/baselines/quality.json` **5 红**（同昨天）／删 `evals/baseline_predictions.jsonl` **1 红**（昨天 **0 红**）／给 `eval-offline` 的门禁 argv 加 `--allow-bootstrap` **1 红**（昨天无人红）／把跳过分支文案改回"首跑建档 / NOT-ENFORCED" **1 红**（昨天无人红）。还原后 `ci.yml` sha256 前缀 `7bba71fd…` 与变异前逐位一致。
- **[连带后果·如实] 生成器跳过的键从此真的会红 CI**：`write_quality_baseline.py` 对值为 `0.0`／分母为 `0` 的键不归档，旧姿态下那只是"响亮无守卫但绿"，现在是 `NOT-ARMED` 退出码 2。可评审的补救只有两条——重录让它拿到非零分母，或把该指标从 `ci.yml` 的 `METRICS` 显式移除（后者必须留下一行可评审的改动，这正是把预算与键清单都钉在 workflow 里的目的）。
- **[同步] 文档面（口径全部改到四态 + 现测日期）**：`evals/README.md`（§1 目录行、§5.5「三条钉子」编号清单 + 变异实测表、§9 双向取证补"也不含 `NOT-ARMED`"）、`docs/runbooks/评测基线录制.md`（第 7 步生成器理由与新后果、第 8 步命令补 `--max-regression 0.02` 并重跑留痕、**第 8.5 步**整段重构为"旧/新命令对照 → 副本实测 → 四态表 + 三条设计理由 + 连带后果 → 现在的钉子 5 条 → 变异实测表 + 防线位置纠正"、执行留痕表门禁取证行）、`docs/guides/测试分层策略.md`（第 2 条四态 + 预算写死在 ci.yml、录制基线段改口径）、`docs/项目实现手册.md`（术语表 **NOT-ARMED / NOT-ENFORCED 拆成两行**、§20.5 标题"三态"⇒"四态" + 4 行表 + 2≠1 理由 + 三条路径已封住的叙事与 5 行新旧对照表、test 清单行）、`README.md`（§3 门禁行、双向取证块重标日期、CI 段 `eval-offline` 描述、§16 那条"三条自废路径未闭合"翻成已闭合并保留残余）、`docs/migration/Phase15-20_深化规划.md` §4.1（含 17.3 行"本地五次播种"流程标废弃）、`docs/migration/Phase21-26_规划.md` 附录 A（步骤 3/5 打 ✅ 2026-10-25，步骤 4 改由生成器产出，步骤 6 写 `NOT-ARMED ⇒ 提交生成基线 ⇒ 真比对`）。顺带清掉一处新产生的腐烂：PROJECT_SPEC §20.5 末尾仍把已落地的三条补法写作"并列待维护者点头"，改为只留 p123 那条真未闭合的 CLI 级用例。
- **[未收口·如实]** 同一处机理的**下一层**仍无人守：内容守卫判"预测档与金标同源"，判不了"这批预测是不是 `--record` 真跑出来的"——机位、模型温度、`status` 分布（`ok` 55 / `degraded` 46 / `error` 1）还得靠 `docs/runbooks/评测基线录制.md` 的执行留痕人工核对。另外 `eval-offline` 缺**文件**时依旧先 `skip=true`（job 绿、红落在 `test` job 的守卫上），这是刻意保留的分层：缺文件是"输入不存在"，缺键是"输入被掏空"，后者必须当场红在门禁身上。**别把「runner 里写了」读成「基线被降级」，也别把防线说成 job 顺序**（`eval-offline` 与 `test` 并行，真防线是顶层 `permissions: contents: read` + `ci-gate` 挡合并）。
- **[验证]**：四份相关守卫合跑 **119 passed**（`test_p41_doc_consistency` 62 + `test_code_hygiene` 14 + `test_ci_gate_contract` 28 + `test_p124_quality_baseline` 15，3.87s）；三份合跑 **105 passed**（变异对照基准）；workflow 静态校验 `RESULT: OK`（5 workflows / 37 run blocks / 4 python heredocs）——注意这条校验本身踩过一次坑：裸 `bash` 在本机解析到 WSL 的 App Execution Alias，`execvpe(/bin/bash) failed` 会被读成"没有语法问题"，脚本改成显式取 Git Bash 且拿不到就退出码 2。`.venv` 的 `ruff check agentix tests scripts evals kb_ingest` 全绿。⚠️ **未跑到**的：本批没重跑全量 `-m "not integration"`（收尾任务 #54 统一跑），CI 侧新形态（rc 2 与钉住的预算）要等下一次 push 的日志才是真跑证据；本地只验到"同一棵树上的 pytest 侧等价复跑"。

### 优化批次 P3 · 路线图落地：E8 审计 + E2 schema 主动失效（2026-10-26）

- **[新增·性能] E2 · schema 缓存主动失效入口**（`docs/design/性能与优化路线图.md` §2 E2，P0）：DDL 之后旧 schema 的立即生效路径今天只有两条——等 TTL，或重启进程（连带清掉别的缓存）。落地四件：
  - `…/components/text2sql/schema_retrieval/node.py::invalidate_schema_cache(database_url=None) -> int`——按 `f"{database_url}|"` 前缀选条目，返回**真清掉的条数**（清不到照实报 0）；`schema_cache_size()` 供报告核对剩余；包门面 `schema_retrieval/__init__.py` 导出两个符号。
  - `kg_neo4j_conn.py::invalidate_neo4j_schema() -> bool`——**打标记而不是重建**：新增进程级 `_schema_stale`，由 `_schema_refresh_due()` **优先于 TTL** 消费（`NEO4J_SCHEMA_REFRESH_SECONDS` 出厂是关的，若失效挂在"周期刷新开着"这个条件下，默认部署里这个入口就是装饰品）；标记在下一次取用时清掉（不做重试风暴），无实例时返回 `False` 且**不连接**。
  - 伞服务 `agentix/application/services/schema_cache.py::invalidate_schema_caches(database_url=None)` → `{text2sql_schema_cleared, text2sql_schema_cache_remaining, neo4j_schema_marked_stale, errors}`。两套缓存的清理语义本就不同（**一个删条目、一个打标记**），压成同一个返回值形态是这个文件唯一的价值，也是它唯一的风险；纪律 2 的具体形态 = 任一侧抛错只进 `errors`、不外抛，且失败侧的字段**全部**置 `None`（不留半个像真数的值）。
  - 运维面 `POST /api/v1/ops/schema-cache/invalidate`（挂既有 `/ops` 的 `Depends(verify_api_key)`，无新键 ⇒ 不触发纪律 4 的五处同步）：报告自带 `ok` 位；两侧全炸也回 200 + `{ok: false, error}`。**刻意不与 `/ops/memory/clear-user` 的「删除失败 ⇒ 500」同款**——清除结果必须可信，而清缓存失败不改变正确性（迟早按 TTL 收敛）。
- **[未做·如实] E2 的后半段没动默认值**：把 `TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS` 从 0 提到 300 是**行为变更**（缓存自此生效，读到旧 schema 的机会从 0 变成 ≤300s），按 §5 需要 E5 基准与联调 p95 取证才能谈生产默认；本批只把窗口期从"最多 300s 且无人可管"变成"最多 300s，且随时可手动归零"。按库收窄也是刻意的：无条件全清等于给一次调用打掉所有目标库热缓存的权限，是一条可反复利用的延迟放大路径。
- **[守卫] `tests/test_p126_schema_cache_invalidation.py` 新增 13 条**：可见性（TTL 600 下第二次读命中、清掉后第三次读到新 schema）、按库收窄、未知连接串 no-op、TTL=0 时不缓存且入口报 0、失效优先于 TTL 开关、标记只消费一次、无图时诚实 False 且不连接、伞服务双侧报告、伞服务降级、失败侧字段全 None、门面与实现同一对象、端点注册与报告透传、两侧全炸仍回 200 形态。**变异反证三组、红因各不相同**：① Neo4j「stale 优先」退回只看 TTL ⇒ 2 红；② text2sql 失效桩成"报 0 但什么都不清" ⇒ 3 红（第三条是变异本身造出的 node↔包分家，正是门面钉子要抓的形态）；③ 失败分支去掉 `remaining = None` ⇒ 1 红。三组都实测跑过，插件文件在仓库外（`-p` 注入），不入库。
- **[调查] E8 · 出口客户端生命周期全景审计**（roadmap §2 附录，零行为改动）：按 E8 原文的审计法把全仓 HTTP/DB 出口构造点逐个判定，**11 行审计表 + 三句话结论**入文档。结论三类必须分开说：已池化（rerank/Redis 经 W3 的 `shared_client`，Neo4j 单例，embedding sqlite 长连接）／**重建但已 `dispose()`**（text2sql 三处 SQLAlchemy Engine——那是延迟问题，不是资源泄漏，不能挂同一个期号）／**每请求新建且无生命周期托管**（aiohttp ×6 处、同步 `httpx.Client` 热路径 1 处、Milvus `alias="default"` 无显式 close、embedding 客户端在 `retriever=None` 分支重建）。审计表还顺手核掉一条假坏点：`cypher_tools/node.py:128-156` 的 `GraphDatabase.driver(...)` 在 `# TODO: Example` 的**字符串字面量**里，不是代码——grep 判据在这一类问题上会撒谎。
- **[同步] 9 份规划文档与代码/CHANGELOG 对表**（口径腐化清理，全部只动 markdown、零代码改动）：`docs/design/多智能体深度优化.md`（cron 已从"另期/只留接口口"改为 P5 已落地，默认关）、`docs/migration/架构演进评估.md` 与 `docs/migration/Phase15-20_深化规划.md`、`Phase21-26_规划.md`（图内 `interrupt()` 由"async 实测否决"改为 F1e 已落地，并**保留** py3.10 死锁这一限定条件的原文与 `test_p32` 特征化证据，不抹平）、`docs/migration/模块清单.md`（Phase 17 全栈部分 blocked ⇒ 已落地 2026-10-25，并如实保留"当时 Milvus/kb_ingest 未起"）、`docs/migration/Phase28-31_优化规划.md`（用户模拟器：前置已解除、**实现未做**）、`docs/design/编排方案选型论证.md`（`check_hallucinations` 的"建议列入后续接线项"改为代码注释自 2026-09 起就声明的**有意留档**，实测 `build_graph` 注册 9 个节点不含它）、`docs/design/阶段四-前端合并验证记录.md`（title 已切 `My Agentix`／favicon 无引用点 ⇒ 不适用）、`docs/design/检索增强深度优化.md` §8（金标与门禁前置项按现状分层登记，明确"缺领域人员审核产物""缺分层报表"两项仍未闭合）。
- **[验证]**：`tests/test_p126` 13 passed；相关面合跑（p126 + p85 + p12 + p64 + p96 + v1 鉴权 + p68 + p82）**195 passed**；`test_code_hygiene` 14 passed + `test_p41_doc_consistency` 全绿（合跑 87 passed）。本批新文件第一版用 `%s` 写 loguru 日志，被 `test_no_printf_style_loguru_calls_in_package` 当场抓红（loguru 会丢弃位置实参）⇒ 改 `{}`；测试文件第一版直接 import 子包，撞上仓库既有循环导入，按 `test_p85` 同款口径先 `import agentix.application.agents.text2sql` 破环。

### 沙箱阶梯第三档 · §24.3 T2 ⑮⑯（2026-10-25）

- **[新增·安全] ⑮ `subprocess` 档：阶梯上第一面真的算隔离边界的墙**：`agentix/tools/sandbox.py::SubprocessSandbox` + `agentix/tools/subprocess_worker.py`。口径六条，每条都撞过现实才写成这样——① **一次调用一个进程**，启动形态是 **worker 脚本的绝对路径**（`python <abs>/subprocess_worker.py`）而**不是** `python -m agentix.tools.subprocess_worker`：后者会先执行 `agentix/__init__` 与 `agentix/tools/__init__`，实测单项 import **747ms**、p50 **517ms**，更要命的是它让"资源闸先于业务代码"当场作废（包导入在 `setrlimit` 之前就已经把整个数据面拉起来了）；② 子进程侧 `apply_gates` **先**落 `RLIMIT_AS=512MiB` / `RLIMIT_CPU=30s` / `RLIMIT_FSIZE=32MiB`（软限；硬限保持 `-1`，理由见文件注释），可选 uid 降权同样在 import 业务代码之前，**再** import handler；③ 环境变量走与 G2（MCP stdio）**同一份**白名单 `agentix/domain/tool/exec_env.py`，不是复制第二份清单，并注入 `PYTHONIOENCODING=utf-8`（Windows 宿主 cp936 会把协议里的中文打成乱码）；④ 协议是 stdin 一条 JSON 请求 + stdout **带哨兵的一行** JSON 应答（`@@AGENTIX_SANDBOX@@`，单行：被隔离代码往 stdout 打任何东西都不能污染信封），请求 1MiB / 应答 8MiB 各有体积闸；**协议 v2 追加一条归属凭证**：成功信封必须回显**本次调用**的 nonce（父进程每次调用现生成 `secrets.token_hex(16)`）——墙内代码有 stdout 写权限，只靠"哨兵行"认应答等于把伪造应答的笔交给墙内代码；**退出码非 0 一律不采信 stdout 上的任何信封**（否则"先印一条 ok 信封、再被 `RLIMIT_CPU` 打成 SIGKILL"这一串能把伪造载荷当**成功结果**返回，crash 消失）；失败信封**刻意不校验** nonce（协议阶段就错时 worker 确实还拿不到 nonce，硬校验只会把真实原因盖成"没有应答"）；⑤ 取消与超时经 `start_new_session` + `os.killpg` + shielded reap **真杀进程组**；⑥ 失败带 `stage` ∈ {`protocol`,`limits`,`reconstruct`,`invoke`,`serialize`,`crash`}，运维不必猜死在哪一段。绝不静默降级：档位不可用 ⇒ 执行点终局拒绝（`SandboxUnavailable`）。
- **[新增·⑯] 能力探测优先于二进制探测**：`probe_subprocess_caps()` 探的是 `resource.setrlimit(RLIMIT_AS/CPU/FSIZE)` 在不在、POSIX 进程组 kill 能不能用，**不是**"某个 binary 存在吗"。探不过 ⇒ `subprocess` 档在该机器执行点响亮拒绝：Windows 上**一个 handler 都不调、一个进程都不起**（这是 ⑮ 唯一诚实的平台口径，也是本批 POSIX 证据只能在 Linux 取的原因）。
- **[新增·开关] 三个键全部默认关/不生效**（纪律 1）：`TOOL_SANDBOX_SUBPROCESS_ENABLED=False`、`TOOL_SANDBOX_SUBPROCESS_UID=-1`（`-1` 才是不降权；填 `0` 等于出厂以 root 跑 handler）、`TOOL_SANDBOX_SUBPROCESS_ENV_EXTRA_KEYS=""`。五处同步齐备（`settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `docs/architecture/配置管理.md` §5 / `tests/test_p41_doc_consistency.py::test_new_settings_are_documented`），compose 转发 `${…:-False}` / `${…--1}` / `${…:-}`，**staging override 刻意没有钉开档位**——预发端口开着而 ⑮ 关着，于是"给某条工具加 `sandbox='subprocess'` 声明"在预发是**全量失败**而不是悄悄跑起来（fail-closed 的正身，开启顺序写在 ADR-0007 的缓解段）。两位新键也**不进** `main.py::_INSECURE_GATE_FLAGS`：判据是"关着会让实例不该对外"，而它关着时与基线逐位相同，列进去只会稀释那条告警（与 `TOOL_HANDLER_CONTRACT_ENABLED` 同口径，守卫用 AST 读源码钉住这个"不点名"，反向锚点是 `MCP_SUBPROCESS_ENV_SCRUB` 必须在清单里）。
- **[新增·守卫] `tests/test_p125_subprocess_sandbox.py` p125 当时 80 条**（六层：纯逻辑 / 协议与信封 / 注册表与开关 / 真子进程 / 契约与文档 / 部署物），Windows 可跑 **63** 条、**17** 条 POSIX 专属 `skip`（由 Linux/CI 真跑，不是拿 skip 当通过）；`tests/test_p114_execution_sandbox.py` 66→**68** 条（⑮ 落地后 `implemented_kinds()` 与阶梯词汇表的差集只剩 `{container}`，这条差集本身就是 ⑮ 的落地证据）。反证形态各有专门一条：端口开着而档位关着 ⇒ 拒绝的 reason/retryable/不调 handler/不起进程与 ⑮ 不存在时**行为面逐位相同、差别仅在拒绝措辞**；档位开着而端口关着 ⇒ 一个字都不查 `spec.sandbox`（新键不许越过自己的总闸）。
- **[返工·独立审查第 1 轮] 1 严重 + 3 一般项，逐条按"最小必要修改"处置**：① **严重**（伪造应答可能被当成成功结果）：旧口径「取最后一条哨兵」与「rc≠0 但 stdout 里有哨兵就仍采信封」两条合起来，被隔离代码只要 `print` 一行就能凭空造出结果；"先印一条 ok 信封、再被 `RLIMIT_CPU` 打成 SIGKILL"这一串还会把 crash 盖掉。改法：**协议 v2 加归属凭证 nonce**（父进程每次调用现生成 `secrets.token_hex(16)`，成功信封必须回显；rc≠0 一律不采信 stdout 上的任何信封）。失败信封**刻意不校验** nonce——协议阶段就错时 worker 确实拿不到 nonce，硬校验只会把"缺 key""协议版本不符"盖成"没有应答"；不对称是刻意的，理由写在 worker 的 docstring 里。② **一般**（`skip` 谓词与探测分家）：旧写法 `not POSIX` 在"POSIX 但没有 `resource` 扩展"的机器上，会把 POSIX 专属用例从"响亮地跳过"变成"各自失败"（失败会被误读成代码缺陷）；判据改成 `probe_subprocess_caps()["runnable"]`（与执行点同源），并补一条用例把"判据 = 探测真值"钉住。③ **一般**（代理变量这条边界没写清）：`HTTP_PROXY`/`HTTPS_PROXY` 在环境白名单里，而代理 URL 的 `user:pass@` **本身就是凭据**；本批**不改行为**（守纪律 5：行为变更要先征得维护者同意），改为在代码注释、runtime-guardrails §15.10 与 README §16 如实登记这条已知边界，并用特征化用例钉住"代理变量确实过界"这一事实。④ **一般**（体积闸余量没写清）：父进程侧读 stdout 的上限刻意是 worker 侧应答上限的 **2×**（留给业务日志），"应答超限"由子进程侧改写成失败信封；改法是把这条写进注释与用例，并钉住"超限改写后 nonce 必须照抄"——否则父进程会把"结果太大"读成"应答不可信"，真实原因被盖掉。
- **[返工·独立审查第 2 轮] 1 严重 + 4 一般项，逐条按"最小必要修改"处置**：① **严重**（归属凭证仍可被绕过——第 1 轮那一项**没有真正闭合**）：第 1 轮把"取最后一条哨兵"换成"只认 nonce 匹配的那条"，但**两侧的成功/失败判定不同源**——解析器用 `e.get("ok") is True` 收 trusted 桶、用 `is not True` 收"失败桶"，而终局判定用的是**真值** `if not envelope.get("ok"):`。于是墙内代码印一条 `"ok": 1`（JSON 数字，不是布尔）的伪造件：进不了 trusted 桶、又被失败桶收下，最后被真值判成**成功**，`data` 原样返回——**nonce 校验在这条路上压根没执行过**（第 2 轮审查实测复现：`BAD forged result accepted: {"pwned": true}`）。改法：`ok` 只认**严格布尔**（`True` / `False`），`1` / `1.0` / `"true"` / 缺失一律按**畸形信封**拒、**刻意不归拢进失败桶**；终局那一行改成 `is not True`。**两层各自独立**：只改回其中一层，伪造件仍被拒——但两次单点回归的**拒因不同**（这正说明两层判的不是同一件事）：只改终局判定 ⇒ 解析器那层还在，仍拒成 `malformed response envelope: 'ok' must be a boolean, got 1`；只改失败桶 ⇒ `ok=1` 被归进失败桶，终局判定把它拒成 `[unknown] no error given`；**两层一起**改回旧写法才复现 `BAD forged result accepted: {"pwned": true}`（同一份变异副本上跑 p125 ⇒ **5 failed / 67 passed / 2 skipped**，红的正是本批新增的三条守卫）。新增 6 条用例（纯函数侧三种非布尔取值 / 终局判定独立钉子 / 真进程边界的非布尔伪造件）。② **一般**（runbook 声称文件闸的触发覆盖存在，实际不存在，且留着死探针）：runbook 数字表那格写着"触发靠 p125 的 `RLIMIT_FSIZE` 用例"，而仓里**没有**触发它的那条，`file_writer` 探针 **0 引用**。改法：**补一条真触发用例**（墙内写 64MiB 进 32MiB 的闸，真进程里拿到 `errno 27` = `EFBIG`；另一种结局是被 `SIGXFSZ` 打死，rc≠0——两种都算闸生效，唯一不算生效的形态是"64MiB 写成功了"），并把死探针接到这条用例上；探针的落盘目录同时从 cwd 改到临时目录（闸生效的形态之一是进程被 `SIGXFSZ` 打死，那种形态下 `finally` 清理不执行，留在仓库工作树里就是一坨要人手动收拾的垃圾）。runbook 那一格改成"**已真触发**"，CPU 秒闸那半**如实写成"仍只回读、未取证触发"**（触发它要烧掉 30 个 CPU 秒，代价与收益不成比例）。③ **一般**（代码注释里的条数陈旧）：注释写的 POSIX 条数与实际不符——改成不写数字（数字的真源只有 README §11 与 runbook 的当轮实测）。④ **一般**（flag-off 的"逐位等价"措辞要收紧）：⑭ 基线的拒绝措辞是 `"not implemented"`，本批是 `"switched off"` 并点名开关，**行为面**（reason 分类 / `retryable=False` / 不调 handler / 不起进程）四项确实逐位相同，差的只是诊断串。文档口径改成"**差别仅在拒绝措辞**"，免得后来者按字面判成"不等价"。⑤ **一般**（失败信封不校验 nonce 的副作用没言明）：墙内代码可以印一条伪造的失败信封**顶替**真实失败原因——只盖住诊断，**换不到任何数据**。已在 runtime-guardrails §15.10、README §16（已知边界从两条变三条）与代码注释里写明。
- **[返工·独立审查第 3 轮] 6 条一般项（G1–G6）逐条按"最小必要修改"处置，且**只动测试与文档面**：运行时三个文件（`agentix/tools/sandbox.py`、`agentix/tools/subprocess_worker.py`、`tests/subprocess_probe_handlers.py`）的 sha256 与第 2 轮返工收口时逐位一致 ⇒ 纪律 5（"任何对数据、脚本、测试基线的改动需先征得维护者同意"）没被绕过，改的只是守卫与文字。① **G1**（"缺 `ok` 键"与 `ok=null` 两条路径没有守卫）：`test_ok_must_be_a_strict_boolean_not_a_truthy_value` 的参数化从 3 例扩到 **5 例**（新增 `json_null`、`key_absent`；后者用 `_OMIT_OK = object()` 哨兵构造"键不存在"，与显式 `null` 是**两种形态**，不能靠一个 `None` 顺手覆盖）。变异复验：把 `non_boolean` 的判据放宽成"再把 `None` 排除出去"⇒ **恰好 `json_null` 与 `key_absent` 两条变红**（`2 failed / 3 passed`），还原后 `sandbox.py` 与宿主逐位一致。② **G2**（本文件在模块顶部自算第二个平台判点 `POSIX = os.name == "posix"`，与 `probe_subprocess_caps()` 各算各的——纪律 6 ④要的正是"诊断口径与执行链判定同源"）：删掉那个判点，5 处使用全改读 `_CAPS["posix"]`，并补一条 **AST 守卫** `test_this_file_declares_no_second_platform_predicate`（本文件里任何 `os.name` 属性访问都算违规）。变异复验：把一处 `if _CAPS["posix"]:` 改回 `if (os.name == "posix"):` ⇒ 新守卫**当场变红**，还原后测试文件 sha256 一致。③ **G3**（`docs/architecture/配置管理.md` 那一行仍写"关 = 与 ⑭ 落地时逐位等价"，比另外三处松）：改成"**行为面逐位等价，差别仅在拒绝措辞**"，并把四项行为面（reason 分类 / `retryable=False` / 不调 handler / 不起进程）点名，与 `运行时护栏.md` §15.10、README §7、`[返工·第 2 轮]④` 同口径。④ **G4**（CHANGELOG 与 README 写着"既有用例零删除零改写"，与 p114 的三条实改不符）：改成"**零删除，但不是零改写**"，逐条列出改了哪三条、为什么必须改（差集守卫要显式把开关钉成 `False`，否则 `.env`/CI 变量一改就从"差集可见"退化成"看运气"；两种"不可用"的措辞必须继续分开，因为修法不同——合成一句运维照着报错改不动；只钉 `subprocess` 的那条若不按两档参数化，⑮ 落地当天等于把 fail-closed 的证据换成"开开关"的证据、`container` 那条腿没人钉），`git diff --numstat` 对 p114 现在（含本轮两处 black 折行）是 **66 added / 13 deleted**、66→68 条。⑤ **G5**（取证 runbook 没有强制的源编译步骤）：第 3 步之前补"清字节码"硬步骤，并写清**为什么它是硬性的**——CPython 判 `.pyc` 过期只看源文件的 `(mtime, size)`，bind-mount 与 `cp -a` 都保留这两项 ⇒ 容器直接复用**宿主编的字节码**，而 `PYTHONDONTWRITEBYTECODE=1` 只挡写、不挡读；第 5 步补 mypy 缓存同理（它的判定依据也是源文件指纹）。第 4.7 步的变异轮改用 `tar --exclude=__pycache__` 出副本并打印副本内 `__pycache__` 目录数（必须是 0）才继续：**变异反证比常规复跑更依赖这一步**，因为"变异没变红"会被读成"这层闸不承重"。⑥ **G6**（套件总数有 `TestTestCountHasSingleSource` 守着，**单文件条数没有**，于是本文件 74→78 这三轮里六处手抄数字同时腐烂、而全量回归一个字都没红）：把"文档写的条数"改成**生成物**——新增 `test_docs_declaring_this_files_case_count_match_the_live_collection`，从**本次真实采集**的 `session.items`（就是产出终端那行"N passed / M skipped"的同一份清单）现算条数，再核所有写 `p125 共 N 条` 这个短语的文档；短语是刻意的（历史口径块写"当时 N 条"，不会被当成当前声明），采集被 `-k` / nodeid / `--lf` 缩窄时它**响亮跳过**而不是假绿。**这条守卫自己先红过一次**：新增它时 runbook 还写着 74 ⇒ `1 failed（写 74，本轮实采 78）`，改文档后才绿——这就是它能承重的证据。
- **[返工·独立审查第 4 轮] 1 严重（S1）+ 2 一般（N1/N2），仍然只动测试与文档面**：运行时三文件 sha256 本轮再次逐位一致（`a95a6415…` / `ef64074a…` / `e8f45bef…`），改动全落在 `tests/test_p125_subprocess_sandbox.py` 与 markdown（p125 78→**80** 条，两条新用例都跨平台）。① **严重 S1**：第 3 轮刚加的条数守卫**自己**留了一个第二身份源——它用 `item.location[0]` 认"这条用例属不属于本文件"，而 `location` 派生自 code object 的 `co_filename`，那是**编译时**写进 `.pyc` 的路径。bind-mount 让容器复用宿主 Windows 编的字节码时，同一个用例的两份身份分家（采集侧 `/repo/tests/…`、`co_filename` `D:\Code\…\tests\…`），而 **POSIX 上反斜杠不是路径分隔符** ⇒ `Path(...).name` 把整条 Windows 字符串当成一个文件名 ⇒ 本文件被数成 **0 条**，守卫红在**取证流程**上（容器 root 与非 root 各 `1 failed / 143 passed / 2 skipped`）而不是红在它该核的文档上——最坏的地方在于这条红长得像"文档又腐烂了"，会把人往错方向赶。修法：身份源换成采集侧的 `item.nodeid`（pytest 用 `/` 拼、相对 rootdir、两平台同形），并加 `test_the_case_count_identity_source_survives_a_stale_pyc`——直接构造"nodeid 是 POSIX 相对路径 + location 是 Windows 绝对路径"的假采集项，**不需要真陈旧 `.pyc` 也复现得出案发形态**。复验：**故意不做第 3 步字节码卫生**的容器轮 root 7.30s / 非 root 8.70s 各 **146 通过 / 2 跳过**，`-rs` 的 skip 行照旧打印 `D:\Code\My-Agentix\tests\…`（**刻意不写行号**：行号随源码漂，写死就等于再造一处手抄面）（复用确实发生，只是不再影响判定）。这是纪律 6 ④"同一个判定不许两个源"第二次咬人：第一次是 skip 判据与探测分家（第 1 轮②），这次是"用例归属"与"采集"分家。② **一般 N1**：缩窄判据漏了 `--deselect` —— 局部复跑时守卫**红**（把人为缩窄报成文档腐烂），已把 `config.option.deselect` 并入跳过判据，实测 `--deselect` 一条 ⇒ `61 passed / 18 skipped / 1 deselected`（多出来的那条 skip 就是守卫自己）；另把"`--ignore` 掉本文件时守卫**根本不执行**、那是真空不是绿"写进 docstring 与 runbook，读日志先看 `collected` 行。③ **一般 N2**：第 3 轮那条 AST 守卫只认字面 `os.name`，`getattr(os, "name")` / `import os as _os` / `sys.platform` / `from os import name` 全都过得去 ⇒ 把判点抽成纯函数 `_find_platform_predicates(source)`（按"模块 + 属性"二元组抓，覆盖别名导入、getattr、from-import），守卫本体读它，另加 `test_the_platform_predicate_guard_catches_the_evasive_forms` 钉**六种绕法全抓 + 三种合法读法不误抓**（`os.getuid()` / `os.defpath` / `os.path.*` / `sys.version_info` / 探测返回值——误抓的实际代价是逼人把判点藏得更深）。这条新用例**当场逼出一个真 bug**：第一版实现对"片段里没有 import"的 `os.name` 返回空 ⇒ 守卫对合成源码说谎；修法是默认绑定 `os`/`sys` 两个名字、再叠加别名。
- **[返工·独立审查第 5 轮] 判**通过**，随结论登记两条一般项并同轮闭合（仍然只动测试与文档面，运行时三文件 sha256 复验逐位一致）**：审查员独立复现了 S1 的三条关键证据——容器不做字节码卫生跑 p125+p114 ⇒ 146/2、`-rs` 仍带 `D:\Code\…` 路径；把身份源改回 `location` 的变异副本 ⇒ 钉子用例当场红；把某份文档的条数改成 81 的变异副本 ⇒ 条数守卫红并点名差异。N1 的四种缩窄（`--deselect` / `-k` / 带 `::` 的 nodeid / `--lf`）实测全部是响亮 skip 而不是 red。闭合的两条一般项：① **N3**——README §11 里 2026-10-23 的历史块标题还写着"当前口径"，与它下面四块"上一口径"的标注相悖（同一份文档里两个"当前"＝核对基准歧义）；改成"上一口径（2026-10-23，同日第三轮之后）"，`grep -c 当前口径 README.md` 现在只剩 1。② **N4**——`_find_platform_predicates` 的 docstring 用"读过就无处可藏"超幅声称：审查员实测 `o = __import__("os"); o.name`、`os.__getattribute__("name")`、`operator.attrgetter("name")(os)`、`getattr(os, "na" + "me")` 四种形态**返回空**（漏抓）；docstring 改成"抓的是读平台属性这个动作，覆盖别名导入 / getattr / from-import；**不覆盖**上述四种——要绕得故意，成本高于收益"，并把口径写成"顺手写的判点跑不掉，不是对抗性隐藏跑不掉"（守卫的声称范围比守卫本身更容易伤人：写成后者，下一个人就会拿它当证据放行真正该拦的东西）。同轮另把钉子用例里那处 `if os.sep == "/"` 改成读 `_CAPS["posix"]`：按守卫自己声明的口径（"本文件所有 POSIX 判定只能读探测的返回值"）它本就在该拦的范围内，只是实现只盯 `os.name` / `sys.platform` 两个属性名、抓不到 `os.sep`——**声称范围比实现范围宽**这一条本轮如实承认，但没有顺手扩 `_PLATFORM_MODULES`：扩了就得在钉子用例里加一条对应的"绕法参数"才能证明这条规则真咬人，而加参数会把本文件条数从 80 推到 81、牵动六份文档——为一个本文件里已经不再存在的判点付这个代价不划算（`grep -c os.sep tests/test_p125_subprocess_sandbox.py` 现在是 0，全仓 `tests/` 与 `agentix/` 也搜不到 `os.sep`）。它服务于断言而非跳过、且与探测真值恰好等值，但纪律 6 ④ 不认"恰好等值"。**改的是判点来源而不是用例数，所以条数仍是 80**，六份文档一个字没动——这正是那条生成物守卫希望的形态：数字只在真加了用例时才漂。闭合后复跑：全量 `-m "not integration"` 在**最终树**上 **4911 passed / 31 skipped / 3 deselected / 0 failed**（411.20s，与闭合前的 406.67s 逐位一致——改的是判点来源与文字，没有动用例数）、定向合跑 **260 passed / 17 skipped**（16.63s）、`ruff` 全绿、`black --check` 对 p125 unchanged、容器（依旧不清字节码，跑在全量之后 ⇒ 复用的正是本轮新编的宿主 `.pyc`）**146 passed / 2 skipped in 6.95s** 且 `-rs` 两行照旧是 Windows 路径。
- **[实测·容器取证] `python:3.12-slim` 里 root 与非 root 各两轮**（两轮 `test_p125` + `test_p114` 合跑均为 **146 通过 / 2 跳过**；148 collected = p125 当时 80 条 + p114 68 条。2026-10-26 第 4 轮审查返工后复跑，root 7.30s / 非 root 8.70s，容器 `Python 3.12.15`；这两轮**故意跳过 runbook 第 3 步的字节码卫生**，用来复验 S1 改的是身份源而不是运气——`-rs` 的 skip 行因此带着 `D:\Code\…` 这条 Windows 路径，即"容器正在复用宿主编的字节码"的现场证据，而计数已不受它影响），逐项真读到：`RLIMIT_AS` 回读 `[536870912, -1]`、4GiB 分配被拦成 `MemoryError`、墙内 `visible_keys` 只有 `HOME/LANG/PATH/PYTHONIOENCODING/PYTHONPATH`、父进程注入的 `DATABASE_URL`/`LLM_API_KEY` 不过界（`opened=false reason=missing_env:'DATABASE_URL'`）、root 轮 `uid/euid=65534`、非 root 轮每一条探针都以 `[limits] uid drop to 65534 failed: Operation not permitted` 终局失败（**同错** ⇒ 降权失败发生在 import 业务代码之前，后面根本没有调用）、墙内 `sys.modules` 无 `agentix.*`、故意抛错回报 `[invoke] ValueError: p125_deliberate_failure`、**墙内代码自己印一条成功信封 ⇒ 父进程抛 `untrusted success envelope`（独立重放脚本实测；把父进程那条 nonce 规则改回旧写法，同一脚本会把伪造载荷当成功结果 `{"pwned": true}` 返回）**、**墙内代码印一条非布尔 `ok` 的伪造件（`1` / `1.0` / `"true"` / `null` 四例走真进程重放，"整个 `ok` 键缺失"那一形态只在纯函数侧钉——探针写不出缺键信封）⇒ 父进程抛 `malformed response envelope: 'ok' must be a boolean`（真子进程边界实测）；**三层变异各自实测**：只改终局判定 ⇒ 五条仍按畸形信封拒、同一副本上 p125 **1 红**（红的正是那条"把解析器桩成宽的、只钉终局"的独立钉子）；只改失败桶 ⇒ 仍全部被拒但**拒因退化**成 `[unknown] no error given`、**6 红**；两层一起改回旧写法 ⇒ `1`/`1.0`/`"true"` 被当成功结果 `{"pwned": true}` 返回、**7 红**（`null` 在这种旧口径下仍被拒，是真值判定对 `None` 求值为真的**巧合**，新口径不靠巧合）**、**墙内写 64MiB 进 32MiB 的文件闸 ⇒ 拿到 `errno 27`（`EFBIG`），文件没写出来**。容器内 `ruff`（0.5.7）全绿、`mypy agentix/domain`（1.8.0）`Success: no issues found in 38 source files`。延迟（同一 handler、同机、20 次样本）：裸解释器 6.6/7.4/8.4ms、`in_process` p50 **0.001ms**、`thread` **0.074ms**、`subprocess` 容器本机 FS p50 **≈50ms**（两轮 43.7 / 49.9）、Windows bind-mount min 133–141 / p50 **142–308ms**（宿主 FS 抖动，引用必须连环境一起说）。步骤与数字表见 `docs/runbooks/子进程沙箱取证.md`。
- **[同步] 文档面**：`README.md`（§3 运行时内核新增"执行沙箱（⑮ 起有墙）"行、§7 Phase S bullet 补 ⑮ 与其代价、§11 测试真源数、§14 文档索引新增取证 runbook、§16 原"**没有执行隔离墙，只有一个刚建好的插座**"整条改写为"有一面墙，但默认不开、只挡它声明过的那几个维度、且平台不对称"并把 G1–G13 改 G1–G14、四项默认全关）、`SECURITY.md`（缓解表新增"子进程沙箱档（⑮：真的有一面墙）"、部署建议里 `EXECUTION_SANDBOX_ENABLED` 条目说明预发**没有**钉档位、已知边界同步）、`docs/architecture/运行时护栏.md`（概览表 ⑮ 行、flag 表三新键、§15.10 标题改"⑭ 是插座，⑮ 才是墙"并补两条 ⑮ 口径、§15.11 未收口段把"要等 ⑮ 才改写"改成现状 + ⑰ 剩余面、回归覆盖 p114/p125 两段计数）、`docs/项目实现手册.md`（§23.8 重新评估触发补记实测、§24.1 墙句按"默认路径仍然没有墙 / 开了 ⑮ 才有墙"分层改写、§24.2 G14 严重度升为高并记 ⑮ 落地时当场撞见的形态、§24.3 ⑮⑯ 行翻 ✅ 与 T2 验收句）、`docs/architecture/adr/0007-执行沙箱端口.md`（后果的负面/缓解两段按 ⑮ 落地改写，含"先钉 override 再改声明"的开启顺序）、`docs/源码研读笔记.md`（§2.4 "没有这一层"改为"方法已采纳、能力面小得多"，X.1 行补 ⑭⑮⑯ 现状与仍未接线的 namespace/seccomp/只读根/出网）、`docs/runbooks/子进程沙箱取证.md`（第 3 步的当轮计数、第 4 步数字表的文件闸那一格改成"**已真触发**"、CPU 秒闸如实写成"仍只回读未取证触发"、**新增第 4.7 步**：`ok` 非布尔的伪造件在真进程边界上的取证与"两层一起改回才复现绕过"的反证）、`.env.example` / `docs/architecture/配置管理.md` / `agentix/config/selfcheck.py`（纪律 4 的三处）。
- **[未做·如实]**：⑰ 的四件（只读根、指定可写目录、出网默认拒绝、`RLIMIT_NPROC`）一件都没做 ⇒ **是墙，不是笼子**；没有照原规划把"MCP server 与一切外部工具默认 `subprocess`"落地（那是行为变更，会改掉所有既有工具的执行位置与延迟量级），现在是**按工具逐条声明**；`ToolExecutor.register_handler` 只在 `not tool_registry.has_tool(name)` 时登记 spec ⇒ 同名重复注册会**静默丢弃后一份 `sandbox` 声明**（§24.2 G14），本批只把它升为"高"并留档，**未改行为**（纪律 5）；warm pool（复用进程摊掉每次调用的生命周期成本）是 ⑰ 之前的可选优化，未做。
- **[验证]**：Windows 宿主（Python 3.12.13，`.venv`）全量离线套件 `pytest -m "not integration" -q` → **4911 passed / 31 skipped / 3 deselected / 0 failed**（411.20s，单机离线、未开并行；本批四次全量 455.76s / 415.30s / 406.67s / 411.20s **计数逐位一致**，末次跑在**第 5 轮登记项闭合之后的最终树**上，引用末次），加法逐项可对账（相对第 2 轮返工后口径 4905：**+6 = 第 3 轮 +4 + 本轮 +2，全部是 p125 在 Windows 上真跑的条数 57→61→63**——第 3 轮新增的四条（`json_null` / `key_absent` 两例、AST 平台判点守卫、条数生成物守卫）与本轮新增的两条（S1 的假采集项身份源钉子、N2 的六种绕法钉子）**全都跨平台**，所以 **skipped 仍是 31、deselected 仍是 3，两处零变化**（skipped 的 +2 发生在第 3 轮那两条 POSIX 专属用例上，29→31）；既有用例**零删除，但不是零改写**——`tests/test_p114_execution_sandbox.py` 有三条按 ⑮ 落地的必要改写：`test_unimplemented_rungs_are_exactly_the_future_items` 加 `monkeypatch` 把档位开关钉成 `False` 再断言差集（不钉就是"看运气"），`test_future_rungs_refuse_with_their_own_reason` 的参数表从"两种未实现"改成"两种不可用 + 各自的措辞"（`switched off` / `not implemented` 必须继续分开，因为修法不同），`test_unimplemented_rung_refuses_without_calling_handler` **改名**为 `test_unavailable_rung_refuses_without_calling_handler` 并按两档参数化（原先只钉 `subprocess`，⑮ 落地后不改就等于把 fail-closed 的证据换成"开开关"的证据，`container` 那条腿没人钉了）；另**纯新增** `test_enabling_the_subprocess_tier_narrows_the_gap_to_container_only`。`git diff --numstat` 对 p114 是 66 added / 13 deleted（本轮那两处 black 折行贡献 +4 行，断言与语义零变化），计数 66→68 的来源是那两条参数化/新增带来的 +2——把这一处写成"零改写"会把一次有据可查的口径迁移藏起来）。**第 3 轮对 p114 只动了两处 black 折行**（第 2 轮把参数化后的函数签名与那条断言推到 104 / 110 列），零行为改动、计数仍 68 条。定向复核：`test_p41 + test_p125 + test_p114 + test_p115 + test_p66 + test_code_hygiene` 合跑 **260 passed / 17 skipped**（16.86s；+2 就是本轮 p125 那两条新增，17 条 skip 全部是 p125 的 POSIX 专属，由 Linux 真跑而非跳过即算过）；`python scripts/gen_staging_matrix.py --check` 退出 0（"15 行由 override/Settings 生成，1 行登记为手填"）。CI 同一把尺子：`ruff check agentix tests scripts evals kb_ingest` 全绿、`mypy agentix/domain --config-file=pyproject.toml` 38 文件 0 错、`black --check` 对本次五个新/改 py 文件（`agentix/tools/sandbox.py`、`agentix/tools/subprocess_worker.py`、`tests/test_p125_subprocess_sandbox.py`、`tests/subprocess_probe_handlers.py`、`agentix/domain/tool/exec_env.py`）全部 unchanged——**用的是 black 而不是 `ruff format`**，因为 CI 从不跑后者，拿它当基准会重排一批既有文件。全仓口径也登记清楚，免得"五个文件绿"被读成"仓库绿"：`black --check agentix scripts evals kb_ingest` 当场报 **397 个文件会被重排 / 228 个 unchanged**（CI 因此把这一步设成 `continue-on-error`，README §14 的工具链口径就是这句话），而 `tests/` 本就不在 CI 的 black 范围内——p114 里除本轮那两处外还有**五处既有**超长行，本轮没有顺手重排（那会把与本批无关的历史债混进 diff，也让"零行为改动"这句话失去可读的 diff 作证据）。Linux 侧（`python:3.12-slim` 容器、root 与非 root 两轮，均为 **146 通过 / 2 跳过**，2026-10-26 第 4 轮审查返工后复跑；148 collected = p125 当时 80 条 + p114 68 条）见上一条 [实测·容器取证]。**文档面收口后又复跑了一轮 root（默认用户，`PYTHONDONTWRITEBYTECODE=1`、刻意不清字节码）⇒ `collected 148 items` / **146 passed, 2 skipped in 9.62s**，`-rs` 两行仍打印 `D:\Code\My-Agentix\tests\test_p125_subprocess_sandbox.py`（skip 行的行号随源码漂，此处刻意不写）——这一轮的意义在**时序**：条数守卫要读六份文档，文档刚改完就绿，说明"生成物核手抄面"这条链在最终树上成立，而不是靠上一轮的记忆。**未验到的**：本批没有真容器部署形态下的端到端验证（compose 里 backend 的 `AGENTIX_CONTAINER_USER` 默认 `0:0` 仍是 root，档位也默认关），⑮ 目前只在"测试与探针在 Linux 容器里真跑过"这个意义上成立；CI（ubuntu / Python 3.10）对这 17 条 POSIX 用例的真跑要等下一次 push 的日志。

### 优化批次 P2-A · 录制基线并武装质量门禁（2026-10-25）

- **[新增·录制档] `evals/recording_env.py`：宿主机录制环境装配 + 录制前自检**：`.env` 是**容器视角**写的（`DATABASE_URL` 指 `postgres:5432`、`NEO4J_URI` 指 `neo4j:7687`、`LLM_BASE_URL` 指远端 API），在 Windows 宿主机上要么解析不到、要么违反「模型一律本地」。新增模块按 **进程环境 > `.env.local`（git 忽略） > 录制档默认** 装配，主机端口从 `docker-compose.yml` 的 `ports:` **推导**（纪律 7：不抄表；`${VAR:-default}` 也解析），只对内网名做替换、采纳 `.env` 的白名单键；装配清单**逐条脱敏打印**（连接串只留 user，密码替换 `***`），不留暗改。`preflight_local_recording()` 在提问前核：端点模型是否存在、KB `document`/`chunk` 行数、Neo4j 节点数、`articles` 行数、Redis ping、kb_ingest 可达性——不齐退出码 2（自检几秒，跳过它的代价是两小时后一份废基线）。
- **[变更·工具] `run_eval.py --local-llm` + 预测逐行落盘**：`--record` 走本地端点时先装配环境、再自检、再做本地模型适配（复用 `scripts/demo/local_kb_demo.py` 的探测，不造第二套口径）。预测文件从「跑完一次性写」改成**逐行 `flush()`** 并打进度行 `[n/102] id route=… status=… src=… ms`：录制是 80 分钟量级的脱离进程，中断必须留下可核对的部分产物。实测中断读取安全性：写到第 78 行时读取，102 行全部可 JSON 解析、id 唯一、末行以换行收尾。
- **[新增·生成物] `evals/write_quality_baseline.py`：基线只有一个来源**（旧 runbook 的「逐指标 `quality_gate --current` 建档再手动提交」废弃）。三条闸：① **落盘值为 0.0 的键不归档**（分母为 0 或分母不为 0 都算；判定在 `round(…, 4)` **之后**，因为恒 PASS 的形态由写进文件的那个数决定，`0.00004` 折成 `0.0` 同样是常绿灯），因为 `0.0` 基线的阈值仍是 `0.0` ⇒ 该键恒 PASS，`--force` 也越不过；② `missing_prediction_count > 0` 拒绝建档（退出码 2）；③ `_meta` 落来源 summary、题数、各指标分母、跳过原因，路径一律**仓库相对**（不留 `D:\Code\...` 机器痕迹）。
- **[实测·基线] 102 条真实录制（15:50:21 → 17:11:55，81.5 分钟；同输入复打分 17:29:25）**：机位为本机 LM Studio `qwen/qwen3-8b` + `text-embedding-bge-m3`，容器 Redis/Neo4j/MySQL 起，Milvus 与 kb_ingest 未起。单条耗时 min 11.0 / median 52.8 / p95 76.8 / max 148.5 / mean 48.0 秒。status：`ok` 55 / `degraded` 46 / `error` 1。路由与金标不符 **38 条**，其中 `graphrag-query → kb-query` **17 条（该路由全量误判）**、`additional → kb` 6、`general → additional` 5、`general → kb` 5、`text2sql → kb` 2、`text2sql → 报错` 1、`file → kb` 1、`additional → graphrag` 1。五指标基线：`route_accuracy=0.6275`（/102）· `citation_hit_rate_applicable=0.6268`（/46）· `citation_correctness_applicable=0.5833`（/34）· `must_contain_applicable=0.9098`（/61）· `reject_accuracy_applicable=1.0000`（/13）。**时间维度能核到哪一步**（审查追问后如实分层，并按 `.gitignore` 现测纠正过一次——原先写的是「仓库里可核的是两份 summary 的 `timestamp`」，而 `evals/results/` 整目录被 `.gitignore:387` 忽略，那句不成立）：**仓库里能核的只有预测档本身**——`evals/baseline_predictions.jsonl` 的 102 行、行序、102 个唯一 id 与逐条字段，加上「五指标可由 committed 预测 × committed 金标 × 打分代码逐键重算」（CI 每跑一次 `run_eval --offline` 都在重做这件事，这也正是门禁真正拦的东西）。**时间戳一律不在仓库里**：四份打分产物都以启动时刻命名、都只在本地——`20261025_155021`（录制那一轮就地打分）/ `20261025_160418`（**故意留档的残缺轮**：`missing_prediction_count=85`、17 条有预测、`route_accuracy=0.1176`、`citation_correctness_applicable=0.0`，「假绿」反例的输入就是它）/ `20261025_171228`（录制收尾后的 `--offline` 打分，`quality.json` 的 `_meta.source_summary` 指向它——那是**来源记号，不是可下载证据**）/ `20261025_172925`（同输入复打分，与 171228 逐键相同）；「15:50:21 → 17:11:55、81.5 分钟」这个窗口同样只存在于本机。**逐条耗时也核不到**：`summary` 里没有任何 `*_ms` 键，min/median/p95/max/mean 只在 gitignored 的录制进度日志 `.tmp/record_102.log` 里，本机按 `record_stats.py` 口径复算过（n=102、11.0/52.8/76.8/148.5/48.0 全对），所以这组秒数与录制窗口都是**本机留档的转录**，重录一次不会得到同一组数（模型非确定性），引用时必须连这句话一起说。
- **[取证·门禁] 双向，且把「假绿」复现了一遍**：同值五键逐个 `verdict=PASS` 且输出**不含** `NOT-ENFORCED`（真比对分支），注入 −5pp 五键逐个 `FAIL` / 退出码 1；CI `eval-offline` 的 shell skip 判定实测落在「两输入齐 ⇒ 真打分 + 逐指标门禁」。反证：拿一份 17/102 的残缺 summary 对空基线 `{}` 逐个建档 ⇒ 五键全绿 exit 0，而写进基线的是 `route_accuracy=0.1176` + 两个 `0.0`（从此恒 PASS）——这正是闸 ① 存在理由，全程打在 `quality.json` 的**副本**上，committed 文件未受影响。确定性核对（本轮复测时纠正过一次措辞）：`--offline` 同输入连跑两次（`171228` / `172925`）summary **逐键相同**；与录制那轮的就地打分（`155021`）相比只差 `best_at_k_requested` / `pass_k_requested` / `pass_k_tasks_with_full_runs` 三个 **pass@k 记账键**（离线重打分只见单 attempt ⇒ 102→0、1→None），而**五个门禁指标逐键相同** ⇒ CI 重算必然与基线同值，PASS 不是巧合；原先那句「与录制当时的打分逐键相同」按实测收窄，同源改到 `docs/项目实现手册.md` §20.5 与 `evals/README.md` §9。⚠️ **本批未改 CI 一行**（`git diff --numstat -- .github/workflows/ci.yml` 为空；`git status` 里那个 `M` 是行尾噪声）：`eval-offline` 的 skip 判定与五指标门禁循环都在 HEAD 里，门禁从「响亮跳过」翻成「真比对」靠的纯粹是**两个输入文件入库**（`baseline_predictions.jsonl` + `baselines/quality.json`）。两条降级路径的拦截力**不对等**（本轮把删除变异打在**真树**上量出来，不是推断；备份 → `mv` 走 → 5 份相关用例合跑 → `mv` 回 → sha256 复核，两次还原后逐位一致）：删 `quality.json` ⇒ **5 failed / 187 passed**（`test_p124` 3 条 + 文档路径守卫 `test_p41` 2 条，因 `_PATH_EXT` **含 `json`**，文档里被引用的路径没了就判腐烂）；删 `baseline_predictions.jsonl` ⇒ **192 passed / 0 failed**，机理在同一处：`tests/test_p41_doc_consistency.py` 的 `_PATH_EXT` **刻意不含 `jsonl`**，注释给的理由是「该文件待录制、未入库，检查它只会得到一条永远红的守卫」——本批之后这个前提已不成立，但白名单没动（动它属变更测试基线，纪律 5；连注释一起改是最低成本的一条，也还没改）。⚠️ 原先把红算给 `test_ci_gate_contract`（"用例直接打开该文件"）是**措辞错误，本轮更正**：该文件 20 条用例只用 `tmp_path` 造基线，从不打开仓库里那份，两种删除变异下它都全绿。现测（复用 p41 自己的 `_MD_PATH_RE` / `_DOCX_PATH_RE` 扫 73 份文档），抽出的 540 条路径里**不含**这个 jsonl；`test_ci_gate_contract.py:227` 也只断言 ci.yml **正文**提到这个文件名，不检查文件是否存在。CI 于是退回「响亮地不把关」：step summary 明写绿灯不代表指标达标，job 依旧绿而门禁是关着的。两条补法待点头：① 把 `jsonl` 放进 `_PATH_EXT`（会连带抽出 `legal_golden.jsonl` 等所有 jsonl 引用，得先核一遍）；② 加一条**内容**守卫「committed 预测必须存在、102 行、id 唯一、id 全在 golden 内」——②更值钱，因为「文件在、但被换成空档或短档」这条路连路径存在性都拦不住。**第三条关闭路径本轮由独立审查点名、现测确认无人钉**：ci.yml 的门禁循环只传 `--baseline-file/--metric/--current`，**不传** `--max-regression`，容差全在 `evals/quality_gate.py` 的 argparse 默认 `0.02`（2026-10-27 复核：原引裸行号 `quality_gate.py:102-105` 已腐烂——现文 `--max-regression` 与 `default=0.02` 在该文件 `:124`/`:126`，`:102-105` 是 direction 比较分支；腐烂源于更早批次的插行，非本条撰写时之过 ⇒ 按 §3.7 第 4 条同案改用文件级指针，本条历史结论不回改），而仓里两处引用该旗标的用例都是自己显式传值（`test_agent_phase20_events_evolution.py:252`、`test_ci_gate_contract.py:139`）⇒ 在 ci.yml 加一行 `--max-regression 0.99` 即可静默解除五个指标的门禁，本地与 CI 都不会红；最小补法（抽 ci.yml 的完整 argv 断言）同属新增测试基线，待点头。
- **[新增·产品接线] 录制机位逼出的两处默认关旋钮 + 一份补灌脚本（审查返工 S2 点名补登记）**：本批**不是**「只动 `evals/` 与文档」——`agentix/config/settings.py` 多了两个键，此前只在 `tests/test_p121_baseline_knobs.py` 的 docstring 留了一句「录制 evals 基线时补的两处接线旋钮」，CHANGELOG 与 README §11 都没记，于是 README 一度自称「产品代码零改动」。① **`TEXT2SQL_DATABASE_URL`**（默认 `""` ⇒ 逐位回退应用自身 `DATABASE_URL`，纪律 1）：出厂接线让 schema 拉取与 SQL 执行都读 `DATABASE_URL`，而法律库是 compose 单独的 `legal_db` + `MYSQL_*`，text2sql 因此只能看见会话/产物表，「民法典有多少条」这类问题**结构上答不对**（2026-10-25 录制实测）。目标库改走 `components/text2sql/target_db.py::text2sql_database_url()` **单源**，schema 拉取 / SQL 执行 / `EXPLAIN` 试跑 / schema 缓存键四处共用（`schema_retrieval/node.py`、`sql_execution/node.py`、`sql_validation/dry_run.py` 三处导入），并钉住「不再各自直读 `settings.DATABASE_URL`」这条静态边——防的就是日后冒出第五处。② **`KB_SCOPE_DESCRIPTION`**（默认 `""` ⇒ `general_handler.py:266` 透传 `scope_description=None`，子图沿用内置「测试数据集」文案，逐位等价）：语料换域而不声明范围时，KB 子图 guardrails 按旧域判越界，回「超出测试数据集知识库处理范围」且 **0 引用**（录制前探针在 L009/L012 两条上实测到）。③ 两键按纪律 4 **五处同步**齐备（现测：`settings.py` 字段、`selfcheck.py::DEFAULT_IGNORED_MISSING` 各一条带「合法省略」注释、`.env.example`（`TEXT2SQL_DATABASE_URL=` 赋值行 + `#KB_SCOPE_DESCRIPTION=` 注释示例）、`docs/architecture/配置管理.md` §5 两行、`tests/test_p41_doc_consistency.py::test_new_settings_are_documented` 手写清单两项）；守卫 `tests/test_p121_baseline_knobs.py` **12 条**（留空回退 / 显式配置四处同源 / 静态边 / 默认值本身）。④ **`scripts/data/backfill_legal_sql.py`**（本批新增）：把 `data/legal/legal_seed.json` 的 1,953 条补灌进 Text2SQL 目标库——图谱（Neo4j 1,953）与向量库（Milvus 4,865 块）都以它为真源，唯独 SQL 这条路停在 `data/insert_sample_data.sql` 的 4 部法律 / 10 个条目，金标 L075 的 `must_contain=1260` 在抽样库里**物理不存在** ⇒ `must_contain_applicable` 是被**环境**判负而不是被模型。口径三条：**只补不覆盖**（法律行按 `name` 命中即复用，抽样 SQL 里手工校对过的 `effective_date`/`upper_law_id`/`doc_number` 一律保留；对已灌全量的库是幂等空操作）、目标库走 ① 的单源（不会出现「补到 A 库、查 B 库」）、**默认 dry-run 且须显式 `--apply`**（纪律 5：写库要人点头）。守卫 `tests/test_p123_legal_sql_backfill.py` 共 **11 条**（离线 10 + 1 条挂 `-m integration` 真库核对民法典 1260 / 刑法 452 / 劳动合同法 98 / 宪法 143），含一条**案发过的真 bug** 回归（自然键→id 映射写成 `{id: name}` ⇒「已存在」永远判不出来 ⇒ `INSERT` 撞 `UNIQUE` 把整批事务打回，实测报错 `(1062, "Duplicate entry '全国人民代表大会' for key 'authorities.name'")`）。同窗落地的 `tests/test_p122_kg_csv_referential_integrity.py`（12 条，KG CSV 引用完整性）与本批叙述无关，只进 §11 的加法账。⚠️ 顺带修一处本批写坏的交叉引用：`配置管理.md` 的 `KB_SCOPE_DESCRIPTION` 行原写「（§24.3 T2 ⑯ 附带发现）」，而 PROJECT_SPEC §24.3 的 ⑯ 行是「`subprocess` 实现 + 能力探测 ⬜ 未开始」，与这句无关——改指 `evals/README.md` §5.2 与 `docs/guides/数据准备指南.md`（接线动机与实测留痕的实际登记处）。
- **[新增·守卫] `tests/test_p124_quality_baseline.py` 15 条（初版 13 条，审查返工 +2）**：零分母跳过、零值跳过（含 `--force` 越不过）、**落盘取整后才判 0.0**、五分母齐备、指标键脱钩响亮失败、CLI 溯源与仓库相对路径、拒绝残缺录制、`--dry-run` 不落盘、坏 summary 拒绝、真门禁闭环（同值 PASS 无 NOT-ENFORCED / −5pp FAIL）、committed 基线不再是空档（值下界收严为**开区间** `0.0 < value ≤ 1.0`）、committed 基线**覆盖** ci.yml `METRICS` 的每个键、README 发布数字与基线一致。其中 `test_committed_baseline_is_a_real_gate_not_an_empty_archive` 在基线生成前**故意红**（验收断言，不是可放宽的断言），生成后转绿。README 数字守卫按 p119 的教训只做「格式约定 + 值一致」，实测把 `route_accuracy=0.6275` 改成 `0.6300` 即红、还原即复绿。`tests/test_ci_gate_contract.py::test_empty_baseline_dict_is_also_loud` 的 docstring 同步更正（committed `quality.json` 不再是 `{}`，该用例守的是生成物退化成空档）。
- **[更正·审查返工 S1] 「删一个基线键」= 门禁的静默降级形态**：独立审查复现——`quality_gate.py` 对「基线里没有这个键」的处理是 NOT-ENFORCED + **把本轮值写回去建档** + 退出码 0（在**副本**上删 `citation_correctness_applicable` ⇒ `rc=0` 且该键被写回；committed 文件未动）。单看是"响亮地没把关"，配合删键就成了降级通道：那一轮不比对，下一轮起本轮值就是新地板，一次 10 pp 的退化可以被固化成基线，而 CI 依旧全绿。返工前的守卫只覆盖生成侧（拒绝产出 0.0/零分母键），`test_committed_baseline_…` 只遍历**已存在**的键（看不出少了谁）、值域下界还放着 `0.0 ≤`（手改的常绿档照样放行）。补两条钉子：① `test_committed_baseline_covers_every_ci_gate_metric`——committed 基线必须覆盖 ci.yml `METRICS` 的每个键，并要求生成器 `CI_GATE_METRICS` 与之**双向相等**（键清单复用 `test_ci_gate_contract._ci_gate_metrics` 从 ci.yml 现抽，不在测试里抄第二份清单，见 AGENTS.md §15.9「两处各写一份判据迟早只关一条」）；② 值域下界收严为开区间 `0.0 < value ≤ 1.0`。变异实测（两次都打在 `.tmp/review/` 的**副本**上，committed 文件全程未参与）：**删一键 ⇒ 1 条红**（`test_committed_baseline_covers_every_ci_gate_metric`，其余 14 绿）；**`route_accuracy` 改 `0.0` ⇒ 2 条红**（收严后的开区间下界那条 + 门禁闭环那条，后者因建档路径把副本里的值改写成本次 current）；两次还原后 15 条全绿、副本 sha256 与变异前一致，committed `evals/baselines/quality.json` 的 sha256 始终 `4ee3e0d0…` 未变（备份→变异→还原→校验留档在 `.tmp/review/`）。生成器侧同源补第三道闸：0.0 判定移到 `round(…, 4)` **之后**（`0.00004` 折成 `0.0` 同样是常绿灯；当前五个分母够不到该区间，属潜在形态，由 `test_generator_does_not_archive_a_value_that_rounds_to_zero` 钉住）。**没做的**：把「缺键 ⇒ 建档」改成硬失败——那是改首跑建档的既有语义，属口径变更（纪律 5），只在 `docs/项目实现手册.md` §20.5 的 ⚠️ 段与 `docs/runbooks/评测基线录制.md` 新增「第 8.5 步」里登记待定。⚠️ 防线位置本轮**纠正一处措辞**（原写「删键的提交在 CI 的 `test` job 就红，**走不到 `eval-offline` 的建档分支**」，独立审查指出不成立，现测 ci.yml 证实）：`eval-offline` 没有 `needs`，与 `test` **并行**跑，同一轮里它照样会走进建档分支。真正的防线是①workflow 顶层 `permissions: contents: read` 且 `eval-offline` 无 commit/push 步骤 ⇒ 建档只写在 runner 的 workspace 副本、随 job 销毁，回不到仓库；②`test` job 红 ⇒ `ci-gate`（`needs` 全部 job）不通过 ⇒ 带删键的提交合不进去。**「runner 里写了」不等于「基线被降级」，防线也不是 job 顺序。**
- **[同步] 文档面**：`README.md`（§2/§3 门禁行与 ⚠️ 块改成「已武装 + 覆盖面边界」、§8 轨迹回归注释按实测改写并分清**打分基线 ≠ 轨迹基线**、§11 测试真源数 + 一条对账缺项登记、§16 三条限制替换「门禁尚未武装」）、`evals/README.md`（§1 目录行的生成器规则补「值为 0.0 也不建档」、§5.2 录制档、新增 §5.5「基线是生成物不是抄本」、新增 §5.6 机位限定与实测路由混淆表、§9 门禁态从「未生效」改为「已武装 + 拦口径不拦效果」）、`docs/项目实现手册.md`（§20.5 三态现状与「这条门禁到底拦什么」、§20 构成行 `quality.json`「当前 `{}`」→ 已生成、术语表 NOT-ENFORCED 条目、已知限制表 1–4 行重写）、`docs/runbooks/评测基线录制.md`（八步全量重写 + 执行留痕表实测数）、`docs/guides/数据准备指南.md`（宿主机三条检索路径灌库三步与核对口径）、`docs/architecture/配置管理.md`（`TEXT2SQL_DATABASE_URL` 补「建表 ≠ 有数据，须 `backfill_legal_sql.py --apply`」）；清掉三处**别的地方**还在说「没录」的旧口径——`docs/design/性能与优化路线图.md` §5 第 2 条（「大声跳过、绿灯只代表没跑」→ 现在是真门禁，并写明它拦的是口径不是效果）、`docs/源码研读笔记.md` 对照表第 6 行「金标与 live 基线尚未录制」→ 已就位 + 残余改列（轨迹基线 / 消融 / 跨环境）、`docs/migration/Phase15-20_深化规划.md`（评估行现状 + 两条 checklist 打勾 + 2026-10-25 补记「拒绝伪造」纪律未变）；`docs/guides/面试十问.md` 的三个结构量按文档自带的命令现测重录（测试文件 210→**214**、`def test_` 4,130→**4,178**、"逐位等价"扫描 34 文件/69 处→**35 文件/72 处**，parametrize 51 文件与 14 文件 `byte_identical` 未变）。`.gitignore` 加 `evals/results/` 与 `evals/ablations/results/`（运行期打分产物，入库的是预测文件与生成的基线）。**审查返工后再动四处**：`evals/README.md` §5.5 补「落盘值才判 0.0」+ 一条 ⚠️ 说明「生成器拦不住已入库的键被人删」、`docs/runbooks/评测基线录制.md` 新增**第 8.5 步**（缺键降级形态 + 只在副本上复现的步骤 + 两条钉子 + 两次变异结果）、`docs/项目实现手册.md` §20.5 加 ⚠️ 段（同口径，并写明「建档改硬失败」属待定的口径变更）、`README.md` §11 当前口径数字改到 **4846**（+49，p124 15 + p121 12 + p122 12 + p123 10）与返工后全量实测数。
- **[如实记录·未修] 录制暴露的产品缺陷**：L080 在 Text2SQL live 路径抛 `KeyError: 'code'` ⇒ `run_eval` 记为 `status=error`、按失败计入基线，**未**为数字好看剔除；本轮未修（属行为变更，需单独口径）。图谱路径本轮**未被度量**（17/17 graphrag 金标误判），故基线不能读成「图谱检索质量」。
- **[验证]**：`test_p124` + `test_ci_gate_contract` 合跑 **35 passed**（返工后 15 + 20）；文档面改动后把六份相关守卫合跑（`test_p41_doc_consistency` / `test_p124` / `test_p119` / `test_p120` / `test_ci_gate_contract` / `test_p66_arch_guards`）⇒ **117 passed**（本轮二次复跑仍是 117：文档措辞改动没有移动任何计数），README 数字守卫另按变异验过（`route_accuracy=0.6275` 改成 `0.6300` 即红、还原复绿）；**删除变异（第 2 轮复验后补做，打在真树上）**——备份到 `.tmp/hold/backup/` → `mv` 走 → 5 份相关用例合跑 → `mv` 回 → `sha256sum` 复核，结果 = 对照 192 全绿 / 删 `quality.json` **5 红 187 绿**（p124 3 条 + p41 2 条）/ 删 `baseline_predictions.jsonl` **0 红**，两次还原后 sha 逐位一致（`4ee3e0d0…` / `ab3cbb52…`）。⚠️ 补做过程自己也踩到一条既有纪律：runbook 的表初稿写「192 passed」，被 `test_p41::test_only_readme_declares_the_suite_size` 判成「操作性文档复述测试总数」而红——该守卫用 `\d{3,}\s*passed` 粗匹配，**分不清「变异计数」与「套件总数」**；正解不是加豁免而是改表述（`docs/runbooks/*.md` 用「红/绿」，原始 pytest 输出留在 `.tmp/`，正文写明可按该步在新克隆复跑）。`ruff check agentix tests scripts evals kb_ingest` 全绿；离线守卫可复现的失败形态逐条真跑（缺预测文件 → `--offline` 退出码 2 并打印引导；残缺 summary → 生成器退出码 2；`--dry-run` → 不落盘；照 `-b evals/baselines` 跑轨迹回归 → 实测仍报「`quality.json` 缺少 'steps' 字段」，README §8 那段注释据此改写而非沿用旧描述）。****全量离线套件** `pytest -m "not integration"` → 返工前 **4844**（433.11s）、返工后 **4846 passed / 14 skipped / 3 deselected / 0 failed** 三次复跑计数一致（421.46s / 409.55s / **406.50s** 末次为准，Python 3.12.13，未开并行；这 **+2 全部来自 `test_p124` 的两条防降级钉子**（13 → 15），其余键计数零变化），实测数登记于 README §11（唯一真源）；+49 逐项对账（p124 **15** + p121 12 + p122 12 + p123 离线 10 = 49）与该三个未登记守卫文件一并写进 §11。⚠️ **没验到的**：CI `eval-offline` 那条「真打分 + 逐指标门禁」分支将在 **Python 3.10** 上首次真跑（本机只有 3.12.13）——模块可导入性由 `eval-smoke`（`test_p21_eval_harness.py` 以 importlib 加载 `run_eval.py`）在 3.10 上覆盖，门禁循环本身用同一棵树在本地等价复跑过；下一次 push 的 CI 日志才是那条分支的真跑证据。
- **[审查·第 2 轮复验结论与一般项登记]**：独立审查第 2 轮对 S1/S2 的复验结论为**通过**（无阻塞、无严重；三条钉子实测全部打在 `.tmp/r2/` 副本上，committed `quality.json` sha256 前后 `4ee3e0d0…` 未变；生成器变异 `0.00004` 在旧写法下会落盘 `0.0`、现行代码拒绝）。其 4 条**一般项**逐条处置：① `eval-offline` 与 `test` **并行**、"走不到建档分支"不成立 ⇒ 措辞已在 `CHANGELOG`/runbook 更正（见上一轮 [更正] 与本条 [验证] 之后的措辞），真防线是 `contents: read` 不落盘 + `ci-gate` 挡合并；② `test_ci_gate_contract` 从不打开 committed 基线 ⇒ 已更正四处文档并把删除变异改为**真树实测**（删 `quality.json` 5 红 / 删预测文件 0 红，还原后 sha 逐位一致）；③ ci.yml 门禁循环不传 `--max-regression`、默认 0.02 无人钉 ⇒ **现测确认成立**，属新增测试基线（纪律 5），与两条 jsonl 补法并列登记待定；④ `tests/test_p123_legal_sql_backfill.py:355-369` 的集成用例自行读 `settings.TEXT2SQL_DATABASE_URL` 判空跳过、不调 `text2sql_database_url()`——本轮核对后判定**这是刻意的语义差**而非重复判据：调函数会在留空时回退到应用库（那里没有 `laws`/`articles`），把"该跳过"变成"连接后报错"；风险确实存在（两处规则可能漂移），已在 PROJECT_SPEC 登记；同文件另一条一般项「`--apply` 默认 dry-run 只由代码与 prose 保证、无 CLI 用例」成立，补测属新增测试基线，与 ③ 一并待点头。

### 优化批次 P1-D · 金标扩样 23→50（2026-10-25）

- **[新增·数据] `evals/legal_golden_ext.jsonl`（27 题，L103–L129）**：基线 `evals/legal_golden.jsonl` **一个字节未动**。新题按「金标条文确实在语料里」取样——覆盖《民法典》12 条（147/150/157/509/533/563/590/680/725/1032/1064/1207）、《劳动合同法》5 条（10/14/37/40/47）、《刑法》5 条（20/133/264/266/271）、《宪法》3 条（37/39/40）、司法解释 2 条（婚姻家庭编解释（一）第5条彩礼、总则编解释第3条滥用权利）。每条都用脚本对原文核过 `# 《X》第N条` 标题存在、`must_contain` 关键词逐字出现在该条正文（27/27 通过）——扩样集**没有新增覆盖缺口**，这一点由守卫单独断言，不靠散文。
- **[新增·工具] `eval --golden PATH [PATH ...]`（默认仍是基线单文件）**：`scripts/demo/local_kb_demo.py` 的 `eval` 支持按序拼接多份 golden（默认路径、summary 键集与逐题输出均不变；23 题默认跑复现 Recall@5 0.8696 / MRR 0.7029）。`--limit` 只在**真截断**时向 stderr 提示（此前"合格题数恰好等于 limit"也会误报，已改成对比截断前后计数）；显式给 `--golden` 时 summary 多一个 `golden` 来源键。
- **[实测·检索层] 50 题合并样本：Recall@5 0.9400（47/50）、严格 43/50 = 0.8600、MRR 0.7933、top-1 35/50**：`--retrieve-only --skip-route --no-prewarm --limit 50`、单轮。silver 漏召回仍是基线的 L049/L055/L056（三条都是覆盖缺口），citation-only 仍是 L053/L054/L060/L062；严格口径天花板 45/50 = 0.9000（5 条缺口金标全部来自基线）。**扩样 27 题 27/27（21 题 top-1、6 题 2–5 位），silver 与严格同值**——新增题不引入漏召回、不引入缺口。`--repeat 3` 复跑 recall/MRR 逐轮相同（0.94 / 0.7933），检索 P50 50.8/51.8/51.4ms、P95 126.1/122.0/117.2ms（池化 51.6/122.0）；同日三次单轮分别是 115.4/226.8ms（冷态离群）、47.8/116.5ms、49.5/119.2ms ⇒ **延迟不进口径⑦ 的发布表**（单轮分位数不配当基线，延迟仍看口径③/⑥）。
- **[新增·生成物] 口径⑦ 的数字由生成器产出**：`scripts/demo/report_eval.py` 新增 `--retrieval-only` / `--ext-golden` / `--generated-for` 三个开关；默认路径与 `published_strings` 的 e2e 分支不变（重生成基线 summary 的 `metrics` / `published` / `cross_stack` 与旧文件**逐键相同**，仅多 `kind` / `golden` / `ext_golden` 三个自描述键）。进仓产物 `docs/evals/golden50-summary.json`（含两份 golden 的显式来源、语料双 sha256、`ext_n=27`）；守卫 `tests/test_p120_golden50_retrieval_summary.py` 5 条——汇总能由 `metrics` 回算、发布串锚定在 README 口径⑦ 小节内（不是全文任意命中）、扩样组零漏召回零新增缺口、两文件题号不重叠且合计 50。
- **[同步] 文档面**：`README.md` §15.2 新增口径⑦（表 + 三条读法：silver 0.9400 与严格天花板 0.9000 不是一回事；Recall 抬升首先反映"新题都挑可答题"的有偏取样，不能说检索变强；延迟不发布但已测）；口径⑦ 刻意**不复述**基线小数（0.8696 / 0.7029 / 16-23），避免把 p119 的 §15.2 小节锚定变成"别处同名数字兜住"的假守卫。§11 测试真源更新为 4797；`docs/guides/面试十问.md` 结构量 209→**210** 文件、4,125→**4,130** 个 `def test_`。
- **[验证]**：`test_p119` + `test_p120` 合跑 **11 passed**；`report_eval --check` 对两份 summary 均退出 0；`.venv` 的 `ruff check agentix tests scripts evals kb_ingest` 全绿；**全量离线套件** `pytest -m "not integration"` → **4797 passed / 14 skipped / 2 deselected / 0 failed**（445.68s，Python 3.12.13，未开并行），实测数登记于 README §11（唯一真源）。逐题明细留档 `data/demo/eval_ext50_retrieve_only.json`（gitignore）与 `.tmp/p1d_ext50_repeat3.json`。

### 优化批次 P1 · 小样本重复稳定性实测（2026-10-25）

- **[新增] `eval --repeat R`（默认 1，纯工具）**：`scripts/demo/local_kb_demo.py` 的 `eval` 子命令新增重复轮次。R=1 时 summary **键集与历史完全一致**（不新增任何键、逐题打印照旧；已与 `data/demo/eval_retrieve_only_cacheOFF.json` 比对 key set，Recall/MRR 也逐位相同，变的只是重跑本就不同的毫秒数）；R>1 时新增三块**只增不改**的观测——每轮的 `latency_ms`（`repeats`）、各分位数的跨轮极差（`repeat_spread_ms`）、把 R 轮并成一池重算的分位数（`pooled_latency_ms`），并按轮打一行汇总（逐题噪音太大时改成每轮一行）。预留在第 1 轮前做一次、不在每轮重复。用于回答 §15.2 那句"n=23 的 P95 连自己这一轮都稳不住"。产物留档 `data/demo/`（gitignore）与 `.tmp/p1_*.json`。
- **[实测] 排序结论逐轮一致、分位数不逐轮一致**：三种口径各真跑——检索层 ×5（`--retrieve-only --skip-route`）、检索+生成 ×3（`--skip-route`）、全链路路由 ×2（默认）。**质量指标跨轮完全相同**（Recall@5 恒 0.8696、MRR 恒 0.7029——检索确定性，routed 轮 `route` 也 23/23 一致），但分位数会动：检索 P95 119.3–144.4ms（≈±20%）、生成 P50 8818–9537ms、e2e P95 14454–15026ms（skip-route）与 **31740.3 / 35666.5ms**（routed）。两条结论：① §15.2 录的 e2e P95 **31543.1ms 落在重复区间内、31740ms 那轮几乎逐位复现**（不是手气好，但周围有 ≈±12% 抖动）；② **路由段自身在漂**——同样 23 题，路由残差 P50 两轮 16174 / 18019ms vs 上表 13462ms（差 2–4s），**路由既是最大段也是最不稳的一段**。数字全部来自实测产物，写进 README §15.2 口径⑥。
- **[同步] 文档面**：`README.md` §15.2 新增口径⑥（重复稳定性 + 两条结论）、口径③ 尾部"连自己这一轮都稳不住"补指向。

### 优化批次 P0 · 检索缓存开关 / 路由兜底计数 / 发布数字生成物（2026-10-25）

- **[新增·P0-1] `RETRIEVAL_RETRIEVER_CACHE_ENABLED`（默认关）：检索器进程内缓存**：`RetrievalPortAdapter.search()` 此前每次检索都走 `build_retriever → create_vector_store → FAISSVectorStore.load → create_embeddings`（每次新建一个 OpenAI 兼容客户端）。新增 `agentix/retrieval/infra/vectorstore/retriever_cache.py`（`get_or_build_retriever(index_path) → (retriever, vectorstore)`）：flag 关 ⇒ 每次直调 `build_retriever`，与接线前**逐位等价**（同一对象图、同一异常）；flag 开 ⇒ 按签名 `backend | abspath(index_path) | index.faiss(mtime_ns,size) | embedding_model | embedding_base_url | k` 复用，索引换代（文件被重写）自动失效（**没有** TTL——有效性只由磁盘事实决定）；`index.faiss` stat 失败（含不存在）⇒ 不走缓存直接构造，让 `FileNotFoundError` 的抛出时机与基线一致；构造持锁（single-flight），并发首建只构造一次、其余等待后命中。两个调用点（`agentix/retrieval/usecases/retrieval_port_adapter.py` 检索主路径、`chat_service.py` 预检路径）改走该包装；`index_completion_verifier.py` **刻意仍直调** `build_retriever`——它的职责是从磁盘**独立地**重载刚落盘产物来核对，拿进程内缓存对象等于自证、校验就失效了。口径与接线注释见模块 docstring 六条；缓存**进程私有**（内存 ≈ 索引大小 × worker 数）。
- **[新增·P0-2] `router_fallback_total{reason}`：静默兜底变成可归因计数**：`analyze_and_route_query` 的兜底原先只有一行日志、没有任何计数，`record_degraded` / `gateway_fallback_total` 都**不覆盖**这条路径，"模型判的 kb-query"与"兜底的 kb-query"在 `/metrics` 上同形。新增计数器与 `record_router_fallback(reason)`（`agentix/observability/metrics.py`），`reason` 有界 `exception|empty|invalid_type`（表外归 `unknown`，已在 `ALLOWED_METRIC_LABELS` 内，AGENTS.md 纪律 3）。**纯新增、无行为变更**（路由返回值一字未改）：`except` 分支记 `exception`，非法类型分支记 `invalid_type`/`empty`；计数走 fail-open（指标子系统抛错只 `logger.debug`，绝不打断路由，纪律 2）。**同时记录一条仍未修的空洞**：结构化输出返回非 `Router` 实例且类型非法时，`router_cls.model_validate` 在 `try` 之外（`agentix/application/agents/router.py:261`）⇒ `ValidationError` 直接冒出节点，既不兜底也不计数；修它属行为变更，本批只用 characterization 用例把现状钉住、不改产品行为。另两条防御分支（`invalid_type`/`empty`）按构造不可达——`Router.type` 是 `Literal`，构造期即校验（所以它们的计数在本批常态读 0）。
- **[新增·P0-3] README §15.2 的发布数字改成生成物**：新增 `scripts/demo/report_eval.py`（生成器兼校验器）——从逐题明细**独立重算**全部指标（不读 eval 自带的 `summary`，两者不一致即 `SystemExit`），按语料文本推导覆盖缺口，产出 12 条 `label/string` 发布串与一张 markdown 表；`--write-summary` 写进仓的汇总 JSON，`--check README.md` 校验目标文档与汇总一致（退出码即门禁）。进仓产物 `docs/evals/minimal-slice-summary.json`（含 `source_eval`/语料双 sha256 与环境指纹），守卫 `tests/test_p119_readme_metrics_summary.py`：断言汇总能由生成器从自身 `metrics` 回算、语料指纹与 README 的 `1ca58564b1f3a58c` 前缀一致、且发布串出现在 README **§15.2 小节内**（不是全文任意命中）。**首版全文 `in` 检查被证明是假守卫**：改一处数字会被别处的同名数字兜住；改成小节锚定后把 §15.2 里 5 处数字一起改错 ⇒ 用例才变红，还原后复绿（README 字节复原为 sha `1f14e00bec9a7a37`）。
- **[新增·量测] 检索缓存开关的 A/B 实测（检索结果 0 处差异）**：同日、本机 `.venv`、`eval --retrieve-only --skip-route --no-prewarm --limit 23`（冷进程、单轮、只测检索层）：逐题检索集合与位次 **0 处差异**，Recall@5 / MRR 逐位相同（0.8696 / 0.7029）；检索 **P50 93.9→4.4ms（21.3×）、P95 341.5→5.5ms**，均值 229.4→38.5ms（开关侧均值被首题冷启 789.9ms 拉高，其后中位 4.3ms）。⇒ 省掉的正是"每次检索重建客户端 + 反序列化索引"这段；**检索结果一位没变**。⚠️ 本机单轮小样本，不外推到其他语料/栈；也不是端到端结论（端到端仍以路由为最大段）。数字口径写进 README §15.2 口径③。
- **[修复] `test_p97` 时钟断言（经维护者同意，只改用例）**：`tests/test_p97_runtime_control.py::TestAdaptiveToolTimeout::test_scope_deadline_helpers` 在本机**确定性**失败，断言 `assert 50.00000004656613 <= 50`；根因是 `RequestScope.set_deadline_seconds`（`agentix/application/services/request_scope.py:47-56`）把 `time.monotonic() + 0.05` 的浮点上舍入与 Windows 时钟粒度叠加（`set()` 与 `remaining_ms()` 可能读到同一个 monotonic 值 ⇒ 差值可比 50ms 大 ~1e-8ms 量级），**与本批改动无关**。按 AGENTS.md 纪律 5 先征得同意，再**只改用例**：给该断言的上界加 `1e-3`（ms）浮点容差、保留"正值且未超预算"原意，产品代码零改动；单文件连跑 5 次 42 passed 稳定。
- **[验证]**：本批新增 3 个守卫文件共 **27 条**全绿（`test_p117` 12 / `test_p118` 9 / `test_p119` 6）；`test_p118` 与 `tests/test_p100_genai_observability.py` 合跑 26 passed；`report_eval.py` 的生成/校验两条路径各真跑一次（`--check README.md` 退出 0；重生成 summary 与进仓文件**字节相同**）；`ruff check agentix tests scripts evals kb_ingest` 全绿（顺手修掉生成器与守卫里两处 `E741` 模糊变量名 `l`）。**全量离线套件** `pytest -m "not integration"` → **4792 passed / 14 skipped / 2 deselected / 0 failed**（398.58s，Python 3.12.13，未开并行），实测数登记于 README §11（唯一真源）；这条是 p97 用例修好后的复跑（修前一次为 4791 passed / 1 failed，即上一条）。
- **[同步] 文档面**：`README.md` §15.2 口径③（检索缓存开关 + A/B 实测）、§16 两条已知限制（客户端重建条目加"已有开关"、路由兜底条目加"已有计数器 + 仍留 `model_validate` 空洞"）、§11 测试真源补本轮全量口径；`docs/guides/面试十问.md`「坑三」补"该静默半边已于 2026-10-25 由 `router_fallback_total{reason}` 关掉"并重测三处结构量（测试文件 206→**209**、`def test_` 4,098→**4,125**、"逐位等价"扫描 33 文件 / 67 次 →**34 文件 / 69 次**）；`.env.example` 补 `RETRIEVAL_RETRIEVER_CACHE_ENABLED=false` 与 `RETRIEVAL_RETRIEVER_CACHE_MAX_ENTRIES=8`（沿检索域惯例只进 `.env.example` 注释示例块，不进 `配置管理.md` / p41 手写清单）。

### 最小可演示切片 · 第一批端到端实测数字（2026-10-25）

- **[新增] 三条命令跑通一条带引用的链路**：`scripts/demo/local_kb_demo.py`（`seed` / `ask` / `eval`）把「L1 路由 → L2 kb 检索 → 带引用回答」压进同一进程——元数据进 SQLite 文件、向量进 FAISS 本地目录、嵌入与生成全走本机 LM Studio（`text-embedding-bge-m3` + `qwen/qwen3-8b`），不需要 Neo4j / Milvus / DeepSeek / Docker。**纯新增**：不改生产代码、不新增 Settings 字段（只经 `os.environ.setdefault` 提供本进程默认），产物只落 `data/demo/`（本轮已加进 `.gitignore`）与 `--json-out`。脚本自带三道守卫：语料条目数必须等于 4,865（标题行判据是行首恰好 `# `，宽正则会把手里的 3 行裸 `#` 当条目，条目数变 4,868）、FAISS 重建前调生产 `_prepare_index_dir`、`index.doc_count == chunks`（历史事故：重复灌库把 doc_count 堆到 9,773，重复命中稀释 Recall@5）。
- **[新增] 本地适配按「能力探测」而不是版本号**：`_enable_local_llm_compatibility()` 先探 SDK 有没有 `chat.completions.with_raw_response.parse`、再探那套库能否用给定 kwarg **构造出**结构化输出链（只构造不 invoke，不发请求），于是两套实测过的栈各拿各的可用形态——`.venv`（openai 1.109.1 / langchain-openai 0.2.14）⇒ `json_schema`；Anaconda（openai 1.47.0 / langchain-openai 1.1.6）⇒ `function_calling + tool_choice="auto"`。选中的组合与环境指纹一并打印，不留暗改；生产默认不动（云端 tool-calling 走出厂形态）。
- **[新增] README §15.2：第一批端到端小样本表**（n=23、单轮串行、`.venv`）：Recall@5 **0.8696**（20/23；换成「金标文档本身进 top-5」的严格口径是 **0.6957**=16/23，而这份语料对严格口径的天花板是 **0.7826**=18/23）、MRR **0.7029**、检索 P50/P95 **100.3 / 284.5ms**、生成 **7108.2 / 13739.9ms**、端到端 **20546.8 / 31543.1ms**（路由残差 13462.3 / 17682.1ms，**占端到端均值 64%**）、单请求 token 均值 prompt 718.7 / completion 283.3 / 合计 1002.0（只计生成那次调用）。五条口径逐条写明：silver 匹配器的可量化后果（4 题靠"别的条文引用了它"得分，其中 2 题金标条目在库里只是排不进 top-5，属真·排序失败）、最近邻秩分位数（P95 排除最慢一条）、延迟对 Python 栈敏感而质量不敏感、路由才是最大一段、chunk 含标题行且未走生产切片器。§15 标题去掉「（待实测）」，⚠️ 说明改为"三条实测线索各有边界、互不可替"。README 顶部同步加「最小 Demo：3 条命令」段，把 GraphRAG / Text2SQL / MCP / 多智能体 / Milvus / 全栈 compose 降级为一行可选能力清单。
- **[发现·未修] 两个可观测与性能形状问题只记录**：① `RetrievalPortAdapter.search()` 每次搜索都重建 embedding 客户端（`build_retriever → create_vector_store → FAISSVectorStore.load → create_embeddings`，无缓存）——`.venv` 上构造 16–260ms，另一套栈 2053–4025ms（cProfile 指向 `_ssl._SSLContext.load_verify_locations`，而 LM Studio 是明文 HTTP，这笔 CA 开销用不上），同一份 23 题在两套栈上**逐题位次与检索集合 0 处差异**但检索 P50 差 23 倍；② 路由的 `except`（`agentix/application/agents/router.py:240-244`）把任何结构化输出失败吞成 `fallback_router`，唯一可辨信号是 `logic` 字符串，`record_degraded` / `gateway_fallback_total` 都不覆盖这条路径，"模型判的 kb-query"与"兜底的 kb-query"在 `/metrics` 上同形。**两者均属生产代码行为变更，按 AGENTS.md 纪律 5 未动**（要改需配套 flag + 守卫并先征得维护者同意）；`evals/legal_golden.jsonl` 里 5 条落在语料覆盖之外的金标也未改动，只在 §16 记为口径事实。
- **[验证]**：全部数字在仓库登记的 `.venv`（`python 3.12.13`）上重测取得——此前一轮误用系统 Anaconda 3.13 跑，`tests/test_dependency_consistency.py` 的"未登记解释器版本/平台 3.13"采集期报错把这件事抓了出来，随即重灌 `legal-demo`、重跑 23 题与四通道对照。守卫：`.venv/Scripts/python.exe -m pytest tests/test_p41_doc_consistency.py tests/test_code_hygiene.py tests/test_dependency_consistency.py -q` ⇒ **103 passed**（`test_p41` 期间抓过两次自伤：面试提纲复述套件规模触发"总数单源在 README §11"守卫、新增 `##` 标题未进目录——都改文档而不放宽守卫）。`ruff check scripts/demo/local_kb_demo.py` 全绿。四通道对照（BM25 0.6957/0.5486、Dense 0.8696/0.7029、RRF 0.8696/0.7065、+Rerank 0.8696/0.7065）在两套栈各跑一遍、逐位相同；`+Rerank` 与 `RRF` 相同是本机无 Cross-Encoder（日志 `rerank=closed`），**不能读成"精排无效"**。跨栈对照产物留档 `data/demo/eval_e2e_anaconda_py313_openai147.json` 与 `data/demo/hybrid_anaconda_py313.json`。

### 鉴权面收口 · G7 卫生项（2026-10-23）

- **[安全·G7] 公开路径清单收成单一事实源，`/api` 不再匿名**：`APIKeyMiddleware.EXEMPT_PATHS` 与 `GlobalRateLimitMiddleware._EXEMPT_PATHS` 原先各抄一份、注释只说"同口径"——加一个键只改一处，同一个 URL 就会在两套闸门上得到相反答案。现在清单只存在于 `agentix/interfaces/http/public_paths.py::PUBLIC_PATHS`（`frozenset`，外加 `PUBLIC_PREFIXES=("/docs","/redoc")`），两个中间件 import 同一对象，限流侧的两份私有副本已删除。同时把 `/api` 移出公开面：它是根信息端点（应用名/版本号），不是探针——探针是 `/health`（compose healthcheck 与 Vite proxy 都用它）。行为变化只在 `REQUIRE_API_KEY=true` 时可见：`/api` 由 200 变 **401**，`/health`、`/metrics`、`/docs`、`/openapi.json` 仍匿名可达，`/api/v1/*` 照旧 401；flag 关时 `/api` 仍 200（逐位等价）。**这不是漏洞修复**：匹配一直是精确成员判断，历史上从未构成前缀绕过（G7 原文已把这句判据保留为案发记录）；错的是一份清单写两遍。
- **[校验] 35 条 + 7 处变异**：`tests/test_p116_public_paths_single_source.py` 覆盖——精确集合双向（往公开面加东西必须先改测试）、`/api` 缺席、集合不可变、两个中间件与 `PUBLIC_PATHS` 的 **identity**、`__call__` 的源码形状（必须用 `PUBLIC_PATHS` / `PUBLIC_PREFIXES` 判断，不得另抄字面量）、flag-on 的 401/200 面、`/docs` 前缀穿透、flag-off 的 `/api` 200、限流侧对同一批路径的豁免与对 `/api`、`/api/v1/healthz`、`/metrics/extra` 的不豁免。**首版 7 处变异只抓到 5 处**：漏网的两处是「`__call__` 里另抄一份字面量」（类属性身份仍在、行为今天一样）与「精确匹配改成子串匹配」（采样路径里没有"只包含公开路径名"的串）——前者补源码形状钉子、后者补 `/api/v1/healthz` 与 `/metrics/extra` 两条路径后才归位；**身份断言证明不了判断点用的是哪个名字**。受影响面 `test_p37_http_endpoints` / `test_unit_security` / `test_p47_wiring_gaps` / `test_p43_path_safety` / `test_observability_metrics` / `test_v1_endpoint_auth` / `test_p41_doc_consistency` / `test_p115_staging_matrix_generated` 合跑 **171 passed**，`ruff check agentix tests scripts kb_ingest` 全绿。
- **[同步] 文档面**：`docs/项目实现手册.md` §24.2 G7 的修复状态改为「默认态未修复 + 该卫生项已收口」并补收口形态/两条漏网变异/历史判据保留；§24.4 P2 行翻「已落地」；§1.5 的装配表、§13.1 与 §16.3 的中间件表、§21.5 的启动/健康检查段落统一改指 `public_paths.py::PUBLIC_PATHS`，并写明 `/api` 不在公开面。`SECURITY.md` 与 `README.md` 无需改动：两处讲的是「生产必须开鉴权」这条默认态建议，本批次没有改变它，也没有新增部署开关。

### 沙箱与执行边界加固 · §24.3 T0（2026-10-23）

- **[新增] 两章审计文档**：`docs/项目实现手册.md` 新增 §23「技术选型依据与被拒方案」（含 23.1 证据强度分级 留档/结构/补记，逐条给出「为什么是这个栈、被拒方案差在哪」）与 §24「执行隔离与沙箱现状」（七层治理「能挡什么/挡不住什么」矩阵 + G1–G12 实测缺口 + T0–T3 分档路线）。§0.3 章节地图同步登记两章。
- **[安全·G1] `TOOL_SYNC_OFFLOAD_ENABLED`（默认关）**：`ToolExecutor._invoke` 之前对**非协程** handler 直接 `handler(args)`，同步调用占住事件循环，外层 `asyncio.wait_for` 无从取消——实测声明 `timeout_ms=100` 的阻塞 handler 实跑 **1201ms 且 `success=True`**，超时治理完全失效。flag-on 改走 `asyncio.to_thread`，同一 handler 实测 **111ms 被截断、`success=False`**；异步 handler 两种 flag 下均在 112ms 正确截断。
- **[安全·G2] `MCP_SUBPROCESS_ENV_SCRUB`（默认关）+ `MCP_SUBPROCESS_ENV_EXTRA_KEYS`**：`create_subprocess_exec` 原本不传 `env=`，MCP server（可以是任意第三方命令）继承主进程**全部**环境变量，含 `LLM_API_KEY` / `SERPAPI_KEY` / `DATABASE_URL`。打开后按 `OS_ENV_ALLOWLIST` 白名单裁剪（实测 14 个键，密钥类 0 个），并在裁掉 `PYTHONPATH` 时自动补回仓库根；真起子进程实测 `tools/list` 与 `calculator 6*7=42` 仍正常。
- **[修复·G3] 内置计算器资源上限**：`_calc` 的白名单挡语法不挡资源，`9**9**9` 会算到内存耗尽。加 `MAX_EXPRESSION_CHARS=200` / `MAX_POW_EXPONENT=64` / 位数与有限性校验，`9**9**9`、`2**1000`、`1e309`、超长表达式全部**立即抛错**（<0.5s）。
- **[修复·G3b] 一元负号被吞**：`_safe_eval` 的 `UnaryOp` 分支只递归取 `operand`，`-5` 会算成 `5`、`-4+2` 算成 `6`。改为真正施加符号，并把 `UnaryOp` 也接进 `ev`，`-(2+3)` 现在正确得 `-5`。
- **[安全·G4] 搜索日志脱敏**：httpx 的异常文本原样带出请求 URL，而 SerpAPI 的密钥就在 query 里——一次 400 就把密钥写进日志。新增 `redact_secrets()`（query / `k: v` / `Bearer` 三形态 + 本进程 `SERPAPI_KEY` 原值兜底），并保留 `\b` 边界避免误伤 `monkey=1`。用本地 400 服务真跑错误分支实测：日志只剩 `api_key=***`。
- **[安全·G5] Text2SQL 单一词法**：新增 `agentix/domain/policies/sql_read_only.py`，`sql_execution/_is_read_only_query` 与 `sql_validation/validate_sql_security` 改用同一份判定（此前两闸各持一份黑名单，同一句 SQL 会一闸过一闸拦）。改为**先剥字符串字面量与注释、再按词边界匹配**：消掉 `WHERE note LIKE '%update %'`、`SELECT 'it''s'` 这类误杀；补上 `LOAD_FILE` / `INTO OUTFILE` / `SLEEP` / `BENCHMARK` / `FOR UPDATE` 这类原来一条都不命中的读文件与打时间模式。歧义动词只拦写用法（`REPLACE()` 放行、`REPLACE INTO` 拦截）。语法闸维持原有的窄口径（只认 `SELECT`/`WITH`），不跟执行闸一起放宽。
- **[安全·G9] 设置接口不再明文回显模型密钥**：`agentix/retrieval/api/routers/settings.py` 的 `DEFAULT_CONFIG` 在导入期把 `LLM_API_KEY` / `EMBEDDING_API_KEY` 原值收进来，`GET /settings/{kb_id}` 直接返回给**任意登录用户**（路由只挂 `get_current_user`，非 admin），`PUT` 还以 `body: dict` 无键白名单全量落库。现在密钥统一掩码（`***` + 末 4 位），`PUT` 只接受 `ALLOWED_CONFIG_KEYS`（18 键）并丢弃掩码值——防止前端把 `***` 原样写回覆盖真 key。前端 `RagConfig` 类型本就不含这两个键，无 UI 回归。
- **[修复·G11] `DB_ECHO`（默认空 = 旧行为）**：`_DEFAULT_LOG_ECHO = bool(settings.DEBUG)` 且 `DEBUG` 出厂 `True`，实测 `engine.echo is True`——完整 SQL **连同参数**经标准库 logging 落盘，绕过 loguru 单一出口。新增显式覆盖位，`DB_ECHO=false` 实测 `engine.echo=False`，不设则逐位等价。
- **[修复·G6] 生产身份不再由 `DEBUG` 反推**：新增 `APP_ENV`（优先）与 `UVICORN_RELOAD`，`ENVIRONMENT` 解析顺序改为 `APP_ENV` > legacy `ENVIRONMENT` > `DEBUG` 推断。实测 `APP_ENV=production` 且 `DEBUG=True` 时 `validate_app_settings()` 正常报出限流/索引完整性两条 error（此前被 `DEBUG` 反推成 development 而整段静默）。启动另增一条 `SECURITY POSTURE` 告警，点名当前关闭的 7 个闸门 flag；显式声明 `APP_ENV` 或闸门全开则不打。compose 的 `--reload` 判据从 `DEBUG` 改为 `UVICORN_RELOAD`（默认值仍跟随 `DEBUG`，本地联调行为不变）。
- **[修复·G6 连带] staging 曾掉出硬失败面**：修 G6 时自己踩出来的回归——`docker/compose.override.staging.yml` 早已显式写了 `APP_ENV: staging`，而 `APP_ENV` 一生效，`ENVIRONMENT` 就不再被 `DEBUG=false` 反推成 `"production"`；若拒绝启动仍只判 `PRODUCTION`，**staging 的配置校验会静默降级成只打日志**。新增 `STRICT_VALIDATION`（`prod`/`production`/`staging`，与 `resolved_secret_key` 既有口径一致）并接到 `validate_app_settings` 与 `main._run_settings_validation` 两处；`APP_ENV` 留空时该集合与旧 `PRODUCTION` 完全等价，flag-off 逐位等价未破。实测 `APP_ENV=staging` 时 `_run_settings_validation()` 抛 `RuntimeError` 且消息含 `staging` 与 `RATE_LIMIT_ENABLED`，`APP_ENV=development` 时不抛。
- **[安全·G8] 容器边界**：`docker-compose.yml` 的 backend 补 `security_opt: no-new-privileges:true` 与 `deploy.resources.limits`（`BACKEND_CPUS=4.0` / `BACKEND_MEMORY=8g` / `BACKEND_PIDS_LIMIT=4096`，此前整个数据面只有 `retrieval-worker` 带限额）。`docker compose config` 实测通过并解析出预期值（首版把 `pids_limit` 写在顶层，与 `deploy.resources.limits.pids` 冲突，已改到 `limits.pids`）。
- **[勘误] 撤回一条错误结论**：§24.2 初稿的 G9「OTel 声明为硬依赖但本机未安装 ⇒ OTLP 分支零覆盖」是**用错解释器**（Anaconda base 而非 `.venv`）得出的。用 `.venv` 复测：`opentelemetry-*` 全部 1.44.0、`OTEL_AVAILABLE is True`、`tests/test_observability_metrics.py::test_init_tracing_produces_real_tracer` 真断言了非 noop tracer。结论已撤回并在 §24.2 留下「G9 勘误」审计过程记录，§23.7 的对应表述同步改为口径提醒。
- **[新增] 缺口 G10/G11/G12**：G10 `agentix/infrastructure/core/database.py:32` 的模块级 `create_engine` 使 `import agentix.capabilities.mcp` 连带建库引擎（缺驱动即 import 失败，也堵死 T2 的轻量子进程路线）；G11 即上面的 echo；G12 `kb_ingest` 的 `connection_url` 无 validator ⇒ 持 key 者可让服务端连任意 MySQL（SSRF）。G10 归 T1，G12 已在本轮以 T1 ⑫ 提前落地，G11 已在 T0 修。
- **[安全·G12] `KB_MYSQL_URL_GUARD`（默认关）：kb_ingest 不再拿用户输入当连接参数**：`/ingest/mysql` 把请求体的 `connection_url` 直接喂给 `create_engine()`，持 API key 者因此能让**服务端**去连任意 `host:port`（内网探测、把连接指向攻击者的 MySQL 读握手信息、`sqlite:////app/…` 读容器内文件）。新增 `kb_ingest/kb_service/core/db_url_guard.py`：scheme 必须是 `mysql[+driver]`、必须有主机名、可选 `KB_MYSQL_ALLOWED_HOSTS` 白名单（`*.dom`/`.dom` 表子域且**不含 apex**，可带 `:port` 绑定）。（逗号/分号/空白分隔，`*.dom`/`.dom` 表子域且**不含 apex**，可带 `:port` 绑定）。**第二条正交闸**同期补上：主机白名单只回答「连到谁」，不回答「连过去之后客户端愿意干什么」——`?local_infile=1` 会让**对端服务器**要求本客户端上传本地文件，实测 `create_engine("mysql+pymysql://u:p@h/db?local_infile=1").dialect.create_connect_args()` 产出 `{'local_infile': 1, 'client_flag': 2}`（SQLAlchemy 确实 coerce 后交给驱动并置上 `CLIENT_LOCAL_FILES` 位）；现拒 `local_infile` / `allow_local_infile` / `read_default_file` / `read_default_group` 与带该位的 `client_flag`，显式 `local_infile=0` 与不含该位的 `client_flag` 放行，detail 只回显选项名。校验点落在 `MySQLIngestor.ingest()`（HTTP 与 CLI 的唯一汇合点）与路由的**入队前**——放在后台任务里拒绝，客户端只会拿到 202 加一条服务端异常日志。**不做 DNS 解析**：解析结果与真正连接的 IP 没有绑定关系（重绑定照样翻），却把外网延迟引进每次导入，故文档与测试都不暗示它做了内网段判定。开关留 false 时逐位等价（连 `sqlite:` 也照放）。
- **[修复·G12 自查] 闸门自己曾是凭据回显器**：⑫ 的初版写成 schema 的 `@field_validator("connection_url")`。真跑一条被拒请求，FastAPI 的 422 响应体是 `{"detail":[{"msg":"Value error, host 'evil.example' is not in …","input":"mysql://u:Tr3m0d1a@evil.example/kb"}]}`——**pydantic 把被拒输入放进 `errors()["input"]`，而它默认进响应体**，于是只有带凭据的那条会在错误路径上把密码原样吐回去。修法是把校验下移出 pydantic、detail 自己构造（只说为什么被拒、不回放输入），并留一枚反向钉子 `test_schema_does_not_validate_connection_url_anymore` 阻止校验器回到 schema。**通用口径：承载凭据的字段不要过会回显给调用方的 validator。**
- **[安全·T1 ⑧] `TOOL_HANDLER_CONTRACT_ENABLED`（默认关）：handler 类型契约前移到注册期**：G1 那份「生产在案的 6 个同步 handler」清单是人肉 grep 出来的——工具注册时没有任何信号说明它到底同不同步，只有等一次「超时没生效」的排障才发现。现在 `register_handler` 用 `handler_shape` 把 handler 归成 `async` / `sync` / `async_gen`，非协程即打一条带**声明超时**的点名告警并指向 `TOOL_SYNC_OFFLOAD_ENABLED`。口径是「调用后能否拿到可 await 的结果」而非 `iscoroutinefunction` 的字面返回值：类的 `async def __call__`（对 `iscoroutinefunction(实例)` 为 False）判 async，否则每个类形式的工具都会吃假告警；`functools.partial` 包协程仍算 async；异步生成器单列一档、文案**不给**下放线程的建议（它返回 async_generator，下放只会把一个错换成另一个错）。**告警而非拒绝注册**：`search` / `read_skill` / `cypher_query` 本就是同步 I/O 注册进来的，fail-closed 等于在开 flag 那刻打断全部功能。实测开 flag 装上真实技能工具即打印 `TOOL HANDLER CONTRACT: tool 'read_skill' handler is not a coroutine function; … (declared timeout: 30000ms)`——打中生产里真实的 handler，不是夹具。首版用 `getattr(type(handler), "__call__", None)` 被 `ruff` B004 判成「用 `__call__` 探可调用性」（这里要的不是能不能调用，是调用体是不是协程），改为 `type(handler).__call__` 属性访问 + `AttributeError` 兜底。
- **[修复·G13] runbook 配置矩阵与 staging override 漂移**：写 ⑧ 的文档时发现的**我自己这一轮的错**，也是最容易被漏掉的一类——`docs/runbooks/预发环境与配置矩阵.md` 的「配置矩阵」把 `TOOL_SYNC_OFFLOAD_ENABLED` / `MCP_SUBPROCESS_ENV_SCRUB` 的 staging 列写成 `true`，而 `docker/compose.override.staging.yml` 一个都没设（基线 compose 只有 `${VAR:-False}` 转发，实际取值看宿主机 `.env`）。那张表是同一文件验收清单第 5 条「配置矩阵逐项 diff」的基准，等于给运维一张假图。现在 override 显式钉住三个执行边界 flag（契约 `true`、裁剪 `true`、**下放过 `false`**——「先逐个确认 handler 无 loop 亲和」不该被一个自动打开的默认值绕过），矩阵的 staging 列改成 override 的实际值，基线 compose 补 `TOOL_HANDLER_CONTRACT_ENABLED` 转发（默认 `False`）。对齐关系做成断言并各验一次「篡改必须红」：删 override 的键 → 2 条红，改矩阵声明 → 1 条红。§24.2 新增 G13 记录，含「其余行仍靠人眼，表由 compose 生成才是终态」的未收口说明。
- **[安全·T1 ⑬] 数据库 engine 惰性化，切断 G10 的 import 副作用链**：`agentix/infrastructure/core/database.py` 的模块级 `engine = create_engine(...)` 让「导入路径经过它」就等于建池——`import agentix.capabilities.mcp` 实测会连带建库引擎，缺驱动的解析器下直接 `ModuleNotFoundError`，于是只测 MCP 客户端也必须装齐数据面依赖、`DATABASE_URL` 写错的故障落在 import 期而不是启动校验点，T2 的「子进程只加载一个工具」路线被这条副作用堵死。现在改由 `get_engine()` / `get_session_factory()` 首次**取用**时构造单例，`engine` / `SessionLocal` 两个旧名字经 PEP 562 `__getattr__` 再导出，既有 `from … import engine` 写法逐位可用。**最坑的一层不是 `database.py`**：包门面 `infrastructure/core/__init__.py` 里那句 `from .database import … engine` 本身就会在导入期取一次，只改下层等于没改——两层都得收，且这一点静态检查与类型检查都看不出来，只有真起子进程数 `create_engine` 的调用次数才现形。**没配 flag 的理由**：⑬ 是纯重构（取用时机后移、语义不变），§0.5 的「新能力 = 新 flag + 默认关」针对的是会改变执行语义的能力；给纯重构加旗只会留下「两条都跑得通、但只有一条被测试覆盖」的长期分叉，等价性改由测试钉住。实测：mcp / tools / core 门面三条 import 路径建池数 1 → **0**，首次取用恰好 1 次且 `SessionLocal` 身份稳定；`DATABASE_URL=postgresql+nosuchdriver://…` 时 import 成功、取用才抛 `NoSuchModuleError`（SQLAlchemy 自己包的方言错误，不是裸 `ModuleNotFoundError`——断言到具体类型，不放宽成 `Exception`）；retrieval 侧共享同一实例（R3.1 契约未破）；生产代码里模块级取用 `engine` 的只剩数据面 4 处（`retrieval/infra/db/session.py`、`migrate.py`、`index_worker.py`、`scripts/migration/backfill_tenant_id.py`），「import 的就是 DB 层 ⇒ 导入即建池」是有意保留的边界。
- **[安全·T0 ⑤] 容器运行身份成为可声明的旋钮（`AGENTIX_CONTAINER_USER`，默认 `0:0`）**：G8 的容器面此前只补了配额与 `no-new-privileges`，`user:` 一直空着——不是没想到，而是这一格的**失败形态是 restart 崩溃循环而不是拒绝启动**：backend 与 retrieval-worker 共享 `./data` / `./uploads` 两个绑定挂载，entrypoint 会往里写（LightRAG bootstrap 的 `mkdir`/`cp -a`、`TIKTOKEN_CACHE_DIR`），非 root 写不进去时 `set -euo pipefail` 直接退出、`restart: unless-stopped` 把它变成无限重启。所以两个服务同时加 `user: "${AGENTIX_CONTAINER_USER:-0:0}"`（**共用一个变量**是刻意的：两把 uid 必有一方 EACCES），默认 `0:0` 与不写这行逐位等价（server stage 没有 `USER` 指令，这条前提本身也被测试钉住——镜像哪天加了 `USER`，`0:0` 就从 no-op 变成升权），切换步骤、`.env` 的组可读口径与被否掉的「直接把默认值改成 1000:1000」写进 `docs/runbooks/预发环境与配置矩阵.md` 新增的「容器降权（非 root）切换步骤」一节。实测：`docker compose config` 默认渲染 `user: "0:0"`，`AGENTIX_CONTAINER_USER=1000:1000` 时 backend 与 retrieval-worker 两处同时变 `1000:1000`；`docker compose config --quiet` 通过。明确未收口：`cap_drop`、`read_only` 根文件系统、user namespace 映射，以及 `kb_ingest`（独立镜像独立 entrypoint，本轮没给同一个旋钮）。
- **[修复·G13 第二例] staging 的 `--reload` 由操作者的工作站 `.env` 决定**：把 G13 的「矩阵 ↔ override」对齐从三个已知 flag 推广成**整表通则**（凡加粗声明具体值的 staging 单元格都逐行核对）之后，立刻报出两行漂移——`UVICORN_RELOAD` 与 `DB_ECHO` 的矩阵写着 false、override 没设。第一条不是纸面问题：基线 compose 是 `UVICORN_RELOAD=${UVICORN_RELOAD:-${DEBUG:-True}}`，而 **compose 的插值发生在渲染期、取宿主机环境 / `.env`**，跟 override 给容器设的 `DEBUG` 无关。实测 `DEBUG=true docker compose -f docker-compose.yml -f docker/compose.override.staging.yml config` 渲染出 `UVICORN_RELOAD: "true"` ⇒ 预发以 `--reload` 起（文件监听、单 worker 语义）；`DB_ECHO` 当时恰好也是 false，但那是「空值 ⇒ 由容器内 DEBUG 推断」的巧合，不是声明。两个键现在 backend 与 retrieval-worker 都显式钉 `"false"`，重新渲染实测为 `"false"`。**通用口径**：「容器取值 = 文档声明值」在 compose 里不是自动成立的事实，用了嵌套默认又指望 override 覆盖，就是在赌操作者的主机环境干净——要么显式写值，要么文档别写具体值。
- **[安全·T1 ⑪] `SETTINGS_AUTHZ_ENABLED`（默认关）：掩码堵的是读，写要授权才堵得住**：G9 把 `*_api_key` 掩成 `***` 之后，设置接口的残余风险换了形态但没消失——`llm_base_url` / `embedding_base_url` 仍是**任意登录用户可写**的字段，把端点改指攻击者的服务不需要任何密钥，却足以让该租户的提问内容整批外送；`GET /settings/{kb_id}` 也只按 `kb_id` 主键查写，从不比较调用者。现在打开 flag 后：读要求该 kb 属于调用者租户（沿用 `require_kb_in_tenant` 的「不存在 404 / 无权 403、不泄露存在性」口径，`tenant_id IS NULL` 的存量 kb 走 fail-closed 一律 403），写在此之上再要求 admin（沿用 `require_admin`）。**落点口径不是原规划那句「整路由升 `get_current_admin`」**：`KbRagSettings.vue` 是 member 也在用的日常页面，整路由升档会把读一起锁死，而读面已被 G9 掩码收敛过——实际是「读收归属、写加 admin」，与 `DELETE /knowledge-bases/{kb_id}` 上早已存在的读写分档同一套 RBAC 语义。**归属校验刻意排在负载校验之前**，所以越权拿到的是 403 而不是「先验键名再验身份」的 422。进 `_INSECURE_GATE_FLAGS`（关着时启动响亮点名）但**不进** `validate_app_settings()` 的硬失败面——把一条 T1 收紧变成存量部署的拒绝启动，风险大于收益。staging 显式钉 `false`：一则是 compose 在宿主机插值、不设就是让操作者的 `.env` 决定预发权限；二则是全仓**没有自助升 admin 的接口**，此刻开 `true` 会让整面设置页 403。前端同期把 403/404 的**服务端具体 `message`**（而非所有 FORBIDDEN 共用的通用 `user_hint`）显示到面板底部，并在 GET 失败时**禁用保存**——失败后表单里躺着一整份前端默认值，而 PUT 是合并写、提交键又很全，「读取失败仍点保存」会把默认值整批盖掉该 kb 的真实配置。
- **[修复·⑪ 的测试跑出来的真 bug] 同一 kb 的第二次 PUT 静默不落库**：`update_settings` 原先写 `row.config.update(updates)`，而 `ModelConfig.config` 是裸 `Column(JSON)`、没挂 `MutableDict.as_mutable` 监听，原地改字典不被 SQLAlchemy 变更检测看见 ⇒ **第一次** PUT 靠 `INSERT` 侥幸写进去了，**第二次及以后**回 200、`{"data": ...}` 也正常，一个新键都不落库。改为整体重新赋值 `row.config = {**(row.config or {}), **updates}`（比给一列挂监听面小），并由「第二次 PUT 确实落库」这条用例钉住——这条不是 flag 相关行为，任何档位都受影响，之前没被发现只因为没有人连着改两次。
- **[安全·T1 ⑫ 第二半] `KB_MYSQL_WHERE_PARAMETRIZED`（默认关）：导入过滤从「拼接」改成「结构化 + 绑定」**：`/ingest/mysql` 的 `where` 是一段**原始 SQL 片段**，`_build_query` 原先写 `base += f" WHERE {request.where}"`，前面只有一道黑名单（拒 `;` 与注释）。黑名单拦得住堆叠语句，拦不住 `1=1 OR 1=1`、`id IN (SELECT … FROM mysql.user)` 这类**合法但越界**的片段——对拼出来的 SQL 而言，调用方给的就是语法，所以「再补几个黑名单词」这条路本质上收不了口（与 §15.3 是同课）。新增 `kb_ingest/kb_service/core/sql_filters.py`：`MySQLIngestRequest` 多一个结构化 `filters`（`{column, op, value}`，`extra="forbid"`），列名过 `^[A-Za-z0-9_]{1,64}$`（与 `table` 同口径；`1a` 这种数字开头**故意放行**——列名一律反引号 quoted，MySQL 的 quoted 标识符允许数字开头，收紧到字母开头只会拒掉真实存在的名），运算符取封闭枚举 `eq/ne/gt/ge/lt/le/like/in/not_in/is_null/is_not_null`、展开成写死的词不参与拼接，值全部 `bindparams`，条件之间只允许 `AND`（要布尔或关系就分多次导入，别让本服务拼自己看不懂的 SQL）。**闸门是硬切，不是"多一个可选字段"**：flag 开 ⇒ 原始片段一律 400；flag 关 ⇒ 传 `filters` 也 400（静默忽略一个调用方以为生效的过滤条件，等于把整张表灌进向量库，比报错更坏）。只加字段不关旧路，注入面一条也没关——那叫加功能，不叫收口。开 flag 当天所有仍传 `--where` 的作业全部 400，所以出厂默认关、两个 `.env.example` 都写明「先让调用方迁完再开」，CLI 给的是 JSON 形态的 `--filters` 而**不发明 `col=op=val` 这类迷你语法**（那会长出第二套需要被绕过的语言）。`eq NULL`、空 `IN ()`、`is_null` 带值一律报错而不是静默恒假；`MAX_FILTERS = 32` 让「拿过滤器打探针」在请求期现形。错误消息只说违反了哪条规则、第几个条件，**不回显被拒的列名与值**（沿用 G12 那条 pydantic 422 回显凭据的教训）。判据只有 `enforce_where_policy` 一个入口，路由（入队前，回 400）与 `MySQLIngestor.ingest`（HTTP/CLI 汇合点，排在 `create_engine` 之前）共用——两处各写一份判据，迟早只关一条。
- **[安全·T2 ⑭] `EXECUTION_SANDBOX_ENABLED`（默认关）：执行位置从写死变成声明，但**这一步没有增加任何隔离****：`ToolExecutor._invoke` 原先把「在主进程里调用 handler」写死在方法体内，于是「按工具选择执行位置」根本没有落点——G1 的下放只能靠一个全局布尔（`TOOL_SYNC_OFFLOAD_ENABLED` 一开改变**每条**工具的线程语义，一关连该下放的也下不放）。现在 `ToolSpec.sandbox` 声明位置，端口 `ExecutionSandbox` 定义在 `agentix/domain/tool/sandbox.py`（零第三方依赖、不 import 本项目其它层，ADR-0001），实现集中在 `agentix/tools/sandbox.py`。**刻意把「可以声明什么」和「实际能跑什么」分成两份事实**：域侧 `SANDBOX_LADDER = (in_process, thread, subprocess, container)` 是完整阶梯（声明校验只判在不在阶梯内，所以 ⑮/⑰ 的目标档位今天就能写在 spec 里而不出事），基础设施侧 `implemented_kinds()` 只登记 `in_process` 与 `thread`。声明了未实现的档位 ⇒ 在执行点拿到 `reason="sandbox_unavailable"`、`retryable=False` 的失败并**拒绝执行**，不静默退回进程内（静默降级会让「以为有墙」这个误判一直活着）；判定排在 `_observe_before` 之前，因为闸门拒绝不是一次执行，不该被护栏记成工具失败。flag 关时一个都不查，走的还是原来那条进程内调用。等价性是结构性的而非复制出来的：`_invoke` 现在只有一行 `return await invoke_in_process(handler, args)`，而 `InProcessSandbox.run` 复用同一个函数，测试里做了「旧 `_invoke` 实现 / `_invoke` / `InProcessSandbox.run`」三方逐位对照（同步、协程、类的 `async __call__`、异步生成器四种 handler × 下放开关两档）。端口的即时收益：`sandbox="thread"` 让**单条**工具不必打开全局下放就能让出事件循环，实测四种 flag×档位组合下的循环停摆时长（0.2s 同步 handler + 5ms ticker，取相邻 tick 最大间隙）分得很清：占循环的两档 **200–312ms**、下放的两档 **20ms**，其中「端口开 + 默认档」仍是 200ms——打开端口本身不改变语义。⚠️ 三条诚实标注：能跑的两档同进程、同 GIL、同 `os.environ`、同文件权限；原 T2 验收项「沙箱内 `open(os.environ['DATABASE_URL'])` 必须失败」在 ⑭ 阶段是**做不到的**，属 ⑮；这一位**不进** `_INSECURE_GATE_FLAGS`（关着不构成对外风险，理由同 ⑧），staging 置 `true` 的目的是让这条新缝被持续走到，不是宣称预发有隔离。落点偏差一处：规划说实现落 `agentix/infrastructure/`，该层没有 `tool/` 子目录、工具基建统一在 `agentix/tools/`，故实现落那里并由 AST 钉子要求它**不 import** executor（防止依赖反向）。选型与被拒方案（subprocess 的序列化代价与 G2 前置、namespace/seccomp 的 `unshare_probe` 能力探测结论、容器与 microVM 的代价、以及「不要端口直接加 `if` 分支」这条被否）记于 [ADR-0007](docs/architecture/adr/0007-执行沙箱端口.md) 与 `PROJECT_SPEC` §23.8。
- **[文档·G13 终态] `docs/runbooks/预发环境与配置矩阵.md` 的配置矩阵从「人抄的表」改成「部署物的生成物」**：这张表是那份 runbook 验收清单第 5 条的核对基准，也就是**运维照着做的那张表**，而它原先只有第 1 列（键名）是硬事实，dev / staging 两列靠人从 `docker-compose.yml` 与 `docker/compose.override.staging.yml` 里抄。§24.2 G13 记了两次案发：第一次是三个执行边界 flag 的 staging 列写着 `true` 而 override 一个都没设；第二次是把核对推广成整表通则后立刻抓出 `UVICORN_RELOAD` / `DB_ECHO`——后者不只是纸面漂移，基线 compose 写的是 `${UVICORN_RELOAD:-${DEBUG:-True}}`，**插值发生在宿主机的渲染期**，所以「工作站 `.env` 里留着 `DEBUG=true` 的人启动 staging」会把预发以 `--reload` 起。p109 第 6 节当时补的是「针对已知若干键的断言」，本轮把它换成终态形态：新增 `scripts/gen_staging_matrix.py`，既是生成器也是校验器（无参打印生成表、`--check` 漂移即退出 1，给 CI 用）。三条口径从此是机器的而不是人的：**覆盖面被定义**（表的行 = staging override 的 backend 里设了的键 ∪ `MANUAL_KEYS` 里署名登记的手填行，目前只有 `SECRET_KEY`，理由是它的取值由运行期 `resolved_secret_key` 决定而不是字段默认值——想加一行只有"让 override 也管这个键"和"在脚本里写清为什么只能手填"两条路，两条都留名字，「文档作者觉得这项重要」不再是进表的理由）；**加粗 ⇔ override 真设了**，且加粗那格必须**等于** override 的字面值（这一条把 G13 两例的共同形态变成机械冲突，不再依赖谁记得去对）；**dev 列也是派生物**（`${K:-X}` 取 X、`${K:-}` 记「空」、嵌套 `${K:-${OTHER:-…}}` 记「空 + 点名由谁推断」、未转发回落 `Settings` 的**字段默认值**——刻意读 `model_fields` 而不是实例化 `Settings`，因为实例化会吃宿主机 `.env`，那正是这张表要避免的污染源）。落地当轮就报出两处真漂移并都改的是**文档**：`OTEL_EXPORTER_OTLP_ENDPOINT` 表里写 `prometheus:9090`、override 写 `http://prometheus:9090`（少协议头的值抄到别的部署物上就是一个无效端点）；`PROMPT_INJECTION_DETECTION_ENABLED` 被 override 设了却没加粗（按新约定即「预发没动它」，与事实相反）。**一处刻意的落点偏差要记清**：规划说终态是「由 `docker compose config` 的渲染输出生成」，实现读的是两份 YAML 的**声明**——渲染会把宿主机真实 `.env` 展开到 stdout（终端与 CI 日志因此变成密钥出口），而且渲染结果本身就被 `.env` 污染，用它校验文档等于让被校验的对象决定校验基准。所以 staging 列逐字等于容器值（override 写的是不参与插值的带引号字面量），dev 列则**在表旁边写明**它描述的是「宿主机没设该变量时」的基线取值，不再靠读者自觉。残余两块机器买不到，也不再假装：production 列没有第三份部署物、脚本不校验它；表外的键（compose 转发、override 不管）不进表——收口方向是让 override 成为跨环境差异的唯一声明源，而不是再补几行人眼检查项。验收清单第 5 条因此从「逐项 diff」改成跑 `--check`。
- **[校验] 全部改动真跑过**：新增 `tests/test_p107_sandbox_hardening.py` **66 条**（含 flag-off 与加固前逐位等价的反向断言、G5 的 §24.2 表逐行复测、G2/G8/G11 的子进程与本地 HTTP 服务实测、staging 硬失败面跑到真实启动函数而非只看布尔值）与 `tests/test_p108_kb_mysql_url_guard.py` **72 条**（纯函数 / 开关语义 / 真实入口三层；入口层用真实 `TestClient` 断言被拒请求返回 400 **且响应体不含哨兵密码**，并跑 `MySQLIngestor.ingest` 证明闸门排在 `create_engine` 之前）与 `tests/test_p109_tool_handler_contract.py` **33 条**（flag-off 逐位静默、`partial` 与类的 `async __call__` 不误报、异步生成器的文案不得指向下放线程、声明超时进告警、取不到 spec 时降级成 `?` 而不抛，同一批 handler 在「契约开/关 × offload 开/关」下 `_invoke` 返回值逐位一致，以及第 6 节的 runbook 矩阵 ↔ staging override ↔ compose 转发对齐，**第 6 节本轮已从三个已知 flag 推广成整表通则**，并配「删 override 的键 / 改 override 的值」两种篡改都必须报出来的反向验证）与 `tests/test_p110_lazy_database_engine.py` **11 条**（前三条各起真子进程数 `create_engine` 调用次数；另含旧形状可用、单例身份、`__getattr__` 语义、缺驱动时「import 成功 / 取用才炸」，以及一条禁止模块级建池回潮的形状钉子）与 `tests/test_p111_container_user_knob.py` **11 条**（默认 `0:0` 等价、镜像无 `USER` 这条前提本身、两个服务同源、runbook 是否真教了切换步骤、compose 被改坏必须红；全部走解析文件，所以没有 Docker 守护进程也能跑）与 `tests/test_p112_settings_authz.py` **22 条**（用真实 `TestClient` 打路由而不是单测 helper：flag-off 逐位等价——跨租户读/写照旧 200、**不存在的 kb 读也照旧 200 而不是 404**；flag-on 的 403/404 分工、响应体不回显 `kb_id`、`tenant_id IS NULL` fail-closed、**归属校验排在负载校验之前所以越权是 403 不是 422**、第二次 PUT 确实落库；两条口径同源钉子要求路由复用 `require_kb_in_tenant` / `require_admin` 且不得再出现 `current_user.role != "admin"` 这类本地判断；一条反向验证要求 admin 在同租户不被锁在外面；部署面验 `.env.example` / compose 转发 / staging 钉值 / worker 不拿这个旋钮 / runbook 写了两个前置；可发现性用 `_record` 桩真跑一遍 `_warn_insecure_posture()` 并格式化模板）与 `tests/test_p113_where_parametrization.py` **75 条**（七层：默认姿态、flag-off 与加固前**逐字节**相同且 `_bindparams` 为空、flag-on 的收紧面、两个入口 + CLI、口径同源反向钉子、部署面含 `clone_config` 必须保住这一位、**真执行**（在内存 sqlite 上跑 `_build_query` 的产出——sqlite 认反引号 quoted 标识符，MySQL 形状 SQL 原样可跑——并且走生产那条**一模一样**的调用形状 `pd.read_sql_query(stmt, conn, chunksize=…)`；布尔恒真探针必须匹配 0 行，另配一条「同表不过滤确有 3 行」的对照组，免得那个 0 其实是没连上库；flag-off 那一支也同样真跑，因为"逐位等价"不该只是字符串等价）；「值不进 SQL 文本」用**真方言编译**验而不是只看 `str(text())`——pymysql / mysqldb / mysqlconnector 是 `format` 风格渲染 `%s`、sqlite 是 `qmark` 渲染 `?`，四种渲染文本里都没有哨兵值，而「按名字传值、不靠顺序拼串」由 `compiled.params` 的键名证明；HTTP 面刻意用真 `Config()` 而非哑配置，因为成功路径会 `clone_config(config)` 遍历 `fields(Config)`，换哑配置压根跑不到那条代码；另有一条反向钉子把 `_build_query` 源码按 `elif request.where:` 切开，要求 `{request.where}` 这个插值形状只出现在 flag-off 一侧。**这套用例另外做过 11 处变异**——值内联、参数化分支变死、legacy 分支变死、两处策略调用各被中和、默认值翻转、标识符白名单放宽、运算符枚举放宽、空 `IN` 放行、`filters` 被静默忽略、原始片段被照收——**11/11 全部被抓**，跑完逐一还原并复测基线绿）与 `tests/test_p114_execution_sandbox.py` **66 条**（七层：默认姿态，含一条「词汇表缺口恰好等于未实现的 ⑮/⑰」的机械核对（`("subprocess", "container")`）与「非阶梯词在声明期就报 `ValueError`」（`Subprocess` / `inprocess` / `docker` / 带空格的 `thread ` 全拒）；分层钉子走 **AST 而不是 grep**——端口文件的项目内/第三方 import 集合必须为空、实现文件不得 import executor（防依赖反向）、`_invoke` 源码里不得再出现 `to_thread`（防第二条下放分支各跑一套）；**三方**逐位等价（加固前的 `_invoke` 内联实现 / 现在的 `_invoke` / `InProcessSandbox.run` × 同步、协程、类的 `async __call__`、异步生成器四种 handler × 下放开关两档）；flag 关时**故意**声明跑不了的 `subprocess` 且照常执行成功（证明声明惰性）；flag 开的拒绝面——`calls == []` 证明 handler 一次都没被调用、护栏观察钩子不被触发（闸门拒绝不是一次执行）、拒绝排在重试之前、拒绝名单跟着注册表走（`monkeypatch.delitem` 摘掉 `thread` 就必须变成拒绝，防止把阶梯写死成第二份清单）；事件循环停摆时长四行对照（关下放 × `in_process` × 阻塞 handler 必须停摆，其余三档必须不停摆；计时取相邻 tick 最大间隔而不是 tick 计数，因为循环被堵住时恰恰是探针饿死的时候）；配置与部署面五处同步 + compose 默认 `False` 转发 + runbook 矩阵那句加粗的 staging 声明与 override 实际值同源，并配一条「把 override 改坏必须红」；`ADR-0007` 文件存在且进了两处索引；最后一条钉子要求台账同时出现「0007 链接」与「⑮」——**记 ⑭ 落地不等于记 T2 完成**。**这套用例另做过 19 处变异**（默认值翻转、未知档位与未实现档位两处"静默退回进程内"、executor 根本不查声明、声明被忽略成永远按 `in_process` 解析、拒绝被吞掉继续执行、拒绝记成可重试、拒绝排到观测钩子之后、声明校验放宽成任意字符串、域层端口染上第三方依赖、实现层懒 import executor、executor 重新自带第二套下放分支、把 `subprocess` 假装成已实现、`thread` 档偷回进程内、selfcheck / `.env.example` / p41 手写清单各删键、compose 默认翻 `True`、staging override 改 `false`），**19/19 全部被抓**；其中「selfcheck 删键」与「p41 清单删键」两条**首版没被抓到**，根因是同步检查写成 `KEY in text`——`EXECUTION_SANDBOX_ENABLED_TYPO` 里就含这个子串，于是"五处同步"的守卫在最常见的"把键名写错"场景下空转。改成整词匹配并补一条反向钉子（后缀形态必须不匹配、裸 `KEY = false` 与 compose 的 `${KEY:-False}` 形态必须匹配）后才归位；**同一口径复盘了 p41 那份手写清单**：它走的是正则抽键 + 集合成员判断，本来就是整词口径，弱的是我这处 `in`。跑完逐一还原并复测基线绿）；与 `tests/test_p115_staging_matrix_generated.py` **44 条**（G13 终态：compose 四种转发写法各自的 dev 推导、`environment:` 的 mapping 与 `- K=V` 两形读出同一个字典（只吃 mapping 的解析器遇到改写过的 compose 会**静默少一行**，于是表不再覆盖那个键而 `--check` 照样绿）、表的覆盖面**双向**等于「override 设了的键 ∪ 署名的手填行」、`MANUAL_KEYS` 每条前提本身（不在 override 里、且确实是 Settings 字段——否则它连字段默认值都读不到）、`check()` 当前必须干净、加粗 ⇔ 被钉 的整表通则、G13 两例的 dev/staging 三格现在是由 override 算出来而非抄的、表头必须等于 `render()` 的表头（那句「production 无部署物兜底」写在表头里，粘表时会被顺手改掉）、解析器受**小节边界**约束（今天那 16 行恰好全在 §配置矩阵 内，所以"整篇扫"看不出差别；新加一张形状相近的表就会逐条假报漂移，所以这堵墙得有用例压着）、可发现性两条（表旁边必须写着生成器、验收清单第 5 条必须还让人眼 diff）、`--check` 的退出码与 UTF-8 输出。**八种漂移形态各有反向用例**：删 override 的键 / 改 override 的值 / 改表的 staging 声明 / 改表的 dev 声明 / 删表一行 / 多一行未登记的键 / 多一行却加粗 / 把该加粗的抹平——后三条分别对应「没登记」「假声明」「记号被抹掉」三种不同性质的问题，各有独立的报错措辞。**对 `scripts/gen_staging_matrix.py` 本身做过 18 处变异**（`check()` 整个中和、dev/staging 两个比较各变死、加粗要求与加粗假声明两处各被摘、少一行与多一行两个覆盖面条目各被删、`normalize_cell` 丢掉加粗信号、单元格不再剥反引号、`environment` 只吃 mapping、`MANUAL_KEYS` 不再参与生成、手填行反而进校验、`pinned` 恒 False、Settings 默认读不到、`${K:-}` 改由默认值填空、解析不受小节约束、service 名写错、行首正则放宽），**18/18 全部被抓**；其中最后一条首版是**等价变异**——`runbook_matrix()` 里正则与 `startswith("| \`")` 两条判据同义，放宽正则被后者挡住，改坏了也看不出来；删掉那条冗余 `startswith`（判据只留一份，与 §15.9「两处各写一份判据迟早只关一条」同一个病）之后才被抓。p113 那 11 处变异在 75 条的当前基线上**重跑一遍仍然 11/11 全抓**（当时是 71 条）；所有变异跑完逐一还原并复测基线绿）；前端另有 `web/src/modules/knowledge/components/KbRagSettings.test.ts` **15 条**（新增 6 条覆盖 ⑪ 的可辨识度）；`npm run lint`（`vue-tsc --noEmit`）0 错误、`npm run test` **223 passed / 26 files**；`ruff check agentix tests kb_ingest` 全绿（G13 终态后范围扩到 `ruff check agentix tests kb_ingest scripts`，脚本目录首次进 lint 面）；配置键五处同步（`settings.py` / `selfcheck.py::DEFAULT_IGNORED_MISSING` / `.env.example` / `配置管理.md` / p41 手写清单）后 `test_p41_doc_consistency.py` + `test_p35_config_governance.py` 63 passed，连同 p109 与 `test_p32_tool_guards.py` 合跑 182 passed；`docker compose config --quiet` 通过；受影响的 `test_mcp_client` / `test_mcp_server` / `test_p32` / `test_p17` / `test_p66` / `test_p85` / `test_p38` / `test_p47` / `test_p51` 全绿；**全量离线套件** `pytest -m "not integration"` → **4729 passed / 14 skipped / 0 failed**（401.34s，Python 3.12.13，未开并行），实测数登记于 README §11（唯一真源）。这一轮是 ⑫ 落地后的第一次全量：首跑 **2 failed**，全部落在 `tests/test_p91_final_boundaries.py::TestMySQLIngestSQLGuard`——两条 P91 老用例用 `MySQLIngestor._build_query(None, request)` 裸调，而 ⑫ 后该方法要读 `self.config.mysql_where_parametrized`，`self=None` 直接 `AttributeError`。⑫ 当时只跑了定向守卫（p113 及其邻域），这个文件不在其中，破坏面直到全量才露头；`p113` 的 11 处变异、`p115` 的 18 处变异也都证明不了"别的文件没被撞坏"。修法只改用例——`MySQLIngestor.__new__` + 真 `Config()`，flag 默认 `false`、断言仍是加固前的 SQL 字符串（参数化分支的逐字节对照仍由 p113 负责，不在 p91 重复），产品代码零改动；p91 复跑 **50 passed**，再复跑全量得 4694；随后 G7 卫生项批次新增 p116 35 条，最终复跑为 **4729**（401.34s）。教训写进 README §11：定向回归证明"新行为对"，全量套件才证明"旧行为没被撞坏"，两者不可互替。
- **[同步] 文档面**：`docs/项目实现手册.md` 新增 §23（技术选型依据与被拒方案）与 §24（执行隔离与沙箱现状审计，G1–G12 逐项带修复状态 + 落地状态表），§0.3 章节地图补入两章与「元问题」分组，§24.5 补第 4 条失效条件（正文是案发记录、落地状态表才是当前事实）。`docs/architecture/运行时护栏.md` 新增 §15「执行边界加固」四条口径 + 顶部能力表 7 行 + §9 速查 4 行；`SECURITY.md` 安全设计概览 +9 行、部署建议 +5 条、已知边界 +3 条；`README.md` §7 可靠性 +1 条、§16 已知限制 +1 条；`docs/runbooks/预发环境与配置矩阵.md` 配置矩阵 +4 行、验收清单 +2 条；`docs/runbooks/密钥管理.md` 当前防线 +4 项；`docs/源码研读笔记.md` §2.4 沙箱对照 + §5「未采纳项」表（明确记录 namespace/seccomp 未采纳的理由与替代）；`AGENTS.md` 硬性纪律新增第 6 条（语法闸≠资源闸、`wait_for` 只截断让出循环的调用）并把 `SECURITY.md` 与 README §7/§16 补进文档同步面。⑫ 落地后的增量同步：`PROJECT_SPEC` G12 改写为「已修复 + 修复形态 + 仍未收口（`where` 参数化）」并翻转落地状态表第 ⑫ 行（**该条已被下面「⑫ 第二半」覆盖**，此处保留当时口径是为了留下"谁在什么时候改了什么"的痕迹）；`运行时护栏.md` 新增 §15.5「跨服务输入：连接串不是可透传的参数」（原「未收口」顺延为 §15.6，含 pydantic `input` 回显与不做 DNS 两条口径）、能力表 +1 行、§9 速查补一段说明 kb_ingest 的键为何不进本表（否则 p41 会索要主应用 Settings 字段）；`SECURITY.md` 概览 +1 行、部署建议改 `STRICT_VALIDATION` 口径并 +1 条 kb_ingest 双闸门、已知边界改写为「闸门默认关」；`README.md` §7 补 ⑫、§16 +1 条 kb_ingest 边界默认关；`kb_ingest/.env.example` 与根 `.env.example`、`docker-compose.yml` 三处补两个新键的语义。⑧ 落地后的增量同步：`PROJECT_SPEC` G1 补「配套治理（T1 ⑧）」与判定口径两条、落地状态段落与状态表第 ⑧ 行翻绿、T1 规划行标注「落点口径不是 `iscoroutinefunction` 字面返回值」；`运行时护栏.md` 新增 §15.6「handler 类型契约：先让『谁不可超时』可 grep」（原「未收口」顺延 §15.7，开篇口径计数由四条改七条）、能力表 +1 行、§9 速查行的指向从 §15.1 改 §15.6、回归覆盖补 p109 25 项；`SECURITY.md` 概览 +1 行、部署建议 +1 条「预发开一次契约 flag 并把清单抄进部署记录」；`README.md` §7 补 ⑧、G1–G12 口径改 G1–G13；`docs/architecture/配置管理.md` 顺手消掉一处歧义引用（`DB_ECHO` 行的「§15.5 loguru 出口」补成 `PROJECT_SPEC §15.5`，同文件另一处 §15.5 指的是运行时护栏）；G13 侧另动 `docker-compose.yml`（转发新键，默认 `False`）、`docker/compose.override.staging.yml`（钉住三个执行边界 flag）、`docs/runbooks/预发环境与配置矩阵.md`（矩阵三行改为 override 实际值 + 验收清单第 8 条收集契约告警）；另按 `test_p41_doc_consistency.py::test_new_settings_are_documented` 自带那句「新增 Settings 字段时请一并把键加到这里」把新键补进该手写清单（不加就是静默绕过），并在 `agentix/main.py` 的 `_INSECURE_GATE_FLAGS` 上留一条判据注释——⑧ 只产日志、关着不构成对外风险，所以**不进** `SECURITY POSTURE` 告警面（否则那条响亮日志会变成噪音）。⑬ 落地后的增量同步：`PROJECT_SPEC` G10 翻「已修复」并补「修复形态（含门面那层）」+「没配开关的理由」+ 回归指向，落地状态段落与状态表第 ⑬ 行翻绿、T1 规划行标注实测的模块级取用点（生产代码只剩数据面 4 处，`retrieval/infra/db/session.py` 有意保留导入期取用）；`运行时护栏.md` §15.7 从待做清单移除 ⑬ 并留一条新口径（「import 了 DB 模块但没连过库」从此是存在状态，建池不再由导入顺序保证）、回归覆盖补 p110 11 项；`README.md` §7 执行边界加固条目补 ⑬。⑤ 与 G13 第二例的增量同步：`PROJECT_SPEC` G8 翻「已修复（含运行身份旋钮）」并补「修复形态 / 等价性前提 / 仍未做」三段、G13 补第二个实例与「容器取值 = 声明值不是自动成立」的通用口径、落地状态段落与状态表第 ⑤ 行翻绿；`运行时护栏.md` 新增 §15.7「compose 的插值发生在宿主机，不在容器里」并把「未收口」顺延为 §15.8（开篇口径计数由七条改八条，§9 速查的指向不受影响）、§15.8 从待做清单移除 ⑤ 并改写容器边界那段（原来那句「没有 pid namespace 之外的进程隔离」不准确——namespace 是 docker 默认就给的，缺的是 `cap_drop`/`read_only`/user namespace）、回归覆盖补 p110/p111 并把 p109 改为 33 项；`docker-compose.yml` backend 与 retrieval-worker 各加一行 `user:`，`docker/compose.override.staging.yml` 钉 `UVICORN_RELOAD` / `DB_ECHO`；`docs/runbooks/预发环境与配置矩阵.md` 新增「容器降权（非 root）切换步骤」一节（含「为什么这个键有意不进配置矩阵」）、验收清单第 5 条改为指向整表通则并 +1 条运行身份验证；`SECURITY.md` 部署建议的容器那条改写为「旋钮已到位、默认仍 root、失败形态是崩溃循环」。⑪ 落地后的增量同步：`PROJECT_SPEC` G9 的「修复状态」改为「后半段已由 ⑪ 补上（默认关）」并新增「后半段」「两个开启前置」「顺带修掉的 JSON 列真 bug」三段、落地状态段落加入 p112 与 ⑪（并把 p109 的条数从过期的 31 改回实测 33）、状态表第 ⑥/⑪ 行翻绿且 ⑪ 行写明「口径修正：不是整路由升 admin」，T1 规划行标注同一处落点偏差，§24.5 第 4 条失效条件的剩余项由「⑨⑪」改为「⑨」；`运行时护栏.md` 顶部能力表 +1 行、§9 速查 +1 行、新增 §15.8「掩码不是授权：可写字段就是一条出站通道」（原「未收口」顺延 §15.9，开篇口径计数由八条改九条）、§15.9 从待做清单移除 ⑪ 并留下落点偏差与残余的「全仓没有自助升 admin 的接口」，回归覆盖补 p112 与前端 vitest 面；`docs/architecture/配置管理.md` 安全类行补键名并新增一整行语义/前置说明；`docs/runbooks/预发环境与配置矩阵.md` 配置矩阵 +1 行（staging 钉 false 的理由、prod 建议 true）、新增「设置接口授权收紧（`SETTINGS_AUTHZ_ENABLED`）切换步骤」一节（两条前置各给可跑 SQL、curl 验 403/404、回滚、买到/没买到什么）、验收清单 +1 条；`SECURITY.md` 已知边界的「密钥掩码不等于授权」从「仍未收口」改写为「flag 已存在但默认关 + 残余是写面」；`README.md` §7 执行边界加固条目补 ⑪。前端 `KbRagSettings.vue` 的错误可见性与保存禁用口径写进 runbook，避免现场把「按钮是灰的」当故障报上来。⑫ 第二半落地后的增量同步：`PROJECT_SPEC` G12 的「修复状态」去掉「`where` 参数化未做」、「剩余风险」里那句改成案发记录口径、「归入 T1」那条标注实际落点，原「仍未收口」一条换成五条（第二半的判据更换 / 硬切形态的理由 / 代价写在配置面上 / 错误不回显值 / 口径同源钉子）、落地状态段落加入 p113 并把 T1 剩余项收成只剩 ⑨、状态表第 ⑫ 行改写为「两半都已落地，都默认关」、§24.5 第 4 条失效条件的剩余项同步收窄；`运行时护栏.md` 新增 §15.9「拼接式过滤：值不许进 SQL 文本」（原「未收口」顺延 §15.10，开篇口径计数由九条改十条）、顶部能力表 +1 行、§9 速查那段从「两个键」改「三个键」并指向 §15.9、§15.10 从待做清单移除 ⑫ 且留一句「已落地在这里的含义是能拧过去，不是已经拧过去」、回归覆盖补 p113 那条；`docs/architecture/配置管理.md` 对端键段落补第三个键并指向 §15.9，**继续用散文形态**——写成 `| KEY |` 行会被 p41 索要成主应用 Settings 字段；`SECURITY.md` 部署建议 +1 条「先迁调用方再开，别在生产直接翻开」、已知边界 +1 条「过滤参数化闸门默认关」；`README.md` §7 补 ⑫ 第二半（含"为什么不是再补几个黑名单词"）、§16 那条「`where` 仍是黑名单式过滤」从"两道边界"改写为"三道边界 + 后一位是硬切"；配置面 `agentix/config/selfcheck.py::DEFAULT_IGNORED_MISSING` + 根与 `kb_ingest` 两个 `.env.example` + `docker-compose.yml` 各补该键（默认 `false`）。⑭ 落地后的增量同步：`PROJECT_SPEC` 新增 §23.8「工具执行位置：进程内默认 + 可声明的端口」（含四条被拒方案：不加端口直接加分支 / `subprocess` 直接上 / namespace+seccomp / microVM，各自的代价与重新评估触发条件），原 §23.8「选型变更的同步面」顺延为 §23.9；§24.1 那句「那面墙要等 T2」改成「⑭ 只把位置变成可声明、墙要等 ⑮」；落地状态段落加入 p114 并把「T2、T3 未开始」改成「T2 只剩 ⑮⑯、T3 未开始」，状态表把原 `⑭–⑯` 一行**拆成两行**（⑭ 翻绿并写明两处落点偏差 + ⚠️ 那条假验收，⑮⑯ 留白），T2 规划行标注「验收项现在只兑现了一半」；`docs/architecture/adr/0007-执行沙箱端口.md` 新建（状态写「已采纳（**部分**）」，含四档阶梯的理由、被拒方案、负面后果「它没有带来任何新的隔离」与未收口清单），并在 `adr/README.md` 与 `docs/architecture/README.md` §8 两处索引登记（后者顺手补齐此前缺的 0004–0006）；`运行时护栏.md` 新增 §15.10「执行位置是声明出来的：但"有端口"不等于"有墙"」（原「未收口」顺延 §15.11，开篇口径计数由十条改十一条）、顶部能力表 +1 行并把 ④ 的落点从 `executor.py::_invoke` 改指 `tools/sandbox.py::invoke_in_process`、§9 速查 +1 行、§15.11 从待做清单移除 ⑭ 且写明「⑭ 在这里的含义是位置可以声明了，不是执行被隔离了」、回归覆盖补 p114；`docs/architecture/配置管理.md` 工具类键行 +1；`docker-compose.yml` 转发该键（默认 `False`）、`docker/compose.override.staging.yml` 钉 `true` 并在注释里写清「打开是为了走到这条缝，不是为了隔离」；`docs/runbooks/预发环境与配置矩阵.md` 配置矩阵 +1 行（含「开 true 也不等于预发有隔离」与 prod 按需的理由）；`SECURITY.md` 概览 +1 行、部署建议 +1 条「staging 置 true 的目的不是买隔离，且不进 `SECURITY POSTURE` 点名清单」、已知边界的「没有执行沙箱」改写为「没有墙，只有一个刚建好的插座」；`README.md` §7 补 ⑭、§16 同一条改写；另把 `test_p41_doc_consistency.py` 的手写已记录键清单补上该键。G13 终态落地后的增量同步：`PROJECT_SPEC` §24.2 G13 的「修复状态」由「override 补齐 + 键名对齐做成测试断言」改成三段式「override 补齐 → 整表通则做成断言 → **终态：表由部署物生成**」，案发证据里那句「验收清单第 5 条要求逐项 diff」补一句「⚠️ 引的是案发当时的原文，这一条已改写」（§24.5 第 4 条失效条件说的就是这种"正文是案发记录、状态表才是当前事实"的分裂，不做标注就会让人去表里找一个已经不存在的步骤）；原「仍未收口」换成三段——终态怎么落地的（覆盖面被定义、加粗 ⇔ 被钉、`MANUAL_KEYS` 署名）、**为什么刻意不用 `docker compose config` 的渲染结果当事实源**（密钥外泄面 + 被校验对象决定校验基准）、残余是什么（production 列不校验、表外的键不进表）；§24.3 落地状态段落补一行 T1 收尾项。`运行时护栏.md` §15.7（compose 插值发生在宿主机）末尾从「把表改成由渲染输出生成才是终态」改写成终态已落地 + 四条口径（含落点偏差的理由），并把那句错误的终态指向删掉——那句如果被后来人照做，就会把 `.env` 渲染进 CI 日志；§15.11 未收口清单加一条「这张表只兜住 dev/staging 两列，production 列与表外的键仍在射程外」（不新增小节，避免 §15.x 编号与开篇口径计数再次整体顺延）；回归覆盖补 p115 那条（含等价变异那一段），并顺手把 p113 那句「对这 71 项做过 11 处变异」改成实测的 75 项。`docs/runbooks/预发环境与配置矩阵.md`：表头第四列改为 `production（**手写，无部署物兜底**）`（与 `render()` 逐字一致，由 p115 钉住）、表旁边的「两个可信度来源不一样」整段重写为「dev/staging 是生成物 + 覆盖面怎么定义 + 加粗的记号含义 + production 为什么不受校验」、验收清单第 5 条从「逐项 diff」改成跑 `--check` 并写明人眼 diff 为什么不算（看不出假声明、且被宿主机 `.env` 污染）；两处由生成器报出的真漂移改正（`OTEL_EXPORTER_OTLP_ENDPOINT` 补协议头、`PROMPT_INJECTION_DETECTION_ENABLED` 补加粗）。`SECURITY.md` 与 `README.md` 本轮不动——这张表不是安全边界也不是运行时能力，写进那两处只会稀释它们的内容；生成器的存在由 runbook（运维的入口）+ PROJECT_SPEC（台账）+ runtime-guardrails §15.7（口径）三处覆盖，p115 检查的就是这三处是否互相指得到。

### CI 配置与文档断链修复（2026-10-22）

- **[修复] `.github/` 整目录缺失**：仓库快照里没有任何 CI 配置，导致 19 个契约测试长期红灯（`tests/test_ci_gate_contract.py`、`tests/test_p46_toolchain_parity.py` 等把 `ci.yml` 当作被守的真源）。按 `Makefile` 的 lint 契约与 `docs/项目实现手册.md` §21.2/§20.5 重建 5 个 workflow：`ci.yml`（8 个并行 job + `ci-gate` 汇总；lint 三步与 `make lint` 逐字同口径；`eval-offline` 只对 5 个 `*_applicable` 指标设门）、`perf-baseline.yml`（`set -o pipefail` + `actions/cache` 持久化 `perf-baseline.json` + `p95_seconds` 以 `lower_is_better` 比对）、`release.yml`、`codeql.yml`、`secrets-scan.yml`。
- **[修复] 两处文档断链**：`README.md` 的 [PR 模板] 与 `docs/版本管理策略.md` 的 `../.github/workflows/release.yml` 指向不存在的文件；随 `.github/PULL_REQUEST_TEMPLATE.md` 一起补齐，并补上 `CONTRIBUTING.md` 早已引用的 `.github/ISSUE_TEMPLATE/*` 与 `.github/dependabot.yml`（pip / npm / github-actions / docker 四生态）。
- **[修复] `docs/migration/` 被整体删除**：11 个迁移文档缺失，`docs/architecture/接口与契约.md` 指向 `Agent_Core_2.0_规划.md` 的链接因此悬空，另有 24 处引用指向已删文件。用 `git restore docs/migration/` 取回，内容零改写。
- **[校验] 不是"语法正确"就算数**：5 个 workflow 先过 YAML 解析 + 每个 `run` 块 `bash -n` + heredoc 内 Python `ast.parse`（37 个 run 块 / 4 个 Python 块），再把真实步骤正文抽到沙箱逐条执行 16 项：缺 `baseline_predictions.jsonl` 走响亮跳过并写 `skip=true`；门禁首跑对 5 个指标各建档一次；基线抬高后必须 `FAIL` 退出码 1；summary 缺键必须 `::error` + 退出码 2；本机实测压测 200/200 成功（p95≈0.010s、≈1078 rps）；端口不通时 `tee` 管道仍返回 2（证明 `pipefail` 有效）；`p95=null` 判失败而非 0；tag 与包版本不一致时 `::error` + 退出码 1；`ci-gate` 对 failure/cancelled 均阻断。
- **[修复] 一个"换台机器就红"的架构守卫**：`tests/test_p17_domain_layer.py` 的探针用例用 `try: relative_to(PROJECT_ROOT)` 来区分真实源码树与临时探针目录，于是只要 pytest 的 `--basetemp` 落在仓库内（如 `--basetemp=.tmp/...`），临时目录也能相对仓库根解析成功，键名变成一长串 basetemp 路径并 `KeyError`。改为按"是否真实 infra 目录"显式选基准；两种 basetemp 位置各跑一遍均 27 passed。
- **[修复] 同一类污染的第二个守卫**：`tests/test_p44_cache_semantics.py` 用 `os.walk(REPO_ROOT)` 扫配置文件里的宽开放缓存指令，而它自己的 `test_detector_has_teeth_on_synthetic_samples` 会往 `tmp_path` 写一份**故意违规**的 `nginx.conf`。`--basetemp` 指到仓库内时，扫描会把这些合成样例捞回来，让"无违规"那条断言随机红。把 `.tmp` 加进 `_SKIP_DIR_PARTS`（该目录已被 `.gitignore` 的 `*.tmp` 规则忽略、`git ls-files` 命中 0，本就不属于交付源码），非真空用例仍然扫到真实配置文件，两种 basetemp 各跑一遍均 42 passed。
- **[同步] 文档一致性**：`README.md` §界面截图的采集口径改为 2026-10-22 实机重拍；`tests/test_p41_doc_consistency.py` 里"`.github/` 不随快照分发"的排除理由改为现状理由；全量离线回归 `pytest -m "not integration"` **4294 passed / 14 skipped / 0 failed**（6:22），默认 basetemp 与 `--basetemp` 落在仓库内两种位置均已实测。

### 图片类旧残留清理（2026-10-22）

- **[重拍] 两张界面截图**：启动本地全栈（后端 + 前端 + 数据库容器）后用隔离测试账号在 1440×900 实机重拍 `docs/assets/ui/knowledge-workspace.png` 与 `assistant-workspace.png`，替换掉文案改造前的旧图；截图中已无清理前的领域文案，`README.md` 的截图说明同步改写为「来自本地隔离测试账号与测试数据库」。
- **[重生成] 架构图与 RAG 流程图**：`docs/assets/project-architecture.png` / `.svg` 由 `docs/design/generate_project_architecture.py` 重新出图；`docs/assets/rag-hybrid-pipeline.png` 改为由新增的 `docs/design/generate_rag_pipeline_diagram.py` 产出（1800×640，与 Word 说明书内边距同宽高比），并替换 `docs/My-Agentix项目说明书.docx` 中那张字符被降级成 `?` 的旧图（docx 结构 928 段 / 119 表未变，原文件备份在 `.tmp/`）。
- **[重建] 前端产物**：`web/dist` 重新构建，包体内旧文案命中数 19 → 0。

### 说明书乱码单元与零引用图片（2026-10-22）

- **[修复] docx 里 50 个被降级成 `?` 的文本单元**：`docs/My-Agentix项目说明书.docx` 的 §0.1 章节地图第 23 行、`2.2 agentix/ 子包职责` 表（19 行 × 2 列里的 20 格）与整章「23. RAG ……」（13 个正文段落 + 末尾路线表 16 格）中文字符全部变成了 `?`，只剩 ASCII 数字与英文可读。按 `docs/项目实现手册.md` §2.2 / §9.9 与 `docs/design/检索增强深度优化.md` §8 的当前内容逐格重写，微基准数字（6.76 ms / 0.0034 ms / 1989× / 20.22 ms / 19.05 ms / 1.06×）按设计文档原口径复述、不自行加工。改写脚本只动 run 文本、且**改写前必须判定该单元当前确实是乱码**，结构保持 928 段 / 119 表；改后全文 `?{3,}` 命中 0，`tests/test_p41_doc_consistency.py` 37 passed（docx 内所有仓库路径引用仍真实存在）。原文件备份 `.tmp/My-Agentix项目说明书.backup-preQFix.docx`。
- **[补充] 残留措辞**：`docs/design/检索增强深度优化.md` §8 的「相似条文」漏在上一轮文案清理之外，改为「相似的数据条目」。
- **[清理] 两张零引用图片**：`web/src/modules/knowledge/assets/hero.png`（44 KB，全仓 `hero.png` 引用 0 处）删除，可从 Initial commit 取回；`data/1.png`（21 MB，`.gitignore:365` 已单独忽略、无文档引用）先移到 `.tmp/deleted-assets-1022/1.png` 而非直接删除，确认无误后可自行清空。删除后 `npm run build`（`vue-tsc --noEmit && vite build`）退出码 0，产物 hash 仍为 `index-DgCQzoZy.js` 与构建前一致——反向证明这两张图从未进过包。

### 领域文案泛化（2026-10-22）

- **[清理] 面向人的表述**：把项目内面向读者的中文文案、提示词与注释中的特定行业措辞统一为「测试数据集 / 资料库」口径；前端界面文案与 HomeView 提示卡同步。
- **[保留] 功能性字面量**：Neo4j 标签、`legal_db` 等库表与 schema 名、`MILVUS_COLLECTION="legal"`、`legal_*` 标识符与文件名、环境变量、数据文件与测试断言一律不动——它们是被运行时或守卫消费的键，改了会直接坏。

### Notebook 系列扩充与源码覆盖补全（2026-10-22）

- **[新增] 边界与增长三册**：`06 接口层：中间件、鉴权与安全防线`、`07 稳定性、性能与降级`、`08 多智能体、技能、Hooks、事件与自进化`，把 00–05 未覆盖的 `interfaces/`、`security/`、`reliability/`、`performance/`、`orchestration/strategies/multi_agent/`、`agent/{skills,hooks,events,evolution}/` 纳入可执行导读。
- **[补充] 00–05 缺失源码与机制**：01 补 `MappingStepExecutor` 三条出口与 `LayeredVerifier` 三层短路；02 补 supervisor 后半段、general/KB/task 三个 handler 与有界查询改写；03 补选择层、有效风险、`ToolExecutor.execute` 两段执行链、结果限量与 MCP 协议/客户端的真实源码摘录；04 补 `MemoryManager`、`LongTermMemory` 与真实 `retrieve_hybrid_docs` 四阶段演示；05 补 `request_id/trace_id` ContextVar、JSON 日志、`request_scope` 并发累加器与评测显著性/成本全式；00 补与 06 册的中间件职责分工交叉引用。
- **[校验] 全量离线执行**：9 本共 472 个单元、其中 236 个代码单元全部执行成功，零报错、零 stderr；所有演示不依赖 API Key、Redis、PostgreSQL、Neo4j、Milvus。
- **[文档] 索引同步**：`docs/notebooks/README.md`、README §14 文档表、`docs/项目实现手册.md`、`docs/architecture/README.md` 统一为 9 本；各册页尾的系列导航补齐 06–08。

### 源码讲解 Notebook 系列（2026-10-21）

- **[新增] 可执行导读**：`docs/notebooks/` 落地 6 本中文讲解 notebook（00 平台全景与请求链 / 01 Agent Runtime 与执行循环 / 02 LangGraph 三层编排与路由 / 03 工具治理与 MCP / 04 记忆·上下文·混合检索 / 05 可观测·配置开关·工程质量），共 186 个单元、其中 94 个代码单元已全部执行且零报错零告警。讲解单元用 `show_file` / `show_symbol` 现场读取仓库源码，不粘贴代码副本，避免文档与实现漂移。
- **[新增] 离线运行前提**：全套演示不依赖 API Key、Redis、PostgreSQL、Neo4j、Milvus，需要外部服务的分支一律演示其降级路径；配置相关演示只解析 `.env` **键名**，绝不读取值，临时文件写在 `.tmp/` 下。
- **[新增] 内核搭建脚本**：`scripts/setup_notebook_kernel.py` 把 `ipykernel` 及依赖以 `--no-deps --target` 装到 `.tmp/notebook_tools` 并生成 `.tmp/jupyter/kernels/agentix/kernel.json`——`.venv` 与 `pyproject.toml` 依赖树不被学习工具污染；重复执行幂等。`.tmp/` 已被现有 `*.tmp` 规则忽略，`.gitignore` 无需改动。
- **[文档] 导航接入**：README §13 目录树与 §14 文档表、`docs/项目实现手册.md` §0.2 阅读路线与 §0.7 相关文档地图、`docs/architecture/README.md` §2 文档导航均指向 `docs/notebooks/README.md`；该索引说明阅读顺序、内核搭建与命令行重跑方式（含两条来自宿主环境、与内容无关的 zmq / TCP 告警）。学习材料不属于 `AGENTS.md` 的「改行为同步清单」，但其仓库路径引用受 `tests/test_p41_doc_consistency.py` 校验。

### 项目说明书去腐烂与结构重构（2026-10-19）

- **[文档] 去行号定位**：`docs/项目实现手册.md` 全文 426 处 `文件:行号`、343 处 `Lxx` 引用改为**符号级定位**（模块 / 类 / 函数 / 常量 / 开关名），并删除 15 张表的「行 / 行号」列。理由：行号随每次提交漂移、无守卫可守；符号名可被搜索与跳转。
- **[文档] 修正漂移计数**：按源码实测修正 13 处结构计数——检索域路由文件 16→15、`chat_service.py` 1697→1871 行、`chat_agent_tools.py` 346→399 行、工具执行器 617→672 行、`model_gateway.py` 183→232 行、`trajectory.py` 627→720 行等；`agentix/runtime/` 包组成补上 `control_plane.py`（7 个文件）。
- **[文档] 结构重构**：新增「按目标选阅读路线」「五组章节地图」「术语表（L0–L3 / M0–M6 / Phase H / NOT-ENFORCED 等）」「相关文档地图」，每章加一句「本章速览」；§2.4（Strangler shim）与 §16.7（release_gate）明确标注为**历史留档**，§21.7 的发布门禁指向 CI 现状。
- **[文档] 维护约定**：说明书开头写明「不带行号」的编辑约定、p41 守卫的覆盖边界，以及改行为时的同步面（与 `AGENTS.md` 一致）；README §14 注明 Word 图文版为 2026-10-10 快照、以 md 为准。

### 统一知识库问答与智能助手工作台

- **[前端] 统一聊天入口**：根路径进入单一聊天工作台；输入框的知识库开关按需切换检索域与 Agent 助手，不再使用页面级「知识库问答 / 智能助手」模式切换。
- **[前端] 按模式保留上传语义**：知识库开启时上传 PDF 并写入所选知识库、提交索引；关闭时通过助手附件接口上传本轮文件，并随 Agent 流式请求发送。知识库未选择时禁用知识库上传。
- **[文档] 会话与入库边界**：当前两种模式复用 UI，但仍使用不同后端 API 与服务端会话历史；助手回答直接保存为知识库文档尚未实现。

### 本地真实检索评测（2026-10-11）

- **[评测/文档] 数据条目语料真实对照**：在隔离索引中使用 LM Studio BGE-M3 与 Infinity Cross-Encoder 对 4,865 条数据条目、23 条 `kb-query` 金标运行 BM25/Dense/RRF/Z-score/Rerank 对照；Z-score α=0.75 + 10 个精排候选的 Recall@5 为 0.7826、MRR 为 0.7283。Recall@5 与 Dense 持平（18/23），仅观察到排序提升，不能外推为召回覆盖或通用准确率提升；README §15.1 记录指标、延迟口径和复现方法。代码默认 flag 保持不变。

### RAG 质量与评测路线（2026-10-10）

- **[性能] 混合检索双通道并发**：Dense 与 BM25 候选检索并发执行，融合与后续精排仍等待两路完成；不改变 flag-off 路径、通道过滤或索引快照隔离。真实 KB 环境下的端到端收益仍需基准测量。
- **[召回] 数据条目编号跨格式规范化**：BM25 将“第148条”和“第一百四十八条”等相同编号映射为统一词项，并避免原始编号字符片段制造相似数据条目误匹配；补充双向编号形式与相邻条号干扰回归。
- **[性能/可观测] BM25 倒排查询与精排降级状态**：BM25 改为按词项 posting 累加候选分数，不再遍历整个语料；rerank 对 HTTP 200 错误对象/无效结果显式告警并 fail-open，混合链阶段报告将 `applied` 标为 false。
- **[修复] 混合检索 BM25 索引快照一致性**：BM25 进程缓存从仅按 `kb_id` 改为 `(kb_id, artifact_id)`；生产 sparse 检索读取活动 manifest 内且 `ready` / `indexed_artifact_id` 匹配的 chunks，避免索引重建后 TTL 内融合旧语料，或提前混入未激活的新语料。补充跨 artifact 缓存与 manifest 状态回归。
- **[文档] RAG 质量优化计划**：README、项目说明书、项目规格、运行时护栏、RAG 设计与评估说明同步梳理分阶段路线：人工金标、Recall@K/MRR 阶段归因、CJK/元数据召回、RRF/Rerank 校准及入库/索引一致性。明确当前模板数据和未录制 live 基线不构成数据条目准确率证据。

### 前后端契约审计修复 + 死代码清理（2026-10-09）

- **[修复] 文档缩略图/阅读器默认 401（审计 B1）**：后端 `GET /documents/{id}/thumbnail|file` 均要求 Bearer 鉴权（`get_current_user`），而 `<img src>`/iframe 无法携带 Authorization 头、`AUTH_COOKIE_SESSION_FALLBACK` 默认关——默认配置下缩略图全破图、PDF 阅读器整页 401。前端改为**经 axios（自动带鉴权头）拉 blob → `URL.createObjectURL`** 渲染（`documents.ts` 新增 `getDocumentThumbnailUrl`/`getDocumentReaderUrl`/`releaseDocumentThumbnails`/`releaseDocumentReaderUrl`；`PersonalKbView.vue` 换用并在列表刷新/组件卸载时回收 object URL 防泄漏；PDF fragment 参数保留）。同步删除旧直连封装 `documentThumbnailUrl`。
- **[修复] 知识库文档列表超 20 条静默截断（审计 B2）**：后端分页默认 `page_size=20` 且前端不传分页、丢弃 `total`——第 21 个文档起列表/重命名/删除全部不可达。`listKbDocuments` 改按上限 100/页循环拉全（以 `total` 为停止条件，100 页防御上限）；folders 接口后端无分页无此问题。
- **[修复] cron_create 无界 prompt 可落盘（清理批次发现）**：`install_cron_tools` 导入了 `MAX_CRON_PROMPT_CHARS` 却从未参与校验（spec description 声称 ≤4000 但 Field 与处理器均无约束）——`_create` 补 prompt 长度校验（与 name 同款语义）+ `test_p58` 新增超限拒绝/边界通过用例。
- **[修复] 反馈提交补 trace_id（审计 B3）**：meta 帧 `request_id` 经 `useChatStream` 落到助手消息（`StreamableMessage`/`ChatMessage`/`KbModalMessage`/`ChatModalMessage` 四类型 + `useConversationChat` 投影同步）→ `ChatMessageList.sendFeedback` 携带，反馈回流评测管道恢复 trace 归因列。
- **[修复] chat 域 401 对齐全局语义（审计 B5）**：`chatStream.ts` 流式与 `ChatWidget.vue` 非流式在 401 时 `clearToken()` + 广播 `tracerag:unauthorized`（原为静默失败；`V1_REQUIRE_JWT` 默认关无症状，开启后不再踩坑）。
- **[文档] `VITE_API_BASE_URL` 构建期陷阱（审计 B4）**：README §10 补约束说明——该变量仅限本地开发直连，配绝对地址会打进构建产物、容器部署下绕过 nginx 同源反代必然连接失败。
- **[清理] 死代码与冗余（静态扫描确认 + 全量回归）**：后端 `ruff --select F401` 人工复核后删除 23 处确认未用导入（typing 标准库 10 + 测试残留 13；注册副作用类逐一排除保留）；F841 3 处处理（2 删 1 显式弃值——`test_p89` 的"触发模块加载"副作用保留语义）；前端删除旧签名残留 `uploadProgressStatusLabel`（生产单源为 `uploadTaskStatusLabel`，测试改测生产函数）+ 死类型 `ErrorCodeValue` + `VectorDbView` 冗余手动 multipart Content-Type 头。`clearToasts` 为测试清理钩子保留。

### 增量优化批次：安全自评 · 评测 runbook · fallback 跨端点 · 反馈闭环 · Ragas/promptfoo/LangSmith 引入（2026-10-09）

- **[新增] OWASP LLM Top 10（2025）对照自评文档**：`docs/security/大模型安全风险自评.md`——LLM01–LLM10 逐条三栏（风险 → 现有缓解含代码位置 → 残余风险），LLM03 供应链与 LLM07 系统提示泄露如实标注未专项覆盖；README §14 文档索引同步收录。
- **[文档] 模型快照固定纪律注释**：`.env.example` `LLM_MODEL` 上方新增注释——云 API 必须钉快照版本号、禁用 `latest`/浮动别名（理由：`--compare` A/B 与质量基线可比性依赖同模型快照）；LM Studio 本地模型名即快照无需处理。
- **[文档] 评测基线录制 runbook**：`docs/runbooks/评测基线录制.md`——8 步 PowerShell 操作版（启动基础设施 → `--record` → 提交 predictions → 离线复打分 → 五指标 `evals/quality_gate.py` 首跑建档 → **手动提交 quality.json** → 推送验证 CI 门禁升级）；`evals/README.md` §9 武装顺序处加链接。
- **[文档] LangSmith 占位（默认关）**：`.env.example` 新增 `LANGCHAIN_TRACING_V2=false` / `LANGSMITH_API_KEY=`（留空）/ `LANGSMITH_PROJECT=my-agentix` 三行注释（免费档 5000 traces/月、14 天保留、fail-open、只接开发评测流量）；README §9 接入说明（生效边界：LangGraph/LangChain runnable 自动上报，自建体系全保留）；`requirements.txt` 注释补一句启用指引。不新增 langsmith 依赖钉版（websockets>=15 冲突，既有决策维持）。
- **[新增] promptfoo 回归主入口（P1-5）**：新建 `promptfooconfig.yaml`（纯 CLI 模式：结果落本地不上云；provider 用自定义 Python provider 持久 worker 直调 ChatService——与 `run_eval --record` 同一调用缝）+ `evals/promptfoo_provider.py`（`call_api` → `get_chat_service().ask`，结构化结果 JSON 序列化、单条失败如实上报）+ `evals/promptfoo_asserts.py`（`get_assert` **直接 import run_eval 的 score_route/score_citations/score_reject/score_must_contain**——打分口径单源，守卫测试断言无实现副本）+ `evals/golden_to_promptfoo.py`（legal_golden → promptfoo tests 机械映射、取前 N 可复现，默认 25 条试点）+ `evals/promptfoo_tests.jsonl`（转换产物）。CI 新增 `promptfoo-eval` job 并入 `ci-gate` needs——**常驻只跑契约守卫 `tests/test_p104_promptfoo_contract.py`（12 条：yaml 可解析/引用面存在/转换产物=golden 子集/期望零加工/口径单源棘轮/get_assert 行为），全离线不跑在线 eval**（M-6 修正：CI 无 LLM，在线 eval 无意义）；README §8 补「如何复现评测」PowerShell 步骤。`npx promptfoo eval` 全量在线跑留待活环境（T11 验收的本地联调项，见报告未尽事项）。
- **[新增] compose web 前端 service（P2-3）**：新建 `web/Dockerfile`（node:22-alpine 构建 → nginx:alpine 托管 dist；构建上下文 `./web` 绕开根 .dockerignore 排除规则）+ `web/nginx.conf`（SPA fallback + `/api` 同源反代 `backend:8000`，`proxy_buffering off`/HTTP/1.1/拉长 read timeout 的 SSE 配置；同源方案 ⇒ 后端 CORS 零改动）+ `web/.dockerignore`。`docker-compose.yml` 新增 `web` service（第 13 个，`profiles: ["full"]` 隔离——默认 up 不启动，开发模式叙述不变；`VITE_API_BASE_URL` 按实测真名，M-4 修正）；README 快速开始补 `--profile full` 变体。`docker compose config` 校验通过。
- **[新增] ragas 忠实度报告（P2-2，observe-only）**：新 `evals/ragas_report.py`（沿 hybrid_eval.py 先例：输入 golden+predictions、报告键族固定 `ragas_faithfulness`/`ragas_answer_relevancy`/`ragas_questions`/`ragas_unavailable`、缺依赖/缺 judge 端点/缺输入一律 exit 2 诚实降级、不进 ci.yml METRICS 与 quality.json 门键）+ `tests/test_p106_ragas_report.py`（9 条：build_samples 纯函数、exit 2 降级、报告键与 CI METRICS 五键交集为空 + METRICS 元组零变化源级钉死）。**依赖实测结论（requirements-dev.txt 钉死组合）**：`ragas==0.4.3 + datasets==4.4.0 + pyarrow==21.0.0`——ragas 0.4.3 硬依赖 datasets≥4，其默认解析链（datasets 5.x→pyarrow≥24→pyarrow 26 起要求 numpy≥2）与运行时 `numpy<2.0.0` 硬冲突；本组合 numpy 1.26.4 下 `import ragas` 实测通过、`pip check` 零冲突。**pip-audit 实测（36 漏洞/15 包）**：既有包（langchain 家族/langsmith/black/pytest 等）为已记录存量；本批新引入且**在 numpy<2 约束下无可达修复版**的余留：datasets 4.4.0（PYSEC-2026-3716，修复版 5.0.1 需 numpy≥2）、pyarrow 21.0.0（PYSEC-2026-113，修复版 23.0.1 同前）、ragas 0.4.3（PYSEC-2026-3046，暂无修复版）、diskcache 5.6.3（PYSEC-2026-2447，暂无修复版）——四者均为 dev 依赖（生产镜像只装 requirements.txt 不装 requirements-dev.txt），ragas 链路 observe-only 不接生产数据面；升级 numpy 后可解除。
- **[新增] 满意度反馈闭环（P2-1）**：新 flag `FEEDBACK_ENABLED`（默认关——flag-off 路由不注册=与基线等价）+ `FEEDBACK_PII_REDACT`（默认开）。后端新增 `agentix/interfaces/http/v1/feedback.py`（`POST /api/v1/feedback`：用户面鉴权 `require_v1_user` + `resolve_v1_caller` + 会话归属校验跨用户 404；事件 `message_feedback` 落 EventPort 三后端；reason 过 `redact_pii`；发布失败 fail-open 回 503 不影响主链路）并注册进 v1 聚合点；新指标 `feedback_total{rating}`（label `rating` 有界 up|down，已入 p100 白名单）。前端新增 `web/src/api/feedback.ts`（fetch 封装、404/网络异常静默失败）+ `ChatMessageList.vue` 消息操作区 👍/👎（`feedbackEnabled`+`sessionId` prop 控制，默认零渲染、一票一消息、失败回滚可重试）。**接线（自动发现）**：`GET /api/v1/feedback/status` 探活端点（挂在同一 flag 门 router 内，flag-off 即 404）+ `probeFeedbackEnabled()`（模块级 memo 只缓存成功，404/网络异常 fail-open 不缓存）+ 组件挂载自动探活（`feedbackActive = feedbackEnabled prop || 探活结果`；prop=true 跳过探活）——装配方无需手工传 prop，后端开 flag 后前端自动出现反馈按钮。导出脚本 `evals/feedback_export.py`：EventPort `message_feedback`(down) → `feedback_ingest.py` 输入格式（`error_code=user_feedback_down` 表达"用户判定的质量失败"，不伪造 `status=error`；question/route_type 留空交人工补全）。测试 `tests/test_p105_feedback_loop.py`（19 条）+ vitest `ChatMessageList.feedback.test.ts`（4 条）。
- **[文档] 性能与成本采数 runbook + README 骨架**：`docs/runbooks/性能与成本采数.md`——TTFT P50/P95（`agent_ttft_seconds` 直方图 PromQL）、单请求平均 token（`llm_tokens_total` 聚合）、成本 A/B（`LG_TOKEN_HISTORY_BUDGET` 两轮 `--record` + `--compare`）全 PowerShell 步骤；README 新增 §15「性能与成本（待实测）」（数字全部 `<待实测>` 占位 + 测量方法）与 §16「已知限制」（集中收录散落诚实标注）。
- **[新增] fallback 跨 base_url 适配层（P1-1）**：新 Settings 键 `LLM_FALLBACK_MODEL_SPECS`（默认空 = 基线等价）——逗号分隔 `model|base_url|api_key_env` 三元组（`|` 分隔因 base_url 含 `://`；`api_key_env` 是环境变量名，密钥不落配置值、不与主 `LLM_API_KEY` 复用；`base_url`/`api_key_env` 段可空 = 仅换模型名回退主端点）。`model_gateway.py` 新增 `FallbackSpec` dataclass + `fallback_specs()`（解析语义与 `fallback_chain` 一致：去空段/保序/去重/剔除 primary；旧键旧语义零变化，specs 非空时消费点优先）；`chat_agent_tools.py` `_build_fallback_client` 按条目分支（跨端点直接构造 `ChatOpenAI`，仅模型名走 `create_chat_llm` 同构路径）。打点零新增（复用 `gateway_fallback_total{primary_model,fallback_model}`）。五处同步齐（settings/selfcheck/.env.example/configuration.md §5/p41 清单）。新测试 `tests/test_p103_fallback_specs.py`（10 条：解析去重/剔除 primary/空段、specs 优先旧键、两键皆空基线等价、api_key_env 缺失不抛错、消费点源级棘轮、五处同步棘轮）。生效面诚实边界：fallback 只接线检索域 `query_knowledge_base` 工具链路。
- **[文档] 降级演练 runbook**：`docs/runbooks/LLM降级演练.md`——8 步 PowerShell 演练（配置三元组 → 停主端点 → 发 KB 检索请求 → 断言响应非主模型 + `gateway_fallback_total` 增长 → 恢复回滚），写明"必须带知识库检索才触发降级"的诚实边界。

### L2 生产链路上下文工程补齐：Prompt Caching 可观测 · 指令分层 · token 级历史窗口 · 状态尾注（2026-10-09）

> 背景：优化规划按投入产出排序的 A/B/C/D 四阶段——把已建好的上下文基础设施（指令级联、token 计数、预算台账）真正接进 L2 生产主链路，并补齐缓存命中率可观测。**三个新 flag 默认全关，flag-off 路径与基线逐位等价。**

- **[新增] A·Prompt Caching 可观测与纪律**：①general 角色的前缀指纹改取**模板常量**——`llm_factory` 新增 `_TEMPLATE_PREFIX_ROLES`，`general` 正文含 memory/指令动态注入不再逐请求上报 `prompt_prefix_fingerprint` 抖动噪声，模板变更仍是漂移信号（`router/planner/tool/summarizer/hallucination` 静态角色行为不变）；②provider 缓存命中量采集——`_TrackedChatOpenAI.ainvoke` 读 `usage` 的 `input_token_details.cache_read`（langchain 归一）与 `prompt_tokens_details.cached_tokens`（OpenAI 原生）双形态，`record_llm_tokens` 新增 `label_type` 显式覆盖参数落 `llm_tokens_total{type="cached"}`，命中/未命中打 `cache_{hits,misses}_total{cache_type="prompt_prefix"}`——**字段缺失（供应商不支持）不记账**，不把「不支持」误算成「未命中」；标签 `type`/`cache_type` 均在 p100 白名单内，零新标签名
- **[新增] B·指令分层接入 L2 生产链路**：新 flag `LG_INSTRUCTIONS_ENABLED`（默认 `False`）——`chat_service._maybe_instructions` 组装级联指令块（复用 `build_instructions`：settings → skills → 全局 → 项目 → 子目录，4k/12k 预算 + hash 去重），经 `configurable["instructions"]` 透传，`general_handler` 按「静态 system 常量 → 指令块 → memory_context」顺序拼接（越稳定越靠前，保护 KV Cache）；**进程级指纹缓存**（`_INSTRUCTIONS_CACHE`：相关 flag + 约定文件 stat + 触达目录任一变化才重读盘，读盘经 `asyncio.to_thread`，故障 fail-open 返回空串）；流式/非流式两路同写；新建仓库根 `AGENTS.md`（项目层指令来源 + 协作约定，约 50 行）
- **[新增] C·token 级历史窗口**：新 flag `LG_TOKEN_HISTORY_BUDGET`（默认 `0`）——`create_message_window_node` 在条数窗口（`MAX_HISTORY_MESSAGES`）之内再按 token 预算从最新消息**向前一次性累加** `count_tokens`（每条只计一次，无逐条重算的 O(n²)），超预算即停、至少保留最新一条（本轮输入不可裁）；主图与 supervisor 图同一工厂自动生效；`0` = 该分支完全不执行，与旧条数窗口逐位等价
- **[新增] D·状态尾注（Manus 式）**：新 flag `LG_STATUS_TAIL_ENABLED`（默认 `False`）——`general_handler._status_tail` 把「对话轮次 / 历史条数 / 预算余量（`BudgetLedger.snapshot_lines`，仅非流式 ask 有 ledger 时）」追加到**本轮 human 消息尾部**（`model_copy` 副本，瞬态只进本次 ainvoke，不写回 checkpointer 历史、绝不进静态前缀）；已知边界：流式路径无 ledger ⇒ 预算段缺席，轮次/历史量两段两路径一致
- **[变更] `record_llm_tokens`**：新增可选 `label_type` 参数（缺省仍按 direction 推导 `prompt`/`completion`），既有调用点行为不变
- **[文档] 同步面**：`README.md`（§3 架构 + §11 测试真源数）、`docs/项目实现手册.md`（§3.4 开关表 + §5.9.2 L2 消费 + §8.1 注入点）、`docs/architecture/配置管理.md` §5（3 新键）、`docs/architecture/运行时护栏.md` §13（生产守护测试）、`docs/源码研读笔记.md` P3·3.3（缓存断点纪律勾选）、`.env.example`（3 新键）、`agentix/config/selfcheck.py`（合法省略清单）、`tests/test_p41_doc_consistency.py`（手写清单）
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
- **[修复] 文档死链与过期命令**：`docs/版本管理策略.md` 4 条悬空相对链接（根目录文件按同级链接、缺 `../`）；`docs/guides/快速启动.md` 3 条从未存在的引用（`INTEGRATION_SUMMARY.md`/`INTEGRATION_VERIFICATION.md`/`CLAUDE.md`）+ `server`/`milvus-standalone` 两个 compose 中不存在的服务名 + 补 8100/15432/9000/3000 四个端口
- **[移除] 两处死代码**：`agentix/retrieval/settings.py` 的 `CORS_ORIGINS` 属性（默认含全仓零引用的历史端口 **23416**，真正生效的白名单是 `config/settings.py::cors_origins_list`）；`text2sql/sql_execution/node.py` 的 `_map_db_type_to_driver`（把 postgresql 映射到 `postgresql+psycopg2`，而声明只有 `psycopg[binary]`，全新安装会 `NoSuchModuleError`；全仓无调用者）
- **[修复] 凭据回退名补进 example**：`GLM_API_KEY` / `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `VITE_PORT` 代码都会读（`DEEPSEEK_*` 设了还会**覆盖**主配置），根 `.env.example` 与 `kb_ingest/.env.example` 此前一概未提
- **[修复] `Makefile` `init-data`** 死目标 `data/chroma`（chroma 早非向量后端，R3.7 起为 Milvus）→ 建 `uploads`/`data/tiktoken_cache`/`data/lightrag`/`data/indexes`/`data/kb`；补 `.PHONY` 与 `help` 条目；`.gitignore` 补 `data/indexes/`；`docs/项目实现手册.md` 行号与内容同步
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
**W1 · LLM 优化**：路由器确定性 fast-path（寒暄/数据条目直答免 LLM，`llm_calls_saved_total{reason}` 计数）、角色档位路由（`LLM_TIER_ROUTING_ENABLED`）、终答验证三旋钮预算、上下文段级配额 + `obs://` 外置引用、`prompt_prefix_fingerprint{builder}` 前缀稳定观测；新 flag ×7（含 `VERIFICATION_TOKEN_BUDGET` 执行中增补）。**用户可感知变化**：简单问题首字更快、验证不再无限烧 token。
**W2 · 检索优化**：singleflight 请求合流、分级升级级联（`RETRIEVAL_ADAPTIVE_ENABLED`）、rerank 高分动态跳过、Neo4j Cypher 成本护栏、Milvus nprobe 配置化、缓存键租户+融合指纹升级（旧键 600s 冷却）；新 flag 统一域 2 + 检索域 4。**用户可感知变化**：并发同问不再重复打检索链路，图查询风险被前置拦截。
**W3 · 运行时治理**：三态准入（30s 有界排队，超限 429）、跨层共享重试预算桶、工具超时与剩余预算取 min、七级负载阶梯甩载、四池隔离舱（检索/Neo4j/MCP/WebSearch）、共享 HTTP 连接池（`SHARED_HTTP_MAX_CONNECTIONS`，rerank/Redis 出口接线）；新 flag ×12。**用户可感知变化**：过载时平台礼貌拒绝而非雪崩，LLM/检索不再互相拖死。
**W4 · 持久化 + SSE 后端**：Redis Stream 事件批写（`EVENT_BATCH_MAX_SIZE`，默认 0=关——即时可见契约优先）、轨迹异步写、SSE 到期检查式合并 + producer 有界队列背压（`SSE_COALESCE_ENABLED`，`sse_dropped_frames_total{reason}` 计数、终态帧永不丢）；新 flag ×5。**用户可感知变化**：高扇出流式场景服务端 CPU 显著下降，帧合并窗口内呈现无感知。
**W5 · 多智能体**：Peer 结果契约面（`SUBAGENT_RESULT_CONTRACT_ENABLED`，证据条目外置、契约面过限整体外置可取回）、顺序拓扑早停（`MULTIAGENT_EARLY_EXIT_ENABLED`，跳过 reviewer 与后续轮次）；新 flag ×2。**用户可感知变化**：多智能体收敛轮次减少、token 消耗下降。
**W6 · 前端感知性能与实时动态 UI**（`web/`）：50ms 批窗流式缓冲（`useStreamBuffer`，渲染次数降至 ~20/s）、智能吸底与回底胶囊（`scrollBehavior`）、marked+DOMPurify 消毒渲染、StreamStepper 流式阶段步进器、工具执行卡/耗时芯片/复制+重新生成、useToast 全局 toast（替换 3 处 alert）、停止/重新生成全链（ChatPanel/Home/History/PersonalKb/KbChatModal/PdfReader）；vitest 126 → **198**。**用户可感知变化**：长答案滚动不再抖动、Markdown 即时渲染、流式进度与工具状态可视化、可随时停止/重问。
**W7 · 观测收口 + 全文档同步 + 交付**：OTel GenAI 语义属性（`genai_span` 网关 `chat {model}` + `gen_ai.*` 请求/用量属性，接入 `invoke_with_breaker` 与 `_TrackedChatOpenAI.ainvoke` 两 LLM 出口）+ Prometheus 指标标签基数白名单守卫（29 项有界白名单 + 高基数身份标签禁入）；`web/package.json` 补 `lint` 脚本（修 ci-gate 必红）；perf-smoke 从 `continue-on-error` 转 **CI 阻塞门禁**；新增 `tests/test_p100_genai_observability.py`。

**Phase H 汇总**：后端测试 3908 → **4198**（+290，六个新测试模块 p92–p99 + p100 + perf_smoke），前端 vitest 126 → 198；新 flag **35 枚全部默认关/基线等价**（五处同步完成，检索域 4 键沿 R-P1 纪律两处同步）；新模块 `agentix/performance/`、`agentix/infrastructure/{cache,http}/`、`agentix/reliability/{retry_budget,bulkhead}.py`、`agentix/orchestration/strategies/multi_agent/contract.py`、`agentix/application/services/stream_gate.py`、`evals/performance/`、`web/src/modules/knowledge/{composables,utils,components}/` 六新前端文件；设计文档 `docs/design/前端流式渲染优化.md`。

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
- **[文档] 交付前同步**：SECURITY.md 安全设计表补「SQL 注入护栏」「弱哈希治理」两行 + 新增「依赖安全现状」节（pip-audit 21 残余口径 / bandit medium+ 0 / 双声明同步纪律）；`docs/migration/现有目录树.md` tests 179 → **181** 模块（+p91、route_introspect，口径 = 顶层 .py 不含 conftest）；`.gitignore` 补 `*.db-shm` / `*.db-wal`（SQLite 运行时伴生文件不入库）
- **[版本] 0.1.2 → 0.1.3（PATCH，VERSIONING「安全修复」递增位）**：`scripts/bump_version.py 0.1.3` 同步四处真源（pyproject / `agentix.__init__` / `web/package.json` / `.env.example APP_VERSION`），`--check` 一致；文档字面量同步——README 版本徽章与 FastAPI 徽章（0.109→0.142）、PROJECT_SPEC §1.2 版本真源行/包头行、VERSIONING 示意图、TECH_STACK 示例
- **[文档] PROJECT_SPEC §1.2 技术栈表实况对齐**：修 5 处陈旧——`fastapi 0.109.0 → 0.142.2`（行内补 `starlette==1.7.0`）、`langchain 0.3.7 → 0.3.30`、`langchain-community 0.3.4 → 0.3.27`、`langchain-core>=0.3.39 → >=0.3.85`（行内补 `langchain-neo4j==0.3.0`）、全部 `pyproject.toml:` 出处行号对齐现文件（Web 68–70 / 校验 71–72 / LLM 80–84 / 图编排 89–91 / pythonpath 306）；`docs/源码研读笔记.md` FastAPI 0.109 → 0.142；统一执行链方案 v2 执行状态行 `p61–p90 → p61–p91`（+p91 注记）
- 回归：全量 **3908 passed / 15 skipped / 0 failed**（2026-10-04 实测）；`ruff check .` 全绿；`mypy agentix/domain` 0 错（35 files）；覆盖率 **73.45%** ≥ 60；bandit medium+ 0；web vitest **126** + `vue-tsc` 0 错；pip-audit 残余 21 项已记录

### 文档守卫盲区扩展 · 旧版孤儿清理 · 性能优化路线图（2026-10-04）

- **[测试] p41 覆盖盲区扩展（33 → 37 条）**：扫描集并入根目录当前态文档（README / SECURITY / CONTRIBUTING / CODE_OF_CONDUCT / `evals/README.md`；CHANGELOG 历史记录刻意除外）与说明书 `docs/*.docx`（stdlib zipfile 逐段抽取——`w:tab`/`w:br`/段边界保留成分隔符防假粘连，首尾 lookahead 边界防 `legal_golden.jsonl` 被抽成 `.json` 假匹配）；路径前缀补 `docs`/`config`/`.github`/`skills`、扩展名补 `vue/ts/js/mjs/mermaid/png/csv`；新增有意留档豁免 `DOCX_INTENTIONAL_REFS`（≤5，当前 1 = release_gate 删除面留档句）+ 根文档贡献/docx 存在性/docx 防空真/豁免上限 4 条守卫
- **[文档] 新守卫当场拦截 4 处 `config/` 简写腐烂**：`docs/architecture/README.md`（单一事实来源行）、`docs/architecture/分层架构与依赖规则.md`（§4 横切表整列改全路径）、`docs/migration/模块清单.md` ×2 → 一律 `agentix/config/{settings,ablation,selfcheck}.py`
- **[文档] 删旧版孤儿 `docs/design/TEXT2SQL_IMPLEMENTATION.md`**（零外部引用；text2sql 现状以 `docs/项目实现手册.md` 相关章节 + `docs/design/Wren语义层集成.md` + `agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/text2sql/` 代码为准；保守清理范围——仅删真正零引用孤儿，被 README/CHANGELOG/测试引用的历史记录一律保留）
- **[文档] 新增 `docs/design/性能与优化路线图.md`（性能与优化路线图总纲）**：§1 五层现有性能功能盘点 18 项（LLM 调用/编排/上下文/检索/负载帽，每项锚定真实代码符号与 settings 行号 + 关闭回退行为）；§2 新可扩展优化点 E1–E8——P0：轮内并发灰度、schema TTL 生产化、HTTP 出口连接池化 → P1：embedding 缓存观测与预热、最小延迟基准、prompt 静态前缀稳定化 → P2：语义结果缓存、出口连接池全景审计（每条含现状→方案与方法实现→可扩展方式→验收→风险回退，拟增项均标「拟增」）；§3 分域深钻索引（rag/memory/multiagent-optimization）；§4 分期依赖链 E8→E3→(E1∥E2∥E6)→E4→E5→E7；§5 守卫口径（诚实声明「无专用性能基准」，速度声明须带测量日期+环境）
- **[文档] README §11 真源**：3854 → **3858 passed**（+4 = p41 盲区扩展入链，2026-10-04 实测）
- 回归：全量 **3858 passed / 15 skipped / 0 failed**（2026-10-04 实测）；`ruff check .` 全绿；db-shm/wal 未入

### 文档缺口修复 · 说明书(docx)终态同步（2026-10-04）

- **[文档] md 真缺口 3 处**：SECURITY.md XSS 行 `web/src/components/ChatWidget.vue` → `web/src/modules/chat/components/ChatWidget.vue`；design-legal-domain 两处 `docs/{class,sequence}-diagram.mermaid` → `docs/design/`；其余扫描命中复裁为非缺口（PROJECT_SPEC 迁移出处表 / ADR-0002 已有终态勘误 / ADR-0003 背景更新注记 / `data/cron/jobs.json` 运行时产物 / hermes·wren·archive 外部引用 / `已不存在` 否定句）
- **[文档] 说明书 `docs/My-Agentix项目说明书.docx` 终态同步（原稿 2026-09-30，3395 → 3398 段）**：头部插入「终态同步修订」3 段（迁移映射 + 删除面 + 行号基线声明）；49 段级覆写（AgentLoop 类型表 → `orchestrator.py:32/46/88/94`、`__init__` 契约补 `context_budget_tokens/round_concurrency`、`classify_exception` → `executor.py:115-137`、`get_active_graph` → `lg_builder.py:396-408`、Run API 两段 → `interfaces/http/v1/runs.py` + `/api/v1/agents/runs` + `require_run_bearer`、shim 三行表头/行文 → M6 已删留档、`release_gate` 段 → M5 已删（现门禁 `evals/quality_gate.py`+`release.yml`）、kg single_agent chart/text2cypher 拓扑两行 → M5 已删 + components 现状、ToolExecutor `471→617 行`、loop `290→550 行`、hitl_bridge `640→642 行`、tools 目录表 → `tools/`+`gateway/`+`adapters/` 布局、`settings.py` 函数/属性错行 4 处 `:935→:1287`、`:22→:23`、`:651→:952`、`:870→:1188`）；95 段原位替换保格式（33 条旧路径映射全量 repoint + `settings.py:NNN` 按相邻 KEY 重锚 57 处 + `KEY（:NN）` 2 处 + AgentLoop 段短 `loop.py` → `orchestrator.py` 25 段）；验证 = python-docx 打开 OK + XML 合法 + 路径扫描 missing 归零（唯 `reliability/release_gate.py` 留档句有意保留）
- 回归：p41+p40+p17 = **69 passed**；`ruff check .` 全绿（db-shm/wal 未入）

### 文档细节完善 · 全仓「文档与终态一致性」66 条审计修复（2026-10-04）

- **[文档] README（6 条）**：Chat 示例 `question` → `message`（`ChatRequest` 实况字段）×2 + 删已不存在的「发布门禁」引、`GET /api/v1/ops/events`（旧 `GET /api/v1/events`）、tools 目录树注释改 `definitions.py / registry_init.py / selection.py` 单一注册面、multiagent 测试路径 → `orchestration/strategies/multi_agent/`
- **[文档] PROJECT_SPEC（24+ 条）**：§5 编排段整体改 `orchestration/orchestrator.py`（550 行、run docstring:142、`_run_state:88`、while:196、budget:197-206、回调窗:210-221、终态判定:313-333）+ §5.1 表行号（executor classify :266-277、StepExecutor :32、LoopResult :46）+ 主循环 15 行 loop.py → orchestrator.py 行号映射 + §6.4 事件表 → orchestrator emit 真源（`ON_STEP_END:282` / `ON_RUN_END:152` / `ON_ERROR:166,183,277,301`）+ §7 十组（`build_graph:329-361`、9 节点 :343-356、`get_active_graph:396-408`、supervisor 428-444、route_query 7 值 Literal、settings 键行号 9 处）+ 全文 `settings.py:NNN` 29 处核对清零（含 L2244 secret 段 → `resolved_secret_key:1201`、L2278/2354 KB 键 → settings:1022 起）+ §12 domain 44 → 30 .py、删 `mcp/` `infrastructure/{knowledge,tools}` 行 + §13 异常处理 `main.py:690-728`、`app:741`、v1 `:21-26` + 文件行数 7 处
- **[文档] architecture（15 条）**：README「终态顶层包归属」11 包表 + 代码分层地图整树重绘、分层架构与依赖规则.md 决策树与代表文件（domain 无 models/、infra 删 knowledge/tools）、runtime-guardrails「result_limiter 未接线」→ 已接线（`HITL_IN_LOOP_ENABLED:828` + `POST /api/v1/ops/hitl/in-loop`）、extension 绝对导入 `kg_tools_list`、interfaces 默认实现三分拆、configuration `GET /api/v1/ops/events`；`generate_architecture_diagram.py:219` 改 orchestrator 路径并重生成两 PNG
- **[文档] guides（16 条）**：QUICK_START 检索 curl → kb_ingest :8100 + X-API-Key、「常见操作/端点速查」按 kb_ingest 5 端点（`/ingest/excel[/upload]` `/ingest/mysql` `/search` `/search/hybrid`）与 `DELETE /api/v1/chat/session/{id}` 重写（KG/GraphRAG 标注「无独立 HTTP 端点」）；crawler_guide 整文件重写；TECH_STACK_GUIDE `knowledge_service/vector_store/reranker/embeddings/kg_neo4j_conn` 等 11 处文件名 → 现行路径（`retrieval/infra/*`、`kb_ingest/kb_service/services/*`、`kg_sub_graph/`）+ `knowledge_router` → `interfaces/http/v1/*`；testing L5 → `scripts/e2e/e2e_phase4.mjs`；routing quick-ref 死链 → `docs/guides/测试分层策略.md`；lightrag 语料 → `data/legal/legal_lightrag_corpus.json`
- **[文档] design（7 条）**：design-legal-domain M14/M15 前端路径 → `web/src/modules/chat/components/*`、M17 `data/kb/data.txt` → `legal_corpus.txt`、L3 工具三行全路径 + 旧 `knowledge_service` 已归档注；orchestration-rationale `<A>/lg_builder` → `orchestration/langgraph/lg_builder.py` 全量行号重核（A1/A2/A5/A6、附录 B router 161/160-164/170-184、utils.py 七段、summarize「generate_summary 死链已清」、validation 28-135）；rag-optimization 发布门禁 → `release.yml` + `evals/quality_gate.py`；phase4 前端 e2e 路径 → `scripts/e2e/`
- **[测试] p41 拦截 1 例**：crawler_guide 引用不存在的 `scripts/import_provisions.py` → 改文字表述（路径白名单机制正常工作）
- 回归：全量 **3854 passed / 15 skipped / 0 failed**（2026-10-04 实测，基线不动）；`ruff check .` 全绿；p41+p40+p17 = 69 passed；提交 `5b87532`

### Stage T · 统一执行链：终态收口（evals 报告键 + 全套文档终态化）（2026-10-04）

- **[新增] evals 退出契约报告键（总纲 §6 Multi-Agent 四键；只增报告键、零门禁接触）**：`run_eval` 新独立聚合 `_aggregate_multi_agent_exit`（与老聚合 `_aggregate_multi_agent` 分离——p88 零接触断言原样）产出 ① `multi_agent_exit_reason_dist`（按 `domain/runtime/lifecycle.py` 单源四值 `ExitReason` 计数）② `multi_agent_budget_exhaust_rate`（`max_iterations` 占比）③ `multi_agent_cancel_terminal_applicable` + `multi_agent_cancel_terminal_rate`（口径：分母 = `exit_reason=="interrupted"` 取消样本，分子 = status ∈ 明确终态集合；离线无取消样本 ⇒ `None`/`False` 诚实显示「—」不造假，运行时取消终态由 p61 断言）；meta 缺 `exit_reason` ⇒ 整组键缺席（「没有」不读成 0）；`evaluate()` 接线 + CI `METRICS` 五键门零交集（p90 源级断言）
- **[文档] README §11 真源**：基线 3838 → **3854**（2026-10-04 实测）+ Stage T 段入链（+16 = p90）
- **[文档] README 架构段终态重绘（T2）**：§3 新增「终态统一执行链（Stage A/B/M 收敛后）」ASCII（Run 入口 → 唯一循环 → Runtime → Tool Gateway → Capabilities → Infra）+ 运行时内核表落地路径（Agent Core 2.0 → `orchestration`+`policies`+`agent`+`tools`；MCP → `capabilities/mcp`）+ §4 导语（M2/M3 终态改写、p66 10 文件白名单棘轮）+ 端口契约 → `domain/agent/ports.py` + 依赖边界（`application/runtime` shim 已删）+ `services/` → `application/services/` + MCP Server 命令两处 → `python -m agentix.capabilities.mcp.server` + L1 路由 7 出口补 `text2sql` 与 `task-query` flag 注记 + §12 项目结构树全量重绘（+`orchestration/` `policies/` `tools/`，domain 五子包，删 `mcp/`、`infrastructure/tools`、`infrastructure/knowledge`、`application/runtime` 三簇）
- **[文档] PROJECT_SPEC 终态修订（T3）**：§0.2 SSE 双链路措辞（词汇已归一 type+seq/run_id A5/R5b，帧类型集合仍不同）+ §4.1 domain 文件职责表（35 .py、M0 拆入 `agent/` `runtime/` `tool/` 三子包，删 `agent_types.py`/`ports/agent_core.py` 行）+ 七端口引用三处 → `domain/agent/ports.py`（PlannerPort:53 / MemoryPort:92 / EventPort）+ §8 服务分组表（`control_plane`→M4 runtime、`hitl_bridge`→M3 policies、`llm_factory`→agents 已迁标注）+ §8.9 `hitl_bridge` 行标 M3 已迁 + §14.1 `agentix/mcp` 四转发壳 M6 删除 + 入口命令改写 + §22.2「帧类型集合」+ 对照表编排行 → capability 内部工具循环（M5 已剥离 langgraph ReAct）
- **[文档] 对照矩阵与 DoD**：v2 §5 升级「结构迁移对照矩阵（终态实测 2026-10-04）」+ 逐行新增第 4 列「终态状态」全 ✅（M0–M6/A4/A5 含证据引用）+ §4 执行状态行 + §9 DoD 五项全部勾选；总纲 §3 三方向状态 → ✅ 全部完成（唯 A-P2-3 按 D5 另期）+ §8 DoD 四项勾选 + 终态句
- **[文档] 现有目录树.md 全量刷新（T4b）**：数字同步 2026-10-04（`agentix/` 527 个 .py、`tests/` 179 模块、`.env.example` 264 活行）；仓库根/一级包/application/domain/infrastructure/retrieval 详树按终态重绘（+`orchestration` `policies` `tools` 一级包，删 `mcp/`，domain 三子包 + ports 再导出，infra 删 `knowledge/`+`tools/`）；§8 缺口清单补终态行（三簇 shim 清零 p66/p69 断言）
- **[文档] 模块清单.md 收口段（T4c）**：追加「统一执行链 Stage A/B/M/F/T」六条勾选（p61–p90 逐批映射 + 主题清单）+ 终态收口行（全量真源指 README §11、shim 空集、白名单 10 文件）
- **[文档] evals/README.md**：新增 §4.8（退出契约四键表 + 离线无取消口径 + 「只增报告键不进门禁」纪律）+ §5.4 输出注记补 §4.8
- **[测试] 新增 `tests/test_p90_eval_report_keys.py` 16 条**：dist 精确计数 / budget 率三态 / 取消口径四态（有样本有终态 / 有样本无终态 / 无样本 None+False / 有样本全终态=1.0）/ 老预测缺键整组缺席 / `evaluate()` 接线有 meta 出键·无 meta 正交 / 真实 golden 空预测键组缺席 / CI `METRICS` 与新键零交集源级 / 老聚合函数无新键回归
- 回归：全量 **3838 → 3854 passed / 15 skipped / 0 failed**（2026-10-04，`ruff check .` 全绿，p41 33 条全绿）

### Stage F2 · F2c-b 统一执行链：Multi-Agent 子代理隔离三件套（A-P2-1）（2026-10-03）

- **[开关] `MULTIAGENT_SUBAGENT_ISOLATION_ENABLED` 默认 false（五处同步：settings Field / selfcheck / `.env.example` 活行 / 配置管理.md 表行 / p41 手写清单）**：flag-off supervisor 现行为逐位等价（handler 收原 state/config 对象、meta 无 `isolation` 键、Manager 不收 `budgets`）
- **[新增] `orchestration/strategies/multi_agent/isolation.py`（纯基础设施，零 I/O 零 LLM）**：① `IsolationPolicy`（frozen）+ `build_task_face_prompt`——ephemeral 恒不拼父轨迹，只读快照段按 `memory_snapshot_readonly` 注入；② `take_memory_snapshot`——str 原样暂停、Mapping 深拷贝后 `MappingProxyType`（写入 TypeError，真只读非口头）、空值/未知类型 → None；③ `SubAgentSession`（session_id + 独立 `PeerBudget` 副本 + state/心跳/steering），`session_budgets` 产出 `{actor: budget}` 经 Manager `budgets` 单源注入；④ `LiveSubagentRegistry`——register/get/by_actor、`heartbeat`/`is_alive(ttl, at)` 时间可注入、pause/resume/kill（kill 终态幂等且审计保留，heartbeat/steer/resume 对其失效）、steer/take_steering 一次性、`status()` counts+records；⑤ `wait_while_paused`——resume 继续 / kill 短路 / 超时 `max_pause_wait_seconds` fail-open 放行（绝不悬挂）；⑥ `ephemeral_task_face_state`（浅拷贝 + messages 收敛最后一条，router 等任务态字段共享）+ `isolated_config`（摘除活 `memory_context` = skip_memory，只读快照改 `memory_snapshot` 键，原 config 不就地修改）；⑦ `session_gate`（心跳→pause 有界等待→kill 短路返回 `session=killed` PeerResult）+ `wrap_peer`（包装内层：steer 注入 `[steering]` 段+`steered` 标记、快照段追加、内层零执行）
- **[接线] `lg_supervisor`（仅 flag-on 分支）**：handler 子 agent 收任务面 state/config；Proposer/Reviewer 各持会话经 `wrap_peer` 包装（心跳在被调即刷新）；`_new_isolation` 组装 registry+双会话（budget 走 `_budget_factory` settings 单源镜像）；meta 仅 flag-on 追加 `isolation` = policy/sessions/snapshot_chars/registry 四键；模块与 `supervisor_dispatch` docstring 同步诚实化
- **[文档] 多智能体深度优化.md** 状态行 A-P2-1 移入已完成（待实施清零，余 A-P2-3 按 D5 另期）+ §6 A-P2-1 完整注记；配置管理.md 新增键表行
- **[测试] 新增 `tests/test_p89_subagent_isolation.py` 47 条**：policy 默认与 to_dict / 快照五态（str·空值三态·Mapping 暂停写入抛·未知类型·frozen 抛 `FrozenInstanceError`）/ 任务面 prompt 三态（ephemeral 丢父轨迹·非 ephemeral 含·快照段按 policy 门控）/ registry 九态（注册查找·TTL 时间旅行·pause-resume·kill 终态幂等审计·steer 一次消费·status 计数与字段·budgets 映射与独立副本）/ `wait_while_paused` 四态（直通·resume 解除·kill 解除·超时 fail-open·未知会话）/ 任务面 state/config 六态（截除·router 共享·空消息·摘记忆注入快照·原 config 未污染·非 dict 直通）/ `wrap_peer` 七态（直通+心跳刷新·steer 一次性+标记·快照段·kill 短路内层零执行·pause-resume 跑内层·pause 中 kill 短路）/ supervisor flag-off 两态逐位等价·flag-on 五态（任务面截除+摘记忆+快照键·isolation 块结构·无记忆零快照·budgets=会话预算·kill 闸·steer+心跳经 dispatch）/ 配置键四处同步
- 回归：全量 **3791 → 3838 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2c-a 统一执行链：Multi-Agent 退出契约 + 六失败模式打点（A-P2-2 余项）（2026-10-03）

- **[契约] `ManagerOutcome` 统一退出契约（无 flag——A-P0-3「先契约后功能」正确性修复）**：增 `exit_status/exit_reason/truncated` 三字段，`__post_init__` 按 `reason` 推导——新 helper `derive_exit_contract`：`converged`→completed/completed；`proposer|reviewer_budget_exhausted`/`max_rounds_exceeded`→failed/max_iterations（truncated=True）；`peer_cancelled:*`→interrupted/interrupted；`proposer_or_reviewer_not_wired`/`no_progress`/`contradiction_unresolved`/`proposer_accept_reviewer_reject`/深度超限/兜底→failed/error（ok=True 兜底 completed）；词表**单源复用** `domain/runtime/lifecycle.py` 的 `ExitStatus/ExitReason`（不另造平行词表），并强制契约等式 `truncated == (exit_reason == "max_iterations")` 按 reason 重算（显式传入也强制对齐）
- **[透传] `lg_supervisor` meta 增三键** `status/exit_reason/truncated`（成功与失败两分支都带）；Manager 异常路径（outcome=None）诚实 `failed/error/False` 不冒充完成态；`supervisor_dispatch` docstring 同步声明
- **[打点] `multiagent_failure_mode_total{mode}`**（`agentix/observability/metrics.py`）：prometheus Counter + 无依赖 `_NoopMetric` 双分支；`record_multiagent_failure(mode)`；`ManagerOutcome.__post_init__` 在 `failure_mode != NONE` 时恰打一次（含 `escalate` 深度超限路径），**NONE 不计**——成功收敛不进失败计数器
- **[评测面零接触]** `run_eval._aggregate_multi_agent` 只读既有键，meta 新增键不改变聚合结果（p88 断言双输入聚合相等）
- **[文档] 多智能体深度优化.md** 状态行 A-P2-2 移入已完成 + P0（A-P0-1/2/3/4/5）标注已随 v2 Stage A1/A2/A3 完成 + §6 A-P2-2 完整注记
- **[测试] 新增 `tests/test_p88_multiagent_exit_contract.py` 55 条**：词表单源两断言 / 推导表 13 行 × 枚举入值域（16 组合）/ 落章四态 + 契约等式全谱 + 显式字段不覆盖等式仍重算 / 打点三态（恰一次·NONE 不计·escalate 路径）+ not_wired 映射 + metrics 面存在 / supervisor meta 三态（收敛·未收敛·manager 异常）/ 评测聚合零接触 / docstring 三键在案
- 回归：全量 **3736 → 3791 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-e 统一执行链：RAG P2 混合检索归因报告（R-P2-5）（2026-10-03）

- **[新增] `evals/hybrid_eval.py` 独立归因报告脚本（先例 = `memory_eval.py`，只增报告键、零门禁接触）**：`Recall@5/MRR × {BM25, Dense, RRF, +Rerank}` 四列归因表——同问题集、同 `--search-k`、同匹配函数，唯一变量是通道，回答「哪条通道把期望数据条目召回到 top-5、排多前」；四通道接线 = `sparse_search` 直调（bm25）/ `retrieve_docs_with_retry` 直调（dense）/ `retrieve_hybrid_docs` 强制 sparse 参与融合 + `RETRIEVAL_HYBRID_RERANK=false`（rrf）/ 同链 rerank 开（+Rerank），flag 调用即翻、finally 即还原
- **[口径] silver 匹配复用 `run_eval` 同源解析器**：金标 `expected_sources`（如《示例数据集 A》第148条）与检索文档（正文 + metadata 干草）比对——数据集名先剥 `第X条` 再 `_extract_law` 归一（原函数为引用对引用设计、会吞条号段）后互相包含；条号统一整数按 `第X条` 三形匹配（`第148条` ≡ `第一百四十八条`，新写 `_int_to_cn` 0–9999 含空位补零 1005→一千零五）；解析失败退化子串（与 `_citation_matches` 同策略）
- **[口径] 分母与 fail-open**：仅金标非空题进分母（默认 `legal_golden.jsonl` 23 问 `kb-query` 小集，`--limit`/`--route` 可调，10–50 问带）；单题通道失败剔出该列分母，整列跑不通 = 表内 `n/a` + `unavailable` 诚实降级，绝不产出假数；`recall_at_5` = top-5 内首中题占比（rank>5 记未中），`mrr` = 首中位次倒数均值（全列表不截断），四舍五入 4 位
- **[门禁边界]** 报告键固定 `{columns, questions, table, unavailable}`，不进 `run_eval.evaluate()` summary / `CSV_FIELDS` / CI `METRICS` 五键门 / `quality_gate`（源级守卫钉死）；缺 `--kb-id`、`--search-k < 5`、索引 artifact 未就绪/索引文件缺失 ⇒ 诚实 exit 2，不降级出假表
- **[文档] evals/README.md** 目录树 + 新增 §4.7（匹配/分母/报告键/门禁边界）；检索增强深度优化.md 状态行 **P2×5 已全部完成** + §6 R-P2-5 完整注记
- **[测试] 新增 `tests/test_p87_hybrid_eval_report.py` 38 条**：`_int_to_cn` 十二例（含空位补零与超界退回）/ 匹配八态（数据集名 content·metadata 标题、条号三形互转、错条/错法不中、无数据集名退化子串、空文档）/ `first_rank` 四态 / 报告固定键集、指标数学与 rank6 口径、unavailable 诚实、四列 markdown 表 / 注入 runner 端到端（空金标不进分母、单题通道失败剔除）/ golden 小集装载（默认 23 问带 + 过滤/上限）/ CLI 诚实退出三态 / 门禁边界四条源级（run_eval·quality_gate·CI 零接触 + README 在案）
- 回归：全量 **3698 → 3736 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-d 统一执行链：RAG P2 Graph 动态化（R-P2-3）（2026-10-03）

- **[新增] schema 感知 Cypher 生成（键 `KG_SCHEMA_AWARE_GENERATION_ENABLED` 默认 false，五处同步）**：新 helper `components/utils/utils.py::resolve_generation_schema`——flag-on 时生成链与修正链（`cypher_tools/utils.py` 两处 ainvoke）的 `{schema}` 改用与校验同源的 `retrieve_and_parse_schema_from_graph_for_prompts`（测试数据域文档摘要 + 花括号清洗后的**发现态** `graph.schema`，标签/属性/关系来自库内发现而非静态中文标签描述），发现/渲染异常 **fail-open 回裸 `graph.schema`** 绝不打断生成；flag-off = 基线裸 `graph.schema` 逐位等价；孪生链（`text2cypher/generation/prompts.py`）静态中文标签冗余块改 `{legacy_schema_hint}` 占位 + `legacy_schema_hint()` 调用期取值——flag-off 拼接结果与基线**逐字节等价**，flag-on 置空（动态 `{schema}` 成为唯一权威源），孪生 node 同步接 `resolve_generation_schema`
- **[新增] 执行结果统一进证据池（键 `EVIDENCE_POOL_ENABLED` 默认 false，五处同步）**：`task_execution._to_observation` 成功分支携带 `meta["evidence"] = [{"content": 正文[:4000], "source": tool}]`——取**去重 stub 之前的原文**（grounding 不吃折叠占位文本），失败观测不携带；`LayeredVerifier._extract_evidence` 既有读取口（`meta.evidence`/`hit_chunks`）即刻可消费，喂 L2 `sources` 与 L3 grounding；flag-off = meta 无 `evidence` 键与基线逐位等价
- **[新增] L3 grounding 总闸（键 `VERIFIER_GROUNDING_ENABLED` 默认 false，五处同步）**：`task_execution` 与 `agent_loop_adapter` 两处生产调用点原硬编码 `require_grounding=False` 改读本键；与 `EVIDENCE_POOL_ENABLED` 同开 = 工具/图执行结果真正进 L3 支撑度核验（此前 L3 仅测试侧可达）
- **[裁定] 示例分类不在暂停设计范围**：`legal_retriever` 模块 docstring 与 `_dynamic_classify` 注释诚实化——R-P2-3 暂停条目为 schema 发现/感知生成/fast-path/证据池四件，示例分类保持显式 `NotImplementedError`（p68 契约钉住，warning 文案不变），few-shot 维持策展通用示例 fast-path + 词法相关性重排
- **[保留] 预定义模板 fast-path**：`predefined_cypher` 7 类模板与 `run_governed` 只读闸不动（p68 既有守卫覆盖）
- **[文档] 检索增强深度优化.md**：状态行改「P2 余 1 项待实施（R-P2-1/2/3/4 已落地），R-P2-5 仍待实施」+ §6 R-P2-3 完整注记（三键、fail-open 语义、裁定与守卫指针）；配置管理.md 特性开关分组行 + §5 详情三行
- **[测试] 新增 `tests/test_p86_graph_dynamic.py` 25 条**：三键默认值与五处同步（selfcheck/.env 活行/configuration.md 双落/p41 清单/rag 注记）/ schema 渲染（flag-off 恒等、flag-on 发现态+文档摘要、故障 fail-open、文档缺失仍动态）/ 生产链与孪生链接线源级 / 孪生静态块 flag-off 逐字节等价与 flag-on 置空 / 证据池六态（flag-off 无键、成功携带、失败不带、4000 截断、去重下保留原文、`_extract_evidence` 可消费）/ L3 两调用点接线 / fast-path 与 p68 分类契约未破
- 回归：全量 **3673 → 3698 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-c 统一执行链：RAG P2 Text2SQL 加固（R-P2-4）（2026-10-03）

- **[修复·correctness 无 flag] 重试耗尽显式失败，不把未执行 SQL 包装成答案**：`format_answer` 增 `is_valid` 分支（execute 节点仅 `is_valid=True` 可达 ⇒ 走到 formatter 即「重试耗尽从未执行」）——答案换成 `SQL_NOT_EXECUTED_ANSWER`（不再产出误导性「查询结果摘要」）+ 回写 `execution_error=SQL_NOT_EXECUTED_ERROR`，`text2sql_tool` 层既有 `if execution_error: errors.append` 消费 ⇒ 下游拿到显式失败而非成功样式的假答案；覆盖空 SQL/词法失败/dry-run 失败三类耗尽路径
- **[新增] dry-run 校验（键 `TEXT2SQL_DRY_RUN_ENABLED` 默认 false，unified 五处同步）**：`sql_validation` 节点词法+安全检查通过后跑 `EXPLAIN` 试跑（新模块 `sql_validation/dry_run.py`，先例 = Cypher `EXPLAIN` 试跑）——失败追加错误喂既有重试环（retry_count 照常递增）；**基础设施故障（连不上库/账号/库名等 `_INFRA_MARKERS`）fail-open 放行**不误杀合法查询；词法失败不重复试跑
- **[新增] schema 缓存（键 `TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS` 默认 0=关闭=基线逐位等价，unified 五处同步）**：`schema_retrieval` 节点按 `(DATABASE_URL, 归一问题)` 缓存 INFORMATION_SCHEMA 拉取（schema_context 含按问题筛表结果故键含问题）；失败不入缓存、128 条上限过期剪除；打点 `record_cache_hit/miss("text2sql_schema")`（`cache_hits_total{cache_type}`）
- **[修复] Wren 缺失显式降级打点**：`TEXT2SQL_BACKEND=wren` 但工厂返回 None（wrenai 未装/项目无效）此前只 `logger.warning`、steps 无痕——现同样留痕 `sql_execution_wren_degraded` + `record_degraded("text2sql_wren")`；既有 `WrenNotAvailable`/异常降级路径同打点（steps 此前被 tool 层丢弃，指标让降级在 Prometheus 可见）
- **[配置] 两新键五处同步**：settings 默认 false/0（Field+ge=0）/ selfcheck `DEFAULT_IGNORED_MISSING` +2 / `.env.example` 两活行 / 配置管理.md 特性开关分组行 + §5 详情表两行（p63 双落表口径）/ p41 手写清单 +2
- **[文档] 检索增强深度优化.md**：状态行改「R-P2-1/2/4 已落地、余 2 项待实施」+ §6 R-P2-4 完整注记（四件套、fail-open 语义、守卫指针）
- **[测试] 新增 `tests/test_p85_text2sql_p2_hardening.py` 29 条**：键五处同步 / formatter 显式失败四态（耗尽/空 SQL/执行错误原样/有效路径逐位不变）+ tool 消费源级 / 路由器 execute·retry·end 契约 / dry-run（flag-off 绝不调、失败喂重试环、通过保持 valid、词法失败跳过、空 SQL 短路、`_is_infra_error` 分类、无连接串 fail-open、sqlite EXPLAIN 通/拒）/ schema 缓存（TTL=0 每问现拉、同题命中+归一入键、换题重拉、失败不入缓存、命中计数）/ Wren 三态（请求缺失降级打点、默认后端不误报、异常降级打点）
- 回归：全量 **3644 → 3673 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-b 统一执行链：RAG P2 查询改写/HyDE（R-P2-1）（2026-10-03）

- **[新增] 查询改写/HyDE（两独立键 `RETRIEVAL_QUERY_REWRITE_ENABLED` / `RETRIEVAL_HYDE_ENABLED`，均默认 false = 与基线逐位等价；检索域 env 键沿 hybrid 块注释示例三处同步）**：`agentix/retrieval/usecases/query_rewrite.py`——`REWRITE_PROMPT` 多轮改写（意图补全，结合历史对话补全省略主语/指代，只输出检索式）+ `HYDE_PROMPT` 假设性文档生成（200 字内知识库风格正文片段）；归一纯函数 `normalize_query_output`（取首行、去包裹引号/直角引号、截 512、去空白忽略大小写等价 ⇒ 判未改写）/ `normalize_document_output`（截 1200）；`rewrite_query`/`hyde_expand` 经 `invoke_with_breaker`（熔断+限流）+ 8s `asyncio.wait_for` 有界调用，**LLM 失败/超时/空输出/等价输出一律回退原 query（fail-open），绝不阻断检索主链路**
- **[接线] `retrieve_scoped_docs_cached` 单缝（预检/工具/端口适配三调用方全覆盖）**：flag-off 路径代码逐字未动（`effective_norm = question_norm`、`cache_suffix = ""`）；生效才替换 dense/hybrid 的检索文本并给缓存池加后缀 `|r1`（改写）/`|y1`（HyDE）——沿 hybrid `|h1` 先例隔离结果池，失败回退原 query ⇒ 与 flag-off 同池语义相同；rerank 仍按原用户查询打分；新增 `history` 参数（预检路径由 `_stream_retrieval_preflight` 传 `_prepare_context` 的 `history_text`，工具/端口适配缺省空 ⇒ 单轮意图补全）
- **[文档] 检索增强深度优化.md**：状态行改「R-P2-1/2 已落地 F2b-b/F2b-a、余 3 项待实施」+ §6 R-P2-1 完整注记（键名、归一与 fail-open、单缝接线、缓存后缀、history 透传、守卫指针）；`.env.example` hybrid 块补两注释示例行
- **[测试] 新增 `tests/test_p84_rag_p2_query_rewrite.py` 27 条**：键同步 / 归一全谱（空回退、首行去引号、等价判未改写、512/1200 截断、多行文档）/ LLM 薄封装（成功带历史、异常 fail-open、空历史占位、超时 fail-open、空问题短路、HyDE 成败双态）/ 接线（flag-off 绝不调变换、生效改写/HyDE 检索文本、回退与异常保原查询、双 flag 组合改写→HyDE 序、缓存池 `|r1` 隔离与回退/基线同池无后缀、history 透传、预检源级接线）
- 回归：全量 **3617 → 3644 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2b-a 统一执行链：RAG P2 证据门禁（R-P2-2）（2026-10-03）

- **[新增] 证据门禁（键 `EVIDENCE_GATE_MODE`，检索域 env 键沿 `GROUNDING_*` 同族：rset 声明 + `.env.example` 活行 + selfcheck 白名单三处同步，不进 unified 五处）**：判定纯函数 `agentix/domain/policies/evidence_gate.py`——`normalize_gate_mode`（未知值 fail-safe 回 `observe`）+ `decide_evidence_gate`（触发信号任一即触发：grounding 低分 = 报告未跳过且 `score < GROUNDING_LOW_SCORE_WARN`（`0` 关该信号）∪ R1-2 数据条目引用未核验 `unverified_refs` ∪ 引用无法溯源被剥离 `stripped`）；三档动作：`observe`（默认，与基线逐位等价，触发仍如实记录）/ `warn`（答案尾追加一条「证据核验提示」警告，与 R1-2「引用核验提示」标注文案区分）/ `refuse`（答案整体替换为 `EVIDENCE_GATE_REFUSAL_MSG` 且 citations 清空，落库/推流一致）
- **[接线] `_post_process_and_persist` 两验证器之后**：门禁自身任何故障 fail-open 回基线交付（绝不因门禁拦答案）；审计 `tool_ctx.last_evidence_gate = {mode, action, triggers}` + Prometheus `evidence_gate_total{action}` 计数（`record_evidence_gate`，observe 交付不计）
- **[文档] 检索增强深度优化.md**：状态行改「R-P2-2 已落地 F2b-a、余 4 项待实施」+ §6 R-P2-2 完整注记（键名、三档语义、触发信号、fail-safe/fail-open、审计与指标、守卫指针）
- **[测试] 新增 `tests/test_p83_rag_p2_evidence_gate.py` 25 条**：键三处同步 / 模式归一 fail-safe / 判定全谱（observe 记触发仍 deliver、阈值 0 关信号、分数等于阈值不触发、skipped_reason 不触发、refuse 零触发照常交付）/ 警告文案与 R1-2 头区分 / `_post_process_and_persist` 集成（observe 基线含 R1-2 照常标注、warn 尾部警告、refuse 替换+citations 清空+落库一致、干净答案 refuse 不触发、grounding 低分触发、审计字段落 tool_ctx）/ Prometheus 计数
- 回归：全量 **3592 → 3617 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F2 · F2a 统一执行链：Memory P2（M-P2-2/4）（2026-10-03）

- **[新增] PreCompact 重注（flag `CONTEXT_PRECOMPACT_REINJECT_ENABLED` 默认关，挂 `CONTEXT_AUTOCOMPACT` 通道）**：`compaction.build_reinject_block` 纯函数——约束优先（五要素 `constraint_key` token 按出现序去重）+ 规则行按声明序补满，`PRECOMPACT_REINJECT_MAX_CHARS=1500` 有界；`ContextEngine`/`SimpleContextBuilder` 超预算触发时**压缩前**采集、块挂 assemble 尾部（不在 `traj_lines` 内 ⇒ 丢弃循环不删、规则必须幸存；计入 token 估算照常收敛），行带 `[precompact:<source>]` 审计标记 + `snapshot.meta["precompact_reinject"]` 来源计数；flag-off 块恒空与基线逐字节等价
- **[新增] 画像作用域（flag `MEMORY_PROFILE_SCOPE_ENABLED` 默认关）**：① 分级单源 `application/services/profile_scope.py`（`usage_habits`=session、`user_constraints`/`legal_domain_prefs`/`conflicts`=stable、`schema_version`=meta，**未知字段 fail-closed 按 stable**）+ 投影纯函数；② 授权键 `{prefix}:profile_scope:{user}` 仅 `granted` 视为授权、**读取异常 fail-closed**；`get_context_for_query` flag-on 扣下稳定层（画像卡稳定字段/频繁领域/语义卡）并审计留痕，flag-off 投影为直通拷贝逐位等价；③ 授权面 `GET/POST /ops/memory/profile-scope`；④ `POST /ops/memory/clear-user` → `clear_user_profile_chain` 分段尝试逐段计数、授权随画像一并撤销、`errors` 非空显式 500（删除面不静默吞错），复活 `clear_user_memories` 收摘要+索引+画像
- **[配置] 两新旋钮五处同步**：settings 默认 False / selfcheck `DEFAULT_IGNORED_MISSING` / `.env.example` `=false` 行 / p41 手写清单 +2 / 配置管理.md §2 记忆行 + §5 行
- **[文档] 记忆域深度优化.md**：状态行「F2a 已完成 ✓ M-P2-2/M-P2-4」+ §6 两项完整注记（flag 名、fail-closed、端点、审计标记、守卫指针），P2 全部完成
- **[测试] 新增 `tests/test_p82_memory_p2.py` 43 条**：五处同步 / `build_reinject_block` 纯函数（约束优先序、去重、max_chars 两段截断、空源、空行跳过）/ engine+simple 双接线（flag-off 逐字节等价与规则单次出现、flag-on 尾部块挺过丢弃循环+meta 计数、通道门、预算门、无规则仍注约束、reader fail-safe）/ 分级投影全谱 / 授权读写与 `get_context_for_query` 五态（flag-off 不读授权仍全量、未授权扣稳定层、授权全量、读故障 fail-closed、无画像仅卡扣下）/ clear chain 全删计数+中段失败后续段照跑+无授权照删 / ops 端点 200·500
- 回归：全量 **3549 → 3592 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F · F1e 统一执行链：A-P1-4 HITL 进环（2026-10-03）

- **[新增] 进环挂起/恢复（flag `HITL_IN_LOOP_ENABLED` 默认关 = 与基线逐位等价，父闸 `HUMAN_REVIEW_ENABLED`）**：`lg_builder` 在 `add_node("execute_task")` 处包注入壳（langgraph import 留 p66 白名单文件；四条件 = py3.12+ × 本 flag × 父闸 × 非 stream ask 声明的 `configurable.hitl_in_loop` 意图，stream 恒 L2 永不声明），把 `langgraph.types.interrupt` 经 config 传 `task_handler → run_task_execution(review_interrupt=…)`；`task_execution` 在 `run_batch` **之外**消费首个 `review_required`（`task_retry.run_task` 把 BaseException 转 outcome 会吞掉 GraphInterrupt，中断必须在节点内、批外抛出），恢复值 `approve` ⇒ `gate.approve` 落批准后重试一次并把该步 outcome 换成成功，`reject`/未知值/批准失败 ⇒ 终止后续波次回 failed 终态（保持 run_task_execution「只返回 LoopResult 不抛」契约，fail-closed）；py3.10 async `interrupt` 必死锁（test_p32:429-451 特征化）⇒ 版本门控，3.10 诚实降级回 `review_required`（绝不注入绝不调用）；已知边界：LangGraph 节点重入 ⇒ 挂起点之前的步骤恢复后重执行（非幂等写须调用方幂等化或规划为单步）
- **[新增] ② 无 approver REJECT（沿 sidecar 语义）**：executor `_review_preflight` 终局失败收进 `_blocked`——flag-on 且闸开且无 approver 且无决策面 ⇒ `gate.reject` + `reason=no_approver_wired_fail_closed`（不留永远没人批的 PENDING）；有 HITL 桥决策面（新增公开读取点 `human_review.decision_surface_active()`）或 flag-off ⇒ 照旧 `review_required`（flag-off 返回与原字面量相同的结果，逐位等价）
- **[新增] ③ 裁决顺序契约锁定** `deny → hook → ask强制 → allow`（前置风险/guardrail 闸 = deny，hook 可代批但代批仍须复过 ask，allow = 执行点 `consume` 消费；flag-off 路径代码逐字未动）
- **[新增] 恢复面 `POST /ops/hitl/in-loop`**：惰性调 `lg_builder.resume_in_loop`（`Command(resume=…)` + 迟绑定 `graph`，langgraph import 不出 p66 白名单）；非 approve|reject ⇒ 400，未知 thread/无挂起 ⇒ 409；裁决落账由 `task_execution` 消费侧负责
- **[配置] 新旋钮五处同步**：settings 默认 False / selfcheck `DEFAULT_IGNORED_MISSING` / `.env.example` `=false` 行 / p41 手写清单 +1 / 配置管理.md §2 Agent 行为行 + §5 行
- **[文档] 多智能体深度优化.md**：状态行 A-P1-4 改「F1e 已落 HITL 进环」、六缺口⑤ 标注已补、验收「高风险无 approver 必 REJECT」标注落点、灰度顺序 HITL 中途 ✓、§5 A-P1-4 完整注记（含三子句、节点重入边界与守卫清单）
- **[测试] 新增 `tests/test_p81_hitl_in_loop.py` 35 条**：五处同步 / 注入壳四条件门控（flag-off 透传原 config 对象、py3.10 真版本降级分支、3.12 假版本注入）/ Q3 REJECT 双态（flag-on 无通道 REJECT、有决策面 review_required、flag-off 基线逐位、低风险零登记、连续否决不漏 PENDING）/ ③ 顺序契约（hook 收物化请求、代批仍复闸、flag-off 原序）/ 进环管线（approve 落批准重试一次恰一产物、reject 快速失败零副作用、未知值 fail-closed、非 review 失败不挂起、缺省基线语义、链式两步各挂一次、图节点贯通）/ ops 端点（approve|reject 委托、400、409）/ 接线锁（chat_service 意图键、ops 零顶层 langgraph、lg_builder 壳与助手）/ 真图 e2e（skipif py3.12+：挂起 → `Command(resume)` 恢复返回裁决值，py3.12 实测 35 passed）
- 回归：全量 **3515 → 3549 passed / 15 skipped / 0 failed**（2026-10-03，`ruff check` 全绿；py3.12 并行环境 p81 35 passed）

### Stage F · F1d 统一执行链：A-P1-3 并发工具轮（2026-10-03）

- **[新增] 并发工具轮（flag `AGENT_TOOL_ROUND_CONCURRENCY` 默认 1 = 串行逐位等价）**：一把旋钮共管两面——L2 检索聊天 `chat_agent_runner._execute_tool_calls` 与 L3 `AgentLoop` 批路径（`round_concurrency` 组合根注入，orchestration 保持不 import config 纯编排）；flag-on 批路径顺序 = 授权门（逐调用 `round_gate.preflight_block` 执行点语义 `consume=True`，HUMAN_REVIEW 关闭零副作用恒放行、未获批 fail-closed 落 error 不派发）→ 危险强制串行（`is_dangerous`：有效风险 MEDIUM+ = L1+，含参数提权；危险绝不与同批任何调用并发）→ 剩余步数预算裁剪（并发不放宽 max_steps）→ 批前一次上下文重建 → Semaphore(N) gather（异常按单步路径同款 classify）→ 观测按声明序落位 + steps_used 单调 + ON_STEP_END → 每批至多一次 replan（取声明序首个失败观测）；L2 结果按输入序组装（ToolMessage 落位 = 输入顺序，事件序不变），危险先串行执行、safe 集限并发；依赖步不与前置同批（ready 谓词与 `_next_pending` 同款）；「家政工具不计进度」当前两面无家政在途（chat 5 工具全检索、L3 表 act），机制留口归 F2；flag-off 两面原路径代码逐字未动（守 test_p52 G6 / p74 事件序）
- **[新增] 共享派发闸 `agentix/tools/gateway/round_gate.py`**：`is_dangerous`（判定故障保守按危险串行）+ `preflight_block`（闸件不可用 fail-closed，与 ToolExecutor `_review_preflight` 同款）；两面均函数内惰性 import——retrieval→tools 新边（R6 已知）沿 orchestration/executor.py result_limiter 先例
- **[配置] 新旋钮五处同步**：settings 默认 1（ge=1）/ selfcheck `DEFAULT_IGNORED_MISSING` / `.env.example` `=1` 行 / p41 手写清单 +1 / 配置管理.md §2 Agent 行为行 + §5 行
- **[文档] 多智能体深度优化.md**：状态行 A-P1-3 改「F1d 已落 L2/L3 并发工具轮」、六缺口③ 标注已补、§5 A-P1-3 完整注记（含家政留口与守卫清单）
- **[测试] 新增 `tests/test_p80_multiagent_p1_concurrent_round.py` 33 条**：五处同步武轮 / round_gate 危险判定与授权门双态 / Surface A（flag-off 串行基线、并发与 Semaphore 封顶、危险先串行零重叠、授权门拦截、缺工具与异常 error 回执、入参解析）/ Surface B（串行基线、独立批并发与封顶、依赖不入批、预算裁剪、危险单例批、拦截步 failed Observation、声明序落位、ON_STEP_END 单调、每批至多一次 replan、批前恰一次重建、批内异常分类）
- 回归：全量 **3482 → 3515 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F · F1c-1 统一执行链：A-P1-2 LLM 规划 + TodoWrite（2026-10-03）
- **[新增] LLM 规划器（flag `AGENT_LLM_PLANNER_ENABLED` 默认关，父闸 `AGENT_LOOP_ENABLED`）**：`agentix/policies/planner/llm.py::LLMPlanner` 经 `agent_loop_adapter._select_planner` 注入 AgentLoop——`with_structured_output(LLMPlanOutput)` 结构化输出（temperature=0，沿 `_llm_decompose` 同款）、执行表键集合白名单整步过滤（`tool`→`action` 解析，白名单外整步丢弃；A-P1-2「按任务类型白名单」的执行表交集校验注入口）、步数封顶 `MAX_ITERATIONS`、id 去重与依赖裁剪（未知依赖/自引用裁除）；LLM 调用失败/输出空/全被滤掉一律 fail-open 回 `SingleStepPlanner` 语义（flag-on 也绝不拖垮整轮）；`DeclaredSteps` 确定性 fast-path 原样保留；规划提示携带当前 TodoWrite 快照（`todo_provider` 注入，provider 故障 fail-open 无快照照常规划）；`AGENT_MAX_REPLANS>0` 时 LLM planner 同时注入 `RetryReplanner` 内层（`_select_replanner` 拆出，flag-off 分支与基线逐位等价——不传 `planner` kwarg 保持内层 None）；flag-off 选择器返回与基线同型同参的 `SingleStepPlanner`
- **[新增] `todo_write` 工具（同 flag）**：Codex/Claude TodoWrite 语义——**整体替换**当前清单、`status ∈ pending/in_progress/completed`、至多一条 `in_progress`、50 条/500 字封顶（schema pydantic 与 handler 双侧校验，直连调用同等把关）；`TodoStore`（application 层，`data/todos/current.json` 默认路径）JSON 原子落盘（tmp+`os.replace`）跨进程可恢复、文件损坏按空清单起步 fail-open、落盘失败只告警内存态保留（更新绝不因磁盘故障丢）；回执含全表+版本号即读通道；声明 `L2+control`（名称词表 `write` → 有效风险 HIGH，两层一致沿 `write_task_artifact` 先例）+ `side_effect=True` 禁 auto-retry；`registry_init` 声明与 `main.py` 装配成对 flag 门控（`install_todo_write_tool` 自身 belt-and-suspenders 再判一次 flag 并 `ensure_registered()`），`Plan 工具` = 既有 `enter/exit_plan_mode`（A-P2-4 已完成不重复立项）
- **[文档] 多智能体深度优化.md**：状态行更新（A-P1-2 部分完成→F1c 已落 LLM 规划+TodoWrite，余按任务类型分型细化；A-P1-3/A-P1-4 分别标注 F1d/F1e 待施）+ §5 A-P1-2 完整注记 + 六缺口②标注；配置管理.md §2 Agent 行为行 + §5 新增 `AGENT_LLM_PLANNER_ENABLED` 行
- **[修复] `test_notice_never_contains_config_values` 假阳性**：随机 `config_hash`（hex）偶发含数字子串（如 "21"）导致误报——断言改为剔除哈希字段后再做值子串检查（语义不变，仍精确命中 event/changed_keys 泄值）
- **[测试] 新增 `tests/test_p79_multiagent_p1_llm_plan.py` 36 条**：五处同步棘轮 / 选择器双态与 replanner 内层注入 / LLMPlanner 全谱（成功、白名单丢步、三类 fail-open、去重裁依赖、步数封顶、Todo 快照进提示与 provider 故障、adapter 端到端）/ 声明契约（L2+control+title）/ registry 双态与 main 源级门控 / executor 端到端回环与四类校验错误 / TodoStore（整体替换、版本、恢复、损坏、落盘故障、清空、直连 handler）
- 回归：全量 **3446 → 3482 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿）

### Stage F · F1b2 统一执行链：M-P1-3/4/5 残余三项（2026-10-03）
- **[新增] M-P1-3 MEMORY 索引制（flag `MEMORY_INDEX_ENABLED` 默认关，挂 `MEMORY_CONTEXT_ENABLED` 通道）**：记忆注入段升级为「段落头保留 + 正文 `obs://` 指针索引」——正文行经 `offload_content`（kind=`memory_topic`，强制外置、rebuildable）落 ArtifactStore，入口只留 `- {预览≤100字} → obs://{artifact_id}` 索引行；常驻预算 `MEMORY_INDEX_MAX_CHARS=1500`（常量决策，与指令层 12k 同范式）超限截索引尾部附提示；外置降级该行保留原文（fail-open 不丢记忆），索引变换自身异常回原文；取回端 = `read_artifact`（`obs://` 前缀自动剥离），`MEMORY_INDEX_ENABLED` 单开（TASK_EXEC 关）也声明并装 read handler（registry gate 改为两 flag 任一、main.py 独立接线块、handler 拆出 `install_artifact_read_tool` 幂等复用）；flag-off 注入与基线逐位等价
- **[新增] M-P1-4 AGENTS 级联（flag `AGENTS_CASCADE_ENABLED` 默认关）**：`build_instructions` 升级为「settings → skills → 全局 `~/.agentix/`（常驻）→ 项目根（常驻）→ 子目录（消息触达才加载）→ 会话」六层——全局层 flag-on 才读（`global_start` 可注入，读失败给空）、子目录层 `mentioned_instruction_dirs` 从消息解析类路径 token（存在 + 项目边界内 + 去重 + 上限 8）、逐目录向上找最近约定文件（根级命中由既有 hash 去重丢弃），预算/去重/截断全沿既有 4k/12k 机制；生产接线 `agent_loop_adapter` 传 `subdirs=mentioned_instruction_dirs(message)`（flag-off 返回 `()` ⇒ 输出与基线逐字节一致）；配套 L0 只读工具 `read_agents_file`（规划新建已落地 `agentix/tools/adapters/agents_file_read.py` + `agentix/agent/context/install.py`，四步接线 = 声明/校验/组合根/`main.py`，项目边界内向上查找最近约定文件，未命中结构化 `found=false` 回执，正文沿 4k 截断）
- **[新增] M-P1-5 obs:// 隔离接线半程（flag `CONTEXT_ISOLATE_PERSIST_ENABLED` 默认关）**：`ContextEngine` 超长观测隔离从「字符串替换的悬空引用」升级为真正外置——flag-on 正文经 `offload_content`（kind=`agent_step`）落 ArtifactStore，`isolated_refs` 与截断标记落 `obs://{artifact_id}`（经 `read_artifact` 可取回，与记忆索引指针共用同一条取回通道）；外置失败/降级 fail-open 回 `obs://{step_id}` 裸引用（隔离绝不抛错）；flag-off 隔离引用与基线逐位一致（守卫 `test_agent_context_budget` 暂停语义不变）；`read_artifact` handler 增 `obs://` 前缀剥离 + 描述补指针取回语义
- **[守卫/配置] 3 新旋钮五处同步**（settings 默认关 / selfcheck `DEFAULT_IGNORED_MISSING` +3 / `.env.example` 3 活跃 `=false` 行 / p41 手写清单 +3 / 配置管理.md §2 记忆行 + §5 三行）；记忆域深度优化.md 状态行与 §5 三条注记（M-P1-3/4/5 均改「F1b2 已落地」并给出机制细节）
- **[测试] 新增 `tests/test_p78_index_cascade_isolate.py` 35 条**：3 键五处同步 / 记忆索引（flag-off 原文注入、嵌套闸零 LTM、段落头保留 + 3 topic 外置取回回环、长正文预览封顶、降级留原文、预算 130 截尾、端到端注入→`read_artifact` 指针取回、索引异常回原文、MEMORY_INDEX 单开也声明取回端）/ AGENTS 级联（flag-off 忽略两新参、六层序、子目录根命中去重、settings 读取、触达解析四态 + limit、title/校验契约、声明与 handler 闸 + 审计成对、边界内取回与结构化 miss、生产接线源断言）/ obs 隔离（flag-off 裸引用逐位、flag-on 外置可取回且头部保留、降级回裸引用、`obs://` 前缀取回与未知 ID fail-fast、源断言）
- 回归：全量 **3411 → 3446 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿，门禁 883 passed）

### Stage F · F1b 统一执行链：Memory P1 接线（M-P1-1/2/5，2026-10-03）
- **[新增] M-P1-1 向量召回（flag `MEMORY_VECTOR_RECALL_ENABLED` 默认关）**：进程内 provider `InMemoryMemory.recall` 升级为「关键词 × 向量」双路 + recency 衰减（半衰期沿 `MEMORY_RECENCY_HALF_LIFE_DAYS`，TopK 沿 `MEMORY_RECALL_LIMIT`）——纯策略落 `agentix/domain/policies/memory_recall.py`（`cosine_similarity`/`recency_weight`/`fuse_recall_scores`，稳定排序、双通道加权 `(0.5·kw + 0.5·vec) × recency`、融合分 0 不返回保持「只给命中」语义）；embedding 复用检索域 `embed_texts_sync`（懒 import + 1.0s 内部预算 < MemoryManager provider 1.5s 超时，防「向量慢把关键词一并拖降级」）；embedding 失败/超时/空向量 ⇒ 打点后落回纯关键词路径（验收「断向量服务仍可答」）；写路径 best-effort 挂向量、`forget` 随删清理；flag-off 召回与基线逐位等价（零 embedding 零 `_vectors` 开销）；读路径二次脱敏（M-P0-3）与 scope 隔离在向量路径同样生效
- **[新增] M-P1-2 后台评审 LLM 路由 + turn-end 生产者（gate 沿既有 `MEMORY_REVIEW_ENABLED` 默认关）**：`memory_review.route_memory_item_llm`（`REVIEW_ROUTE_PROMPT` 只认 `user_profile`/`episodic` 两词，LLM 不可用/解析失败 ⇒ 规则路由兜底 reason 标 `fallback-rule:` 前缀，honest 可审计）；`review_items` 增 `router` 可选参（缺省/路由器异常走规则，既有契约零变化）；`schedule_review_for_summary` 增 `use_llm`；新生产者 `ChatService._maybe_schedule_turn_review`（turn 文本 fire-and-forget，调度失败只打点）三接线点 = ask 成功 / stream 正常 done / stream 兜底 done（错误路径不评审）
- **[新增] M-P1-5 真分词/真计数半程（flag `CONTEXT_REAL_TOKEN_COUNT_ENABLED` 默认关）**：`agentix/agent/context/token_count.py` `count_tokens`——flag-off ≡ `len//4` 与全部既有估点逐字节一致；flag-on 走 tiktoken cl100k_base（进程内懒加载一次 + 负缓存，钉版 requirements `tiktoken==0.13.0`），tokenizer 缺失/编码失败退 CJK 感知启发式（永不打断上下文构建）；接线 7 处估点（`context/engine.py` ×3、`context/simple.py` ×3、`context/compaction.py` ×1）；obs:// 超长记忆接线与 M-P1-3/4 归 F1b2
- **[守卫/配置] 2 新旋钮五处同步**（settings 默认关 / selfcheck `DEFAULT_IGNORED_MISSING` / `.env.example` `=false` 两行 / p41 手写清单 +2 / 配置管理.md §2 记忆行 + §5 两行）；p74 `RETRIEVAL_BYPASS_ALLOW` 棘轮随迁 +1 对（`agentix/agent/memory/service.py → agentix.retrieval.infra.rag.dense`，批次评审入册，零僵尸）；记忆域深度优化.md 状态行与 §5 三项 F1b 注记
- **[测试] 新增 `tests/test_p77_memory_p1_wiring.py` 51 条**：flag 五处同步 ×2 键 + requirements 钉版 / 融合纯策略已知值（cosine、半衰期 0.5、双通道 > 单通道、recency 乘法、平分稳定序）/ 向量召回（flag-off 零 embedding、无子串语义召回、双通道排序、embedding 失败与空向量 fail-open、写失败仍存、PII 脱敏、recency 降序、forget 清向量、scope 隔离）/ LLM 路由（双目标解析、失败与乱输出回退规则、prompt 渲染、router 参数化与异常回退、`use_llm` 调度路径与原契约保持）/ turn-end 生产者（flag-off 零调度、flag-on 文本与 `use_llm=True`、调度异常吞掉、三接线点源断言）/ 真分词（flag-off ≡ `len//4` 参数化、flag-on ≡ tiktoken、tokenizer 与编码失败退 CJK、7 估点源断言、builder flag-off 不变）
- 回归：全量 **3360 → 3411 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿，90 条门禁全绿）

### Stage F · F1a 统一执行链：RAG P1 真 Hybrid（R-P1-1..5）（2026-10-03）
- **[修复] 检索后端元组解包（R-P1-5，无条件随行）**：faiss/milvus/qdrant 三个真实后端 `similarity_search` 返回 `(doc, score)` 元组，`_similarity_search_with_fetch` 的 else 分支曾整包当 doc——走真实后端且 fetch 未命中即 `AttributeError: 'tuple' object has no attribute 'metadata'`；新增 `_as_page_content_doc` + 元组解包、真实分数流入 scored pairs，连带 `RETRIEVAL_MAX_DISTANCE=1.2` 距离阈值由「从未生效」变为实际过滤（非元组元素仍 score 0.0 透传旧行为）
- **[新增] 融合纯策略与过滤谓词**：`agentix/domain/policies/rank_fusion.py`（`rrf_fuse` k=60 / `zscore_fuse` alpha，首通道加权、std=0 记 0、稳定排序、纯 stdlib 零 retrieval 依赖——Channel A/B 共用，规避 p74 application→retrieval 引用面）；`chat_scope.meta_matches_filter`（None/dict(值 list=IN)/callable，与 `build_faiss_filter` 同语义，异常 fail-closed）
- **[新增] 进程内 BM25 稀疏通道**：`retrieval/infra/rag/sparse.py`——`tokenize`（ASCII 词 + CJK 一元/二元）、`BM25Index`（Okapi k1=1.5 b=0.75、按 chunk_id 映射）、`_load_index`（kb_id TTL=600s 缓存、容量 8、锁外加载）、`sparse_search`（filter 复算同口径、min_score 过滤、过取 2x 截断、加载失败 fail-open 返回 []）
- **[新增] 四阶段 Hybrid 编排**：`retrieval/usecases/retrieval_hybrid.py` `retrieve_hybrid_docs`——rewrite→dense（`to_thread` 调 `retrieve_docs_with_retry`，channel_n=max(search_k, min(FUSION_TOP_N, search_k*4))）→sparse（子闸门 / 无 session 跳过 / filter_in_force 同口径）→融合（rrf/zscore 按 chunk_id 融合、dense 优先占位、融合分写回 `_retrieval_score`）→可选 rerank（候选截 `RETRIEVAL_RERANK_CANDIDATES`，双层 try/finally fail-open + 熔断状态记报告）；ContextVar 阶段报告（`set_stage_report`/`pop_stage_report`）+ 事后 `span("rag.hybrid_stages", **attrs)` 结构化轨迹；`retrieve_scoped_docs_cached` 增 `session` kwarg + hybrid 分支 + 缓存键 `|h1` 标记隔离
- **[变更] SSE 四阶段进度帧**：preflight 前清报告、preflight 后 pop，有报告即追加 `{type:progress, stage:"retrieval", detail:{step:2,total:3,stages:[...]}}` 事件（进度暂停测试三帧基线不受扰）；三调用点补传 session（`chat_service_helpers` preflight / `chat_agent_tools` `ctx.session` / `retrieval_port_adapter`）
- **[变更] Channel B 本地检索融合**：`local_search_node.fuse_local_channels`（rrf k=60 / zscore alpha=0.5，chunk 内容哈希去重、pg 侧优先），`KB_LOCAL_FUSION_ENABLED` 门控——关时走旧 postgres-first 逐位不变
- **[配置] 12 新旋钮全部默认关**：retrieval settings 10 键（`RETRIEVAL_HYBRID_ENABLED`/`RETRIEVAL_SPARSE_ENABLED`/`RETRIEVAL_SPARSE_MIN_SCORE`/`RETRIEVAL_SPARSE_CACHE_TTL`/`RETRIEVAL_FUSION_METHOD`/`RETRIEVAL_RRF_K`/`RETRIEVAL_FUSION_ALPHA`/`RETRIEVAL_FUSION_TOP_N`/`RETRIEVAL_HYBRID_RERANK`/`RETRIEVAL_RERANK_CANDIDATES`）+ 统一 settings 2 键（`KB_LOCAL_FUSION_ENABLED`/`KB_LOCAL_FUSION_METHOD`）；`.env.example` 熔断块后 12 行注释块、配置管理.md §5 两行（仅 KB 键，retrieval 专属键不进文档纪律保持）、p41 手写清单 +2
- **[测试/文档] 新增 `tests/test_p76_rag_p1_hybrid.py` 27 条**：配置默认值 / rrf+zscore 已知值 / tokenize+BM25+作用域+缓存 / 后端元组解包与阈值生效 / 四阶段链路与融合顺序（flag 开关、缓存 `|h1` 隔离、rerank fail-open）/ ContextVar 报告交接 / SSE 四帧 / Channel B 融合与门控；`docs/design/检索增强深度优化.md` 状态行改「P1×5 已完成」+ §5 各项 F1a 注记
- 回归：全量 **3333 → 3360 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check` 全绿，90 条门禁全绿）

### Stage M · M6 统一执行链：删三簇 shim + 全部旧路径清尾（2026-10-03）
- **[移除] 三簇 strangler shim 整批删除**（56 文件 + 空目录）：`agentix/infrastructure/tools/`、`agentix/mcp/`、`agentix/capabilities/tools/` 三簇 + `agent/` 执行件七目录（`core`/`executor`/`multiagent`/`planner`/`replanner`/`verifier`/`hitl`）+ `application/agents/lg_{builder,states,supervisor}.py` + `application/services/{control_plane,hitl_bridge}.py` + `domain/agent_types.py` + `domain/ports/agent_core.py` + M5 两件旧路径（`retrieval/domain/policies/legal_effectiveness.py`、`retrieval/domain/services/grounding_verify.py`）；全仓模块级 `_TARGET` 转发常量归零
- **[变更] codemod 全仓 import 改指规范位**：113 文件 / 384 处（`PREFIX_MAP` 按 p70 `SHIM_MAP` per-file 映射 + infrastructure/tools 链式 + `NAME_WISE` 生命周期/步骤/预算名收敛 + `capabilities.tools` 特例 `human_review→tools.gateway`、`guardrails→policies.guardrails`）；手工面：`domain/ports/__init__` 七端口改从 `domain.agent.ports` 再导出、`domain/status` shed `ExitStatus`/`ExitReason`（只余 `RequestStatus`）、`runtime/{state,steps,budget}` shed 对应名（`BudgetState` 保持规范位同一）、`capabilities/__init__` 去 tools 六名 re-export、`agent/__init__` 惰性门面 string 改指 orchestrator、phase12/20/30 `touched_paths`、ablation `WIRED_FLAGS` 斜杠路径、10 处 `from ...application.agents import lg_*` package-form 改指 `orchestration.langgraph`、2 处 hitl_bridge/control_plane package-form
- **[守卫翻转]**：p36 `_TARGET` 白名单条目删除（同名异值清单余 `GUARDRAILS_SYSTEM_PROMPT` 一条）；p66 `SHIM_DIRS_ALLOWED` → **空集终态**（守卫 #12「迁移期白名单相等 → 终态空集」达成）+ 守卫 #3 增 `TOOLS_GUARDRAILS_EDGE_ALLOWED = ("agentix.policies.guardrails",)` 窄豁免（`tools/gateway/executor.py` 删 shim 后直连规范位 guardrails，蓝图 §5 Tool Gateway 链含 Guardrails 步，PROJECT_SPEC §9.5 M3 注记的「M6 清尾时同步调整守卫与 import」落定）；p69 `OLD_PATHS` 存在性断言翻转为 `SHIM_PATHS_DELETED_M6` 负向 + 同一对象断言拆分为「shed 名已收敛 / 存活名规范位同一 / 已删模块 ImportError」三组 + `COMPLETED_STAGES` 登记 M6；p70/p71/p72/p73 逐文件断言翻转（shim `_TARGET`/别名式形态 → 文件删除 + import `ImportError` + 规范位单一入口 + monkeypatch 只落规范位 + 加载序子进程改「旧路径断、规范位通」）；p74 M5 口径已随 M5 翻转
- **[测试] 新增 `tests/test_p75_m6_shim_removal.py` 30 条**：零 `_TARGET` equality 棘轮（agentix/ 生产源 AST 模块级扫描 == 0）、20 个旧 dotted 前缀静态 import 面 == 0（`agentix/` + `tests/` + `scripts/` + `evals/` + `kb_ingest/` + 根入口 `*.py`；archive 暂停历史不入面）、19 个删除面 + 5 dotted `ImportError` 参数化、生产源零旧斜杠路径字符串（tests 留档清单不入面）、门面消失三断言（`capabilities` 无 `tools` 属性、`import agentix.agent` 不拖旧子包（子进程 `sys.modules` 检查）、`domain.status` 单一来源）
- **[顺手修] 生产源历史路径措辞**：`domain/agent/types.py`、`orchestration/langgraph/lg_builder.py`、`tools/{adapters,gateway}/__init__.py` 四处 docstring/注释的旧斜杠路径改 dotted/删除口径（保 p75 棘轮为零）；p69/p73 负向断言改 `importlib.import_module` 动态化（保静态 import 面为零）
- **[文档]**：docs sweep 14 文件——current 文档（architecture/design/guides/runbooks/PROJECT_SPEC）27 条 backtick 旧路径 repoint 规范位、历史文档（module-inventory、Phase21-26/28-31/32-34、蓝图、source-code-study）去反引号 + `（M6 已删，现见 new）` 注记；PROJECT_SPEC 手工 16 处：§2.4 shim 表改「M6 已删、留档迁移期形态」口径、`capabilities/__init__` 行（13 行 re-export → 7 行 docstring）、Strangler 链段、§9.5/§11.1/防御常量表/目录结构表时态、`PROTECTED_VERIFIER_PATHS` 表行（双护 → 终态 4 条）；README 测试真源 3305→3333
- 回归：全量 **3305 → 3333 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check .` 全绿，81 条门禁 + p36 共 90 条全绿）

### Stage M · M5 统一执行链：检索域收编（剥离 ReAct + 单门 + 共享域）（2026-10-03）
- **[变更] 执行链剥离改造**：`retrieval/usecases/chat_agent_runner.py`（349 → 391 行）剥离 langgraph（删 `create_agent` 版本自适应、`GraphRecursionError`、`_stream_agent_plan_e` 与 `stream_mode=["messages","updates"]` 消费），换 `_stream_native_rounds` 手写 capability 内部工具循环（`llm.bind_tools(...).astream` 聚合 `AIMessageChunk.tool_call_chunks` → `.tool_calls` + `tool.ainvoke(ToolCall)` → `ToolMessage`，工具异常转 error ToolMessage 回注继续）。**事件契约四 kind 一字不变**（`AgentStreamEvent` / `chat_service._stream_agent_and_persist` 消费零改动，p65 词汇单轨）；事件时序复刻旧路径：缓冲轮 `model_usage`（工具执行前，供输入预算检查）→ `tool_running`（`announced` 按工具名跨轮去重、仅 deep）→ 执行；已跑工具轮文本 delta 实时流出、轮末 `model_usage` 滞后对齐 updates 语义。**顺手修双调 bug**：首轮无工具 = 单个 `answer_delta` 一次模型调用（旧路径 messages 闸门关死 → fallback `agent.ainvoke` 二次调模型且 usage 计 0）；步数（模型步 + 工具步，复刻 langgraph super-step 粒度）对照 `AGENT_RECURSION_LIMIT`（默认 25，函数内读取）超限给 `_RECURSION_HINT` + `terminated(recursion_limit)`；流失败 fallback 改 `_ainvoke_rounds` 非流式多轮，终文单 delta **带真实 usage**（旧 fallback 从 state dict 取 usage 恒 (0,0)，预算检查此前实为死代码，现可核算）
- **[变更] Port 装配单门收敛**：`knowledge_gateway` 顶层增 `get_retrieval_port()`（应用侧取 `RetrievalPort` 的唯一装配入口，application 内唯一触碰 `retrieval_port_adapter` 的文件）；`file_handler` / `general_handler` / `kb_tools/node` 三处散点 import 改指网关（调用点同步 `create_retrieval_port()` → `get_retrieval_port()`）
- **[变更] 纯策略件迁共享域**：`retrieval/domain/policies/legal_effectiveness.py`（208 行）与 `retrieval/domain/services/grounding_verify.py`（201 行）迁 `agentix/domain/policies/`（两件纯 stdlib，零 retrieval 依赖）；旧路径 = `sys.modules` 别名式 shim（M2 定型，新旧同一模块对象，p42/r2/r3 旧路径测试零改动）；外部引用改指新家：`kg_sub_graph/cypher_tools/node`（1 处）、`policies/verifier/layered`（1 处）、`retrieval/usecases/chat_service`（5 处函数内延迟 import）、`retrieval/domain/policies/__init__` re-export 源
- **[变更] 进化保护顺手修**：`agentix/agent/evolution/loop.py` `PROTECTED_VERIFIER_PATHS` 此前仍指 M3 旧路径（`agentix/agent/verifier/*` shim），**真实 `agentix/policies/verifier/*` 未受防线 3 保护**——补齐 M3 实体 + M5 新家（`domain/policies/grounding_verify.py`）并保留旧 shim 双护（M6 拆 shim 时同步收缩）
- **[测试] 新增 `tests/test_p74_m5_retrieval_unification.py` 25 条**：原生循环金测（事件时序四场景、单 delta + 单次模型调用、deep/fast tool_running 语义与去重、暂停工具文案、输入/输出预算终态且工具不跑、轮次上限提示、流失败 fallback 带 usage、双失败错误兜底、工具异常回注、kind 词表暂停）、runner 与整个 retrieval 包 langgraph-free AST、单门三断言（装配单点 / 三调用方改指 / 返回 Adapter 实例）、**旁路清零 equality 棘轮**（包外 `agentix.retrieval.*` 静态 import 面 == 13 条 (file, module) 豁免：main 组合根 5 + 网关 2 + settings 配置面 4，零新增零僵尸）、共享域迁移四断言（同对象 / `_TARGET` / 零依赖 / PROTECTED 覆盖）
- **[守卫随迁]**：p66 `LANGGRAPH_ALLOWED` 删 chat_agent_runner 行（11 → 10，equality 断言）+ `SHIM_DIRS_ALLOWED` 增 `retrieval/domain/{policies,services}` 两目录；p69 `COMPLETED_STAGES` 登记 M5 + MIGRATION_MANIFEST 增 2 条 M5 迁移条目（参数化随清单 +2）；p36 `_TARGET` 注释补 M5 配对谱系
- **[文档]**：PROJECT_SPEC §9.5 runner 节按 391 行新实现重写（含三轮时序图式条目）、§22.1 生成步去 `create_agent`、L3 grounding 路径指新家、进化防线常量表更新（行号 +9）、knowledge_gateway 方法表行号刷新并记 `get_retrieval_port`、白名单计数 11→10；README 测试真源 3278→3305；dependency-map LangGraph 出现点表移除 runner 行（附 M5 注记）+ Verifier 簇 grounding 行指新家；`chat_service` 模块 docstring 去 `create_agent`；蓝图为规划口径、module-inventory/Agent_Core_2.0_规划/current-tree 按历史口径不动
- 回归：全量 **3278 → 3305 passed / 14 skipped / 0 failed**（2026-10-03，`ruff check agentix tests scripts evals` 全绿，81 条门禁 + p36 共 90 条全绿）

### Stage M · M4 统一执行链：控制平面 → runtime/control_plane.py（2026-10-02）
- **[变更] 规范位落成**：`application/services/control_plane.py`（321 行）迁往 `agentix/runtime/control_plane.py`（蓝图 §5「Run registry / cancellation 归位」）——`ControlPlane`（spawn/bind/状态迁移/cancel 真打断 asyncio.Task/`check_admission` 并发与深度准入 fail-closed/有界 FIFO 淘汰）、`AgentRun`、`AdmissionDenied`、单例 `get_control_plane`/`reset_control_plane` + M0 `RunState`/`TERMINAL_STATES` 再导出（p69 契约经 alias shim 对象同一保持）。**唯一包内改写**：`TokenBudget` 模块级 import 收进 `if TYPE_CHECKING`（两处字符串注解 + `to_dict()` 鸭子类型，运行期零引用）——Runtime 簇首次纳入跨簇文件而不破「模块级 agentix 边只到 runtime/domain/config」自包含不变量（先例 `runtime/state.py` 对 `steps` 的 TYPE_CHECKING 回指）；`config.settings` 本就在 `get_control_plane()` 内延迟读取
- **[变更] 旧路径降级 shim**：`application/services/control_plane.py` = **`sys.modules` 别名式**转发（M2 定型形态；旧/新同一模块对象，phase25 `monkeypatch.setattr(cp_mod, "get_control_plane", ...)` 形态与 `from agentix.application.services import control_plane` 包形态皆零改动通过）；`runtime/__init__.py` **刻意不加汇总导出**（避免包面膨胀与 eager 加载序扰动），仅 docstring 记入 control_plane 条目与 shim 说明
- **[测试] 新增 `tests/test_p73_m4_runtime_control_plane.py` 21 条**：目标位/旧路径 alias 形态、7 符号同一（含 p69 RunState/TERMINAL_STATES 再导出契约）+ 模块同一 + 包形态 import 解析、monkeypatch 回滚契约（裸 sentinel + `get_control_plane` phase25 形态）+ 单例注册表跨路径共享、Runtime 自包含（全包模块级 AST 扫描仅许 runtime/domain/config + control_plane 的 token_budget 仅 TYPE_CHECKING + `AgentRun.budget` 惰性字符串注解可无 TokenBudget 构造）、双向加载序 3 子进程
- **[守卫随迁]**：p69 `COMPLETED_STAGES` 登记 M4（MIGRATION_MANIFEST 条目 M4 翻转验证）、p36 `_TARGET` 注释补 M4 配对；p66 `SHIM_DIRS_ALLOWED` 的 `application/services` 目录 M3 已在册，无需再增
- **[文档]**：PROJECT_SPEC 顶层包表 `runtime/` 行记 M4 迁入、§11.1 增 M4 段 + 规范位/shim 两表行、全文 3 处路径 replaceAll；README:107 Control Plane 行、`运行时护栏.md` 控制平面行指新家；dependency-map Runtime 簇表增 control_plane 行 + 「运行期自包含」结论改写（两处惰性跨簇边：runtime.py→observability 函数内延迟、control_plane→token_budget TYPE_CHECKING）；module-inventory §Plan 期 defer 行与 Phase21-26/蓝图按历史/计划口径不动
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
- **[文档]** PROJECT_SPEC：顶层包表新增 `orchestration/` 行 + `agent/` 行改写为 Context Provider 家、§2.4 shim 表补 capabilities/tools（M1）与 M2 两行、§2.5 两套编排辨析重写（L3 推进权两分家 + LangGraph 允许位置改 p66 白名单口径）、§11.1 增 M2 规范位/shim 段与两行、全文 21 处路径 replaceAll；README:136、分层架构与依赖规则.md、dependency-map、module-inventory（executor/lg_builder/符号表 4 处）、routing quick reference、research-agent-demo、interview 同步
- 回归：全量 **3148 → 3203 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁全绿

### Stage M · M1 统一执行链：capabilities/tools → 顶层 tools/（2026-10-02）
- **[变更] 规范位落成**：16 个工具模块迁往 `agentix/tools/`——`gateway/`（executor / result_limiter / human_review / kg_gateway）、`adapters/`（search / notify_user / memory_tools / cron_tools / artifact_read / task_writes / plan_mode / integrity / tool_search）、**顶层**注册面（definitions / registry_init / selection + `__init__` 注册门面与 `get_tool` 粗闸）。注册三件套刻意不放 `registry/` 子包——公开单例 `registry` 与子包名冲突会让 `import agentix.tools.registry.x` 属性链解析到单例上（影子化地雷；§4 路线表本就只写「顶层 tools/」）；包内 import 全部改指新路径（切断旧路径依赖），`guardrails.py` **留驻** capabilities（M3 条目，新 executor 在 M6 前仍从旧路径引用）
- **[变更] 旧路径降级 shim**：16 个 `_TARGET` 转发文件（p36 模板，p66 `SHIM_DIRS_ALLOWED` 登记 `agentix/capabilities/tools`）+ 包门面 `capabilities/tools/__init__.py` 改 **PEP 562 惰性转发**（刻意不用 eager star-import：避免 agentix.tools 半初始化窗口重入 ImportError；`__all__` 与 `get_tool`/`registry`/`list_tool_specs` 等对象同一）；`infrastructure/tools` 老链收敛为 infrastructure → capabilities(shim) → agentix/tools，M6 一并清尾
- **[测试] 新增 `tests/test_p70_m1_tools_migration.py` 66 条**：目标位 19 文件存在 + 16 shim `_TARGET` 指向与 `_mod is` 对象同一 + 7 组代表符号同一 + 包行为（`__all__`/`get_tool`/`registry` 同一、双路径注册副作用、新家独立 `import agentix.tools` 不反向拖 `agentix.capabilities`、旧路径优先进程注册完整）+ guardrails 留驻断言；**守卫随实体迁移**：p40 `agentix.tools` 守卫由「禁止引用不存在模块」翻转为「import 目标必须存在」、p66 守卫 #3 扫描面扩 `agentix/tools`、code_hygiene 三枚 fix-marker 指新家、p36 `_TARGET` 白名单注记 M1 配对、p69 `COMPLETED_STAGES` 登记 M1（存在性翻转验证）
- **[文档]**：PROJECT_SPEC §11.1 目录组织重写（规范位表 + shim 链三层收敛）、§11.2–§11.5 路径全部指新家（行号不变——迁移逐字保留）、§2.4/§12.1/§11.8/§12 漂移点 shim 链说明、顶层包表新增 `tools/` 行；module-inventory / configuration / failure-recovery 路径同步
- 回归：全量 **3082 → 3148 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage M · M0 统一执行链：契约拆分 + 逐文件迁移清单暂停（2026-10-02）
- **[新增] 三个纯契约子包（蓝图 §3 目标树 / §4 M0）**：`agentix/domain/agent/`（`types.py` = Task/Plan/Observation/Verification/Memory 等，拆自 `domain/agent_types.py`；`ports.py` = Harness 七端口，拆自 `domain/ports/agent_core.py`）、`agentix/domain/runtime/`（`runs.py` = `RunState`+`TERMINAL_STATES` 拆自 control_plane；`steps.py` = `StepStatus`+`StepType` 拆自 `runtime/state.py`+`runtime/steps.py`；`budget.py` = `BudgetState`+`Budget`+`BudgetExceededError` 拆自 `runtime/state.py`+`runtime/budget.py`；`lifecycle.py` = `ExitStatus`+`ExitReason` 拆自 `domain/status.py`）、`agentix/domain/tool/`（`spec.py` = `ToolSpec`+`RISK_LEVELS`+`FORBIDDEN_RISK_LEVEL`+`CONTROL_REQUIRED_FROM`+`_strip_control_chars` 拆自 `capabilities/tools/definitions.py`；`risk.py` = `RiskLevel` 拆自 `human_review.py`；`permission.py` = `PermissionMode` 拆自 `hitl_bridge.py`）——三包零第三方依赖（stdlib/typing only）
- **[变更] 旧路径全部降级 re-export shim（对象同一，M6 清尾）**：`domain/agent_types.py`、`domain/ports/agent_core.py`、`domain/status.py`（`RequestStatus` 留存）、`runtime/state.py`（改顶部导入，中段散落 import 删除、TYPE_CHECKING 注释同步）、`runtime/steps.py`、`runtime/budget.py`、`application/services/control_plane.py`（改导入 + 未用 `Enum` 清除）、`capabilities/tools/definitions.py`（`ToolRegistry`/`registry` 留存，typed 拆走）、`capabilities/tools/human_review.py`（`ReviewDecision` 留存）、`application/services/hitl_bridge.py`（`__all__` 的 `PermissionMode` 不变）——全仓 import 面零改动
- **[测试] 新增 `tests/test_p69_stage_m_manifest.py` 51 条**：M0 规范位 12 存在 + 旧路径 shim 10 在位 + 新旧对象 `is` 同一（8 组参数化 + 2 组直断）+ M1–M6 逐文件迁移清单暂停（`MIGRATION_MANIFEST` 16 条目结构校验 + 无重复 source；`COMPLETED_STAGES` 翻转机制：已完成批断目标存在/删除批断源消失、未完成批断源必须还在——后续每批只许按清单搬家）；纯度守卫随契约迁新家：phase1 types/ports AST 改指 `domain/agent/`（+旧路径 shim 同一性）、p22 status 纯度拆为 `status.py` ∈ {enum, agentix} + `lifecycle.py` ∈ {enum}（+shim 同一性）、p17 结构/导出断言扩 `agent/runtime/tool` 三子包
- **[文档]**：PROJECT_SPEC §11.2（ToolSpec 规范位 `agentix/domain/tool/spec.py`，常量/`__post_init__`/`ToolRegistry` 行号刷新）、§8.3/§10.8（`TERMINAL_STATES`/`RunState` 指 `agentix/domain/runtime/runs.py`:15/:18，control_plane 侧 `AgentRun`/`ControlPlane`/`check_admission`/单例行号刷新，顺手修正 `SPAWNING` 幽灵值为实际 `pending` 枚举）、§4.2 标题标注规范位；`docs/architecture/接口与契约.md` §1.4 七端口标注规范位 + 旧路径 shim（保留 `agent_core` 对照锚点）
- 回归：全量 **3028 → 3082 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage B · B2b 统一执行链：33-P0 RAG 收敛后三项（R-P0-3/4/5，2026-10-02）
- **[变更] rerank 配置三态收敛（R-P0-3）**：`agentix/application/services/knowledge_gateway.py` 的 `RerankerCompatStub.enabled` 改 `None` 三态（None=未决，不再回落 True 假开）；`agentix/config/settings.py` 新增 `_deprecated_kb_rerank_score_threshold` validator——旧键 `KB_RERANK_SCORE_THRESHOLD` 映射到 `KB_POSTGRES_RERANK_THRESHOLD` 并告警（新键胜出，弃用键不再静默覆盖）；`.env.example` 弃用注释与 `docs/architecture/架构详解.md` 同步
- **[变更] Cypher 只读闸 + Neo4j 硬化最小集（R-P0-4）**：`agentix/application/agents/kg_sub_graph/kg_neo4j_conn.py` 新增公共件 `query_readonly/assert_readonly_cypher/find_write_clauses`——写子句在触库**之前** `ValueError` fail-closed（`WRITE_CLAUSES` 子串保守匹配，`graph.query` 零调用），`NEO4J_QUERY_TIMEOUT_SECONDS<=0`（默认 0.0）同步内联、调用形状与原缝逐位等价（位置参/`query=`+`params=` 关键字），`>0` 走 `asyncio.to_thread + wait_for` 超时抛 `RuntimeError`；五处执行缝接线（`text2cypher/execution/node.py`、`cypher_tools/utils.py` 软闸 block、`predefined_cypher/node.py`、`kg_gateway_bridge.py` 两 handler 硬闸）；schema 快路径 TTL 刷新 `NEO4J_SCHEMA_REFRESH_SECONDS=300`（锁内刷新，失败保留旧 schema + warning + 时间戳推进防风暴，reset 同步清时间戳）；`legal_retriever.py` dynamic 分类显式 `NotImplementedError(R-P0-4)`、`get_examples` 降级通用示例 + warning（静默 `_classify` 与 `type_based_examples` 删除）；两配置键五处同步（settings / `.env.example` / selfcheck `DEFAULT_IGNORED_MISSING` / 配置管理.md 表 / p41 名单，根 `.env` 照既有惯例不动）
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
- **[变更] PII 脱敏补齐（M-P0-3，规则只增不减）**：`redact_pii` 基线 4 条（PHONE/EMAIL/ID/CARD）不动 + 新增 11 条——中文地址 5（类别三区划起手/道路门牌/楼号室/栋室/小区房号）、英文地址 1（门牌+道路通名）、中文姓名锚点 2（`我叫…`、`姓名：…`——冒号锚点防测试文本误伤）、英文姓名锚点 3（`my name is/I'm/I am`，大小写敏感）；写路径全覆盖——`InMemoryMemory.remember`（原有）+ `LongTermMemory.store_summary`（summary/topics/key_facts）+ `SemanticMemoryStore.store_cards`（incoming + 存量 legacy 重写即清洗）；读路径二次兜底——`InMemoryMemory.recall`（正文 + `meta.conflicts` 标记行）、`get_context_for_query`（摘要/画像卡/语义卡区段）
- **[变更] 冲突键加固（M-P0-4）**：`resolve_conflicts` 键 `tags[0]` → **全 tags 排序集**（全集不同 = 不同事实不裁决）；同键按 `stored_at` **30 天时间窗**（`CONFLICT_TIME_WINDOW_DAYS=30.0`）分簇，窗外历史独立保留（各出胜者、互不覆盖）；窗内胜者（confidence 最高，同分取新、再同保首见）以 `meta["conflicts"]` **标记**被裁决值（item_id/content/confidence/stored_at——不覆写不删除不静默）；缺 `stored_at` 视为当下（兼容存量无时间戳数据）；纯函数 `dataclasses.replace` 不改输入
- **[变更] M-P0-5 3 scope 收口**：service 模块 docstring / README 记忆行 / PROJECT_SPEC §5.10 / agent_types docstring 统一「三 scope」口径（以 `MemoryScope` 3 值为准）
- **[新增] 验收：p67 守卫 30 条 + 离线评测**：新增 `tests/test_p67_memory_p0_convergence.py` 30 条（序列化只增/双卡冲突行同构、标记不覆写与同行累积、脱敏基线不减+中英姓名地址+零误伤+幂等+写读零留存、冲突键全 tags+时间窗+标记+纯函数、3 scope 文档一致、evals 报告键）+ `evals/memory_eval.py`（离线确定性零 LLM，报告键 `profile_stability=1.0 / conflict_precision=1.0 / privacy_leak=0`，不进门禁）；改写 `test_r3_memory_layers.py::test_merge_conflict_bumps_version` 为标记语义
- 回归：全量 **2951 → 2981 passed / 14 skipped / 0 failed**（2026-10-02）；`ruff check agentix tests` 全绿；81 条门禁（p41 33 + p35 26 + code_hygiene 12 + p46 10）全绿

### Stage A · A6 统一执行链：死代码批（51 文件）+ 分层守卫定稿 p66（2026-10-02）
- **[移除] 死代码批（A6-1，51 文件 + 15 空目录）**：全仓可达性扫描（AST import + 点号字符串提及 + 祖先包标记防伪死）零引用直删——text2sql/components 10 个 `__getattr__` 转发器；agentic_rag_agents 31 件（gather_cypher/gather_visualizations 4、summarize/prompts、text2cypher/models、tool_selection/models、visualize/{models,schema}、embeddings/{__init__,embedder_protocol}、exceptions.py、utils/{__init__,config}、ingest/ 与 cypher_examples 子包死件、dynamic_schema_retriever、neo4j_vector_example_retriever、ui/ 4、workflows 多 agent 两死子图、workflows/single_agent 整目录）；kg_sub_graph/{kg_builder,multi_tools}.py；infrastructure/reliability/retrieval 8 件（core/{hashing,middleware}、**tools/executor 死 shim（零 importer）**、reliability/release_gate、conflict_arbitration、infra/llm/{__init__,chat_client}、vectorstore/migration）；**排除清单显式不动**：agent/{observer,policy}×4（保留决策）、kg_states.py、kg_sub_graph/planner/{__init__,planner_node}、components/planner/unified.py（p63 活）、crawler、graph/ 占位、application/agents/main.py、KNOWN_BYPASS_TOOLS 五项
- **[变更] 文档与导出同步（A6-2）**：p41 三红清零——design-legal-domain M3 行改 plain path + A6 删除注、PROJECT_SPEC §7.5 single_agent 表删 chart/text2cypher 两行 + 注、§16.4 shim「8 个文件」→7 且示例改 definitions.py、§16.7 release_gate 改「A6 已删」；failure-recovery L1 路径改 capabilities；module-inventory 三处死 shim 陈述；current-tree 五处陈旧行；p40 `KNOWN_MISSING_DEPS` 清空；`application/__init__` 删 `prompts` 再导出、`infrastructure/__init__` 删 `knowledge` 再导出与 docstring
- **[新增] 分层守卫定稿 p66（A6-3，12 守卫测试化收敛）**：新增 `tests/test_p66_arch_guards.py` 9 条——**#11 LangGraph 白名单棘轮**（equality 断言，A6 后暂停 11 文件，M 批次迁图随评审收缩）、**#12 `_TARGET` shim 目录棘轮**（equality，2 目录，终态空集）、**#3 能力层零反依赖**（capabilities 对 agent/orchestration/policies 零 import，无白名单）、**#6 应用核心零客户端直连**（pymilvus/neo4j/langchain_neo4j/langchain_community/langchain_mcp/mcp；显式豁免前缀 `application/agents/` = M2 迁移对象、随迁收缩）、**#7 promote 只在离线进化**（`.promote`/`.run_regression` 调用点只在 agent/evolution/）、**#4 适配层无业务 DB**（agents/ 子树零 persistence/core.database import + SessionLocal 名零出现）**+ runtime 零 langgraph**、**#9 replan 有界步预算兜底**（RetryReplanner 反复触发但 max_steps=2 先触顶 → `budget_exceeded` 终态、`asyncio.wait_for` 防挂起）、**#1 跨路径单一平面**（chat + retrieval 双流同平面、agent_name 各异、run_id 互异、跨路径重复 spawn 拒绝）；#2/#5/#8/#10 分别由 p62/p65/p31+p61/p32+p33 承载（docstring 守卫映射表）
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
- **[新增] 统一 Planner 适配与影子对拍报告 `components/planner/unified.py`（A3b，对齐 v2 §4 A3 与 §8 风险对策「影子报告先行，golden 零抖动才切流」）**：`get_unified_planner()` 取用点（现役 `SingleStepPlanner` 零 LLM 确定性，与 `agent_loop_adapter` 生产口径一致；未来 LLM 规划器换装口，测试可 monkeypatch）+ `unified_decompose()`（`Plan` → 图内 `Task`，每 plan step 一个任务单元，step 空时与图内空回退同构单任务兜底）+ `PlannerShadowReport`（seq/question/driver/in_graph/unified/agree/error 暂停数据类）+ 有界报告队列（deque maxlen=200，淘汰后序号仍严格单调）+ `record_report`（strip 归一对拍；影子侧异常记 `error` 且 `agree=False`；不一致/异常/一致三档结构化日志）——**报告只记不改**
- **[变更] 图内 planner 节点三分支（`components/planner/node.py`）**：默认双 flag 关 = 图内私有链原样（含 ignore_node 空分解单任务兜底语义）；`UNIFIED` 切流 = 统一 Planner 驱动、私有链零 LLM 调用、驱动侧异常诚实抛出（与图内链失败口径一致，不静默降级）；`SHADOW` = **非驱动侧**并行运行落报告（in_graph 驱动时统一侧跑、unified 驱动时私有链反向跑，双向复用同一报告机制），异常只记不抛绝不影响输出；`ignore_node` 优先级最高（任何 flag 下不调 Planner）
- **[测试] 新增 `tests/test_p63_planner_convergence.py` 22 条**：flag 默认关与五处同步（含 TEXT2SQL_ENABLED 四面清除 + 活键不受影响）、flag-off 基线（图内链唯一调用方/统一零触达零报告/空分解回退/ignore_node 跨 flag 语义）、影子（驱动不变/不纠偏/影子侧异常不破输出）、切流（私有链零调用/空回退/驱动异常诚实抛/双开反向对拍/next_action 契约）、报告存储（归一对拍/error 强制不一致/有界淘汰 + 序号单调/clear 归一）、`unified_decompose` 取用点可替换
- 回归：全量 **2896 → 2918 passed / 14 skipped / 0 failed**（2026-10-01）；`ruff check agentix tests` 全绿；p41（含 2 新键）33 条、p35 26 条、code_hygiene + p46 随全量通过

### Stage A · A2 统一执行链：三闸有效化 + 五项署名旁路收编 + 记忆三工具（2026-10-01）
- **[新增] 三闸有效化（A2a，无新 flag）**：`get_tool_executor()` 构造点在 `TOOL_SELECTION_ENABLED` 打开时注入 `selection.build_tool_policy()` 与新函数 `selection.tool_permission_check()`——此前生产路径 `policy` 与 `permission_checker` **零注入**（Gate A/B 空转，v2 §1 出处）；flag-off `build_tool_policy()` 返 `None` ⇒ `ToolExecutor()` 零注入基线，注入故障按零注入回落不打挂单例；权限兜底 = `DISABLED_TOOLS` 执行侧（声明层裁剪的同名配置兜底）+ 策略黑名单/白名单/能力标签，checker 故障 fail-closed 语义不变；**Gate C 决策面**：`human_review` 新增 `mark_decision_surface()/clear_decision_surface()`、`get_review_gate()` 告警条件加 `not _decision_surface`——`hitl_bridge.install_executor_hook` 先宣告决策面再建闸门（HITL 桥 `bridge.decide` 即审批决策通道），消除「开了却无人可批」的误导性启动告警；`reset_review_gate`/`reset_hitl_bridge` 归零，test_p32「无决策通道必告警」两条契约原样通过；HITL 生产审批端点属 **A4** 本批不做
- **[新增] 两个 flag（默认关，五处同步 settings/selfcheck/`.env.example`/configuration.md 表+分组行+p41 名单）**：`TOOL_GATEWAY_ENFORCED=False`（五项旁路收编闸）、`MEMORY_TOOLS_ENABLED=False`（记忆三工具）——flag-off 节点直连/不声明不注册，与基线逐位等价
- **[新增] 五项署名旁路收编（A2b）**：新执行缝 `capabilities/tools/kg_gateway.py`——`run_governed(tool_name, args, direct=)`：flag-off 直连（同步/awaitable 均透传，行为逐位等价）、flag-on 经 `ToolExecutor.execute`（失败抛 `GatewayDenied`，节点既有 `except` 分支按普通执行失败处理）、flag-on 无 handler 告警回落直连（接线缺失不打挂节点）；六处执行缝接入：`kb_tools` search、`cypher_tools/utils` 与 `text2cypher/execution` cypher_query（执行侧写子句二次闸门前置不变）、`predefined_cypher`、`customer_tools` microsoft_graphrag_query、`text2sql_tool`（workflow 构造抽为 `build_text2sql_workflow()`，节点/handler 共用防漂移）；`application/services/kg_gateway_bridge.install_kg_gateway_handlers()` 装齐五 handler（graph/lightrag/工作流均 handler 内懒加载，`ensure_registered()` 先补 spec），`main._startup` flag-on 安装；`KNOWN_BYPASS_TOOLS` **保持五项不变**（flag-off 默认仍旁路、豁免继续说真话；flag-on 装齐后审计 `stale_exemptions` 亮起属预期灰度信号）+ 棘轮测试钉死只减不增；声明超时放宽 `text2sql_query=120s`、`microsoft_graphrag_query=60s`（收编后治理链超时闸按 LLM 工作流时长，cypher 两族保持 30s）
- **[新增] 记忆三工具（A2c）**：`capabilities/tools/memory_tools.py` 三个 schema（类名==工具名 title 契约）+ spec——`remember`/`forget` L1 + side_effect（`HUMAN_REVIEW_ENABLED` 时走 MEDIUM 审批半）、`recall` L0 只读，`capabilities=("memory",)`；`memory_tools_bridge.install_memory_tools()` 三 handler（缺 content / forget 无匹配键 → ValueError → executor failed 诚实；**无通配删除**：item_id/content 二选一必填）；`MemoryManager.forget()` duck-typed 扇出——**`MemoryPort` Protocol 不扩容**（runtime_checkable isinstance 契约不动），无 `forget` 方法的 provider 静默跳过，scope 不支持跳过、单 provider 失败 warning 继续；`LongTermMemoryProvider.forget`（hdel 精确 field/内容匹配，比对原文与脱敏文本）+ `InMemoryMemory.forget`（scope+subject 隔离，双键皆空返 0 绝不误清）；`registry_init` flag-on 追加三声明、`main._startup` flag-on 安装
- **[测试] 新增 `tests/test_p62_unified_gateway.py` 18 条**：三闸（flag-off 零注入基线 / flag-on 注入 policy+checker / 非法 `TOOL_SELECTION_MAX_RISK` 回落零注入 / 权限兜底全真值表 / `DISABLED_TOOLS` 执行侧 `permission_denied`）、决策面（标记抑制启动告警、安装置位/复位清零）、执行缝（flag-off 直连等价、无 handler 回落、flag-on 治理链路由 + `GatewayDenied`、`KNOWN_BYPASS_TOOLS` 棘轮基线暂停）、bridge（flag-off 零安装、flag-on 五 handler + 审计 `ok` + `stale_exemptions` 信号）、记忆三工具（flag-off 双排除、title 契约 + 风险分级 + capability、remember→recall→forget roundtrip、无通配契约、未声明 forget 的 provider 跳过）；flag-on 注册的 L1 spec 测试完按 p50/p60 模式撤出全局注册表（防 r1「注册表全 L0」顺序红）
- 回归：全量 **2878 → 2896 passed / 14 skipped / 0 failed**（2026-10-01）；`ruff check agentix tests` 全绿；p41（含 2 新键）33 条、p35 26 条、code_hygiene + p46 随全量通过

### Stage A · A1 统一执行链骨架：流式运行登记闭环 + 退出契约三件套 + Run API 硬迁移 v1 + 元数据死重剔除（2026-10-01）
- **[新增] 流式 run 登记与终态闭环（A1a，无新 flag，登记失败降级不断流）**：`control_plane.spawn()` 支持显式 `run_id`（缺省 uuid4，重复 ID 抛 `ValueError`）、`bind()` 单 owner（活任务改绑返回 False + warning，同 task 幂等，`done()` 后可改绑）；`chat_service._stream_events_impl` **首帧前**登记（spawn `"kg_graph"` + meta `{channel: stream, session_id}` + `bind(asyncio.current_task())` + `mark_running`；`AdmissionDenied` 透传 fail-closed，其余异常降级 `plane, run_id = None, None`）；新增闭包 `_finish_run(state, error)`（懒解析 `RunState` → `finish/cancel`）与 `stream_failure` 状态位；metadata 首帧**移入 try 内**（防首帧前断连泄漏 RUNNING）；尾部 `except asyncio.CancelledError → finish("cancelled","cancelled")` + re-raise、`except GeneratorExit → finish("cancelled","client_disconnected")` + re-raise（其间不 yield）、`except Exception → finish("failed", f"{type}: {e}")` 再产 error/done 帧；`finally` 兜底：`stream_failure` 非空 → `finish("failed", stream_failure)` 否则 `finish()`（默认 SUCCEEDED），并释放信号量——正常/断连/取消/异常/挂起五条路径均落终态，无 RUNNING 泄漏。
- **[新增] 退出契约三件套（A1b）**：`domain/status.py` 新增 `ExitStatus{COMPLETED="completed", INTERRUPTED="interrupted", FAILED="failed"}` + `ExitReason{COMPLETED, MAX_ITERATIONS="max_iterations", INTERRUPTED, ERROR="error"}`（str-Enum，可与裸串比较）；`LoopResult` 增只读属性 `exit_status/exit_reason/truncated`（`budget_exceeded → max_iterations + truncated=True`，`context_build_failed` 只是 reason 前缀、terminal 仍为 `failed`）；`TaskOutcome` 增同名三字段 + `__post_init__` 派生（**不覆盖显式传值**；ok→completed、retryable 耗尽→failed/max_iterations+truncated、非可重试→failed/error）+ `to_dict` 三键落地。
- **[变更] Run API 硬迁移 v1（A1c，无双路径别名）**：删除 `agentix/retrieval/api/routers/agent_runs.py`，新家 `agentix/interfaces/http/v1/runs.py`，include 于 `v1/__init__.py`（无前缀 → `/api/v1/agents/runs`，tag "Agent Runs"，L19-24）；鉴权改 router 级 `require_run_bearer`（严格 Bearer JWT + `safe_decode_access_token`，**不查库、无 Cookie 回退、默认拒绝 401**，与旧 `get_current_user` 口径一致，避免鉴权倒退）；错误信封保持顶层 `{request_id, error_code, message, user_hint?}`（handler 内直接返回 JSONResponse）；`agent_executors` 新增 `chat` 执行器（ask→`{output, meta:{executor/session_id/route/status/error_code/sources}}`，session 缺省 `uuid4().hex`）+ `register_executor("chat", ...)`；400 hint 改动态 `executor_names()`。文档八处同步：README §11 表、PROJECT_SPEC（§9.2 表删行/§10.6 段/§13.4 表/§13.5 对照）、`docs/runbooks/失败恢复演练.md`（新路径 + Bearer 头）、`docs/migration/模块清单.md:114`、`docs/design/generate_architecture_diagram.py`（p41 约束：旧路径在文档中仅纯文本或带删除表述）。
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
- **[文档] 阶段三同步（P1–P6 终态）**：README §11 测试真源 2463 → **2847**（2026-10-01 实测 + 逐批 delta 链 2463→2496→2542→2594→2668→2761→2847）；`现有目录树.md` 数字刷新（.env 241 键 / agentix 559 py / tests 150 模块 + services 补 7 文件）；`模块清单.md` §6 新增「优化批次 P1–P6」六批收口记录；`源码研读笔记.md` §4 对照矩阵更新为终态（职责 1/5、要求 1/2/3/5 升级，职责 7 加分项 Proactive/长期记忆 ❌/⚠️ → ✅ 默认关）+ §5 借鉴落点具体化到新文件 + 章节头标注已落地；`项目实现手册.md` §3.4 行号全量实测刷新 + 附 P1–P6 十五个新键独立表
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
- **[文档] 说明书/规划同步**：`编排方案选型论证.md`、`项目实现手册.md` 节点清单 8 → 9 并刷新 `lg_builder.py` 行号引用（build_graph :273 / add_node :287-298 / edges :300-303）
- 回归：全量 **2463 → 2496 passed / 14 skipped / 0 deselected**（2026-10-01）；`ruff check agentix tests` 全绿（`ruff format` 非本仓门禁）；p41 文档守卫随全量通过

### 项目说明书 · 调研全量输出 + Word 图文版（2026-09-30）
- **[新增] `docs/项目实现手册.md`**（297,972 字节 / 2,702 行 / §0–§22 共 23 章，p41 守卫 33 条全绿、154 处路径引用零缺失）：面向新人的实现级说明书——技术栈与分层架构、两套并行编排（L2 LangGraph 默认开 / L3 AgentLoop `AGENT_LOOP_ENABLED=False`）、AgentLoop 主循环与四终态、LangGraph 扁平主图拓扑、**双 SSE 链路辨析**（`/api/v1/retrieval/chat/stream` 检索域 RAG vs `/api/v1/chat/stream` LangGraph 图）、五层中间件、文档入库与索引、质量与发布门禁、端到端双链路对照；章节级 `文件:行号` 定位，事实基准 = 2026-09 源码实测
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
- **[新增] 触发器接线**：`LongTermMemory.refresh_profile_card`（只更新卡片不动旧计数器、无事件/flag-off 零写入）由 `memory_consolidation` handler 在 **flag 开且非 dry_run** 时调用并写回报告 `profile_card`（flag-off 报告形状与基线一致；dry_run 零写入约定不被破坏）；**读路径不隐式重放**（读零成本）；`get_context_for_query` 卡片区段仅 flag 开拼装（Gotcha 3：开启时点已文档化于 配置管理.md）
- **[配置] `MEMORY_PROFILE_CARD_ENABLED: bool = False` 五处同步**（settings/selfcheck/`.env.example`/configuration.md 表 + p41）；**执行注记**：原案 flag 名 `MEMORY_CARDS_ENABLED` 已被 R3-8 语义卡片占用（默认 true、不同特性）⇒ 按 Phase 28 正交开关先例改独立新名
- **[测试] 新增 `tests/test_agent_phase29_memory_cards.py`（20 条）**：flag-off 7 键精确等价/存储有卡不写盘/prompt 无卡片区段；roundtrip + 旧数据回退；冲突三值只标记不覆写 + fold 不突变输入 + 事件内去重计数 + 次序无关 + 增量 ≡ 重放幂等 + 空日志不伪造 + 容量 50 重放；handler flag-off 报告形状/dry_run 零写入/空日志无键/归档红线 flag 开用例；flag-on prompt 卡片段
- **[修复] `tests/fakes/redis.py`**：`zrange`/`zremrangebyrank` 负 stop 区间语义对齐真实 Redis（原实现 `stop=-201` 在小集合上误删全部索引，`store_summary` 真实调用链首次覆盖到）
- **[文档] 基线同步（2438 → 2458 passed / 14 skipped）**：README §11 / current-tree（137 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头 + §8 批三状态 + DoD 勾选（含 flag 撞名执行注记）/ module-inventory §6 勾选
- 回归：全量 **2458 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 干净 + 新文件严格 select 全过；`mypy agentix/domain` 0 错；Phase 24/R3-8/单测相邻记忆套件 72 条全绿

### Phase 30 · 进化闭环：knowledge/program 载体 + 提案三状态落盘 + 睡眠学习（2026-09-28）
- **[新增] 两载体扩展**（`agentix/agent/evolution/loop.py`）：`EvolutionCarrier` +`knowledge`/`program`（六值，Phase 12 断言同步）；门元组复用既有门键——KNOWLEDGE `("citation","scope_isolation")`、PROGRAM `("param_fidelity","verifier_logic_unchanged")`；payload 白名单校验 fail-closed：KNOWLEDGE 仅 `prompt_fragment/top_k/threshold/notes`（top_k/threshold 须数值）、PROGRAM 仅 `rules/notes` 且每条 rule 须声明式 `{id,when,then}`——越界即拒（`*_payload_keys_not_allowed:[...]`/`*_payload_type:*`/`program_rules_not_declarative`），触碰 `PROTECTED_VERIFIER_PATHS` 拒绝语义不变
- **[新增] 提案三状态落盘**：`proposal_sink` ctor 注入 + `_sink_record(proposal, state)` 五落盘点（校验拒/staged/回归拒/applied），去重键 `(content_hash, state)` append-only 幂等；`jsonl_proposal_sink(path)` 工厂（反斜杠归一、含 `evals/baselines/` 相邻段或空路径构造即 `ValueError`，fail-closed 不写盘）；无 sink 时内部状态与基线完全一致；导出 `agentix.agent.evolution`（+`KNOWLEDGE_PAYLOAD_ALLOWLIST`/`PROGRAM_PAYLOAD_ALLOWLIST`/`PROGRAM_RULE_KEYS`）
- **[新增] 睡眠学习 handler**（`task_handlers.handle_evolution_sleep`，`EVOLUTION_SLEEP_ENABLED` 默认关，五处同步）：flag 关 → `skipped disabled`；门槛（`min_signals=20`/`min_error_rate=0.1`）未达 → `no_op threshold_not_met`；dry_run 不占窗；端点预检（OPENAI_API_KEY/OPENAI_API_BASE 空 → `skipped no_endpoint`，先于 claim，no-op 路径不消耗窗口）→ 进程内 monotonic 幂等 claim（`force` 默认 False、window 86400，镜像 consolidation 语义）→ LLM temp 0.0 合成 → carrier 限 `knowledge|program` → `loop.propose` → `proposed/rejected` + staged/proposal_id/carrier + sink 记录；**绝不 `promote`/`run_regression`**；非法 JSON → `invalid_proposal_json:...` 不写档案；`EVOLUTION_SLEEP_PROPOSAL_PROMPT` 外置 `services/prompts.py`（静态指令在前、占位符在后，p23 规范）
- **[新增] `evolution_run.py --proposal-log`**（默认空=不落盘，stdout 与基线逐键一致）：设置时 sink 接线 + `report["proposal_log"]`；路径非法 → stderr + exit 2
- **[配置] 两 key 五处同步**：`settings.py`（`EVOLUTION_PROPOSAL_LOG=data/evolution/proposals.jsonl`、`EVOLUTION_SLEEP_ENABLED=false`）/ selfcheck `DEFAULT_IGNORED_MISSING` +2 / `.env.example` +2 行 / `配置管理.md` 表 +2 行
- **[测试] 新增 `tests/test_agent_phase30_evolution_carriers.py`（41 条）**：载体门/knowledge/program 白名单与拒绝 reason/sink 三状态 + `content_hash` 幂等 + 无 sink 等价/jsonl 路径守卫（正反斜杠与 `evals/baselines` 拒）/settings-selfcheck-env 四方一致/sleep handler 12 条（flag/门槛/dry_run/端点预检/claim 幂等/非法 JSON/禁晋升）/`--proposal-log` 3 条（默认 stdout 逐键等价/接线/坏路径 exit 2）；Phase 12 四载体断言改写六值
- **[文档] 基线同步（2397 → 2438 passed / 14 skipped）**：README §11 / current-tree（136 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头 + §8 批二状态 + DoD 勾选 / module-inventory §6 勾选
- 回归：全量 **2438 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 干净 + 新文件严格 select 全过；`mypy agentix/domain` 0 错；守卫批（p41/dependency/ci_gate/p35/p47/p40/p36/hygiene）162 条全绿

### Phase 31 · 评估二期：分层 golden + 声称一致性 + 敏感性/隐私 + Best@k（2026-09-28）
- **[新增] 声称一致性检查**（新 `evals/claim_check.py`，纯 stdlib 无时钟，p42 规范）：条号正则族（`第X条` 阿拉伯数字 + 中文数字 + `示例数据集 A/示例数据集 B/宪法…` 法名归一）抽取答案声称，与「实际 `sources` ∪ golden `expected_sources`/`expected_citations`（人工真值并入证据池）」比对 → `ok/mismatch/no_claims/missing_prediction`；无 trajectory 行诚实 `applicable=false` 不计分；报告含 `checked/ok_count/mismatch_count/mismatch_ids/note`（**只标记不判分，mismatch 需人工复核**）；与 `run_eval.py` 互不反向 import（脚本直跑顶层名 / pytest 走 `evals.claim_check` 双路径导入）
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
- **[安全] A2 上传读/删鉴权（写读不对称收口）**（`v1/upload.py`）：GET `files/images` 补 `require_v1_user`（flag 开无凭证 → 401，与写同口径——原「渲染面读豁免」契约翻转，见 `架构审计-2026-09-28.md` §1/§3 实施注记）；上传落 owner sidecar（`UPLOAD_DIR/.owners/<name>.owner`，独立目录防被读走）——读/删比对归属，跨身份/匿名读有主文件 → 404，无 sidecar（历史/匿名文件）放行；DELETE 跳过 sidecar 目录并同步清理
- **[安全] A3 限流口径统一**（`infrastructure/core/ratelimit.py` + `retrieval/api/routers/chat.py`）：`rate_limit_dependency` 停写共享 `limiter.config`（原并发互踩地雷），改 `_limiter_for` per-params 派生缓存（同参数复用、异参数隔离、flag 关零开销）；retrieval `_chat_limiter` 由 import 期绑定改惰性热重建（`rate/burst` 签名变化即重建，运行期改配置即时生效）
- **[测试] 新增 `tests/test_p48_ownership.py`（21 条离线归属契约）**：跨 user 404 / 同 user 200 / 匿名现状不变 / header 覆盖 user_id / 匿名桶可见 / JWT regime（401、sub 身份、头不可伪造）/ 写路径先拦 / 上传 sidecar 读门禁与删除隔离——会话 CRUD 走内存替身、chat 走脚本 stub、上传走 tmp 目录，零 DB/零网络；`test_rate_limit_middleware.py` 扩 4 条 A3 用例；`test_v1_endpoint_auth.py` 上传读契约翻转 + 补 JWT 过鉴权进归属层用例
- **[文档] 基线同步（2303 → 2329 passed / 14 skipped）**：README §11 / current-tree（133 模块）/ Agent_Core 头 / Phase15-20 头 / Phase21-26 头 / Phase28-31 头+§8 状态 / module-inventory §6 勾选 / `架构审计-2026-09-28.md` §2 P0 移入 §1 + §3 批次 A 标记完成
- 回归：全量 **2329 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 干净 + 严格 select 全过；`mypy agentix/domain` 0 错

### 优化迭代 Phase 28–31 立项（2026-09-28）
- **[文档/立项] `docs/migration/Phase28-31_优化规划.md`（v1.0）**：依据《Agent 全书笔记》对照 + `架构审计-2026-09-28.md` 分期拍板四主题——**批一** = 前置审计批次 A（A1 会话/历史归属 IDOR 校验、A2 上传读鉴权、A3 限流口径；安全修复默认开）+ Phase 28 自主护栏（`CLARIFY_ROUTING_ENABLED` 确定性澄清决策挂 handler 函数体不加图节点 + 状态栏 `max_iter` 常量键/轨迹 append `budget_notice` 预算可见性）∥ Phase 31 评估二期（golden 只增 `difficulty`/`source` 分层 + 新 `evals/claim_check.py` 声称一致性 + `--sensitivity` 提示敏感性 + PII 离线 `privacy_flagged` + `best_at_k`，全部报告键只增不进门禁）；**批二** = Phase 30 进化闭环（`EvolutionCarrier` +`KNOWLEDGE`/`PROGRAM` 载体与回归门、提案三状态 append-only 落盘 `data/evolution/proposals.jsonl`、`EVOLUTION_SLEEP_ENABLED` 睡眠学习 handler 只产提案不晋升）；**批三** = Phase 29 记忆结构化（`MEMORY_CARDS_ENABLED` JSON Cards + `derive_profile` User-as-Code 派生幂等）；缺口 10–12 另期；批次 B/C 随批穿插
- **[文档]** `模块清单.md` §6 新增「优化迭代 Phase 28–31」立项勾选节 + `Agent_Core_2.0_规划.md` §9 头注/状态表同步

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
- **[文档] 基线同步**：README §11 / current-tree / Agent_Core 头+阶段表 / Phase15-20 头+验证段 / Phase21-26 头 / module-inventory §6；新增 `docs/architecture/架构审计-2026-09-28.md` 架构审计（可优化点 + 下一步方案）
- 回归：全量 **2303 passed / 14 skipped / 0 deselected**（2026-09-28）；ruff 主检 + 严格 select 全绿；`mypy agentix/domain` 0 错；`vue-tsc` 0 错 + vitest **113** 全绿

### Phase 27 执行：死代码清理 + 文档收尾（2026-09-27）
- **[移除] `search_service` 全家**（evolution-assessment §4「标记 → 删除」两步流程的删除步，同批执行经拍板）：删 `agentix/application/services/search_service.py`（`SearchService` 类 + `generate_stream`）、`agentix/application/prompts/search_prompts.py`、空包 `agentix/application/prompts/`——依据：`SearchService` 全仓零 importer（含动态引用）、`generate_stream` 零生产调用、5 类事件 `search_start/search_results/direct_answer/direct_content` 零前端/测试引用；**只删 `generate_stream` 会留下零调用的 `_call_with_tool`/`search_tool`/prompt 孤儿（制造新死代码），故整体删除**
- **[测试] 防复活守卫**：`test_p40_import_health.py::test_deleted_search_service_stays_deleted`（同 `text2cypher` 先例：删除是审计结论，恢复前必须先给出调用方）；`test_previously_broken_modules_now_import` 守卫范围随删收缩（docstring 记录去向）
- **[同步] 引用清零**：`services/__init__.__all__`、`settings.py` DISPLAY_TZ 理由注释（删 cur_date 示例弹）、`.pre-commit-config.yaml` trailing-whitespace 排除表、`docs/architecture/扩展机制.md` Prompt 位置、`现有目录树.md` 目录树、`依赖关系图.md` 工具簇消费者、`架构演进评估.md` §4（处置列 + 保留项判定）/§8（Phase 24–26 完成、死代码已清理）
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
- **[文档/新增] `docs/migration/架构演进评估.md`**：依赖方向/分层偏离/双通道 SSE/零调用面（`search_service.generate_stream` 0 调用、`/v1/sessions` 前端 0 调用）/事件矩阵/框架候选「均不动」/遗留优先级
- 守卫复跑：p41+p35+code_hygiene+p36+p17+p40 = 114 passed

### Phase 22 执行：上下文二期——缓存纪律 + L4 auto-compact（2026-09-27）
- **[新增] 工具序稳定（KV Cache 纪律，Ch2 §2.4）**：`ToolRegistry.freeze()` 注册序快照（幂等、`delete` 不受限、`unfreeze` 供测试）——暂停后 `register` **告警拒绝**（Gotcha：运行期动态注册属违例，写测试锁定）；`schema_prefix_hash()` 测试辅助（canonical JSON over schema 序列 + sha256，同配置两次一致；`CONTEXT_AUTOCOMPACT` 开关不进哈希/前缀）；**生产自动触发放延后**（`build_graph`/`_startup` 锚点会跨测试暂停全局 registry），显式 API + 测试先行，落地注见规划 §3
- **[新增] 配置变更 append `system_notice`**（Ch2 §2.4）：`application/services/config_watch.py` — 非 secret 配置 hash（键名含 key/secret/token/password/credential/dsn 即排除）首调记基线 → 运行中变更向轨迹 `record(kind="notice", name="system_notice")`，**detail 只含事件/变更键名/哈希前缀、不含任何配置值**（Gotcha 3 无 PII）；接线 `_ask_impl` 与 `_stream_events_impl` 入口（自吞异常），**静态前缀字节不变**
- **[新增] L4 语义压缩**（Ch2 §2.7 分层压缩最后一档）：`domain/ports` 新 `CompactorPort`（零第三方 Protocol）+ `agentix/agent/context/compaction.py`（**五要素机制断言** `missing_elements`：trace_id/错误记录/约束 key/来源引用/任务状态字段 token 级保留，缺一弃用；`maybe_compact` 单次尝试 + 任何失败返回 None）；接入 `ContextEngine`（`trim_policy` 同层 `compactor` 参数）与 `SimpleContextBuilder`——L1–L3 后仍超预算才触发，**flag off 与 Phase 18 逐字节等价**，压缩失败/断言拒绝/仍超限 ⇒ 确定性丢弃循环回退；生产实现 `application/agents/compactor.py::LLMCompactor`（`llm_factory` 角色 `summarizer` 零温度）经 `agent_loop_adapter` 组合根 **flag-gated** 装配（默认 None 零行为变化；离线 record/eval 永不构造）
- **[测试]** 新增 `tests/test_agent_phase22_context_cache.py` **19 用例**（DoD 全项：schema 哈希两次一致 + flag 不进前缀/指令、暂停后注册被拒+告警+unfreeze 恢复、config 变更 `system_notice` 仅键名无值 + 前缀字节不变、L4 off 双 builder 逐字节等价且压缩器零调用、L4 on 五要素保留/坏压缩器拒绝回退/异常回退/仍超限终止、`missing_elements` 每类丢失检出 + 空真通过）
- **[变更] p17 守卫 `ports.__all__` 清单增 `CompactorPort`**（domain 端口集合唯一同步点）
- **[文档]** `Phase21-26_规划.md` §3 DoD 勾选 + §7 状态 + 落地注（freeze 生产触发延后决策）；`模块清单.md` §6 Phase 22 勾选；基线同步 README/current-tree（128 模块）/Agent_Core（§9 加 Phase 22 行）/Phase15-20/规划头部
- 回归：全量 **2183 passed / 4 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select（源码）全绿；`mypy agentix/domain` 0 错（前端未触及）

### Phase 21 执行：Skills 渐进披露 + Hooks 生命周期（2026-09-27）
- **[新增] Skills 两段式加载**（书 §2.6/§4.7；flag `SKILLS_ENABLED` 默认关 + `SKILLS_DIR="skills"`，三 key 五处同步 settings/selfcheck/`.env.example`/configuration.md/p41）：`agentix/agent/skills/` — `loader` 扫 `skills/**/SKILL.md` 扁平 frontmatter（name/description/anti_when **三键必填**，缺项告警跳过、重名 first-wins、控制字符名拒绝）；`registry` 懒加载单例 + `read_skill`（**name 不拼路径**，只读扫描期存下的 path + `relative_to(root)` 纵深防穿越，正文 8000 字符截断、未知名字回友好回执+可用清单）；`build_instructions` flag-on 注入元数据目录（**正文永不进前缀**；flag-off 字节等价且不 import skills 包，`skills_enabled` 参数供测试覆盖）；`read_skill` 声明进 `registry_init`（flag-gated，进 `bind_tools`）+ handler 组合根 `install_read_skill_tool()`；示例 `skills/release-checklist/SKILL.md`
- **[新增] Hooks 5 事件**（flag `HOOKS_ENABLED` 默认关）：`agentix/agent/hooks/` — `events`（on_tool_before/after、on_step_end、on_run_end、on_error）+ `registry`（**仅同步 callable**、注册序触发、逐个 try/except 隔离、`emit` 首行 gate settings 永不抛）；`ToolExecutor.set_observation_hooks` 中性回调口（capabilities 零反向依赖，事件语义在 agent 侧绑定）——**闸门全放行后才 before**（Gotcha 3：与 HITL hook 正交有序，被拒/unknown 尝试不观察）；`loop.py` 四相位 `on_error`（context/plan/step/verify）+ `on_step_end`（观测入列后）+ `on_run_end`（`run→_run_inner` finally 恰好一次）；`main.py` 组合根 flag-gated 接线（同 Phase 16 HITL 模式）
- **[新增] `ToolRegistry.describe_groups()`**：按首个 capability 标签分组（无标签归 `general`），组序/组内均为注册序、两次输出稳定——供 planner/文档的元数据目录第一层
- **[变更] `ensure_registered()` 改逐项补缺**：原「首项已注册即整体 return」会漏掉 flag-on 后增量声明的 read_skill；现按 spec 逐个补注册（幂等语义更强）
- **[测试]** 新增 `tests/test_agent_phase21_skills_hooks.py` **17 用例**（DoD 全项：flag-off 指令区/工具表字节等价、目录无正文、缺 anti_when 跳过并告警、穿越拒绝（名字层 + 伪造注册项纵深）、hook 顺序+异常隔离+flag-off 零调用、闸后 before/after 配对+故障不改结果、loop 四相位/step/run 事件、describe_groups 稳定完整）
- **[文档]** `Phase21-26_规划.md` §3 DoD 勾选 + §7 状态 + 决策表/清单措辞对齐落地事实；`模块清单.md` §6 新增 Phase 21–26 小节；基线同步 README/Agent_Core/current-tree/Phase15-20/规划头部
- 回归：全量 **2164 passed / 4 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select（源码）全绿；`mypy agentix/domain` 0 错；`vue-tsc` 0 错 + vitest 92 全绿（前端未触及）

### 下一版规划立项 + 项目定位表述统一（2026-09-27）
- **[文档/新增] `docs/migration/Phase21-26_规划.md`（v1.0）**：基于《Agent 全书笔记》分章版 11 文件对照立项——Phase 21 Skills+Hooks / 22 上下文二期（缓存纪律+L4）/ 23 交互二期（出站通知+Redis Stream）/ 24 记忆知识深化 / 25 multi-agent 运行时防线 / 26 评估三期+Phase 17 解阻清单；分批已拍板（批一 = 21+22），`Agent_Core_2.0_规划.md` §9 加指向行
- **[文档] 项目定位表述统一**：统一口径为「**以 Agent 内核 + RAG 等多路检索为核心的综合服务平台**（领域无关，测试数据仅为参考领域之一）」——修 README §1 概览句、`architecture/README.md` 一句话架构（原「可迁移垂直领域智能客服框架」陈旧）、`扩展机制.md` 定位句、`测试数据域-产品需求.md` 产品目标（原误把 PRD 领域等同项目整体）、`Phase21-26_规划.md` §4；CHANGELOG 历史条目与 ADR 0001 决策上下文保持原样（历史记录不改写）

### 审查项收尾：逐端点鉴权 + 契约接线 + progress/命名收敛（2026-09-27）
- **[安全/新增] v1 逐端点鉴权**：新配置 `V1_REQUIRE_JWT`（默认关；settings / selfcheck / `.env.example` / 配置管理.md / p41 反向清单五处同步）→ `interfaces/http/v1/deps.py::require_v1_user` 接 **chat / sessions 全部路由** 与 **upload 三个写端点**（GET 文件/图片读取豁免——渲染面取不到 Authorization 头）；**events / ops** 接线既有 `verify_api_key`（`REQUIRE_API_KEY` 归属，为未挂全局中间件的装配面兜底）；两开关正交；`tests/test_v1_endpoint_auth.py` 12 用例（轻量 app 无中间件直测端点级依赖：缺/坏/好 token、错/对 x-api-key、豁免面、正交性）
- **[修复/UX] ChatWidget 消费 `ChatResponse.status/error_code`**：新增 `chat/api/chatContract.ts`——`extractChatError` 覆盖三种错误体形状（FastAPI `detail` 字符串/对象、AppError `{error_code,message,user_hint}`、APIKey 中间件 `{error,message}`）+ 422 数组 + 网络错误；`decorateDegraded` 给非 ok 状态追加可见提示（含 ErrorCode 中文文案与 trace_id）——降级兜底不再被当正常答案、4xx/5xx 不再一律固定文案；vitest 14 用例
- **[接线] sessions/history 契约**：ChatWidget 持久化 `session_id`（localStorage）→ 刷新后 `GET /api/v1/chat/history/{id}` 恢复历史（服务端 404 则自动弃旧 id 重开）；「重置会话」接 `DELETE /api/v1/chat/session/{id}` 清服务端消息——先前前端对 v1 sessions/history 契约**零调用**，刷新即丢历史
- **[接线] SSE progress 事件**：retrieval 编排层三处**真实边界**补发 `type=progress`（`loading` 准备会话 → `retrieval` 检索预检 → `llm_generate` 出答，带 `stage/message/detail{step,total}`，router `type→event` 直通）——前端 progress 分支从死代码变实链；**不虚报** sparse/dense/rrf 阶段（后端无此划分，标签表补 `retrieval`）；`test_retrieval_progress_events.py` 2 用例（含拒答路径只发 loading）
- **[收敛] executor 命名**：`模块清单.md` §2 新增 **Executor 对照表**（`MappingStepExecutor` / `ToolExecutor`+shim / `Executor` 别名 / `StepExecutorPort` / 已删 `AgentExecutor`，并钉死 `classify_exception` 归属）；修 `面试十问.md` 把 `classify_exception` 张冠到 capabilities 侧的史实错误；4 个测试文件统一改走 `capabilities.tools.executor` 规范 import 路径（`infrastructure` shim 保留）；**删除死代码 `AgentExecutor`**（`services/breakpoint_resume.py`，137 行，全仓零调用）+ inventory/dependency-map 符号锚点同步（原同名三件套混淆源清除）
- 回归：全量 **2147 passed / 4 skipped / 2 deselected**（2026-09-27）；ruff 主检 + 严格 select 全绿；`vue-tsc` 0 错 + vitest 92 全绿

### 接口审查第二轮：安全就绪 + 契约补齐（2026-09-27）
- **[安全] `GET /api/v1/sessions/` 的 `user_id` 改必填**：原可缺省 → 无鉴权下任何人可无条件枚举**全量用户**的会话时间线；现缺参 422（FastAPI 校验先于 DB，离线可测），带参行为不变
- **[修复] `DocumentData` 补 `updated_at`**：`Document` 模型有列但 DTO 漏带 → 前端 `updated_at` 恒 `undefined`；`_to_doc_data` 单点补齐 + schema 加可选字段，与前端 `documents.ts` 的 `updated_at?: string` 对齐
- **[新增] 前端 `x-api-key` 就绪**：`web` 新增 `API_KEY`/`apiKeyHeader()`（读 `VITE_API_KEY`，未配置时零行为变化），注入全部请求触点——knowledge `http` 拦截器、chat/knowledge 两路 SSE fetch、`ChatWidget` 上传 + chat POST、`useKbPersonalUpload` 跨域直连兜底两处（顺带回补缺失的 Bearer）；`Vite` 代理下 VectorDbView 走 `http` 实例自动携带。开 `REQUIRE_API_KEY=true` 前的前端前置就绪（键打进产物，仅作门禁钥匙，机密仍由 JWT 承载）
- **[决策] 死代码保留不删**：`agentix/agent/observer/`、`agentix/agent/policy/`（`InMemoryObserver`/`LoopBudget` 全仓零调用方）按 `模块清单.md` §1.1/§1.3 记录的 Phase 2 规划骨架保留——文档引用其路径，删除会破坏 p41 路径守卫与规划史；待对应 Phase 启动时接线或正式移除
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
- **[文档]** 规划 §4–§11 勾选/执行状态 + module-inventory §6；Phase 17 全栈标 **blocked**（本机无 Docker/LM Studio，**拒绝伪造 baseline**）；配置管理.md + p41 反向清单补 `EVENT_PORT_BACKEND`
- 回归：全量 **2118 passed / 4 skipped / 2 deselected**（2026-09-26）；p17/p22/p35/p41/code_hygiene 全绿；ruff 主检 + 严格 select 全过；`mypy agentix/domain` 0 错

### Phase 15–20 深化规划 + Phase 15/17 执行（2026-09-24）

- **[文档]** 新增 `docs/migration/Phase15-20_深化规划.md`：对照《Agent 全书笔记》+ Claude Code/Codex 公开架构，定义 Phase 15（点亮 Harness）–20（进化点亮）范围、依赖、DoD 与风险；已拍板：flag `AGENT_LOOP_ENABLED` 默认关、stream 仍走 L2（决策 A）、拒绝手工伪造 eval baseline
- **[新增] Phase 15 点亮 Harness**：`AGENT_LOOP_ENABLED`（settings/selfcheck/`.env.example` 三处同步）+ `application/services/agent_loop_adapter.py`（TaskSpec / 工具桥 / LoopResult→图 dict / observations→轨迹）；`ChatService._ask_impl` 在 flag 开时于同一 `_AGENT_SEM`/`AGENT_TIMEOUT`/`runtime.astep` 包裹内跑 `AgentLoop`；**stream 恒 L2**；flag-off 与原路径等价
- **[新增] Phase 17 在线信号（离线部分）**：`OnlineLoopRecorder` 单例 + `get_online_loop_recorder`；`chat_service` 成功 / AgentError / 内部错误三处 `record_request` 漏斗旁路零写 `record(...)`；轨迹包导出强制 `evals/baselines/traces/` 子目录（避开 `quality.json` 碰撞）；`evals/README` §9 / runtime-guardrails §12.5 / 测试分层策略.md §4 写明武装顺序（全栈 record → 本地播种并 **手动 commit** quality.json——CI 建档不持久）
- **[测试]** `tests/test_agent_phase15_wiring.py` 18 用例（flag-off 等价、flag-on smoke、stream 仍 L2、三漏斗在线信号、轨迹桥、零写签名）
- **[变更] 文档数字**：README §11 / current-tree / 规划 §9 基线对齐 **2000 passed / 4 skipped**（2026-09-24）；module-inventory §6 勾 Phase 15 + 17 离线
- 回归：全量 **2000 passed / 4 skipped**；p17/p22/p35/p41/code_hygiene 全绿；ruff + mypy 全过

### Agent Core 审查纠错 + README 深化（2026-09-24）

- **[修复] `FailureMode.Livelock` → `LIVELOCK`**：枚举成员命名规范 + 去掉事后 `_value_` hack；`stop_on_contradiction=False` 时 `max_rounds` 终态不再丢失 `contradiction` 标记（`reason=contradiction_unresolved`）
- **[修复] Event 总线持久化**：`InMemoryEventBus.to_persistence` 的 dedup 表由 tuple 键改为可 JSON 序列化字符串键，补 `from_persistence` 往返（历史 `json.dumps` 直接 TypeError）
- **[修复] HITL Sidecar**：`EDIT` 裁决在注入 approver 时同样先问 approver（fail-closed，防「改参放行」绕过审批）
- **[修复] Replanner**：自定义 planner 返回与已观测同 id 的 step 时重映射，避免被 `_next_pending` 跳过导致空转
- **[修复] Event 虚拟身份往返**：`InMemoryEventBus.from_persistence` 此前忽略 blob 内 `identity`，每次恢复都铸新身份（`identity_id`/scopes/`credential_ref` 静默丢失）；现从持久化还原，显式 `identity=` 参数仍优先
- **[修复] 清理**：`agent/` 与若干遗留 F401/F841 死代码；`domain/evals` 补 `aggregate_run_costs`/`SIGNIFICANCE_Z_*` 进 `__all__`；`研究型Agent演示.md` 修正 `SimplePlanner` 错误引用
- **[变更] README**：新增 §4 Agent Core 2.0（Harness/HITL/事件/多智能体/演化/端口）；测试真源 **1982 passed**（2026-09-24，Python 3.10.20）；HITL 口径对齐 L1+（MEDIUM+）；目录重编号 §1–§12；文档表补 agent-core-layers / migration / demo
- **[文档]** 迁移清单与 runbook 中「README §10」引用改为 §11；`current-tree`/`module-inventory` 基线数字对齐 1982；规划 §9 执行状态收口为 Phase 0–14 完成；Phase 10 消融口径按 `variants.json` 记为 A–G（原文 A–H 的 H 无定义）
- **[清理]** `--select F401,F811,F841` 全仓清零（evals/kb_ingest/scripts 死 import 与未用赋值）
- 回归：Event 持久化 JSON + identity 往返、EDIT×approver、contradiction 终态、自定义 planner step 重映射；全量 **1982 passed / 4 skipped**

### 求职收益优化批次（Agent Runtime/MCP/评测/Trace UI/README，2026-09-22）

- **[#P1] 任务级 Agent Run API**（`16252c5`）：Control Plane HTTP 化——`POST/GET /api/v1/retrieval/agents/runs`、`GET .../runs/{id}`、`POST .../runs/{id}/cancel`；执行器注册表（内置 probe 回显执行器）；超时统一 `asyncio.wait_for`、取消幂等、步数预算挂 TokenBudget；错误码 `AGENT_RUN_NOT_FOUND`/`UNKNOWN_AGENT`；9 用例
- **[#MCP] MCP 一等公民**（`871f393`）：零依赖 stdio server（MCP 2024-11-05 子集：initialize/ping/tools/list/tools/call）+ 客户端（握手/调用/超时保护）+ ToolExecutor 桥接适配（远端工具 L0 只读建模入统一执行器，`mcp_` 前缀防撞名）；E2E 子进程测试；18 用例
- **[#评测] Ablation 脚手架**（`469d419`）：`evals/ablations/`——A-G 七变体定义（裸 LLM → Full Agentix，开关单调叠加）+ 在线编排器（`AGENTIX_ABLATION_*` 环境注入 run_eval --live）+ Benchmark 表聚合（离线可跑，缺数据变体占位）；3 用例
- **[#P0] Trace UI**（`0a9a3f1`）：前端执行轨迹时间线——流式事件折叠（工具/阶段/生成/完成/异常，相邻去抖），消息内「执行轨迹」折叠面板实时呈现；纯函数 + vitest 5 用例；vitest 全量 78 / vue-tsc 0 错
- **[#文档] README 减肥**（`43d29d0`）：762 → 253 行，按 What/Why/Runtime/Knowledge/Reliability/Evaluation/Observability/Quick Start/Engineering 重构首页；架构细节迁 `docs/architecture/架构详解.md`、数据准备迁 `docs/DATA_PREPARATION.md`；定位调整为「Agent Platform · 测试数据领域参考实现」
- **仓库文件夹重命名**：`Legelbot-develop` → `My-Agentix`（本地路径与品牌一致；源码零硬编码路径依赖，仅外部辅助脚本路径更新）
- 测试基线：**1418 passed / 7 skipped / 0 failed**（+30）+ vitest 78 / vue-tsc 0 错

### My Agentix 迁移批次（P0-P2，2026-09-21/22）

- **[#P0] 重命名迁移**（`b0b7e08`）：`legelbot` → `agentix` 包名全量迁移（344 个 .py + pyproject/Makefile/compose/Dockerfile/CI/kb_ingest/evals/web），品牌文案升级「My Agentix」；`STATIC_PREFIX_VERSION/PROMPT_VERSION` 经 content_version 动态滚动。保留：前端 localStorage 键 `legelbot:access_token`（避免强制全部用户重登）与存量本地库名。644 文件，独立 commit 可 revert
- **[#P1] 可部署收口**（`bcebc6e`）：Dockerfile 多阶段重构（builder 编译工具链与运行时分离）；compose healthcheck 补齐 13/13（retrieval-worker=celery ping / etcd=etcdctl / kb_ingest=/health）；`LIGHTRAG_INIT_LIMIT` 缺省修复；`.env.development/staging/production.example` 三模板；发布与回滚 Runbook（`docs/runbooks/发布与回滚.md`，五层验收链 + 四级回滚）
- **[#P1] 可测试+可评估收口**（`0d71a3f`）：CI 新增 `eval-offline` 作业（离线评估 + 四指标质量门禁，预测基线缺失优雅跳过）；`docs/testing.md` 五层测试体系；`evals/baselines/quality.json` 建档
- **[#P2] 可观测+可解释收口**（`b8c95b8`）：业务指标 `grounding_low_score_total` / `gateway_fallback_total`（grounding 低分与 Gateway fallback 接线）；**Explanation API v1**（`GET /api/v1/retrieval/answers/{id}/explanation`，聚合问题/证据/引用/模型，未持久化块显式 not_recorded，零 schema 变更）；告警 +2（grounding 低分突增 / fallback 连锁）；SLO 目录（`docs/runbooks/SLO目录.md`）
- **[#遗留] AppError 全局转换器**（`1ab5d3c`）：修复检索域业务错误一律 500 的平台缺口——`AppError` 现按 `ERROR_CODE_TO_STATUS` 映射语义化状态码（401/404/409/429…），响应体与 `response.error()` 标准结构一致；非 AppError 异常仍收敛 500（契约不变）
- **[#遗留] X3 过程折叠面板**（`1813a1c`）：前端 assistant 消息下「过程」面板——懒加载 Explanation API 展示检索证据（相关度 + 有效性徽标）/模型/未记录块占位；`not_recorded` 不编造
- **文档与整理**：README 全面刷新（Milvus 统一口径、测试分层、项目结构、GitHub 链接修正）；品牌资产更名（agentix-logo/icon.png）；清空遗留 `backend/` 空壳目录
- 测试基线：**1388 passed / 7 skipped / 0 failed**（后端）+ vitest 73 / vue-tsc 0 错（前端）

### Round 3 续：Semantic Memory 卡片层（P2，2026-09-21）

- **[#R3-8] Semantic Memory**：`application/services/semantic_memory.py`——§29-33 四层收口：Working→AgentState（R3-1/R3-3）、Episodic→MemorySummary（既有）、Semantic→**本模块**、Procedural→留待（无真实场景，决策记录）。卡片管线全链（§32）：规则式 Extractor（零 LLM，从**会话摘要**提取偏好/指示卡——遵守"不把每句对话存进去"纪律）→ Dedup（同内容 confidence 取 max）→ Conflict（同类型互为包含 → version+1 覆盖）→ Redis hash Store。接线 `LongTermMemory`：`store_summary` 时提取（生产者）、`get_context_for_query` 注入"用户稳定偏好"区段（消费者）；`MEMORY_CARDS_ENABLED` 默认开但挂 `MEMORY_CONTEXT_ENABLED`（默认关）之下——整体默认零行为变化。16 用例

### Round 3 续：Model Gateway（P2，2026-09-21）

- **[#R3-7] Model Gateway**：`infrastructure/llm/model_gateway.py`——补齐 §42-44 三缺口（Strangler Pattern 叠加在 llm_factory 之上，不推翻）：① **Model Registry/Router**（§43）：任务分档 fast/standard/reasoning → `resolve_model`（`LLM_MODEL_FAST/STANDARD/REASONING` 显式配置优先，空 = 回退主模型——单模型部署零行为变化）；② **Fallback 执行器**（§44）：`ainvoke_with_fallback` 按 primary → 链逐模型尝试，`LLM_FALLBACK_MAX_ATTEMPTS` 硬上限防成本爆炸，GatewayResult 携带 model_used/attempts/errors 归因；**分层纪律：Gateway 不构建 client 不发起调用**——`build_client`/`invoke` 由消费点注入，保证 fallback 客户端与主链路同构（同 base_url/extra_body/计量/熔断）。接线 `query_knowledge_base`（`LLM_FALLBACK_MODELS` 空 = 默认零行为变化；非空时 primary 失败 → 链内兜底，熔断/重试语义不变）。vision/embedding 不重复建档（已有 RERANKER_MODEL/向量库 embedding 独立配置）。14 用例

### Round 2 收口：有效性过滤覆盖 Neo4j 路径（2026-09-21）

- **[#R2-2b] KG 记录级有效性后过滤**：`legal_effectiveness.filter_records_in_force`——`List[Dict]` 记录变体（与 `filter_in_force` 同语义：fail-open、日期别名、失效当天仍有效）；**无有效性字段的记录原样保留且不注入字段**（存量图数据/统计聚合查询字节级零变化）；有效记录回写 `effective_status` 供下游展示。接线 `cypher_tools` 节点 records 产出点（text2cypher 执行结果进摘要前过滤），过滤器异常不阻断 KG 主链路；开关复用 `EFFECTIVENESS_FILTER_ENABLED`。**决策**：LightRAG/GraphRAG 返回纯文本无结构化有效性字段，记录级后过滤不可判，维持现状不接线。10 用例

### Round 3 续：Evidence Layer 补 GroundingValidator（2026-09-21）

- **[#R3-6] GroundingValidator v1**：`retrieval/domain/services/grounding_verify.py`——句子级证据锚定校验（架构文档 §104 四件套的最后一个缺口；与 CitationValidator 互补：后者管"引用对不对"，前者管"每句话有没有证据支撑"）。纯函数零 LLM 调用：中文句子切分（短句/纯数字过滤）→ 字符 bigram 包含度比对 → grounding score + 未锚定句预览；无证据 fail-open 不判定（与 citation_verify 同哲学）；`GroundingValidator` 类封装为语义级（NLI）演进留接缝。**观测模式**：只产出报告（`AgentToolContext.last_grounding`）+ 低分 WARN，不改答案、不拦截交付；`GROUNDING_VALIDATOR_ENABLED/SUPPORT_THRESHOLD/LOW_SCORE_WARN` 三配置。15 用例

### Round 3 续：Context Engine（2026-09-21）

- **[#R3-5] Context Engine**：`retrieval/domain/services/context_engine.py` 四能力——① 静态前缀版本化（`STATIC_PREFIX` + `content_version` 求版号 `ctx-*`，不变性测试锁定"同版本号 ⇒ 同前缀"）；② 测试数据 authority 排序（§26 五档：测试数据正式文本>示例说明>处理机构案例>二级资料>普通网页；读 `source_authority_level` 及入库别名，缺失档位 0 稳定排序——存量数据顺序零变化）；③ Context Budget（§27 token 级贪心装配，超预算低档截断/丢弃，`CONTEXT_BUDGET_TOKENS=0` 默认不限额）；④ 大文档隔离通道（指南 L1⑦，复用 artifact_store，超阈值全文外置、Prompt 留预览+句柄，`CONTEXT_ISOLATE_THRESHOLD=0` 默认关闭）。`assemble_context` 统一装配管线（Ranking→Isolation→Budget→拼接）接入 `query_knowledge_base`；`build_prompt` 增 `status_fields` 状态栏注入（【对话状态】单行，默认不传零变化）；`AgentToolContext.last_context_meta` 携带装配统计。18 用例
- **[修复] 工具注册表测试顺序污染**：`ToolExecutor.register_handler` 会把 spec 自动登记进全局 registry，R3 测试的 L2 夹具泄漏会打红 `test_r1` 的"全部 L0"断言（仅自定义顺序触发）。`ToolRegistry.delete_tool` 新增 + `test_r3_agent_runtime`/`test_r3_tool_selection` 夹具快照恢复
- **[事故] git 对象库损毁与重建**：2026-09-21 发现 `.git` 对象全部丢失（refs 目录、.pack、loose objects 均被外部删除，成因未明）。工作区源码经逐项验证完整无损；从 GitHub 只读 fetch 回上游基线，本地 78 个提交以单一恢复提交 `d581981` 重建（提交消息归档于 `D:\workbuddy工作目录\2026-09-15\My Agentix_recovery_backup\git_reflog_HEAD.txt`，工作区完整备份 `worktree_2045.tar.gz`）。下文历史 commit 哈希仅存于本日志文字记录

### Round 2 + 3：《技术变更指南 v2.0》落地 + Agent Runtime 架构（2026-09-21）

> 依据《技术变更与架构演进指南 v2.0》（L1-L3 P0 项）与《生产级 Agent 系统架构与实现设计》
> （Strangler Pattern，LangGraph 降级为 Execution Backend）。共 5 个独立 commit，每项配测试。

- **[#R2-1] Python 3.10→3.12 升级**（`c37de42`）：pyproject `requires-python >=3.12,<3.14`、Dockerfile×2 `python:3.12-slim`、CI/README 同步；钉版校准 langgraph-sdk `>=0.1.42,<0.2.0`、pyahocorasick `2.1.0`（cp312 wheel）、pydantic `2.13.5`（对齐实测基线）；特征化测试按解释器版本分支（3.12 contextvar 隐式传播 config / async interrupt 限制解除）。`.venv312` 全量 **1296 passed / 0 failed**
- **[#R2-2] 数据条目有效性过滤**（`99609e6`）：`domain/policies/legal_effectiveness.py` 四字段契约（effective_status/effective_date/expire_date/replaced_by），存量默认 in_force + fail-open；检索后过滤（三后端一致）+ fetch_k 有界补偿 + citations/hit_chunks 带 status + 答案"检索截至"标注 + kb_ingest 入库透传与显式标注入口；双开关配置。25 用例
- **[#R2-3] golden 扩充 + 引用正确性评分**（`5df20d4`）：golden 22→**102 条**（7 路由全覆盖、6 条有效性感知样例）；`score_citation_correctness`（答案文本真实引用 + sources 可归因，中文条号归一）。60 用例
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
- **测试数据语料元数据规范化**：`legal_metadata` helper（`effective_from/to`、`authority_level`），kb_ingest 接入测试数据元数据透传
- **请求执行图 + 基础设施关键性 ADR**：`docs/architecture/请求执行图与关键路径.md`、`adr/0006-infrastructure-criticality`
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
- **R4.2** 统一入口 main.ts + 根 App（auth gate + 共享导航双 tab）+ 根 router（`/`=测试数据智能助手，`/knowledge/**`=知识库问答）；knowledge 模块路由表前缀化、不再自建 router 实例
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
  - `CHANGELOG.md` + `docs/版本管理策略.md`（SemVer 规则、单一版本事实来源、分支模型、兼容性承诺）
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
  - `分层架构与依赖规则.md`（各层职责 / **4 条强制依赖规则** / 棘轮语义 / 新增文件归属决策树）
  - `接口与契约.md`（端口 Protocol / Payload TypedDict / 错误码四层分类 / HTTP 公共 API）
  - `扩展机制.md`（扩展点总览表：新增工具 / Provider / Agent 子图 / 领域适配）
  - `配置管理.md`（单一事实来源 / 优先级 / **只读 property 陷阱** / 特性开关回滚表）
  - `运行时护栏.md`（上下文隔离 / 控制平面 / 目标裁定流程 / 工具限量 / 人工审批）
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
  - `domain/policies/goal_arbitration.py`：**目标优先级与冲突裁决**（硬优先级 安全>规范>准确>有用>时延>成本 + 8 组冲突规则 + 确定性全序；显式提权不可越过硬优先级）
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
- **ruff 规则暂停为分类忽略清单**（每类写明理由）：`typing.List/Dict` 风格（~2200 处）刻意保留；`W291/W293` 因位于 prompt 多行字符串内（删除会改变送给模型的字节、击穿逐字节断言）而忽略，`.pre-commit-config.yaml` 的 `trailing-whitespace` 钩子加同源排除
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
  - 测试数据领域评估集（`evals/`）：22 条 golden 覆盖 7 类路由 + 3 个确定性指标
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
- 测试数据知识图谱（4 部数据集 / 1953 数据条目 / 67 示例说明）与向量语料（4865 块）
- FastAPI 后端（RESTful + SSE）与 Vue 3 前端聊天组件
- Docker Compose 一键部署（Neo4j / Milvus / PostgreSQL / MySQL / Redis / LightRAG）

---

[Unreleased]: https://github.com/Jia-Yihao/Legelbot/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/Jia-Yihao/Legelbot/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/Jia-Yihao/Legelbot/releases/tag/v0.1.0
