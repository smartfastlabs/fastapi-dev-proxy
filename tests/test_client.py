from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from unittest.mock import AsyncMock, Mock

from fastapi_dev_proxy.client import _forward_request, _parse_override_paths, run_client


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, Any]]):
        self._messages = list(messages)
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if not self._messages:
            raise RuntimeError("stop")
        return json.dumps(self._messages.pop(0))


class FakeWebSocketContext:
    def __init__(self, websocket: FakeWebSocket):
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeAsyncClientContext:
    def __init__(self, responder):
        self.client = Mock(spec=httpx.AsyncClient)
        self.client.request = AsyncMock(side_effect=responder)

    async def __aenter__(self) -> httpx.AsyncClient:
        return self.client

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
async def test_forward_request_success() -> None:
    async def fake_request(method: str, url: str, headers: dict[str, str], content: bytes) -> httpx.Response:
        assert method == "POST"
        assert url == "http://localhost:8080/webhook?foo=bar"
        assert headers["x-test"] == "ok"
        assert content == b"payload"
        return httpx.Response(status_code=201, headers={"x-return": "ok"}, content=b"ok")

    client = Mock(spec=httpx.AsyncClient)
    client.request = AsyncMock(side_effect=fake_request)
    message = {
        "type": "webhook_request",
        "id": "123",
        "method": "POST",
        "path": "/webhook",
        "query": "foo=bar",
        "headers": {"x-test": "ok", "host": "ignore"},
        "body_b64": "cGF5bG9hZA==",
    }
    response = await _forward_request(client, "http://localhost:8080", message)

    assert response["status_code"] == 201
    assert response["headers"]["x-return"] == "ok"
    assert response["body_b64"] == "b2s="


@pytest.mark.asyncio
async def test_forward_request_error() -> None:
    async def fake_request(*args: Any, **kwargs: Any) -> httpx.Response:
        raise RuntimeError("boom")

    client = Mock(spec=httpx.AsyncClient)
    client.request = AsyncMock(side_effect=fake_request)
    message = {
        "type": "webhook_request",
        "id": "123",
        "method": "POST",
        "path": "/webhook",
        "query": "",
        "headers": {},
        "body_b64": "",
    }
    response = await _forward_request(client, "http://localhost:8080", message)

    assert response["status_code"] == 502
    assert response["headers"] == {}
    assert response["body_b64"]


def test_parse_override_paths() -> None:
    values = ["/a", "/b"]
    csv_values = ["/c,/d", ""]
    assert _parse_override_paths(values, csv_values) == ["/a", "/b", "/c", "/d"]


@pytest.mark.asyncio
async def test_run_client_sends_init_and_response(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket = FakeWebSocket(
        messages=[
            {"type": "relay_ready", "override_paths": ["/webhook"]},
            {
                "type": "webhook_request",
                "id": "1",
                "method": "POST",
                "path": "/webhook",
                "query": "",
                "headers": {"x-test": "ok"},
                "body_b64": "aGVsbG8=",
            }
        ]
    )

    async def fake_request(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status_code=200, headers={}, content=b"ok")

    def fake_connect(*args: Any, **kwargs: Any):
        return FakeWebSocketContext(websocket)

    monkeypatch.setattr("fastapi_dev_proxy.client.connect_websocket", fake_connect)
    monkeypatch.setattr(
        "fastapi_dev_proxy.client.AsyncClient",
        lambda timeout: FakeAsyncClientContext(fake_request),
    )

    with pytest.raises(RuntimeError, match="stop"):
        await run_client(
            relay_url="ws://relay",
            target_base_url="http://localhost:8080",
            override_paths=["webhook"],
        )

    sent_messages = [json.loads(payload) for payload in websocket.sent]
    assert sent_messages[0]["type"] == "relay_init"
    assert sent_messages[0]["override_paths"] == ["/webhook"]
    assert sent_messages[1]["type"] == "webhook_response"
