"""Redis key/channel naming and TTL/heartbeat semantics for distributed relay mode."""

from __future__ import annotations

import hashlib

KEY_PREFIX = "fastapi-dev-proxy"

# Owner key TTL; owner instance must renew before expiry (heartbeat).
OWNER_KEY_TTL_SECONDS = 30
# How often the owner instance renews the owner key.
HEARTBEAT_INTERVAL_SECONDS = 10


def token_hash(token: str) -> str:
    """Stable hash of token for use in Redis keys (avoid storing raw token)."""
    return hashlib.sha256(token.encode()).hexdigest()


def owner_key(th: str) -> str:
    return f"{KEY_PREFIX}:{th}:owner"


def enabled_key(th: str) -> str:
    return f"{KEY_PREFIX}:{th}:enabled"


def override_paths_key(th: str) -> str:
    return f"{KEY_PREFIX}:{th}:override_paths"


def requests_channel(th: str) -> str:
    return f"{KEY_PREFIX}:{th}:requests"


def responses_channel(th: str) -> str:
    return f"{KEY_PREFIX}:{th}:responses"
