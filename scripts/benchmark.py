#!/usr/bin/env python3
"""
Benchmark /meetings endpoint response time.

Usage:
    python scripts/benchmark.py                          # default: 10 warm-up, 20 measured
    python scripts/benchmark.py --requests=50            # 50 measured requests
    python scripts/benchmark.py --warmup=5               # 5 warm-up requests
    python scripts/benchmark.py --endpoint=/members       # benchmark a different route
    python scripts/benchmark.py --no-cache                # add ?_=timestamp to bypass cache
"""

import argparse
import time
import urllib.request
import statistics


BASE_URL = "http://127.0.0.1:5000"


def benchmark(endpoint: str, warmup: int, requests: int, no_cache: bool) -> dict:
    url = f"{BASE_URL}{endpoint}"
    results = []
    errors = 0

    # Warm-up
    for _ in range(warmup):
        try:
            req_url = f"{url}?_={int(time.time())}" if no_cache else url
            urllib.request.urlopen(req_url, timeout=10)
        except Exception:
            pass

    # Measured requests
    for i in range(requests):
        try:
            t0 = time.monotonic()
            req_url = f"{url}?_={int(time.time())}" if no_cache else url
            with urllib.request.urlopen(req_url, timeout=10) as resp:
                content_len = len(resp.read())
            elapsed = time.monotonic() - t0
            results.append(elapsed)
        except Exception as e:
            errors += 1
            results.append(None)

    valid = [r for r in results if r is not None]
    if not valid:
        return {"error": "All requests failed", "errors": errors}

    return {
        "endpoint": endpoint,
        "requests": requests,
        "errors": errors,
        "warmup": warmup,
        "no_cache": no_cache,
        "min": min(valid),
        "max": max(valid),
        "mean": statistics.mean(valid),
        "median": statistics.median(valid),
        "stdev": statistics.stdev(valid) if len(valid) > 1 else 0,
        "total_bytes": content_len if valid else 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark Poliscopic endpoints")
    parser.add_argument("--endpoint", default="/meetings", help="Route to benchmark")
    parser.add_argument("--requests", type=int, default=20, help="Number of measured requests")
    parser.add_argument("--warmup", type=int, default=10, help="Number of warm-up requests")
    parser.add_argument("--no-cache", action="store_true", help="Bypass server cache with _=timestamp")
    args = parser.parse_args()

    print(f"Benchmarking {args.endpoint} ...")
    print(f"  Warm-up: {args.warmup}, Measured: {args.requests}, No-cache: {args.no_cache}")
    print()

    result = benchmark(args.endpoint, args.warmup, args.requests, args.no_cache)

    if "error" in result:
        print(f"ERROR: {result['error']}")
        return

    print(f"  {'Min:':12s} {result['min']:.3f}s")
    print(f"  {'Max:':12s} {result['max']:.3f}s")
    print(f"  {'Mean:':12s} {result['mean']:.3f}s")
    print(f"  {'Median:':12s} {result['median']:.3f}s")
    print(f"  {'Std Dev:':12s} {result['stdev']:.3f}s")
    print(f"  {'Total bytes:':12s} {result['total_bytes']:,}")
    print(f"  {'Errors:':12s} {result['errors']}")


if __name__ == "__main__":
    main()
