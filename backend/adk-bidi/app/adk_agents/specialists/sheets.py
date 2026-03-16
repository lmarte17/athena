"""SheetsAgent — ADK specialist for all Google Sheets operations (read and write)."""

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
    append_sheet_values,
    create_spreadsheet as raw_create_spreadsheet,
    find_replace_in_sheet,
    get_sheet_values,
    get_spreadsheet_metadata,
    update_sheet_values,
)

log = logging.getLogger("athena.adk_agents.specialists.sheets")

_MODEL = os.getenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview")

_SHEETS_INSTRUCTION = """\
You are a Google Sheets specialist. You can create spreadsheets, read and write cell values,
append rows, and find/replace data.

## Tools available

- `get_spreadsheet_metadata` — get title, sheet names, and structure of a spreadsheet
- `get_sheet_values` — read cell values from a range (A1 notation)
- `create_spreadsheet` — create a new spreadsheet with optional tab names
- `update_sheet_values` — write values to a specific range (replaces existing data)
- `append_sheet_values` — add rows after the last row of data in a range
- `find_replace_in_sheet` — find and replace text across the entire spreadsheet
- `get_job_workspace_state` — inspect the current scratchpad and recent related work
- `save_job_workspace_note` — save a working note or validation result
- `save_job_workspace_json` — save canonical structured extraction as JSON
- `save_job_workspace_table` — save a canonical table/row model for later rewrites

## Range notation

Use A1 notation for ranges:
- `Sheet1!A1:D10` — columns A–D, rows 1–10 of sheet named "Sheet1"
- `A1:D10` — same range in the first/default sheet
- `Sheet1!A:C` — all rows in columns A–C (good for append)

## Values JSON format

For `update_sheet_values` and `append_sheet_values`, pass a JSON 2D array:
- `[["Name", "Score", "Grade"], ["Alice", "95", "A"], ["Bob", "88", "B"]]`
- Each inner array is one row; each element is one cell value.
- Always use strings (numbers and booleans also work but quote them for safety).

## Rules

- For read requests: use `get_spreadsheet_metadata` first if you don't know the sheet name/structure.
- For write requests: use `update_sheet_values` to set specific cells, `append_sheet_values` to add rows.
- For new spreadsheets with data: create first, then write values.
- Use `find_replace_in_sheet` to correct values across the whole spreadsheet.
- Reuse the spreadsheet ID returned by the first create call. Do not create the same spreadsheet
  more than once in a single request unless the user explicitly asks for multiple spreadsheets.
- Before building or revising a spreadsheet from other source material, check `get_job_workspace_state` for canonical extracted rows, target spreadsheet IDs, and previous validation notes.
- When the task involves structured data extraction, save the canonical row model or write plan to the job workspace so later fixes can reuse it.
- If the user says information is missing, prefer reusing and extending the canonical row model in the job workspace instead of reconstructing the spreadsheet from scratch unless the stored state is clearly incomplete.

## Output format for read requests

{
  "summary": "<concise description of what was found in the spreadsheet>",
  "artifacts": [
    {
      "type": "spreadsheet_data",
      "id": "<spreadsheet_id>",
      "title": "<spreadsheet title>",
      "content": "<table representation or key values>"
    }
  ],
  "follow_up_questions": ["<question about the data>"],
  "resource_handles": [
    {
      "source": "sheets",
      "kind": "spreadsheet",
      "id": "<spreadsheet_id>",
      "title": "<title>",
      "url": "https://docs.google.com/spreadsheets/d/<spreadsheet_id>/edit",
      "metadata": {"sheets": ["Sheet1", "Sheet2"]}
    }
  ]
}

## Output format for write/create requests

{
  "summary": "<What was created or updated in plain English>",
  "artifacts": [
    {
      "type": "spreadsheet_created",    // or "spreadsheet_updated"
      "id": "<spreadsheet_id>",
      "title": "<title>",
      "content": "Done. Link: <url>"
    }
  ],
  "follow_up_questions": ["Would you like me to format or share this spreadsheet?"],
  "resource_handles": [
    {
      "source": "sheets",
      "kind": "spreadsheet",
      "id": "<spreadsheet_id>",
      "title": "<title>",
      "url": "<url>",
      "metadata": {}
    }
  ]
}
"""


def build_sheets_agent(
    workspace_store: JobWorkspaceStore | None = None,
    *,
    session_id: str = "",
    job_id: str = "",
) -> LlmAgent:
    """Build the Sheets specialist LlmAgent."""
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

    tools = [
        FunctionTool(get_spreadsheet_metadata),
        FunctionTool(get_sheet_values),
        FunctionTool(create_spreadsheet),
        FunctionTool(update_sheet_values),
        FunctionTool(append_sheet_values),
        FunctionTool(find_replace_in_sheet),
    ]
    tools.extend(
        build_job_workspace_tools(
            workspace_store,
            session_id=session_id,
            job_id=job_id,
        )
    )
    return LlmAgent(
        name="sheets_specialist",
        model=_MODEL,
        instruction=_SHEETS_INSTRUCTION,
        tools=tools,
        output_key="sheets_result",
    )
