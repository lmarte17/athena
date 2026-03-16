"""FunctionTool helpers for job-scoped scratchpad state."""

from __future__ import annotations

from google.adk.tools import FunctionTool

from app.job_workspace import JobWorkspaceStore, format_json_for_workspace


def build_job_workspace_tools(
    workspace_store: JobWorkspaceStore | None,
    *,
    session_id: str,
    job_id: str,
) -> list[FunctionTool]:
    if workspace_store is None or not session_id or not job_id:
        return []

    async def get_job_workspace_state() -> dict:
        """Read the current job workspace and the most recent related work in this session."""
        return workspace_store.payload(session_id, job_id)

    async def save_job_workspace_note(
        title: str,
        content: str,
        kind: str = "note",
    ) -> dict:
        """Save a scratch note for this job workspace."""
        entry = workspace_store.save_entry(
            session_id,
            job_id,
            kind=kind,
            title=title,
            content=content,
        )
        return {"status": "saved", "kind": kind, "title": title, "saved_at": entry.updated_at.isoformat()}

    async def save_job_workspace_json(
        name: str,
        json_text: str,
        kind: str = "structured",
    ) -> dict:
        """Save validated JSON into the job workspace for later reuse."""
        formatted = format_json_for_workspace(json_text)
        entry = workspace_store.save_entry(
            session_id,
            job_id,
            kind=kind,
            title=name,
            content=formatted,
        )
        return {
            "status": "saved",
            "kind": kind,
            "title": name,
            "chars": len(formatted),
            "saved_at": entry.updated_at.isoformat(),
        }

    async def save_job_workspace_table(
        name: str,
        rows_json: str,
        description: str = "",
    ) -> dict:
        """Save a canonical table or row model as JSON for later sheet/doc rewrites."""
        formatted = format_json_for_workspace(rows_json)
        entry = workspace_store.save_entry(
            session_id,
            job_id,
            kind="table",
            title=name,
            content=formatted,
            metadata={"description": description},
        )
        return {
            "status": "saved",
            "kind": "table",
            "title": name,
            "chars": len(formatted),
            "saved_at": entry.updated_at.isoformat(),
        }

    return [
        FunctionTool(get_job_workspace_state),
        FunctionTool(save_job_workspace_note),
        FunctionTool(save_job_workspace_json),
        FunctionTool(save_job_workspace_table),
    ]
