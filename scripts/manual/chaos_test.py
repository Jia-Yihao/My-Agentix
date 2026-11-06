"""Small, safe chaos smoke test: verify graceful failure and recovery of an HTTP endpoint."""

import argparse
import socket
import time
import urllib.request


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000/health")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--wait", type=float, default=1)
    a = p.parse_args()
    print(
        "Chaos probe: checking TCP connectivity and HTTP recovery "
        "(no destructive actions)."
    )
    try:
        with socket.create_connection((a.host, a.port), timeout=3):
            pass
    except OSError as exc:
        print("initial connectivity failed:", exc)
        return 1
    for _ in range(a.retries):
        try:
            with urllib.request.urlopen(a.url, timeout=5) as response:  # nosec B310 - URL 由操作员 CLI 提供
                if 200 <= response.status < 500:
                    print("service responded:", response.status)
                    return 0
        except Exception as exc:
            # 探针的语义就是"把任何瞬态故障记下来继续重试"，故此处刻意广捕。
            print("transient failure:", exc)
        time.sleep(a.wait)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
