"""Job queue and in-memory job store for workspace background jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.jobs.models import JobStatus, WorkspaceJobRequest, WorkspaceJobResult

log = logging.getLogger("athena.jobs.queue")


class JobStore:
    """In-memory store for job requests and results, keyed by job_id."""

    def __init__(self) -> None:
        self._requests: dict[str, WorkspaceJobRequest] = {}
        self._results: dict[str, WorkspaceJobResult] = {}

    def put_request(self, request: WorkspaceJobRequest) -> None:
        self._requests[request.job_id] = request
        log.info(
            "[job:%s] stored request for session %s (hint=%s, priority=%d)",
            request.job_id[:8],
            request.session_id,
            request.job_type_hint,
            request.priority,
        )

    def get_request(self, job_id: str) -> WorkspaceJobRequest | None:
        return self._requests.get(job_id)

    def list_session_requests(self, session_id: str) -> list[WorkspaceJobRequest]:
        requests = [r for r in self._requests.values() if r.session_id == session_id]
        requests.sort(key=lambda r: r.submitted_at)
        return requests

    def put_result(self, result: WorkspaceJobResult) -> None:
        self._results[result.job_id] = result
        log.info(
            "[job:%s] result stored (status=%s, session=%s)",
            result.job_id[:8],
            result.status,
            result.session_id,
        )

    def get_result(self, job_id: str) -> WorkspaceJobResult | None:
        return self._results.get(job_id)

    def list_session_results(
        self,
        session_id: str,
        *,
        status: JobStatus | None = None,
    ) -> list[WorkspaceJobResult]:
        results = [r for r in self._results.values() if r.session_id == session_id]
        if status is not None:
            results = [r for r in results if r.status == status]
        results.sort(key=lambda r: r.completed_at or datetime.min)
        return results

    def list_session_active_requests(self, session_id: str) -> list[WorkspaceJobRequest]:
        requests = self.list_session_requests(session_id)
        return [request for request in requests if request.job_id not in self._results]

    def clear_session(self, session_id: str) -> None:
        job_ids = [
            jid for jid, req in self._requests.items() if req.session_id == session_id
        ]
        for jid in job_ids:
            self._requests.pop(jid, None)
            self._results.pop(jid, None)
        if job_ids:
            log.info("Cleared %d job(s) for session %s", len(job_ids), session_id)


class JobQueue:
    """Async queue that delivers WorkspaceJobRequests to the job dispatcher."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[WorkspaceJobRequest] = asyncio.Queue()

    async def submit(self, request: WorkspaceJobRequest) -> None:
        """Enqueue a job request for background processing."""
        await self._queue.put(request)
        log.info(
            "[job:%s] enqueued for session %s",
            request.job_id[:8],
            request.session_id,
        )

    def submit_nowait(self, request: WorkspaceJobRequest) -> None:
        """Non-blocking enqueue (use when caller has no event loop context)."""
        self._queue.put_nowait(request)

    async def get(self) -> WorkspaceJobRequest:
        """Wait for and return the next job request."""
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()
