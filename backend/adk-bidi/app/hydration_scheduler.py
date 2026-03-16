"""Background hydration queue for workspace resources."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterable

from app.agents.calendar_hydrator import CalendarHydrator
from app.agents.doc_hydrator import DocHydrator
from app.agents.drive_hydrator import DriveHydrator
from app.agents.gmail_hydrator import GmailHydrator
from app.agents.sheets_hydrator import SheetsHydrator
from app.agents.slides_hydrator import SlidesHydrator
from app.gog_client import GogError
from app.hydration_types import HydrationResult, ResourceHydrator
from app.resource_store import ResourceHandle, SessionResourceStore
from app.tracing import atrace_span, base_metadata, finish_span, preview_value, trace_span

log = logging.getLogger("athena.hydration_scheduler")


class HydrationScheduler:
    """Queues supported resource handles for opportunistic background hydration."""

    def __init__(
        self,
        resource_store: SessionResourceStore,
        hydrators: Iterable[ResourceHydrator] | None = None,
    ) -> None:
        self._resource_store = resource_store
        self._hydrators = tuple(
            hydrators
            or (
                GmailHydrator(),
                DocHydrator(),
                CalendarHydrator(),
                DriveHydrator(),
                SheetsHydrator(),
                SlidesHydrator(),
            )
        )
        self._session_tasks: dict[str, set[asyncio.Task]] = {}
        self._active_keys: set[tuple[str, str, str, str, str]] = set()
        self._cancelled_sessions: set[str] = set()
        self._semaphore = asyncio.Semaphore(
            int(os.getenv("ATHENA_HYDRATION_CONCURRENCY", "2"))
        )

    def schedule_resources(
        self,
        session_id: str,
        handles: tuple[ResourceHandle, ...] | list[ResourceHandle],
    ) -> int:
        with trace_span(
            "athena.hydration.schedule",
            inputs={
                "session_id": session_id,
                "handle_count": len(handles),
                "handles": [
                    {
                        "source": handle.source,
                        "kind": handle.kind,
                        "id": handle.id,
                        "version": handle.version,
                    }
                    for handle in handles
                ],
            },
            metadata=base_metadata(
                component="hydration.schedule",
                athena_session_id=session_id,
            ),
            tags=["hydration"],
        ) as run:
            self._cancelled_sessions.discard(session_id)

            scheduled = 0
            max_per_batch = int(os.getenv("ATHENA_HYDRATION_MAX_RESOURCES_PER_BATCH", "2"))
            for handle in handles:
                if scheduled >= max_per_batch:
                    break

                hydrator = self._find_hydrator(handle)
                if hydrator is None:
                    continue

                task_key = _task_key(session_id, handle)
                if task_key in self._active_keys:
                    continue

                snapshot = self._resource_store.get_snapshot(
                    session_id,
                    source=handle.source,
                    kind=handle.kind,
                    resource_id=handle.id,
                )
                if snapshot is None:
                    continue
                if snapshot.status == "hydrating":
                    continue
                if snapshot.status == "content_ready" and snapshot.handle.version == handle.version:
                    continue

                self._resource_store.mark_hydrating(session_id, handle)
                task = asyncio.create_task(
                    self._hydrate_resource(session_id, handle, hydrator),
                    name=f"hydrate-{handle.source}-{handle.id[:8]}",
                )
                self._session_tasks.setdefault(session_id, set()).add(task)
                self._active_keys.add(task_key)
                task.add_done_callback(
                    lambda done, sid=session_id, key=task_key: self._forget_task(sid, key, done)
                )
                scheduled += 1

            finish_span(
                run,
                outputs={
                    "scheduled": scheduled,
                    "max_per_batch": max_per_batch,
                },
            )
            return scheduled

    def cancel_session(self, session_id: str) -> None:
        self._cancelled_sessions.add(session_id)
        for task in tuple(self._session_tasks.get(session_id, set())):
            if not task.done():
                task.cancel()

    def pending_count(self, session_id: str) -> int:
        return len(self._session_tasks.get(session_id, set()))

    async def _hydrate_resource(
        self,
        session_id: str,
        handle: ResourceHandle,
        hydrator: ResourceHydrator,
    ) -> None:
        async with atrace_span(
            "athena.hydration.resource",
            inputs={
                "handle": {
                    "source": handle.source,
                    "kind": handle.kind,
                    "id": handle.id,
                    "title": handle.title,
                    "version": handle.version,
                }
            },
            metadata=base_metadata(
                component="hydration.resource",
                athena_session_id=session_id,
                specialist=type(hydrator).__name__,
            ),
            tags=["hydration"],
        ) as run:
            try:
                async with self._semaphore:
                    result = await hydrator.hydrate(handle)
            except asyncio.CancelledError:
                finish_span(run, error="cancelled")
                raise
            except GogError as exc:
                if session_id not in self._cancelled_sessions:
                    self._resource_store.mark_failed(
                        session_id,
                        handle,
                        error=str(exc),
                    )
                log.warning(
                    "Hydration failed for %s %s in session %s: %s",
                    handle.source,
                    handle.id,
                    session_id,
                    exc,
                )
                finish_span(run, error=str(exc))
                return
            except Exception as exc:
                if session_id not in self._cancelled_sessions:
                    self._resource_store.mark_failed(
                        session_id,
                        handle,
                        error="Unexpected hydration error",
                    )
                log.exception(
                    "Unexpected hydration failure for %s %s in session %s",
                    handle.source,
                    handle.id,
                    session_id,
                )
                finish_span(run, error=str(exc))
                return

            if session_id in self._cancelled_sessions:
                finish_span(run, outputs={"cancelled_session": True})
                return
            if result is None or not result.normalized_text.strip():
                self._resource_store.mark_failed(
                    session_id,
                    handle,
                    error="Hydrator returned no content",
                )
                finish_span(run, error="Hydrator returned no content")
                return
            if _snapshot_version_changed(self._resource_store, session_id, handle):
                log.info(
                    "Skipping stale hydration result for %s %s in session %s",
                    handle.source,
                    handle.id,
                    session_id,
                )
                finish_span(run, outputs={"stale": True})
                return

            normalized_text = _truncate_text(result.normalized_text)
            chunks = _chunk_text(normalized_text)
            self._resource_store.mark_content_ready(
                session_id,
                result.handle,
                normalized_text=normalized_text,
                metadata=result.metadata,
                chunks=chunks,
                relations=result.relations,
            )
            if result.relations:
                self._resource_store.upsert_resources(session_id, result.relations)
                self.schedule_resources(session_id, result.relations)

            finish_span(
                run,
                outputs={
                    "normalized_text": preview_value(normalized_text),
                    "chunk_count": len(chunks),
                    "relation_count": len(result.relations),
                },
            )

    def _forget_task(
        self,
        session_id: str,
        task_key: tuple[str, str, str, str, str],
        task: asyncio.Task,
    ) -> None:
        self._active_keys.discard(task_key)
        tasks = self._session_tasks.get(session_id)
        if tasks is None:
            return

        tasks.discard(task)
        if tasks:
            return

        self._session_tasks.pop(session_id, None)
        self._cancelled_sessions.discard(session_id)

    def _find_hydrator(self, handle: ResourceHandle) -> ResourceHydrator | None:
        for hydrator in self._hydrators:
            if hydrator.supports(handle):
                return hydrator
        return None


def _task_key(session_id: str, handle: ResourceHandle) -> tuple[str, str, str, str, str]:
    return (
        session_id,
        handle.source,
        handle.kind,
        handle.id,
        handle.version or "",
    )


def _snapshot_version_changed(
    resource_store: SessionResourceStore,
    session_id: str,
    handle: ResourceHandle,
) -> bool:
    snapshot = resource_store.get_snapshot(
        session_id,
        source=handle.source,
        kind=handle.kind,
        resource_id=handle.id,
    )
    if snapshot is None:
        return True
    return snapshot.handle.version != handle.version and (
        snapshot.handle.version is not None or handle.version is not None
    )


def _truncate_text(text: str) -> str:
    limit = int(os.getenv("ATHENA_HYDRATION_MAX_TEXT_CHARS", "12000"))
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _chunk_text(text: str) -> list[str]:
    max_chars = int(os.getenv("ATHENA_HYDRATION_CHUNK_CHARS", "1200"))
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n{2,}", text) if paragraph.strip()]
    if not paragraphs:
        return [text] if text else []

    chunks: list[str] = []
    current: list[str] = []

    for paragraph in paragraphs:
        candidate = "\n\n".join([*current, paragraph]).strip()
        if current and len(candidate) > max_chars:
            chunks.append("\n\n".join(current).strip())
            current = [paragraph]
            continue

        if len(paragraph) > max_chars:
            if current:
                chunks.append("\n\n".join(current).strip())
                current = []
            chunks.extend(_split_large_paragraph(paragraph, max_chars))
            continue

        current.append(paragraph)

    if current:
        chunks.append("\n\n".join(current).strip())

    return [chunk for chunk in chunks if chunk]


def _split_large_paragraph(paragraph: str, max_chars: int) -> list[str]:
    words = paragraph.split()
    if not words:
        return []

    chunks: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) > max_chars:
            chunks.append(current)
            current = word
            continue
        current = candidate

    chunks.append(current)
    return chunks
