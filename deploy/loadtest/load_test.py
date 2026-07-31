"""
Load test script (Step 18).

A small, dependency-light async load generator (httpx only - already a
project dependency, no need to pull in k6/locust for this) that fires
concurrent requests at a running gateway and reports status-code
distribution and latency percentiles. Good for demoing rate limiting,
admission control, and fallback/circuit-breaking under real concurrent
load, not just one request at a time.

Usage:
    python deploy/loadtest/load_test.py --team team-a --requests 100 --concurrency 20
    python deploy/loadtest/load_test.py --team team-b --requests 30 --concurrency 30   # team-b's rpm=5 -> expect lots of 429s
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx

TEAM_KEYS = {
    "team-a": "sk-team-a-demo-key",
    "team-b": "sk-team-b-demo-key",
    "team-c": "sk-team-c-demo-key",
}


async def _one_request(client: httpx.AsyncClient, url: str, headers: dict, payload: dict) -> tuple[int, float]:
    start = time.monotonic()
    try:
        resp = await client.post(url, json=payload, headers=headers, timeout=30.0)
        return resp.status_code, time.monotonic() - start
    except httpx.HTTPError:
        return 0, time.monotonic() - start  # connection-level failure, not an HTTP status


async def run(base_url: str, team: str, model: str, total_requests: int, concurrency: int) -> None:
    api_key = TEAM_KEYS.get(team, team)  # allow passing a raw key directly too
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": "load test message"}]}
    url = f"{base_url}/v1/chat/completions"

    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(client):
        async with semaphore:
            return await _one_request(client, url, headers, payload)

    print(f"Firing {total_requests} requests at {url} (team={team}, model={model}, concurrency={concurrency})...")
    start = time.monotonic()
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[bounded(client) for _ in range(total_requests)])
    elapsed = time.monotonic() - start

    status_codes = [r[0] for r in results]
    latencies = sorted(r[1] for r in results)

    print(f"\nCompleted in {elapsed:.2f}s ({total_requests / elapsed:.1f} req/s attempted)\n")
    print("Status code distribution:")
    for code in sorted(set(status_codes)):
        count = status_codes.count(code)
        label = {0: "connection error", 200: "OK", 429: "rate limited",
                  402: "budget exceeded", 503: "server busy (admission)", 502: "all providers failed"}.get(code, str(code))
        print(f"  {code:>3} ({label}): {count}")

    def pct(p: float) -> float:
        idx = min(len(latencies) - 1, int(len(latencies) * p))
        return latencies[idx]

    print("\nLatency (successful + failed requests together):")
    print(f"  p50: {pct(0.50)*1000:.0f}ms   p95: {pct(0.95)*1000:.0f}ms   p99: {pct(0.99)*1000:.0f}ms")
    print(f"  min: {latencies[0]*1000:.0f}ms   max: {latencies[-1]*1000:.0f}ms   mean: {statistics.mean(latencies)*1000:.0f}ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load test the LLM gateway")
    parser.add_argument("--url", default="http://localhost:8000", help="Gateway base URL")
    parser.add_argument("--team", default="team-a", help="team-a | team-b | team-c, or a raw API key")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--requests", type=int, default=100, help="Total number of requests to fire")
    parser.add_argument("--concurrency", type=int, default=20, help="Max concurrent in-flight requests")
    args = parser.parse_args()

    asyncio.run(run(args.url, args.team, args.model, args.requests, args.concurrency))


if __name__ == "__main__":
    main()
