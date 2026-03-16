from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.jobs.models import WorkspaceJobResult
from app.jobs.queue import JobQueue, JobStore
from app.orchestrator.contracts import TaskSpec, TaskStatus, TurnEnvelope, TurnRecord
from app.orchestrator.state_store import SessionMode, StateStore
from app.orchestrator.task_manager import TaskManager
from app.orchestrator.workspace_spoke import WorkspaceSpoke


def _build_workspace_spoke():
    async def fake_backend(_request):
        return WorkspaceJobResult(status="completed", summary="done")

    return WorkspaceSpoke(fake_backend)


def test_task_manager_create_task_dedupes_active_task():
    store = StateStore()
    manager = TaskManager(
        state_store=store,
        job_queue=JobQueue(),
        job_store=JobStore(),
        workspace_spoke=_build_workspace_spoke(),
    )
    session = SimpleNamespace(id="session-1", state={})

    first = TaskSpec(
        task_id="task-1",
        task_kind="gmail_search",
        goal="Check email",
        input_payload={"user_request": "Check email"},
        dedupe_key="gmail_search:check email",
    )
    duplicate = TaskSpec(
        task_id="task-2",
        task_kind="gmail_search",
        goal="Check email",
        input_payload={"user_request": "Check email"},
        dedupe_key="gmail_search:check email",
    )

    assert manager.create_task(session, first) == "task-1"
    assert manager.create_task(session, duplicate) == "task-1"
    assert store.get_active_task_ids(session) == ["task-1"]
    assert store.get_current_mode(session) == SessionMode.tasks_running


@pytest.mark.asyncio
async def test_task_manager_submit_pending_tasks_enqueues_workspace_request():
    store = StateStore()
    job_queue = JobQueue()
    job_store = JobStore()
    manager = TaskManager(
        state_store=store,
        job_queue=job_queue,
        job_store=job_store,
        workspace_spoke=_build_workspace_spoke(),
    )
    session = SimpleNamespace(id="session-1", state={})
    store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id="session-1",
            turn_id="turn-1",
            transcript="Check email",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            recent_turns=[TurnRecord(role="user", content="Earlier question")],
            active_task_ids=[],
        ),
    )
    store.set_pending_task_specs(
        session,
        [
            TaskSpec(
                task_id="task-1",
                task_kind="gmail_search",
                goal="Check email",
                input_payload={
                    "user_request": "Check email",
                    "job_type_hint": "gmail_search",
                    "resource_hints": ["gmail"],
                },
            )
        ],
    )

    submitted = await manager.submit_pending_tasks(session)

    assert [request.job_id for request in submitted] == ["task-1"]
    request = job_store.get_request("task-1")
    assert request is not None
    assert request.source_turn_id == "turn-1"
    assert request.conversation_window == [{"role": "user", "content": "Earlier question"}]
    assert request.resource_hints == ["gmail"]
    assert job_queue.qsize() == 1


def test_task_manager_converts_completion_into_task_result_state():
    store = StateStore()
    manager = TaskManager(
        state_store=store,
        job_queue=JobQueue(),
        job_store=JobStore(),
        workspace_spoke=_build_workspace_spoke(),
    )
    session = SimpleNamespace(
        id="session-1",
        state={
            "user:active_tasks": {
                "task-1": {
                    "task_id": "task-1",
                    "task_kind": "gmail_search",
                    "goal": "Check email",
                    "input_payload": {"user_request": "Check email"},
                    "dependencies": [],
                    "run_policy": "background",
                    "confirmation_required": False,
                    "surface_on_completion": True,
                }
            }
        },
    )

    task_result = manager.workspace_job_result_to_task_result(
        WorkspaceJobResult(
            job_id="task-1",
            session_id="session-1",
            status="completed",
            summary="Found it",
            resource_handles=[{"id": "resource-1"}],
        )
    )
    manager.complete_task(session, task_result)

    assert task_result.status == TaskStatus.completed
    assert session.state["user:active_tasks"] == {}
    assert session.state["user:completed_tasks"][0]["resource_handles"] == ["resource-1"]
    assert session.state["user:completed_tasks"][0]["task_metadata"]["resource_handles_raw"][0]["id"] == "resource-1"
    assert store.get_current_mode(session) == SessionMode.idle


def test_task_manager_cancel_task_marks_result_suppressed_and_completes_cancelled():
    store = StateStore()
    manager = TaskManager(
        state_store=store,
        job_queue=JobQueue(),
        job_store=JobStore(),
        workspace_spoke=_build_workspace_spoke(),
    )
    session = SimpleNamespace(
        id="session-1",
        state={
            "user:active_tasks": {
                "task-1": {
                    "task_id": "task-1",
                    "task_kind": "gmail_search",
                    "goal": "Check email",
                    "input_payload": {"user_request": "Check email"},
                    "dependencies": [],
                    "run_policy": "background",
                    "confirmation_required": False,
                    "surface_on_completion": True,
                }
            }
        },
    )

    cancelled = manager.cancel_task(
        session,
        "task-1",
        reason="Superseded by correction.",
        replacement_task_id="task-2",
    )

    assert cancelled is not None
    assert cancelled.status == TaskStatus.cancelled
    assert session.state["user:active_tasks"] == {}
    assert session.state["user:completed_tasks"][-1]["task_id"] == "task-1"
    assert store.consume_suppressed_task_result(session, "task-1") == {
        "replacement_task_id": "task-2",
        "reason": "Superseded by correction.",
    }
