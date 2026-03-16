"""Job dispatcher — drains the job queue and sends each request to the workspace task executor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.job_workspace import JobWorkspaceStore
from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.jobs.queue import JobQueue, JobStore
from app.orchestrator.contracts import TaskResult, TaskStatus
from app.tracing import atrace_span, base_metadata, finish_span

log = logging.getLogger("athena.jobs.dispatcher")

TaskExecutor = Callable[[WorkspaceJobRequest], Awaitable[TaskResult]]
TaskResultAdapter = Callable[[TaskResult, WorkspaceJobRequest], WorkspaceJobResult]
TaskResultCallback = Callable[[TaskResult], Awaitable[None]]


class JobDispatcher:
    """Drains the job queue and calls the workspace task executor for each job.

    The executor is injected at startup to avoid import cycles.
    """

    def __init__(
        self,
        queue: JobQueue,
        store: JobStore,
        on_result: Callable[[WorkspaceJobResult], Awaitable[None]] | None = None,
        workspace: JobWorkspaceStore | None = None,
    ) -> None:
        self._queue = queue
        self._store = store
        self._on_result = on_result
        self._workspace = workspace
        self._task_executor: TaskExecutor | None = None
        self._result_adapter: TaskResultAdapter | None = None
        self._task_result_callback: TaskResultCallback | None = None
        self._drain_task: asyncio.Task | None = None

    def set_task_executor(self, executor: TaskExecutor) -> None:
        self._task_executor = executor

    def set_result_adapter(self, adapter: TaskResultAdapter) -> None:
        self._result_adapter = adapter

    def set_task_result_callback(self, callback: TaskResultCallback) -> None:
        self._task_result_callback = callback

    def set_result_callback(self, callback: Callable[[WorkspaceJobResult], Awaitable[None]]) -> None:
        self._on_result = callback

    async def startup(self) -> None:
        self._drain_task = asyncio.create_task(self._drain(), name="job-dispatcher-drain")
        log.info("JobDispatcher started")

    async def shutdown(self) -> None:
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

    async def _drain(self) -> None:
        while True:
            try:
                request = await self._queue.get()
                asyncio.create_task(
                    self._process(request),
                    name=f"job-{request.job_id[:8]}",
                )
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Unexpected error in job dispatcher drain loop")

    async def _process(self, request: WorkspaceJobRequest) -> None:
        async with atrace_span(
            "athena.job.dispatch",
            inputs={"request": request.to_dict()},
            metadata=base_metadata(
                component="job.dispatch",
                athena_session_id=request.session_id,
                turn_id=request.source_turn_id,
                job_id=request.job_id,
                task_id=request.job_id,
                source_turn_id=request.source_turn_id,
            ),
            tags=["job", "dispatch"],
        ) as run:
            log.info(
                "[job:%s] processing for session %s (hint=%s)",
                request.job_id[:8],
                request.session_id,
                request.job_type_hint,
            )

            try:
                if self._task_executor is None or self._result_adapter is None:
                    log.error("[job:%s] no task executor configured — dropping job", request.job_id[:8])
                    task_result = TaskResult(
                        task_id=request.job_id,
                        session_id=request.session_id,
                        status=TaskStatus.failed,
                        summary="",
                        error="No task executor configured",
                        task_metadata={"source_turn_id": request.source_turn_id},
                    )
                else:
                    if self._workspace is not None:
                        self._workspace.start_job(request)
                    task_result = await self._task_executor(request)
            except asyncio.CancelledError:
                finish_span(run, error="cancelled")
                raise
            except Exception as exc:
                log.exception("[job:%s] task executor raised unexpectedly", request.job_id[:8])
                task_result = TaskResult(
                    task_id=request.job_id,
                    session_id=request.session_id,
                    status=TaskStatus.failed,
                    summary="",
                    error=f"Task executor error: {exc}",
                    task_metadata={"source_turn_id": request.source_turn_id},
                )

            if self._result_adapter is None:
                result = WorkspaceJobResult(
                    job_id=request.job_id,
                    session_id=request.session_id,
                    source_turn_id=request.source_turn_id,
                    status="failed",
                    error="No task-result adapter configured",
                    completed_at=datetime.now(timezone.utc),
                )
            else:
                result = self._result_adapter(task_result, request)

            result.job_id = request.job_id
            result.session_id = request.session_id
            result.source_turn_id = request.source_turn_id or result.source_turn_id
            if result.completed_at is None:
                result.completed_at = datetime.now(timezone.utc)

            self._store.put_result(result)
            if self._workspace is not None:
                self._workspace.record_job_result(request, result)

            log.info(
                "[job:%s] completed with status=%s for session %s",
                request.job_id[:8],
                result.status,
                result.session_id,
            )

            if self._task_result_callback is not None:
                try:
                    await self._task_result_callback(task_result)
                except Exception:
                    log.exception("[job:%s] task-result callback raised", request.job_id[:8])

            if self._on_result is not None:
                try:
                    await self._on_result(result)
                except Exception:
                    log.exception(
                        "[job:%s] result callback raised", request.job_id[:8]
                    )

            finish_span(run, outputs={"result": result.to_dict()})
            self._queue.task_done()
