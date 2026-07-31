"""
Audit log for admin actions: who changed what, when, and the before/after
values. Stored as a capped Redis list (newest first via LPUSH + LTRIM) - a
full audit/event store with indexing and a real retention policy is a
natural next step, not built here for a local demo.
"""
from __future__ import annotations

import json
import time
from typing import Any

import redis.asyncio as redis

_AUDIT_KEY = "admin:audit_log"
_MAX_ENTRIES = 1000


class AuditLog:
    def __init__(self, redis_url: str):
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._redis.aclose()

    async def record(
        self, *, admin_name: str, action: str, team_name: str, before: Any, after: Any
    ) -> None:
        entry = {
            "timestamp": time.time(),
            "admin": admin_name,
            "action": action,
            "team": team_name,
            "before": before,
            "after": after,
        }
        await self._redis.lpush(_AUDIT_KEY, json.dumps(entry))
        await self._redis.ltrim(_AUDIT_KEY, 0, _MAX_ENTRIES - 1)

    async def list_recent(self, limit: int = 50, team_name: str | None = None) -> list[dict]:
        # Capped at 1000 entries, so scanning the whole list client-side is
        # fine for a demo; at real scale this would be a per-team stream or
        # an indexed store instead of a single filtered list.
        raw_entries = await self._redis.lrange(_AUDIT_KEY, 0, -1)
        entries = [json.loads(raw) for raw in raw_entries]
        if team_name:
            entries = [e for e in entries if e["team"] == team_name]
        return entries[:limit]


_audit_log: AuditLog | None = None


def build_audit_log(redis_url: str) -> AuditLog:
    global _audit_log
    _audit_log = AuditLog(redis_url)
    return _audit_log


def get_audit_log() -> AuditLog:
    if _audit_log is None:
        raise RuntimeError("Audit log not initialized yet")
    return _audit_log
