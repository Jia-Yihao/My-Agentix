# 版本管理策略 (Versioning Strategy)

本文档定义 My Agentix 的版本号规则、发布流程、分支模型与兼容性承诺。

---

## 1. 版本号规则（SemVer 2.0.0）

版本号格式：`MAJOR.MINOR.PATCH`（可带预发布标识与构建元数据）。

```
0.2.0
│ │ │
│ │ └── PATCH：向后兼容的缺陷修复
│ └──── MINOR：向后兼容的功能新增
└────── MAJOR：不兼容的 API / 行为变更
```

预发布标识示例：`0.2.0-rc.1`、`1.0.0-beta.2`。

### 在 `0.y.z` 阶段的特殊约定

由于本项目当前处于 `0.y.z` 开发线（尚未发布 1.0），遵循 SemVer 的「0 阶段」惯例：

- **MINOR（`0.y` → `0.y+1`）**：可包含不兼容变更，但**必须在 CHANGELOG 中显著标注**并提供迁移说明
- **PATCH（`0.y.z` → `0.y.z+1`）**：仅向后兼容的修复
- 发布 **1.0.0** 的判据：核心公共 API（见第 4 节）稳定、评估集基线达标、文档完备

### 各版本号递增的判定表

| 变更类型 | 示例 | 递增位 |
|---------|------|--------|
| 破坏性变更 | 移除 `ChatService.ask` 签名参数；修改响应 `status` 枚举取值 | **MINOR**（1.0 后为 MAJOR） |
| 新增功能 | 新增工具、新增 Provider、新增配置项（有默认值） | **MINOR** |
| 缺陷修复 | 修复路由误判、修复内存泄漏 | **PATCH** |
| 文档 / CI / 测试 | 仅文档或流水线改动，无运行时影响 | **PATCH**（可合并至下次发布） |
| 安全修复 | 修复鉴权绕过 | **PATCH**，并按需 backport 到维护分支 |

---

## 2. 单一版本事实来源

版本号在以下位置保持一致，由 `scripts/bump_version.py` 统一维护：

| 文件 | 字段 |
|------|------|
| `pyproject.toml` | `[project].version` |
| `agentix/__init__.py` | `__version__` |
| `web/package.json` | `version` |

> **发布前自检**：CI 的 `version-consistency` 作业会比对三处版本号，不一致即失败。

---

## 3. 分支模型

采用简化版 **Trunk-Based + 发布分支** 模型：

```
main ────●────●────●────●──────────●──────────►  （始终可发布）
          \         \                \
           \         feat/x (PR)      release/0.1 ──►  hotfix
            \                                    （仅 backport）
             feat/y (PR)
```

| 分支 | 用途 | 生命周期 | 保护 |
|------|------|---------|------|
| `main` | 主干，始终可发布 | 永久 | 受保护，禁止直推，需 PR + CI + Review |
| `feat/*` `fix/*` `docs/*` `chore/*` | 功能/修复分支 | 合并即删 | — |
| `release/x.y` | 发布维护（backport 安全修复） | 直至该 minor 线 EOL | 受保护 |

---

## 4. 兼容性承诺

以下为**公共 API**（在 1.0 后受 SemVer 保护）：

| 层 | 公共 API |
|----|---------|
| HTTP | `/api/v1/chat`、`/api/v1/chat/stream`、`/api/v1/upload`、`/api/v1/sessions`、`/health`、`/ops/*` 的路由与请求/响应字段 |
| 领域端口 | `domain.ports` 中的 `Protocol` 与 `Payload` TypedDict |
| 扩展点 | `ToolRegistry.register` / `ToolSpec`、Provider 注册表接口 |
| 配置 | `settings.py` 中公开的配置项名称与语义 |

**内部实现**（不承诺兼容，可能随 MINOR 变更）：

- `agentix/application/agents/**` 下的节点函数签名与子图拓扑
- `agentix/infrastructure/**` 下的具体实现类
- 任何以 `_` 开头的符号

> 注意：本项目大量行为由 LLM 驱动，**模型输出不属于兼容性承诺范围**。评估集（`evals/`）用于量化行为回归，而非保证逐字一致。

---

## 5. 发布流程

```bash
# 1. 确保 main 干净且全绿
git checkout main && git pull
pytest -m "not integration" -q

# 2. 更新版本号（三处同步）
python scripts/bump_version.py 0.2.0

# 3. 更新 CHANGELOG：把 [Unreleased] 内容移入新版本段并标注日期
#    编辑 CHANGELOG.md

# 4. 提交并打标签
git add -A
git commit -m "chore(release): v0.2.0"
git tag -a v0.2.0 -m "Release v0.2.0"

# 5. 推送（触发 release.yml 自动发布）
git push origin main --follow-tags
```

发布工作流（`.github/workflows/release.yml`）会自动：

1. 校验标签与三处版本号一致
2. 构建 Python 包（sdist + wheel）
3. 从 CHANGELOG 提取该版本说明
4. 创建 GitHub Release 并上传构建产物

### 预发布

打 `-rc.N` / `-beta.N` 标签（如 `v0.2.0-rc.1`）会创建 **Pre-release**，不会成为「最新版本」。

---

## 6. 维护与 EOL

| 版本线 | 维护策略 |
|--------|---------|
| 最新 minor | 全量支持（功能 + 修复 + 安全） |
| 上一个 minor | 安全修复 + 严重缺陷修复（backport） |
| 更早版本 | 不再维护，建议升级 |

安全修复的 backport 通过 `release/x.y` 分支进行，并在 CHANGELOG 对应版本段记录。

---

## 7. 变更记录要求

- 每个 PR **必须**在 `CHANGELOG.md` 的 `[Unreleased]` 段添加条目（文档/CI 类改动可豁免）
- 条目需面向使用者描述**影响**，而非罗列实现细节
- 破坏性变更必须以 **`### 变更 (Changed)`** 或独立 **`### 破坏性变更`** 段落显著标注，并给出迁移步骤

---

## 8. 相关文件

- [`CHANGELOG.md`](../CHANGELOG.md) — 变更记录
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — 贡献流程与提交规范
- [`.github/workflows/release.yml`](../.github/workflows/release.yml) — 发布自动化
- [`scripts/bump_version.py`](../scripts/bump_version.py) — 版本号同步脚本
