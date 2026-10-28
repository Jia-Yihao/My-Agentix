# HITL 与恢复链断点收口 实施计划

> **文档状态**：已完成（留档）· 计划已交付；文中「待做 / 下一批 / 本轮不跑」是**当时**的待做，不是现在的待办。
> 文档分层：唯一现行总览是根 [README](../../README.md)，实现细节见 [PROJECT_SPEC](../项目实现手册.md)。

> **落地状态（2026-10-27 批次收口）**：本计划任务 1–8 已全部落地，下方 27 个步骤复选框在收口时统一勾选。**勾选的含义要限定清楚**：任务 1、2 由本会话按 `organizer→executor→reviewer` 流程落地，含各自的 RED→GREEN 过程；任务 3–6 的落地与复验由**同一工作树上的并发会话**完成，本会话只**逐条读代码核验了终态落点与锚点**（见 `CHANGELOG.md` `[Unreleased]` PhaseK 小节），**没有见证它们的 RED 输出**——所以下方「步骤 2：运行，确认按预期失败」这类过程项对任务 3–6 表示"由该批次覆盖"，不表示本会话逐步见证。任务 7 经三轮独立审查判通过（第 3 轮闭合唯一严重项），任务 8 经独立审查判通过（随结论登记 4 条一般项，其中两条在收口轮闭合）。**本轮不产生"0 failed"的全量绿**：唯一失败项的归因与证据见 `docs/工程口径日志.md` PhaseK 条。

**目标：** 把外部重构提案所指、并经现树实测确认的 4 处断点补实（checkpointer 静默降级无 fail-closed、
进环审批裁决不打指标、`model_used` 产了没人取、路由/恢复决策在可观测面上不存在），
**不删改任何已实现的层**（L1 路由、Model Gateway、准入/甩载/隔离舱、interrupt 注入与 resume 面）。

**架构：** 全部走「新增机位 + 默认关 + flag-off 逐位等价」（`agents.md` 纪律 1）。
checkpointer 的 fail-closed 用新 Settings flag；可观测三处只写 **span 属性**（纪律 3：判定值有界可枚举，
高基数与理由文本仍不落）；审批指标复用既有 `retrieval_approval_decisions_total{target_type,decision}`
并把进环路径的 `target_type` 与桥路径逐字对齐为 `"tool"`（纪律 6 ④：同一个判定不许两个源）。

**技术栈：** Python 3.12 · FastAPI · LangGraph（`langgraph.types.interrupt` / `Command`）·
`langgraph-checkpoint-redis` · prometheus_client · loguru · pytest · ruff 0.5.7（仓库 pin）。

**规格：** 本文件即规格。事实基线（每条都已实测，执行者**不得**再引外部提案的说法覆盖它）：

| 断点 | 现状证据 |
| --- | --- |
| Redis 连不上 ⇒ 静默换 `MemorySaver`，无 fail-closed 判据 | `agentix/infrastructure/persistence/checkpointer.py:97`–`106`（降级分支）、`:58`（docstring「任何失败都降级……绝不抛出异常阻塞启动」）、`:17`（降级单例粘死不再重试）；`agentix/config/settings.py:1297` `CHECKPOINTER_TYPE="redis"` |
| 进环 `interrupt()` 裁决不打指标，桥路径打 | 打点侧 `agentix/policies/hitl/hitl_bridge.py:575`–`579`（`record_approval_decision(target_type="tool", decision=label)`）；未打点侧 `agentix/application/services/task_execution.py:493`（`decision = review_interrupt(payload)`）到 `:507` 全程无指标调用 |
| **同一个指标 `decision` label 现在有三套词汇，且无守卫**（本批新发现的腐烂，实测） | 检索侧写 `"approved"`（`agentix/retrieval/usecases/approval_service.py:123`）；桥侧写**枚举原值** `result.action.value` ⇒ `approve`/`reject`/`edit`/`resume`（`hitl_bridge.py:352` + `agentix/policies/hitl/sidecar.py:29`–`32`），**而它自己内部的计数用的是另一套** `bucket` ⇒ `approved`/`rejected`/`edited`/`resumed`（`hitl_bridge.py:353`–`359`）；自动放行还写 `"auto"`（`:215`–`223`）。全仓 `tests/**` 无任何用例钉过 `decision` 的取值集合（grep `APPROVAL_DECISIONS` 在 tests 里 0 命中）。实测还存在第四种形态：`PermissionMode.BYPASS` 下经 `hitl_bridge.py:211`→`:223` 写入 `"bypass"`（模式级豁免），一并收进值域（六值，见任务 2） |
| `GatewayResult.model_used` 生产后无人消费 | 产出 `agentix/infrastructure/llm/model_gateway.py:68`、`:201`；全仓 `model_used` 读取点 **0 处**（仅类内产出与 docstring） |
| 路由决策与恢复点在可观测面上不存在 | `agentix/application/agents/router.py:223`（`analyze_and_route_query`，该文件 `set_attribute` 0 命中）；`agentix/retrieval/api/routers/explanations.py:41`–`44` 把 `route_decision` 显式登记为 `not_recorded`；`agentix/orchestration/langgraph/lg_builder.py:351`–`355`（`Command(resume=...)`）与 `agentix/application/services/breakpoint_resume.py:305`（`resume_thread`）均无属性落点 |

## 全局约束

- 新增 Settings flag 一律 `False`/`0` 起步，且 **flag-off 路径与基线逐位等价**（纪律 1）。
- 新增配置键必须**五处同步**：`agentix/config/settings.py` · `agentix/config/selfcheck.py`（`DEFAULT_IGNORED_MISSING`）·
  `.env.example` · `docs/architecture/配置管理.md` §5 表 · `tests/test_p41_doc_consistency.py:237::test_new_settings_are_documented` 手写清单（纪律 4）。
- 指标 label 名必须先在 `tests/test_p100_genai_observability.py:227`–`252` 的 `ALLOWED_METRIC_LABELS` 里；
  **本计划不新增任何 label**（`target_type`/`decision` 已在 `metrics.py:137`–`141`）。判定值与理由文本进 span，不进指标（纪律 3）。
- 观测与指标故障一律 **fail-open**（`# noqa: BLE001 - 理由`，捕获后降级、`logger.debug/warning` 留痕，绝不影响主链）（纪律 2）。
- 审批语义不得放宽：无批准人 ⇒ 拒绝（fail-closed），且该拒绝属**治理层终局**，不进自愈循环
  （`.qoder/rules/code-exec.md` §3.1/§7）。
- 注释与文档字符串用简体中文，说明「为什么」。测试文件命名 `tests/test_p<NN>_<主题>.py`；本批用 **p132 / p133**。
- 复验命令一律用仓库 venv：`.venv/Scripts/python.exe -m pytest … -q -m "not integration"`；lint 用
  `ruff check agentix tests scripts evals kb_ingest`。
- **本轮不跑评测**（维护者 2026-10-26 裁决）；不做 `git commit`（用户明令）。

## 审查重点

- `CHECKPOINTER_TYPE=redis` 且 Redis 连不上、`CHECKPOINTER_FAIL_CLOSED=true` ⇒ 启动**显式失败并带原因**，
  而不是静默落到 `MemorySaver`；且 `_saver` 不得被赋成 MemorySaver（否则粘死，见 `checkpointer.py:17`）。
- `CHECKPOINTER_TYPE=memory`（显式选择）时**即使 fail-closed 开也不抛**——显式配置不是故障。
- flag 关时三处行为与基线**逐位不变**：降级仍走 `MemorySaver` + `_record_degraded` + 那条 ERROR 日志。
- 进环裁决为 `reject` / 未知值 / `gate.approve` 返回 False ⇒ 指标记 `rejected` 且**绝不放行执行**。
- span 属性写入点在当前无 span（Noop）或 OTel 抛异常时**静默降级**，不得让一次正常问答失败。

## 非目标（本轮不做，理由实测登记；改口径需维护者单独裁决）

1. **多中断配对**：`Command(resume=...)` 的全部生产点只喂单值（`lg_builder.py:354`、`application/agents/main.py:111`），
   `task_execution.py:436` 的 `pending is None` 守卫使一波只暂存**一个**待审载荷。支持「一波多工具各自裁决」
   要改中断协议与图拓扑，且本仓已实测记录 **async 节点里 `interrupt()` 不可用**
   （`agentix/application/agents/kg_sub_graph/.../multi_tool.py:151`–`157`，据此撤回过）——不是补一行能解决的。〔2026-10-27 限定：该实测结论只适用于「py3.10 包装式接线」那条路；`HITL_IN_LOOP_ENABLED`（`agentix/config/settings.py:1044`，2026-10-03 F1e 落地、默认关）已用注入壳让图内 `interrupt()` 可用，口径见 `docs/migration/架构演进评估.md:66`；原结论当时为真，保留不删〕
2. **动作级幂等（防恢复后重复副作用）**：`task_execution.py:486`–`487` 已把这条例属**调用方责任**写死在注释里，
   而进程内去重对「进程重启后恢复」这一目标无效，要做必须落**持久 claim（Redis + TTL）并定义 claim 失败是否放行**——
   这是存储与 fail 语义的设计决策，需维护者先裁。
3. 不删 L1 路由、不删 Model Gateway、不删准入/甩载/隔离舱（实测：后两者都在图外且默认关，
   `ADMISSION_CONTROL_ENABLED=False` settings.py:561、`LOAD_SHEDDING_ENABLED=False` :599——2026-10-27 复测：两处锚点未漂移、默认值仍为 `False`）。
   **维护者 2026-10-27 追问的「三层路由转一层」= 本条非目标，未做且刻意不做**：三层结构在文档与代码里都仍在
   （`docs/architecture/README.md:11` 写「主路由层 → 多工具子图层 → 原子工具层」、`README.md` §「三层 Multi-Agent 请求链路」、
   L1 入口 `agentix/application/agents/router.py:224` 的 `analyze_and_route_query` 未删），本批只**加观测点、零删除**。
   删它们换不来延迟，只会废掉既有覆盖。**本行原文的「11 个测试文件与 `route_type` 的 29 个消费面」是 2026-10-26 读数、且"消费面"当时没给判据
   ⇒ 2026-10-27 换成带命令的可复现读数**：`grep -rl "route_type" tests --include=*.py` → **17 个测试文件**；
   `grep -rn "route_type" agentix --include=*.py` → **36 处 / 9 个文件**；`grep -rl "route_query\|analyze_and_route_query" tests --include=*.py` → **13 个文件**；
   `grep -rl "ADMISSION_CONTROL_ENABLED\|LOAD_SHEDDING_ENABLED" tests --include=*.py` → **2 个文件**（`tests/test_p41_doc_consistency.py`、`tests/test_p97_runtime_control.py`）。

---

### 任务 1：checkpointer 的 fail-closed 开关

**文件：**
- 修改：`agentix/config/settings.py:1297` 附近（紧随 `CHECKPOINTER_TYPE` 之后插入新字段）
- 修改：`agentix/infrastructure/persistence/checkpointer.py:54`–`106`（`init_checkpointer`）
- 修改：`agentix/config/selfcheck.py`（`DEFAULT_IGNORED_MISSING`）、`.env.example`、
  `docs/architecture/配置管理.md`（§5 配置表）、`tests/test_p41_doc_consistency.py:237`（手写清单）
- 新建：`tests/test_p132_checkpointer_fail_closed.py`

**接口：**
- 新增 `Settings.CHECKPOINTER_FAIL_CLOSED: bool = False`（描述文案固定为：
  「checkpointer 初始化失败时是否拒绝启动（true=fail-closed，绝不落 MemorySaver）；显式 `CHECKPOINTER_TYPE=memory` 不受影响」）。
- `async def init_checkpointer() -> Union[object, MemorySaver]` 签名不变；
  新增抛出类型 `RuntimeError`，消息以 `checkpointer fail-closed: ` 起头并内插原降级原因。

- [x] 步骤 1：写失败测试（`tests/test_p132_checkpointer_fail_closed.py`）

```python
asyncio 用例沿用本仓既有范式（照 tests/test_p69_stage_m_manifest.py 的 import 与 marker 写法，不自创）。
三条用例：
async def test_fail_closed_off_degrades_to_memory_saver_as_before(monkeypatch):
    # 构造 Redis 连不上（monkeypatch _try_import_async_redis_saver 返回真类、其 asetup 抛 ConnectionError）
    # 期望：返回 MemorySaver、get_degraded_reason() 非 None、不抛 —— 与基线逐位一致
async def test_fail_closed_on_raises_and_keeps_singleton_unset(monkeypatch):
    # CHECKPOINTER_FAIL_CLOSED=true + 同样的 ConnectionError
    # 期望：pytest.raises(RuntimeError)，且 exc 消息含 "checkpointer fail-closed"；
    #       抛完 get_checkpointer() is None（单例没被粘成 MemorySaver）
async def test_explicit_memory_type_never_raises_even_when_fail_closed(monkeypatch):
    # CHECKPOINTER_TYPE="memory" + CHECKPOINTER_FAIL_CLOSED=true
    # 期望：正常返回 MemorySaver，不抛
每条用例前后都调 reset_checkpointer()（checkpointer.py:129）。
```

- [x] 步骤 2：运行，确认按预期失败

Run: `.venv/Scripts/python.exe -m pytest tests/test_p132_checkpointer_fail_closed.py -q`
Expected: 第 2 条 FAIL（当前实现只降级不抛），第 1、3 条 PASS。

- [x] 步骤 3：实现。在 `init_checkpointer` 的两条 **Redis 路径失败分支**（`:73`–`81` 未安装、`:97`–`106` 连接/asetup 失败）
  各加一个前置判断：`settings.CHECKPOINTER_FAIL_CLOSED` 为真 ⇒ `raise RuntimeError(f"checkpointer fail-closed: {原因}")`，
  **且在 raise 之前不得给 `_saver` 赋值**；`:65`–`70` 的显式 memory 分支不加此判断。
  同步更新模块 docstring `:19`–`24` 的降级清单（补一句 fail-closed 开关的存在与默认关），并把 `:58`
  那句「任何失败都降级……绝不抛出异常阻塞启动」改准为「默认任何失败都降级；`CHECKPOINTER_FAIL_CLOSED=true` 时 Redis 路径失败改为拒绝启动」。

- [x] 步骤 4：五处同步 —— `selfcheck.DEFAULT_IGNORED_MISSING` 加键名、`.env.example` 加
  `CHECKPOINTER_FAIL_CLOSED=false` 一行（带一句注释）、`配置管理.md` §5 表加一行、
  `test_p41_doc_consistency.py:237` 的手写清单加该字段。

- [x] 步骤 5：运行，确认通过

Run: `.venv/Scripts/python.exe -m pytest tests/test_p132_checkpointer_fail_closed.py tests/test_p41_doc_consistency.py -q -m "not integration"`
Expected: 全 PASS；`ruff check agentix tests` 全绿。

---

### 任务 2：进环审批裁决补打指标（与桥路径同源）

**文件：**
- 修改：`agentix/application/services/task_execution.py:489`–`520`（`pending is not None` 分支）
- 修改（测试）：`tests/test_p81_hitl_in_loop.py`（既有进环用例文件，新增 2 条用例，不动既有用例）

**接口：**
- 上游：`agentix/observability/metrics.py:576` `record_approval_decision(target_type: str, decision: str)`；
  其 docstring `:577` 自己声明的值域是 `approved / rejected`——**与桥侧实际写入的枚举原值不符**（见事实表最后一行）。
- 对外：无新签名。落点为分支内新增一个**模块级私有函数**
  `def _record_in_loop_decision(decision: str) -> None`，内部
  `record_approval_decision(target_type="tool", decision=decision)`，
  `except Exception` ⇒ `logger.debug`（调用形状与 `hitl_bridge.py:575`–`579` 同构）。
- **值域归一（维护者 2026-10-27 裁决，本任务按此执行，不再保留备选）**：`decision` 只允许
  `{approved, rejected, edited, resumed, auto, bypass}` 六个值（第 1 轮审查发现：计划初稿的五值是枚举缺漏——`PermissionMode.BYPASS` 下 `hitl_bridge.py:211`→`:223` 也写同一 label；2026-10-27 据裁收敛为六值。`bypass` 是模式级豁免、`auto` 是逐请求自动放行，治理含义不同，归并会抹掉"谁批的"这层区分）。落法三处：
  1. 进环（新）：放行 ⇒ `approved`，其余全部 ⇒ `rejected`；
  2. 桥（改）：`agentix/policies/hitl/hitl_bridge.py:352` 由 `self._record_decision(result.action.value)`
     改为 `self._record_decision(bucket[result.action])` —— `bucket` 映射 `:353`–`:358` 已存在，
     **必须先于 `:352` 计算**（现树里 `bucket` 在 `:353` 才定义，直接引用会 `NameError`；
     所以这一步是**把 `bucket` 的定义上移到 `_record_decision` 之前**，不是新增映射）；
  3. 检索侧 `approval_service.py:123` 已是 `approved` ⇒ 不动；自动放行 `hitl_bridge.py:223` 的 `"auto"`
     属合法值 ⇒ 不动，但要进守卫的值域清单。
  **已登记的代价**：`decision="approve|reject|edit|resume"` 的历史序列在新查询里查不到（本仓无看板，
  维护者知情裁决）；`CHANGELOG.md` 必须写成**破坏性可观测变更**（label 值域收敛），不得写成"新增打点"。
- 归一化只有一个源：`"approve"` 且 `gate.approve(...)` 返回真且重试成功 ⇒ `approved`；
  其余（`reject` / 未知值 / `approve` 但 `request_id` 缺失 / `gate.approve` 返回 False / 重试仍失败）⇒ `rejected`。
- 新增一条**静态值域守卫**（放 `tests/test_p133_decision_span_attributes.py`）：用源码文本检查
  **四个**写入点——`task_execution.py`、`hitl_bridge.py` 的 `bucket[result.action]` 打点与
  `auto_label` 打点（`auto`/`bypass` 两处）、`approval_service.py`——写入 `record_approval_decision` 的
  decision 实参只能是上述六值之一（禁止 `result.action.value` 这类开放取值重新出现），
  **不靠"跑一遍才有值"**（CI 上恒真的守卫等于没写）。

- [x] 步骤 1：写失败测试（追加进 `tests/test_p81_hitl_in_loop.py`）

```python
# 读值范式取自仓内既有两处：tests/test_p118_router_fallback_metric.py:44 (_counter) 与
# tests/test_p105_feedback_loop.py:137 (COUNTER.labels(...)._value.get())——不新造第三套读法。
def _decision_value(decision: str) -> float:
    from agentix.observability.metrics import APPROVAL_DECISIONS
    return APPROVAL_DECISIONS.labels(target_type="tool", decision=decision)._value.get()

async def test_in_loop_approve_records_approved_decision(...):
    # before = _decision_value("approved")；跑一次进环批准；断言 _decision_value("approved") - before == 1
    # 且 _decision_value("rejected") 增量 == 0
async def test_in_loop_reject_records_rejected_decision(...):
    # before = _decision_value("rejected")；裁决 "reject"；断言增量 == 1，且 executor.execute 未被再次调用
```
两条都要覆盖 `reject` 与「`approve` 但 `gate.approve` 返回 False」 ⇒ 后者也记 `rejected`（可再追加第三条）。

- [x] 步骤 2：运行，确认失败

Run: `.venv/Scripts/python.exe -m pytest tests/test_p81_hitl_in_loop.py -q -k "records"`
Expected: 2 条 FAIL（增量 0）。

- [x] 步骤 3：实现 `_record_in_loop_decision` 并在 `:493` 取到 `decision` 后、以及各失败出口（reject/未知/批准失败/重试失败）
  调用一次——**每条裁决路径恰好一次**，不得双计。

- [x] 步骤 4：运行，确认通过

Run: `.venv/Scripts/python.exe -m pytest tests/test_p81_hitl_in_loop.py -q -m "not integration"`
Expected: 全 PASS（含既有用例不变红）。

---

### 任务 3：spans.py 新增三个决策属性 helper（纯新增，无消费点）

**文件：**
- 修改：`agentix/observability/spans.py`（`_clean_attributes:27`、`span:33`、`genai_span:53`、`sync_span:74` 之后追加）
- 修改：`agentix/observability/tracing.py:230`–`234` 常量表（新增 `GENAI_RESPONSE_MODEL = "gen_ai.response.model"`）
- 新建：`tests/test_p133_decision_span_attributes.py`

**接口：**
```python
# agentix/observability/spans.py
ROUTE_DECISION_ATTR = "agentix.route.decision"
RESUME_ATTR = "agentix.resume_from_checkpoint"

def record_route_decision(route_type: str) -> None:      # 当前 span 写 agentix.route.decision；空串/无 span ⇒ 静默 no-op
def record_model_used(model_used: str, *, primary_model: str) -> None:
                                                          # 写 gen_ai.response.model；model_used 与 primary 相同也写（语义是"实际服务方"）；
                                                          # 空串/无 span/异常 ⇒ no-op
def record_resume_from_checkpoint(*, thread_id_present: bool) -> None:
                                                          # 写 agentix.resume_from_checkpoint = True（仅 thread_id_present 为真时）
```
三者共同纪律：内部 `try/except Exception` ⇒ `logger.debug(...)`，**绝不向上抛**；不新增指标；不改函数签名以外的任何既有代码。

- [x] 步骤 1：写失败测试（`tests/test_p133_decision_span_attributes.py`）

```python
def test_record_route_decision_writes_attr_on_fake_span():     # monkeypatch 当前 span 为可记属性的假对象
def test_record_model_used_writes_genai_response_model():       # 断言键名逐字 == "gen_ai.response.model"
def test_record_resume_from_checkpoint_only_when_thread_present()
def test_helpers_never_raise_when_span_is_noop_or_broken():     # 假 span 的 set_attribute 抛异常 ⇒ helper 不抛
def test_no_new_metric_labels_registered():                     # 断言三处调用后 prometheus REGISTRY 无新 label name
```

- [x] 步骤 2：运行，确认失败

Run: `.venv/Scripts/python.exe -m pytest tests/test_p133_decision_span_attributes.py -q`
Expected: FAIL，`ImportError: cannot import name 'record_route_decision'`。

- [x] 步骤 3：按上述签名实现三个 helper；`GENAI_RESPONSE_MODEL` 加进 `tracing.py` 常量表并在 `spans.py` 引用（**不在两处重复字面量**，纪律 6 ④）。

- [x] 步骤 4：运行，确认通过

Run: `.venv/Scripts/python.exe -m pytest tests/test_p133_decision_span_attributes.py -q`
Expected: 5 条全 PASS。

---

### 任务 4：路由决策上 span + 把 `not_recorded` 占位改准

**文件：**
- 修改：`agentix/application/agents/router.py:223`（`analyze_and_route_query` 返回最终 route 之前）
- 修改：`agentix/retrieval/api/routers/explanations.py:41`–`44`
- 测试：`tests/test_p133_decision_span_attributes.py`（追加 1 条）

**接口：**
- 上游：任务 3 的 `record_route_decision(route_type: str)`。
- 落点：**唯一一处**——`analyze_and_route_query` 得到最终 route 值后调用；纯 Python 分派的
  `route_query()`（`router.py:332`）不调（它不返回 route_type，调了就是两个源）。
- `explanations.py` 里 `"route_decision"` 那条占位文案改为：
  `"路由决策值已进 span（agentix.route.decision），理由文本仍未持久化"`，
  且该键**留在** `_NOT_RECORDED_BLOCKS`（理由文本确实仍未落库，不许写成"已持久化"）。

- [x] 步骤 1：写失败测试：`test_router_records_route_decision_attr()` —— 走一次 `analyze_and_route_query`
  （沿用 p81/router 既有的假模型桩），断言假 span 上出现 `agentix.route.decision` 且值 ∈ `router.py:181`–`189` 的 7 类词表（2026-10-27 实测改准：原引 `:180`–`:188` 系行号漂移；flag-off 7 类；`CODE_EXEC_ENABLED=true` 时 `:280` 并入 `code-query` ⇒ 8 类，`TASK_EXEC_ENABLED=true` 时另由 `:278` 并入 `task-query`；复现命令：`grep -n "ALLOWED_TYPES\|allowed_types.add" agentix/application/agents/router.py`）。

- [x] 步骤 2：运行确认失败 → 步骤 3：实现（一处调用 + 文案改准）→ 步骤 4：

Run: `.venv/Scripts/python.exe -m pytest tests/test_p133_decision_span_attributes.py tests/test_p41_doc_consistency.py -q -m "not integration"`
Expected: 全 PASS。

---

### 任务 5：`model_used` 上 span（回退后实际服务模型终于有人取）

**文件：**
- 修改：`agentix/retrieval/usecases/chat_agent_tools.py:294` 与 `:312`（两行都是 `resp = gw_result.response`，
  分别紧跟 `:285`、`:303` 的 `ainvoke_with_fallback`）
- 测试：`tests/test_p133_decision_span_attributes.py`（追加 1 条）

**接口：**
- 上游：任务 3 的 `record_model_used(model_used, *, primary_model)`；`GatewayResult.model_used`（`model_gateway.py:68`、`:201`）。
- 两处的实参**逐字同形**：`record_model_used(gw_result.model_used, primary_model=ctx.model_name)`
  ——主模型名只从 `ctx.model_name` 取（`:285`/`:303` 的 `primary_model=ctx.model_name` 已证明这是那两处唯一现成的主模型名），
  **不得**再经 `settings` 或 `gateway.resolve_model` 另取一份（纪律 6 ④）。
- 只在 `gw_result` 存在的两个分支里调用；无 fallback 的原路径**不调**（那里没有 `model_used` 可报）。

- [x] 步骤 1：写失败测试：`test_fallback_result_records_actual_model_on_span()` —— 桩让 `model_used != primary_model`，
  断言假 span 上 `gen_ai.response.model == 桩的 model_used`。

- [x] 步骤 2：运行确认失败 → 步骤 3：实现两处调用 → 步骤 4：

Run: `.venv/Scripts/python.exe -m pytest tests/test_p133_decision_span_attributes.py tests/test_p132_checkpointer_fail_closed.py -q -m "not integration"`
Expected: 全 PASS；`ruff check agentix` 全绿。

---

### 任务 6：恢复点上 span（进环 resume 与断点续跑）

**文件：**
- 修改：`agentix/orchestration/langgraph/lg_builder.py:351`–`355`（`Command(resume=...)` 分支）
- 修改：`agentix/application/services/breakpoint_resume.py:305`（`resume_thread` 的 `graph.ainvoke(None, config=...)` 之前）
- 测试：`tests/test_p133_decision_span_attributes.py`（追加 1 条）

**接口：**
- 上游：任务 3 的 `record_resume_from_checkpoint(thread_id_present=bool)`。
- `thread_id_present` 的判据**只允许**来自 `config["configurable"]["thread_id"]` 是否非空——
  不得从别处再取一份 thread 概念（`thread_id = session_id`，真源 `chat_service.py:774`、`:1486`）。

- [x] 步骤 1：写失败测试：`test_resume_paths_mark_checkpoint_attr()` —— 两条路径各断一次属性为 `True`，
  且无 `thread_id` 时**不写该属性**。

- [x] 步骤 2：运行确认失败 → 步骤 3：实现两处 → 步骤 4：

Run: `.venv/Scripts/python.exe -m pytest tests/test_p133_decision_span_attributes.py tests/test_p81_hitl_in_loop.py -q -m "not integration"`
Expected: 全 PASS。

---

### 任务 7：文档同步面（代码行为变更时的强制清单）

**文件：** `CHANGELOG.md`（[Unreleased]）· `README.md`（§3 架构 / §7 可靠性 / §16 已知限制）·
`SECURITY.md`（已知边界）· `docs/项目实现手册.md` · `docs/architecture/配置管理.md` ·
`docs/architecture/运行时护栏.md` · `.env.example` · `agentix/config/selfcheck.py`（任务 1 已改，此处核对）·
`docs/工程口径日志.md`

- [x] 步骤 1：`CHANGELOG.md` 新增一条 `[新增]`/`[修复]` 混合条目：4 个断点逐条带 `文件:行` 锚点、
  新 flag 名与默认值、以及**自我限定**（写进文档的三句不许省略）：
  「本轮不删任何既有层（提案的三层串联前提经实测不成立）」、
  「多中断配对与动作级幂等属非目标，理由见本计划 §非目标」、
  「span 属性只落判定值，理由文本与高基数内容仍不落」。
- [x] 步骤 2：`README.md` §16 / `SECURITY.md` 已知边界补 checkpointer 一条：
  「`CHECKPOINTER_FAIL_CLOSED` 默认关 ⇒ Redis 不可用时审批/恢复状态**不持久**（有 ERROR 日志与降级指标），
  需要持久审批链的部署应显式开此开关」。
- [x] 步骤 3：`docs/工程口径日志.md` 登记本轮**历史**读数与新 flag，并把本计划路径写成分工指针
  （不在 CHANGELOG 复述全量计数，§11 是唯一真源）。
- [x] 步骤 4：复验

Run: `.venv/Scripts/python.exe -m pytest tests/test_p41_doc_consistency.py tests/test_ci_gate_contract.py tests/test_code_hygiene.py -q -m "not integration"`
Expected: 与任务 1–6 全部落地后的计数一致（基线在任务 8 re-cite）。

---

### 任务 8：全量重跑与基线 re-cite（本轮最后一个任务，不得提前）

- [x] 步骤 1：跑全量

Run: `.venv/Scripts/python.exe -m pytest -m "not integration" -q`
Expected: **0 failed**；计数 = 基线 5084 + 本批新增用例数（p132 三条 + p133 若干 + p81 两条 + 任务 4/5/6 追加项），
以实测原样记录，**不得推算**。

**实测（2026-10-27 全量重跑）**：`5239 passed / 40 skipped / 4 deselected / **1 failed**`（419.53s）⇒ **本行 Expected 的「0 failed」未成立**。唯一失败项是文档路径存在性守卫点名两条不存在的引用，出自并发批次的设计文档（该文档自己已标注其未落地），本轮按纪律 5 未动守卫、按边界未改该文档。「计数 = 基线 5084 + 本批新增」同样未成立：相对 5084 的 **+155 passed** 是两端实测相减、**不做逐项对账**（同一棵树含并发批次落地的测试文件）；本批可单独点名的新增只有 **30** 条（`p132` 4 / `p133` 13 / `p134` 13）。归因、deselected 3 → 4 的可点名来龙去脉见 `docs/工程口径日志.md` PhaseK 条。

- [x] 步骤 2：把新读数写进 `README.md:578`（§11 唯一真源）与 `CHANGELOG.md`，
  并按 `agents.md` §11 惯例在 `docs/工程口径日志.md` 保留旧读数作为历史。
- [x] 步骤 3：`ruff check agentix tests scripts evals kb_ingest` 全绿；`mypy agentix/domain` 仍 0 错；
  `python scripts/gen_staging_matrix.py --check` 退出 0（新 flag 若进矩阵需同步 override 或 `MANUAL_KEYS` 署名，纪律 7）。
- [x] 步骤 4：确认 `tests/test_p131_code_exec.py` 未被本批触碰 ⇒ 四处 `p131 共 39 条` 短语与条数守卫仍应核过；
  若本批动了该文件（默认不许），必须同步四处短语。

---

## 派单与验收边界

- 任务 1、2 可并行（文件集不相交）；任务 3 必须先于 4/5/6（三者共用 `spans.py`，**不得同时改** `agentix/observability/spans.py`）；
  任务 4/5/6 之间文件集不相交，可并行；任务 7、8 串行收尾。
- 每个任务由 `executor` 自检后提交，`reviewer` 只读验收；复验命令与期望读数以本文件为准。
- 任何任务实现中若发现本文件的事实表与现树不符，**停止并上报**，不得自行改口径或顺手扩大范围。
