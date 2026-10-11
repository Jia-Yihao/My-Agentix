# Staging 环境与配置矩阵（企业级评估 §7.1）

> 部署物：`docker/compose.override.staging.yml`（与根 `docker-compose.yml` 叠加）

## 启动

```bash
docker compose -f docker-compose.yml -f docker/compose.override.staging.yml \
  --profile monitoring up -d
```

注意：staging 使用独立 compose 项目名 `agentix-staging`，容器/卷均与 dev/prod 隔离，可整组销毁重建。

## 配置矩阵（环境间差异清单）

| 配置 | dev | staging | production |
|---|---|---|---|
| `DEBUG` | true | **false** | false |
| `REQUIRE_API_KEY` | false | **true** | true（强校验） |
| `SECRET_KEY` | 临时生成+告警 | 显式注入 | 显式注入（缺失拒绝启动） |
| `RATE_LIMIT_ENABLED` | false | **true** | true（强校验） |
| `LLM_MAX_CONCURRENCY` | 0（不限） | **10** | 按配额定（建议 20） |
| `PROMPT_INJECTION_DETECTION_ENABLED` | true | true | true |
| `PROMPT_INJECTION_INGEST_BLOCK` | false | **false**（观察期） | 上线 2 周后评估开启 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 空（本地 span） | **prometheus:9090** | 真实 OTLP 后端 |
| 监控栈 | 不起 | **--profile monitoring** | 独立部署或同 compose |
| 数据 | sqlite/内存 | 真实存储 + 小容量 | 真实存储全量 |
| 卷 | 本地目录 | `staging_` 前缀独立卷 | 持久卷 + 备份 |

## staging 验收清单（发布前跑一遍）

1. `/readiness` 全绿 + `/metrics` 可抓取
2. 登录 → 上传 → 索引 → 检索 → 流式问答全链路通
3. Grafana 看板有数据（QPS/P95/5xx/限流/降级/熔断六组曲线）
4. `LLM_MAX_CONCURRENCY` 生效：压测中观察到排队（llm_concurrency 贴上限）
5. 配置矩阵逐项 diff（`docker compose config` 输出比对）
