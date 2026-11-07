# 安全策略

## 支持的版本

| 版本 | 支持状态 |
|------|---------|
| 0.1.x（当前开发线） | 活跃支持 |
| < 0.1.0 | 不再支持 |

## 报告漏洞

请不要通过公开 Issue 报告安全漏洞。可用以下两种私下渠道：

1. GitHub 私密漏洞报告（推荐）：在本仓库 `Security` 标签页点 *Report a vulnerability*
2. 邮件：发至项目维护者邮箱（见仓库主页联系方式），主题前缀 `[SECURITY]`

报告中尽量包含：

- 漏洞类型与影响范围（如鉴权绕过、注入、信息泄露）
- 复现步骤，最小可复现示例最好
- 受影响的版本、配置与部署形态
- 有 PoC 的话一并附上
- 你的联系方式与披露意向

### 响应时间线

| 阶段 | 目标时限 |
|------|---------|
| 确认收到 | 3 个工作日内 |
| 初步评估（严重性分级） | 7 个工作日内 |
| 修复或缓解方案 | 视严重性，30 天内 |
| 公开披露 | 修复发布后协调进行 |

我们会在 Release Notes 中致谢报告者，除非你要求匿名。

## 安全设计概览

下表是部署与二次开发时可以直接对照的机制清单。

| 机制 | 说明 | 位置 |
|------|------|------|
| API Key 认证 | 可选 `X-API-Key` 中间件，用 `secrets.compare_digest` 做常量时间比较 | `agentix/main.py` |
| CORS 白名单 | 按配置的域名白名单放行，默认不允许通配符 | `agentix/main.py` |
| 路径遍历防护 | 上传拦截 `..` / `/` / `\` / `\x00`，路径经安全解析 | `agentix/interfaces/http/v1/upload.py` |
| 文件大小限制 | 统一读 `settings.FILE_UPLOAD_MAX_MB`，分块写入防 OOM | `agentix/interfaces/http/v1/upload.py` |
| Cypher 只读闸门 | 执行前两道闸：修饰符白名单，加正则拦截 `CREATE/DELETE/MERGE/SET/REMOVE/DROP/LOAD/CALL` 等写子句 | `agentix/.../components/cypher_tools/utils.py` |
| 标识符白名单 | LLM 输出的节点标签与属性键插值进 Cypher 前，必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$` | `agentix/.../text2cypher/validation/validators.py` |
| 只读 SQL 词表 | 先在等长处抹掉字符串、注释与反引号标识符，再按词边界匹配危险词，避免 `SELECT 'it''s'` 误判；语法闸与执行闸共用这一份表 | `agentix/domain/policies/sql_read_only.py` |
| MySQL 导入护栏 | `table` 走标识符白名单（`^[A-Za-z0-9_]{1,64}$`），`where` 拒 `;` / `--` / `/*`，FastAPI body 校验自动 422 | `kb_ingest/kb_service/schemas/ingest.py` |
| 表达式资源护栏 | AST 节点白名单只挡语法不挡资源，另外限值域：表达式长度、`**` 指数模、整数结果位宽、浮点有限性 | `agentix/capabilities/mcp/tool_source.py` |
| Guardrails | 每个查询经范围校验，越界请求显式拒绝 | `agentix/application/agents/guardrails.py` |
| 错误信息收敛 | 对外不返回内部异常详情，仅 DEBUG 下附带 `detail` | `agentix/application/services/chat_service.py` |
| 可信根不可自改 | 生产环境缺 `SECRET_KEY` 直接启动失败，禁止运行时随机生成 | `agentix/config/settings.py` |
| XSS 防护 | 前端做 HTML 实体转义与安全 linkify | `web/src/components/AgentChatWidget.vue`、`web/src/modules/knowledge/utils/markdown.ts` |
| 弱哈希治理 | 缓存与向量库的键哈希显式标 `usedforsecurity=False`，bandit B324 清零 | `agentix/application/services/redis_cache.py` 等 3 处 |
| MCP 子进程环境白名单 | stdio 子进程只拿启动进程、找包、编码、证书、代理所需的键，不再继承 `LLM_API_KEY` / `SERPAPI_KEY` / `DATABASE_URL`；第三方 server 需要凭据时用 `MCP_SUBPROCESS_ENV_EXTRA_KEYS` 显式加名 | `agentix/capabilities/mcp/client.py` |
| 日志凭据脱敏 | httpx 异常文本会原样带出请求 URL，密钥常在 query string 里；对 query、header、`Bearer` 三类形态把值打成 `***` 并保留键名 | `agentix/tools/adapters/search.py` |
| 密钥只写不读 | 设置接口按 `*_api_key` / `*_secret` / `*_token` 后缀掩码，保留末 4 位供辨认；PUT 拒收掩码回写，未知键 422 | `agentix/retrieval/api/routers/settings.py` |
| SQL 日志出口独立 | `DB_ECHO` 三态开关。留空时沿用旧的 `bool(DEBUG)` 推断，而 `DEBUG` 出厂为 `True`，echo 会把完整 SQL 连参数经标准库 logging 打印，绕过 loguru 出口 | `agentix/infrastructure/core/database.py` |
| 生产身份显式声明 | `APP_ENV` 优先于 legacy `ENVIRONMENT`，两者皆空才回落 `DEBUG` 反推；未显式声明且闸门关闭时，启动打 `SECURITY POSTURE` 告警点名当前关掉的 flag | `agentix/retrieval/settings.py`、`agentix/main.py` |
| 跨服务连接串闸门 | `kb_ingest` 的 `connection_url` 直连 `create_engine()`，持 key 者能让服务端连任意 `host:port`（SSRF、读容器内 `sqlite:` 文件）。开启后校验「scheme 必须 mysql，主机在白名单内」，刻意不做 DNS 解析；另拒 query 里改写客户端行为的选项（`local_infile` 族、`read_default_file/group`、带 `CLIENT_LOCAL_FILES` 位的 `client_flag`）——主机白名单管连到谁，这些选项管对端能让本机上传哪个文件。拒绝时 detail 只说原因，不回放输入 | `kb_ingest/kb_service/core/db_url_guard.py` |
| 工具超时真截断 | 非协程 handler 下放 `asyncio.to_thread`，事件循环让出来之后 `asyncio.wait_for` 才取消得掉（默认关，逐工具确认 loop 亲和后再开） | `agentix/tools/gateway/executor.py` |
| handler 类型契约 | `TOOL_HANDLER_CONTRACT_ENABLED` 打开后，注册期按可等待口径（`handler_shape`：类的 `async __call__` 算 async，异步生成器单列）点名非协程 handler 并附声明超时。只告警不拒绝注册，因为仓里最重的几个工具本就是同步 I/O，fail-closed 会在开 flag 那刻打断全部功能 | `agentix/tools/gateway/executor.py` |
| 同名工具声明不静默丢弃（G14） | 同名再注册且 `ToolSpec` 不同时，executor 与 registry 两个入口默认打印 `TOOL DUPLICATE REGISTRATION:`，列出差异字段名并说明执行时读哪一份；`TOOL_DUPLICATE_REGISTRATION_STRICT=true` 才在改动 registry/handler 之前拒绝。默认语义仍是 executor 第一次赢、registry 最后一次赢，所以默认路径买到的是可见，不是不可能 | `agentix/tools/gateway/executor.py`、`agentix/tools/definitions.py`、`agentix/domain/tool/spec.py` |
| 执行位置可声明 | `ToolSpec.sandbox` 决定 handler 在哪儿跑，`EXECUTION_SANDBOX_ENABLED`（默认关）打开才读声明。校验只判档位在不在词汇表里，不判实现没实现：`container`、以及档位关着或本机落不下资源闸的 `subprocess`，都会在执行点拿到 `sandbox_unavailable` 且不可重试，不会静默退回进程内。默认两档 `in_process` / `thread` 同进程、同 GIL、同 `os.environ`、同文件权限，不构成隔离边界（[ADR-0007](docs/architecture/adr/0007-执行沙箱端口.md)） | `agentix/domain/tool/sandbox.py`、`agentix/tools/sandbox.py` |
| 子进程沙箱档 | 声明 `sandbox="subprocess"` 且 `TOOL_SANDBOX_SUBPROCESS_ENABLED=true`（默认关）时，一次调用一个进程：子进程先落资源闸 `RLIMIT_AS=512MiB` / `RLIMIT_CPU=30s` / `RLIMIT_FSIZE=32MiB`（可选 `TOOL_SANDBOX_SUBPROCESS_UID` 降权，默认 `-1` 不降），再 import 业务代码；环境走与 MCP 同一份白名单；协议 v2 要求成功信封回显本次调用的 nonce，退出码非 0 时 stdout 上的信封一律不采信，`ok` 必须是严格布尔；取消经 `start_new_session` 加 `os.killpg` 杀掉整个进程组。只有 POSIX 落得下。进程数闸 `RLIMIT_NPROC`（上限 32 个任务）已实现，但只在配了独立且非 0 的 uid 时才落。不含文件权限收紧与出网限制 | `agentix/tools/sandbox.py::SubprocessSandbox`、`agentix/tools/subprocess_worker.py`、`agentix/domain/tool/exec_env.py` |
| 运行时写代码执行 | `CODE_EXEC_ENABLED=true`（默认关）时，模型可经 `code_execute` 工具在 subprocess 档里执行代码。隔离到的是独立进程与地址空间、三道资源闸、环境白名单、可杀进程组；没隔离到的是文件系统与网络。非 POSIX 平台 fail-closed 拒绝，永不做无隔离降级 | [ADR-0007](docs/architecture/adr/0007-执行沙箱端口.md)、`agentix/tools/sandbox.py` |

沙箱档的实测边界、nonce 归属与进程数闸的前提条件，见[已知限制](docs/已知限制.md)的「执行与沙箱」一节。

## 部署安全建议

- 生产环境必须显式配置 `SECRET_KEY` 与 `API_KEY`，并置 `REQUIRE_API_KEY=true`。
- 必须显式声明 `APP_ENV=production`。生产身份原先是靠 `DEBUG` 反推的，忘设 `DEBUG=False` 会让配置校验的硬失败分支整段静默不生效，该拒绝启动的错配置会照常对外服务。硬失败面判的是 `STRICT_VALIDATION`（`prod` / `production` / `staging`）；预发的价值正是用生产的失败语义提前跑一遍，把它降级成观察环境等于把这道校验从发布链路上摘掉。
- 默认关闭的闸门在打开之前不得对外：`REQUIRE_API_KEY` / `V1_REQUIRE_JWT` / `RATE_LIMIT_ENABLED` / `HUMAN_REVIEW_ENABLED` / `TOOL_GUARDRAILS_ENABLED` / `TOOL_SYNC_OFFLOAD_ENABLED` / `MCP_SUBPROCESS_ENV_SCRUB`。启动日志的 `SECURITY POSTURE` 行会点名当前关闭的那些。
- 生产显式设 `DB_ECHO=false`、`UVICORN_RELOAD=false`（两者留空时沿用 `DEBUG` 推断的旧行为）。
- 在预发开一次 `TOOL_HANDLER_CONTRACT_ENABLED=true`，把启动日志里 `TOOL HANDLER CONTRACT:` 点名的工具清单抄进部署记录。这些工具的 `timeout_ms` 在 `TOOL_SYNC_OFFLOAD_ENABLED=false` 时不会截断，「哪个工具能占住这个 worker 多久」必须是已知量。
- `TOOL_DUPLICATE_REGISTRATION_STRICT` 默认 `false`：预发先收集 `TOOL DUPLICATE REGISTRATION:` 点名的清单，或确认每条都是无害重绑，再由运维显式打开；打开后同名且声明不同的注册会让启动失败。预发 override 刻意钉 `false`，避免宿主机 `.env` 决定预发是否 fail-closed。
- `EXECUTION_SANDBOX_ENABLED` 在预发置 `true` 不是为了买隔离。默认档 `in_process` 与关着行为一致，它也不进 `SECURITY POSTURE` 的点名清单（那条清单只列关掉会让安全属性变弱的闸门，这两位开关不影响边界，守卫是 `tests/test_p125_subprocess_sandbox.py::test_the_two_sandbox_keys_are_deliberately_not_posture_flags`）。它的价值是让这条新缝被持续走到。预发没有把 `TOOL_SANDBOX_SUBPROCESS_ENABLED` 钉进 override（继承基线 `False`），所以声明 `subprocess` 的工具在预发拿到的是「档位 switched off」的终局拒绝；要在预发真走墙，得显式开档位并确认该机是 POSIX，步骤见 [subprocess 沙箱取证](docs/runbooks/子进程沙箱取证.md)。
- 打开 `MCP_SUBPROCESS_ENV_SCRUB` 后，第三方 MCP server 需要凭据时只加它需要的那几个键名，不要退回继承整个环境。
- 跑 `kb_ingest` 时一并设置 `KB_REQUIRE_API_KEY=true` 与 `KB_API_KEY`（入站闸门默认关；开启却不配 key 是 fail-closed 的全部 401，那是锁死不是安全），并按实际数据源填 `KB_MYSQL_URL_GUARD=true` 与 `KB_MYSQL_ALLOWED_HOSTS=db.internal,*.intranet.example`；白名单留空时闸门只校验 scheme。
- `KB_MYSQL_WHERE_PARAMETRIZED=true` 把导入过滤从拼接改成结构化加绑定，但它是硬切：开当天仍在传 `where` / `--where` 的导入作业会全部 400。顺序是「调用方先迁到 `filters`（CLI 用 `--filters` 传 JSON），确认没有存量作业还在传片段，再开」，不要在生产直接翻开。
- 不要将 `.env` 提交到版本库（已在 `.gitignore` 中排除）。生产环境通过 KMS / Vault / 环境变量注入下发密钥。
- 数据库（Neo4j / PostgreSQL / MySQL / Redis / Milvus）不要暴露公网，放内网或私有网络。反向代理层启用 TLS，并限制请求体大小与速率。
- 容器侧 backend 与 retrieval-worker 已带 `security_opt: no-new-privileges:true` 与 `deploy.resources.limits`（CPU / 内存 / pids，可用 `BACKEND_CPUS`、`BACKEND_MEMORY`、`BACKEND_PIDS_LIMIT` 调整），另有运行身份旋钮 `AGENTIX_CONTAINER_USER`（默认 `0:0`，仍以 root 运行）。
  降权不是改一个值：非 root 写不进 `./data` / `./uploads` 时，entrypoint 的 `set -euo pipefail` 会表现成重启崩溃循环，而不是拒绝启动，所以必须先在宿主机准备目录属主，步骤见[预发环境与配置矩阵](docs/runbooks/预发环境与配置矩阵.md)的「容器降权」一节。降权买到的是「写宿主挂载要过属主检查，setuid 位失效」，买不到 `cap_drop`、只读根文件系统与 user namespace 映射。
- 定期运行 `pip-audit` 复核依赖漏洞。仓库开启 Dependabot 安全公告与安全更新，版本升级按需手动执行（`open-pull-requests-limit: 0`）。

## 依赖安全现状

- 以 `pip-audit -r requirements.txt` 实测为准。2026-10-04 那轮的历史读数是残余 21 项 / 8 包，全部属 langchain 0.3.x 生态（langgraph-checkpoint、langgraph、langchain-core、langchain、langchain-openai、langchain-text-splitters、langgraph-sdk、langsmith），清零需大版本迁移。
- LG-0-2（2026-10-27）已把 langchain 家族迁到 1.x（`langgraph 1.2.18` / `langchain-core 1.7.0` / `langsmith 0.14.13` 等，见 `requirements.txt`），上面 8 包全部换了版本线。因此那 21 项既不能算已清零，也不能当现状，必须在 1.x 栈上重跑 `pip-audit` 才能出结论。当时环境未装 `pip-audit`，未复测，所以不写新数字。
- 核心 Web 栈（`fastapi==0.142.2` / `starlette==1.7.0` 等）的漏洞在 2026-10-04 那轮已清零，本次未动这两个 pin。
- `bandit`（medium 及以上）当前 0 findings。LOW 级信息性项（try/except pass 等）保留并逐项复核。
- 升级依赖时 `requirements.txt` 与 `pyproject.toml` 的双声明必须同步（`tests/test_dependency_consistency.py` 守卫），升级后以全量测试验证，基线见 README §6。

## 已知边界（非缺陷）

- `Guardrails` 是概率性范围判定（LLM 驱动），不能替代确定性的访问控制，不要当作唯一的安全边界。
- 本地开发默认使用 LM Studio；切换到云端模型时，请自行评估数据出境与合规要求。
- **有一面墙，但默认不开、平台不对称，且只挡声明过的维度**：`ToolSpec.sandbox` 声明执行位置，`subprocess` 档一次调用一个进程，先落 `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_FSIZE` 再 import 业务代码，环境按白名单裁剪，取消能杀掉进程组。Linux 容器实测：父进程里的 `DATABASE_URL` / `LLM_API_KEY` 不过界。能力探测不过（Windows 没有 `resource` 与进程组 kill）就在执行点拒绝，绝不静默退回进程内。它没挡住文件权限（只读根、指定可写目录）与出网（网络默认拒绝），这两件属 ⑰/T3；`unshare` / namespace / seccomp 未接线，容器默认仍以 root 运行。所以口径是「是墙，不是笼子」：handler 崩了、吃光内存、读环境密钥都出不了进程，但 `open()` 一个 world-readable 文件或往外连 socket，这一档不管。若要引入「执行用户提供代码」的工具，⑮ 是必要条件而非充分条件，须先补 ⑰，路线见 [ADR-0007](docs/architecture/adr/0007-执行沙箱端口.md) 的未收口清单，取证步骤见 [subprocess 沙箱取证](docs/runbooks/子进程沙箱取证.md)。
- **密钥掩码不等于授权**：设置接口对 `*_api_key` 等字段只写不读，但「谁能读写哪个 kb」由 `SETTINGS_AUTHZ_ENABLED` 决定且默认关。关着时任一已登录用户仍可读改任意 `kb_id` 的配置。残余风险不是读到密钥（掩码已挡），而是可写字段本身就是一条出站通道：把 `llm_base_url` / `embedding_base_url` 改指外部端点不需要任何密钥，却足以让该租户的提问内容整批外送。打开后读要求 kb 属于调用者租户，写再要求 admin；两个前置（`tenant_id` 回填到无 NULL、该租户内确有 admin，全仓没有自助升 admin 的接口）与切换步骤见[预发环境与配置矩阵](docs/runbooks/预发环境与配置矩阵.md)的「设置接口授权收紧」一节。
- **`kb_ingest` 的连接串闸门默认关**：`KB_MYSQL_URL_GUARD=false` 时 `connection_url` 仍是未校验的用户输入（SSRF、内网探测、凭据外带面原样存在）。开启后只校验「scheme 必须 mysql，主机在 `KB_MYSQL_ALLOWED_HOSTS` 内」，不做 DNS 解析，因此拦的是配置意图而不是 IP 归属；白名单留空时等于只查 scheme。无论开关状态，该服务都请只在受信网络内可达。
- **`kb_ingest` 的过滤参数化闸门默认关**：`KB_MYSQL_WHERE_PARAMETRIZED=false` 时，导入过滤仍走黑名单加字符串拼接，拦得住 `;` 堆叠语句与注释，拦不住 `1=1 OR 1=1`、`id IN (SELECT … FROM mysql.user)` 这类合法但越界的片段。对拼出来的 SQL 而言，调用方给的就是语法。开启后原始片段一律拒，过滤只能走结构化 `filters`（列名过标识符白名单，运算符取封闭枚举，值全部 bindparams）。这一位是破坏性变更，迁移顺序写在两个 `.env.example` 里，口径见[运行时护栏 §15.9](docs/architecture/运行时护栏.md)。
- **审批与恢复状态的持久性要靠显式开关**：`CHECKPOINTER_FAIL_CLOSED` 默认关。Redis 路径失败（包未安装、连接或 `asetup()` 失败）时服务不会拒绝启动，而是静默降级为进程内存态的 `MemorySaver`（连接失败记 ERROR，缺包记 WARNING，两者另有 `_record_degraded("checkpointer")` 指标），重启即失，会话与 HITL 审批态因此不持久。「审批可追溯、可恢复」属于需要显式开 `CHECKPOINTER_FAIL_CLOSED=true` 才成立的保证。显式 `CHECKPOINTER_TYPE=memory` 不属故障路径，开关为真也不抛。
- **运行态落库面（T21 `agent_run_states`）落的是业务内容，且没有保留期**：`RUN_STATE_PERSIST_ENABLED=true`（默认关）时 `agent_run_states.meta` 存运行登记面的 JSON 快照，可能含任务预览（`task_preview`，任务前 200 字符）、结果（回答正文与引用来源）、会话与线程标识（`session_id` / `thread_id`）。这些是业务内容而非凭据（不落 LLM key、不落 `DATABASE_URL`），但按数据分级应与会话内容同等对待。该表无 TTL、无自动清理，长期开启需自建归档策略；停用时 `DROP TABLE agent_run_states` 即清除。从未开过 flag 的库不会建出这张表；开过 flag 的库关掉 flag 只是不再读写，表不会自己消失。
- **路由结果缓存（`ROUTING_CACHE_ENABLED`，默认关）只存摘要**：进程内缓存只存问句的 sha256 加四个字段（`type` 为封闭枚举，`logic` / `decision` 为模型生成的短文本，`confidence` 为数值），TTL 300 秒，不存问答原文，不存 `reasoning`；键只含归一化问句的 sha256，命中后原样回填本次请求原文。每个进程各持一份、不落盘、重启即清空，不构成跨副本的数据面。
