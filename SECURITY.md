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
| **XSS 防护** | 前端 HTML 实体转义 + 安全 linkify | `web/src/modules/chat/components/ChatWidget.vue` |
| **文件大小限制** | 统一使用 `settings.FILE_UPLOAD_MAX_MB`，分块写入防 OOM | `agentix/interfaces/http/v1/upload.py` |

## 部署安全建议

- **必须**在生产环境显式配置 `SECRET_KEY` 与 `API_KEY`，并将 `REQUIRE_API_KEY=true`
- **不要**将 `.env` 提交到版本库（已在 `.gitignore` 中排除）
- 生产环境请通过密钥管理服务（KMS / Vault / 环境变量注入）下发密钥
- 数据库（Neo4j / PostgreSQL / MySQL / Redis / Milvus）请勿暴露公网，使用内网或私有网络
- 反向代理层启用 TLS，并限制请求体大小与速率
- 定期运行 `pip-audit` / Dependabot 检查依赖漏洞（CI 已集成 Dependabot）

## 依赖安全现状

- 以 `pip-audit -r requirements.txt` 实测为准（2026-10-04，S2 安全批）：残余 **21 项 / 8 包**，全部属 langchain 0.3.x 生态（langgraph-checkpoint、langgraph、langchain-core、langchain、langchain-openai、langchain-text-splitters、langgraph-sdk、langsmith），清零需 langchain 1.x 大版本迁移（后续计划）；核心 Web 栈（`fastapi==0.142.2` / `starlette==1.7.0` 等）漏洞已清零，详见 `CHANGELOG.md`「S2 安全批」。
- `bandit`（medium+ 级别）当前 **0 findings**；LOW 级信息性项（try/except pass 等）保留并逐项复核。
- 升级纪律：`requirements.txt` 与 `pyproject.toml` 双声明必须同步（`tests/test_dependency_consistency.py` 守卫），升级后以全量测试套件验证（README §11 唯一真源）。

## 已知边界（非缺陷）

- `Guardrails` 为**概率性**范围判定（LLM 驱动），不能替代确定性的访问控制；请勿将其作为唯一的安全边界
- 本地开发默认使用 LM Studio，若切换为云端模型，请评估数据出境与合规要求

---

感谢你帮助保持 My Agentix 及其用户的安全。
