"""Dependency-free HTTP load test: python scripts/load_test.py --url URL."""
import argparse, concurrent.futures, time, urllib.request

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
    latencies = sorted(t for _, t in results)
    print({"requests": a.requests, "success": len(ok), "errors": a.requests-len(ok),
           "rps": round(a.requests/elapsed, 2), "p95_seconds": round(latencies[int(.95*len(latencies))-1], 4)})
    return 0 if len(ok) == a.requests else 1

if __name__ == "__main__":
    raise SystemExit(main())
