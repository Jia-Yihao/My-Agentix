#!/usr/bin/env python
"""轻量 HTTP 负载压测器（企业级 P2-6.3 性能基线 workflow 的执行体）。

**为什么存在**：``.github/workflows/perf-baseline.yml`` 每天 UTC 18:00 跑一次
性能基线，并靠本脚本产出的 ``p95_seconds`` 与上一版基线比对（劣化 >20% 阻断）。
本文件此前**只被引用、未被实现**——workflow 一执行就会在 "Run load test" 步
直接报 *No such file or directory*，也就是说性能门禁从未真正运行过
（NOT-ENFORCED 的最坏形态：配置看起来齐全，其实是死的）。

设计约束：

- **只用标准库**：CI 里本脚本在 ``pip install -r requirements.txt`` 之后运行，
  但把压测器绑死在第三方 HTTP 客户端上会平添一个"未声明依赖"的隐患面
  （requirements.txt 与 pyproject.toml 的双声明漂移就是这么发生的）。此处用
  ``http.client`` 的**线程内复用连接**（keep-alive），既贴近真实压测语义
  （不把 TCP 握手成本算进单请求延迟），又零依赖。
- **stdout 只输出 JSON**：workflow 把 stdout ``tee`` 进 ``baseline.json``
  再 ``json.load``。任何进度/日志都必须走 **stderr**，否则文件不是合法 JSON。
- **零成功样本时不报 0.0**：分母为 0 时返回 ``null``（``"无数据"``），
  而不是 ``0.0``（``"全都很快"``）。把"没跑成"伪装成"很快"是性能门禁最
  容易自欺的地方；此处以非零退出码 + ``::error::`` 注解双保险。

用法::

    python scripts/load_test.py --url http://localhost:8000/liveness \\
        --requests 200 --workers 10

    # 结果（stdout，可直接重定向为 baseline.json）
    {"url": "...", "p95_seconds": 0.0131, "success": 200, "errors": 0, ...}
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 直跑（``python scripts/load_test.py``）时 sys.path[0] 是 scripts/，仓库根不在
# 搜索路径里；分位数的**唯一实现**住在 evals/ 包里（2026-10-27 口径统一），
# 先把根插进来才 import 得到。该模块只用标准库 ⇒ 本脚本"零第三方依赖"的前提不变。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.perf_stats import nearest_rank_quantile  # noqa: E402  （依赖上面的根注入）


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def percentile(values: List[float], p: float) -> Optional[float]:
    """最近邻秩分位数——**委托**全仓唯一实现 ``evals/perf_stats.py``。

    本函数此前是**线性插值**（与 numpy 默认 ``method="linear"`` 一致），理由是
    "小样本上最近秩会让 p95 == p99，尾部劣化看不出来"。2026-10-27 起全仓统一
    到最近邻秩（规划文档 T2）：一把尺子优先于每处各自挑"更合用"的那把——同一份
    延迟样本过去按调用哪个脚本得出不同 p95，而 ``perf-baseline.yml`` 的
    「劣化 >20% 就阻断」正好建在这个数字上。⚠️ 切换日期之前的 p50/p95/p99
    与之后不同尺，跨日期比对前必须先按新口径重跑基线。

    样本量足够大时（本脚本默认 200 请求）两种口径几乎重合；差异主要落在小样本。

    :param values: 样本（未排序亦可）
    :param p: 分位点，0–100（本脚本的对外口径仍是百分数）
    :return: 分位数；``values`` 为空时返回 ``None``（**不是 0.0**）
    """
    return nearest_rank_quantile(values, p / 100.0)


def summarize(samples: List[float]) -> Dict[str, Optional[float]]:
    """把延迟样本（秒）压成一份统计摘要。"""
    if not samples:
        return {
            "count": 0, "min": None, "mean": None, "p50": None,
            "p90": None, "p95": None, "p99": None, "max": None,
        }
    return {
        "count": len(samples),
        "min": min(samples),
        "mean": sum(samples) / len(samples),
        "p50": percentile(samples, 50),
        "p90": percentile(samples, 90),
        "p95": percentile(samples, 95),
        "p99": percentile(samples, 99),
        "max": max(samples),
    }


# ---------------------------------------------------------------------------
# 压测
# ---------------------------------------------------------------------------


class _Worker:
    """单线程 worker：复用一条 keep-alive 连接，逐个发请求。"""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        *,
        method: str,
        body: Optional[bytes],
        headers: Dict[str, str],
        timeout: float,
        use_tls: bool,
    ) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._method = method
        self._body = body
        self._headers = headers
        self._timeout = timeout
        self._use_tls = use_tls
        self._conn: Optional[http.client.HTTPConnection] = None
        self.latencies: List[float] = []
        self.errors: List[str] = []
        self.status_codes: Dict[int, int] = {}

    def _connect(self) -> http.client.HTTPConnection:
        if self._conn is not None:
            return self._conn
        if self._use_tls:
            ctx = ssl.create_default_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                self._host, self._port, timeout=self._timeout, context=ctx
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败的连接没有补救价值
                pass
            self._conn = None

    def run(self, count: int) -> None:
        for _ in range(count):
            started = time.perf_counter()
            try:
                conn = self._connect()
                conn.request(self._method, self._path, body=self._body, headers=self._headers)
                response = conn.getresponse()
                response.read()  # 必须读完，否则连接不可复用
                self.latencies.append(time.perf_counter() - started)
                self.status_codes[response.status] = self.status_codes.get(response.status, 0) + 1
            except Exception as exc:  # noqa: BLE001 - 压测中任何异常都算一次失败样本
                self.errors.append(f"{type(exc).__name__}: {exc}")
                # 连接可能已半死，丢弃后重连（不要把坏连接带进下一轮）
                self.close()


def run_load(
    url: str,
    *,
    requests_total: int,
    workers: int,
    timeout: float,
    method: str = "GET",
    body: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    warmup: int = 0,
) -> Tuple[Dict[str, Any], int]:
    """执行压测。返回 ``(report, exit_code)``。

    退出码语义：``0`` = 有成功样本（可用）；``2`` = **零成功样本**，
    报告里的分位数是 ``null`` —— 调用方不得把它当成"很快"。
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme {parsed.scheme!r} (expect http/https)")
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    requests_total = max(1, int(requests_total))
    workers = max(1, min(int(workers), requests_total))
    per_worker = [requests_total // workers] * workers
    for i in range(requests_total % workers):
        per_worker[i] += 1

    base_headers = {"Accept": "application/json", "Connection": "keep-alive"}
    base_headers.update(headers or {})

    def _make() -> _Worker:
        return _Worker(
            host, port, path,
            method=method, body=body, headers=base_headers,
            timeout=timeout, use_tls=parsed.scheme == "https",
        )

    # 预热：把建连 / 首次编译 / 连接池填充的成本排除在统计之外
    if warmup > 0:
        warm = _make()
        warm.run(warmup)
        warm.close()

    pool = [_make() for _ in range(workers)]
    threads = [threading.Thread(target=w.run, args=(n,)) for w, n in zip(pool, per_worker)]

    wall_started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall_seconds = time.perf_counter() - wall_started

    latencies: List[float] = []
    errors: List[str] = []
    status_codes: Dict[int, int] = {}
    for worker in pool:
        latencies.extend(worker.latencies)
        errors.extend(worker.errors)
        for code, n in worker.status_codes.items():
            status_codes[code] = status_codes.get(code, 0) + n
        worker.close()

    latency = summarize(latencies)
    report: Dict[str, Any] = {
        "url": url,
        "method": method,
        "requests": requests_total,
        "workers": workers,
        "success": len(latencies),
        "errors": len(errors),
        "error_samples": errors[:5],
        "status_codes": {str(k): v for k, v in sorted(status_codes.items())},
        "wall_seconds": round(wall_seconds, 6),
        # 零成功样本 ⇒ 吞吐未知（None），不是 0.0。写成 0.0 会让"服务打不通"
        # 与"服务慢到 0 吞吐"在下游看起来一样，而两者的处置完全不同。
        "throughput_rps": (
            round(len(latencies) / wall_seconds, 3) if latencies and wall_seconds > 0 else None
        ),
        "latency_seconds": {k: (None if v is None else round(v, 6)) for k, v in latency.items()},
        # workflow 直接读这个顶层键（约定：key 名带单位，避免"这到底是 ms 还是 s"）
        "p95_seconds": None if latency["p95"] is None else round(latency["p95"], 6),
    }
    return report, (0 if latencies else 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="轻量 HTTP 压测器（stdout 输出单个 JSON，供 perf-baseline workflow 消费）"
    )
    parser.add_argument("--url", required=True, help="目标 URL（http/https）")
    parser.add_argument("--requests", type=int, default=200, help="总请求数（默认 200）")
    parser.add_argument("--workers", type=int, default=10, help="并发线程数（默认 10）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒（默认 10）")
    parser.add_argument("--method", default="GET", help="HTTP 方法（默认 GET）")
    parser.add_argument("--body", default=None, help="请求体字符串")
    parser.add_argument(
        "--header", action="append", default=[], metavar="K:V",
        help="附加请求头，可重复（如 --header 'Authorization: Bearer x'）",
    )
    parser.add_argument("--warmup", type=int, default=0, help="预热请求数（不计入统计）")
    parser.add_argument(
        "--pretty", action="store_true", help="缩进输出（人工阅读用；CI 请勿使用）"
    )
    return parser


def _parse_headers(raw: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in raw:
        if ":" not in item:
            raise ValueError(f"--header 需要 'K:V' 形式，收到 {item!r}")
        key, _, value = item.partition(":")
        headers[key.strip()] = value.strip()
    return headers


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    body = args.body.encode("utf-8") if args.body else None
    try:
        report, code = run_load(
            args.url,
            requests_total=args.requests,
            workers=args.workers,
            timeout=args.timeout,
            method=args.method.upper(),
            body=body,
            headers=_parse_headers(args.header),
            warmup=args.warmup,
        )
    except ValueError as exc:
        print(f"[load-test] 参数错误：{exc}", file=sys.stderr)
        return 2

    # stdout 必须是**唯一**的 JSON 文档（workflow 会 tee 成 baseline.json）
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))

    if report["success"] == 0:
        # 零成功样本 ⇒ 分位数是 null。必须显式报错，不能让 p95=null 被当成
        # "没有劣化"而静默通过。
        print(
            "::error title=perf-baseline::压测零成功样本，p95_seconds 为 null"
            "（不是 0）——性能门禁本次无法判定，请先修复被测服务。",
            file=sys.stderr,
        )
        return 2
    if report["errors"] > 0:
        print(
            f"::warning title=perf-baseline::{report['errors']}/"
            f"{report['requests']} 次请求失败，p95 仅基于成功样本计算。",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
