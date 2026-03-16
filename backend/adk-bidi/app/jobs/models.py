"""WorkspaceJobRequest and WorkspaceJobResult for background workspace execution.

All background workspace work is described as a WorkspaceJobRequest and completed as a
WorkspaceJobResult. This replaces implicit prompt injection with a typed, traceable flow.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

JobStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
]

JobTypeHint = Literal[
    # Gmail
    "gmail_search",
    "gmail_read",
    "gmail_send",
    "gmail_draft",
    # Drive
    "drive_search",
    "drive_read",
    "drive_write",
    # Docs
    "doc_read",
    "doc_create",
    "doc_write",
    # Calendar
    "calendar_read",
    "calendar_write",
    # Sheets
    "sheets_read",
    "sheets_write",
    # Slides
    "slides_read",
    "slides_create",
    # Meta
    "retrieval",
    "action",
    "general",
]


@dataclass
class WorkspaceJobRequest:
    """Submitted by the live voice agent when it decides background work is needed.

    The coordinator and specialists use this to decide what to do — they never
    parse live transcripts directly.
    """

    # Identity
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Context
    source_turn_id: str = ""          # Which turn triggered this job
    user_request: str = ""            # What the user actually said
    conversation_window: list[dict[str, str]] = field(default_factory=list)
    # [{role: "user"|"assistant", content: "..."}]

    # Hints for routing
    job_type_hint: JobTypeHint = "general"
    resource_hints: list[str] = field(default_factory=list)
    # e.g. ["gmail", "calendar"] — coordinator may ignore and use LLM reasoning

    # Execution policy
    priority: int = 5                 # 1 (highest) – 10 (lowest)
    requires_confirmation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "submitted_at": self.submitted_at.isoformat(),
            "source_turn_id": self.source_turn_id,
            "user_request": self.user_request,
            "conversation_window": self.conversation_window,
            "job_type_hint": self.job_type_hint,
            "resource_hints": self.resource_hints,
            "priority": self.priority,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass
class WorkspaceJobResult:
    """Returned when a background job completes (successfully or not).

    Stored in the job store and injected into the live session as a structured event.
    The live agent instruction tells Athena to consume these events and present results.
    """

    # Identity
    job_id: str = ""
    session_id: str = ""
    source_turn_id: str = ""
    status: JobStatus = "pending"
    completed_at: datetime | None = None

    # Result payload
    summary: str = ""                 # Short human-readable summary for voice
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # e.g. [{type: "gmail_thread", id: "...", title: "...", content: "..."}]

    resource_handles: list[dict[str, Any]] = field(default_factory=list)
    # Serialized ResourceHandle dicts for SessionResourceStore updates

    follow_up_questions: list[str] = field(default_factory=list)
    # Suggested follow-up questions Athena can offer

    action_proposals: list[dict[str, Any]] = field(default_factory=list)
    # Pending write actions requiring confirmation

    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "source_turn_id": self.source_turn_id,
            "status": self.status,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "resource_handles": self.resource_handles,
            "follow_up_questions": self.follow_up_questions,
            "action_proposals": self.action_proposals,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceJobResult":
        result = cls()
        result.job_id = str(data.get("job_id") or "")
        result.session_id = str(data.get("session_id") or "")
        result.source_turn_id = str(data.get("source_turn_id") or "")
        result.status = data.get("status", "pending")
        raw_completed = data.get("completed_at")
        if raw_completed:
            try:
                result.completed_at = datetime.fromisoformat(raw_completed)
            except (ValueError, TypeError):
                pass
        result.summary = str(data.get("summary") or "")
        result.artifacts = list(data.get("artifacts") or [])
        result.resource_handles = list(data.get("resource_handles") or [])
        result.follow_up_questions = list(data.get("follow_up_questions") or [])
        result.action_proposals = list(data.get("action_proposals") or [])
        result.error = data.get("error")
        return result
