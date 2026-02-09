## FastAPI Dev Proxy

[![CI](https://github.com/smartfastlabs/fastapi-dev-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/smartfastlabs/fastapi-dev-proxy/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/smartfastlabs/fastapi-dev-proxy/graph/badge.svg)](https://codecov.io/gh/smartfastlabs/fastapi-dev-proxy)

FastAPI webhook relay/proxy library for local development. Production webhooks are
forwarded over WebSocket to a developer machine, replayed against a local server,
and the response is sent back to production.

### Why

Webhook integrations are awkward to build locally because **the third-party needs to reach your machine**:

- Your FastAPI server is on `localhost`, behind NAT/firewalls, and often on an unreliable dev network.
- “Just open a port” is rarely an option (corporate networks, Wi‑Fi, security policies).

Tools like ngrok (and similar tunneling/reverse-proxy setups) can solve this, but for webhook development they can also feel **overly complex**: extra moving parts, extra configuration, and yet another service to pay for.

This project is the alternative I wanted: **a simple, free, open-source relay** specifically for developing webhook handlers. Instead of exposing your laptop to the internet, updating your webhook registration, you run a tiny websocket “relay” websocket in production. Then you connect a local client, receive real webhook requests, replay them against `http://localhost:...`, and send the response back upstream.

A nice side-effect: you can keep the **same third-party webhook URL** (pointing at your production app) and **toggle forwarding on/off** with a config change (e.g. `enabled=False`) or by simply connecting/disconnecting the dev client—no redeploy and no “update webhook URL” dance.

### Install

```
pip install fastapi-dev-proxy
```

### Server integration

```python
from fastapi import FastAPI, Response
from fastapi_dev_proxy.server import RelayManager

app = FastAPI()
relay = RelayManager(app=app, token="secret", enabled=True, timeout_seconds=25)

@app.post("/webhook/sms")
async def sms_webhook() -> Response:
    return Response(status_code=200)
```

If you prefer, you can create the relay first and call `relay.install(app)` later.

When installed (default `path_prefix` is `/fastapi-dev-proxy`), the relay registers:

- `{path_prefix}/websocket` (WebSocket): dev client connects here
- `{path_prefix}/enable` (POST): enable forwarding (requires `?token=...`)
- `{path_prefix}/disable` (POST): disable forwarding (requires `?token=...`)

### Client usage

The client must include the shared token as the `token` query param when connecting (e.g. `...?token=...`). To avoid hardcoding secrets in source control, read it from an environment variable.

### CLI

```
export FASTAPI_DEV_PROXY_TOKEN="your-shared-token"

fastapi-dev-proxy \
  --relay-url "wss://prod.example.com/fastapi-dev-proxy/websocket" \
  --target-base-url http://localhost:8080 \
  --override-path /webhook/sms \
  --override-path /webhook/user/<uuid>
```

Or pass it explicitly:

```
fastapi-dev-proxy \
  --relay-url "wss://prod.example.com/fastapi-dev-proxy/websocket" \
  --token "your-shared-token" \
  --target-base-url http://localhost:8080 \
  --override-path /webhook/sms
```

#### Override paths can include simple patterns:

- `{param}` or `<param>` matches a single path segment.
- `*` matches a single path segment.
- `/**` at the end matches any remaining path segments.

### Development

Install dev dependencies:

```
python -m pip install -e ".[dev]"
```

If you want to use Hatch scripts:

```
pipx install hatch
```

Common commands (Hatch):

```
hatch run test
hatch run lint
hatch run mypy
```

Common commands (direct):

```
python -m pytest --cov=fastapi_dev_proxy --cov-report=term-missing
ruff check src tests
mypy src
```

To generate shareable coverage reports locally:

```
python -m pytest \
  --cov=fastapi_dev_proxy \
  --cov-report=term-missing \
  --cov-report=xml:coverage.xml \
  --cov-report=html:htmlcov
```
