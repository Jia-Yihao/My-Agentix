# My Agentix 法律法规领域 —— 灌库 / 解锁图谱推理 起栈操作手册

> 适用对象：需要在本机启动全套基础设施、把法律数据灌入各存储、并解锁图谱推理能力的工程师。
> 本文档只描述**起栈后的操作**。离线已完成的部分见文末「备注」。

---

## 0. 前置条件

1. 已安装 **Docker Desktop** 并处于可运行状态。
2. 仓库已 `git clone` 到本地，当前工作目录为仓库根目录
   （`My-Agentix`）。
3. 已安装 **LM Studio**（用于本地 LLM / 嵌入端点，OpenAI 兼容，`http://localhost:1234/v1`）。
4. 已具备 `python`（受管 Python `C:\Users\15237\.workbuddy\binaries\python\versions\3.13.12\python.exe` 或等价 3.13+ 环境）。

---

## 1. 启动基础设施（Docker 栈）

在**仓库根目录**执行：

```bash
# 启动全部服务（Neo4j / Milvus / Redis / kb_ingest 等）
docker-compose up -d

# 观察各服务启动与端口就绪情况
docker-compose ps
docker-compose logs -f neo4j milvus
```

等待各端口可用（首次 neo4j 初始化较慢，约数十秒到数分钟）：

| 服务 | 容器内端口 | docker-compose 宿主映射 | 说明 |
|------|-----------|------------------------|------|
| Neo4j Bolt | 7687 | **17687** → 7687 | 图数据库协议 |
| Neo4j HTTP | 7474 | **17474** → 7474 | 浏览器 / REST |
| Milvus | 19530 | 19530 → 19530 | 向量库（KB，R3.7 起唯一向量后端） |

> ⚠️ **端口映射注意**：`docker-compose.yml` 当前把 Neo4j 映射到宿主 `17687/17474`、
> 就绪探测器 `verify_ingestion_pipeline.py` 默认按
> **标准端口 127.0.0.1:7687/7474/19530** 探测。若希望验证脚本跑出 ✅，二选一：
> - 修改 `docker-compose.yml` 端口映射为 `7687:7687` / `7474:7474`；或
> - 在容器网络内（或 port-forward 后）使用标准端口再跑验证。
> 本文其余命令按 docker-compose 实际映射为准。

Neo4j 凭据（见 `agentix/config/settings.py`）：

```text
NEO4J_URI      = bolt://neo4j:7687
NEO4J_USER     = (空，auth 已关闭)
NEO4J_PASSWORD = (空，auth 已关闭)
```

---

## 2. 分阶段执行灌库命令（复制即用）

### ① Neo4j（法律本体图谱）

```bash
# 步骤 1：由 seed.json 生成 Neo4j 导入 CSV（14 个）
python scripts/legal_kg_to_csv.py \
  --seed-json data/legal/legal_seed.json \
  --output-dir data/legal/csv

# 步骤 2：CSV 由 Dockerfile 的 `neo4j_seeded` 构建阶段导入。
# 起栈时 docker-compose 的 neo4j 服务已使用该目标，若需重建：
docker-compose up -d --build neo4j
```

### ② kb 向量库（法律语料）

```bash
# kb_ingest 服务会读取 data/kb/legal_corpus.txt 并写入 Milvus legal 集合
docker-compose up -d kb_ingest

# 查看导入进度
docker-compose logs -f kb_ingest
```

相关配置项（`agentix/config/settings.py`）：
`KB_DATA_FILE=data/kb/legal_corpus.txt`、`KB_USE_LLM`、`KB_EMBEDDING_*`。

### ③ LightRAG（法条 + 司法解释语料）

```bash
# 以 JSON 语料源初始化 LightRAG（写入 LIGHTRAG_WORKING_DIR）
python scripts/init_lightrag.py \
  --source json \
  --json-path data/legal/legal_lightrag_corpus.json
```

---

## 3. 启动 LM Studio 并加载模型（解锁 LLM 抽取）

图谱推理与语义抽取依赖本地 LLM 端点。

1. 打开 **LM Studio**，在 *Local Server* 页启动 Server，确认监听
   `http://localhost:1234/v1`（OpenAI 兼容）。
2. 加载模型 **`qwen/qwen3.5-9b`**（用于后续对法条做语义抽取）。
3. 验证端点可达：

```bash
curl http://localhost:1234/v1/models
```

---

## 4. 验证

### 4.1 跑就绪探测脚本

各阶段依赖就绪后，运行探测器应变为 ✅：

```bash
python scripts/data/verify_ingestion_pipeline.py
```

脚本仅做**只读探测**（不启动/不连接任何服务），即便全部 ⛔ 也会以退出码 0 正常结束。
输出包含「环境快照」与「分阶段状态表」。

### 4.2 Neo4j Cypher 查节点数

```cypher
// 节点总数
MATCH (n) RETURN count(n) AS node_count;

// 按标签统计（法律本体标签，如 Article / Clause / Cause 等）
MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY cnt DESC;

// 边总数（注意：规则化抽取的 `引用` 边在本数据集为 0，属数据限制）
MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC;
```

---

## 5. 备注（离线已完成 / 数据限制）

- **离线已满量验证** `数据→CSV`：`scripts/legal_kg_to_csv.py` 已对
  `data/legal/legal_seed.json` 跑通，`article_nodes.csv` ≈ **1953 行**。
- **就绪探测脚本** `scripts/data/verify_ingestion_pipeline.py` 已就绪，可随时评估各阶段。
- **`引用` 边为空属数据限制**：本数据集中法条间无显式跨条引用，规则化抽取结果为 0；
  若需补全 `引用` / `对应案由` / `有司法解释` 等边，须依赖 **LM Studio（qwen/qwen3.5-9b）**
  做语义抽取（见第 3 节）。
- 当前环境（Docker 未启动、各端口关闭、LM Studio 关闭）下，探测器输出应全部为 ⛔/⏳，
  属预期状态，不影响脚本正常退出。
