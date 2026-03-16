"""Task lifecycle manager for the conversation orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.jobs.queue import JobQueue, JobStore
from app.orchestrator.contracts import TaskResult, TaskSpec, TaskStatus
from app.orchestrator.state_store import (
    ACTIVE_TASKS_STATE_KEY,
    COMPLETED_TASKS_STATE_KEY,
    SessionMode,
    StateStore,
)
from app.orchestrator.workspace_spoke import WorkspaceSpoke

_COMPLETED_TASK_LIMIT = 20


@dataclass(slots=True)
class TaskManager:
    """Owns task creation, dedupe, submission, and completion state."""

    state_store: StateStore
    job_queue: JobQueue
    job_store: JobStore
    workspace_spoke: WorkspaceSpoke

    def create_task(self, session_or_ctx: Any, spec: TaskSpec) -> str:
        state = self.state_store._state(session_or_ctx)
        active = dict(state.get(ACTIVE_TASKS_STATE_KEY, {}))

        existing_task_id = self._find_existing_task_id(active, spec.dedupe_key)
        if existing_task_id is not None:
            return existing_task_id

        active[spec.task_id] = spec.model_dump(mode="json")
        state[ACTIVE_TASKS_STATE_KEY] = active
        self.state_store.set_current_mode(session_or_ctx, SessionMode.tasks_running)
        return spec.task_id

    async def submit_pending_tasks(self, session_or_ctx: Any) -> list[WorkspaceJobRequest]:
        specs = self.state_store.get_pending_task_specs(session_or_ctx)
        self.state_store.clear_pending_task_specs(session_or_ctx)
        if not specs:
            return []

        turn_envelope = self.state_store.get_turn_envelope(session_or_ctx)
        submitted: list[WorkspaceJobRequest] = []
        for spec in specs:
            task_id = self.create_task(session_or_ctx, spec)
            if task_id != spec.task_id:
                continue
            if self.job_store.get_request(task_id) is not None:
                continue

            request = self.workspace_spoke.task_spec_to_workspace_job_request(spec, turn_envelope)
            self.job_store.put_request(request)
            await self.job_queue.submit(request)
            submitted.append(request)
        return submitted

    async def execute_workspace_request(self, request: WorkspaceJobRequest) -> TaskResult:
        return await self.workspace_spoke.run_request(request)

    def task_result_to_workspace_job_result(
        self,
        result: TaskResult,
        request: WorkspaceJobRequest | None = None,
    ) -> WorkspaceJobResult:
        return self.workspace_spoke.task_result_to_workspace_job_result(
            result,
            request=request,
        )

    def complete_task(self, session_or_ctx: Any, result: TaskResult) -> None:
        state = self.state_store._state(session_or_ctx)
        active = dict(state.get(ACTIVE_TASKS_STATE_KEY, {}))
        active.pop(result.task_id, None)
        state[ACTIVE_TASKS_STATE_KEY] = active

        completed = list(state.get(COMPLETED_TASKS_STATE_KEY, []))
        completed.append(result.model_dump(mode="json"))
        state[COMPLETED_TASKS_STATE_KEY] = completed[-_COMPLETED_TASK_LIMIT:]

        next_mode = SessionMode.tasks_running if active else SessionMode.idle
        self.state_store.set_current_mode(session_or_ctx, next_mode)

    def workspace_job_result_to_task_result(self, result: WorkspaceJobResult) -> TaskResult:
        return self.workspace_spoke.workspace_job_result_to_task_result(
            result,
            task_id=result.job_id,
        )

    def get_latest_active_task(self, session_or_ctx: Any) -> TaskSpec | None:
        active_tasks = list(self.state_store.get_active_tasks(session_or_ctx).values())
        return active_tasks[-1] if active_tasks else None

    def get_latest_completed_task(self, session_or_ctx: Any) -> TaskResult | None:
        completed_tasks = self.state_store.get_completed_tasks(session_or_ctx)
        return completed_tasks[-1] if completed_tasks else None

    def cancel_task(
        self,
        session_or_ctx: Any,
        task_id: str,
        *,
        reason: str = "Superseded by a newer user request.",
        replacement_task_id: str = "",
    ) -> TaskResult | None:
        active_tasks = self.state_store.get_active_tasks(session_or_ctx)
        spec = active_tasks.get(task_id)
        if spec is None:
            return None

        self.state_store.mark_task_result_suppressed(
            session_or_ctx,
            task_id,
            replacement_task_id=replacement_task_id,
            reason=reason,
        )

        cancelled = TaskResult(
            task_id=task_id,
            session_id=self._session_id(session_or_ctx, spec),
            status=TaskStatus.cancelled,
            summary="",
            task_metadata={
                "source_turn_id": str(spec.input_payload.get("source_turn_id") or ""),
                "source_event_id": str(spec.input_payload.get("source_event_id") or ""),
                "replacement_task_id": replacement_task_id,
                "cancel_reason": reason,
            },
        )
        self.complete_task(session_or_ctx, cancelled)
        return cancelled

    def _find_existing_task_id(
        self,
        active: dict[str, Any],
        dedupe_key: str | None,
    ) -> str | None:
        if not dedupe_key:
            return None
        for task_id, payload in active.items():
            if isinstance(payload, dict) and payload.get("dedupe_key") == dedupe_key:
                return str(task_id)
        return None

    def _session_id(self, session_or_ctx: Any, spec: TaskSpec) -> str:
        if hasattr(session_or_ctx, "session") and hasattr(session_or_ctx.session, "id"):
            return str(session_or_ctx.session.id)
        if hasattr(session_or_ctx, "id"):
            return str(session_or_ctx.id)
        return str(spec.input_payload.get("session_id") or "")
