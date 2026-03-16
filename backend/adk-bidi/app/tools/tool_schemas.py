"""Shared Pydantic-style schemas for workspace tool inputs and outputs.

These are plain dataclasses/TypedDicts so they work without pydantic dependency.
ADK FunctionTool uses Python type annotations for schema generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Gmail ─────────────────────────────────────────────────────────────────────

@dataclass
class GmailThreadSummary:
    thread_id: str
    subject: str
    sender: str
    snippet: str
    unread: bool
    date_header: str
    internal_date: str | None


@dataclass
class GmailSearchResult:
    query: str
    focus: str
    threads: list[GmailThreadSummary] = field(default_factory=list)
    error: str | None = None


# ── Drive ─────────────────────────────────────────────────────────────────────

@dataclass
class DriveFileSummary:
    file_id: str
    name: str
    mime_type: str
    modified_time: str | None
    web_view_link: str | None
    owners: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class DriveSearchResult:
    query: str
    files: list[DriveFileSummary] = field(default_factory=list)
    error: str | None = None


# ── Calendar ──────────────────────────────────────────────────────────────────

@dataclass
class CalendarEventSummary:
    event_id: str
    title: str
    start: dict[str, Any]
    end: dict[str, Any]
    location: str = ""
    description: str = ""
    html_link: str | None = None
    attendees: list[dict[str, str]] = field(default_factory=list)


@dataclass
class CalendarListResult:
    time_min: str
    time_max: str
    events: list[CalendarEventSummary] = field(default_factory=list)
    error: str | None = None


# ── Docs ──────────────────────────────────────────────────────────────────────

@dataclass
class DocContent:
    document_id: str
    title: str
    text: str
    error: str | None = None


@dataclass
class DocCreated:
    document_id: str
    title: str
    url: str | None = None
    error: str | None = None
