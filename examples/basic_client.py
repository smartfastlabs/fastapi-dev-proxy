"""Minimal client usage example."""

import asyncio

from fastapi_dev_proxy.client import run_client


def main() -> None:
    asyncio.run(
        run_client(
            relay_url="ws://localhost:8000/fastapi-dev-proxy/websocket?token=secret",
            target_base_url="http://localhost:8080",
            override_paths=["/webhook/sms"],
        )
    )


if __name__ == "__main__":
    main()
