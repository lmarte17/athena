"""Background hydration for calendar event details."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from html import unescape
from typing import Any

from app.gog_client import run_gog_json
from app.hydration_types import HydrationResult
from app.resource_store import ResourceHandle


class CalendarHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "calendar" and handle.kind == "event"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        calendar_id = str(
            handle.metadata.get("calendar_id") or os.getenv("ATHENA_CALENDAR_ID", "primary")
        )
        result = await run_gog_json("calendar", "get", calendar_id, handle.id)
        # gog returns {"event": {...}} — unwrap to get the event object directly.
        event = result.get("event", result)

        normalized_text = _normalize_event_text(event)
        if not normalized_text:
            return None

        relations = tuple(_extract_relations(event))
        attendees = _normalize_attendees(event.get("attendees"))
        description = _clean_text(str(event.get("description") or ""))
        version = str(event.get("updated") or "").strip() or handle.version

        return HydrationResult(
            handle=replace(
                handle,
                title=str(event.get("summary") or handle.title),
                url=_string_or_none(event.get("htmlLink") or event.get("hangoutLink")) or handle.url,
                version=version,
                metadata={
                    **handle.metadata,
                    "calendar_id": calendar_id,
                    "description": description,
                    "attendees": attendees,
                },
            ),
            normalized_text=normalized_text,
            metadata={
                "calendar_id": calendar_id,
                "description": description,
                "attendees": attendees,
            },
            relations=relations,
        )


def _normalize_event_text(event: dict[str, Any]) -> str:
    title = str(event.get("summary") or event.get("title") or "").strip()
    if not title:
        return ""

    lines = [f"Event: {title}"]

    start = _event_datetime_value(event.get("start"))
    end = _event_datetime_value(event.get("end"))
    if start:
        lines.append(f"Start: {start}")
    if end:
        lines.append(f"End: {end}")

    location = str(event.get("location") or "").strip()
    if location:
        lines.append(f"Location: {location}")

    organizer = _organizer_label(event.get("organizer"))
    if organizer:
        lines.append(f"Organizer: {organizer}")

    attendees = _normalize_attendees(event.get("attendees"))
    if attendees:
        attendee_labels = [attendee.get("display_name") or attendee.get("email") for attendee in attendees]
        lines.append(f"Attendees: {', '.join(label for label in attendee_labels if label)}")

    description = _clean_text(str(event.get("description") or ""))
    if description:
        lines.extend(["", "Description:", description])

    return "\n".join(lines).strip()


def _event_datetime_value(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("dateTime"):
            return str(value["dateTime"])
        if value.get("date"):
            return str(value["date"])
    if value:
        return str(value)
    return ""


def _organizer_label(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    display_name = str(value.get("displayName") or "").strip()
    email = str(value.get("email") or "").strip()
    if display_name and email:
        return f"{display_name} <{email}>"
    return display_name or email


def _normalize_attendees(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    attendees: list[dict[str, str]] = []
    for attendee in value:
        if not isinstance(attendee, dict):
            continue
        normalized: dict[str, str] = {}
        if attendee.get("displayName"):
            normalized["display_name"] = str(attendee["displayName"])
        if attendee.get("email"):
            normalized["email"] = str(attendee["email"])
        if attendee.get("responseStatus"):
            normalized["response_status"] = str(attendee["responseStatus"])
        if normalized:
            attendees.append(normalized)
    return attendees


def _extract_relations(event: dict[str, Any]) -> list[ResourceHandle]:
    handles: list[ResourceHandle] = []
    seen: set[tuple[str, str, str]] = set()

    description = str(event.get("description") or "")
    for source, kind, resource_id, url in _google_workspace_links(description):
        key = (source, kind, resource_id)
        if key in seen:
            continue
        seen.add(key)
        handles.append(
            ResourceHandle(
                source=source,
                kind=kind,
                id=resource_id,
                title=f"Linked {kind}",
                url=url,
                metadata={"discovered_via": "calendar_description"},
            )
        )

    attachments = event.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            file_url = str(attachment.get("fileUrl") or "").strip()
            title = str(attachment.get("title") or "").strip() or "Linked file"
            match = re.search(r"/d/([A-Za-z0-9_-]+)", file_url)
            if not match:
                continue
            resource_id = match.group(1)
            key = ("drive", "file", resource_id)
            if key in seen:
                continue
            seen.add(key)
            handles.append(
                ResourceHandle(
                    source="drive",
                    kind="file",
                    id=resource_id,
                    title=title,
                    url=file_url,
                    metadata={"discovered_via": "calendar_attachment"},
                )
            )

    return handles


def _google_workspace_links(text: str) -> list[tuple[str, str, str, str]]:
    patterns = (
        (r"https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)", "docs", "document"),
        (r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", "sheets", "spreadsheet"),
        (r"https://docs\.google\.com/presentation/d/([A-Za-z0-9_-]+)", "slides", "presentation"),
        (r"https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)", "drive", "file"),
    )

    matches: list[tuple[str, str, str, str]] = []
    for pattern, source, kind in patterns:
        for match in re.finditer(pattern, text):
            matches.append((source, kind, match.group(1), match.group(0)))
    return matches


def _clean_text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    lines = [line.strip() for line in text.replace("\r\n", "\n").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _string_or_none(value: Any) -> str | None:
    if value in ("", None):
        return None
    return str(value)
