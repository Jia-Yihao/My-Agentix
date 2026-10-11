# 法条 / 批量导入指南

本项目的知识库（KB）以**知识条目文档**为基本存储单元，入库与检索统一走
**kb_ingest 独立服务**（`http://localhost:8100`，compose 映射
`${KB_INGEST_PORT:-8100}:8000`，前缀 `/api/v1/knowledge`）。
现行 5 个端点：

1. `POST /ingest/excel` — 按**主机本地路径**导入 Excel
2. `POST /ingest/excel/upload` — 上传 Excel 文件导入
3. `POST /ingest/mysql` — 从 MySQL 表拉取并入库
4. `POST /search` — 向量检索
5. `POST /search/hybrid` — 向量 + 重排混合检索

> 旧知识库的 provisions 导入脚本与 `/api/v1/knowledge/provisions/*` 端点已随实现移除，
> 下述方式为现行链路。

---

## 前置条件

- 启动服务（推荐 Docker Compose）：

```bash
docker-compose up -d
```

- 确认 kb_ingest API 可访问：

```bash
curl -s http://localhost:8100/docs >/dev/null && echo "OK"
```

如使用非默认端口（`KB_INGEST_PORT`）或域名，请替换上述基址。
开启鉴权（`KB_REQUIRE_API_KEY`）时需带请求头 `X-API-Key: <KB_API_KEY>`。

---

## 方式 1：Excel / MySQL 批量导入（kb_ingest）

### 上传 Excel 入库

```bash
curl -X POST "http://localhost:8100/api/v1/knowledge/ingest/excel/upload" \
  -F "file=@path/to/laws.xlsx"
```

### 按主机本地路径导入

```bash
curl -X POST "http://localhost:8100/api/v1/knowledge/ingest/excel" \
  -H "Content-Type: application/json" \
  -d '{"path": "/path/to/laws.xlsx"}'
```

（请求字段见 `http://localhost:8100/docs` 的 `ExcelIngestRequest`。）

### 从 MySQL 导入

```bash
curl -X POST "http://localhost:8100/api/v1/knowledge/ingest/mysql" \
  -H "Content-Type: application/json" \
  -d '{"table": "provisions"}'
```

（字段见 `MySQLIngestRequest`。）

以上端点均返回 **202 Accepted**，入库在后台任务中完成，进度看日志：

```bash
docker-compose logs -f kb_ingest
```

---

## 方式 2：LightRAG 图谱语料导入

适用于 LightRAG 图谱侧（非 Milvus 向量库）语料灌入，语料文件为
`data/legal/legal_lightrag_corpus.json`：

```bash
# 全量写入（--json-path 可指定语料文件，--limit 可限量）
python scripts/init_lightrag.py --source legal \
  --json-path data/legal/legal_lightrag_corpus.json
```

其它可选参数：`--working-dir`（LightRAG 工作目录）。

---

## 检索验证

```bash
# 向量检索
curl -X POST "http://localhost:8100/api/v1/knowledge/search" \
  -H "Content-Type: application/json" \
  -d '{"query": "违约责任", "top_k": 6}'

# 混合检索（向量召回 + 重排）
curl -X POST "http://localhost:8100/api/v1/knowledge/search/hybrid" \
  -H "Content-Type: application/json" \
  -d '{"query": "违约责任", "vector_top_k": 20, "rerank_top_k": 6}'
```

---

## 常见问题

- **报错：无法连接到 `http://localhost:8100`**
  - 确认 `docker-compose up -d` 已启动
  - 或确认 `KB_INGEST_PORT` 覆盖后的实际端口

- **401 Invalid or missing API key**
  - kb_ingest 开启了 `KB_REQUIRE_API_KEY`，请求需带 `X-API-Key` 头

- **入库返回 5xx / 写入失败**
  - 批量写入会触发向量化与入库流程，请查看后端日志：
    `docker-compose logs -f kb_ingest`
