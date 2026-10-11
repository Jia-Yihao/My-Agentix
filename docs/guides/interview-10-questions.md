# 面试 10 问（Agent Platform / My Agentix）

面向架构与 Agent 平台岗位的技术问答提纲；答案锚点均指向**本仓库真实模块**，
路径经 `tests/test_p41_doc_consistency.py` 守卫。

---

### 1. 为什么说 Agent Core 是 Harness 而不是「又一个 Agent 框架」？

Harness 五职责（Context / 规划 / 工具 / 验证 / 纠偏）映射到七个
`typing.Protocol` 端口（`agentix/domain/agent/ports.py`），实现可替换；
LangGraph 只允许出现在 `agentix/agent/graph/`，框架可换。

### 2. Context 如何防预算爆炸与提示注入？

`Context = Static Prefix + Trajectory`，超预算从轨迹尾部确定性截断，
大块证据进 `isolated_refs` 隔离区；状态栏键白名单（不信任模型自写）。
实现：`agentix/agent/context/engine.py`。

### 3. 五类记忆与读取时冲突裁决是什么？

Working / Episodic / Semantic / Procedural / User × user/task scope；
冲突在 **recall 时** 裁决而非写入时合并；remember 强制 PII 脱敏。
实现：`agentix/agent/memory/service.py`。

### 4. 工具智能（ACI）比 function calling 多做了什么？

负面描述（何时不能用）、参数保真（禁静默改参）、能力检索三层、
MCP 描述当不可信输入（防投毒）。实现：`agentix/tools/`（M1 自 infrastructure/tools 迁入；M6 后旧路径已删）。

### 5. 分层 Verifier 如何避免「模型自证」？

L1 结构 → L2 规则（`domain/policies/completion.py`）→ L3 grounding 纯函数；
Reviewer 只见结构化参数与证据，不见 Proposer 自述。
实现：`agentix/policies/verifier/layered.py`。

### 6. 四层故障与 Replanner 怎么协作？

API / 工具 / 上下文 / 控制流四层分类统一在
`agentix/orchestration/executor.py::classify_exception`
（曾误记为 capabilities 侧——那边没有此符号，见 executor 对照表 in `module-inventory.md` §2）；
错误处理不再无限调 LLM，失败进 Replanner 产出**新** Plan。

### 7. HITL Sidecar 为什么强调上下文隔离？

Reviewer 独立 session，送审面只有 `to_public_dict()` 字段；
Approve/Reject/Edit/Resume 四裁决 + 进程重启恢复。
实现：`agentix/policies/hitl/sidecar.py`。

### 8. Long-running 事件模型的关键设计？

双时刻（occurred_at / received_at）、事件 key 去重、TTL 过期、
指数退避重试、虚拟身份 scope（看不到平台主凭证）。
实现：`agentix/agent/events/adapter.py`。

### 9. Pass@k 与 Pass^k 差在哪？1–2pp 提升算不算数？

Pass@k = 至少一次成功（能力上界）；Pass^k = k 次全对（稳定性）。
统计显著用两比例 z 检验，且绝对提升需 ≥2pp 才 practically meaningful。
实现：`agentix/domain/evals.py`、`evals/run_eval.py`。

### 10. Continuous Evolution 如何保证「进化不拆房」？

在线循环 **零写能力**；离线 聚合→提案→回归→灰度；
三道防线：证据≠指令、staging 隔离、验证器逻辑路径黑名单。
实现：`agentix/agent/evolution/loop.py`。

---

延伸阅读：`docs/architecture/agent-core-layers.md`、
`docs/migration/Agent_Core_2.0_规划.md`、`docs/guides/research-agent-demo.md`。
