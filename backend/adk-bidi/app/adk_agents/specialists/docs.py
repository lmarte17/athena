"""DocsAgent — ADK specialist for all Google Docs operations (read and write)."""

from __future__ import annotations

import logging
import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.job_workspace import JobWorkspaceStore
from app.tools.job_workspace_tools import build_job_workspace_tools
from app.tools.guarded_creation_tools import guard_resource_creation, make_creation_key
from app.tools.workspace_tools import (
    clear_google_doc,
    copy_google_doc as raw_copy_google_doc,
    create_google_doc as raw_create_google_doc,
    export_google_doc_text,
    find_replace_in_doc,
    get_google_doc_info,
    write_google_doc,
)

log = logging.getLogger("athena.adk_agents.specialists.docs")

_MODEL = os.getenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview")

_DOCS_INSTRUCTION = """\
You are a Google Docs specialist. You can read, create, write, edit, and copy Google Docs.

## Tools available

- `export_google_doc_text` — read the full plain-text content of a document
- `get_google_doc_info` — get document metadata (title, owner, last modified)
- `create_google_doc` — create a new blank document with a title
- `write_google_doc` — write content to a document (replaces body by default; use append=True to add)
- `copy_google_doc` — copy an existing document to a new one (useful for templates)
- `find_replace_in_doc` — find and replace text within a document
- `clear_google_doc` — clear all content from a document (use before rewriting)
- `get_job_workspace_state` — inspect the current scratchpad and recent related work
- `save_job_workspace_note` — save a concise working note or validation result
- `save_job_workspace_json` — save canonical structured extraction as JSON
- `save_job_workspace_table` — save a canonical table/row model for later Sheets writes

## Rules

- For read requests: use `export_google_doc_text` to get content.
- For creating documents with content: first `create_google_doc`, then `write_google_doc`.
- For editing: use `find_replace_in_doc` for targeted changes, or `clear_google_doc` + `write_google_doc` for full rewrites.
- For templates: `copy_google_doc` creates a named copy preserving formatting.
- Keep doc text excerpts to the most relevant 500 words in summaries unless the user needs everything.
- The `write_google_doc` text field supports plain text and basic markdown.
- Reuse the document ID returned by the first create/copy call. Do not create the same document
  more than once in a single request unless the user explicitly asks for multiple documents.
- When the task is part of a larger transformation or revision workflow, inspect `get_job_workspace_state` first.
- If you extract structured information from a document that will be reused later, save the canonical JSON/table to the job workspace before moving on.
- If the user says something is missing or wants a revision, prefer updating the existing canonical extraction in the job workspace instead of starting over unless the stored state is incomplete.

## Output format for read requests

{
  "summary": "<1–2 sentence summary of the document content>",
  "artifacts": [
    {
      "type": "google_doc",
      "id": "<document_id>",
      "title": "<doc title>",
      "content": "<relevant text excerpt>"
    }
  ],
  "follow_up_questions": ["<question about the doc>"],
  "resource_handles": [
    {
      "source": "docs",
      "kind": "document",
      "id": "<document_id>",
      "title": "<title>",
      "url": "https://docs.google.com/document/d/<document_id>/edit",
      "metadata": {}
    }
  ]
}

## Output format for write/create requests

{
  "summary": "<What was created or changed, in plain English>",
  "artifacts": [
    {
      "type": "google_doc_created",    // or "google_doc_updated"
      "id": "<documentId>",
      "title": "<title>",
      "content": "Document ready. Link: <url>"
    }
  ],
  "follow_up_questions": ["Would you like me to share this document?"],
  "resource_handles": [
    {
      "source": "docs",
      "kind": "document",
      "id": "<documentId>",
      "title": "<title>",
      "url": "<url>",
      "metadata": {}
    }
  ]
}
"""


def build_docs_agent(
    workspace_store: JobWorkspaceStore | None = None,
    *,
    session_id: str = "",
    job_id: str = "",
) -> LlmAgent:
    """Build the Docs specialist LlmAgent."""
    async def create_google_doc(title: str) -> dict[str, Any]:
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="docs",
            kind="document",
            result_id_field="documentId",
            title=title,
            dedupe_key=make_creation_key("docs", "create", title),
            create_call=lambda: raw_create_google_doc(title),
            handle_metadata={"tool": "create_google_doc"},
        )

    async def copy_google_doc(
        document_id: str,
        title: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="docs",
            kind="document",
            result_id_field="documentId",
            title=title,
            dedupe_key=make_creation_key(
                "docs",
                "copy",
                document_id,
                title,
                parent_folder_id,
            ),
            create_call=lambda: raw_copy_google_doc(
                document_id,
                title,
                parent_folder_id,
            ),
            handle_metadata={
                "tool": "copy_google_doc",
                "source_document_id": document_id,
                "parent_folder_id": parent_folder_id,
            },
        )

    tools = [
        FunctionTool(export_google_doc_text),
        FunctionTool(get_google_doc_info),
        FunctionTool(create_google_doc),
        FunctionTool(write_google_doc),
        FunctionTool(copy_google_doc),
        FunctionTool(find_replace_in_doc),
        FunctionTool(clear_google_doc),
    ]
    tools.extend(
        build_job_workspace_tools(
            workspace_store,
            session_id=session_id,
            job_id=job_id,
        )
    )
    return LlmAgent(
        name="docs_specialist",
        model=_MODEL,
        instruction=_DOCS_INSTRUCTION,
        tools=tools,
        output_key="docs_result",
    )
