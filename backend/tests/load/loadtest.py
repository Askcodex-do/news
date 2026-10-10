"""Standalone load test for the read API (spec section 36: load testing).

Runs a fixed number of concurrent virtual users against a *live* server and
reports throughput and latency percentiles, then exits non-zero if a budget is
missed. This is deliberately dependency-free (httpx + asyncio, both already
required) so it can run in CI without adding locust.

Usage:
    # start the API first, then:
    python tests/load/loadtest.py --base-url http://localhost:8000 \
        --users 50 --duration 30 --max-p95-ms 400 --max-error-rate 0.01

The feed endpoints are the hot path: they run ranking queries per request, so
they are what a capacity plan should be based on.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field

import httpx

# Endpoints exercised. "/feed" is the heaviest (global + local ranking).
PATHS = ["/health", "/feed", "/articles", "/geo", "/country-sources", "/sources"]
# Health is exempt from rate limiting, so a load test of the read path does not
# get throttled; the other paths are rate-limited in production, which is why a
# large --users run may see 429s by design. Those are counted separately.


@dataclass
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    status_counts: dict[int, int] = field(default_factory=dict)
    errors: int = 0

    def record(self, status: int, elapsed_ms: float) -> None:
        self.latencies_ms.append(elapsed_ms)
        self.status_counts[status] = self.status_counts.get(status, 0) + 1

    def percentile(self, pct: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
        return ordered[index]


async def _worker(
    client: httpx.AsyncClient,
    results: Results,
    stop_at: float,
    lock: asyncio.Lock,
) -> None:
    i = 0
    while time.monotonic() < stop_at:
        path = PATHS[i % len(PATHS)]
        i += 1
        start = time.perf_counter()
        try:
            response = await client.get(path)
            elapsed = (time.perf_counter() - start) * 1000
            async with lock:
                results.record(response.status_code, elapsed)
        except httpx.HTTPError:
            elapsed = (time.perf_counter() - start) * 1000
            async with lock:
                results.errors += 1
                results.record(0, elapsed)


async def run(base_url: str, users: int, duration: float) -> Results:
    results = Results()
    lock = asyncio.Lock()
    stop_at = time.monotonic() + duration
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, limits=limits) as client:
        await asyncio.gather(*(_worker(client, results, stop_at, lock) for _ in range(users)))
    return results


def report(results: Results, elapsed: float) -> None:
    total = len(results.latencies_ms)
    print(f"\nrequests: {total} in {elapsed:.1f}s  ({total / elapsed:.0f} req/s)")
    print(f"errors (transport): {results.errors}")
    print("status codes:", dict(sorted(results.status_counts.items())))
    if total:
        print(
            "latency ms: "
            f"p50={results.percentile(50):.1f} "
            f"p90={results.percentile(90):.1f} "
            f"p95={results.percentile(95):.1f} "
            f"p99={results.percentile(99):.1f} "
            f"max={max(results.latencies_ms):.1f} "
            f"mean={statistics.fmean(results.latencies_ms):.1f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test the read API.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=25)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--max-p95-ms", type=float, default=500.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    args = parser.parse_args()

    start = time.monotonic()
    results = asyncio.run(run(args.base_url, args.users, args.duration))
    elapsed = time.monotonic() - start

    report(results, elapsed)

    total = len(results.latencies_ms) or 1
    server_errors = sum(
        count for status, count in results.status_counts.items() if status == 0 or status >= 500
    )
    error_rate = (results.errors + server_errors) / total
    p95 = results.percentile(95)

    failed = False
    if p95 > args.max_p95_ms:
        print(f"FAIL: p95 {p95:.1f}ms exceeds budget {args.max_p95_ms}ms")
        failed = True
    if error_rate > args.max_error_rate:
        print(f"FAIL: error rate {error_rate:.3%} exceeds budget {args.max_error_rate:.1%}")
        failed = True
    if failed:
        return 1
    print("PASS: within latency and error budgets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
