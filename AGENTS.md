# AGENTS.md — My-Agentix 协作约定

> 本文件同时是 `agentix/agent/context/instructions.py` 指令级联的项目层来源
> （`LG_INSTRUCTIONS_ENABLED=true` 时注入 L2 general-query system 前缀）。
> 修改本文件 = 修改生产前缀，请保持本文件**稳定**：约定性的、低频变化的
> 内容放这里；随任务变化的内容放会话/任务描述里。

## 常用命令

```bash
pytest tests -q                                  # 全量回归（约 7 分钟）
pytest -m "not integration" -q --tb=short        # CI 口径（与全量等价）
ruff check agentix tests                         # lint（CI 同一把尺子）
ruff format agentix tests                        # 格式化
mypy agentix/domain                              # 类型检查（domain 簇 0 错门禁）
cd web && npm run lint && npm run test           # 前端 vue-tsc + vitest
```

## 硬性纪律

1. **新开关默认关**：新增 Settings flag 一律 `False`/`0` 起步；flag-off 路径必须与
   基线**逐位等价**（Strangler 惯例）。行为变更走「新 flag + 默认关 + 显式启用」。
2. **fail-open 边界**：指标/记忆/指令/预算等辅助子系统出错**绝不影响主链路**，
   捕获后降级（空串 / 跳过 / 回基线），并 `logger.warning` 留痕。
3. **指标标签有界**：新指标的 label 名必须先进
   `tests/test_p100_genai_observability.py` 的 `ALLOWED_METRIC_LABELS` 白名单；
   禁止 user/session/trace/问答内容等高基数标签进指标（只进 span）。
4. **配置键五处同步**：新增 Settings 字段必须同步——
   ① `agentix/config/settings.py` ② `agentix/config/selfcheck.py`
   （`DEFAULT_IGNORED_MISSING`）③ `.env.example` ④
   `docs/architecture/配置管理.md` §5 表 ⑤
   `tests/test_p41_doc_consistency.py::test_new_settings_are_documented` 手写清单。
5. **改动前先问**：任何对数据、脚本、测试基线的改动需先征得维护者同意。
6. **执行边界四条口径**（§24.3 T0/T1 实测得出，非倡议）：
   ① 任何「先解析再执行」的入口，**语法闸与资源闸必须各自独立存在**——AST 节点
   白名单挡得住 `__import__`，挡不住 `9**9**9` 把内存吃光；同理**白名单只覆盖它
   声明过的那个维度**：连接串的 scheme 白名单不覆盖目的地，目的地白名单也不覆盖
   `?local_infile=1` 这种「让对端读走本机文件」的客户端能力位；
   ② `asyncio.wait_for` 只能截断**已经把事件循环让出来**的调用，非协程 handler
   不下放线程则 `timeout_ms` 形同虚设；
   ③ **承载凭据的字段不要过会回显给调用方的 validator**——pydantic 把被拒输入放进
   `errors()[].input`，FastAPI 又默认把 `input` 写进 422 响应体，于是校验器自己变成
   凭据回显器，而且只在错误路径上回显（比不校验更难发现）。校验点放在服务层，
   错误 detail 自己构造：只说为什么被拒，不回放输入；
   ④ **诊断口径必须与执行链的判定同源，且只告警不设卡**——`handler_shape` 判的是
   「调用后能否拿到可 await 的结果」，不是 `iscoroutinefunction` 的字面返回值（类的
   `async __call__` 对后者为 False，照字面判则每个类形式的工具都吃假告警、告警与实际
   行为各说一套）；契约类检查不许拒绝注册：仓里最重的几个工具本就是同步 I/O 注册进来的，
   fail-closed 等于在开 flag 那一刻打断全部功能。
   口径见 `docs/architecture/运行时护栏.md` §15、逐项实测见 `docs/项目实现手册.md` §24。
7. **被运维当核对基准的表必须是生成物，不是抄本**：`docs/runbooks/预发环境与配置矩阵.md`
   的「配置矩阵」由 `scripts/gen_staging_matrix.py` 从 `docker-compose.yml` +
   `docker/compose.override.staging.yml` 推导（dev 列回落 `Settings` 的**字段默认值**，
   刻意不实例化 `Settings`——实例化会吃宿主机 `.env`，那正是这张表要避开的污染源）。
   改 compose / override 的 backend `environment:`，或改这张表，都必须跑
   `python scripts/gen_staging_matrix.py --check` 且退出 0；想让一个新键进表，只有两条路：
   让 override 也管它，或在脚本的 `MANUAL_KEYS` 里署名登记理由。
   **不要用 `docker compose config` 的渲染结果当基准**：它把宿主机 `.env` 整个展开打印
   （密钥因此进终端与 CI 日志），而渲染值本身就被 `.env` 污染——用它校验文档等于让被校验的
   对象决定校验基准。`docs/项目实现手册.md` §24.2 G13 记录的两次案发（矩阵写着 `true`、
   override 里压根没设；`${K:-${DEBUG:-True}}` 让工作站 `.env` 决定预发是否 `--reload`）
   都是"人抄的表"这一种形态的产物。
8. **文档分层不可回退**（2026-10-27 起）：**`README.md` 是唯一「现行总览」**，
   `docs/项目实现手册.md` 是「实现细节手册」，`docs/archive/**` 是**冻结快照**
   （只用于追溯历史，不再维护、不随代码更新）。新增或修改规划/优化文档必须满足四条：
   ① 顶部有一行「文档状态」，取值只能是 `现行` / `进行中` / `已完成` / `历史`，并指向现行依据；
   ② **不复述** README 已声明的事实（架构分层、默认开关、目录地图、测试基线、已知限制）——只写指针；
   ③ 计划交付后把状态改成「已完成」，**禁止**长期停在「进行中」冒充待办；
   ④ **不要新增第四份「总览」**：要补结构信息就改 `README.md` §3/§12，要补实现细节就改
   `docs/项目实现手册.md`，要补操作动作就写 `docs/runbooks/`。
   判据来源：README / PROJECT_SPEC / 原 `docs/architecture/overview.md` 三份曾并存，同一套架构讲三遍、
   改一次要同步三处（正是纪律 7 说的"人抄的表"）；项目全景-归档快照.md 的独有内容已并入 README，
   原文归档为 `docs/archive/项目全景-归档快照.md`。守卫：`tests/test_p41_doc_consistency.py` 校验文档内
   路径存在性，**不校验**上述四条——它们靠这条纪律与 review 维持。

## 代码风格

- 注释/文档字符串用简体中文，说明「为什么」而非「是什么」。
- 宽 except 必须带 `# noqa: BLE001 - <理由>` 注释，且理由说明降级去向。
- 异步函数用 `async/await`；CPU/IO 阻塞调用经 `asyncio.to_thread` 下放。
- 测试文件命名 `tests/test_p<NN>_<主题>.py`，模块 docstring 写清覆盖面与验收口径。

## 文档同步面（代码行为变更时的强制清单）

`CHANGELOG.md`（[Unreleased]）· `README.md`（§3 架构 / §7 可靠性 / §11 测试真源数 / §16 已知限制）·
`SECURITY.md`（安全设计概览 / 部署安全建议 / 已知边界）·
`docs/项目实现手册.md` · `docs/architecture/配置管理.md` ·
`docs/architecture/运行时护栏.md` · `docs/源码研读笔记.md` ·
`.env.example` · `agentix/config/selfcheck.py` · 本文件。

> code exec 机制（运行时写代码执行任务，业务总闸 `CODE_EXEC_ENABLED` 默认关）的口径见 `.qoder/rules/code-exec.md`。
> 任务排序、状态判定与提交边界的口径见 `.qoder/rules/task-order.md`；排序真源是它指向的
> `docs/design/审批与中断恢复优化规划.md` §8.3。
