"""Server-side dev proxy manager and middleware."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import DEFAULT_TIMEOUT_SECONDS
from .protocol import (
    RelayInitMessage,
    RelayReadyMessage,
    WebhookRequestMessage,
    WebhookResponse,
    WebhookResponseMessage,
    filter_headers,
    match_path,
    normalize_path,
    normalize_paths,
)
from .redis_constants import HEARTBEAT_INTERVAL_SECONDS, OWNER_KEY_TTL_SECONDS
from .store import InMemoryRelayStore, RelayStore

logger = logging.getLogger(__name__)


class RelayProxyMiddleware:
    """ASGI middleware that proxies matched HTTP requests to the dev relay."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        relay: RelayManager,
        path_prefix: str = "/fastapi-dev-proxy",
    ) -> None:
        self._app = app
        self._relay = relay
        prefix = normalize_path(path_prefix.rstrip("/") or "/")
        if prefix == "/":
            self._reserved_paths = {"/websocket", "/enable", "/disable"}
        else:
            self._reserved_paths = {
                f"{prefix}/websocket",
                f"{prefix}/enable",
                f"{prefix}/disable",
            }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path") or "/"
        if normalize_path(path) in self._reserved_paths:
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive)
        response = await self._relay.proxy_request(request)
        if response is not None:
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class RelayManager:
    """Manage a single dev proxy websocket connection.

    With redis_url set, works across multiple FastAPI instances behind a load balancer:
    the dev client can connect to any instance and webhook requests to any instance
    are forwarded to that client. Without redis_url, behavior is single-instance only.
    """

    def __init__(
        self,
        *,
        app: FastAPI | None = None,
        path_prefix: str = "/fastapi-dev-proxy",
        token: str,
        enabled: bool = True,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        redis_url: str | None = None,
        instance_id: str | None = None,
    ) -> None:
        if not token:
            raise ValueError("RelayManager token is required (non-empty).")
        self._token = token
        self._enabled = enabled
        self._timeout_seconds = timeout_seconds
        self._websocket: WebSocket | None = None
        self._override_paths: set[str] = set()
        self._override_paths_cache: set[str] = set()
        self._pending: dict[str, asyncio.Future[WebhookResponse]] = {}
        self._lock = asyncio.Lock()
        self._redis = None
        self._instance_id: str | None = None
        self._last_known_owner: str | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._subscribe_task: asyncio.Task[None] | None = None

        if redis_url:
            try:
                from .redis_bus import RedisStore, create_redis
            except ImportError as e:
                raise ValueError(
                    "redis_url is set but redis is not installed. "
                    "Install with: pip install fastapi-dev-proxy[redis]"
                ) from e
            self._redis = create_redis(redis_url)
            self._instance_id = instance_id or uuid4().hex
            self._store: RelayStore = RedisStore(
                self._redis, token, self._instance_id, enabled=enabled
            )
        else:
            self._store = InMemoryRelayStore(enabled=enabled)

        if app is not None:
            self._install(app, path_prefix=path_prefix)

    def is_enabled(self) -> bool:
        return self._enabled

    def is_connected(self) -> bool:
        if self._websocket is not None:
            return True
        if self._redis is not None and self._last_known_owner is not None:
            return True
        return False

    def override_paths(self) -> set[str]:
        if self._redis is None:
            return set(self._override_paths)
        return set(self._override_paths_cache)

    def install(
        self,
        app: FastAPI,
        *,
        path_prefix: str = "/fastapi-dev-proxy",
    ) -> None:
        """Register the dev proxy endpoints and HTTP proxy middleware on an existing instance.

        Endpoints:

        - `{path_prefix}/websocket` (websocket) connects the dev client
        - `{path_prefix}/enable` (HTTP) enables forwarding
        - `{path_prefix}/disable` (HTTP) disables forwarding
        """
        self._install(app, path_prefix=path_prefix)

    def _install(
        self,
        app: FastAPI,
        *,
        path_prefix: str = "/fastapi-dev-proxy",
    ) -> None:
        """Register the dev proxy endpoints and HTTP proxy middleware."""
        prefix = normalize_path(path_prefix.rstrip("/") or "/")
        websocket_path = f"{prefix}/websocket" if prefix != "/" else "/websocket"
        enable_path = f"{prefix}/enable" if prefix != "/" else "/enable"
        disable_path = f"{prefix}/disable" if prefix != "/" else "/disable"

        self._register_websocket(app, websocket_path)
        self._register_toggle_routes(app, enable_path=enable_path, disable_path=disable_path)
        app.add_middleware(
            RelayProxyMiddleware,
            relay=self,
            path_prefix=prefix,
        )

    def _register_websocket(self, app: FastAPI, websocket_path: str) -> None:
        @app.websocket(websocket_path)
        async def dev_proxy_ws(ws: WebSocket) -> None:
            await self.handle_connection(ws)

    def _register_toggle_routes(self, app: FastAPI, *, enable_path: str, disable_path: str) -> None:
        @app.post(enable_path)
        async def dev_proxy_enable(request: Request) -> dict[str, bool]:
            if not self._is_token_valid(request.query_params.get("token")):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
            await self.set_enabled(True)
            return {"enabled": True}

        @app.post(disable_path)
        async def dev_proxy_disable(request: Request) -> dict[str, bool]:
            if not self._is_token_valid(request.query_params.get("token")):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
            await self.set_enabled(False)
            return {"enabled": False}

    def _is_token_valid(self, token: str | None) -> bool:
        return bool(token) and token == self._token

    async def set_enabled(self, enabled: bool) -> None:
        """Enable/disable forwarding.

        If disabling while a dev client is connected, closes the websocket and clears override paths.
        """
        async with self._lock:
            self._enabled = enabled
            await self._store.set_enabled(enabled)
            if enabled:
                return

            websocket = self._websocket
            if websocket is None:
                self._override_paths = set()
                self._override_paths_cache = set()
                await self._store.set_override_paths([])
                if self._redis is not None:
                    await self._store.set_owner(None)
                return

            try:
                await websocket.close(code=status.WS_1001_GOING_AWAY)
            except Exception:
                logger.debug("Failed to close dev proxy websocket on disable")
            await self._clear_connection(websocket)


    async def handle_connection(self, websocket: WebSocket) -> None:
        """Handle websocket lifecycle and response messages."""

        if not self._enabled:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        if not self._is_token_valid(websocket.query_params.get("token")):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.accept()

        async with self._lock:
            await self._set_connection(websocket)

        if self._redis is not None:
            await self._store.set_owner(self._instance_id, ttl_seconds=OWNER_KEY_TTL_SECONDS)
            self._last_known_owner = self._instance_id
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._subscribe_task = await self._start_redis_subscribe(websocket)

        try:
            init_message = cast(RelayInitMessage, await websocket.receive_json())
            if init_message.get("type") != "relay_init":
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            override_paths = normalize_paths(init_message.get("override_paths") or [])
            self._override_paths = set(override_paths)
            self._override_paths_cache = set(override_paths)
            await self._store.set_override_paths(override_paths)
            ready_message: RelayReadyMessage = {
                "type": "relay_ready",
                "override_paths": override_paths,
            }
            await websocket.send_json(ready_message)

            while True:
                message = cast(WebhookResponseMessage, await websocket.receive_json())
                await self._handle_message(message)
        except WebSocketDisconnect:
            logger.info("Dev proxy disconnected")
        except Exception as exc:
            logger.exception("Dev proxy error: %s", exc)
        finally:
            async with self._lock:
                await self._clear_connection(websocket)

    async def proxy_request(self, request: Request) -> Response | None:
        """Proxy a webhook request to the dev proxy if enabled and matched."""

        if not await self._should_proxy(request):
            return None

        websocket = self._websocket
        if websocket is not None:
            return await self._proxy_request_local(request, websocket)

        if self._redis is not None:
            return await self._proxy_request_via_redis(request)

        return None

    async def _proxy_request_local(
        self, request: Request, websocket: WebSocket
    ) -> Response | None:
        request_id = str(uuid4())
        future: asyncio.Future[WebhookResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future

        try:
            body = await request.body()
            payload: WebhookRequestMessage = {
                "type": "webhook_request",
                "id": request_id,
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "headers": filter_headers(dict(request.headers)),
                "body_b64": base64.b64encode(body).decode("ascii"),
            }

            await websocket.send_json(payload)
            relay_response = await asyncio.wait_for(
                future, timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning("Dev proxy timed out (id=%s)", request_id)
            return Response(status_code=status.HTTP_504_GATEWAY_TIMEOUT)
        except Exception as exc:
            logger.exception("Dev proxy failed (id=%s): %s", request_id, exc)
            return Response(status_code=status.HTTP_502_BAD_GATEWAY)
        finally:
            self._pending.pop(request_id, None)

        return Response(
            content=relay_response.body,
            status_code=relay_response.status_code,
            headers=relay_response.headers,
        )

    async def _proxy_request_via_redis(self, request: Request) -> Response | None:
        from .redis_bus import publish_request, wait_for_response

        request_id = str(uuid4())
        body = await request.body()
        payload: WebhookRequestMessage = {
            "type": "webhook_request",
            "id": request_id,
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "headers": filter_headers(dict(request.headers)),
            "body_b64": base64.b64encode(body).decode("ascii"),
        }
        await publish_request(self._redis, self._token, payload)
        relay_response = await wait_for_response(
            self._redis, self._token, request_id, self._timeout_seconds
        )
        if relay_response is None:
            logger.warning("Dev proxy timed out (id=%s)", request_id)
            return Response(status_code=status.HTTP_504_GATEWAY_TIMEOUT)
        return Response(
            content=relay_response.body,
            status_code=relay_response.status_code,
            headers=relay_response.headers,
        )

    async def _should_proxy(self, request: Request) -> bool:
        enabled = await self._store.get_enabled()
        self._enabled = enabled
        if not enabled:
            return False
        if self._redis is not None:
            owner = await self._store.get_owner()
            self._last_known_owner = owner
            if owner is None:
                return False
            paths = await self._store.get_override_paths()
            self._override_paths_cache = set(paths)
        else:
            if self._websocket is None:
                return False
            paths = self._override_paths
        path = normalize_path(request.url.path)
        return any(match_path(path, pattern) for pattern in paths)

    async def _heartbeat_loop(self) -> None:
        if not hasattr(self._store, "renew_owner_ttl"):
            return
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                await self._store.renew_owner_ttl()
        except asyncio.CancelledError:
            pass

    async def _start_redis_subscribe(self, websocket: WebSocket) -> asyncio.Task[None]:
        from .redis_bus import publish_response, subscribe_requests

        async def on_request(msg: WebhookRequestMessage) -> None:
            request_id = msg.get("id")
            if not request_id:
                return
            future: asyncio.Future[WebhookResponse] = (
                asyncio.get_running_loop().create_future()
            )
            self._pending[request_id] = future
            try:
                await websocket.send_json(msg)
                response = await asyncio.wait_for(
                    future, timeout=self._timeout_seconds
                )
                await publish_response(
                    self._redis, self._token, response.to_message(request_id)
                )
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning("Redis request failed id=%s: %s", request_id, exc)
            finally:
                self._pending.pop(request_id, None)

        return await subscribe_requests(self._redis, self._token, on_request)

    async def _set_connection(self, websocket: WebSocket) -> None:
        if self._websocket is not None and self._websocket is not websocket:
            try:
                await self._websocket.close(code=status.WS_1012_SERVICE_RESTART)
            except Exception:
                logger.debug("Failed to close previous dev proxy connection")
        self._websocket = websocket
        self._override_paths = set()
        self._override_paths_cache = set()

    async def _clear_connection(self, websocket: WebSocket) -> None:
        if self._websocket is websocket:
            self._websocket = None
            self._override_paths = set()
            self._override_paths_cache = set()
            if self._redis is not None:
                await self._store.set_owner(None)
                self._last_known_owner = None
                if self._heartbeat_task is not None:
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass
                    self._heartbeat_task = None
                if self._subscribe_task is not None:
                    self._subscribe_task.cancel()
                    try:
                        await self._subscribe_task
                    except asyncio.CancelledError:
                        pass
                    self._subscribe_task = None
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(RuntimeError("Dev proxy disconnected"))
            self._pending.pop(request_id, None)

    async def _handle_message(self, message: WebhookResponseMessage) -> None:
        if message.get("type") != "webhook_response":
            return
        request_id = message.get("id")
        if not request_id:
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        body_b64 = message.get("body_b64") or ""
        response = WebhookResponse(
            status_code=int(message.get("status_code") or 502),
            headers=cast(dict[str, str], message.get("headers") or {}),
            body=base64.b64decode(body_b64),
        )
        future.set_result(response)
