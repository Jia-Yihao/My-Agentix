# 贡献指南 (Contributing Guide)

感谢你愿意为 **My Agentix** 做出贡献！本文档说明本项目的开发流程、代码规范与提交约定，帮助你快速融入协作。

> 阅读顺序建议：本文件 → [`docs/architecture/README.md`](docs/architecture/README.md)（架构总览）→ [`docs/architecture/扩展机制.md`](docs/architecture/扩展机制.md)（扩展机制）→ [`docs/版本管理策略.md`](docs/版本管理策略.md)（版本策略）。

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
| 🧩 新领域适配 | 把系统迁移到新垂直领域（中医药典、政务服务等） | [`docs/architecture/扩展机制.md`](docs/architecture/扩展机制.md) |
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

```bash
# 1. 克隆仓库（当前未发布到公共远端，发布后替换 URL；目录名与下方 cd 保持一致）
git clone <远端仓库 URL> My-Agentix
cd My-Agentix

# 2. 创建虚拟环境（Python 3.12）
py -3.12 -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

# 3. 安装依赖（运行时 + 测试/工具链）
pip install -r requirements-dev.txt
pip install -e ".[dev,test]"

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env，至少配置：OPENAI_API_KEY / SECRET_KEY / API_KEY

# 5. 启动基础设施
docker compose up -d

# 6. 启动后端
python -m uvicorn agentix.main:application --reload --port 8000

# 7. 启动前端（另开终端）
cd web && npm install && npm run dev
```

### 安装 Git Hook（推荐）

```bash
pre-commit install
pre-commit run --all-files   # 首次全量检查
```

---

## 项目结构速览

```
agentix/
├── config/          # 集中配置（Pydantic Settings，唯一事实来源）
├── domain/          # 领域层：错误词汇表、策略、端口抽象（不依赖任何上层）
├── application/     # 应用层：Agent 编排、业务服务
│   ├── agents/      #   LangGraph 图与节点
│   └── services/    #   会话/记忆/观测/任务队列
├── infrastructure/  # 基础设施：LLM、向量库、图库、持久化、工具注册表
└── interfaces/      # 接口层：HTTP 路由、请求/响应模型
```

完整的目录职责与依赖规则见 [`docs/architecture/分层架构与依赖规则.md`](docs/architecture/分层架构与依赖规则.md)。

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
- 公共 API 变更必须同步更新 `docs/` 与 `CHANGELOG.md`
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
- 补充 tests/test_tool_registry_web_search.py

Closes #42
```

> **一次提交只做一件事**。大改动请拆分为可独立验证的小步提交。

---

## 测试要求

- 所有新功能必须带单元测试，且**默认离线可跑**（不依赖 Docker / LLM 服务）
- 依赖外部服务的测试请标记 `@pytest.mark.integration`，CI 默认跳过
- Bug 修复必须包含一个能复现原缺陷的回归测试

```bash
# 全量（离线）
pytest -q

# 仅单元测试（CI 口径）
pytest -m "not integration" -q

# 覆盖率
pytest --cov=agentix --cov-report=term-missing
```

**合并门槛**：CI 全绿 + 覆盖率不低于基线 + 核心链路（`application/`）新增代码有测试覆盖。

---

## Pull Request 流程

1. Fork 仓库并从 `main` 创建分支：`git checkout -b feat/my-feature`
2. 完成开发，确保 `ruff` / `black` / `mypy` / `pytest` 全部通过
3. 更新 `CHANGELOG.md` 的 `[Unreleased]` 段
4. 提交 PR，按 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 填写描述
5. 等待 CI 通过并至少 1 位维护者 Review
6. Review 通过后由维护者 squash-merge 到 `main`

> 首次贡献者请在 PR 中说明你已阅读并同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

---

## 分支与版本策略

- `main`：始终可发布，受保护，禁止直接 push
- `feat/*` / `fix/*` / `docs/*`：功能分支，生命周期短
- `release/x.y`：发布维护分支（仅在需要 backport 时创建）

版本号遵循 [语义化版本 SemVer 2.0.0](https://semver.org/lang/zh-CN/)，详见 [`docs/版本管理策略.md`](docs/版本管理策略.md)。

---

## 新增能力指南

| 我想… | 参考 |
|-------|------|
| 新增一个工具（Tool） | [`docs/architecture/扩展机制.md#新增工具`](docs/architecture/扩展机制.md) |
| 新增一个 LLM / Embedding / Reranker Provider | [`docs/architecture/扩展机制.md#新增-provider`](docs/architecture/扩展机制.md) |
| 新增一个 Agent 子图 / 节点 | [`docs/architecture/扩展机制.md#新增-agent-子图`](docs/architecture/扩展机制.md) |
| 迁移到新垂直领域 | [`docs/architecture/扩展机制.md#新领域适配`](docs/architecture/扩展机制.md) |
| 新增配置项 | [`docs/architecture/配置管理.md`](docs/architecture/配置管理.md) |

---

## 获取帮助

- 💬 提问与缺陷：本仓库当前未发布到公共远端，暂无 Discussions / Issue 入口；发布后在此回填，日常沟通请先联系维护者
- 🔒 安全：见 [SECURITY.md](SECURITY.md)

再次感谢你的贡献！🎉
