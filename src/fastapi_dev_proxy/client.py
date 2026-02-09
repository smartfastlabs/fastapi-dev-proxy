"""Client-side dev proxy runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Iterable

from httpx import AsyncClient
from websockets import connect as connect_websocket

from .config import DEFAULT_PING_INTERVAL_SECONDS, DEFAULT_PING_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS
from .protocol import (
    RelayInitMessage,
    WebhookRequestMessage,
    WebhookResponseMessage,
    decode_body,
    encode_body,
    filter_headers,
    normalize_paths,
)

logger = logging.getLogger(__name__)


async def _forward_request(
    client: AsyncClient,
    target_base_url: str,
    message: WebhookRequestMessage,
) -> WebhookResponseMessage:
    request_id = message.get("id")
    path = message.get("path") or "/"
    query = message.get("query") or ""
    method = message.get("method") or "POST"
    headers = filter_headers(message.get("headers") or {})
    body = decode_body(message.get("body_b64"))

    url = f"{target_base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{query}"

    try:
        logger.info("Inbound webhook %s %s (id=%s)", method, url, request_id)
        response = await client.request(method, url, headers=headers, content=body)
        response_headers = filter_headers(dict(response.headers))
        logger.info(
            "Webhook response %s (id=%s)",
            response.status_code,
            request_id,
        )
        return {
            "type": "webhook_response",
            "id": request_id or "",
            "status_code": response.status_code,
            "headers": response_headers,
            "body_b64": encode_body(response.content),
        }
    except Exception as exc:
        logger.exception("Failed to forward webhook %s: %s", request_id, exc)
        return {
            "type": "webhook_response",
            "id": request_id or "",
            "status_code": 502,
            "headers": {},
            "body_b64": encode_body(str(exc).encode("utf-8")),
        }


async def run_client(
    *,
    relay_url: str,
    target_base_url: str,
    override_paths: Iterable[str],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ping_interval: float = DEFAULT_PING_INTERVAL_SECONDS,
    ping_timeout: float = DEFAULT_PING_TIMEOUT_SECONDS,
) -> None:
    """Connect to the dev proxy and forward webhook requests."""

    normalized_paths = normalize_paths(list(override_paths))
    init_message: RelayInitMessage = {
        "type": "relay_init",
        "override_paths": normalized_paths,
    }

    async with connect_websocket(
        relay_url,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        max_size=None,
    ) as websocket:
        logger.info("Connected to dev proxy: %s", relay_url)
        await websocket.send(json.dumps(init_message))
        async with AsyncClient(timeout=timeout) as client:
            while True:
                raw_message = await websocket.recv()
                message = json.loads(raw_message)
                if message.get("type") != "webhook_request":
                    continue
                response = await _forward_request(
                    client,
                    target_base_url,
                    message,
                )
                await websocket.send(json.dumps(response))


def _parse_override_paths(values: list[str], csv_values: list[str]) -> list[str]:
    paths = list(values)
    for value in csv_values:
        if value:
            paths.extend(part.strip() for part in value.split(",") if part.strip())
    return paths


def _build_parser() -> argparse.ArgumentParser:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Run dev proxy client.")
    parser.add_argument("--relay-url", required=True, help="Relay websocket URL.")
    parser.add_argument(
        "--target-base-url",
        required=True,
        help="Base URL for local webhook server.",
    )
    parser.add_argument(
        "--override-path",
        action="append",
        default=[],
        help="Path to proxy (repeatable).",
    )
    parser.add_argument(
        "--override-paths",
        action="append",
        default=[],
        help="Comma-separated list of paths to proxy.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout for local requests.",
    )
    parser.add_argument(
        "--ping-interval",
        type=float,
        default=DEFAULT_PING_INTERVAL_SECONDS,
        help="Websocket ping interval.",
    )
    parser.add_argument(
        "--ping-timeout",
        type=float,
        default=DEFAULT_PING_TIMEOUT_SECONDS,
        help="Websocket ping timeout.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:  # pragma: no cover
    parser = _build_parser()
    args = parser.parse_args(argv)
    override_paths = _parse_override_paths(args.override_path, args.override_paths)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(
        run_client(
            relay_url=args.relay_url,
            target_base_url=args.target_base_url,
            override_paths=override_paths,
            timeout=args.timeout,
            ping_interval=args.ping_interval,
            ping_timeout=args.ping_timeout,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
