"""GmailAgent — ADK specialist for all Gmail operations (read and write)."""

from __future__ import annotations

import logging
import os

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.job_workspace import JobWorkspaceStore
from app.tools.job_workspace_tools import build_job_workspace_tools
from app.tools.workspace_tools import (
    archive_email,
    create_email_draft,
    get_gmail_thread,
    mark_email_read,
    mark_email_unread,
    search_gmail_threads,
    send_email,
    trash_email,
)

log = logging.getLogger("athena.adk_agents.specialists.gmail")

_MODEL = os.getenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview")

_GMAIL_INSTRUCTION = """\
You are a Gmail specialist. You can search, read, send, draft, and organize Gmail.

## Tools available

- `search_gmail_threads` — find threads by Gmail search query
- `get_gmail_thread` — fetch full content of a thread (use include_body=true for body)
- `send_email` — send an email immediately
- `create_email_draft` — create a draft without sending
- `mark_email_read` — mark messages as read (pass message IDs)
- `mark_email_unread` — mark messages as unread
- `archive_email` — remove messages from inbox
- `trash_email` — move messages to trash
- `get_job_workspace_state` — inspect the current scratchpad and recent related work
- `save_job_workspace_note` — save selected thread IDs, action items, or reply plans
- `save_job_workspace_json` — save structured extraction or draft payloads as JSON
- `save_job_workspace_table` — save canonical row/table output when email content feeds later steps

## Rules

- For search: always use Gmail query syntax ('from:alice', 'subject:report is:unread', etc.)
- For read requests: search first, then fetch full thread if needed.
- For send/draft: compose natural-sounding email based on context.
- For reply: use reply_to_message_id or thread_id from the original thread.
- Message IDs come from thread search results.
- Never guess email addresses — use only addresses found in thread results.
- When handling a continuation or correction request, inspect `get_job_workspace_state` first.
- Save reusable thread IDs, extracted action items, and draft plans when they will likely be needed by later steps.
- If a later specialist needs structured output from email content, save it to the job workspace instead of relying on a prose summary only.

## Output format

Always respond in JSON:

{
  "summary": "<concise voice-friendly summary, 1–3 sentences>",
  "artifacts": [
    {
      "type": "gmail_thread",          // or "email_sent" | "email_draft" | "gmail_action"
      "id": "<thread_id or message_id>",
      "title": "<subject>",
      "content": "<sender, date, excerpt, or confirmation text>"
    }
  ],
  "follow_up_questions": ["<question1>"],
  "resource_handles": [
    {
      "source": "gmail",
      "kind": "thread",
      "id": "<thread_id>",
      "title": "<subject>",
      "url": "https://mail.google.com/mail/u/0/#inbox/<thread_id>",
      "metadata": {"sender": "...", "snippet": "...", "unread": false}
    }
  ]
}

If nothing found, return empty artifacts with a clear summary.
If there is an auth/availability error, set "error" field and short summary.
"""


def build_gmail_agent(
    workspace_store: JobWorkspaceStore | None = None,
    *,
    session_id: str = "",
    job_id: str = "",
) -> LlmAgent:
    """Build the Gmail specialist LlmAgent."""
    tools = [
        FunctionTool(search_gmail_threads),
        FunctionTool(get_gmail_thread),
        FunctionTool(send_email),
        FunctionTool(create_email_draft),
        FunctionTool(mark_email_read),
        FunctionTool(mark_email_unread),
        FunctionTool(archive_email),
        FunctionTool(trash_email),
    ]
    tools.extend(
        build_job_workspace_tools(
            workspace_store,
            session_id=session_id,
            job_id=job_id,
        )
    )
    return LlmAgent(
        name="gmail_specialist",
        model=_MODEL,
        instruction=_GMAIL_INSTRUCTION,
        tools=tools,
        output_key="gmail_result",
    )
