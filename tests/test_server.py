from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.websockets import WebSocketDisconnect

from fastapi_dev_proxy.server import RelayManager, RelayProxyMiddleware


class FakeWebSocket:
    def __init__(
        self,
        state: dict[str, Any],
        *,
        query_params: dict[str, str] | None = None,
        explode_on_receive: bool = False,
        close_raises: Exception | None = None,
    ):
        self._state = state
        self.query_params = query_params or {}
        self._explode_on_receive = explode_on_receive
        self._close_raises = close_raises

    async def accept(self) -> None:
        self._state["accepted"] = True

    async def close(self, code: int) -> None:
        if self._close_raises is not None:
            raise self._close_raises
        self._state["closed"] = True
        self._state["close_code"] = code

    async def receive_json(self) -> dict[str, Any]:
        if self._explode_on_receive:
            raise ValueError("boom")
        if not self._state["messages"]:
            raise WebSocketDisconnect()
        return self._state["messages"].pop(0)

    async def send_json(self, message: dict[str, Any]) -> None:
        self._state["sent"].append(message)

    @property
    def sent(self) -> list[dict[str, Any]]:
        return list(self._state["sent"])


def _make_websocket(
    *,
    messages: list[dict[str, Any]],
    query_params: dict[str, str] | None = None,
    explode_on_receive: bool = False,
    close_raises: Exception | None = None,
) -> tuple[Any, dict[str, Any]]:
    state = {
        "messages": list(messages),
        "sent": [],
        "accepted": False,
        "closed": False,
        "close_code": None,
    }
    websocket = FakeWebSocket(
        state,
        query_params=query_params,
        explode_on_receive=explode_on_receive,
        close_raises=close_raises,
    )
    return websocket, state


def _make_request(path: str, body: bytes = b"payload") -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3"},
        "method": "POST",
        "path": path,
        "query_string": b"foo=bar",
        "headers": [(b"x-test", b"ok"), (b"host", b"example.com")],
    }
    return Request(scope, receive)


@pytest.mark.parametrize(
    ("override_paths", "path", "expected"),
    [
        (["/webhook/items/{item_id}"], "/webhook/items/abc", True),
        (["/webhook/items/<uuid>"], "/webhook/items/abc", True),
        (["/webhook/items/*"], "/webhook/items/abc", True),
        (["/webhook/items/*"], "/webhook/items/abc/def", False),
        (["/webhook/items/{item_id}/sub"], "/webhook/items/abc/sub", True),
        (["/webhook/items/{item_id}"], "/webhook/items/abc/extra", False),
        (["/webhook/items/{item_id}"], "/webhook/items", False),
        (["/webhook/*"], "/webhook/items", True),
        (["/webhook/*"], "/webhook/items/abc", False),
        (["/webhook/**"], "/webhook/items/abc", True),
        (["/webhook/**"], "/webhook", True),
    ],
)
@pytest.mark.asyncio
async def test_should_proxy_pattern_matching(
    override_paths: list[str],
    path: str,
    expected: bool,
) -> None:
    relay = RelayManager(token="secret", enabled=True)
    relay._override_paths = set(override_paths)
    relay._websocket = object()
    request = _make_request(path)
    assert (await relay._should_proxy(request)) is expected


@pytest.mark.asyncio
async def test_handle_connection_sets_override_paths() -> None:
    relay = RelayManager(token="secret", enabled=True)
    websocket, state = _make_websocket(
        messages=[
            {"type": "relay_init", "override_paths": ["/webhook/sms", "/webhook/email"]},
            {"type": "webhook_response", "id": "1", "status_code": 200, "headers": {}, "body_b64": ""},
        ],
        query_params={"token": "secret"},
    )

    await relay.handle_connection(websocket)

    assert state["accepted"] is True
    assert state["sent"][0]["type"] == "relay_ready"
    assert state["sent"][0]["override_paths"] == ["/webhook/sms", "/webhook/email"]
    assert relay.override_paths() == set()


@pytest.mark.asyncio
async def test_handle_connection_disabled() -> None:
    relay = RelayManager(token="secret", enabled=False)
    websocket, state = _make_websocket(messages=[])

    await relay.handle_connection(websocket)

    assert state["closed"] is True
    assert state["close_code"] == 1008


@pytest.mark.asyncio
async def test_handle_connection_rejects_bad_token() -> None:
    relay = RelayManager(token="secret", enabled=True)
    websocket, state = _make_websocket(messages=[], query_params={"token": "wrong"})

    await relay.handle_connection(websocket)

    assert state["closed"] is True
    assert state["close_code"] == 1008


def test_install_registers_default_endpoints() -> None:
    app = FastAPI()
    relay = RelayManager(token="secret", enabled=True)
    relay.install(app)

    paths = {getattr(route, "path", None) for route in app.router.routes}
    assert "/fastapi-dev-proxy/websocket" in paths
    assert "/fastapi-dev-proxy/enable" in paths
    assert "/fastapi-dev-proxy/disable" in paths


@pytest.mark.asyncio
async def test_enable_disable_endpoints_require_token_when_configured() -> None:
    app = FastAPI()
    relay = RelayManager(token="secret", enabled=False)
    relay.install(app, path_prefix="/fastapi-dev-proxy")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/fastapi-dev-proxy/enable")
        assert missing.status_code == 403

        wrong = await client.post("/fastapi-dev-proxy/enable?token=wrong")
        assert wrong.status_code == 403

        ok = await client.post("/fastapi-dev-proxy/enable?token=secret")
        assert ok.status_code == 200
        assert ok.json() == {"enabled": True}

        ok_disable = await client.post("/fastapi-dev-proxy/disable?token=secret")
        assert ok_disable.status_code == 200
        assert ok_disable.json() == {"enabled": False}

    assert relay.is_enabled() is False


@pytest.mark.asyncio
async def test_disable_closes_active_websocket_and_clears_override_paths() -> None:
    app = FastAPI()
    relay = RelayManager(token="secret", enabled=True)
    relay.install(app, path_prefix="/fastapi-dev-proxy")

    websocket, state = _make_websocket(messages=[])
    await relay._set_connection(websocket)
    relay._override_paths = {"/webhook/sms"}
    assert relay.is_connected() is True

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/fastapi-dev-proxy/disable?token=secret")
        assert res.status_code == 200
        assert res.json() == {"enabled": False}

    assert relay.is_enabled() is False
    assert relay.is_connected() is False
    assert relay.override_paths() == set()
    assert state["closed"] is True


@pytest.mark.asyncio
async def test_handle_connection_bad_init_message() -> None:
    relay = RelayManager(token="secret", enabled=True)
    websocket, state = _make_websocket(messages=[{"type": "unknown"}])

    await relay.handle_connection(websocket)

    assert state["closed"] is True
    assert state["close_code"] == 1008


@pytest.mark.asyncio
async def test_handle_connection_exception_clears() -> None:
    relay = RelayManager(token="secret", enabled=True)
    websocket, _ = _make_websocket(messages=[], explode_on_receive=True)

    await relay.handle_connection(websocket)

    assert relay.is_connected() is False


@pytest.mark.asyncio
async def test_proxy_request_no_connection() -> None:
    relay = RelayManager(token="secret", enabled=True)
    request = _make_request("/webhook/sms")
    assert await relay.proxy_request(request) is None
    assert relay.is_enabled() is True
    assert relay.is_connected() is False


@pytest.mark.asyncio
async def test_proxy_request_non_override_path() -> None:
    relay = RelayManager(token="secret", enabled=True)
    websocket, _ = _make_websocket(messages=[])
    await relay._set_connection(websocket)
    relay._override_paths = {"/webhook/sms"}

    request = _make_request("/webhook/email")
    assert await relay.proxy_request(request) is None
    assert relay.override_paths() == {"/webhook/sms"}


@pytest.mark.asyncio
async def test_proxy_request_disabled() -> None:
    relay = RelayManager(token="secret", enabled=False)
    websocket, _ = _make_websocket(messages=[])
    await relay._set_connection(websocket)
    relay._override_paths = {"/webhook/sms"}

    request = _make_request("/webhook/sms")
    assert await relay.proxy_request(request) is None


@pytest.mark.asyncio
async def test_proxy_request_round_trip() -> None:
    relay = RelayManager(token="secret", enabled=True, timeout_seconds=1)
    websocket, state = _make_websocket(messages=[])
    await relay._set_connection(websocket)
    relay._override_paths = {"/webhook/sms"}

    request = _make_request("/webhook/sms", body=b"hello")

    task = asyncio.create_task(relay.proxy_request(request))
    for _ in range(5):
        if websocket.sent:
            break
        await asyncio.sleep(0)

    assert state["sent"]
    sent_payload = state["sent"][0]
    response_message = {
        "type": "webhook_response",
        "id": sent_payload["id"],
        "status_code": 202,
        "headers": {"x-from": "relay"},
        "body_b64": sent_payload["body_b64"],
    }
    await relay._handle_message(response_message)

    response = await task
    assert response is not None
    assert response.status_code == 202
    assert response.headers["x-from"] == "relay"
    assert response.body == b"hello"


@pytest.mark.asyncio
async def test_handle_message_ignores_invalid() -> None:
    relay = RelayManager(token="secret", enabled=True)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    relay._pending["ok"] = future

    await relay._handle_message({"type": "other"})
    await relay._handle_message({"type": "webhook_response"})
    await relay._handle_message({"type": "webhook_response", "id": "missing"})

    future.set_result(None)
    await relay._handle_message(
        {"type": "webhook_response", "id": "ok", "status_code": 200, "headers": {}, "body_b64": ""}
    )

    assert future.done() is True


@pytest.mark.asyncio
async def test_handle_message_defaults_status_code() -> None:
    relay = RelayManager(token="secret", enabled=True)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    relay._pending["req"] = future

    await relay._handle_message(
        {"type": "webhook_response", "id": "req", "headers": {"x": "y"}, "body_b64": ""}
    )

    response = future.result()
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_proxy_request_timeout() -> None:
    relay = RelayManager(token="secret", enabled=True, timeout_seconds=0)
    websocket, _ = _make_websocket(messages=[])
    await relay._set_connection(websocket)
    relay._override_paths = {"/webhook/sms"}

    request = _make_request("/webhook/sms")
    response = await relay.proxy_request(request)
    assert response is not None
    assert response.status_code == 504


@pytest.mark.asyncio
async def test_clear_connection_fails_pending() -> None:
    relay = RelayManager(token="secret", enabled=True)
    websocket, _ = _make_websocket(messages=[])
    await relay._set_connection(websocket)

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    relay._pending["test"] = future
    await relay._clear_connection(websocket)

    assert future.done() is True
    with pytest.raises(RuntimeError):
        future.result()


@pytest.mark.asyncio
async def test_clear_connection_noop_for_other_socket() -> None:
    relay = RelayManager(token="secret", enabled=True)
    websocket, _ = _make_websocket(messages=[])
    other, _ = _make_websocket(messages=[])
    await relay._set_connection(websocket)

    await relay._clear_connection(other)

    assert relay.is_connected() is True


@pytest.mark.asyncio
async def test_set_connection_replaces_existing() -> None:
    relay = RelayManager(token="secret", enabled=True)
    first, first_state = _make_websocket(messages=[])
    second, _ = _make_websocket(messages=[])

    await relay._set_connection(first)
    await relay._set_connection(second)

    assert first_state["closed"] is True
    assert relay.is_connected() is True


@pytest.mark.asyncio
async def test_set_connection_close_failure() -> None:
    relay = RelayManager(token="secret", enabled=True)
    first, _ = _make_websocket(messages=[], close_raises=RuntimeError("close failed"))
    second, _ = _make_websocket(messages=[])

    await relay._set_connection(first)
    await relay._set_connection(second)

    assert relay.is_connected() is True


@pytest.mark.asyncio
async def test_middleware_bypasses_non_http() -> None:
    relay = RelayManager(token="secret", enabled=True)
    called: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "websocket"
        called.append("app")

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: dict[str, Any]) -> None:
        pass

    middleware = RelayProxyMiddleware(app, relay=relay, path_prefix="/fastapi-dev-proxy")
    scope = {"type": "websocket", "path": "/other"}
    await middleware(scope, receive, send)
    assert called == ["app"]


@pytest.mark.asyncio
async def test_middleware_bypasses_reserved_paths() -> None:
    relay = RelayManager(token="secret", enabled=True)
    called: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        called.append("app")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        pass

    middleware = RelayProxyMiddleware(app, relay=relay, path_prefix="/fastapi-dev-proxy")
    for path in (
        "/fastapi-dev-proxy/websocket",
        "/fastapi-dev-proxy/enable",
        "/fastapi-dev-proxy/disable",
    ):
        called.clear()
        scope = {"type": "http", "path": path, "method": "GET", "asgi": {"spec_version": "2.3"}}
        scope.setdefault("query_string", b"")
        scope.setdefault("headers", [])
        await middleware(scope, receive, send)
        assert called == ["app"]


@pytest.mark.asyncio
async def test_middleware_proxies_when_should_proxy() -> None:
    relay = RelayManager(token="secret", enabled=True)
    websocket, _ = _make_websocket(messages=[])
    await relay._set_connection(websocket)
    relay._override_paths = {"/webhook/sms"}
    app_called: list[str] = []
    sent_messages: list[dict[str, Any]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        app_called.append("app")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent_messages.append(message)

    middleware = RelayProxyMiddleware(app, relay=relay, path_prefix="/fastapi-dev-proxy")
    scope = {"type": "http", "path": "/webhook/sms", "method": "POST", "asgi": {"spec_version": "2.3"}}
    scope.setdefault("query_string", b"")
    scope.setdefault("headers", [])

    task = asyncio.create_task(middleware(scope, receive, send))
    for _ in range(5):
        if websocket.sent:
            break
        await asyncio.sleep(0)
    assert websocket.sent
    request_id = websocket.sent[0]["id"]
    await relay._handle_message({
        "type": "webhook_response",
        "id": request_id,
        "status_code": 200,
        "headers": {},
        "body_b64": "",
    })
    await task
    assert not app_called
    assert any(m.get("status") == 200 for m in sent_messages if isinstance(m, dict))


@pytest.mark.asyncio
async def test_middleware_passes_through_when_not_proxied() -> None:
    relay = RelayManager(token="secret", enabled=True)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = RelayProxyMiddleware(app, relay=relay, path_prefix="/fastapi-dev-proxy")
    scope = {"type": "http", "path": "/webhook/other", "method": "POST", "asgi": {"spec_version": "2.3"}}
    scope.setdefault("query_string", b"")
    scope.setdefault("headers", [])

    await middleware(scope, receive, send)
    assert any(m.get("status") == 404 for m in sent if isinstance(m, dict))
