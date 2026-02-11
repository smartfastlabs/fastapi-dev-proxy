"""Optional Redis backend for distributed relay (request/response bus and store)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING, Any

from .protocol import WebhookRequestMessage, WebhookResponse, WebhookResponseMessage
from .redis_constants import (
    OWNER_KEY_TTL_SECONDS,
    enabled_key,
    override_paths_key,
    owner_key,
    requests_channel,
    responses_channel,
    token_hash,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


def create_redis(url: str) -> Redis:
    """Create an async Redis client from URL. Requires redis package (pip install fastapi-dev-proxy[redis])."""
    from redis.asyncio import from_url

    return from_url(url, decode_responses=True)


class RedisStore:
    """Redis-backed relay state: owner, enabled, override_paths."""

    def __init__(self, redis: Redis, token: str, instance_id: str, enabled: bool = True) -> None:
        self._redis = redis
        self._th = token_hash(token)
        self._instance_id = instance_id
        self._enabled = enabled

    async def get_enabled(self) -> bool:
        raw = await self._redis.get(enabled_key(self._th))
        if raw is None:
            return self._enabled
        return raw == "1"

    async def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        await self._redis.set(enabled_key(self._th), "1" if enabled else "0")

    async def get_override_paths(self) -> list[str]:
        raw = await self._redis.get(override_paths_key(self._th))
        if raw is None:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    async def set_override_paths(self, paths: list[str]) -> None:
        await self._redis.set(override_paths_key(self._th), json.dumps(paths))

    async def get_owner(self) -> str | None:
        return await self._redis.get(owner_key(self._th))

    async def set_owner(self, instance_id: str | None, ttl_seconds: float | None = None) -> None:
        key = owner_key(self._th)
        if instance_id is None:
            await self._redis.delete(key)
            return
        ttl = ttl_seconds if ttl_seconds is not None else OWNER_KEY_TTL_SECONDS
        await self._redis.set(key, instance_id, ex=int(ttl))

    async def is_owner(self, instance_id: str) -> bool:
        owner = await self.get_owner()
        return owner == instance_id

    async def renew_owner_ttl(self) -> None:
        """Renew owner key TTL (heartbeat). Call periodically while this instance owns the connection."""
        key = owner_key(self._th)
        if await self._redis.get(key) == self._instance_id:
            await self._redis.expire(key, OWNER_KEY_TTL_SECONDS)


async def publish_request(redis: Redis, token: str, message: WebhookRequestMessage) -> None:
    """Publish a webhook request to the requests channel."""
    th = token_hash(token)
    await redis.publish(requests_channel(th), json.dumps(message))


async def publish_response(redis: Redis, token: str, message: WebhookResponseMessage) -> None:
    """Publish a webhook response to the responses channel."""
    th = token_hash(token)
    await redis.publish(responses_channel(th), json.dumps(message))


async def subscribe_requests(
    redis: Redis,
    token: str,
    handler: Any,
) -> asyncio.Task[None]:
    """
    Start a task that subscribes to the requests channel and calls handler(message) for each WebhookRequestMessage.
    handler is an async callable taking one WebhookRequestMessage dict. Returns the task; cancel it to stop listening.
    """

    async def run() -> None:
        th = token_hash(token)
        pubsub = redis.pubsub()
        await pubsub.subscribe(requests_channel(th))
        try:
            async for raw in pubsub.listen():
                if raw is None or raw.get("type") != "message":
                    continue
                data = raw.get("data")
                if not data:
                    continue
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "webhook_request":
                        await handler(msg)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Invalid request message: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(requests_channel(th))
            await pubsub.close()

    return asyncio.create_task(run())


async def wait_for_response(
    redis: Redis,
    token: str,
    request_id: str,
    timeout_seconds: float,
) -> WebhookResponse | None:
    """
    Subscribe to the responses channel and return the WebhookResponse for the given request_id, or None on timeout.
    """
    th = token_hash(token)
    channel = responses_channel(th)
    result_container: list[WebhookResponse | None] = [None]
    done = asyncio.Event()

    async def listen() -> None:
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw in pubsub.listen():
                if raw is None or raw.get("type") != "message":
                    continue
                data = raw.get("data")
                if not data:
                    continue
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "webhook_response" and msg.get("id") == request_id:
                        result_container[0] = WebhookResponse(
                            status_code=int(msg.get("status_code") or 502),
                            headers=msg.get("headers") or {},
                            body=base64.b64decode(msg.get("body_b64") or ""),
                        )
                        done.set()
                        return
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    listen_task = asyncio.create_task(listen())
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
        return result_container[0]
    except asyncio.TimeoutError:
        return None
    finally:
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass
