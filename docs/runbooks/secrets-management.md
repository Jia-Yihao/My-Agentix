# 密钥管理 Runbook（企业级评估 §4.3）

> 现状：密钥经 `.env` 明文 + compose `env_file` 注入。本文是过渡方案 + 轮换流程；
> Vault/AWS Secrets Manager 集成为正式方案（待立项，见文末）。

## 当前防线（已有）

1. **dev 缺失**：启动期临时生成强随机密钥 + 强告警（`resolved_secret_key`，P0-6）；
2. **生产缺失**：fail-fast 拒绝启动；
3. **字段集合自检**（p35）：`.env` 静默回落默认值在启动日志显式告警。

## 过渡改进（低成本，立即执行项）

1. `.env` 权限收紧：`chmod 600 .env`（Windows 侧限制到当前用户可读）；
2. `.env` 不进 Git（确认 `.gitignore` 覆盖）；示例只进 `.env.example`（无真实密钥）；
3. compose 迁移 `secrets:` 挂载（swarm/compose v2 语法），避免 `docker inspect` 泄露环境变量——见 `docker-compose.yml` 后续 PR；
4. 日志纪律：任何密钥值不进日志（现有约定：只打"explicit/temporary"状态）。

## 密钥轮换流程（SECRET_KEY / JWT）

1. 新密钥写入 `.env` 的 `SECRET_KEY_NEW`，应用支持双读（当前未支持则跳到快速路径）；
2. **快速路径**（当前实现）：停机窗口内换 `SECRET_KEY` → 重启。代价：所有已签发 JWT 失效，用户重新登录；
3. 验证：登录 → 刷新页面（新 cookie 有效）→ `/readiness` 正常；
4. 更新 `docs/runbooks/postmortems.md` 记录轮换时间与影响面。

## API_KEY（服务间鉴权）轮换

1. 生成新 key → 调用方灰度切换 → 移除旧 key；
2. 中间件按常量比对（`APIKeyMiddleware`），暂不支持多 key 并存——轮换需协调调用方，切换窗口 < 1 分钟。

## 正式方案（立项前置条件）

- 密钥数量 > 10 或多环境部署成为常态时，接 **Vault / AWS Secrets Manager**；
- 选型约束：贴合现有 compose 部署形态（Sidecar 或 env 注入均可），不引入 K8S 依赖。
