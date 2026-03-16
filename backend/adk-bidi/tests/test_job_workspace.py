from datetime import datetime, timezone

from app.job_workspace import JobWorkspaceStore
from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult


def test_job_workspace_store_records_steps_results_and_recent_context():
    store = JobWorkspaceStore()
    request = WorkspaceJobRequest(
        job_id="job-1",
        session_id="session-1",
        user_request="Create a spreadsheet from the pricing document",
    )

    store.start_job(request)
    store.save_entry(
        "session-1",
        "job-1",
        kind="note",
        title="extraction",
        content="Need a canonical row model before writing the sheet.",
    )
    store.record_step_result(
        request,
        step_id="step_1",
        specialist="docs",
        instruction="Read the pricing document and extract rows.",
        output={
            "summary": "Extracted 12 pricing rows from the source document.",
            "artifacts": [
                {
                    "type": "google_doc",
                    "id": "doc-123",
                    "title": "Pricing Source",
                    "content": "Rows extracted from the pricing document.",
                }
            ],
            "resource_handles": [
                {
                    "source": "docs",
                    "kind": "document",
                    "id": "doc-123",
                    "title": "Pricing Source",
                }
            ],
        },
    )
    result = WorkspaceJobResult(
        job_id="job-1",
        session_id="session-1",
        status="completed",
        completed_at=datetime.now(timezone.utc),
        summary="Created the spreadsheet and populated the first pass of rows.",
        artifacts=[
            {
                "type": "spreadsheet_created",
                "id": "sheet-999",
                "title": "Pricing Sheet",
                "content": "Spreadsheet created and populated.",
            }
        ],
        resource_handles=[
            {
                "source": "sheets",
                "kind": "spreadsheet",
                "id": "sheet-999",
                "title": "Pricing Sheet",
            }
        ],
    )
    store.record_job_result(request, result)

    payload = store.payload("session-1")
    assert payload["current"]["job_id"] == "job-1"
    assert payload["current"]["resource_handles"][-1]["id"] == "sheet-999"

    context = store.render_context("session-1", query="fix the spreadsheet and add missing info")
    assert "Pricing Sheet" in context
    assert "Pricing Source" in context
    assert "canonical row model" in context


def test_job_workspace_store_prefers_latest_relevant_workspace():
    store = JobWorkspaceStore()
    doc_job = WorkspaceJobRequest(
        job_id="job-doc",
        session_id="session-2",
        user_request="Summarize the project document",
    )
    sheet_job = WorkspaceJobRequest(
        job_id="job-sheet",
        session_id="session-2",
        user_request="Create a spreadsheet from the project document",
    )

    store.start_job(doc_job)
    store.record_job_result(
        doc_job,
        WorkspaceJobResult(
            job_id="job-doc",
            session_id="session-2",
            status="completed",
            summary="Summarized the project document.",
            resource_handles=[
                {"source": "docs", "kind": "document", "id": "doc-2", "title": "Project Doc"}
            ],
        ),
    )
    store.start_job(sheet_job)
    store.record_job_result(
        sheet_job,
        WorkspaceJobResult(
            job_id="job-sheet",
            session_id="session-2",
            status="completed",
            summary="Created the spreadsheet.",
            resource_handles=[
                {"source": "sheets", "kind": "spreadsheet", "id": "sheet-2", "title": "Project Sheet"}
            ],
        ),
    )

    context = store.render_context("session-2", query="update the spreadsheet")
    first_line = context.splitlines()[1]
    assert "Create a spreadsheet" in first_line
