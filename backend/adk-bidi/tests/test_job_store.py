from datetime import datetime, timedelta, timezone

from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.jobs.queue import JobStore


def test_job_store_lists_active_requests_without_results():
    store = JobStore()
    now = datetime.now(timezone.utc)
    request_1 = WorkspaceJobRequest(
        job_id="job-1",
        session_id="session-1",
        user_request="Find the migration doc",
        submitted_at=now - timedelta(seconds=30),
    )
    request_2 = WorkspaceJobRequest(
        job_id="job-2",
        session_id="session-1",
        user_request="Create the slide deck",
        submitted_at=now - timedelta(seconds=10),
    )
    store.put_request(request_1)
    store.put_request(request_2)
    store.put_result(
        WorkspaceJobResult(
            job_id="job-1",
            session_id="session-1",
            status="completed",
            summary="Found the document.",
            completed_at=now - timedelta(seconds=20),
        )
    )

    active = store.list_session_active_requests("session-1")

    assert [request.job_id for request in active] == ["job-2"]


def test_job_store_lists_session_requests_in_submission_order():
    store = JobStore()
    now = datetime.now(timezone.utc)
    later = WorkspaceJobRequest(
        job_id="job-2",
        session_id="session-1",
        user_request="Second",
        submitted_at=now,
    )
    earlier = WorkspaceJobRequest(
        job_id="job-1",
        session_id="session-1",
        user_request="First",
        submitted_at=now - timedelta(minutes=1),
    )
    store.put_request(later)
    store.put_request(earlier)

    requests = store.list_session_requests("session-1")

    assert [request.job_id for request in requests] == ["job-1", "job-2"]
