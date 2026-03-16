import pytest

from app.job_workspace import JobWorkspaceStore
from app.jobs.models import WorkspaceJobRequest
from app.tools.guarded_creation_tools import (
    guard_resource_creation,
    make_creation_key,
    reject_implicit_blank_presentation,
)


@pytest.mark.asyncio
async def test_guard_resource_creation_reuses_matching_resource_within_job():
    store = JobWorkspaceStore()
    request = WorkspaceJobRequest(
        job_id="job-1",
        session_id="session-1",
        user_request="Create a project brief doc.",
    )
    store.start_job(request)

    calls = {"count": 0}

    async def create_call() -> dict[str, str]:
        calls["count"] += 1
        return {
            "documentId": f"doc-{calls['count']}",
            "title": "Project Brief",
            "url": f"https://docs.google.com/document/d/doc-{calls['count']}/edit",
        }

    first = await guard_resource_creation(
        workspace_store=store,
        session_id="session-1",
        job_id="job-1",
        source="docs",
        kind="document",
        result_id_field="documentId",
        title="Project Brief",
        dedupe_key=make_creation_key("docs", "create", "Project Brief"),
        create_call=create_call,
        handle_metadata={"tool": "create_google_doc"},
    )
    second = await guard_resource_creation(
        workspace_store=store,
        session_id="session-1",
        job_id="job-1",
        source="docs",
        kind="document",
        result_id_field="documentId",
        title="Project Brief",
        dedupe_key=make_creation_key("docs", "create", "Project Brief"),
        create_call=create_call,
        handle_metadata={"tool": "create_google_doc"},
    )

    assert calls["count"] == 1
    assert first["documentId"] == "doc-1"
    assert second == {
        "documentId": "doc-1",
        "title": "Project Brief",
        "url": "https://docs.google.com/document/d/doc-1/edit",
        "reused": True,
    }

    workspace = store.get_workspace("session-1", "job-1")
    assert workspace is not None
    assert workspace.generated_resources() == [
        {
            "source": "docs",
            "kind": "document",
            "id": "doc-1",
            "title": "Project Brief",
            "url": "https://docs.google.com/document/d/doc-1/edit",
            "metadata": {
                "tool": "create_google_doc",
                "dedupe_key": "docs|create|project brief",
            },
            "dedupe_key": "docs|create|project brief",
        }
    ]
    assert workspace.resource_handles == [
        {
            "source": "docs",
            "kind": "document",
            "id": "doc-1",
            "title": "Project Brief",
            "url": "https://docs.google.com/document/d/doc-1/edit",
            "metadata": {
                "tool": "create_google_doc",
                "dedupe_key": "docs|create|project brief",
            },
        }
    ]


def test_job_workspace_store_lookup_generated_resource_returns_saved_handle():
    store = JobWorkspaceStore()
    request = WorkspaceJobRequest(
        job_id="job-2",
        session_id="session-2",
        user_request="Create a summary deck.",
    )
    store.start_job(request)
    store.remember_generated_resource(
        "session-2",
        "job-2",
        dedupe_key=make_creation_key("slides", "create", "Summary Deck"),
        source="slides",
        kind="presentation",
        resource_id="deck-123",
        title="Summary Deck",
        url="https://docs.google.com/presentation/d/deck-123/edit",
        metadata={"tool": "create_presentation"},
    )

    assert store.lookup_generated_resource(
        "session-2",
        "job-2",
        dedupe_key="slides|create|summary deck",
    ) == {
        "source": "slides",
        "kind": "presentation",
        "id": "deck-123",
        "title": "Summary Deck",
        "url": "https://docs.google.com/presentation/d/deck-123/edit",
        "metadata": {
            "tool": "create_presentation",
            "dedupe_key": "slides|create|summary deck",
        },
    }

    payload = store.payload("session-2", "job-2")
    assert payload["current"]["generated_resources"] == [
        {
            "source": "slides",
            "kind": "presentation",
            "id": "deck-123",
            "title": "Summary Deck",
            "url": "https://docs.google.com/presentation/d/deck-123/edit",
            "metadata": {
                "tool": "create_presentation",
                "dedupe_key": "slides|create|summary deck",
            },
            "dedupe_key": "slides|create|summary deck",
        }
    ]


def test_reject_implicit_blank_presentation_requires_explicit_opt_in():
    assert reject_implicit_blank_presentation(
        title="Migration Deck",
    ) == {
        "presentationId": "",
        "title": "Migration Deck",
        "error": "blank_presentation_requires_allow_blank",
    }


def test_reject_implicit_blank_presentation_allows_explicit_blank_or_template():
    assert reject_implicit_blank_presentation(
        title="Migration Deck",
        allow_blank=True,
        user_request="Create a blank presentation shell for the migration review.",
    ) is None
    assert reject_implicit_blank_presentation(
        title="Migration Deck",
        template_id="template-123",
    ) is None


def test_reject_implicit_blank_presentation_blocks_allow_blank_without_explicit_request():
    assert reject_implicit_blank_presentation(
        title="Migration Deck",
        allow_blank=True,
        user_request="Turn the migration notes into a slide deck.",
    ) == {
        "presentationId": "",
        "title": "Migration Deck",
        "error": "blank_presentation_requires_allow_blank",
    }
