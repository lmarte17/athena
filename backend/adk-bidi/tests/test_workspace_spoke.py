from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.orchestrator.contracts import TaskSpec, TaskStatus, TurnEnvelope, TurnRecord
from app.orchestrator.workspace_spoke import WorkspaceSpoke


@pytest.mark.asyncio
async def test_workspace_spoke_runs_backend_and_returns_task_result():
    async def fake_backend(request: WorkspaceJobRequest) -> WorkspaceJobResult:
        assert request.job_id == "task-1"
        return WorkspaceJobResult(
            job_id=request.job_id,
            session_id=request.session_id,
            source_turn_id=request.source_turn_id,
            status="completed",
            summary="Found it",
            resource_handles=[{"id": "resource-1", "source": "gmail", "kind": "thread", "title": "Inbox"}],
        )

    spoke = WorkspaceSpoke(fake_backend)
    result = await spoke.run_request(
        WorkspaceJobRequest(
            job_id="task-1",
            session_id="session-1",
            source_turn_id="turn-1",
            user_request="Check email",
        )
    )

    assert result.task_id == "task-1"
    assert result.status == TaskStatus.completed
    assert result.resource_handles == ["resource-1"]
    assert result.task_metadata["resource_handles_raw"][0]["title"] == "Inbox"


def test_workspace_spoke_converts_task_spec_to_request_and_back():
    spoke = WorkspaceSpoke(lambda _request: None)
    envelope = TurnEnvelope(
        session_id="session-1",
        turn_id="turn-1",
        transcript="Check email",
        timestamp=datetime(2026, 3, 15, tzinfo=UTC),
        recent_turns=[TurnRecord(role="user", content="Earlier question")],
        active_task_ids=[],
    )
    spec = TaskSpec(
        task_id="task-1",
        task_kind="gmail_search",
        goal="Check email",
        input_payload={
            "user_request": "Check email",
            "job_type_hint": "gmail_search",
            "resource_hints": ["gmail"],
        },
    )

    request = spoke.task_spec_to_workspace_job_request(spec, envelope)
    task_result = spoke.workspace_job_result_to_task_result(
        WorkspaceJobResult(
            job_id="task-1",
            session_id="session-1",
            source_turn_id="turn-1",
            status="completed",
            summary="Found it",
            resource_handles=[{"id": "resource-1", "source": "gmail", "kind": "thread", "title": "Inbox"}],
        )
    )
    round_tripped = spoke.task_result_to_workspace_job_result(task_result, request=request)

    assert request.conversation_window == [{"role": "user", "content": "Earlier question"}]
    assert request.resource_hints == ["gmail"]
    assert round_tripped.job_id == "task-1"
    assert round_tripped.resource_handles[0]["id"] == "resource-1"
