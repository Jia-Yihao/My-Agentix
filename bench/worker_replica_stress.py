"""T15：Celery worker 副本压测（可复现脚本）——队列排空吞吐随副本数的变化。

规划出处：``docs/design/审批与中断恢复优化规划.md`` §4.3 T15 卡（验收②
「worker 副本压测读数带 T1 指纹」）。

**测的是什么（一句话钉死）**：把 ``--tasks`` 条**固定成本**任务一次性压进 broker，
量「队列从非空到排空」的墙钟时间，再除以任务数得到**排空吞吐（条/秒）**；对
``--replicas 1,2,4`` 各测 ``--repeats`` 轮，看吞吐是否随副本线性增长。

**任务体是 no-op 假件，不是 ``run_index_job``**（重要，别把读数读成索引吞吐）：

- 真任务要 MySQL/Milvus/MinIO/Postgres 与真实 kb 数据，属 staging 实测范围；
  在开发机上起不起得来，取决于环境，不取决于本卡。
- 假件的固定成本是「一次 BRPOP + 一次 INCR」，因此读数反映的是 **broker/调度面**
  与 worker 循环的扩容能力（这正是「副本数该加到几」要回答的问题），
  不含单作业的真实耗时。**不得**把它乘以任何作业时长去推「一小时能跑几个索引」。

**为什么脚本而不是 bench.targets 里的 target**：本对象需要外部进程（worker 子进程）
与外部服务（Redis broker），生命周期必须由测量者掌握；``bench.run`` 的 target 形态
只适合「一次调用即得样本」的对象。分位数/指纹/落盘仍复用骨架既有实现
（``bench.aggregate.pooled_summary`` / ``bench.fingerprint.collect_fingerprint`` /
``bench.results.write_result``），不另立第二套。

**前置（不满足就是未实测，脚本会落一份 ``skipped_no_environment`` 结果文件）**：

1. 一个可达的 Redis broker。本机最省事的起法（与 compose 同一镜像）::

       docker run --rm -d -p 16379:6379 redis/redis-stack-server:7.4.0-v2

   然后 ``--broker-url redis://127.0.0.1:16379/1``（db1 与生产 ``CELERY_BROKER_URL`` 同口径）。
2. 本机有 celery（``.venv/Scripts/python.exe -c "import celery"``）。
3. Windows 上必须 ``--pool=solo``（prefork 不可用）⇒ **一个副本 = 一个 worker 进程、
   并发 1**；compose 里是 ``--concurrency=2``，两者不可直接换算。
4. 压测会 ``FLUSHDB`` 目标 db——脚本只在 ``--broker-url`` 的 db 上做，**不要指向
   生产/预发 broker**。

用法::

    .venv/Scripts/python.exe -m bench.worker_replica_stress \
        --broker-url redis://127.0.0.1:16379/1 --replicas 1,2,4 --tasks 200
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:  # 兼容非 -m 方式直跑
    sys.path.insert(0, _REPO_ROOT)

from celery import Celery  # noqa: E402

from bench.aggregate import pooled_summary  # noqa: E402
from bench.fingerprint import MIN_REPEATS, QUANTILE_SOURCE, collect_fingerprint  # noqa: E402
from bench.results import DEFAULT_RESULTS_DIR, write_result  # noqa: E402
from bench.targets import SkipNoEnvironment  # noqa: E402

TARGET_NAME = "worker_replica_stress"

ENTRY = (
    "bench.worker_replica_stress:app.send_task('bench.worker_replica_stress.noop')"
    " → celery worker --pool=solo（副本数由 --replicas 指定）"
)

MEASUREMENT_SCOPE = (
    "队列排空吞吐：一次性压入 --tasks 条固定成本任务，从「最后一条入队」到"
    "「队列为空且所有 worker 的 active 集合为空」的墙钟时间，按 条/秒 换算。"
    "任务体是 no-op + 一次 Redis INCR（计数用），不是 run_index_job ⇒ "
    "读数是 broker/调度面的扩容曲线，不是索引作业吞吐。"
)

DEFAULT_REPLICAS = "1,2,4"
DEFAULT_TASKS = 200
#: 生产 app 用的默认队列名（celery_app 未配 task_routes ⇒ 全部进默认队列）
QUEUE_NAME = "celery"
#: 任务完成计数的 Redis 键（每次重复前清掉）
DONE_KEY = "bench:worker_replica_stress:done"

app = Celery("bench_worker_replica_stress")
app.conf.update(
    broker_url=os.environ.get("BENCH_CELERY_BROKER_URL", "redis://127.0.0.1:16379/1"),
    task_ignore_result=True,
    # 一次只预取一条：多副本下消息才均摊，否则快的 worker 会囤货，
    # 测出来的「扩容曲线」其实是单副本的曲线
    worker_prefetch_multiplier=1,
    task_acks_late=False,
)


#: worker 进程内复用的 Redis 客户端。刻意不在任务里 ``Redis.from_url``：
#: 那会每条任务新建一个连接池（一次 TCP 握手），把「调度面扩容」测成「建连扩容」。
_WORKER_CONN = None


def _worker_conn():
    global _WORKER_CONN
    if _WORKER_CONN is None:
        from redis import Redis

        _WORKER_CONN = Redis.from_url(app.conf.broker_url)
    return _WORKER_CONN


@app.task(name="bench.worker_replica_stress.noop")
def noop(index: int) -> int:
    """固定成本的假件：一次 Redis INCR 当完成信号（不写结果后端）。"""
    _worker_conn().incr(DONE_KEY)
    return index


def _redis_client():
    from redis import Redis

    return Redis.from_url(app.conf.broker_url)


def _probe_broker() -> None:
    """broker 连通性前置：不可达 ⇒ 未实测（不伪造读数）。"""
    try:
        _redis_client().ping()
    except Exception as exc:  # noqa: BLE001 - 任何连接失败都归「没环境」
        raise SkipNoEnvironment(
            f"broker {app.conf.broker_url} 不可达（{type(exc).__name__}: {exc}）⇒ 未实测。"
            "起一个本地 Redis 后重跑，命令见模块 docstring 前置 1"
        ) from exc


def _purge() -> None:
    """清掉队列与计数键：每轮从同一初始状态开始。"""
    conn = _redis_client()
    conn.delete(QUEUE_NAME, DONE_KEY)


def _worker_command(replica_index: int) -> List[str]:
    """一条副本的启动命令（Windows 安全：solo 池 + 唯一节点名）。"""
    return [
        sys.executable, "-m", "celery",
        "-A", "bench.worker_replica_stress:app",
        "worker",
        "--pool=solo",
        "--concurrency=1",
        "-n", f"bench-r{replica_index}@%h",
        "--loglevel=WARNING",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
        "-Q", QUEUE_NAME,
    ]


def _start_workers(replicas: int, cwd: str) -> List[subprocess.Popen]:
    procs: List[subprocess.Popen] = []
    for idx in range(replicas):
        procs.append(
            subprocess.Popen(  # noqa: S603 - 命令由本文件构造，无外部输入
                _worker_command(idx),
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
    return procs


def _stop_workers(procs: List[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - 仅清理兜底
            proc.kill()
            proc.wait(timeout=10)


def _wait_workers_ready(expected: int, timeout_s: float = 60.0) -> None:
    """等 ``expected`` 个节点回 ping；超时 ⇒ 环境没起来（不伪造读数）。"""
    deadline = time.monotonic() + timeout_s
    seen = 0
    while time.monotonic() < deadline:
        try:
            replies = app.control.ping(timeout=1.0) or []
        except Exception:  # noqa: BLE001 - ping 期间 broker 抖动不算测量失败
            replies = []
        seen = len(replies)
        if seen >= expected:
            return
        time.sleep(0.5)
    raise SkipNoEnvironment(
        f"{timeout_s:.0f}s 内只等到 {seen} 个 worker 回 ping（期望 {expected}）"
        " ⇒ 环境没起来，未实测"
    )


def _queue_is_empty(conn) -> bool:
    return conn.llen(QUEUE_NAME) == 0


def _one_drain_sample_ms(tasks: int, poll_s: float = 0.005) -> Tuple[float, Dict[str, Any]]:
    """压 ``tasks`` 条任务并量排空时间；返回 (毫秒, 观测到的事实)。"""
    conn = _redis_client()
    # t0 取在**第一条入队之前**：若只从「最后一条入队」起算，副本多到队列在发布期间
    # 就被消费光时，读数会变成「测生产端 send_task 往返」——那是最容易读错的形态。
    t0 = time.perf_counter()
    published = 0
    for index in range(tasks):
        app.send_task("bench.worker_replica_stress.noop", args=[index])
        published += 1
    publish_ms = (time.perf_counter() - t0) * 1000.0

    peak_queue = 0
    while True:
        length = conn.llen(QUEUE_NAME)
        peak_queue = max(peak_queue, length)
        done = int(conn.get(DONE_KEY) or 0)
        # 完成判据取**任务体自己的计数**（done == published），不取 inspect：
        # inspect 是广播 + 固定 timeout，秒级开销会盖掉被测的毫秒级排空时间。
        if done >= published and length == 0:
            break
        if time.perf_counter() - t0 > 300:  # pragma: no cover - 卡死兜底
            raise RuntimeError(
                f"300s 内未排空（done={done}/{published}, llen={length}）："
                "worker 可能已被杀或不消费——本轮读数作废"
            )
        time.sleep(poll_s)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if done != published:
        raise RuntimeError(
            f"完成计数 {done} != 压入数 {published} ⇒ 有任务没跑完，本轮读数作废"
        )
    return elapsed_ms, {
        "published": published,
        "publish_ms": round(publish_ms, 3),
        "peak_queue_length": peak_queue,
    }


def _measure_replicas(
    replicas: int, repeats: int, tasks: int, cwd: str
) -> Dict[str, Any]:
    procs = _start_workers(replicas, cwd)
    try:
        _wait_workers_ready(replicas)
        samples_ms: List[float] = []
        observations: List[Dict[str, Any]] = []
        for _ in range(repeats):
            _purge()
            elapsed_ms, facts = _one_drain_sample_ms(tasks)
            samples_ms.append(round(elapsed_ms, 3))
            observations.append(facts)
        pooled = pooled_summary(samples_ms)
        mean_ms = pooled["mean"]
        # 队列从未堆积（每轮 peak_queue_length 都是 0）⇒ 生产端 send_task 就是瓶颈，
        # 本组读数封顶在发布速率上，不能读成「worker 只能跑这么快」。
        producer_bound = all(o["peak_queue_length"] == 0 for o in observations)
        return {
            "replicas": replicas,
            "repeats": repeats,
            "tasks_per_repeat": tasks,
            "samples_ms": samples_ms,
            "pooled_quantiles_ms": pooled,
            "quantile_source": QUANTILE_SOURCE,
            "tasks_per_second": round(tasks * 1000.0 / mean_ms, 1) if mean_ms else None,
            "producer_bound": producer_bound,
            "producer_bound_note": (
                "队列在发布期间即被消费光（peak_queue_length 全 0）⇒ 本组吞吐是"
                "单线程 send_task 的上限，不是 worker 容量上限"
            ) if producer_bound else None,
            "observations": observations,
        }
    finally:
        _stop_workers(procs)


def _parse_replicas(raw: str) -> List[int]:
    values: List[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        value = int(item)
        if value < 1:
            raise ValueError(f"--replicas 只接受 ≥1 的整数，收到 {raw!r}")
        values.append(value)
    if not values:
        raise ValueError(f"--replicas 为空：{raw!r}")
    return sorted(set(values))


def build_payload(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser(
        prog="python -m bench.worker_replica_stress",
        description="T15：Celery worker 副本扩容曲线（复用 bench 骨架的分位数/指纹/落盘）",
    )
    parser.add_argument(
        "--repeats", type=int, default=MIN_REPEATS,
        help=f"每个副本数的重复轮数（方法学下限 {MIN_REPEATS}）",
    )
    parser.add_argument(
        "--replicas", default=DEFAULT_REPLICAS,
        help=f"逗号分隔的副本数（缺省 {DEFAULT_REPLICAS}）",
    )
    parser.add_argument(
        "--tasks", type=int, default=DEFAULT_TASKS,
        help=f"每轮压入的任务数（缺省 {DEFAULT_TASKS}）",
    )
    parser.add_argument(
        "--broker-url", default=None,
        help="Redis broker（缺省取 BENCH_CELERY_BROKER_URL 或 redis://127.0.0.1:16379/1）",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = parser.parse_args(argv)

    if args.repeats < MIN_REPEATS:
        parser.error(
            f"--repeats={args.repeats} 低于方法学下限 {MIN_REPEATS}"
            "（重复 <5 没有可发布的尾部，与 bench/README.md 铁律 3 同口径）"
        )
    if args.tasks < 1:
        parser.error(f"--tasks={args.tasks} 必须 ≥1")
    try:
        replica_counts = _parse_replicas(args.replicas)
    except ValueError as exc:
        parser.error(str(exc))

    if args.broker_url:
        app.conf.broker_url = args.broker_url

    fingerprint = collect_fingerprint(
        target=TARGET_NAME,
        entry=ENTRY,
        repeats=args.repeats,
        touches_model=False,  # 任务体是 no-op，不碰任何模型接口
    )

    try:
        _probe_broker()
        by_replicas = [
            _measure_replicas(count, args.repeats, args.tasks, _REPO_ROOT)
            for count in replica_counts
        ]
    except SkipNoEnvironment as exc:
        return {
            "fingerprint": fingerprint,
            "target": TARGET_NAME,
            "data_class": "benchmark_measurement",
            "measurement_status": "skipped_no_environment",
            "skip_reason": str(exc),
            "measurement_scope": MEASUREMENT_SCOPE,
            "results": {
                "pooled_quantiles_ms": None,
                "pooled_status": "not_measured_skipped",
                "pooled_note": str(exc),
                "per_repeat": [],
                "notes": None,
            },
            "out_dir": str(args.out_dir),
        }

    baseline = next(
        (row["tasks_per_second"] for row in by_replicas if row["replicas"] == 1), None
    )
    for row in by_replicas:
        row["speedup_vs_replicas_1"] = (
            round(row["tasks_per_second"] / baseline, 2)
            if baseline and row["tasks_per_second"] else None
        )

    return {
        "fingerprint": fingerprint,
        "target": TARGET_NAME,
        "data_class": "benchmark_measurement",
        "measurement_status": "completed",
        "skip_reason": None,
        "measurement_scope": MEASUREMENT_SCOPE,
        "results": {
            # 不同副本数是**不同处理组**：跨组池化没有意义（pooled 只在本组样本内做）
            "pooled_quantiles_ms": None,
            "pooled_status": "per_treatment_groups_only",
            "pooled_note": (
                "每个副本数自成一组的池化分位在 by_replicas[].pooled_quantiles_ms；"
                "跨副本数并池会把两种容量混成一个分布，禁止"
            ),
            "by_replicas": by_replicas,
            "quantile_source": QUANTILE_SOURCE,
            "notes": {
                "what_is_measured": (
                    "队列排空时间（ms）：最后一条入队 → 队列为空且 active 全空；"
                    "吞吐 = tasks*1000/组内池化 mean"
                ),
                "task_body": "no-op + 一次 Redis INCR（DONE_KEY 计数，兼作完成对账）",
                "broker": str(app.conf.broker_url),
                "queue": QUEUE_NAME,
                "worker_flags": {
                    "pool": "solo（Windows 无 prefork；一副本 = 一进程 = 并发 1）",
                    "prefetch_multiplier": 1,
                    "acks_late": False,
                },
                "excluded": (
                    "不是 run_index_job 的真实耗时；真任务需要 MySQL/Milvus/MinIO/Postgres "
                    "与真实 kb 数据 ⇒ 端到端扩容曲线属 staging 实测"
                ),
                "comparability": (
                    "读数只在本指纹 + 同一 broker + 同一 --tasks 下可比；"
                    "与 compose 的 --concurrency=2 不可直接换算"
                ),
            },
        },
        "out_dir": str(args.out_dir),
    }


def _print(text: str) -> None:
    """控制台输出兜底：只替换**该编码装不下**的字符（如 ``⇒`` 在 GBK 下会抛
    ``UnicodeEncodeError``，让一次成功写出结果文件的运行以崩溃收场）。
    中文在 GBK 下可编码，因此不会被替换。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    payload = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    sys.stdout.write(payload + chr(10))


def main(argv: Optional[List[str]] = None) -> int:
    payload = build_payload(argv)
    out_dir = Path(payload.pop("out_dir"))
    path = write_result(payload, out_dir=out_dir)
    status = payload["measurement_status"]
    _print(f"[bench] target={payload['target']} status={status}")
    if status == "completed":
        for row in payload["results"]["by_replicas"]:
            _print(
                "[bench] replicas={r} tasks/s={tps} speedup={sp} "
                "p50={p50} p95={p95} (ms/batch of {tasks}) producer_bound={pb}".format(
                    r=row["replicas"], tps=row["tasks_per_second"],
                    sp=row["speedup_vs_replicas_1"],
                    p50=row["pooled_quantiles_ms"]["p50"],
                    p95=row["pooled_quantiles_ms"]["p95"],
                    tasks=row["tasks_per_repeat"],
                    pb=row["producer_bound"],
                )
            )
    else:
        _print(f"[bench] 未实测：{payload['skip_reason']}")
    _print(f"[bench] 结果（含指纹块）已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
