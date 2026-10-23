# ADR-0007: 工具执行位置做成可声明的端口（ExecutionSandbox）

- 状态：已采纳（**部分**：⑭ 端口 + 两档实现已落地；⑮ `subprocess`、⑯ 能力探测**未实施**）
- 日期：2026-10-23
- 相关：[ADR-0001](0001-layered-architecture-with-ports.md)（分层 + 端口）、
  [ADR-0006](0006-infrastructure-criticality.md)（依赖关键性）、
  [PROJECT_SPEC §24](../../PROJECT_SPEC.md)、[runtime-guardrails §15](../runtime-guardrails.md)、
  [source-code-study §2.4 / X.1](../../source-code-study.md)

## Context（背景）

统一 `ToolExecutor`（`agentix/tools/gateway/executor.py`）管住了**要不要执行**
（有效风险 → 权限 → 人工审批 → 护栏），也管住了**执行多久**（`timeout_ms` + 重试 + 舱壁），
但从没管过**在哪儿执行**：所有 handler 都在 backend 那个进程里被 await。于是：

1. 一个 handler 崩出进程边界的能力 = 零。它抛异常会被 `execute` 吞成 `ToolResult`，
   但 `os.environ`、文件权限、`ulimit`、宿主 namespace 与主进程**完全共享**。
   §24.2 的结论因此是「闸门严、边界有了一格、但仍然没有墙」。
2. 唯一一次"下放线程"（§24.2 G1，`TOOL_SYNC_OFFLOAD_ENABLED`）是**全局 flag**：
   为了让一条同步 handler 的超时能截断，要改变**整个实例**所有 handler 的循环亲和语义。
   这不是隔离，是一刀切的副作用交换——而且「哪条工具需要下放」这件事在代码里不可声明、
   不可 grep、不可测。
3. 真正走出去的那条路径（MCP stdio 子进程）证明边界是会漏的：子进程默认继承主进程
   **全部**环境变量，其中含 LLM/SERPAPI 密钥（G2 才把它收成白名单）。
   也就是说：一旦出现进程外执行，"带什么出去"立刻成为新的信任边界——
   这恰好是 ⑮ 必须先解决的问题，不是可以后补的细节。
4. `claw-code` 的对照（source-code-study §2.4）显示成熟实现把沙箱做成**能力探测**
   （探测内核能力，而不是看某个 binary 存不存在）；本项目连"执行位置"这个概念都没有，
   所以谈不上探测。

问题的形状因此不是「要不要上 gVisor」，而是**先有没有一个可以声明的位置**。
今天若直接给某条工具加子进程执行，改动会散落在 executor 的重试循环里，
下一条工具再要另一种 isolation 时又改一遍——端口缺失的代价就是这么还的。

## Decision（决策）

1. **端口在域层**：`agentix/domain/tool/sandbox.py` 定义 `ExecutionSandbox`（`kind` +
   `async run(handler, arguments)`）、可声明的档位词汇表 `SANDBOX_LADDER`
   = `in_process | thread | subprocess | container`、以及 `SandboxUnavailable`。
   域层零第三方依赖、不 import 其它层（ADR-0001 的依赖方向）。
2. **实现在基础设施层**：`agentix/tools/sandbox.py` 提供 `InProcessSandbox`、
   `ThreadSandbox` 与 `resolve_sandbox(kind)`。**G1 那条下放口径全项目只有一份**
   （`invoke_in_process`），`ToolExecutor._invoke` 也调它——不存在"第二套语义"。
3. **声明进 Spec**：`ToolSpec.sandbox` 默认 `in_process`；构造期只校验"是不是阶梯里的词"
   （拼写错立刻报错），**不**校验"当前能不能跑"。
4. **执行点 fail-closed**：`EXECUTION_SANDBOX_ENABLED`（默认关）打开后，executor 按声明
   解析执行位置；解析不到实现就抛 `SandboxUnavailable` 并**拒绝执行**，
   **绝不静默退回进程内**。拒绝排在观测钩子之前——它是一次闸门拒绝，不是一次执行。
5. **超时不归沙箱**：截断仍由 executor 外层的 `pooled_wait_for` 统一负责。
   若每个沙箱自备超时，「这条工具的 `timeout_ms` 到底生不生效」就又变成要读实现才知道的事。
6. **flag 关时逐位等价**：关 = 完全不查 `spec.sandbox`，走的还是原来那行 `_invoke`。

## Rationale（理由与被拒方案）

| 档位 | 买到什么 | 代价 / 前置 | 结论 |
|---|---|---|---|
| `in_process` | 零开销，现状语义 | 什么都不隔离：同进程、同 GIL、同 `os.environ`、同文件权限 | **默认档**（改它 = 改全项目每条工具调用的语义） |
| `thread` | 「别占住事件循环」——超时能截断，且**按工具声明**而非全局 flag | 仍不是隔离；线程池有自己的上限与排队语义 | **已实现**。它把 G1 从"实例级一刀切"降级成"工具级表态" |
| `subprocess` | 独立地址空间、`rlimit`（CPU 秒/地址空间/文件大小/进程数）、独立 uid、精简环境 | ① 每次调用一个进程 → 冷启动与序列化开销；② **环境白名单是前置**（G2 的教训），带什么出去必须显式；③ `rlimit`/`setuid` 是 POSIX，开发机是 Windows ⇒ 只能"能力探测 + 明确降级"，不能假装跨平台；④ 返回值必须可序列化，进程死了怎么取消 | **未实现（T2 ⑮）**。端口已为它留好位置 |
| `namespace` / `seccomp` 直接接线 | 内核级隔离，最接近"墙" | 需要容器特权或额外 capability，与同一份 compose 里"不额外给能力"的降权方向**正面冲突**（source-code-study X.1 的同一结论） | **拒绝**：在单机 compose 形态下，它买到的隔离要用更大的攻击面去换 |
| `container`（每调用一次） | 文件系统/网络/进程三重边界 | 冷启动以百毫秒～秒计；镜像供应链成为新依赖；挂载与凭据下发要单独设计 | **未实现（T2 ⑮ 之后的形态选择）**。当前所有工具的延迟预算（`timeout_ms` 默认 30s，检索类常为百毫秒级）容不下 |
| gVisor / Firecracker（microVM） | 不可信代码执行级隔离 | 需要专门节点与编排；运维面完全超出本项目 | **留给 T3**，且先决条件是"确实存在不可信代码工具"（今天是 `L3` 禁止注册，没有这类工具） |

被拒的还有一个更省事的方案：**不给端口，直接在 executor 里加一个 `if subprocess: ...` 分支**。
它今天能跑，但「执行位置」就永远不是一个可声明、可校验、可测的概念——
⑮ 到来时改的是执行链本身而不是新增一个实现，而 ADR-0001 的依赖倒置在这里就白写了。

## Consequences（后果）

**正面**

- 「这条工具在哪跑」第一次变成声明式、可 grep（`sandbox=`）、可被测试钉住的事实。
- G1 的一刀切有了替代写法：某条会占循环的 handler 声明 `thread`，不必为它把全实例语义换掉。
- 阶梯词汇表与实现集合的**差集是可见的**（`implemented_kinds()` 对 `SANDBOX_LADDER`），
  这使"T2 还差什么"能被机械检查，而不是靠文档里一句「尚未完成」。
- ⑮ 的落地成本从"改执行链"降为"加一个实现类 + 进注册表"。

**负面**

- **它没有带来任何新的隔离。** 端口打开后生产环境仍然没有墙：两档能跑的实现共享
  进程的一切。把它读成"沙箱已就位"是错误的——§24.2 的结论要等到 ⑮ 才改变。
- 多了一层抽象与一个 flag；默认关时它只是声明，不改变行为，但读代码的人需要知道这件事。
- `EXECUTION_SANDBOX_ENABLED=true` + 某工具声明了未实现档位 ⇒ 该工具**全量失败**
  （这是设计意图，不是 bug，但它是破坏性的：所以 staging 先开、生产按需）。

**缓解**

- staging override 已置 `true`（`docker/compose.override.staging.yml`），预发持续走这条缝；
  默认档 `in_process` 与关着逐位等价，因此 staging 打开不改变结果，只改变代码路径。
- 生产基线 compose 转发 `${EXECUTION_SANDBOX_ENABLED:-False}`：flag-off 等价性一路到部署层。
- 不把它列进 `main.py::_INSECURE_GATE_FLAGS`：判据是"关着会让实例不该对外"。
  它关着时的行为与今天的基线**逐位相同**，列进来只会稀释那条告警（与
  `TOOL_HANDLER_CONTRACT_ENABLED` 同一口径）。
- 回归：`tests/test_p114_execution_sandbox.py`（真跑 handler，不是断字符串——
  含「未实现档位声明时 handler 一次都没被调用」「`thread` 档真的让事件循环空得出来」
  「flag-on 默认档与 flag-off 返回值逐位一致」「阶梯与实现集合的差集口径」）。

## 未收口（本 ADR 不声称已完成的部分）

- ⑮：`subprocess` 实现 + 环境白名单 + `rlimit` 能力探测（探测失败必须**显式降级并记账**，
  不能静默当作已隔离）+ 独立 uid + 只读根 + 指定可写目录。
- ⑯：能力探测优先于二进制探测（对齐 `unshare_probe` 的结论）。
- 验收口径不变：「同步 15s handler 不阻塞并发协程」压测；
  **沙箱内 `open(os.environ['DATABASE_URL'])` 必须失败**——这一条现在是假的，
  它要等 ⑮，在那之前 §24.2 的「没有墙」仍是当前事实。
