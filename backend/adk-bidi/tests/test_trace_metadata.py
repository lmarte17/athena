from __future__ import annotations

import time
from contextlib import asynccontextmanager

import pytest

from app.jobs.dispatcher import JobDispatcher
from app.jobs.injector import ResultInjector
from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.jobs.queue import JobQueue, JobStore
from app.orchestrator.contracts import TaskResult, TaskStatus
from app.orchestrator.workspace_spoke import WorkspaceSpoke
from app.resource_store import SessionResourceStore


def _install_async_span_recorder(monkeypatch: pytest.MonkeyPatch, module) -> list[dict]:
    spans: list[dict] = []

    @asynccontextmanager
    async def fake_atrace_span(name: str, **kwargs):
        run = {"name": name, **kwargs}
        spans.append(run)
        yield run

    def fake_finish_span(run, *, outputs=None, error=None):
        run["outputs"] = outputs
        run["error"] = error

    monkeypatch.setattr(module, "atrace_span", fake_atrace_span)
    monkeypatch.setattr(module, "finish_span", fake_finish_span)
    return spans


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
async def test_job_dispatcher_trace_includes_session_and_job_metadata(monkeypatch):
    from app.jobs import dispatcher as dispatcher_module

    spans = _install_async_span_recorder(monkeypatch, dispatcher_module)

    queue = JobQueue()
    store = JobStore()
    dispatcher = JobDispatcher(queue=queue, store=store)
    spoke = WorkspaceSpoke(lambda _request: None)

    async def fake_executor(request: WorkspaceJobRequest) -> TaskResult:
        return TaskResult(
            task_id=request.job_id,
            session_id=request.session_id,
            status=TaskStatus.completed,
            summary=f"done: {request.user_request}",
            task_metadata={"source_turn_id": request.source_turn_id},
        )

    dispatcher.set_task_executor(fake_executor)
    dispatcher.set_result_adapter(lambda result, request: spoke.task_result_to_workspace_job_result(result, request=request))

    request = WorkspaceJobRequest(
        job_id="job-123",
        session_id="session-abc",
        source_turn_id="turn-123",
        user_request="Summarize the doc",
    )
    queue.submit_nowait(request)

    await dispatcher._process(request)

    assert spans[0]["name"] == "athena.job.dispatch"
    assert spans[0]["metadata"]["athena_session_id"] == "session-abc"
    assert spans[0]["metadata"]["athena_turn_id"] == "turn-123"
    assert spans[0]["metadata"]["athena_job_id"] == "job-123"
    assert spans[0]["metadata"]["athena_task_id"] == "job-123"
    assert spans[0]["outputs"]["result"]["job_id"] == "job-123"
    assert spans[0]["outputs"]["result"]["session_id"] == "session-abc"
    assert spans[0]["outputs"]["result"]["source_turn_id"] == "turn-123"


@pytest.mark.asyncio
async def test_result_injector_trace_includes_correlation_metadata_and_payload(monkeypatch):
    from app.jobs import injector as injector_module

    spans = _install_async_span_recorder(monkeypatch, injector_module)
    runtime = _FakeRuntime()
    injector = ResultInjector(
        resource_store=SessionResourceStore(),
        runtime_lookup=lambda _session_id: runtime,
    )

    monkeypatch.setattr(time, "monotonic", lambda: 5.0)

    result = WorkspaceJobResult(
        job_id="job-456",
        session_id="session-def",
        source_turn_id="turn-456",
        status="completed",
        summary="Created the presentation draft.",
    )

    await injector.inject(result)

    assert [span["name"] for span in spans] == [
        "athena.result_injector.inject",
        "athena.result_injector.do_inject",
    ]
    assert spans[0]["metadata"]["athena_session_id"] == "session-def"
    assert spans[0]["metadata"]["athena_turn_id"] == "turn-456"
    assert spans[0]["metadata"]["athena_job_id"] == "job-456"
    assert spans[0]["metadata"]["athena_task_id"] == "job-456"
    assert spans[1]["metadata"]["athena_session_id"] == "session-def"
    assert spans[1]["metadata"]["athena_turn_id"] == "turn-456"
    assert spans[1]["metadata"]["athena_job_id"] == "job-456"
    assert spans[1]["metadata"]["athena_task_id"] == "job-456"
    assert spans[1]["outputs"]["payload"]["text"].startswith("[Background workspace job completed]")
    assert runtime.events == [{"type": "ready", "job_id": "job-456"}]
    assert len(runtime.queue.messages) == 1
