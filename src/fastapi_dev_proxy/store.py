"""Store abstraction for relay state (in-memory or Redis)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RelayStore(Protocol):
    """Protocol for relay state: enabled flag, override paths, and owner instance."""

    async def get_enabled(self) -> bool: ...
    async def set_enabled(self, enabled: bool) -> None: ...
    async def get_override_paths(self) -> list[str]: ...
    async def set_override_paths(self, paths: list[str]) -> None: ...
    async def get_owner(self) -> str | None: ...
    async def set_owner(self, instance_id: str | None, ttl_seconds: float | None = None) -> None: ...
    async def is_owner(self, instance_id: str) -> bool: ...


class InMemoryStore:
    """In-memory store (single instance); no Redis."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._override_paths: list[str] = []
        self._owner: str | None = None

    async def get_enabled(self) -> bool:
        return self._enabled

    async def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    async def get_override_paths(self) -> list[str]:
        return list(self._override_paths)

    async def set_override_paths(self, paths: list[str]) -> None:
        self._override_paths = list(paths)

    async def get_owner(self) -> str | None:
        return self._owner

    async def set_owner(self, instance_id: str | None, ttl_seconds: float | None = None) -> None:
        self._owner = instance_id

    async def is_owner(self, instance_id: str) -> bool:
        return self._owner == instance_id


class InMemoryRelayStore(InMemoryStore):
    """In-memory implementation of RelayStore (default for single instance)."""
