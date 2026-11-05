# 安全策略 (Security Policy)

## 支持的版本

我们为下列版本提供安全更新。请始终升级到受支持的最新版本。

| 版本 | 支持状态 |
|------|---------|
| 0.1.x（当前开发线） | ✅ 活跃支持 |
| < 0.1.0 | ❌ 不再支持 |

## 报告漏洞

**请不要通过公开 Issue 报告安全漏洞。**

请通过以下任一方式私下报告：

1. **GitHub 私密漏洞报告**（推荐）：在本仓库 `Security` 标签页点击 *Report a vulnerability*
2. **邮件**：发送至项目维护者邮箱（见仓库主页联系方式），主题前缀 `[SECURITY]`

请在报告中尽量包含：

- 漏洞类型与影响范围（如：鉴权绕过 / 注入 / 信息泄露）
- 复现步骤（最小可复现示例最佳）
- 受影响的版本、配置与部署形态
- 若有，附上 PoC 或概念验证代码
- 你的联系方式与披露意向

### 响应时间线

| 阶段 | 目标时限 |
|------|---------|
| 确认收到 | 3 个工作日内 |
| 初步评估（严重性分级） | 7 个工作日内 |
| 修复或缓解方案 | 视严重性，30 天内 |
| 公开披露 | 修复发布后协调进行 |

我们会在 `CHANGELOG.md` 与 Release Notes 中致谢报告者（除非你要求匿名）。

## 安全设计概览

My Agentix 在架构层面内置了以下安全机制，便于你在部署与二次开发时参考：

| 机制 | 说明 | 位置 |
|------|------|------|
| **API Key 认证** | 可选 `X-API-Key` 中间件，使用 `secrets.compare_digest` 常量时间比较 | `agentix/main.py` |
| **CORS 白名单** | 基于配置的域名白名单，默认不允许通配符 | `agentix/main.py` |
| **路径遍历防护** | 文件上传拦截 `..` / `/` / `\` / `\x00`，上传路径经安全解析 | `agentix/interfaces/http/v1/upload.py` |
| **Cypher 只读闸门** | 执行前双闸门（修饰符白名单 + 正则拦截 `CREATE/DELETE/MERGE/SET/REMOVE/DROP/LOAD/CALL` 等写子句） | `agentix/.../components/cypher_tools/utils.py` |
| **标识符白名单** | LLM 输出的节点标签 / 属性键插值进 Cypher 前必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$` | `agentix/.../text2cypher/validation/validators.py` |
| **SQL 注入护栏** | kb_ingest MySQL 导入 `table` 标识符白名单（`^[A-Za-z0-9_]{1,64}$`）+ `where` 拒 `;` / `--` / `/*`，FastAPI body 校验自动 422 | `kb_ingest/kb_service/schemas/ingest.py` |
| **弱哈希治理** | 缓存 / 向量库键哈希显式 `usedforsecurity=False`（非安全用途不入哈希攻击面），bandit B324 清零 | `agentix/application/services/redis_cache.py` 等 3 处 |
| **Guardrails** | 每个查询经范围校验，越界请求被显式拒绝 | `agentix/application/agents/guardrails.py` |
| **错误信息收敛** | 对外不返回内部异常详情（仅 DEBUG 下附带 `detail`） | `agentix/application/services/chat_service.py` |
| **可信根不可自改** | 生产环境缺失 `SECRET_KEY` 直接启动失败，禁止运行时随机生成 | `agentix/config/settings.py` |
| **XSS 防护** | 前端 HTML 实体转义 + 安全 linkify | `web/src/components/AgentChatWidget.vue`、`web/src/modules/knowledge/utils/markdown.ts` |
| **文件大小限制** | 统一使用 `settings.FILE_UPLOAD_MAX_MB`，分块写入防 OOM | `agentix/interfaces/http/v1/upload.py` |
| **工具超时真截断** | 非协程 handler 下放 `asyncio.to_thread`，事件循环让出来之后 `asyncio.wait_for` 才取消得掉（默认关，行为变更需逐工具确认 loop 亲和） | `agentix/tools/gateway/executor.py` |
| **MCP 子进程环境白名单** | stdio 子进程只拿「起进程 + 找包 + 正确编码 + 证书 + 代理」所需的键，不再继承 `LLM_API_KEY` / `SERPAPI_KEY` / `DATABASE_URL`；第三方 server 需要自己的凭据时用 `MCP_SUBPROCESS_ENV_EXTRA_KEYS` 显式加名 | `agentix/capabilities/mcp/client.py` |
| **只读 SQL 词表（两闸共用）** | 先在等长处抹除字符串/注释/反引号标识符，再按词边界匹配危险词——避免 `SELECT 'it''s'` 误判，也让语法闸与执行闸不再各持一份互相矛盾的黑名单 | `agentix/domain/policies/sql_read_only.py` |
| **表达式资源护栏** | AST 节点白名单**只挡语法不挡资源**，故另加值域上限：表达式长度、`**` 指数模、整数结果位宽、浮点有限性 | `agentix/capabilities/mcp/tool_source.py` |
| **日志凭据脱敏** | httpx 异常文本会原样带出请求 URL（密钥常在 query string 里）；对 query / header / `Bearer` 三类形态把值打成 `***` 并保留键名 | `agentix/tools/adapters/search.py` |
| **密钥只写不读** | 设置接口按 `*_api_key` / `*_secret` / `*_token` 后缀掩码（保留末 4 位供辨认配的是哪个 key），PUT 拒收掩码回写（否则把真 key 覆盖成 `***`）、未知键 422 | `agentix/retrieval/api/routers/settings.py` |
| **SQL 日志出口独立** | `DB_ECHO` 三态开关；留空沿用旧的 `bool(DEBUG)` 推断，而 `DEBUG` 出厂为 `True` ⇒ echo 把完整 SQL 连参数走标准库 logging 绕过 loguru 出口 | `agentix/infrastructure/core/database.py` |
| **生产身份显式声明** | `APP_ENV` 优先于 legacy `ENVIRONMENT`，两者皆空才回落 `DEBUG` 反推；未显式声明且闸门关闭时启动打 `SECURITY POSTURE` 告警点名当前关掉的 flag | `agentix/retrieval/settings.py`、`agentix/main.py` |
| **跨服务连接串闸门** | `kb_ingest` 的 `connection_url` 直连 `create_engine()`，持 key 者可使服务端连任意 `host:port`（SSRF / 读容器内 `sqlite:` 文件）。开启后校验「scheme 必须 mysql + 主机在白名单」，**刻意不做 DNS 解析**（解析结果与实际连接之间无绑定关系，却把外网延迟引入导入）；另拒 query 里改写客户端行为的选项（`local_infile` 族、`read_default_file/group`、带 `CLIENT_LOCAL_FILES` 位的 `client_flag`）——主机白名单管「连到谁」，这些选项管「对端可以要求本客户端上传哪个文件」；拒绝时 detail 只说原因，不回放输入 | `kb_ingest/kb_service/core/db_url_guard.py` |
| **工具 handler 类型契约** | 「这条工具的 `timeout_ms` 到底会不会生效」原先只能在排障时反推：非协程 handler 占住事件循环，`wait_for` 取消不了它。开 `TOOL_HANDLER_CONTRACT_ENABLED` 后注册期按**可等待口径**（`handler_shape`：类的 `async __call__` 算 async、异步生成器单列）点名告警并附声明超时。**告警不拒绝注册**——仓里最重的几个工具本就是同步 I/O，fail-closed 等于在开 flag 那刻打断全部功能；真正解除阻塞仍靠 `TOOL_SYNC_OFFLOAD_ENABLED` | `agentix/tools/gateway/executor.py` |
| **同名工具声明不允许静默丢弃（G14）** | 同名再注册且 `ToolSpec` 不同时，两个注册入口（executor / registry）**默认**打印点名 `TOOL DUPLICATE REGISTRATION:` 告警，列出差异字段名（不搬运 `description` 等文本值；executor 侧另回显存活声明的 `sandbox`/`risk_level` 枚举值）并说明执行时读哪一份；`TOOL_DUPLICATE_REGISTRATION_STRICT=true` 才在改动 registry/handler **之前**拒绝。默认语义仍是 executor「第一次赢」/ registry「最后一次赢」——所以默认路径买到的是**可见**，要**不可能**须在部署里打开严格模式（告警本身没有开关） | `agentix/tools/gateway/executor.py`、`agentix/tools/definitions.py`、`agentix/domain/tool/spec.py` |
| **执行位置是声明出来的** | `ToolSpec.sandbox` 决定这条 handler 在哪儿跑，`EXECUTION_SANDBOX_ENABLED`（默认关）打开才读声明。声明校验只判「在不在阶梯词汇表里」，不判「实现没实现」：`container`（以及 ⑮ 档位关着、或本机落不下资源闸时的 `subprocess`）会在执行点拿到 `sandbox_unavailable` 且**不可重试**，**不静默退回进程内**——静默降级会让「以为有墙」这个误判一直活着。⚠️ 默认两档 `in_process` / `thread` 同进程、同 GIL、同 `os.environ`、同文件权限，**不构成任何隔离边界**（[ADR-0007](docs/architecture/adr/0007-执行沙箱端口.md)） | `agentix/domain/tool/sandbox.py`、`agentix/tools/sandbox.py` |
| **子进程沙箱档（⑮：真的有一面墙）** | 声明 `sandbox="subprocess"` 且 `TOOL_SANDBOX_SUBPROCESS_ENABLED=true`（默认关）时，一次调用一个进程：子进程侧 `apply_gates` **先落资源闸** `RLIMIT_AS=512MiB` / `RLIMIT_CPU=30s` / `RLIMIT_FSIZE=32MiB`（可选 `TOOL_SANDBOX_SUBPROCESS_UID` 降权，默认 `-1` 不降；root 容器实测降到 65534，非 root 实测以 `stage=limits` 终局拒绝）**再 import 业务代码**；环境变量走与 G2 **同一份**白名单（白名单里的 `HTTP_PROXY`/`HTTPS_PROXY` 会把代理 URL 一起带进墙里，而代理 URL 的 `user:pass@` **本身就是凭据**——这是登记在案的已知边界）；协议是 **v2**：stdin 一条 JSON 请求 + stdout 一行带哨兵的 JSON 应答，两侧各有体积闸（请求 1MiB / 应答 8MiB），**成功信封必须回显本次调用的 nonce**（墙内代码有 stdout 写权限，只靠哨兵行认应答等于把伪造应答的笔交给它）且**退出码非 0 时 stdout 上的信封一律不采信**（实测：旧口径下"墙内自己印一条 ok 信封"会被当成成功结果返回），**`ok` 字段还必须是严格布尔**（`1`/`1.0`/`"true"`/缺失一律按畸形信封拒——真值判定下"印一条 `"ok": 1` 的信封"同样会被当成成功结果，归属凭证在那条路上压根没执行）；取消经 `start_new_session` + `os.killpg` 杀掉整个进程组。**只有 POSIX 落得下**（⑯ 能力探测判的是 `resource` + 进程组，不是「某个二进制在不在」），Windows 上是执行点拒绝。**进程数闸 `RLIMIT_NPROC`（上限 32 个任务）已实现但有前置**：只在 `TOOL_SANDBOX_SUBPROCESS_UID` 配了**独立且非 0** 的 uid 时才落——该闸按整个 real uid 在全内核范围计费，共用父 uid 时要么假拦要么形同虚设，uid 0 因持 `CAP_SYS_RESOURCE` 被内核豁免（实测 soft=5 写进去并回读到 5、照样连起 12 个进程），落不下时回传 `nproc_gate.applies=false` 并告警一次；⚠️ 计费单位是**任务（线程也算）**，容器里可见的进程数/任务数都是下限。**不含**文件权限收紧与出网限制（属 ⑰） | `agentix/tools/sandbox.py::SubprocessSandbox`、`agentix/tools/subprocess_worker.py`、`agentix/domain/tool/exec_env.py` |
| **运行时写代码执行（code exec，默认关）** | `CODE_EXEC_ENABLED=true`（默认关）时，模型可在运行时写 Python 代码完成任务：经 `code_execute` 工具（声明 `sandbox="subprocess"` / `risk_level="L2"`，有效风险 HIGH，HITL 可审，**不进 LLM 可见工具面**、由节点直接调用）在 subprocess 档里执行。**隔离到的**：独立进程与地址空间、`RLIMIT_AS=512MiB` / `RLIMIT_CPU=30s` / `RLIMIT_FSIZE=32MiB` 三道资源闸、环境变量白名单（不含任何密钥）、超时可杀的进程组。**没隔离到的**：**文件系统与网络**——仍可出网、仍可读宿主可读的文件，这是 ADR-0007 已登记的残留，**不是"完全隔离"**。**非 POSIX 平台 fail-closed 拒绝执行**；Windows 上永不做无隔离降级。真正跑起来还需 `EXECUTION_SANDBOX_ENABLED=true` + `TOOL_SANDBOX_SUBPROCESS_ENABLED=true` + 平台是 POSIX，任一不满足 ⇒ 该次调用失败并说明原因 | [ADR-0007 执行沙箱端口](docs/architecture/adr/0007-执行沙箱端口.md)、`agentix/tools/sandbox.py` |

## 部署安全建议

- **必须**在生产环境显式配置 `SECRET_KEY` 与 `API_KEY`，并将 `REQUIRE_API_KEY=true`
- **必须**显式声明 `APP_ENV=production`：生产身份原先由 `DEBUG` 反推，忘设 `DEBUG=False`
  会让配置校验的硬失败分支**整段静默不生效**——该拒绝启动的错配置会照常对外服务。
  硬失败面判的是 `STRICT_VALIDATION`（`prod` / `production` / `staging`）：预发的价值正是
  「用生产的失败语义提前跑一遍」，把它降级成观察环境等于把这道校验从发布链路上摘掉
- **默认关闭的闸门在打开之前不得对外**：`REQUIRE_API_KEY` / `V1_REQUIRE_JWT` /
  `RATE_LIMIT_ENABLED` / `HUMAN_REVIEW_ENABLED` / `TOOL_GUARDRAILS_ENABLED` /
  `TOOL_SYNC_OFFLOAD_ENABLED` / `MCP_SUBPROCESS_ENV_SCRUB`。启动日志的
  `SECURITY POSTURE` 行会点名当前处于关闭状态的那些
- 生产显式 `DB_ECHO=false`、`UVICORN_RELOAD=false`（两者留空时沿用 `DEBUG` 推断的旧行为）
- 在预发开一次 `TOOL_HANDLER_CONTRACT_ENABLED=true`，把启动日志里点名的非协程 handler 清单
  抄进部署记录：那些工具的 `timeout_ms` 在 `TOOL_SYNC_OFFLOAD_ENABLED=false` 时不会截断，
  「哪个工具能占住这个 worker 多久」必须是已知量，而不是等一次线上停摆再反推
- `TOOL_DUPLICATE_REGISTRATION_STRICT` 默认 `false`：预发先收集 `TOOL DUPLICATE REGISTRATION:`
  点名的清单（或确认每条都是无害重绑），再由运维显式打开；打开后同名且声明不同的注册会让
  启动失败。预发 override 刻意钉 `false`，避免宿主机 `.env` 决定预发是否 fail-closed
- `EXECUTION_SANDBOX_ENABLED` 在预发置 `true` 的目的**不是买隔离**（默认档 `in_process` 与关着
  逐位等价，它也不进 `SECURITY POSTURE` 的点名清单——那条清单只列「关掉会让安全属性变弱」的闸门，
  这两位（`EXECUTION_SANDBOX_ENABLED` / `TOOL_SANDBOX_SUBPROCESS_ENABLED`）开与关都不改变边界，
  守卫是 `tests/test_p125_subprocess_sandbox.py::test_the_two_sandbox_keys_are_deliberately_not_posture_flags`）。
  它的价值是让这条新缝被持续走到：默认档与关着逐位等价，所以打开的代价接近零。
  ⑮ 落地**之后**这条仍然成立，只是拒绝的形状变了：预发**没有**把 `TOOL_SANDBOX_SUBPROCESS_ENABLED`
  钉进 override（继承基线 `False`），所以声明 `subprocess` 的工具在预发拿到的是「档位 switched off」
  的终局拒绝；要在预发真走墙，得显式开档位并确认该机是 POSIX（否则拿到的是能力探测拒绝），
  步骤与判据见 [subprocess 沙箱验证 Runbook](docs/runbooks/子进程沙箱取证.md)
- 打开 `MCP_SUBPROCESS_ENV_SCRUB` 后，第三方 MCP server 需要凭据时**只加它需要的那几个键名**，
  不要退回继承整个环境
- 跑 `kb_ingest` 时一并设置 `KB_REQUIRE_API_KEY=true` + `KB_API_KEY`（入站闸门，默认关；
  开启却不配 key 是 fail-closed 的「全部 401」，那是锁死不是安全），并按实际数据源填
  `KB_MYSQL_URL_GUARD=true` + `KB_MYSQL_ALLOWED_HOSTS=db.internal,*.intranet.example`；
  白名单留空时闸门只校验 scheme，别把它当成已经收紧
- 同一服务的 `KB_MYSQL_WHERE_PARAMETRIZED=true` 把导入过滤从「拼接」改成「结构化 + 绑定」，
  但它是**硬切**：开当天仍在传 `where` / `--where` 的导入作业会全部 400。顺序是
  「调用方先迁到 `filters`（CLI 用 `--filters` 传 JSON）⇒ 确认没有存量作业还在传片段 ⇒ 再开」，
  别在生产直接翻开
- **不要**将 `.env` 提交到版本库（已在 `.gitignore` 中排除）
- 生产环境请通过密钥管理服务（KMS / Vault / 环境变量注入）下发密钥
- 数据库（Neo4j / PostgreSQL / MySQL / Redis / Milvus）请勿暴露公网，使用内网或私有网络
- 反向代理层启用 TLS，并限制请求体大小与速率
- 容器侧 backend 与 retrieval-worker 已带 `security_opt: no-new-privileges:true` 与
  `deploy.resources.limits`（CPU / 内存 / pids，可用 `BACKEND_CPUS`、
  `BACKEND_MEMORY`、`BACKEND_PIDS_LIMIT` 放宽或收紧），以及运行身份旋钮
  `AGENTIX_CONTAINER_USER`（默认 `0:0` ⇒ **仍以 root 运行**，与加固前逐位等价）。
  降权不是改一个值：非 root 写不进 `./data` / `./uploads` 时 entrypoint 的
  `set -euo pipefail` 会表现成 **restart 崩溃循环**而不是拒绝启动，所以必须先在宿主机
  准备目录属主——步骤见 [staging Runbook](docs/runbooks/预发环境与配置矩阵.md) 的「容器降权」一节。
  降权买到的是「写宿主挂载要过属主检查 + setuid 位失效」，买不到 `cap_drop`、
  只读根文件系统与 user namespace 映射（容器内 uid 与宿主 uid 仍是同一身份）
- 定期运行 `pip-audit` 复核依赖漏洞；仓库开启 Dependabot 安全公告与安全更新，版本升级按需手动执行（`open-pull-requests-limit: 0`）

## 依赖安全现状

- 以 `pip-audit -r requirements.txt` 实测为准（2026-10-04 实测，**历史读数**）：残余 **21 项 / 8 包**，全部属 langchain 0.3.x 生态（langgraph-checkpoint、langgraph、langchain-core、langchain、langchain-openai、langchain-text-splitters、langgraph-sdk、langsmith），清零需 langchain 1.x 大版本迁移。
  - **LG-0-2（2026-10-27）已把 langchain 家族迁到 1.x**（`langgraph 1.2.18` / `langchain-core 1.7.0` / `langsmith 0.14.13` 等，见 `requirements.txt`），上面那 8 包**全部换了版本线** ⇒ 21 项既不能当作已清零，也不能沿用为现状：**必须在 1.x 栈上重跑 `pip-audit -r requirements.txt` 才能出结论**（本次环境未装 `pip-audit`，未复测，故不写新数字）。
  - 核心 Web 栈（`fastapi==0.142.2` / `starlette==1.7.0` 等）漏洞在 2026-10-04 那一轮已清零，本次未动这两个 pin。
- `bandit`（medium+ 级别）当前 **0 findings**；LOW 级信息性项（try/except pass 等）保留并逐项复核。
- 升级`requirements.txt` 与 `pyproject.toml` 双声明必须同步（`tests/test_dependency_consistency.py` 守卫），升级后以全量测试套件验证（README §11 唯一真源）。

## 已知边界（非缺陷）

- `Guardrails` 为**概率性**范围判定（LLM 驱动），不能替代确定性的访问控制；请勿将其作为唯一的安全边界
- 本地开发默认使用 LM Studio，若切换为云端模型，请评估数据出境与规范要求
- **有一面墙，但默认不开、平台不对称、且只挡住它声明过的那几个维度**：T2 ⑭ 把「这条工具在哪儿跑」做成
  `ToolSpec.sandbox` 声明（默认档 `in_process` 与声明引入前逐位等价），⑮ 补上阶梯第三档 `subprocess`
  （`TOOL_SANDBOX_SUBPROCESS_ENABLED`，默认关）：一次调用一个进程，子进程侧**先落
  `RLIMIT_AS=512MiB` / `RLIMIT_CPU=30s` / `RLIMIT_FSIZE=32MiB` 再 import 业务代码**、环境变量按与 MCP
  **同一份**白名单裁剪（Linux 容器实测：父进程里的 `DATABASE_URL` / `LLM_API_KEY` 不过界，墙内
  `open(os.environ['DATABASE_URL'])` 因键被裁掉而失败）、取消经 `start_new_session` + `os.killpg`
  真杀进程组；能力探测不过（Windows 没有 `resource` 与进程组 kill）⇒ 在执行点响亮拒绝，
  **绝不静默退回进程内**。**进程数闸（`RLIMIT_NPROC`）已实现但有前置**：只有
  `TOOL_SANDBOX_SUBPROCESS_UID` 配了一个**独立且非 0** 的 uid 时才落下（那道闸按整个 real uid
  在全内核范围计数、而 uid 0 持 `CAP_SYS_RESOURCE` 被豁免，共用父 uid 时它要么假拦要么形同虚设），
  没落时墙回传 `nproc_gate.applies=false` 并响亮告警一次；⚠️ 落了的闸按**任务**（线程也算）计费，
  容器里看得见的两个计数都只是下限。**它没挡住的**（约定 6 ①：白名单只覆盖它声明过的那个维度）：文件权限
  （只读根 / 指定可写目录）、出网（网络默认拒绝）——那三件一起属 ⑰/T3；
  `unshare` / namespace / seccomp 一律未接线，容器默认仍以 root 运行（`AGENTIX_CONTAINER_USER=0:0`）。
  所以口径是「**是墙，不是笼子**」：handler 崩了、把内存吃了、把环境密钥读了都出不了进程，但它想
  `open()` 一个 world-readable 的文件、或往外连一个 socket，这一档不管。若计划引入「执行用户提供
  代码」的工具，⑮ 是**必要条件而非充分条件**，须先补 ⑰（路线见 [docs/项目实现手册.md](docs/项目实现手册.md)
  §24.3 的 T2/T3 与 [ADR-0007](docs/architecture/adr/0007-执行沙箱端口.md) 的「未收口」清单）；
  取证步骤见 [subprocess 沙箱验证 Runbook](docs/runbooks/子进程沙箱取证.md)
- **密钥掩码不等于授权**：设置接口对 `*_api_key` 等字段只写不读，但「谁能读写哪个 kb」
  由 `SETTINGS_AUTHZ_ENABLED` 决定且**默认关**——关着时任一已登录用户仍可读改任意
  `kb_id` 的配置。这里的残余风险不是读到密钥（掩码已挡），而是**可写字段本身就是一条
  出站通道**：把 `llm_base_url` / `embedding_base_url` 改指外部端点不需要任何密钥，
  却足以让该租户的提问内容整批外送。打开后读要求 kb 属于调用者租户、写再要求 admin；
  两个前置（`tenant_id` 回填到无 NULL、该租户内确有 admin——全仓没有自助升 admin 的接口）
  与切换步骤见 [staging Runbook](docs/runbooks/预发环境与配置矩阵.md)「设置接口授权收紧」一节
- **`kb_ingest` 的连接串闸门默认关**：`KB_MYSQL_URL_GUARD=false` 时 `connection_url`
  仍是未校验的用户输入（SSRF / 内网探测 / 凭据外带面原样存在）。开启后只校验
  「scheme 必须 mysql + 主机在 `KB_MYSQL_ALLOWED_HOSTS` 内」，**不做 DNS 解析**，
  因此它拦的是配置意图，不是 IP 归属；白名单留空时等于只查 scheme。
  无论开关状态，该服务都请只在受信网络内可达，勿直接暴露
- **`kb_ingest` 的过滤参数化闸门默认关**：`KB_MYSQL_WHERE_PARAMETRIZED=false` 时，导入的
  过滤条件仍走「黑名单 + 字符串拼接」——它拦得住 `;` 堆叠语句与注释，拦不住
  `1=1 OR 1=1`、`id IN (SELECT … FROM mysql.user)` 这类**合法但越界**的片段：对拼出来的
  SQL 而言，调用方给的就是语法。开启后原始片段一律拒，过滤只能走结构化 `filters`
  （列名过标识符白名单、运算符取封闭枚举、值全部 bindparams）。**这一位是破坏性变更**
  （开当天仍在传 `where` 的作业全部 400），迁移顺序写在两个 `.env.example` 里，
  口径见 [运行时护栏 §15.9](docs/architecture/运行时护栏.md)
- **审批与恢复状态的持久性是显式开关才成立的保证**：`CHECKPOINTER_FAIL_CLOSED` 默认关——Redis
  （checkpointer 后端）路径失败（包未安装 / 连接与 `asetup()` 失败）时服务**不会拒绝启动**，
  而是照旧静默降级为进程内存态的 `MemorySaver`（连接与 `asetup()` 失败 ⇒ 打 **ERROR** 日志；
  依赖包未安装 ⇒ 打 **WARNING** 日志；两者均另有 `_record_degraded("checkpointer")` 降级指标，
  但服务会继续跑），重启即失 ⇒ 会话与 HITL 审批态**不持久**。因此「审批必须可追溯 /
  可恢复」属于**需要显式开 `CHECKPOINTER_FAIL_CLOSED=true` 才成立的保证**，不是默认保证
  （显式 `CHECKPOINTER_TYPE=memory` 不属故障路径，开关为真也不抛）
- **运行态落库面（T21 `agent_run_states`）落的是业务内容，且没有保留期**：
  `RUN_STATE_PERSIST_ENABLED=true`（默认关）时 `agent_run_states.meta` 存的是运行登记面的
  **JSON 快照**，可能含任务预览（`task_preview`，任务前 200 字符）、结果（回答正文与引用来源）
  与会话/线程标识（`session_id` / `thread_id`）——它们是**业务内容而非凭据**（不落 LLM key、不落 `DATABASE_URL`），
  但按数据分级仍应与会话内容同等对待。该表**无 TTL、无自动清理**（与 `hitl_pending_approvals`
  的有界策略不同；内存面 FIFO 500 淘汰的只是进程内存），长期开启需自建归档/清理策略；
  停用或回滚时 `DROP TABLE agent_run_states` 即清除。库形态要分两种看：**从未开过 flag 的库不会被
  建出这张表**（建表只发生在 flag-on 的装配点）；**曾开过 flag 的库需要 `DROP TABLE agent_run_states`
  才清除**——关掉 flag 只是不再读写它（表在也不读写），表本身不会自己消失
- **路由结果缓存（`ROUTING_CACHE_ENABLED`，默认关）的数据口径**：进程内缓存**只存**问句的
  sha256 摘要 + 四个字段（`type` 为封闭枚举，`logic` / `decision` 为模型生成的短文本，
  `confidence` 为数值），TTL 300 秒内有效，**不存问答原文、不存 `reasoning`**；键只存归一化
  问句的 sha256，不含问句原文，命中原样回填本次请求原文；
  每个进程各自一份、不落盘、进程重启即清空，不构成跨副本的数据面

---

感谢你帮助保持 My Agentix 及其用户的安全。
