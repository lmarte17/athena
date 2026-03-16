"""Workspace spoke adapter over the existing planner/execution stack."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.orchestrator.contracts import TaskResult, TaskSpec, TaskStatus, TurnEnvelope


@dataclass(slots=True)
class WorkspaceSpoke:
    """Wraps the workspace planner stack behind task-oriented contracts."""

    backend: Any
    name: str = "workspace"

    async def invoke(self, task_spec: TaskSpec, turn_envelope: TurnEnvelope | None) -> TaskResult:
        return await self.run_task(task_spec, turn_envelope)

    async def run_task(self, task_spec: TaskSpec, turn_envelope: TurnEnvelope | None) -> TaskResult:
        request = self.task_spec_to_workspace_job_request(task_spec, turn_envelope)
        return await self.run_request(request)

    async def run_request(self, request: WorkspaceJobRequest) -> TaskResult:
        backend_result = await self._call_backend(request)
        return self.workspace_job_result_to_task_result(
            backend_result,
            task_id=request.job_id,
        )

    def task_spec_to_workspace_job_request(
        self,
        task_spec: TaskSpec,
        turn_envelope: TurnEnvelope | None,
    ) -> WorkspaceJobRequest:
        payload = dict(task_spec.input_payload)
        user_request = str(payload.get("user_request") or task_spec.goal)
        resource_hints = [str(item) for item in list(payload.get("resource_hints") or [])]
        conversation_window = []
        if turn_envelope is not None:
            conversation_window = [
                {"role": record.role, "content": record.content}
                for record in turn_envelope.recent_turns
            ]
        priority = 3 if task_spec.run_policy.value == "blocking" else 5

        return WorkspaceJobRequest(
            job_id=task_spec.task_id,
            session_id=turn_envelope.session_id if turn_envelope is not None else "",
            source_turn_id=turn_envelope.turn_id if turn_envelope is not None else "",
            user_request=user_request,
            conversation_window=conversation_window,
            job_type_hint=str(payload.get("job_type_hint") or task_spec.task_kind),  # type: ignore[arg-type]
            resource_hints=resource_hints,
            priority=priority,
            requires_confirmation=task_spec.confirmation_required,
        )

    def workspace_job_result_to_task_result(
        self,
        result: WorkspaceJobResult,
        *,
        task_id: str | None = None,
    ) -> TaskResult:
        try:
            status = TaskStatus(result.status)
        except ValueError:
            status = TaskStatus.failed

        raw_resource_handles = list(result.resource_handles or [])
        resource_handle_ids: list[str] = []
        for handle in raw_resource_handles:
            if isinstance(handle, str):
                handle_id = handle.strip()
            else:
                handle_id = str(handle.get("id") or handle.get("handle_id") or "").strip()
            if handle_id:
                resource_handle_ids.append(handle_id)

        return TaskResult(
            task_id=task_id or result.job_id,
            session_id=result.session_id,
            status=status,
            summary=result.summary,
            artifacts=list(result.artifacts or []),
            resource_handles=resource_handle_ids,
            follow_up_questions=list(result.follow_up_questions or []),
            error=result.error,
            task_metadata={
                "source_turn_id": result.source_turn_id,
                "action_proposals": list(result.action_proposals or []),
                "resource_handles_raw": raw_resource_handles,
            },
        )

    def task_result_to_workspace_job_result(
        self,
        result: TaskResult,
        request: WorkspaceJobRequest | None = None,
    ) -> WorkspaceJobResult:
        task_metadata = dict(result.task_metadata)
        raw_resource_handles = list(task_metadata.get("resource_handles_raw") or [])
        if not raw_resource_handles and result.resource_handles:
            raw_resource_handles = [{"id": handle_id} for handle_id in result.resource_handles]

        source_turn_id = str(
            (request.source_turn_id if request is not None else "")
            or task_metadata.get("source_turn_id")
            or ""
        )

        return WorkspaceJobResult(
            job_id=request.job_id if request is not None else result.task_id,
            session_id=request.session_id if request is not None else result.session_id,
            source_turn_id=source_turn_id,
            status=result.status.value,
            summary=result.summary,
            artifacts=list(result.artifacts or []),
            resource_handles=raw_resource_handles,
            follow_up_questions=list(result.follow_up_questions or []),
            action_proposals=list(task_metadata.get("action_proposals") or []),
            error=result.error,
        )

    async def _call_backend(self, request: WorkspaceJobRequest) -> WorkspaceJobResult:
        runner = getattr(self.backend, "run", None)
        if callable(runner):
            return await runner(request)
        if callable(self.backend):
            return await self.backend(request)
        raise TypeError("workspace backend must be callable or expose .run(request)")
