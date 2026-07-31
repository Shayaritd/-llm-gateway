"""
Redis-backed token bucket rate limiting: per-team RPM (requests/minute) and
TPM (tokens/minute) enforcement.

Design:
  - One Lua script (EVAL) checks and, if allowed, deducts from BOTH the RPM
    and TPM buckets for a team in a single atomic round trip. If either
    bucket lacks capacity, NEITHER is deducted - a request that fails on
    TPM doesn't still burn an RPM slot. This is the key reason it's a
    single script instead of two separate check-then-deduct calls: doing
    it in two round trips would either double-spend under concurrency (a
    classic TOCTOU race) or require a client-side transaction just to undo
    a partial deduction.
  - Buckets refill continuously (tokens/sec), not reset at fixed minute
    boundaries, so bursts are smoothed rather than causing a thundering
    herd right after each boundary.
  - Because the whole check+deduct happens inside Redis via one script,
    this is correct across multiple gateway processes/instances talking to
    the same Redis - a real distributed limiter, not a per-process counter.
  - TPM cost is a pre-admission *estimate* (see app.tokens) - prompt tokens
    approximated from message length, completion reserved via max_tokens
    or a default.

Explicitly NOT in scope here: budget/cost tracking in dollars (Step 6),
fallback routing, dashboards.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import redis.asyncio as redis

from app.config import RateLimitConfig

# KEYS[1] = rpm bucket hash key, KEYS[2] = tpm bucket hash key
# ARGV: rpm_capacity, rpm_refill_per_sec, tpm_capacity, tpm_refill_per_sec,
#       tpm_cost, now, rpm_ttl, tpm_ttl
_TOKEN_BUCKET_SCRIPT = """
local rpm_capacity = tonumber(ARGV[1])
local rpm_refill    = tonumber(ARGV[2])
local tpm_capacity  = tonumber(ARGV[3])
local tpm_refill    = tonumber(ARGV[4])
local tpm_cost      = tonumber(ARGV[5])
local now           = tonumber(ARGV[6])
local rpm_ttl       = tonumber(ARGV[7])
local tpm_ttl       = tonumber(ARGV[8])

local function refill(key, capacity, refill_rate)
  local bucket = redis.call("HMGET", key, "tokens", "ts")
  local tokens = tonumber(bucket[1])
  local ts = tonumber(bucket[2])
  if tokens == nil then
    tokens = capacity
    ts = now
  end
  local delta = math.max(0, now - ts)
  tokens = math.min(capacity, tokens + delta * refill_rate)
  return tokens
end

local rpm_tokens = refill(KEYS[1], rpm_capacity, rpm_refill)
local tpm_tokens = refill(KEYS[2], tpm_capacity, tpm_refill)

local rpm_ok = rpm_tokens >= 1
local tpm_ok = tpm_tokens >= tpm_cost

if rpm_ok and tpm_ok then
  rpm_tokens = rpm_tokens - 1
  tpm_tokens = tpm_tokens - tpm_cost
end

redis.call("HMSET", KEYS[1], "tokens", rpm_tokens, "ts", now)
redis.call("EXPIRE", KEYS[1], rpm_ttl)
redis.call("HMSET", KEYS[2], "tokens", tpm_tokens, "ts", now)
redis.call("EXPIRE", KEYS[2], tpm_ttl)

if rpm_ok and tpm_ok then
  return {1, "", "0", tostring(rpm_tokens), tostring(tpm_tokens)}
end

local limit_type
local retry_after
if not rpm_ok then
  limit_type = "rpm"
  retry_after = (1 - rpm_tokens) / rpm_refill
else
  limit_type = "tpm"
  retry_after = (tpm_cost - tpm_tokens) / tpm_refill
end

return {0, limit_type, tostring(retry_after), tostring(rpm_tokens), tostring(tpm_tokens)}
"""


class RateLimitExceeded(Exception):
    def __init__(self, limit_type: str, retry_after: float):
        self.limit_type = limit_type  # "rpm" or "tpm"
        self.retry_after = retry_after
        super().__init__(f"{limit_type} rate limit exceeded, retry after {retry_after:.2f}s")


@dataclass
class RateLimitStatus:
    allowed: bool
    limit_type: str | None
    retry_after: float
    rpm_remaining: float
    tpm_remaining: float


class TokenBucketLimiter:
    def __init__(self, redis_url: str):
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._script = self._redis.register_script(_TOKEN_BUCKET_SCRIPT)

    async def close(self) -> None:
        await self._redis.aclose()

    async def check_and_consume(
        self, team_name: str, limits: RateLimitConfig, tpm_cost: int
    ) -> RateLimitStatus:
        rpm_refill = limits.rpm / 60.0
        tpm_refill = limits.tpm / 60.0
        now = time.time()
        # TTL: long enough for a fully-drained bucket to refill twice over,
        # so an idle team's keys expire instead of living in Redis forever.
        rpm_ttl = max(60, math.ceil(limits.rpm / rpm_refill) * 2) if rpm_refill > 0 else 3600
        tpm_ttl = max(60, math.ceil(limits.tpm / tpm_refill) * 2) if tpm_refill > 0 else 3600

        result = await self._script(
            keys=[f"ratelimit:{team_name}:rpm", f"ratelimit:{team_name}:tpm"],
            args=[
                limits.rpm,
                rpm_refill,
                limits.tpm,
                tpm_refill,
                tpm_cost,
                now,
                rpm_ttl,
                tpm_ttl,
            ],
        )
        allowed, limit_type, retry_after, rpm_remaining, tpm_remaining = result
        return RateLimitStatus(
            allowed=bool(int(allowed)),
            limit_type=limit_type or None,
            retry_after=float(retry_after),
            rpm_remaining=float(rpm_remaining),
            tpm_remaining=float(tpm_remaining),
        )

    async def peek(self, team_name: str, limits: RateLimitConfig) -> tuple[float, float]:
        """Read-only view of current bucket levels, for the admin usage
        endpoint - does not consume anything. Not atomic with a concurrent
        check_and_consume (a plain read, computed client-side), but that's
        fine here: a status view doesn't need transactional consistency
        with live traffic, unlike admission itself."""
        now = time.time()
        rpm_data = await self._redis.hmget(f"ratelimit:{team_name}:rpm", "tokens", "ts")
        tpm_data = await self._redis.hmget(f"ratelimit:{team_name}:tpm", "tokens", "ts")

        def _current(data: list, capacity: int, refill_rate: float) -> float:
            tokens_raw, ts_raw = data
            if tokens_raw is None:
                return float(capacity)
            tokens = float(tokens_raw)
            ts = float(ts_raw)
            delta = max(0.0, now - ts)
            return min(float(capacity), tokens + delta * refill_rate)

        rpm_remaining = _current(rpm_data, limits.rpm, limits.rpm / 60.0)
        tpm_remaining = _current(tpm_data, limits.tpm, limits.tpm / 60.0)
        return rpm_remaining, tpm_remaining


async def enforce_rate_limit(
    limiter: TokenBucketLimiter, team_name: str, limits: RateLimitConfig | None, tpm_cost: int
) -> None:
    """Raises RateLimitExceeded if the team is over its RPM or TPM budget."""
    if limits is None:
        return
    status = await limiter.check_and_consume(team_name, limits, tpm_cost)
    if not status.allowed:
        raise RateLimitExceeded(status.limit_type or "rpm", status.retry_after)


_limiter: TokenBucketLimiter | None = None


def build_rate_limiter(redis_url: str) -> TokenBucketLimiter:
    global _limiter
    _limiter = TokenBucketLimiter(redis_url)
    return _limiter


def get_rate_limiter() -> TokenBucketLimiter:
    if _limiter is None:
        raise RuntimeError("Rate limiter not initialized yet")
    return _limiter
