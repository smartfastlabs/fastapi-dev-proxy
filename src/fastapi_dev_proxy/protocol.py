"""Shared protocol definitions for the dev proxy."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableMapping, TypedDict

from .config import EXCLUDED_HEADERS


class RelayInitMessage(TypedDict):
    type: str
    override_paths: list[str]


class RelayReadyMessage(TypedDict):
    type: str
    override_paths: list[str]


class WebhookRequestMessage(TypedDict):
    type: str
    id: str
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body_b64: str


class WebhookResponseMessage(TypedDict):
    type: str
    id: str
    status_code: int
    headers: dict[str, str]
    body_b64: str


@dataclass(frozen=True)
class WebhookResponse:
    """Internal representation of a proxied webhook response."""

    status_code: int
    headers: dict[str, str]
    body: bytes

    def to_message(self, request_id: str) -> WebhookResponseMessage:
        return {
            "type": "webhook_response",
            "id": request_id,
            "status_code": self.status_code,
            "headers": self.headers,
            "body_b64": base64.b64encode(self.body).decode("ascii"),
        }


def decode_body(body_b64: str | None) -> bytes:
    if not body_b64:
        return b""
    return base64.b64decode(body_b64)


def encode_body(body: bytes) -> str:
    return base64.b64encode(body).decode("ascii")


def filter_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Filter headers that should not be forwarded."""

    filtered: MutableMapping[str, str] = {}
    for key, value in headers.items():
        key_lower = key.lower()
        if key_lower in EXCLUDED_HEADERS:
            continue
        if isinstance(value, (list, tuple)):
            if value:
                filtered[key] = str(value[-1])
            continue
        filtered[key] = str(value)
    return dict(filtered)


def normalize_path(path: str) -> str:
    """Normalize a path for matching override paths."""

    if not path:
        return "/"
    if not path.startswith("/"):
        path = f"/{path}"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def normalize_paths(paths: Iterable[str]) -> list[str]:
    return [normalize_path(path) for path in paths if path]


def match_path(path: str, pattern: str) -> bool:
    """Match a request path against a pattern."""

    normalized_path = normalize_path(path)
    normalized_pattern = normalize_path(pattern)

    if normalized_pattern == "/":
        return normalized_path == "/"

    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3]
        return normalized_path == prefix or normalized_path.startswith(f"{prefix}/")

    path_segments = normalized_path.strip("/").split("/") if normalized_path != "/" else []
    pattern_segments = normalized_pattern.strip("/").split("/") if normalized_pattern != "/" else []

    if len(path_segments) != len(pattern_segments):
        return False

    for path_segment, pattern_segment in zip(path_segments, pattern_segments):
        if _is_wildcard_segment(pattern_segment):
            continue
        if path_segment != pattern_segment:
            return False
    return True


def _is_wildcard_segment(segment: str) -> bool:
    return (
        segment == "*"
        or (segment.startswith("{") and segment.endswith("}"))
        or (segment.startswith("<") and segment.endswith(">"))
    )
