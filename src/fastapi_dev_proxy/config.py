"""Configuration defaults for the dev proxy."""

from __future__ import annotations

DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_PING_INTERVAL_SECONDS = 20.0
DEFAULT_PING_TIMEOUT_SECONDS = 20.0

EXCLUDED_HEADERS = {
    "host",
    "content-length",
    "connection",
}
