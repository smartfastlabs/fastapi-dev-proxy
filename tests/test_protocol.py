from __future__ import annotations

import base64

import fastapi_dev_proxy
from fastapi_dev_proxy.protocol import (
    WebhookResponse,
    decode_body,
    encode_body,
    filter_headers,
    normalize_path,
    normalize_paths,
)


def test_encode_decode_body() -> None:
    payload = b"hello"
    encoded = encode_body(payload)
    assert encoded == base64.b64encode(payload).decode("ascii")
    assert decode_body(encoded) == payload
    assert decode_body("") == b""
    assert decode_body(None) == b""


def test_filter_headers_excludes_defaults() -> None:
    headers = {
        "host": "example.com",
        "Content-Length": "123",
        "X-Test": "ok",
        "Connection": "close",
        "x-multi": ["a", "b"],
    }
    filtered = filter_headers(headers)
    assert "host" not in {key.lower() for key in filtered}
    assert "content-length" not in {key.lower() for key in filtered}
    assert "connection" not in {key.lower() for key in filtered}
    assert filtered["X-Test"] == "ok"
    assert filtered["x-multi"] == "b"


def test_normalize_paths() -> None:
    assert normalize_path("") == "/"
    assert normalize_path("webhook/sms") == "/webhook/sms"
    assert normalize_path("/webhook/sms/") == "/webhook/sms"
    assert normalize_paths(["/a", "b", "/c/"]) == ["/a", "/b", "/c"]


def test_webhook_response_to_message() -> None:
    response = WebhookResponse(status_code=201, headers={"x": "y"}, body=b"ok")
    message = response.to_message("request-id")
    assert message["type"] == "webhook_response"
    assert message["id"] == "request-id"
    assert message["status_code"] == 201
    assert message["headers"] == {"x": "y"}
    assert base64.b64decode(message["body_b64"]) == b"ok"


def test_package_exports() -> None:
    assert fastapi_dev_proxy.RelayManager is not None
    assert fastapi_dev_proxy.run_client is not None
