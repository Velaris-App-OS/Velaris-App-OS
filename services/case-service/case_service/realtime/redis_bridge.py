"""Redis pub/sub bridge for the ConnectionManager.

When enabled, each case-service instance:
  - subscribes to a wildcard Redis channel (helix:rt:*)
  - broadcasts locally received events to Redis for other instances
  - receives Redis events from other instances and fans out to local subscribers

This makes WebSocket events visible across horizontally-scaled replicas.
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


class RedisBridge:
    def __init__(self, manager, redis, prefix: str = "helix:rt:", instance_id: str = ""):
        self.manager = manager
        self.redis = redis
        self.prefix = prefix
        self.instance_id = instance_id
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._subscriber_loop())
        log.info("Redis pub/sub bridge started (prefix=%s)", self.prefix)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def publish(self, channel: str, envelope: dict[str, Any]) -> None:
        """Publish to Redis — other instances will pick this up and fan out locally."""
        if not self._running:
            return
        try:
            payload = {"_src": self.instance_id, "channel": channel, "envelope": envelope}
            await self.redis.publish(f"{self.prefix}{channel}", json.dumps(payload, default=str))
        except Exception as e:
            log.debug("Redis publish failed: %s", e)

    async def _subscriber_loop(self) -> None:
        try:
            pubsub = self.redis.pubsub()
            await pubsub.psubscribe(f"{self.prefix}*")
            while self._running:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except Exception as e:
                    log.debug("Redis subscriber recv failed: %s", e)
                    await asyncio.sleep(1)
                    continue
                if not msg or msg.get("type") not in ("message", "pmessage"):
                    continue
                try:
                    data = json.loads(msg["data"])
                    # Avoid rebroadcast loop: ignore our own messages
                    if data.get("_src") == self.instance_id:
                        continue
                    channel = data["channel"]
                    envelope = data["envelope"]
                    # Fan out to LOCAL subscribers only (no Redis re-publish)
                    await self.manager._local_fanout(channel, envelope)
                except Exception as e:
                    log.debug("Redis message parse failed: %s", e)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await pubsub.close()  # type: ignore
            except Exception:
                pass
