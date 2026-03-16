"""CalendarAgent — ADK specialist for all Google Calendar operations (read and write)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.job_workspace import JobWorkspaceStore
from app.tools.job_workspace_tools import build_job_workspace_tools
from app.tools.workspace_tools import (
    create_calendar_event,
    delete_calendar_event,
    get_calendar_event,
    get_calendar_freebusy,
    list_calendar_events,
    search_calendar_events,
    update_calendar_event,
)

log = logging.getLogger("athena.adk_agents.specialists.calendar")

_MODEL = os.getenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview")
_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _rfc3339_window(days_ahead: int = 3) -> tuple[str, str]:
    """Return (time_min, time_max) in RFC3339 for the next N days."""
    now = datetime.now(tz=_LOCAL_TZ)
    end = now + timedelta(days=days_ahead)
    fmt = "%Y-%m-%dT%H:%M:%S%z"
    return now.strftime(fmt), end.strftime(fmt)


_CALENDAR_INSTRUCTION = """\
You are a Google Calendar specialist. You can read, create, update, delete, search events,
and check free/busy availability.

## Tools available

- `list_calendar_events` — list events in a time window (time_min/time_max in RFC3339)
- `get_calendar_event` — fetch full details of a specific event by ID
- `search_calendar_events` — search events by keyword (title, description)
- `get_calendar_freebusy` — check free/busy blocks for scheduling
- `create_calendar_event` — create a new event
- `update_calendar_event` — update an existing event (only provided fields change)
- `delete_calendar_event` — delete an event permanently
- `get_job_workspace_state` — inspect the current scratchpad and recent related work
- `save_job_workspace_note` — save chosen slots, attendee plans, or event payload notes
- `save_job_workspace_json` — save structured event plans or scheduling output as JSON
- `save_job_workspace_table` — save slot tables or scheduling matrices when useful

## Time rules

All times must be RFC3339, e.g. "2026-03-10T14:00:00-05:00".

- "today": today 00:00 to 23:59 local time
- "tomorrow": tomorrow 00:00 to 23:59 local time
- "this week": today to 7 days out
- "upcoming" / "next few days": now to 3 days out
- If the user says "10am", assume local timezone unless otherwise stated.

## Rules for updates

- Use `update_calendar_event` when modifying an existing event — requires the event_id.
- To find an event to update: use `search_calendar_events` or `list_calendar_events` first.
- Only pass the fields that need to change; omit everything else.
- Use `attendee_emails` to REPLACE the full attendee list; use `add_attendee_emails` to ADD.
- When handling a continuation or edit request, inspect `get_job_workspace_state` first to reuse event IDs and planned payloads.
- Save chosen slots or event payloads when later corrections are likely.

## Output format for read requests

{
  "summary": "<brief summary of events found>",
  "artifacts": [
    {
      "type": "calendar_event",
      "id": "<event_id>",
      "title": "<event title>",
      "content": "<date, time, location, description, attendees>"
    }
  ],
  "follow_up_questions": ["<question>"],
  "resource_handles": [
    {
      "source": "calendar",
      "kind": "event",
      "id": "<event_id>",
      "title": "<event title>",
      "url": "<htmlLink>",
      "metadata": {"start": {}, "end": {}, "location": "", "attendees": []}
    }
  ]
}

## Output format for write requests

{
  "summary": "Created/Updated/Deleted '<title>' on <date> at <time>.",
  "artifacts": [
    {
      "type": "calendar_event_created",    // or "calendar_event_updated" | "calendar_event_deleted"
      "id": "<event_id>",
      "title": "<title>",
      "content": "Done. Link: <htmlLink if available>"
    }
  ],
  "follow_up_questions": [],
  "resource_handles": []
}

If an operation fails, include "error" field and explain clearly in summary.
"""


def build_calendar_agent(
    workspace_store: JobWorkspaceStore | None = None,
    *,
    session_id: str = "",
    job_id: str = "",
) -> LlmAgent:
    """Build the Calendar specialist LlmAgent."""
    tools = [
        FunctionTool(list_calendar_events),
        FunctionTool(get_calendar_event),
        FunctionTool(search_calendar_events),
        FunctionTool(get_calendar_freebusy),
        FunctionTool(create_calendar_event),
        FunctionTool(update_calendar_event),
        FunctionTool(delete_calendar_event),
    ]
    tools.extend(
        build_job_workspace_tools(
            workspace_store,
            session_id=session_id,
            job_id=job_id,
        )
    )
    return LlmAgent(
        name="calendar_specialist",
        model=_MODEL,
        instruction=_CALENDAR_INSTRUCTION,
        tools=tools,
        output_key="calendar_result",
    )
