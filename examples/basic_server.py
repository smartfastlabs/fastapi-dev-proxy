"""Minimal FastAPI integration example."""

from fastapi import FastAPI, Response

from fastapi_dev_proxy.server import RelayManager

app = FastAPI()
relay = RelayManager(app=app, token="secret", enabled=True, timeout_seconds=25)


@app.post("/webhook/sms")
async def sms_webhook() -> Response:
    return Response(status_code=200)
