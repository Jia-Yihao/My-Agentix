# code exec：运行时写代码执行任务（项目级口径）

> 本文件是 **code exec 机制的项目级口径声明**。reviewer 拿它当验收基准。
> 与全局 `AGENTS.md` 的关系：全局只管流程与边界，本文件只管本机制的事实与措辞。
> 同一概念在项目内只有一种叫法——本文件的术语表是唯一真源。

## 1. 目标与非目标

**目标**：让 Agent 在**运行时**自己写 Python 代码来完成任务（而不是把每一步都翻译成
既有工具调用）。代码交给 `code_execute` 工具，在 subprocess 沙箱档里执行。

**非目标**（明确不做，写在这里是为了让后来者别顺手扩边界）：

- **不做多语言**：只做 Python。多语言就意味着每种语言一套闸，而闸是这套机制的安全底座。
- **不做容器档**：`container` 仍在阶梯词汇表里但**未实现**，声明即在执行点被拒。
- **不做文件系统隔离、不做网络隔离**：那是 ADR-0007 登记的残留（⑰ / T3），本机制只借用
  subprocess 档已经买到的那些闸。**"沙箱隔离"在本项目里永远不等于"笼子"**。

## 2. 术语表

| 术语 | 含义 |
| --- | --- |
| `code-query` | 路由意图。flag-on 时 L1 路由可以产出它，指向 code exec 这条支路 |
| `code_agent` | 图节点。承接 `code-query` 的 L2 节点，负责"写码 → 执行 → 读输出 → 必要时改码" |
| `code_execute` | 工具名。执行一段 Python 代码并返回结构化结果 |
| **墙内** | subprocess 档的 worker 进程（父进程起的那一层）。它是被平台注册的一方 |
| **孙进程** | 墙内再起的、**真正跑用户代码**的进程。它拿不到 wall 的父进程环境；它也**造不出父侧认可的成功信封**——父侧只认 nonce 匹配且 `ok` 为**严格布尔**的成功信封（协议 v2，见 `agentix/tools/subprocess_worker.py`），而孙进程的 stdout/stderr 被重定向到临时文件，压根不在协议通道上 |

## 3. 配置键

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `CODE_EXEC_ENABLED` | `false` | 业务总闸。开 = 路由可产 `code-query`、`code_agent` 节点可达、`code_execute` 工具注册 |
| `CODE_EXEC_MAX_ROUNDS` | `3` | **总尝试轮数上限（含首次执行）**——运行层失败才进这个循环（不是资源闸） |
| `CODE_EXEC_TIMEOUT_S` | `20.0` | 单次代码执行超时（秒）——一次执行的上限，不是整轮尝试的预算 |

**真正跑起来需要的前置开关**（`CODE_EXEC_ENABLED` 单独打开**跑不起来**）：

```text
CODE_EXEC_ENABLED=true                （业务总闸）
EXECUTION_SANDBOX_ENABLED=true        （执行器读 ToolSpec.sandbox 声明）
TOOL_SANDBOX_SUBPROCESS_ENABLED=true  （subprocess 档进入已实现集合）
运行平台是 POSIX
```

任一不满足 ⇒ 该次调用**失败并说明原因**（`sandbox_unavailable` / 档位 switched off /
平台不支持），**绝不静默退回进程内执行**。

### 3.1 与既有开关的两处交互（踩了会以为"代码跑不起来"）

1. **`TOOL_SELECTION_MAX_RISK` 默认 `L1`，而 `code_execute` 声明 `risk_level="L2"`**。
   `TOOL_SELECTION_ENABLED=true` 时 `get_tool_executor()` 会把
   `ToolSelectionPolicy(max_risk_level=TOOL_SELECTION_MAX_RISK)` 挂到执行器上，
   执行器的风险闸按**有效风险**判 ⇒ 默认上限下 `code_execute` 被**拒**，
   错误是「risk level L2 exceeds policy max L1」——**不是**沙箱问题、**不是**代码问题。
   要让本机制跑得起来，`TOOL_SELECTION_MAX_RISK` 须提到 `L2`（或 `L3`）。
2. **`HUMAN_REVIEW_ENABLED=true` 时该工具走人工审批**：`code_execute` 命中
   `execute` 审批词 ⇒ 有效风险 HIGH ⇒ 与它声明的 L2 一致。**没有批准人就是拒绝**
   （fail-closed），且该拒绝属**治理层终局**（见 §7），不进自愈循环。

另注：`code_execute` **不进 LLM 可见工具面**（不在 `registry_init._build_declarations()` 里），
所以 `DISABLED_TOOLS` 的**声明层**裁剪对它无效；但执行侧权限兜底
（`selection.tool_permission_check`）照常判 `DISABLED_TOOLS`。

## 4. 工具契约

**入参**：

| 参数 | 取值 | 约束 |
| --- | --- | --- |
| `code` | `str` | 非空字符串，**≤ 65 536 个字符**（量纲是**字符数**，不是字节数——协议层的**字节**闸另有其人：`agentix/tools/sandbox.py` 的 `MAX_REQUEST_BYTES` = 1 MiB） |
| `timeout_s` | `int` / `float` | 1–120 秒，缺省取配置 `CODE_EXEC_TIMEOUT_S`（缺省 20 秒） |

**返回**（`dict`，恰好六个字段）：

| 字段 | 含义 |
| --- | --- |
| `exit_code` | 进程退出码 |
| `stdout` | 标准输出 |
| `stderr` | 标准错误 |
| `timed_out` | 是否被超时截断 |
| `truncated` | 输出是否被截断 |
| `timeout_s` | 本次实际生效的超时秒数 |

**声明**（`ToolSpec`）：

```text
risk_level   = "L2"        （有效风险 HIGH ⇒ HUMAN_REVIEW_ENABLED 开时 HITL 可审）
sandbox      = "subprocess"
side_effect  = True
retryable    = False
capabilities = ("code",)
timeout_ms   = 150000      （工具级闸：覆盖 timeout_s 的**整个取值域** 1–120 秒 + 30 秒余量）
```

工具级超时是治理链的**外层**闸，代码级超时是**内层**闸：代码超时先到 ⇒ 返回
`timed_out=True`（可解释）；工具超时只是兜底。因此工具超时的基准取**代码超时的
取值域上界**（120 秒），不取当前配置值——否则调用方传 `timeout_s=120` 而配置是
20 秒时，工具侧会在代码超时之前把整次调用掐掉，用户看到的是"没有理由的失败"。

**不进 LLM 可见工具面**：它由 `code_agent` 节点**直接**经 `ToolExecutor.execute` 调用。
理由：它是执行面，不是"让模型自己挑的工具"——把它挂进声明清单，等于把"跑任意代码"
变成一次普通的工具选择。

**判定只有一个源**：哪些工具不进 LLM 可见工具面，由
`agentix/tools/definitions.py::NON_LLM_VISIBLE_TOOL_NAMES` 说了算（配套入口：
`is_llm_visible_tool()` / `llm_visible_specs()`）。**下面这些拼装点都受这条口径
约束，一律过这个判定——不许各自另写一份工具名判据**（纪律 6 ④：同一个判定不许
两个源；本地判据与真源漂移时，改一处漏一处）：

| 拼装点 | 位置 | 说明 |
| --- | --- | --- |
| 声明清单 | `agentix/tools/registry_init.py::_build_declarations()` | 清单是**显式声明**的（不是注册表全集）⇒ `code_execute` 不在其中 |
| OpenAI function schema | `agentix/tools/__init__.py::list_tool_specs()` / `list_openai_tools()` / `get_tool_spec()` | 按**注册表全集**导出 ⇒ 必须过筛：`register_handler(..., spec=...)` 会把 spec 顺带登记进注册表，不筛就会漏出去 |
| 任务拆解菜单 | `agentix/application/services/task_execution.py::_executable_menu()` | 菜单会写进拆解提示词，模型看得见就能点名调用 |

**MCP 导出面**（`agentix/capabilities/mcp/tool_source.py`）按 `risk_level == "L0"`
过滤，而 `code_execute` 声明 `L2` ⇒ 已天然被排除，不需要额外判定。

## 5. 隔离承诺与不承诺

**承诺（买到了的）**：

- 独立进程与独立地址空间；
- 三道资源闸：`RLIMIT_AS=512MB` / `RLIMIT_CPU=30s` / `RLIMIT_FSIZE=32MB`；
- 环境变量白名单（**不含任何密钥**）；
- 超时可杀的进程组；
- 孙进程在墙被杀时随之被杀（`PR_SET_PDEATHSIG`，**Linux 才有**（glibc 的 Linux；
  musl 镜像上探测保守判不支持 ⇒ 该承诺静默降级）——macOS 没有 `prctl`
  ⇒ 这一条在 macOS 上不成立，其余承诺不变）。**这一条现在有用例钉住**
  （`tests/test_p131_code_exec.py::TestParentDeathSignal`）：一条**静态 AST 守卫**钉住执行点真的
  接上了 `preexec_fn=_set_parent_death_signal`（任何平台都跑），另有一条**行为用例**在 Linux 上
  真起墙、真起用户代码进程，**只 SIGKILL 墙那一个 pid**（不组杀——组杀顺带带走孙进程的话，
  测的就不是这条承诺）后断言孙进程随之消失。
  **skip 判据与执行点同源**：新增公开探测 `parent_death_signal_supported()`
  （`agentix/code_exec_runtime.py`，已进 `__all__`）返回 `(bool, 理由)`，判据取自执行点**自己**
  模块加载期取好的 libc（有没有 `prctl`）——**刻意不另立平台判点**（纪律 6 ④）。
  **它答的是「平台有没有这个能力」，不是「这一次兑现了没有」**：执行点刻意**不检查 `prctl`
  的返回值**（`preexec_fn` 跑在 fork 之后、exec 之前，抛异常会让整次执行失败），所以运行时
  那一次调用仍可能静默失败 ⇒ 探测为真只保证"这一条落得下去"，失败仍由 worker 侧清理兜底。
  Windows / macOS 上那条行为用例恒 skip 是**按设计**（那条承诺在非 Linux 平台本就不成立），
  **不是漏测**。

**不承诺（没买到的）**：

- **文件系统隔离**——仍可读宿主可读的文件；
- **网络隔离**——仍可出网。

这两条是 ADR-0007 已登记的残留。本机制**不得**被描述成"完全隔离"。

## 6. 平台立场

**POSIX-only。** 非 POSIX 平台 / 墙不可用 / 资源闸读回未生效 ⇒ **fail-closed 拒绝执行**，
**永不降级为进程内执行**。Windows 上永不做无隔离降级。

## 7. 失败语义分层（本机制最容易做错的地方）

**治理层失败**（工具 `success=False`：沙箱不可用 / 风险被拒 / HITL 未批 / 参数非法）
⇒ **终局**。不再自修，不再重试——重试换不来一个实现。

**运行层失败**（工具成功，但退出码非零 / 超时）⇒ 把 `stdout` / `stderr` 喂回下一轮，
让模型改码。最多 `CODE_EXEC_MAX_ROUNDS` 轮**尝试（含首次执行）**。

**轮数用尽** ⇒ 产出失败回执（**含最后一次输出**），不假装成功。

## 8. flag-off 等价性承诺

`CODE_EXEC_ENABLED=false` 时：

- 路由**不产** `code-query`；
- 图里 `code_agent` 节点**不可达**；
- `code_execute` 工具**零注册**；
- 提示词与基线**逐字节相同**。

这是 Strangler 纪律的最低要求（`AGENTS.md` 硬性纪律 1）：flag-off 的生产链路必须与
基线**逐位等价**，否则"默认关"只是嘴上说说。
