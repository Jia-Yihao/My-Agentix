"""Dependency-free HTTP load test: python scripts/load_test.py --url URL."""
import argparse, concurrent.futures, sys, time, urllib.request
from pathlib import Path

# 直跑时 sys.path[0] 是 scripts/test/，仓库根不在搜索路径里；分位数一律取自
# evals/perf_stats 那份唯一实现（2026-10-27 口径统一），本脚本不再自己写一份秩算式。
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evals.perf_stats import nearest_rank_quantile  # noqa: E402  （依赖上面的根注入）


def one(url, timeout):
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # nosec B310 - URL 由操作员 CLI 提供
            response.read(64)
            return response.status, time.perf_counter() - started
    except Exception:
        return 0, time.perf_counter() - started

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000/health")
    p.add_argument("--requests", type=int, default=100)
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--timeout", type=float, default=10)
    a = p.parse_args()
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(lambda _: one(a.url, a.timeout), range(a.requests)))
    elapsed = time.perf_counter() - started
    ok = [r for r, _ in results if 200 <= r < 400]
    latencies = [t for _, t in results]
    p95 = nearest_rank_quantile(latencies, 0.95)
    print({"requests": a.requests, "success": len(ok), "errors": a.requests-len(ok),
           "rps": round(a.requests/elapsed, 2),
           "p95_seconds": round(p95, 4) if p95 is not None else None})
    return 0 if len(ok) == a.requests else 1

if __name__ == "__main__":
    raise SystemExit(main())
