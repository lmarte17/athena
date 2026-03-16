"""ActionAgent — ADK specialist for confirmed write operations across all Workspace services.

Handles create/write/send/delete actions after the user or coordinator has confirmed intent.
This agent is the "commit" layer — it executes, not plans.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.job_workspace import JobWorkspaceStore
from app.tools.job_workspace_tools import build_job_workspace_tools
from app.tools.guarded_creation_tools import (
    guard_resource_creation,
    make_creation_key,
    reject_implicit_blank_presentation,
)
from app.tools.workspace_tools import (
    add_image_slide,
    apply_presentation_theme,
    append_slide_element_text,
    append_sheet_values,
    clear_slide_element_text,
    clear_slide_notes,
    copy_presentation as raw_copy_presentation,
    create_slide,
    create_calendar_event,
    create_google_doc as raw_create_google_doc,
    create_presentation as raw_create_presentation,
    create_presentation_from_markdown as raw_create_presentation_from_markdown,
    create_presentation_from_template as raw_create_presentation_from_template,
    create_spreadsheet as raw_create_spreadsheet,
    delete_calendar_event,
    delete_presentation_slide,
    delete_drive_file,
    duplicate_slide,
    fill_presentation_template,
    get_slide_element_text,
    get_slide_notes,
    inspect_presentation,
    inspect_presentation_template,
    inspect_slide,
    inspect_slide_element,
    insert_slide_image,
    list_slide_elements,
    reorder_slide,
    replace_slide_image_element,
    replace_text_in_presentation,
    replace_slide_image,
    send_email,
    resize_slide_image,
    set_slide_background,
    set_slide_element_text,
    update_calendar_event,
    update_slide_notes,
    update_sheet_values,
    write_google_doc,
)

log = logging.getLogger("athena.adk_agents.specialists.action")

_MODEL = os.getenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview")

_ACTION_INSTRUCTION = """\
You are an action specialist. You execute confirmed write operations across Google Workspace.

## Your job

Execute the write action described in the request — no planning, no clarification loops.
If required information is clearly missing, report it without calling any tool.
When a continuation or correction request references prior work, inspect `get_job_workspace_state`
first and reuse stored resource IDs and structured payloads before creating new resources.
Never create the same document, spreadsheet, or presentation more than once in a single request
unless the user explicitly asked for multiple outputs.
Never use `create_presentation` as a fallback when slide markdown generation fails. If a content
deck cannot be created, report the failure instead of creating a blank presentation.
You can revise an existing presentation only with the slide tools listed below. Inspect first
when a write depends on stable slide or element IDs. Do not claim you can edit the Slides
master/theme system beyond the available deck-wide style layer.

## Available actions

**Gmail**
- `send_email` — send an email

**Calendar**
- `create_calendar_event` — create a new event
- `update_calendar_event` — update an existing event
- `delete_calendar_event` — delete an event

**Docs**
- `create_google_doc` — create a new blank document
- `write_google_doc` — write content to a document

**Sheets**
- `create_spreadsheet` — create a new spreadsheet
- `update_sheet_values` — write values to a range
- `append_sheet_values` — append rows to a sheet

**Slides**
- `create_presentation` — create a blank presentation shell only when the user explicitly asks for a blank deck; requires `allow_blank=true`
- `create_presentation_from_markdown` — create a full deck from markdown
- `create_presentation_from_template` — create a deck from a Slides template with placeholder replacements
- `copy_presentation` — copy an existing presentation
- `inspect_presentation` — inspect the full deck and discover stable IDs before editing
- `inspect_slide` — inspect one slide in detail
- `list_slide_elements` — list element IDs on a slide
- `inspect_slide_element` — inspect one element in detail
- `replace_text_in_presentation` — replace repeated text or template tokens across the deck
- `set_slide_element_text` — replace text in one shape element
- `append_slide_element_text` — append text to one shape element
- `clear_slide_element_text` — clear text from one shape element
- `get_slide_element_text` — read text from one shape element
- `add_image_slide` — add a full-bleed image slide
- `replace_slide_image` — replace an existing slide with a full-bleed image
- `update_slide_notes` — update speaker notes on a slide
- `get_slide_notes` — read speaker notes on a slide
- `clear_slide_notes` — remove speaker notes from a slide
- `create_slide` — insert a slide into an existing deck
- `duplicate_slide` — duplicate an existing slide
- `reorder_slide` — move a slide to a new index
- `set_slide_background` — change one slide's background color
- `insert_slide_image` — insert an image element on a slide
- `replace_slide_image_element` — replace an existing image element
- `resize_slide_image` — move or resize an image element
- `inspect_presentation_template` — inspect template placeholders in an existing deck
- `fill_presentation_template` — fill template placeholders in an existing deck
- `apply_presentation_theme` — apply a deck-wide style preset or explicit theme spec
- `delete_presentation_slide` — delete a slide from a presentation

**Drive**
- `delete_drive_file` — move a file to trash

**Scratchpad**
- `get_job_workspace_state` — inspect prior plans, IDs, and structured job state
- `save_job_workspace_note` — save execution notes or validation output
- `save_job_workspace_json` — save structured write plans or payloads as JSON
- `save_job_workspace_table` — save canonical row models when writes depend on them

## Output format for successful actions

{
  "summary": "<What was done, in plain English>",
  "artifacts": [
    {
      "type": "action_completed",
      "id": "<created/modified resource id>",
      "title": "<resource title>",
      "content": "Done. Link: <url if available>"
    }
  ],
  "follow_up_questions": ["<relevant follow-up>"],
  "resource_handles": [
    {
      "source": "<gmail|calendar|docs|sheets|slides|drive>",
      "kind": "<message|event|document|spreadsheet|presentation|file>",
      "id": "<id>",
      "title": "<title>",
      "url": "<url>",
      "metadata": {}
    }
  ]
}

## Output format when information is missing

{
  "summary": "I need more information to complete this action.",
  "artifacts": [],
  "follow_up_questions": ["<clarifying question>"],
  "resource_handles": [],
  "error": "missing_info: <what is missing>"
}
"""


def build_action_agent(
    workspace_store: JobWorkspaceStore | None = None,
    *,
    session_id: str = "",
    job_id: str = "",
) -> LlmAgent:
    """Build the Action specialist LlmAgent."""
    def _current_user_request() -> str:
        if workspace_store is None or not session_id or not job_id:
            return ""
        workspace = workspace_store.get_workspace(session_id, job_id)
        return workspace.user_request if workspace is not None else ""

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

    async def create_spreadsheet(
        title: str,
        sheets: list[str] | None = None,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        sheet_names = list(sheets or [])
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="sheets",
            kind="spreadsheet",
            result_id_field="spreadsheetId",
            title=title,
            dedupe_key=make_creation_key(
                "sheets",
                "create",
                title,
                sheet_names,
                parent_folder_id,
            ),
            create_call=lambda: raw_create_spreadsheet(
                title,
                sheets=sheet_names,
                parent_folder_id=parent_folder_id,
            ),
            handle_metadata={
                "tool": "create_spreadsheet",
                "sheets": sheet_names,
                "parent_folder_id": parent_folder_id,
            },
        )

    async def create_presentation(
        title: str,
        parent_folder_id: str = "",
        template_id: str = "",
        allow_blank: bool = False,
    ) -> dict[str, Any]:
        blocked = reject_implicit_blank_presentation(
            title=title,
            allow_blank=allow_blank,
            template_id=template_id,
            user_request=_current_user_request(),
        )
        if blocked is not None:
            return blocked
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="slides",
            kind="presentation",
            result_id_field="presentationId",
            title=title,
            dedupe_key=make_creation_key(
                "slides",
                "create",
                title,
                parent_folder_id,
                template_id,
            ),
            create_call=lambda: raw_create_presentation(
                title,
                parent_folder_id=parent_folder_id,
                template_id=template_id,
            ),
            handle_metadata={
                "tool": "create_presentation",
                "parent_folder_id": parent_folder_id,
                "template_id": template_id,
                "allow_blank": allow_blank,
            },
        )

    async def create_presentation_from_markdown(
        title: str,
        content: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="slides",
            kind="presentation",
            result_id_field="presentationId",
            title=title,
            dedupe_key=make_creation_key(
                "slides",
                "create_markdown",
                title,
                content,
                parent_folder_id,
            ),
            create_call=lambda: raw_create_presentation_from_markdown(
                title,
                content,
                parent_folder_id=parent_folder_id,
            ),
            handle_metadata={
                "tool": "create_presentation_from_markdown",
                "parent_folder_id": parent_folder_id,
            },
        )

    async def create_presentation_from_template(
        template_presentation_id: str,
        title: str,
        replacements_json: str = "{}",
        parent_folder_id: str = "",
        exact_match: bool = False,
    ) -> dict[str, Any]:
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="slides",
            kind="presentation",
            result_id_field="presentationId",
            title=title,
            dedupe_key=make_creation_key(
                "slides",
                "create_template",
                template_presentation_id,
                title,
                replacements_json,
                parent_folder_id,
                exact_match,
            ),
            create_call=lambda: raw_create_presentation_from_template(
                template_presentation_id,
                title,
                replacements_json=replacements_json,
                parent_folder_id=parent_folder_id,
                exact_match=exact_match,
            ),
            handle_metadata={
                "tool": "create_presentation_from_template",
                "template_presentation_id": template_presentation_id,
                "parent_folder_id": parent_folder_id,
                "exact_match": exact_match,
            },
        )

    async def copy_presentation(
        presentation_id: str,
        title: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="slides",
            kind="presentation",
            result_id_field="presentationId",
            title=title,
            dedupe_key=make_creation_key(
                "slides",
                "copy",
                presentation_id,
                title,
                parent_folder_id,
            ),
            create_call=lambda: raw_copy_presentation(
                presentation_id,
                title,
                parent_folder_id=parent_folder_id,
            ),
            handle_metadata={
                "tool": "copy_presentation",
                "source_presentation_id": presentation_id,
                "parent_folder_id": parent_folder_id,
            },
        )

    tools = [
        FunctionTool(send_email),
        FunctionTool(create_calendar_event),
        FunctionTool(update_calendar_event),
        FunctionTool(delete_calendar_event),
        FunctionTool(create_google_doc),
        FunctionTool(write_google_doc),
        FunctionTool(create_spreadsheet),
        FunctionTool(update_sheet_values),
        FunctionTool(append_sheet_values),
        FunctionTool(create_presentation),
        FunctionTool(create_presentation_from_markdown),
        FunctionTool(create_presentation_from_template),
        FunctionTool(copy_presentation),
        FunctionTool(inspect_presentation),
        FunctionTool(inspect_slide),
        FunctionTool(list_slide_elements),
        FunctionTool(inspect_slide_element),
        FunctionTool(replace_text_in_presentation),
        FunctionTool(set_slide_element_text),
        FunctionTool(append_slide_element_text),
        FunctionTool(clear_slide_element_text),
        FunctionTool(get_slide_element_text),
        FunctionTool(add_image_slide),
        FunctionTool(replace_slide_image),
        FunctionTool(update_slide_notes),
        FunctionTool(get_slide_notes),
        FunctionTool(clear_slide_notes),
        FunctionTool(create_slide),
        FunctionTool(duplicate_slide),
        FunctionTool(reorder_slide),
        FunctionTool(set_slide_background),
        FunctionTool(insert_slide_image),
        FunctionTool(replace_slide_image_element),
        FunctionTool(resize_slide_image),
        FunctionTool(inspect_presentation_template),
        FunctionTool(fill_presentation_template),
        FunctionTool(apply_presentation_theme),
        FunctionTool(delete_presentation_slide),
        FunctionTool(delete_drive_file),
    ]
    tools.extend(
        build_job_workspace_tools(
            workspace_store,
            session_id=session_id,
            job_id=job_id,
        )
    )
    return LlmAgent(
        name="action_specialist",
        model=_MODEL,
        instruction=_ACTION_INSTRUCTION,
        tools=tools,
        output_key="action_result",
    )
