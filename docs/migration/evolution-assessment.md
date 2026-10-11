# 架构 / 代码框架演进评估（Phase 21–23 执行期审计）

- 日期：2026-09-27
- 方法：执行期实测（守卫测试 + 全仓引用扫描 + 前端/后端双侧审计），不做推测性重构
- 范围：Agent Core（Phase 21–23）、前端双模块（chat/knowledge）、文档体系
- 关联：`docs/migration/Phase21-26_规划.md`（纪律与 §4「明确不做」）、`docs/migration/module-inventory.md`

---

## 1. 依赖方向与组合根（现状健康）

- **capabilities → agent 单向注入**已成立：`notify_user` 工具只声明 spec，
  handler 组合根在 `agentix/application/services/notify_bridge.py` 注入
  （`capabilities` 不 import `agent`，守卫 `test_p40_import_health.py` / `test_p17_domain_layer.py` 通过）。
- **LangGraph 边界未扩散**：`langgraph` 仅出现在 `agentix/agent/graph/`；
  新增事件层（`agentix/agent/events/redis_stream.py`）为纯 `redis` 客户端实现，无 LangGraph 依赖。
- **domain 零第三方**保持（Phase 21–23 新增 domain 代码均只用标准库 + Protocol）。

结论：Strangler 纪律（flag 默认关 / flag-off 字节等价 / 组合根注入）执行期未被破坏，无越界 import。

## 2. 已记录的分层偏离（1 处，已守卫）

| 项 | 规划倾向 | 实际落地 | 状态 |
|----|----------|----------|------|
| `RedisStreamEventBus` | `agentix/infrastructure/` | `agentix/agent/events/redis_stream.py` | **接受偏离** |

理由（已写入模块 docstring）：与 `memory`/`jsonl` 事件后端同居一个包；仓库无
`agent → infrastructure` 先例，反向会开先例；懒 import 防 p40。
守卫：p17（ports `__all__` 精确集）+ p40（import 健康）+ 分派在
`agentix/agent/events/port.py::build_event_port` 内 `redis_stream` 分支（init 异常回退 memory）。
**落地注需补进规划 §3 Phase 23**（本次收尾时同步）。

## 3. 双通道 SSE：两套词汇表共存（结构性，暂不动）

| 通道 | 后端 | 事件词汇 | 前端消费者 |
|------|------|----------|-----------|
| 编排域 | `interfaces/http/v1/chat.py:166 _to_sse`（`event:` 与 `type` 双写） | `metadata / message / tool_start / tool_end / done / error` | `web/src/modules/chat/`（`streamChat`） |
| 检索域 | `retrieval/.../chat stream`（SSE data 内 `type`） | `meta / progress / thinking_chunk / tool_running / answer_chunk / error` | `web/src/modules/knowledge/`（`useChatStream`） |

这是**双后端双模块的结构事实，不是同一流程的漂移**。两套词汇已在
`web/README.md`（本次更新）按域分别记录。
统一词汇表属跨域重构，收益低于风险，归入「另期」：若做，方向是共享
`web/src/api/` 层的事件归一化，而非改后端协议。

## 4. 零调用 / 死代码面

| 位置 | 证据 | 处置 |
|------|------|------|
| `application/services/search_service.py`（含 `generate_stream` 与整个 `SearchService`） | 全仓 0 生产调用（仅 p40 import 冒烟）；产 5 类事件 `search_start/search_results/direct_answer/direct_content` **0 前端/测试引用**；`SearchService` 类零 importer（含动态引用） | **已删除**（2026-09-27，Phase 27）：`search_service.py` + `search_prompts.py` + 空包 `application/prompts/` 一并删除；防复活守卫 `test_deleted_search_service_stays_deleted`（p40），随删随建——只删 `generate_stream` 会留下零调用的 `_call_with_tool`/prompt 孤儿，故整体删除 |
| `interfaces/http/v1/sessions.py`（`/{id}/snapshot` `:177` 等） | `web/src` 对 `/v1/sessions` **0 调用** | **保留**：API 面合法可达（编排域会话 CRUD），非死代码 |
| ops UI（事件查看） | 规划 §4 定为另期；`GET /ops/events` API 已就绪（Phase 23） | **另期**：事件只能 curl 查 |

## 5. 事件矩阵落地状态（Phase 23）

- 出站通知：`notify_user`（L1→MEDIUM、HITL 拦截、`NOTIFY_USER_ENABLED` 默认关）→
  `notify_bridge` → `get_event_port().publish` → ops 可查，链路全绿。
- 三后端参数化契约测试通过（memory / jsonl 真跑；redis **诚实 skip**——本机 6379 关，
  `RedisStreamEventBus` 构造不连库，连接错误向上传播不伪造）。
- 矩阵覆盖：dedup / timeout / cancel / concurrent / cross-restart。

## 6. 框架演进候选（结论：均不动）

| 候选 | 结论 | 依据 |
|------|------|------|
| LangGraph 图外扩展（事件/工具层） | 否 | 边界留在 `agent/graph/`，违者 p40 红 |
| 图内 `interrupt()` | 否（延续规划 §4） | async 节点实测否决（`multi_tool.py:151` 注释） |
| 新中间件/新数据库 | 否 | Redis Stream 复用既有 Redis；工程总则 |
| SSE 词汇统一 / sessions 端点并入 | 另期 | §3、§4；跨域重构收益低于风险 |

## 7. 文档体系（本次收尾动作）

- **删除 8 份过时/重复文档**：`CONFIG_SUMMARY`、`OPERATIONS`、`OPERATIONS_RUNBOOK`、
  `DEPLOYMENT`、`README_CHATBOT`、`README_DEV`（待并内容已并入 `web/README.md` 环境变量表）、
  `ROUTER_PROMPT_FIX`、`design-p26-schema-layering`。删除前全仓引用扫描清零。
- **ADR 重编号**：`0003-infrastructure-criticality` → `0006`（消除与 declarative-configuration
  撞号），索引 `adr/README.md` 补行，`CHANGELOG:170` 引用同步。
- **6 处路径 rot 修复**：`oncall.md`（postmortems 链接 + 告警 6→8 并补 2 条映射）、
  `secrets-management.md:24`、`release-and-rollback.md:66`（`scripts/db/`）、
  `bug_report.yml:19`、`configuration.md` 相关文档行、
  `components/utils/utils.py:117`（schema 文档补 `design/` 段——此前恒返回空串的运行期 bug）。
- 守卫复跑：p41 + p35 + code_hygiene + p36 + p17 + p40 = **114 passed**。

## 8. 遗留与优先级（2026-09-27 Phase 26 后更新）

1. ~~Phase 24–26 未执行~~ → **已完成**（规划 `Phase21-26_规划.md` §7 六阶段全绿，
   基线 2290 passed / 14 skipped）。
2. **Phase 17 全栈 record blocked**：无 Docker/LM Studio/Redis 真机，附录 A 清单待真机，不伪造。
3. **低优先清理**：§4 死代码面 `search_service` **已按两步流程删除**（Phase 27，
   含防复活守卫）；ops UI 另期、SSE 词汇另期。
