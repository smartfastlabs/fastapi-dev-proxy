from __future__ import annotations

import asyncio
import socket
from typing import Awaitable, Callable

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from fastapi_dev_proxy.client import run_client
from fastapi_dev_proxy.server import RelayManager


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for(predicate: Callable[[], bool], timeout_seconds: float = 5.0) -> None:
    start = asyncio.get_running_loop().time()
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() - start > timeout_seconds:
            raise TimeoutError("Timed out waiting for condition")
        await asyncio.sleep(0.05)


async def _start_server(app: FastAPI, *, port: int | None = None) -> tuple[uvicorn.Server, Awaitable[None], int]:
    if port is None:
        port = _get_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await _wait_for(lambda: server.started)
    return server, task, port


async def _stop_server(server: uvicorn.Server, task: Awaitable[None]) -> None:
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


def _build_dev_app() -> FastAPI:
    app = FastAPI()

    @app.get("/webhook/ping")
    async def webhook_ping(request: Request) -> PlainTextResponse:
        return PlainTextResponse(f"pong:{request.query_params.get('q')}", status_code=200)

    @app.get("/webhook/query")
    async def webhook_query(request: Request) -> PlainTextResponse:
        multi = ",".join(request.query_params.getlist("multi"))
        return PlainTextResponse(
            f"q={request.query_params.get('q')};multi={multi}",
            status_code=200,
        )

    @app.post("/webhook/test")
    async def webhook_test(request: Request) -> Response:
        body = await request.body()
        return Response(
            content=body,
            status_code=201,
            headers={
                "x-dev": "ok",
                "x-echo": request.headers.get("x-test", ""),
            },
        )

    @app.post("/webhook/error")
    async def webhook_error() -> Response:
        return Response(status_code=500, content=b"dev error")

    @app.post("/webhook/redirect")
    async def webhook_redirect() -> RedirectResponse:
        return RedirectResponse(url="/webhook/redirected", status_code=307)

    @app.post("/webhook/redirected")
    async def webhook_redirected() -> PlainTextResponse:
        return PlainTextResponse("redirected", status_code=200)

    @app.post("/webhook/items/{item_id}")
    async def webhook_item(item_id: str, request: Request) -> Response:
        body = await request.json()
        return Response(
            content=f"{item_id}:{body.get('value')}:{request.query_params.get('q')}".encode("utf-8"),
            status_code=200,
            headers={"x-item": item_id},
        )

    @app.put("/webhook/put/{item_id}")
    async def webhook_put(item_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"item_id": item_id, "value": body.get("value")})

    @app.post("/webhook/json")
    async def webhook_json(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"received": body, "q": request.query_params.get("q")})

    @app.post("/webhook/binary")
    async def webhook_binary(request: Request) -> Response:
        body = await request.body()
        return Response(
            content=body,
            status_code=200,
            headers={"x-binary": "ok"},
            media_type="application/octet-stream",
        )

    @app.post("/webhook/empty")
    async def webhook_empty() -> Response:
        return Response(status_code=204)

    @app.post("/webhook/large")
    async def webhook_large(request: Request) -> PlainTextResponse:
        body = await request.body()
        return PlainTextResponse(str(len(body)), status_code=200)

    return app


def _build_prod_app() -> tuple[FastAPI, RelayManager]:
    app = FastAPI()
    relay = RelayManager(token="secret", enabled=True, timeout_seconds=2.0)
    relay.install(app)

    @app.get("/webhook/ping")
    async def webhook_ping() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.get("/webhook/query")
    async def webhook_query() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.post("/webhook/test")
    async def webhook_test() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.post("/webhook/error")
    async def webhook_error() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.post("/webhook/redirect")
    async def webhook_redirect() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.post("/webhook/items/{item_id}")
    async def webhook_item(item_id: str) -> Response:
        return Response(status_code=418, content=f"fallback:{item_id}".encode("utf-8"))

    @app.put("/webhook/put/{item_id}")
    async def webhook_put(item_id: str) -> Response:
        return Response(status_code=418, content=f"fallback:{item_id}".encode("utf-8"))

    @app.post("/webhook/json")
    async def webhook_json() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.post("/webhook/binary")
    async def webhook_binary() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.post("/webhook/empty")
    async def webhook_empty() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.post("/webhook/large")
    async def webhook_large() -> Response:
        return Response(status_code=418, content=b"fallback")

    @app.post("/webhook/local-only")
    async def webhook_local_only() -> Response:
        return Response(status_code=204)

    return app, relay


@pytest.mark.asyncio
async def test_relay_integration_roundtrip_disconnect_reconnect() -> None:
    dev_app = _build_dev_app()
    prod_app, relay = _build_prod_app()

    dev_server, dev_task, dev_port = await _start_server(dev_app)
    prod_server, prod_task, prod_port = await _start_server(prod_app)
    dev_base_url = f"http://127.0.0.1:{dev_port}"
    prod_base_url = f"http://127.0.0.1:{prod_port}"
    override_paths = [
        "/webhook/ping",
        "/webhook/query",
        "/webhook/test",
        "/webhook/error",
        "/webhook/redirect",
        "/webhook/items/{item_id}",
        "/webhook/put/{item_id}",
        "/webhook/json",
        "/webhook/binary",
        "/webhook/empty",
        "/webhook/large",
    ]
    async with httpx.AsyncClient() as client:
        preconnect = await client.post(f"{prod_base_url}/webhook/test", content=b"pre")
        assert preconnect.status_code == 418
        assert preconnect.content == b"fallback"

    client_task = asyncio.create_task(
        run_client(
            relay_url=f"ws://127.0.0.1:{prod_port}/fastapi-dev-proxy?token=secret",
            target_base_url=dev_base_url,
            override_paths=override_paths,
            timeout=2.0,
            ping_interval=1.0,
            ping_timeout=1.0,
        )
    )

    try:
        await _wait_for(lambda: relay.override_paths() == set(override_paths))

        async with httpx.AsyncClient(follow_redirects=False) as client:
            ping = await client.get(f"{prod_base_url}/webhook/ping?q=hello")
            assert ping.status_code == 200
            assert ping.text == "pong:hello"

            query = await client.get(
                f"{prod_base_url}/webhook/query?q=hello%20world&multi=a&multi=b"
            )
            assert query.status_code == 200
            assert query.text == "q=hello world;multi=a,b"

            response = await client.post(
                f"{prod_base_url}/webhook/test",
                content=b"payload",
                headers={"x-test": "ping"},
            )
            assert response.status_code == 201
            assert response.content == b"payload"
            assert response.headers.get("x-dev") == "ok"
            assert response.headers.get("x-echo") == "ping"

            redirect = await client.post(f"{prod_base_url}/webhook/redirect")
            assert redirect.status_code == 307
            assert redirect.headers.get("location") == "/webhook/redirected"

            error_response = await client.post(f"{prod_base_url}/webhook/error")
            assert error_response.status_code == 500
            assert error_response.content == b"dev error"

            item_response = await client.post(
                f"{prod_base_url}/webhook/items/abc123?q=hello",
                json={"value": "json-body"},
            )
            assert item_response.status_code == 200
            assert item_response.content == b"abc123:json-body:hello"
            assert item_response.headers.get("x-item") == "abc123"

            item_fallback = await client.post(
                f"{prod_base_url}/webhook/items/other?q=hello",
                json={"value": "json-body"},
            )
            assert item_fallback.status_code == 200
            assert item_fallback.content == b"other:json-body:hello"

            put_response = await client.put(
                f"{prod_base_url}/webhook/put/put1",
                json={"value": "put-body"},
            )
            assert put_response.status_code == 200
            assert put_response.json() == {"item_id": "put1", "value": "put-body"}

            json_response = await client.post(
                f"{prod_base_url}/webhook/json?q=hello",
                json={"value": "json-body", "items": [1, 2]},
            )
            assert json_response.status_code == 200
            assert json_response.json() == {
                "received": {"value": "json-body", "items": [1, 2]},
                "q": "hello",
            }
            assert json_response.headers["content-type"].startswith("application/json")

            binary_payload = b"\x00\x01binary"
            binary_response = await client.post(
                f"{prod_base_url}/webhook/binary",
                content=binary_payload,
            )
            assert binary_response.status_code == 200
            assert binary_response.content == binary_payload
            assert binary_response.headers.get("x-binary") == "ok"
            assert binary_response.headers["content-type"].startswith("application/octet-stream")

            empty_response = await client.post(f"{prod_base_url}/webhook/empty")
            assert empty_response.status_code == 204
            assert empty_response.content == b""

            large_payload = b"a" * 10000
            large_response = await client.post(
                f"{prod_base_url}/webhook/large",
                content=large_payload,
            )
            assert large_response.status_code == 200
            assert large_response.text == "10000"

            local_only = await client.post(f"{prod_base_url}/webhook/local-only")
            assert local_only.status_code == 204

        await _stop_server(dev_server, dev_task)

        async with httpx.AsyncClient() as client:
            failure = await client.post(f"{prod_base_url}/webhook/test", content=b"down")
            assert failure.status_code == 502
            assert failure.content

        dev_server, dev_task, _ = await _start_server(dev_app, port=dev_port)

        client_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client_task
        await _wait_for(lambda: not relay.is_connected())

        async with httpx.AsyncClient() as client:
            fallback = await client.post(f"{prod_base_url}/webhook/test", content=b"local")
            assert fallback.status_code == 418
            assert fallback.content == b"fallback"
            fallback_error = await client.post(f"{prod_base_url}/webhook/error")
            assert fallback_error.status_code == 418
            assert fallback_error.content == b"fallback"
            fallback_redirect = await client.post(f"{prod_base_url}/webhook/redirect")
            assert fallback_redirect.status_code == 418
            assert fallback_redirect.content == b"fallback"
            fallback_item = await client.post(
                f"{prod_base_url}/webhook/items/abc123?q=hello",
                json={"value": "json-body"},
            )
            assert fallback_item.status_code == 418
            assert fallback_item.content == b"fallback:abc123"
            fallback_item_other = await client.post(
                f"{prod_base_url}/webhook/items/other?q=hello",
                json={"value": "json-body"},
            )
            assert fallback_item_other.status_code == 418
            assert fallback_item_other.content == b"fallback:other"
            fallback_ping = await client.get(f"{prod_base_url}/webhook/ping?q=hello")
            assert fallback_ping.status_code == 418
            assert fallback_ping.content == b"fallback"
            fallback_query = await client.get(f"{prod_base_url}/webhook/query?q=hello&multi=a&multi=b")
            assert fallback_query.status_code == 418
            assert fallback_query.content == b"fallback"
            fallback_put = await client.put(
                f"{prod_base_url}/webhook/put/put1",
                json={"value": "put-body"},
            )
            assert fallback_put.status_code == 418
            assert fallback_put.content == b"fallback:put1"
            fallback_json = await client.post(
                f"{prod_base_url}/webhook/json?q=hello",
                json={"value": "json-body", "items": [1, 2]},
            )
            assert fallback_json.status_code == 418
            assert fallback_json.content == b"fallback"
            fallback_binary = await client.post(
                f"{prod_base_url}/webhook/binary",
                content=b"\x00\x01binary",
            )
            assert fallback_binary.status_code == 418
            assert fallback_binary.content == b"fallback"
            fallback_empty = await client.post(f"{prod_base_url}/webhook/empty")
            assert fallback_empty.status_code == 418
            assert fallback_empty.content == b"fallback"
            fallback_large = await client.post(
                f"{prod_base_url}/webhook/large",
                content=b"a" * 10000,
            )
            assert fallback_large.status_code == 418
            assert fallback_large.content == b"fallback"
            fallback_local_only = await client.post(f"{prod_base_url}/webhook/local-only")
            assert fallback_local_only.status_code == 204

        client_task = asyncio.create_task(
            run_client(
                relay_url=f"ws://127.0.0.1:{prod_port}/fastapi-dev-proxy?token=secret",
                target_base_url=dev_base_url,
                override_paths=override_paths,
                timeout=2.0,
                ping_interval=1.0,
                ping_timeout=1.0,
            )
        )
        await _wait_for(lambda: relay.override_paths() == set(override_paths))

        async with httpx.AsyncClient() as client:
            reconnected = await client.post(f"{prod_base_url}/webhook/test", content=b"again")
            assert reconnected.status_code == 201
            assert reconnected.content == b"again"
            reconnected_item = await client.post(
                f"{prod_base_url}/webhook/items/abc123?q=hello",
                json={"value": "json-body"},
            )
            assert reconnected_item.status_code == 200
            assert reconnected_item.content == b"abc123:json-body:hello"
            reconnected_item_other = await client.post(
                f"{prod_base_url}/webhook/items/other?q=hello",
                json={"value": "json-body"},
            )
            assert reconnected_item_other.status_code == 200
            assert reconnected_item_other.content == b"other:json-body:hello"
            reconnected_ping = await client.get(f"{prod_base_url}/webhook/ping?q=hello")
            assert reconnected_ping.status_code == 200
            assert reconnected_ping.text == "pong:hello"
            reconnected_query = await client.get(
                f"{prod_base_url}/webhook/query?q=hello%20world&multi=a&multi=b"
            )
            assert reconnected_query.status_code == 200
            assert reconnected_query.text == "q=hello world;multi=a,b"
            reconnected_put = await client.put(
                f"{prod_base_url}/webhook/put/put1",
                json={"value": "put-body"},
            )
            assert reconnected_put.status_code == 200
            assert reconnected_put.json() == {"item_id": "put1", "value": "put-body"}
            reconnected_json = await client.post(
                f"{prod_base_url}/webhook/json?q=hello",
                json={"value": "json-body", "items": [1, 2]},
            )
            assert reconnected_json.status_code == 200
            assert reconnected_json.json() == {
                "received": {"value": "json-body", "items": [1, 2]},
                "q": "hello",
            }
            reconnected_binary = await client.post(
                f"{prod_base_url}/webhook/binary",
                content=b"\x00\x01binary",
            )
            assert reconnected_binary.status_code == 200
            assert reconnected_binary.content == b"\x00\x01binary"
            reconnected_empty = await client.post(f"{prod_base_url}/webhook/empty")
            assert reconnected_empty.status_code == 204
            assert reconnected_empty.content == b""
            reconnected_large = await client.post(
                f"{prod_base_url}/webhook/large",
                content=b"a" * 10000,
            )
            assert reconnected_large.status_code == 200
            assert reconnected_large.text == "10000"
            reconnected_local_only = await client.post(f"{prod_base_url}/webhook/local-only")
            assert reconnected_local_only.status_code == 204
    finally:
        if not client_task.done():
            client_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await client_task
        await _stop_server(dev_server, dev_task)
        await _stop_server(prod_server, prod_task)
