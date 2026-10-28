# bench —— 统一基准入口（T1 骨架）

规划出处：`docs/design/审批与中断恢复优化规划.md` T1 卡（2026-10-27）。
本目录是**采集面**：把散在 `scripts/load_test.py`、`evals/performance/runner.py`
等处的基准收进一个统一入口，并强制每次测量落**环境指纹**。

## 铁律（违反任何一条 = 读数作废）

1. **走原接口，不复制逻辑**：bench 只做 import + 调用 + 结果整形。计时、压测、
   判定语义全在被测对象自己家里；把测量逻辑抄进 bench 即失真。
2. **每份结果文件顶层必含 `fingerprint` 块**：执行点是 `bench/results.py` 的
   落盘闸——缺指纹或缺必需字段的 payload 直接拒绝写出，不补空指纹。
3. **重复次数 ≥5**（`bench/fingerprint.py` 的 `MIN_REPEATS`），报告用**跨轮池化
   分位数**（原始样本并池后取分位），严禁各轮分位数再平均。
4. **分位数只有一把尺子**：`evals/perf_stats.nearest_rank_quantile`（全仓唯一
   实现，2026-10-27 口径统一）。bench 内不许出现第二份秩算式，守卫见
   `tests/test_p145_bench_fingerprint.py`（复用 `tests/test_p135_quantile_convention.py`
   的 AST 判据把 `bench` 目录也管起来）。
5. **无环境不推断读数**：被测对象需要外部服务（HTTP 端点 / Docker / 模型 key）
   而环境缺失时，产出显式 `skipped_no_environment` 状态的结果文件（有指纹、
   读数字段为 null），绝不伪造。
6. 任何性能声明必须带**测量日期 + 环境**（都在指纹块里），且本骨架阶段
   **不下任何性能结论**——`selfcheck` 的读数逐字标记
   `skeleton_selfcheck_not_a_measurement`。

## 用法

```bash
# 离线自证（骨架验收路径：验证"每份结果必含指纹块"，不测生产组件）
.venv/Scripts/python.exe -m bench.run --target selfcheck

# 收纳 evals/performance/runner.py：iterations 由 --repeats 透传（原接口内部池化）
.venv/Scripts/python.exe -m bench.run --target perf_runner --families stream,cpu

# 收纳 scripts/load_test.py：需要可达端点；不给 --url 即显式跳过
.venv/Scripts/python.exe -m bench.run --target load_test --url http://localhost:8000/liveness
```

结果默认落 `bench/results/`（已登记 `.gitignore`，评测原始数据不进 git）；
文件名 `<target>-<UTC时间戳>.json`，重复跑不互相覆盖。

## 检索构造基准（T12：每请求重建 embedding 客户端）

```bash
# 默认检索路径的"每请求重建"：5 轮 × 每轮 10 个样本，跨轮池化
.venv/Scripts/python.exe -m bench.run --target retrieval_vectorstore_build

# 只测构造缝：create_embeddings() 新建客户端（不含任何嵌入请求）
.venv/Scripts/python.exe -m bench.run --target retrieval_embedding_client

# 可选：对现有索引读（缺省是运行时用确定性假件铸的小 FAISS 索引）
.venv/Scripts/python.exe -m bench.run --target retrieval_vectorstore_build \
    --index-path data/demo/indexes/legal-demo
```

口径与 caveat（同样写进结果文件的 `results.notes`）：

- 被测缝是 `get_or_build_retriever(index_path)`；`RETRIEVAL_RETRIEVER_CACHE_ENABLED`
  为默认关时它每次直调 `build_retriever`，**本 bench 不改该默认值**；开关打开时
  本 target 直接拒绝取读数（读数会被缓存命中污染）。
- 只测 **faiss** 档。milvus/qdrant 构造期只连服务、`_get_embeddings` 是惰性
  调用点，本机无服务 ⇒ 未实测；跨栈差异**只作 caveat，不作结论**。
- 默认 fixture 是 20 文档 / 8 维的小索引：读数反映"新建客户端 + 索引反序列化"
  的固定开销，真实索引越大反序列化占比越高；读数只在本指纹与同一索引规模下可比。
- 两个 target 都零网络、不发起检索或嵌入请求——是构造代价，不是检索延迟，
  更不是线上结论。

## 队列与入库吞吐基准（T15：Celery 积压 / worker 副本 / kb_ingest 逐行）

```bash
# kb_ingest 逐行扁平化吞吐（离线；5 轮 × 每轮 200 行，跨轮池化）
.venv/Scripts/python.exe -m bench.kb_ingest_throughput --repeats 5 --rows 200

# worker 副本扩容曲线（需可达 Redis broker；db1 与生产 CELERY_BROKER_URL 同口径）
docker run --rm -d -p 16379:6379 redis/redis-stack-server:7.4.0-v2
.venv/Scripts/python.exe -m bench.worker_replica_stress \
    --broker-url redis://127.0.0.1:16379/1 --replicas 1,2,4 --tasks 200
```

口径与 caveat（同样写进结果文件的 `results.notes`）：

- `kb_ingest_flatten` 测的是 `kb_service.services.utils.flatten_row` 的逐行**确定性
  转换**（列名归一化 + 拼接），即 `USE_LLM=false` 与 LLM 兜底路径上的同一函数。
  **不含** LLM 改写 / embedding / Milvus 写入 / MySQL 读取 ⇒ 它是逐行 CPU 吞吐，
  不是端到端入库吞吐；读数只在同一指纹 + 同一行宽（12 列）+ 同一 `flat_max_len` 下可比。
- `worker_replica_stress` 的任务体是 **no-op 假件**（固定成本），量的是
  broker/调度面的扩容曲线，**不是** `run_index_job` 的作业吞吐；真任务要
  MySQL/Milvus/MinIO/Postgres ⇒ 端到端扩容属 staging 实测。
  计时从**第一条入队前**起算（否则副本多时读数会变成「测生产端」），
  完成判据取任务体自己的 Redis 计数（`inspect` 的秒级广播会盖掉毫秒级排空）；
  某组 `peak_queue_length` 全 0 时该组 `producer_bound=true`——读数是发布端上限，
  不能读成 worker 容量上限。Windows 下只能 `--pool=solo`（一副本 = 一进程 = 并发 1），
  与 compose 的 `--concurrency=2` **不可直接换算**。
- 两个 target 都不触碰模型接口（指纹里 `temperature` 记 `not_applicable` 并附判据）。

## 指纹必需字段

`measured_at`（ISO8601 UTC）、`target`、`entry`（逐字记录调的是哪个原接口）、
`python_version`、`sdk_versions`（agentix/openai/anthropic/langchain/
langchain-openai/langgraph/fastapi，未安装记 null）、`temperature`
（与被测模型无关的对象记 `not_applicable` 并附判据 `temperature_basis`；
触碰模型的对象缺温度直接报错）、`repeats`、`os_platform`、`cpu`、
`quantile_source` + `quantile_convention`。

## 文件一览

| 文件 | 职责 |
|---|---|
| `run.py` | CLI 入口（`python -m bench.run`），参数化 target/重复/落点 |
| `fingerprint.py` | 指纹采集 + 必需字段清单 + 下限/温度纪律 |
| `aggregate.py` | 跨轮池化摘要（分位数委托唯一实现） |
| `targets.py` | 被收纳对象登记表（原接口调用 + 返回形态分流） |
| `results.py` | 落盘闸：无指纹不写出 |
