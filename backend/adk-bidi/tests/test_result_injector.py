import pytest

from app.jobs.injector import PendingInjection, ResultInjector, _build_injection_prompt
from app.jobs.models import WorkspaceJobResult
from app.resource_store import SessionResourceStore


class _FakeHydrationScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[tuple[str, str, str]]]] = []

    def schedule_resources(self, session_id: str, handles):  # type: ignore[no-untyped-def]
        resources = [(handle.source, handle.kind, handle.id) for handle in handles]
        self.calls.append((session_id, resources))
        return len(resources)


class _ExplodingHydrationScheduler:
    def schedule_resources(self, session_id: str, handles):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"boom for {session_id}: {len(handles)}")


class _FakeRuntimeQueue:
    def __init__(self) -> None:
        self.messages = []

    def send_content(self, content) -> None:  # type: ignore[no-untyped-def]
        self.messages.append(content)


class _FakeRuntime:
    def __init__(self) -> None:
        self.queue = _FakeRuntimeQueue()
        self.events: list[dict[str, str]] = []
        self.last_user_audio_at = 0.0

    async def send_event(self, payload: dict[str, str]) -> None:
        self.events.append(payload)


@pytest.mark.asyncio
async def test_result_injector_stores_and_schedules_resource_handles_without_live_runtime():
    store = SessionResourceStore()
    scheduler = _FakeHydrationScheduler()
    injector = ResultInjector(
        resource_store=store,
        runtime_lookup=lambda _session_id: None,
        hydration_scheduler=scheduler,
    )

    result = WorkspaceJobResult(
        job_id="job-1",
        session_id="session-1",
        status="completed",
        summary="Resources ready.",
        resource_handles=[
            {
                "source": "docs",
                "kind": "document",
                "id": "doc-123",
                "title": "Interview Notes",
            },
            {
                "source": "slides",
                "kind": "presentation",
                "id": "deck-456",
                "title": "Quarterly Review",
            },
        ],
    )

    await injector.inject(result)

    assert scheduler.calls == [
        (
            "session-1",
            [
                ("docs", "document", "doc-123"),
                ("slides", "presentation", "deck-456"),
            ],
        )
    ]

    handles = store.list_handles("session-1")
    assert [(handle.source, handle.kind, handle.id) for handle in handles] == [
        ("docs", "document", "doc-123"),
        ("slides", "presentation", "deck-456"),
    ]


def test_build_injection_prompt_preserves_concrete_failure_reason():
    prompt = _build_injection_prompt(
        WorkspaceJobResult(
            job_id="job-1",
            session_id="session-1",
            status="failed",
            error="presentation_created_empty",
        )
    )

    assert "Reason: presentation_created_empty" in prompt
    assert "Do not collapse it into a generic 'technical difficulty'." in prompt


@pytest.mark.asyncio
async def test_result_injector_still_injects_when_handle_processing_fails():
    runtime = _FakeRuntime()
    injector = ResultInjector(
        resource_store=SessionResourceStore(),
        runtime_lookup=lambda _session_id: runtime,
        hydration_scheduler=_ExplodingHydrationScheduler(),
    )

    result = WorkspaceJobResult(
        job_id="job-2",
        session_id="session-2",
        status="completed",
        summary="Found the document.",
        resource_handles=[
            {
                "source": "docs",
                "kind": "document",
                "id": "doc-123",
                "title": "Project Brief",
            }
        ],
    )

    await injector.inject(result)

    assert len(runtime.queue.messages) == 1
    assert runtime.events == [{"type": "ready", "job_id": "job-2"}]


@pytest.mark.asyncio
async def test_result_injector_uses_prompt_override_for_direct_injection():
    runtime = _FakeRuntime()
    injector = ResultInjector(
        resource_store=SessionResourceStore(),
        runtime_lookup=lambda _session_id: runtime,
    )

    await injector.inject_direct(
        runtime,
        PendingInjection(
            result=WorkspaceJobResult(
                job_id="job-3",
                session_id="session-3",
                status="completed",
                summary="Original summary",
            ),
            prompt_override="override prompt",
        ),
        handles_prepared=True,
    )

    payload = runtime.queue.messages[0]
    assert payload.parts[0].text == "override prompt"
