import asyncio

import pytest

from app.hydration_scheduler import HydrationScheduler
from app.hydration_types import HydrationResult
from app.resource_store import ResourceHandle, SessionResourceStore


async def _wait_for(predicate, timeout: float = 0.3) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


class _FakeHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "docs"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        return HydrationResult(
            handle=handle,
            normalized_text="Paragraph one.\n\nParagraph two.",
        )


class _DelayedHydrator:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "docs"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        await self._gate.wait()
        return HydrationResult(
            handle=handle,
            normalized_text=f"content for {handle.version}",
        )


class _RelationHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "calendar"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        return HydrationResult(
            handle=handle,
            normalized_text="calendar content",
            relations=(
                ResourceHandle(
                    source="docs",
                    kind="document",
                    id="doc-1",
                    title="Agenda",
                ),
            ),
        )


class _RelatedDocHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "docs"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        return HydrationResult(
            handle=handle,
            normalized_text="linked doc content",
        )


@pytest.mark.asyncio
async def test_hydration_scheduler_marks_snapshot_content_ready():
    store = SessionResourceStore()
    scheduler = HydrationScheduler(store, hydrators=[_FakeHydrator()])
    handle = ResourceHandle(
        source="docs",
        kind="document",
        id="file-123",
        title="Interview Notes",
        version="v1",
    )
    store.upsert_metadata("session-1", handle)

    scheduled = scheduler.schedule_resources("session-1", (handle,))

    assert scheduled == 1
    snapshot = store.get_snapshot("session-1", source="docs", kind="document", resource_id="file-123")
    assert snapshot is not None
    assert snapshot.status == "hydrating"

    await _wait_for(
        lambda: (
            (current := store.get_snapshot("session-1", source="docs", kind="document", resource_id="file-123"))
            is not None
            and current.status == "content_ready"
        )
    )

    snapshot = store.get_snapshot("session-1", source="docs", kind="document", resource_id="file-123")
    assert snapshot is not None
    assert snapshot.normalized_text == "Paragraph one.\n\nParagraph two."
    assert snapshot.chunks == ["Paragraph one.\n\nParagraph two."]


@pytest.mark.asyncio
async def test_hydration_scheduler_skips_stale_version_results():
    gate = asyncio.Event()
    store = SessionResourceStore()
    scheduler = HydrationScheduler(store, hydrators=[_DelayedHydrator(gate)])
    handle_v1 = ResourceHandle(
        source="docs",
        kind="document",
        id="file-123",
        title="Interview Notes",
        version="v1",
    )
    handle_v2 = ResourceHandle(
        source="docs",
        kind="document",
        id="file-123",
        title="Interview Notes",
        version="v2",
    )

    store.upsert_metadata("session-1", handle_v1)
    assert scheduler.schedule_resources("session-1", (handle_v1,)) == 1

    store.upsert_metadata("session-1", handle_v2)
    assert scheduler.schedule_resources("session-1", (handle_v2,)) == 1

    gate.set()
    await _wait_for(
        lambda: (
            (current := store.get_snapshot("session-1", source="docs", kind="document", resource_id="file-123"))
            is not None
            and current.status == "content_ready"
            and current.handle.version == "v2"
        )
    )

    snapshot = store.get_snapshot("session-1", source="docs", kind="document", resource_id="file-123")
    assert snapshot is not None
    assert snapshot.handle.version == "v2"
    assert snapshot.normalized_text == "content for v2"


@pytest.mark.asyncio
async def test_hydration_scheduler_schedules_related_resources():
    store = SessionResourceStore()
    scheduler = HydrationScheduler(
        store,
        hydrators=[_RelationHydrator(), _RelatedDocHydrator()],
    )
    handle = ResourceHandle(
        source="calendar",
        kind="event",
        id="event-123",
        title="Weekly sync",
    )
    store.upsert_metadata("session-1", handle)

    scheduled = scheduler.schedule_resources("session-1", (handle,))

    assert scheduled == 1

    await _wait_for(
        lambda: (
            (
                current := store.get_snapshot(
                    "session-1",
                    source="docs",
                    kind="document",
                    resource_id="doc-1",
                )
            )
            is not None
            and current.status == "content_ready"
        )
    )

    snapshot = store.get_snapshot(
        "session-1",
        source="docs",
        kind="document",
        resource_id="doc-1",
    )
    assert snapshot is not None
    assert snapshot.normalized_text == "linked doc content"
