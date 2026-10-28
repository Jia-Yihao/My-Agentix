"""T11：取消传播时延（bench 口径读数）——走 ``ControlPlane.cancel`` 原接口。

规划出处：``docs/design/审批与中断恢复优化规划.md`` §4.2 T11 卡（验收①）。

**测的是什么（一句话钉死，防止读数被引用成别的东西）**：一次
``await ControlPlane.cancel(run_id)`` 的**传播往返**毫秒数——从调用进入，到
被绑 ``asyncio.Task`` 真正退出（``task.done()``）为止的墙钟时间。``cancel``
不只是置终态，它还 ``await`` 被绑 Task 的退出（"已报取消但仍在跑"的假象由
这一步消掉），所以"等它退出的那段"就是传播时延本体。

**为什么计时在 bench 层而没违反"不复制逻辑"**（``bench/README.md`` 铁律 1）：
``ControlPlane`` 自己**不**产出耗时字段，也没有"测量模式"开关——它只做登记/
取消。bench 因此只包一层 ``time.perf_counter`` 与"调原接口 + 断言打断真发生"，
不重写打断语义、不重写状态迁移、不做第二个 cancel 实现。对比 ``targets.py``
里被禁止的那种"用 bench 自己的计时替代被测对象的计时"：本对象压根没有自己的
计时可比替代，属"采集面"该干的事。

**分位数、指纹、落盘全走骨架既有实现**（不许在本文件另立一份）：

- 分位数：``bench.aggregate.pooled_summary`` → ``evals.perf_stats.nearest_rank_quantile``；
- 指纹：``bench.fingerprint.collect_fingerprint``（含 ``quantile_source``）；
- 落盘：``bench.results.write_result``（无指纹不写出）。

**每轮样本数**：一轮 = 一次 ``asyncio.run`` 里跑 ``--samples`` 次独立的
"spawn → 绑 Task → cancel"；``--repeats`` ≥5 轮（``MIN_REPEATS``），报告用
**跨轮池化**分位数（原始样本并池后取分位，不是各轮分位再平均）。

**不承诺**：本文件只给读数（p50/p90/p95/p99/max/mean/count），不带任何阈值门禁，
也不改 ``ControlPlane`` 的任何行为——测出来的数字不构成"合格/不合格"判定。

用法::

    .venv/Scripts/python.exe -m bench.cancel_propagation --repeats 5 --samples 40

---

**T11 同卡口径（``BudgetExceededError`` 处置，事实陈述，不写建议）**

1. 生产路径**没有**任何专用 ``except BudgetExceededError``（``agentix/**`` 零处），
   它落到 ``chat_service.ask`` 末尾的 ``except Exception``：默认回执是
   **HTTP 500 ``internal_error``**；``LEGACY_ERROR_MODE=True`` 时是 200 + 兜底文案
   + ``error_code="internal_error"``。若异常的 ``str()`` 里恰好含瞬时特征
   （``500``/``429``/``502``/``503``/``504``/``timeout`` 等，含 ``used=``/``limit=``
   的数字）则被 ``is_retryable`` 判为上游瞬断 ⇒ 503 ``LLM_UNAVAILABLE``。
2. 生产装配的 Runtime 预算六维**全 0**（``chat_service.py`` 的 ``RuntimeBudget``）⇒
   ``BudgetManager`` 的每道 ``check_*`` 都带 ``> 0`` 前置 ⇒ 生产链路上该异常
   **不可达**。0 是「不限额」，不是「额度用完」，两者不得混为一谈。
3. 口径全量版（触发条件、六个抛点的前置判据、与 ``ErrorCode.TOKEN_BUDGET_EXCEEDED``
   的区别）写在 ``tests/test_p166_control_plane_cancel.py`` 模块 docstring，并由该
   文件的用例把现状钉住。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:  # 兼容非 -m 方式直跑
    sys.path.insert(0, _REPO_ROOT)

from agentix.runtime.control_plane import ControlPlane  # noqa: E402
from bench.aggregate import pooled_summary  # noqa: E402
from bench.fingerprint import MIN_REPEATS, QUANTILE_SOURCE, collect_fingerprint  # noqa: E402
from bench.results import DEFAULT_RESULTS_DIR, write_result  # noqa: E402

TARGET_NAME = "cancel_propagation"

#: 逐字记录调的是哪个原接口（进指纹的 ``entry`` 字段）
ENTRY = "agentix.runtime.control_plane:ControlPlane.cancel（原接口；bench 只包计时与断言）"

#: 读数口径：被测对象的边界（下游引用读数时必须连着这句一起引）
MEASUREMENT_SCOPE = (
    "单次 await ControlPlane.cancel(run_id) 的传播往返毫秒数：从调用进入到位"
    "给器断言 task.done() 为止（含 cancel 内部 await task 的等待）。"
    "不含 spawn/bind 开销，不含事件循环启动开销（t0 在 cancel 前一刻才取）。"
)

DEFAULT_SAMPLES = 40


async def _one_cancel_sample_ms() -> float:
    """一次独立测量的原始样本：spawn → 绑一件挂住的 Task → cancel → 计时。

    被测对象每次都是**全新的** ``ControlPlane``（不共享登记表）：重复测量之间
    不残留上一次的 run 记录，``max_runs`` 淘汰逻辑也就不会混进读数。
    """
    plane = ControlPlane()
    gate = asyncio.Event()
    started = asyncio.Event()

    async def _victim() -> None:
        # 明确停在可取消点：先自报"已进入"，再挂住——保证 t0 之后的 cancel
        # 打到的是"真的在跑"的任务，而不是还没被调度起来的空壳。
        started.set()
        await gate.wait()

    run = plane.spawn("bench_cancel_victim")
    task = asyncio.create_task(_victim())
    if not plane.bind(run.run_id, task):
        raise RuntimeError("bench 前置失败：bind 拒绝（ControlPlane 被改？）")
    plane.mark_running(run.run_id)
    await started.wait()

    t0 = time.perf_counter()
    cancelled = await plane.cancel(run.run_id, reason="bench")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # 读数只在"真打断"时才有意义：没打断的样本会把传播时延伪装成 0 附近。
    if not cancelled or not task.done():
        raise RuntimeError(
            "bench 前置失败：cancel 返回 true={} 但 task.done()={}——"
            "传播时延不可测，拒绝产出读数".format(cancelled, task.done())
        )
    gate.set()  # 防御：万一 task 没被取消，别把事件循环留在挂住态
    return elapsed_ms


async def _samples_async(count: int) -> List[float]:
    return [await _one_cancel_sample_ms() for _ in range(count)]


def collect_samples(repeats: int, samples_per_repeat: int) -> List[float]:
    """跑 ``repeats`` 轮、每轮 ``samples_per_repeat`` 个样本，原始样本全量交回。"""
    samples: List[float] = []
    for _ in range(repeats):
        samples.extend(asyncio.run(_samples_async(samples_per_repeat)))
    return samples


def build_payload(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser(
        prog="python -m bench.cancel_propagation",
        description="T11：取消传播时延读数（复用 bench 骨架的分位数/指纹/落盘）",
    )
    parser.add_argument(
        "--repeats", type=int, default=MIN_REPEATS,
        help=f"轮数（方法学下限 {MIN_REPEATS}，低于下限直接拒绝）",
    )
    parser.add_argument(
        "--samples", type=int, default=DEFAULT_SAMPLES,
        help=f"每轮样本数（缺省 {DEFAULT_SAMPLES}）",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = parser.parse_args(argv)

    if args.repeats < MIN_REPEATS:
        parser.error(
            f"--repeats={args.repeats} 低于方法学下限 {MIN_REPEATS}"
            "（重复 <5 没有可发布的尾部，与 bench/README.md 铁律 3 同口径）"
        )
    if args.samples < 1:
        parser.error(f"--samples={args.samples} 必须 ≥1")

    fingerprint = collect_fingerprint(
        target=TARGET_NAME,
        entry=ENTRY,
        repeats=args.repeats,
        touches_model=False,  # 纯进程内事件循环，不触碰任何模型采样接口
    )
    samples_ms = collect_samples(args.repeats, args.samples)
    pooled = pooled_summary(samples_ms)

    payload: Dict[str, Any] = {
        "fingerprint": fingerprint,
        "target": TARGET_NAME,
        "data_class": "benchmark_measurement",
        "measurement_status": "completed",
        "skip_reason": None,
        "measurement_scope": MEASUREMENT_SCOPE,
        "results": {
            "pooled_quantiles_ms": pooled,
            "pooled_status": "pooled_from_raw_samples",
            "pooled_note": (
                f"{args.repeats} 轮 × 每轮 {args.samples} 个原始样本（共 "
                f"{pooled['count']} 个）并池后取最近邻秩——非各轮分位数再平均"
            ),
            "per_repeat": [
                {
                    "samples_ms": [
                        round(v, 4)
                        for v in samples_ms[i * args.samples : (i + 1) * args.samples]
                    ]
                }
                for i in range(args.repeats)
            ],
            "quantile_source": QUANTILE_SOURCE,
        },
        "out_dir": str(args.out_dir),
    }
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    payload = build_payload(argv)
    out_dir = Path(payload.pop("out_dir"))
    path = write_result(payload, out_dir=out_dir)
    pooled = payload["results"]["pooled_quantiles_ms"]
    print(f"[bench] target={payload['target']} status={payload['measurement_status']}")
    print(
        "[bench] count={count} p50={p50} p90={p90} p95={p95} p99={p99} max={mx} (ms)".format(
            count=pooled["count"], p50=pooled["p50"], p90=pooled["p90"],
            p95=pooled["p95"], p99=pooled["p99"], mx=pooled["max"],
        )
    )
    print(f"[bench] 结果（含指纹块）已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
