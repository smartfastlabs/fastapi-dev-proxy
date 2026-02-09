## FastAPI Dev Proxy

[![CI](https://github.com/smartfastlabs/fastapi-dev-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/smartfastlabs/fastapi-dev-proxy/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/smartfastlabs/fastapi-dev-proxy/graph/badge.svg)](https://codecov.io/gh/smartfastlabs/fastapi-dev-proxy)

FastAPI webhook relay/proxy library for local development. Production webhooks are
forwarded over WebSocket to a developer machine, replayed against a local server,
and the response is sent back to production.

### Status

- v0 software: unstable APIs and behavior, expect breaking changes.
- Extracted from another project; not all original features are included yet.

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

Alternatively, use the class method for a one-line setup: `relay = RelayManager.for_app(app, token="secret", ...)`. Or create the relay first and call `relay.install(app)` later.

### Client usage

```python
import asyncio

from fastapi_dev_proxy.client import run_client

asyncio.run(
    run_client(
        relay_url="wss://prod.example.com/fastapi-dev-proxy?token=secret",
        target_base_url="http://localhost:8080",
        override_paths=["/webhook/sms", "/webhook/items/{item_id}"],
    )
)
```

Override paths can include simple patterns:

- `{param}` or `<param>` matches a single path segment.
- `*` matches a single path segment.
- `/**` at the end matches any remaining path segments.

### CLI

```
fastapi-dev-proxy \
  --relay-url wss://prod.example.com/fastapi-dev-proxy?token=secret \
  --target-base-url http://localhost:8080 \
  --override-path /webhook/sms
```

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

CI uploads `coverage.xml` and `htmlcov/` as workflow artifacts. If you enable Codecov for the repo, CI will also upload coverage there (badge above).
