"""Redis-backed token bucket rate limiter — suitable for multi-instance deployments.

Uses a single atomic Lua script per request to consume a token and return remaining.
Falls back gracefully if Redis is unavailable.
"""
from __future__ import annotations
import logging
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

# Atomic token bucket — returns [allowed(0/1), remaining, retry_after_ms]
_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then tokens = capacity end
if ts == nil then ts = now_ms end

local elapsed = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
local retry_ms = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
  allowed = 1
else
  retry_ms = math.floor(((1.0 - tokens) / refill_rate) * 1000)
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, ttl)
return {allowed, math.floor(tokens), retry_ms}
"""


class RedisRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        requests_per_minute: int = 120,
        burst: int = 30,
        exclude_paths: list[str] | None = None,
    ):
        super().__init__(app)
        self.rpm = requests_per_minute
        self.refill_rate = requests_per_minute / 60.0
        self.burst = burst
        self.exclude = set(exclude_paths or ["/health", "/ready", "/metrics"])
        self._script_sha: Optional[str] = None

    async def _get_redis(self):
        from case_service.redis_client import get_redis
        return await get_redis()

    async def _consume(self, redis, ip: str) -> tuple[bool, int, int]:
        if self._script_sha is None:
            try:
                self._script_sha = await redis.script_load(_LUA)
            except Exception as e:
                log.debug("script_load failed: %s", e)
                return True, self.burst, 0
        now_ms = int(time.time() * 1000)
        try:
            res = await redis.evalsha(
                self._script_sha, 1,
                f"helix:rl:{ip}",
                self.burst, self.refill_rate, now_ms, 600_000,
            )
            allowed, remaining, retry_ms = res
            return bool(int(allowed)), int(remaining), int(retry_ms)
        except Exception as e:
            log.debug("rate limit eval failed: %s", e)
            return True, self.burst, 0  # fail-open

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.exclude:
            return await call_next(request)

        redis = await self._get_redis()
        if redis is None:
            # Redis unavailable — fail-open (allow request)
            return await call_next(request)

        ip = "unknown"
        if request.client:
            ip = request.client.host
        fwd = request.headers.get("X-Forwarded-For")
        if fwd:
            ip = fwd.split(",")[0].strip()

        allowed, remaining, retry_ms = await self._consume(redis, ip)
        if not allowed:
            retry_after = max(1, retry_ms // 1000)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded", "retry_after_seconds": retry_after},
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self.rpm),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Backend": "redis",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.rpm)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Backend"] = "redis"
        return response
