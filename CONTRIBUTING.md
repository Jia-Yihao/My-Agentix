# 贡献指南 (Contributing Guide)

感谢你愿意为 **My Agentix** 做出贡献！本文档说明本项目的开发流程、代码规范与提交约定，帮助你快速融入协作。

> 阅读顺序建议：本文件 → [README](README.md)（项目总览与快速开始）→ [`docs/architecture/README.md`](docs/architecture/README.md)（架构总览）。

---

## 目录

- [行为准则](#行为准则)
- [我能贡献什么](#我能贡献什么)
- [开发环境搭建](#开发环境搭建)
- [项目结构速览](#项目结构速览)
- [架构约束（重要）](#架构约束重要)
- [代码规范](#代码规范)
- [提交规范](#提交规范)
- [测试要求](#测试要求)
- [Pull Request 流程](#pull-request-流程)
- [分支与版本策略](#分支与版本策略)
- [新增能力指南](#新增能力指南)
- [获取帮助](#获取帮助)

---

## 行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。请在所有交互中保持专业与尊重。

---

## 我能贡献什么

| 类型 | 说明 | 入口 |
|------|------|------|
| 🐛 Bug 修复 | 修复缺陷，需附复现步骤与回归测试 | [Bug 报告模板](.github/ISSUE_TEMPLATE/bug_report.yml) |
| ✨ 新功能 | 新增工具 / Agent / Provider / 领域适配 | [功能请求模板](.github/ISSUE_TEMPLATE/feature_request.yml) |
| 🧩 新领域适配 | 把系统迁移到新垂直领域（中医药典、政务服务等） | 先开 Issue 讨论领域适配方案 |
| 📖 文档 | 修正、补充、翻译文档 | 直接提 PR |
| ⚡ 性能 | 延迟、吞吐、成本优化 | 需附前后对比数据 |
| 🔒 安全 | 漏洞报告请走 [SECURITY.md](SECURITY.md)，**不要**开公开 Issue |

---

## 开发环境搭建

### 前置要求

- **Python 3.12.13**（项目支持范围见 `pyproject.toml`；本地开发统一使用此版本）
- **Node.js 18+**（前端）
- **Docker & Docker Compose**（基础设施：Redis / Neo4j / PostgreSQL / Milvus / MySQL）

### 快速开始

环境搭建、依赖安装与启动步骤统一见 [README 的「快速开始」](README.md#4-快速开始)，此处不重复。密钥类配置项以 `.env.example` 为准（`API_KEY` / `SECRET_KEY` / `LLM_API_KEY` / `EMBEDDING_API_KEY`）。

### 安装 Git Hook（推荐）

```bash
pre-commit install
pre-commit run --all-files   # 首次全量检查
```

---

## 项目结构速览

完整目录树见 [README 的「项目结构」](README.md#10-项目结构)。改动前先确认要落的位置：`domain/` 只放纯契约（零第三方依赖），业务规则进 `application/`，实现 Domain 端口才进 `infrastructure/`。

各层职责与四条强制依赖规则由 AST 守卫在 CI 中强制（`import-integrity` 作业），跨层引用会被直接拦下。

---

## 架构约束（重要）

本项目采用**分层架构 + 依赖倒置**。提交前请确认你的改动不违反以下规则（CI 中有自动化守卫）：

1. `interfaces` 只做协议转换与鉴权，**不得**包含业务编排或 ORM 操作。
2. `application` 依赖 `domain.ports` 抽象，**不得**直接 import `infrastructure` 具体实现或 `interfaces`。
3. `domain` **不得**依赖任何 `infrastructure` / `interfaces` / `application` 符号。
4. `infrastructure` 实现 `domain.ports` 中定义的 Protocol，**不得**反向依赖 `interfaces`。

这些规则由 `tests/test_p17_domain_layer.py` 与 `tests/test_p22_obs_scope.py` 中的静态扫描守卫强制执行。本地运行：

```bash
pytest tests/test_p17_domain_layer.py tests/test_p22_obs_scope.py -q
```

---

## 工程约定（重要）

除分层规则外，本仓库还有八条约定。它们不是风格偏好——每一条都对应一类已经踩过的
故障，改动时请按「约定 N」的说法在评审里对齐。下文与各设计文档里出现的「约定 N」
均指本节。

1. **新开关默认关**：新增 Settings 字段一律以 `False` / `0` 起步，flag-off 路径与
   改动前**逐位等价**。行为变更走「新 flag + 默认关 + 显式启用」，不直接改默认行为。
2. **辅助子系统 fail-open**：指标、记忆、指令、预算这类旁路出错**不得影响主链路**；
   捕获后降级（空串 / 跳过 / 回基线）并 `logger.warning` 留痕。
3. **指标标签有界**：新指标的 label 名先登记进
   `tests/test_p100_genai_observability.py::ALLOWED_METRIC_LABELS`；用户、会话、trace、
   问答内容等高基数维度只进 span，不进指标。
4. **配置键五处同步**：新增 Settings 字段需同步五处——
   `agentix/config/settings.py`、`agentix/config/selfcheck.py` 的 `DEFAULT_IGNORED_MISSING`、
   `.env.example`、`docs/architecture/配置管理.md` 的键表，以及
   `tests/test_p41_doc_consistency.py::TestDocumentedConfigKeys` 的手写清单。
5. **改基线先确认**：任何对数据、脚本、测试基线的改动需先与维护者确认。
6. **执行边界四条**：①「先解析再执行」的入口，语法闸与资源闸必须各自独立存在——
   AST 白名单挡得住 `__import__`，挡不住 `9**9**9` 把内存吃光；白名单也只覆盖它声明过
   的那个维度（连接串的 scheme 白名单不覆盖目的地，目的地白名单也不覆盖
   `?local_infile=1` 这类让对端读走本机文件的客户端能力位）。② `asyncio.wait_for` 只能
   截断已经让出事件循环的调用，非协程 handler 不下放线程时 `timeout_ms` 形同虚设。
   ③ 承载凭据的字段不要过会回显调用方输入的 validator——pydantic 会把被拒输入放进
   `errors()[].input`，FastAPI 默认把它写进 422 响应体，校验器就变成了凭据回显器；
   校验点放服务层，错误 detail 只说为什么被拒。④ 诊断口径必须与执行链的判定同源，
   且只告警不设卡。
7. **核对基准必须是生成物**：`docs/runbooks/预发环境与配置矩阵.md` 的配置矩阵由
   `scripts/gen_staging_matrix.py` 从 `docker-compose.yml` 与
   `docker/compose.override.staging.yml` 推导。改 compose / override 或改这张表，
   都必须 `python scripts/gen_staging_matrix.py --check` 退出 0。不要用
   `docker compose config` 的渲染结果当基准——它会展开宿主机 `.env`（密钥进日志），
   而渲染值本身已被 `.env` 污染。
8. **文档分层**：`README.md` 是唯一的现行总览，`docs/runbooks/` 是操作动作。
   新增文档不复述 README 已声明的事实（架构分层、默认开关、目录地图、测试基线、
   已知限制），只写指针；要补结构信息就改 README，要补操作就写 runbook。

---

## 代码规范

### Python

- **格式化**：[black](https://black.readthedocs.io/)，行宽 100（`pyproject.toml` 已配置）
- **Lint**：[ruff](https://docs.astral.sh/ruff/)（替代 flake8/isort）+ `black --check`
- **类型**：mypy（渐进式，新增公共接口请补类型标注）
- **导入**：使用绝对导入 `from agentix.xxx import yyy`；禁止跨层反向导入
- **日志**：统一使用 `agentix.infrastructure.core.logger.get_logger(service="...")`，禁止 `print()`

```bash
# 一键检查与修复
ruff check agentix --fix
black agentix
mypy agentix
```

### 前端 (Vue 3 + TypeScript)

- 使用 `<script setup lang="ts">`
- 提交前运行 `cd web && npm run lint`（`vue-tsc --noEmit`）

### 命名与文档

- 模块/函数级 docstring 使用中文，说明**为什么**（设计依据）而非仅描述做什么
- 公共 API 变更必须同步更新 `docs/`
- 涉及架构决策的改动，请补充 `docs/architecture/` 下的说明或 ADR

---

## 提交规范

采用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**type**：`feat` | `fix` | `docs` | `style` | `refactor` | `perf` | `test` | `build` | `ci` | `chore` | `revert`

**scope**（可选）：`agents` | `kb` | `kg` | `llm` | `domain` | `infra` | `api` | `web` | `evals` | `docs`

示例：

```
feat(tools): 新增 web_search 工具并注册到 ToolRegistry

- 在 registry_init 声明式清单中登记 spec（含"何时不该用"反例）
- 补充 tests/test_p109_tool_handler_contract.py

Closes #42
```

> **一次提交只做一件事**。大改动请拆分为可独立验证的小步提交。

---

## 测试要求

- 所有新功能必须带单元测试，且**默认离线可跑**（不依赖 Docker / LLM 服务）
- 依赖外部服务的测试请标记 `@pytest.mark.integration`，CI 默认跳过
- Bug 修复必须包含一个能复现原缺陷的回归测试

```bash
# 全量（离线，先清空 PYTHONPATH，否则沙箱相关用例假失败）
env -u PYTHONPATH python -m pytest -m "not integration" -q

# 单文件 / 单用例
python -m pytest tests/test_p41_doc_consistency.py -q

# 覆盖率（本地自查用）
python -m pytest --cov=agentix --cov-report=term-missing
```

**合并门槛**：CI 的四个 job 全绿 + 至少 1 位维护者 Review；核心链路（`application/`）的新增代码要有测试覆盖。CI 不跑 pytest，测试由提交者在本地跑通后再提交。

---

## Pull Request 流程

1. Fork 仓库并从 `main` 创建分支：`git checkout -b feat/my-feature`
2. 完成开发，确保 `ruff` / `black` / `mypy` / `pytest` 全部通过
3. 提交 PR，按 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 填写描述
4. 等待 CI 通过并至少 1 位维护者 Review
5. Review 通过后由维护者 squash-merge 到 `main`

> 首次贡献者请在 PR 中说明你已阅读并同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

---

## 分支与版本策略

- `main`：始终可发布，受保护，禁止直接 push
- `feat/*` / `fix/*` / `docs/*`：功能分支，生命周期短
- `release/x.y`：发布维护分支（仅在需要 backport 时创建）

版本号遵循 [语义化版本 SemVer 2.0.0](https://semver.org/lang/zh-CN/)。

---

## 新增能力指南

| 我想… | 参考 |
|-------|------|
| 新增一个工具（Tool） | 参照 `agentix/domain/tool/spec.py` 与 `agentix/tools/` 现有工具实现 |
| 新增一个 LLM / Embedding / Reranker Provider | 参照 `agentix/infrastructure/registry.py` 的注册表模式 |
| 新增一个 Agent 子图 / 节点 | 参照 `agentix/orchestration/` 的单层主图与节点注册 |
| 迁移到新垂直领域 | 先开 Issue 讨论领域适配方案 |
| 新增配置项 | [`docs/architecture/配置管理.md`](docs/architecture/配置管理.md) |

---

## 获取帮助

- 💬 提问与缺陷：仓库目前为私有仓库；有访问权限的协作者可使用 GitHub Issues。公开开放贡献前，请先联系维护者获取仓库访问权限
- 🔒 安全：见 [SECURITY.md](SECURITY.md)

再次感谢你的贡献！🎉
