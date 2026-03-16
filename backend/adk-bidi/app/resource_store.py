"""Session-scoped workspace resource state for Phase 3."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ContentReadyCallback = Callable[["ResourceSnapshot"], Awaitable[None]]

ResourceSource = Literal["gmail", "drive", "calendar", "docs", "sheets", "slides"]
ResourceStatus = Literal["metadata_ready", "hydrating", "content_ready", "failed"]


@dataclass(frozen=True)
class ResourceHandle:
    source: ResourceSource
    kind: str
    id: str
    title: str
    url: str | None = None
    version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))


@dataclass(frozen=True)
class WorkspaceObservation:
    summary_text: str
    resources: tuple[ResourceHandle, ...] = ()
    hint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "resources", tuple(self.resources))


@dataclass
class ResourceSnapshot:
    handle: ResourceHandle
    status: ResourceStatus = "metadata_ready"
    metadata: dict[str, Any] = field(default_factory=dict)
    normalized_text: str | None = None
    chunks: list[str] = field(default_factory=list)
    relations: list[ResourceHandle] = field(default_factory=list)
    fetched_at: datetime | None = None
    error: str | None = None


class SessionResourceStore:
    """In-memory working set of resources discovered during one live session."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[tuple[str, str, str], ResourceSnapshot]] = {}
        self._on_content_ready: ContentReadyCallback | None = None

    def set_content_ready_callback(self, callback: ContentReadyCallback) -> None:
        """Register a callback fired (as a background task) when a resource becomes content_ready.

        Used by session_manager to trigger the semantic indexer without blocking
        the hydration path.
        """
        self._on_content_ready = callback

    def upsert_metadata(self, session_id: str, handle: ResourceHandle) -> ResourceSnapshot:
        session = self._sessions.setdefault(session_id, {})
        key = _resource_key(handle)
        now = datetime.now(timezone.utc)
        existing = session.get(key)

        if existing is None or _version_changed(existing.handle.version, handle.version):
            snapshot = ResourceSnapshot(
                handle=deepcopy(handle),
                status="metadata_ready",
                metadata=deepcopy(handle.metadata),
                fetched_at=now,
            )
            session[key] = snapshot
            return deepcopy(snapshot)

        existing.handle = deepcopy(handle)
        existing.metadata = deepcopy(handle.metadata)
        existing.fetched_at = now
        if existing.status == "failed":
            existing.status = "metadata_ready"
            existing.error = None
        return deepcopy(existing)

    def upsert_resources(
        self,
        session_id: str,
        handles: tuple[ResourceHandle, ...] | list[ResourceHandle],
    ) -> list[ResourceSnapshot]:
        return [
            self.upsert_metadata(session_id, handle)
            for handle in handles
            if handle.id.strip()
        ]

    def list_snapshots(self, session_id: str) -> list[ResourceSnapshot]:
        session = self._sessions.get(session_id, {})
        return [deepcopy(snapshot) for snapshot in session.values()]

    def list_handles(self, session_id: str) -> list[ResourceHandle]:
        return [snapshot.handle for snapshot in self.list_snapshots(session_id)]

    def mark_hydrating(self, session_id: str, handle: ResourceHandle) -> ResourceSnapshot:
        snapshot = self._snapshot_for_update(session_id, handle)
        snapshot.handle = deepcopy(handle)
        snapshot.metadata = _merge_metadata(snapshot.metadata, handle.metadata)
        snapshot.status = "hydrating"
        snapshot.error = None
        snapshot.fetched_at = datetime.now(timezone.utc)
        return deepcopy(snapshot)

    def mark_content_ready(
        self,
        session_id: str,
        handle: ResourceHandle,
        *,
        normalized_text: str,
        metadata: dict[str, Any] | None = None,
        chunks: list[str] | None = None,
        relations: tuple[ResourceHandle, ...] | list[ResourceHandle] | None = None,
    ) -> ResourceSnapshot:
        snapshot = self._snapshot_for_update(session_id, handle)
        snapshot.handle = deepcopy(handle)
        snapshot.metadata = _merge_metadata(snapshot.metadata, handle.metadata, metadata or {})
        snapshot.status = "content_ready"
        snapshot.normalized_text = normalized_text
        snapshot.chunks = list(chunks or [])
        snapshot.relations = list(relations or [])
        snapshot.fetched_at = datetime.now(timezone.utc)
        snapshot.error = None
        result = deepcopy(snapshot)
        if self._on_content_ready is not None:
            asyncio.create_task(self._on_content_ready(result))
        return result

    def mark_failed(
        self,
        session_id: str,
        handle: ResourceHandle,
        *,
        error: str,
        metadata: dict[str, Any] | None = None,
    ) -> ResourceSnapshot:
        snapshot = self._snapshot_for_update(session_id, handle)
        snapshot.handle = deepcopy(handle)
        snapshot.metadata = _merge_metadata(snapshot.metadata, handle.metadata, metadata or {})
        snapshot.status = "failed"
        snapshot.normalized_text = None
        snapshot.chunks = []
        snapshot.relations = []
        snapshot.fetched_at = datetime.now(timezone.utc)
        snapshot.error = error
        return deepcopy(snapshot)

    def get_snapshot(
        self,
        session_id: str,
        *,
        source: ResourceSource,
        kind: str,
        resource_id: str,
    ) -> ResourceSnapshot | None:
        snapshot = self._sessions.get(session_id, {}).get((source, kind, resource_id))
        if snapshot is None:
            return None
        return deepcopy(snapshot)

    def clear_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _snapshot_for_update(self, session_id: str, handle: ResourceHandle) -> ResourceSnapshot:
        session = self._sessions.setdefault(session_id, {})
        key = _resource_key(handle)
        snapshot = session.get(key)
        if snapshot is None:
            snapshot = ResourceSnapshot(
                handle=deepcopy(handle),
                metadata=deepcopy(handle.metadata),
            )
            session[key] = snapshot
        return snapshot


def _resource_key(handle: ResourceHandle) -> tuple[str, str, str]:
    return (handle.source, handle.kind, handle.id)


def _version_changed(previous: str | None, current: str | None) -> bool:
    return previous != current and (previous is not None or current is not None)


def _merge_metadata(*parts: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        merged.update(deepcopy(dict(part)))
    return merged
