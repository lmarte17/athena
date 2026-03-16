"""Typed ADK FunctionTool wrappers around WorkspaceBackend operations.

Each function here is registered as a tool on specialist ADK agents.
All workspace API calls go through the hybrid workspace backend provider.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.gog_client import GogAuthError, GogError, GogUnavailableError
from app.slides_agent_client import (
    SlidesAgentAuthError,
    SlidesAgentError,
    SlidesAgentUnavailableError,
)
from app.tools.workspace_backends import get_workspace_backend

log = logging.getLogger("athena.tools.workspace_tools")


def _backend():
    return get_workspace_backend()


def _workspace_err(empty: Any, exc: Exception) -> dict[str, Any]:
    """Return a normalized error dict."""
    if isinstance(exc, GogUnavailableError):
        return {**empty, "error": "workspace_unavailable"}
    if isinstance(exc, GogAuthError):
        return {**empty, "error": "workspace_auth_error"}
    return {**empty, "error": str(exc)}


def _slides_agent_err(exc: SlidesAgentError) -> dict[str, Any]:
    """Return a normalized slides-agent error dict."""
    payload = dict(getattr(exc, "payload", {}) or {})
    error_code = str(payload.get("error_code") or "").strip() or "api_error"
    detail = str(payload.get("detail") or exc).strip() or str(exc)
    response = {
        "ok": False,
        "error_code": error_code,
        "detail": detail,
        "isRetryable": error_code in {"api_error", "rate_limited", "conflict"},
    }

    hint = str(payload.get("hint") or "").strip()
    if hint:
        response["suggestedAction"] = hint

    if isinstance(exc, SlidesAgentUnavailableError):
        response["error_code"] = "slides_agent_unavailable"
        response["detail"] = detail or "slides-agent is not installed or not on PATH."
        response["isRetryable"] = False
        response.setdefault("suggestedAction", "install_slides_agent")
    elif isinstance(exc, SlidesAgentAuthError):
        response["error_code"] = "auth_error"
        response["isRetryable"] = False
        response.setdefault("suggestedAction", "slides_agent_auth_login")

    return response


def _resource_create_response(
    result: dict[str, Any],
    *,
    id_field: str,
    id_keys: tuple[str, ...],
    title: str,
    url_template: str,
    missing_id_error: str,
) -> dict[str, Any]:
    raw_error = str(result.get("error") or "").strip()
    if raw_error:
        return {id_field: "", "title": title, "error": raw_error}

    resource_id = ""
    for key in id_keys:
        candidate = str(result.get(key) or "").strip()
        if candidate:
            resource_id = candidate
            break

    if not resource_id:
        return {id_field: "", "title": title, "error": missing_id_error}

    return {
        id_field: resource_id,
        "title": title,
        "url": url_template.format(resource_id=resource_id),
    }


# ── Gmail — read ──────────────────────────────────────────────────────────────


async def search_gmail_threads(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search Gmail threads using a Gmail search query string.

    Args:
        query: Gmail search query (e.g. 'from:alice is:unread', 'subject:budget in:inbox').
        max_results: Maximum number of threads to return (default 5).

    Returns:
        Dict with 'threads' list and optional 'error' key.
    """
    try:
        return await _backend().search_gmail_threads(query=query, max_results=max_results)
    except GogError as exc:
        log.warning("search_gmail_threads failed: %s", exc)
        return _workspace_err({"threads": []}, exc)


async def get_gmail_thread(thread_id: str, include_body: bool = False) -> dict[str, Any]:
    """Get a single Gmail thread by ID, with headers and optional body.

    Args:
        thread_id: Gmail thread ID.
        include_body: If True, fetch full message body; otherwise headers only.

    Returns:
        Dict with thread details or 'error' key.
    """
    fmt = "full" if include_body else "metadata"
    headers = ["From", "To", "Subject", "Date"]
    try:
        return await _backend().get_gmail_thread(
            thread_id=thread_id, format=fmt, metadata_headers=headers
        )
    except GogError as exc:
        log.warning("get_gmail_thread failed for %s: %s", thread_id, exc)
        return {"error": str(exc)}


# ── Gmail — write ─────────────────────────────────────────────────────────────


async def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    reply_to_message_id: str = "",
    thread_id: str = "",
) -> dict[str, Any]:
    """Send an email via Gmail.

    Args:
        to: Recipient email(s), comma-separated.
        subject: Email subject.
        body: Plain-text body.
        cc: CC recipients, comma-separated (optional).
        bcc: BCC recipients, comma-separated (optional).
        reply_to_message_id: If replying to a specific message, pass its Gmail message ID.
        thread_id: If replying within a thread, pass the Gmail thread ID.

    Returns:
        Dict with send confirmation or 'error' key.
    """
    try:
        return await _backend().send_email(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_message_id=reply_to_message_id,
            thread_id=thread_id,
        )
    except GogError as exc:
        log.warning("send_email failed: %s", exc)
        return {"error": str(exc)}


async def create_email_draft(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    reply_to_message_id: str = "",
) -> dict[str, Any]:
    """Create a Gmail draft without sending it.

    Args:
        to: Recipient email(s), comma-separated.
        subject: Email subject.
        body: Plain-text body.
        cc: CC recipients, comma-separated (optional).
        bcc: BCC recipients, comma-separated (optional).
        reply_to_message_id: If drafting a reply, pass the original Gmail message ID.

    Returns:
        Dict with draft ID and details, or 'error' key.
    """
    try:
        return await _backend().create_email_draft(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_message_id=reply_to_message_id,
        )
    except GogError as exc:
        log.warning("create_email_draft failed: %s", exc)
        return {"error": str(exc)}


async def mark_email_read(message_ids: list[str]) -> dict[str, Any]:
    """Mark Gmail messages as read.

    Args:
        message_ids: List of Gmail message IDs to mark read.

    Returns:
        Dict confirming the action or 'error' key.
    """
    try:
        return await _backend().mark_email_read(message_ids=message_ids)
    except GogError as exc:
        log.warning("mark_email_read failed: %s", exc)
        return {"error": str(exc)}


async def mark_email_unread(message_ids: list[str]) -> dict[str, Any]:
    """Mark Gmail messages as unread.

    Args:
        message_ids: List of Gmail message IDs to mark unread.

    Returns:
        Dict confirming the action or 'error' key.
    """
    try:
        return await _backend().mark_email_unread(message_ids=message_ids)
    except GogError as exc:
        log.warning("mark_email_unread failed: %s", exc)
        return {"error": str(exc)}


async def archive_email(message_ids: list[str]) -> dict[str, Any]:
    """Archive Gmail messages (remove from inbox).

    Args:
        message_ids: List of Gmail message IDs to archive.

    Returns:
        Dict confirming the action or 'error' key.
    """
    try:
        return await _backend().archive_email(message_ids=message_ids)
    except GogError as exc:
        log.warning("archive_email failed: %s", exc)
        return {"error": str(exc)}


async def trash_email(message_ids: list[str]) -> dict[str, Any]:
    """Move Gmail messages to trash.

    Args:
        message_ids: List of Gmail message IDs to trash.

    Returns:
        Dict confirming the action or 'error' key.
    """
    try:
        return await _backend().trash_email(message_ids=message_ids)
    except GogError as exc:
        log.warning("trash_email failed: %s", exc)
        return {"error": str(exc)}


# ── Drive — read ──────────────────────────────────────────────────────────────


async def search_drive_files(query: str, page_size: int = 5) -> dict[str, Any]:
    """Search Google Drive files by title or content.

    Args:
        query: Drive search query using Drive query syntax
               (e.g. "name contains 'budget' and trashed = false").
        page_size: Maximum number of files to return (default 5).

    Returns:
        Dict with 'files' list and optional 'error' key.
    """
    fields = "files(id,name,mimeType,modifiedTime,webViewLink,owners(displayName),description)"
    try:
        return await _backend().search_drive_files(
            query=query, page_size=page_size, fields=fields
        )
    except GogError as exc:
        log.warning("search_drive_files failed: %s", exc)
        return _workspace_err({"files": []}, exc)


async def get_drive_file(file_id: str) -> dict[str, Any]:
    """Get Google Drive file metadata by ID.

    Args:
        file_id: Drive file ID.

    Returns:
        Dict with file metadata or 'error' key.
    """
    fields = "id,name,mimeType,modifiedTime,webViewLink,owners(displayName),description"
    try:
        return await _backend().get_drive_file(file_id=file_id, fields=fields)
    except GogError as exc:
        log.warning("get_drive_file failed for %s: %s", file_id, exc)
        return {"error": str(exc)}


# ── Drive — write ─────────────────────────────────────────────────────────────


async def copy_drive_file(
    file_id: str, name: str, parent_folder_id: str = ""
) -> dict[str, Any]:
    """Copy a Google Drive file.

    Args:
        file_id: ID of the file to copy.
        name: Name for the new copy.
        parent_folder_id: Optional destination folder ID.

    Returns:
        Dict with new file details or 'error' key.
    """
    try:
        return await _backend().copy_drive_file(
            file_id=file_id, name=name, parent_folder_id=parent_folder_id
        )
    except GogError as exc:
        log.warning("copy_drive_file failed for %s: %s", file_id, exc)
        return {"error": str(exc)}


async def move_drive_file(file_id: str, parent_folder_id: str) -> dict[str, Any]:
    """Move a Google Drive file to a different folder.

    Args:
        file_id: ID of the file to move.
        parent_folder_id: Destination folder ID.

    Returns:
        Dict with updated file details or 'error' key.
    """
    try:
        return await _backend().move_drive_file(
            file_id=file_id, parent_folder_id=parent_folder_id
        )
    except GogError as exc:
        log.warning("move_drive_file failed for %s: %s", file_id, exc)
        return {"error": str(exc)}


async def rename_drive_file(file_id: str, new_name: str) -> dict[str, Any]:
    """Rename a Google Drive file or folder.

    Args:
        file_id: ID of the file or folder to rename.
        new_name: New name.

    Returns:
        Dict with updated file details or 'error' key.
    """
    try:
        return await _backend().rename_drive_file(file_id=file_id, new_name=new_name)
    except GogError as exc:
        log.warning("rename_drive_file failed for %s: %s", file_id, exc)
        return {"error": str(exc)}


async def share_drive_file(
    file_id: str,
    to: str = "user",
    email: str = "",
    role: str = "reader",
    discoverable: bool = False,
) -> dict[str, Any]:
    """Share a Google Drive file or folder.

    Args:
        file_id: ID of the file to share.
        to: Share target: 'user' (specific person), 'anyone' (public link).
        email: Email address when to='user'.
        role: Permission level: 'reader' or 'writer'.
        discoverable: Allow the file to appear in search results (anyone/domain only).

    Returns:
        Dict with permission details or 'error' key.
    """
    try:
        return await _backend().share_drive_file(
            file_id=file_id, to=to, email=email, role=role, discoverable=discoverable
        )
    except GogError as exc:
        log.warning("share_drive_file failed for %s: %s", file_id, exc)
        return {"error": str(exc)}


async def create_drive_folder(name: str, parent_folder_id: str = "") -> dict[str, Any]:
    """Create a new Google Drive folder.

    Args:
        name: Folder name.
        parent_folder_id: Optional parent folder ID (defaults to root).

    Returns:
        Dict with new folder details or 'error' key.
    """
    try:
        return await _backend().create_drive_folder(
            name=name, parent_folder_id=parent_folder_id
        )
    except GogError as exc:
        log.warning("create_drive_folder failed: %s", exc)
        return {"error": str(exc)}


async def delete_drive_file(file_id: str) -> dict[str, Any]:
    """Move a Google Drive file to trash.

    Args:
        file_id: ID of the file to delete.

    Returns:
        Dict confirming deletion or 'error' key.
    """
    try:
        return await _backend().delete_drive_file(file_id=file_id)
    except GogError as exc:
        log.warning("delete_drive_file failed for %s: %s", file_id, exc)
        return {"error": str(exc)}


# ── Docs — read ───────────────────────────────────────────────────────────────


async def export_google_doc_text(document_id: str) -> dict[str, Any]:
    """Export the full text content of a Google Doc.

    Args:
        document_id: Google Docs document ID (from its URL).

    Returns:
        Dict with 'text' field (plain text content) or 'error' key.
    """
    try:
        text = await _backend().export_google_doc_text(document_id=document_id)
        return {"document_id": document_id, "text": text}
    except GogError as exc:
        log.warning("export_google_doc_text failed for %s: %s", document_id, exc)
        return {"document_id": document_id, "text": "", "error": str(exc)}


async def get_google_doc_info(document_id: str) -> dict[str, Any]:
    """Get metadata for a Google Doc (title, owner, last modified, etc.).

    Args:
        document_id: Google Docs document ID.

    Returns:
        Dict with document metadata or 'error' key.
    """
    try:
        return await _backend().get_google_doc_info(document_id=document_id)
    except GogError as exc:
        log.warning("get_google_doc_info failed for %s: %s", document_id, exc)
        return {"error": str(exc)}


# ── Docs — write ──────────────────────────────────────────────────────────────


async def create_google_doc(title: str) -> dict[str, Any]:
    """Create a new blank Google Doc with the given title.

    Args:
        title: The document title.

    Returns:
        Dict with 'documentId', 'title', and optional 'error' key.
    """
    try:
        result = await _backend().create_google_doc(title=title)
        return _resource_create_response(
            result,
            id_field="documentId",
            id_keys=("documentId", "id"),
            title=title,
            url_template="https://docs.google.com/document/d/{resource_id}/edit",
            missing_id_error="create_google_doc_missing_document_id",
        )
    except GogError as exc:
        log.warning("create_google_doc failed: %s", exc)
        return {"documentId": "", "title": title, "error": str(exc)}


async def write_google_doc(document_id: str, text: str, append: bool = False) -> dict[str, Any]:
    """Write content to a Google Doc, replacing the body by default.

    Args:
        document_id: Google Docs document ID.
        text: Content to write (plain text or markdown).
        append: If True, append to existing content instead of replacing.

    Returns:
        Dict confirming the write or 'error' key.
    """
    try:
        result = await _backend().write_google_doc(
            document_id=document_id, text=text, append=append
        )
        return {"document_id": document_id, "written": True, **result}
    except GogError as exc:
        log.warning("write_google_doc failed for %s: %s", document_id, exc)
        return {"document_id": document_id, "written": False, "error": str(exc)}


async def copy_google_doc(
    document_id: str, title: str, parent_folder_id: str = ""
) -> dict[str, Any]:
    """Copy a Google Doc to a new document.

    Args:
        document_id: Source document ID.
        title: Title for the new copy.
        parent_folder_id: Optional destination folder ID.

    Returns:
        Dict with 'documentId', 'title', 'url', or 'error' key.
    """
    try:
        result = await _backend().copy_google_doc(
            document_id=document_id, title=title, parent_folder_id=parent_folder_id
        )
        return _resource_create_response(
            result,
            id_field="documentId",
            id_keys=("documentId", "id"),
            title=title,
            url_template="https://docs.google.com/document/d/{resource_id}/edit",
            missing_id_error="copy_google_doc_missing_document_id",
        )
    except GogError as exc:
        log.warning("copy_google_doc failed for %s: %s", document_id, exc)
        return {"documentId": "", "title": title, "error": str(exc)}


async def find_replace_in_doc(
    document_id: str, find: str, replace: str
) -> dict[str, Any]:
    """Find and replace text in a Google Doc.

    Args:
        document_id: Google Docs document ID.
        find: Text to find.
        replace: Replacement text.

    Returns:
        Dict confirming the operation or 'error' key.
    """
    try:
        return await _backend().find_replace_in_doc(
            document_id=document_id, find=find, replace=replace
        )
    except GogError as exc:
        log.warning("find_replace_in_doc failed for %s: %s", document_id, exc)
        return {"error": str(exc)}


async def clear_google_doc(document_id: str) -> dict[str, Any]:
    """Clear all content from a Google Doc (leaves it empty).

    Args:
        document_id: Google Docs document ID.

    Returns:
        Dict confirming the operation or 'error' key.
    """
    try:
        return await _backend().clear_google_doc(document_id=document_id)
    except GogError as exc:
        log.warning("clear_google_doc failed for %s: %s", document_id, exc)
        return {"error": str(exc)}


# ── Calendar — read ───────────────────────────────────────────────────────────


async def list_calendar_events(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    max_results: int = 6,
) -> dict[str, Any]:
    """List calendar events within a time window.

    Args:
        time_min: Start of window in RFC3339 format (e.g. '2026-03-09T00:00:00Z').
        time_max: End of window in RFC3339 format.
        calendar_id: Calendar to query (default 'primary').
        max_results: Maximum events to return (default 6).

    Returns:
        Dict with 'items' list of events and optional 'error' key.
    """
    import os
    cal_id = os.getenv("ATHENA_CALENDAR_ID", calendar_id)
    try:
        return await _backend().list_calendar_events(
            calendar_id=cal_id,
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
        )
    except GogError as exc:
        log.warning("list_calendar_events failed: %s", exc)
        return _workspace_err({"items": []}, exc)


async def get_calendar_event(event_id: str, calendar_id: str = "primary") -> dict[str, Any]:
    """Get a single calendar event by ID.

    Args:
        event_id: Calendar event ID.
        calendar_id: Calendar containing the event (default 'primary').

    Returns:
        Dict with event details or 'error' key.
    """
    import os
    cal_id = os.getenv("ATHENA_CALENDAR_ID", calendar_id)
    try:
        return await _backend().get_calendar_event(event_id=event_id, calendar_id=cal_id)
    except GogError as exc:
        log.warning("get_calendar_event failed for %s: %s", event_id, exc)
        return {"error": str(exc)}


async def search_calendar_events(
    query: str,
    calendar_id: str = "primary",
    time_from: str = "",
    time_to: str = "",
    max_results: int = 10,
) -> dict[str, Any]:
    """Search calendar events by keyword.

    Args:
        query: Keyword to search for in event titles and descriptions.
        calendar_id: Calendar to search (default 'primary').
        time_from: Optional start bound in RFC3339 format.
        time_to: Optional end bound in RFC3339 format.
        max_results: Maximum events to return (default 10).

    Returns:
        Dict with matching events or 'error' key.
    """
    import os
    cal_id = os.getenv("ATHENA_CALENDAR_ID", calendar_id)
    try:
        return await _backend().search_calendar_events(
            query=query,
            calendar_id=cal_id,
            time_from=time_from,
            time_to=time_to,
            max_results=max_results,
        )
    except GogError as exc:
        log.warning("search_calendar_events failed: %s", exc)
        return _workspace_err({"items": []}, exc)


async def get_calendar_freebusy(
    time_min: str,
    time_max: str,
    calendar_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Get free/busy information for one or more calendars.

    Args:
        time_min: Start of window in RFC3339 format.
        time_max: End of window in RFC3339 format.
        calendar_ids: List of calendar IDs to check (defaults to primary).

    Returns:
        Dict with free/busy blocks or 'error' key.
    """
    try:
        return await _backend().get_calendar_freebusy(
            time_min=time_min,
            time_max=time_max,
            calendar_ids=calendar_ids,
        )
    except GogError as exc:
        log.warning("get_calendar_freebusy failed: %s", exc)
        return {"error": str(exc)}


# ── Calendar — write ──────────────────────────────────────────────────────────


async def create_calendar_event(
    summary: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    location: str = "",
    attendee_emails: list[str] | None = None,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """Create a new calendar event.

    Args:
        summary: Event title.
        start_datetime: Start time in RFC3339 format.
        end_datetime: End time in RFC3339 format.
        description: Optional event description (supports plain text or agenda).
        location: Optional event location.
        attendee_emails: Optional list of attendee email addresses.
        calendar_id: Calendar to create the event in (default 'primary').

    Returns:
        Dict with 'id', 'htmlLink', and optional 'error' key.
    """
    import os
    cal_id = os.getenv("ATHENA_CALENDAR_ID", calendar_id)
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start_datetime},
        "end": {"dateTime": end_datetime},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendee_emails:
        body["attendees"] = [{"email": email} for email in attendee_emails]

    try:
        result = await _backend().create_calendar_event(calendar_id=cal_id, body=body)
        return {
            "id": str(result.get("id") or ""),
            "htmlLink": str(result.get("htmlLink") or ""),
            "summary": summary,
        }
    except GogError as exc:
        log.warning("create_calendar_event failed: %s", exc)
        return {"id": "", "error": str(exc)}


async def update_calendar_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: str = "",
    start_datetime: str = "",
    end_datetime: str = "",
    description: str = "",
    location: str = "",
    attendee_emails: list[str] | None = None,
    add_attendee_emails: list[str] | None = None,
) -> dict[str, Any]:
    """Update an existing calendar event. Only provided fields are changed.

    Args:
        event_id: Calendar event ID to update.
        calendar_id: Calendar containing the event (default 'primary').
        summary: New event title (leave empty to keep current).
        start_datetime: New start time in RFC3339 format (leave empty to keep current).
        end_datetime: New end time in RFC3339 format (leave empty to keep current).
        description: New description (leave empty to keep current).
        location: New location (leave empty to keep current).
        attendee_emails: Replace ALL attendees with this list (replaces existing list).
        add_attendee_emails: Add these emails to existing attendees (preserves others).

    Returns:
        Dict with updated event details or 'error' key.
    """
    import os
    cal_id = os.getenv("ATHENA_CALENDAR_ID", calendar_id)
    body: dict[str, Any] = {}
    if summary:
        body["summary"] = summary
    if start_datetime:
        body["start"] = {"dateTime": start_datetime}
    if end_datetime:
        body["end"] = {"dateTime": end_datetime}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendee_emails is not None:
        body["attendees"] = [{"email": e} for e in attendee_emails]
    elif add_attendee_emails:
        body["add_attendees"] = [{"email": e} for e in add_attendee_emails]

    try:
        result = await _backend().update_calendar_event(
            event_id=event_id, calendar_id=cal_id, body=body
        )
        return {
            "id": str(result.get("id") or event_id),
            "htmlLink": str(result.get("htmlLink") or ""),
            "summary": str(result.get("summary") or summary),
        }
    except GogError as exc:
        log.warning("update_calendar_event failed for %s: %s", event_id, exc)
        return {"id": event_id, "error": str(exc)}


async def delete_calendar_event(
    event_id: str, calendar_id: str = "primary"
) -> dict[str, Any]:
    """Delete a calendar event.

    Args:
        event_id: Calendar event ID to delete.
        calendar_id: Calendar containing the event (default 'primary').

    Returns:
        Dict confirming deletion or 'error' key.
    """
    import os
    cal_id = os.getenv("ATHENA_CALENDAR_ID", calendar_id)
    try:
        await _backend().delete_calendar_event(event_id=event_id, calendar_id=cal_id)
        return {"deleted": True, "event_id": event_id}
    except GogError as exc:
        log.warning("delete_calendar_event failed for %s: %s", event_id, exc)
        return {"deleted": False, "event_id": event_id, "error": str(exc)}


# ── Sheets — read ─────────────────────────────────────────────────────────────


async def get_spreadsheet_metadata(spreadsheet_id: str) -> dict[str, Any]:
    """Get metadata for a Google Sheets spreadsheet (title, sheet names, etc.).

    Args:
        spreadsheet_id: Spreadsheet ID.

    Returns:
        Dict with spreadsheet metadata or 'error' key.
    """
    try:
        return await _backend().get_spreadsheet_metadata(spreadsheet_id=spreadsheet_id)
    except GogError as exc:
        log.warning("get_spreadsheet_metadata failed for %s: %s", spreadsheet_id, exc)
        return {"error": str(exc)}


async def get_sheet_values(spreadsheet_id: str, range: str) -> dict[str, Any]:
    """Read cell values from a Google Sheet range.

    Args:
        spreadsheet_id: Spreadsheet ID.
        range: Cell range in A1 notation (e.g. 'Sheet1!A1:D10' or 'A1:D10').

    Returns:
        Dict with 'values' 2D array or 'error' key.
    """
    try:
        return await _backend().get_sheet_values(
            spreadsheet_id=spreadsheet_id, range=range
        )
    except GogError as exc:
        log.warning("get_sheet_values failed for %s %s: %s", spreadsheet_id, range, exc)
        return {"values": [], "error": str(exc)}


# ── Sheets — write ────────────────────────────────────────────────────────────


async def create_spreadsheet(
    title: str,
    sheets: list[str] | None = None,
    parent_folder_id: str = "",
) -> dict[str, Any]:
    """Create a new Google Sheets spreadsheet.

    Args:
        title: Spreadsheet title.
        sheets: Optional list of sheet/tab names to create (e.g. ['Q1', 'Q2']).
        parent_folder_id: Optional Drive folder ID to save the spreadsheet in.

    Returns:
        Dict with spreadsheet ID, title, URL, or 'error' key.
    """
    try:
        result = await _backend().create_spreadsheet(
            title=title, sheets=sheets, parent_folder_id=parent_folder_id
        )
        return _resource_create_response(
            result,
            id_field="spreadsheetId",
            id_keys=("spreadsheetId", "id"),
            title=title,
            url_template="https://docs.google.com/spreadsheets/d/{resource_id}/edit",
            missing_id_error="create_spreadsheet_missing_spreadsheet_id",
        )
    except GogError as exc:
        log.warning("create_spreadsheet failed: %s", exc)
        return {"spreadsheetId": "", "title": title, "error": str(exc)}


async def update_sheet_values(
    spreadsheet_id: str, range: str, values_json: str
) -> dict[str, Any]:
    """Write values to a Google Sheet range.

    Args:
        spreadsheet_id: Spreadsheet ID.
        range: Cell range in A1 notation (e.g. 'Sheet1!A1:C3').
        values_json: JSON 2D array of values, e.g. '[["Name","Score"],["Alice","95"]]'.

    Returns:
        Dict with update confirmation or 'error' key.
    """
    try:
        return await _backend().update_sheet_values(
            spreadsheet_id=spreadsheet_id, range=range, values_json=values_json
        )
    except GogError as exc:
        log.warning("update_sheet_values failed for %s: %s", spreadsheet_id, exc)
        return {"error": str(exc)}


async def append_sheet_values(
    spreadsheet_id: str, range: str, values_json: str
) -> dict[str, Any]:
    """Append rows to a Google Sheet (adds after last row of data in range).

    Args:
        spreadsheet_id: Spreadsheet ID.
        range: Target range/sheet (e.g. 'Sheet1!A:C' to append to columns A-C).
        values_json: JSON 2D array of rows to append, e.g. '[["Bob","88"],["Carol","91"]]'.

    Returns:
        Dict with append confirmation or 'error' key.
    """
    try:
        return await _backend().append_sheet_values(
            spreadsheet_id=spreadsheet_id, range=range, values_json=values_json
        )
    except GogError as exc:
        log.warning("append_sheet_values failed for %s: %s", spreadsheet_id, exc)
        return {"error": str(exc)}


async def find_replace_in_sheet(
    spreadsheet_id: str, find: str, replace: str
) -> dict[str, Any]:
    """Find and replace text across a Google Sheets spreadsheet.

    Args:
        spreadsheet_id: Spreadsheet ID.
        find: Text to find.
        replace: Replacement text.

    Returns:
        Dict confirming the operation or 'error' key.
    """
    try:
        return await _backend().find_replace_in_sheet(
            spreadsheet_id=spreadsheet_id, find=find, replace=replace
        )
    except GogError as exc:
        log.warning("find_replace_in_sheet failed for %s: %s", spreadsheet_id, exc)
        return {"error": str(exc)}


# ── Slides — read ─────────────────────────────────────────────────────────────


async def get_presentation_info(presentation_id: str) -> dict[str, Any]:
    """Get metadata for a Google Slides presentation (title, slide count, etc.).

    Args:
        presentation_id: Presentation ID.

    Returns:
        Dict with presentation metadata or 'error' key.
    """
    try:
        return await _backend().get_presentation_info(presentation_id=presentation_id)
    except GogError as exc:
        log.warning("get_presentation_info failed for %s: %s", presentation_id, exc)
        return {"error": str(exc)}


async def list_presentation_slides(presentation_id: str) -> dict[str, Any]:
    """List all slides in a Google Slides presentation with their IDs.

    Args:
        presentation_id: Presentation ID.

    Returns:
        Dict with slides list (each has objectId, title/index) or 'error' key.
    """
    try:
        return await _backend().list_presentation_slides(presentation_id=presentation_id)
    except GogError as exc:
        log.warning("list_presentation_slides failed for %s: %s", presentation_id, exc)
        return {"slides": [], "error": str(exc)}


async def read_presentation_slide(
    presentation_id: str, slide_id: str
) -> dict[str, Any]:
    """Read the content of a single Google Slides slide (text, notes, etc.).

    Args:
        presentation_id: Presentation ID.
        slide_id: Slide object ID (from list_presentation_slides).

    Returns:
        Dict with slide content or 'error' key.
    """
    try:
        return await _backend().read_presentation_slide(
            presentation_id=presentation_id, slide_id=slide_id
        )
    except GogError as exc:
        log.warning(
            "read_presentation_slide failed for %s/%s: %s", presentation_id, slide_id, exc
        )
        return {"error": str(exc)}


# ── Slides — write ────────────────────────────────────────────────────────────


async def create_presentation(
    title: str, parent_folder_id: str = "", template_id: str = ""
) -> dict[str, Any]:
    """Create a new blank Google Slides presentation.

    Args:
        title: Presentation title.
        parent_folder_id: Optional Drive folder ID.
        template_id: Optional template presentation ID to copy from.

    Returns:
        Dict with presentationId, title, URL, or 'error' key.
    """
    try:
        result = await _backend().create_presentation(
            title=title, parent_folder_id=parent_folder_id, template_id=template_id
        )
        return _resource_create_response(
            result,
            id_field="presentationId",
            id_keys=("presentationId", "id"),
            title=title,
            url_template="https://docs.google.com/presentation/d/{resource_id}/edit",
            missing_id_error="create_presentation_missing_presentation_id",
        )
    except GogError as exc:
        log.warning("create_presentation failed: %s", exc)
        return {"presentationId": "", "title": title, "error": str(exc)}


async def create_presentation_from_markdown(
    title: str, content: str, parent_folder_id: str = ""
) -> dict[str, Any]:
    """Create a Google Slides presentation from markdown content.

    Format: Uses the gog slide-markdown parser. Each slide is rendered as:
        ## Slide Title
        - Bullet point one
        - Bullet point two

        ---

        ## Slide Title
        - Bullet point one
        - Bullet point two

    Example:
        ## Introduction
        - Key point one
        - Key point two

        ---

        ## Next Steps
        - Action item A

    Args:
        title: Presentation title.
        content: Markdown content. The wrapper normalizes simple headed sections into the
                 gog-compatible slide format automatically.
        parent_folder_id: Optional Drive folder ID.

    Returns:
        Dict with presentationId, title, URL, or 'error' key.
    """
    normalized_content = _normalize_slide_markdown(title=title, content=content)
    if not normalized_content:
        return {
            "presentationId": "",
            "title": title,
            "error": "presentation_content_empty",
        }

    try:
        result = await _backend().create_presentation_from_markdown(
            title=title,
            content=normalized_content,
            parent_folder_id=parent_folder_id,
        )
        response = _resource_create_response(
            result,
            id_field="presentationId",
            id_keys=("presentationId", "id"),
            title=title,
            url_template="https://docs.google.com/presentation/d/{resource_id}/edit",
            missing_id_error="create_presentation_from_markdown_missing_presentation_id",
        )
        pres_id = str(response.get("presentationId") or "").strip()
        if response.get("error") or not pres_id:
            return response
        verification_error = await _verify_presentation_content(pres_id)
        if verification_error:
            return {**response, "error": verification_error}
        return response
    except GogError as exc:
        log.warning("create_presentation_from_markdown failed: %s", exc)
        return {"presentationId": "", "title": title, "error": str(exc)}


async def create_presentation_from_template(
    template_presentation_id: str,
    title: str,
    replacements_json: str = "{}",
    parent_folder_id: str = "",
    exact_match: bool = False,
) -> dict[str, Any]:
    """Create a Slides deck by copying a template and applying text replacements.

    Args:
        template_presentation_id: Source presentation ID to use as a template.
        title: Title for the new presentation.
        replacements_json: JSON object mapping placeholder keys to replacement text.
        parent_folder_id: Optional Drive folder ID.
        exact_match: If True, match raw strings instead of template-key placeholders.

    Returns:
        Dict with presentationId, title, URL, or 'error' key.
    """
    try:
        result = await _backend().create_presentation_from_template(
            template_id=template_presentation_id,
            title=title,
            replacements_json=replacements_json,
            parent_folder_id=parent_folder_id,
            exact_match=exact_match,
        )
        return _resource_create_response(
            result,
            id_field="presentationId",
            id_keys=("presentationId", "id"),
            title=title,
            url_template="https://docs.google.com/presentation/d/{resource_id}/edit",
            missing_id_error="create_presentation_from_template_missing_presentation_id",
        )
    except GogError as exc:
        log.warning(
            "create_presentation_from_template failed for %s: %s",
            template_presentation_id,
            exc,
        )
        return {"presentationId": "", "title": title, "error": str(exc)}


def _normalize_slide_markdown(*, title: str, content: str) -> str:
    text = str(content or "").replace("\r\n", "\n").strip()
    if not text:
        return ""

    fallback_title = title.strip() or "Presentation"
    if re.search(r"^\s*#{1,6}\s+\S", text, re.MULTILINE):
        slides = _slides_from_markdown_sections(text, fallback_title=fallback_title)
    else:
        bullets = _text_to_slide_bullets(text)
        slides = _render_slide_chunks(fallback_title, bullets)

    return "\n\n---\n\n".join(slides).strip()


def _slides_from_markdown_sections(text: str, *, fallback_title: str) -> list[str]:
    slides: list[str] = []
    current_title = fallback_title
    current_bullets: list[str] = []
    saw_heading = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"(?:---|\*\*\*|___)", line):
            continue

        heading = re.match(r"^#{1,6}\s+(.*\S)\s*$", line)
        if heading:
            if saw_heading or current_bullets:
                slides.extend(_render_slide_chunks(current_title, current_bullets))
            current_title = _compact_slide_text(heading.group(1)) or fallback_title
            current_bullets = []
            saw_heading = True
            continue

        current_bullets.extend(_text_to_slide_bullets(line))

    if saw_heading or current_bullets:
        slides.extend(_render_slide_chunks(current_title, current_bullets))

    return slides


def _text_to_slide_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        bullet = re.match(r"^(?:[-*+]|\d+\.)\s+(.*\S)\s*$", line)
        if bullet:
            compact = _compact_slide_text(bullet.group(1))
            if compact:
                bullets.append(compact)
            continue

        sentences = re.split(r"(?<=[.!?])\s+", line)
        extracted = [_compact_slide_text(sentence) for sentence in sentences]
        bullets.extend(sentence for sentence in extracted if sentence)

    return bullets


def _render_slide_chunks(title: str, bullets: list[str]) -> list[str]:
    if not bullets:
        return []

    slides: list[str] = []
    for index in range(0, len(bullets), 5):
        chunk = bullets[index : index + 5]
        chunk_number = index // 5
        chunk_title = title if chunk_number == 0 else f"{title} (cont. {chunk_number + 1})"
        slide = "\n".join(
            [
                f"## {chunk_title}",
                *(f"- {bullet}" for bullet in chunk),
            ]
        )
        slides.append(slide)
    return slides


def _compact_slide_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t-")


async def _verify_presentation_content(presentation_id: str) -> str | None:
    attempts = 3
    last_error: str | None = None

    for attempt in range(attempts):
        try:
            slides_payload = await _backend().list_presentation_slides(
                presentation_id=presentation_id
            )
            slide_ids = _extract_slide_ids(slides_payload)
            if slide_ids:
                for slide_id in slide_ids[:3]:
                    slide_payload = await _backend().read_presentation_slide(
                        presentation_id=presentation_id,
                        slide_id=slide_id,
                    )
                    if _slide_payload_has_visible_text(slide_payload):
                        return None
                last_error = "presentation_created_empty"
            else:
                last_error = "presentation_created_empty"
        except GogError as exc:
            last_error = f"presentation_verification_failed: {exc}"

        if attempt < attempts - 1:
            await asyncio.sleep(0.25)

    return last_error


def _extract_slide_ids(payload: dict[str, Any]) -> list[str]:
    raw_slides = payload.get("slides") or payload.get("items") or []
    slide_ids: list[str] = []
    for value in raw_slides:
        if not isinstance(value, dict):
            continue
        slide_id = str(
            value.get("objectId") or value.get("id") or value.get("slideId") or ""
        ).strip()
        if slide_id:
            slide_ids.append(slide_id)
    if len(slide_ids) > 1:
        real_slide_ids = [slide_id for slide_id in slide_ids if slide_id != "p"]
        if real_slide_ids:
            return real_slide_ids
    return slide_ids


def _slide_payload_has_visible_text(payload: dict[str, Any]) -> bool:
    return bool(_collect_slide_text(payload))


def _collect_slide_text(node: Any, *, exclude_notes: bool = True) -> list[str]:
    fragments: list[str] = []

    def walk(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = key.lower()
                if exclude_notes and lowered in {"notes", "speakernotes", "speaker_notes"}:
                    continue
                if lowered in {"text", "content", "plaintext", "speakernotestext"}:
                    fragments.extend(_string_values(child))
                    continue
                if lowered == "textrun" and isinstance(child, dict):
                    content = str(child.get("content") or "").strip()
                    if content:
                        fragments.append(content)
                walk(child, lowered)
            return
        if isinstance(value, list):
            for child in value:
                walk(child, parent_key)

    walk(node)
    return [fragment for fragment in fragments if fragment.strip()]


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        items: list[str] = []
        for child in value:
            items.extend(_string_values(child))
        return items
    if isinstance(value, dict):
        items: list[str] = []
        for child in value.values():
            items.extend(_string_values(child))
        return items
    return []


async def add_image_slide(
    presentation_id: str,
    image_path: str,
    speaker_notes: str = "",
    before_slide_id: str = "",
) -> dict[str, Any]:
    """Add a full-bleed image slide to an existing presentation.

    Args:
        presentation_id: Presentation ID.
        image_path: Local PNG/JPG file path.
        speaker_notes: Optional speaker notes text for the new slide.
        before_slide_id: Optional slide object ID to insert before.

    Returns:
        Dict confirming the operation or 'error' key.
    """
    try:
        return await _backend().add_image_slide(
            presentation_id=presentation_id,
            image_path=image_path,
            speaker_notes=speaker_notes,
            before_slide_id=before_slide_id,
        )
    except GogError as exc:
        log.warning("add_image_slide failed for %s: %s", presentation_id, exc)
        return {"error": str(exc)}


async def replace_slide_image(
    presentation_id: str,
    slide_id: str,
    image_path: str,
    speaker_notes: str = "",
) -> dict[str, Any]:
    """Replace an existing slide with a full-bleed image.

    Args:
        presentation_id: Presentation ID.
        slide_id: Slide object ID to replace.
        image_path: Local PNG/JPG/GIF file path.
        speaker_notes: Optional new speaker notes text.

    Returns:
        Dict confirming the operation or 'error' key.
    """
    try:
        return await _backend().replace_slide_image(
            presentation_id=presentation_id,
            slide_id=slide_id,
            image_path=image_path,
            speaker_notes=speaker_notes,
        )
    except GogError as exc:
        log.warning("replace_slide_image failed for %s/%s: %s", presentation_id, slide_id, exc)
        return {"error": str(exc)}


async def update_slide_notes(
    presentation_id: str,
    slide_id: str,
    speaker_notes: str,
) -> dict[str, Any]:
    """Update speaker notes on an existing slide.

    Args:
        presentation_id: Presentation ID.
        slide_id: Slide object ID.
        speaker_notes: New speaker notes text.

    Returns:
        Dict confirming the operation or 'error' key.
    """
    try:
        return await _backend().update_slide_notes(
            presentation_id=presentation_id,
            slide_id=slide_id,
            speaker_notes=speaker_notes,
        )
    except GogError as exc:
        log.warning("update_slide_notes failed for %s/%s: %s", presentation_id, slide_id, exc)
        return {"error": str(exc)}


async def delete_presentation_slide(
    presentation_id: str,
    slide_id: str,
) -> dict[str, Any]:
    """Delete a slide from an existing presentation.

    Args:
        presentation_id: Presentation ID.
        slide_id: Slide object ID to delete.

    Returns:
        Dict confirming the operation or 'error' key.
    """
    try:
        return await _backend().delete_presentation_slide(
            presentation_id=presentation_id,
            slide_id=slide_id,
        )
    except GogError as exc:
        log.warning(
            "delete_presentation_slide failed for %s/%s: %s",
            presentation_id,
            slide_id,
            exc,
        )
        return {"error": str(exc)}


async def copy_presentation(
    presentation_id: str, title: str, parent_folder_id: str = ""
) -> dict[str, Any]:
    """Copy a Google Slides presentation.

    Args:
        presentation_id: ID of the presentation to copy.
        title: Title for the new copy.
        parent_folder_id: Optional Drive folder ID.

    Returns:
        Dict with new presentationId, title, URL, or 'error' key.
    """
    try:
        result = await _backend().copy_presentation(
            presentation_id=presentation_id, title=title, parent_folder_id=parent_folder_id
        )
        return _resource_create_response(
            result,
            id_field="presentationId",
            id_keys=("presentationId", "id"),
            title=title,
            url_template="https://docs.google.com/presentation/d/{resource_id}/edit",
            missing_id_error="copy_presentation_missing_presentation_id",
        )
    except GogError as exc:
        log.warning("copy_presentation failed for %s: %s", presentation_id, exc)
        return {"presentationId": "", "title": title, "error": str(exc)}


# ── Slides — advanced inspect/edit via slides-agent ─────────────────────────


async def inspect_presentation(presentation_id: str) -> dict[str, Any]:
    """Inspect a presentation deeply and return stable slide and element IDs.

    Use this tool when the user wants to inspect, summarize, edit, restyle, reorder,
    or otherwise revise an existing presentation and you need exact slide IDs,
    element IDs, notes text, layout names, or theme-related structure.

    Required:
        presentation_id: Google Slides presentation ID.

    Returns:
        Dict with `ok`, `presentation`, `warnings`, and `errors`.
        `presentation.slides[].slide_id` and `presentation.slides[].elements[].element_id`
        are the stable IDs for follow-up edit tools.

    Errors:
        slides_agent_unavailable, auth_error, not_found, api_error

    Do NOT use this to create a new deck from source material. Use
    `create_presentation_from_markdown` or `create_presentation_from_template` instead.
    """
    try:
        return await _backend().inspect_presentation(presentation_id=presentation_id)
    except SlidesAgentError as exc:
        log.warning("inspect_presentation failed for %s: %s", presentation_id, exc)
        return _slides_agent_err(exc)


async def inspect_slide(presentation_id: str, slide_id: str) -> dict[str, Any]:
    """Inspect one slide in detail, including all elements, notes, and layout metadata.

    Use this tool when the user references a specific slide and you need exact element IDs
    before changing text, images, or layout.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID from `inspect_presentation` or `list_presentation_slides`.

    Returns:
        Dict with `ok`, `presentation_id`, `slide`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, api_error

    Do NOT use this for whole-deck inspection. Use `inspect_presentation` first when the
    target slide is not already known.
    """
    try:
        return await _backend().inspect_slide(
            presentation_id=presentation_id,
            slide_id=slide_id,
        )
    except SlidesAgentError as exc:
        log.warning("inspect_slide failed for %s/%s: %s", presentation_id, slide_id, exc)
        return _slides_agent_err(exc)


async def list_slide_elements(
    presentation_id: str,
    slide_id: str,
    element_type: str = "",
) -> dict[str, Any]:
    """List the elements on a slide with stable IDs and optional type filtering.

    Use this tool when you know the slide ID and need candidate element IDs for text or image
    edits. Common `element_type` values are `shape` and `image`.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.

    Optional:
        element_type: Filter to a specific element type.

    Returns:
        Dict with `ok`, `elements`, `element_count`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, api_error

    Do NOT use this to inspect the entire deck. Use `inspect_presentation` for that.
    """
    try:
        return await _backend().list_slide_elements(
            presentation_id=presentation_id,
            slide_id=slide_id,
            element_type=element_type,
        )
    except SlidesAgentError as exc:
        log.warning("list_slide_elements failed for %s/%s: %s", presentation_id, slide_id, exc)
        return _slides_agent_err(exc)


async def inspect_slide_element(
    presentation_id: str,
    slide_id: str,
    element_id: str,
) -> dict[str, Any]:
    """Inspect one slide element in full detail.

    Use this tool when you already have an `element_id` and need to confirm its type,
    placeholder role, text content, transform, or image metadata before editing.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.
        element_id: Element object ID from `list_slide_elements` or `inspect_presentation`.

    Returns:
        Dict with `ok`, `element`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, api_error
    """
    try:
        return await _backend().inspect_slide_element(
            presentation_id=presentation_id,
            slide_id=slide_id,
            element_id=element_id,
        )
    except SlidesAgentError as exc:
        log.warning(
            "inspect_slide_element failed for %s/%s/%s: %s",
            presentation_id,
            slide_id,
            element_id,
            exc,
        )
        return _slides_agent_err(exc)


async def replace_text_in_presentation(
    presentation_id: str,
    find: str,
    replace: str,
    match_case: bool = True,
) -> dict[str, Any]:
    """Replace all occurrences of text across a presentation.

    Use this tool when the user wants to fill placeholders, rename a repeated term,
    or perform a deck-wide find/replace. This is the preferred tool for template token
    replacement of a named template token such as a customer placeholder.

    Required:
        presentation_id: Google Slides presentation ID.
        find: Exact text to find.
        replace: Replacement text.

    Optional:
        match_case: Whether matching should be case-sensitive. Defaults to True.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, api_error

    Do NOT use this to edit only one shape. Use `set_slide_element_text`,
    `append_slide_element_text`, or `clear_slide_element_text` for targeted edits.
    """
    try:
        return await _backend().replace_text_in_presentation(
            presentation_id=presentation_id,
            find=find,
            replace=replace,
            match_case=match_case,
        )
    except SlidesAgentError as exc:
        log.warning("replace_text_in_presentation failed for %s: %s", presentation_id, exc)
        return _slides_agent_err(exc)


async def set_slide_element_text(
    presentation_id: str,
    slide_id: str,
    element_id: str,
    text: str,
) -> dict[str, Any]:
    """Replace all text in a specific text-bearing element.

    Use this tool when the user wants to rewrite one title, body box, or other shape
    and you already know the exact `element_id`.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID containing the element.
        element_id: Shape element object ID.
        text: Full replacement text. Use `\\n` for line breaks.

    Returns:
        Dict with `ok`, `presentation_id`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, invalid_reference, validation_error

    Do NOT use this when you need a deck-wide placeholder replacement.
    """
    try:
        return await _backend().set_slide_element_text(
            presentation_id=presentation_id,
            slide_id=slide_id,
            element_id=element_id,
            text=text,
        )
    except SlidesAgentError as exc:
        log.warning(
            "set_slide_element_text failed for %s/%s/%s: %s",
            presentation_id,
            slide_id,
            element_id,
            exc,
        )
        return _slides_agent_err(exc)


async def append_slide_element_text(
    presentation_id: str,
    slide_id: str,
    element_id: str,
    text: str,
) -> dict[str, Any]:
    """Append text to the end of an existing element's content.

    Use this tool when the user wants to add another bullet or line to an existing
    text box without clearing the current content.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.
        element_id: Text-bearing element object ID.
        text: Text to append.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, invalid_reference, api_error
    """
    try:
        return await _backend().append_slide_element_text(
            presentation_id=presentation_id,
            slide_id=slide_id,
            element_id=element_id,
            text=text,
        )
    except SlidesAgentError as exc:
        log.warning(
            "append_slide_element_text failed for %s/%s/%s: %s",
            presentation_id,
            slide_id,
            element_id,
            exc,
        )
        return _slides_agent_err(exc)


async def clear_slide_element_text(
    presentation_id: str,
    slide_id: str,
    element_id: str,
) -> dict[str, Any]:
    """Clear the text content of a specific shape element.

    Use this tool when the user wants to empty a title or body placeholder but keep the
    slide element itself.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.
        element_id: Text-bearing element object ID.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, invalid_reference, api_error
    """
    try:
        return await _backend().clear_slide_element_text(
            presentation_id=presentation_id,
            slide_id=slide_id,
            element_id=element_id,
        )
    except SlidesAgentError as exc:
        log.warning(
            "clear_slide_element_text failed for %s/%s/%s: %s",
            presentation_id,
            slide_id,
            element_id,
            exc,
        )
        return _slides_agent_err(exc)


async def get_slide_element_text(
    presentation_id: str,
    slide_id: str,
    element_id: str,
) -> dict[str, Any]:
    """Read the text content of one specific element.

    Use this tool when the user asks what a specific title/body field says or when you need
    to verify the current text before editing it.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.
        element_id: Text-bearing element object ID.

    Returns:
        Dict with `ok`, `text`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, invalid_reference, api_error
    """
    try:
        return await _backend().get_slide_element_text(
            presentation_id=presentation_id,
            slide_id=slide_id,
            element_id=element_id,
        )
    except SlidesAgentError as exc:
        log.warning(
            "get_slide_element_text failed for %s/%s/%s: %s",
            presentation_id,
            slide_id,
            element_id,
            exc,
        )
        return _slides_agent_err(exc)


async def get_slide_notes(presentation_id: str, slide_id: str) -> dict[str, Any]:
    """Read the speaker notes for one slide.

    Use this tool when the user asks for the talk track or hidden speaker notes on a specific
    slide.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.

    Returns:
        Dict with `ok`, `notes_text`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, api_error
    """
    try:
        return await _backend().get_slide_notes(
            presentation_id=presentation_id,
            slide_id=slide_id,
        )
    except SlidesAgentError as exc:
        log.warning("get_slide_notes failed for %s/%s: %s", presentation_id, slide_id, exc)
        return _slides_agent_err(exc)


async def clear_slide_notes(presentation_id: str, slide_id: str) -> dict[str, Any]:
    """Clear the speaker notes for one slide.

    Use this tool when the user explicitly wants the notes removed. Use `update_slide_notes`
    when the user wants to replace notes with new content instead of clearing them.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, api_error
    """
    try:
        return await _backend().clear_slide_notes(
            presentation_id=presentation_id,
            slide_id=slide_id,
        )
    except SlidesAgentError as exc:
        log.warning("clear_slide_notes failed for %s/%s: %s", presentation_id, slide_id, exc)
        return _slides_agent_err(exc)


async def create_slide(
    presentation_id: str,
    insertion_index: int | None = None,
    layout: str = "",
) -> dict[str, Any]:
    """Create a new slide in an existing presentation.

    Use this tool when the user wants to insert a slide into an existing deck.

    Required:
        presentation_id: Google Slides presentation ID.

    Optional:
        insertion_index: 0-based position. If omitted, append at end.
        layout: Predefined layout such as `BLANK` or `TITLE_AND_BODY`.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, api_error

    Do NOT use this to create a brand-new presentation file.
    """
    try:
        return await _backend().create_slide(
            presentation_id=presentation_id,
            insertion_index=insertion_index,
            layout=layout,
        )
    except SlidesAgentError as exc:
        log.warning("create_slide failed for %s: %s", presentation_id, exc)
        return _slides_agent_err(exc)


async def duplicate_slide(presentation_id: str, slide_id: str) -> dict[str, Any]:
    """Duplicate an existing slide within a presentation.

    Use this tool when the user wants a copy of a slide to edit further.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID to copy.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, api_error
    """
    try:
        return await _backend().duplicate_slide(
            presentation_id=presentation_id,
            slide_id=slide_id,
        )
    except SlidesAgentError as exc:
        log.warning("duplicate_slide failed for %s/%s: %s", presentation_id, slide_id, exc)
        return _slides_agent_err(exc)


async def reorder_slide(
    presentation_id: str,
    slide_id: str,
    insertion_index: int,
) -> dict[str, Any]:
    """Move a slide to a new position in the deck.

    Use this tool when the user asks to reorder slides by index.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID to move.
        insertion_index: New 0-based slide position.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, api_error
    """
    try:
        return await _backend().reorder_slide(
            presentation_id=presentation_id,
            slide_id=slide_id,
            insertion_index=insertion_index,
        )
    except SlidesAgentError as exc:
        log.warning("reorder_slide failed for %s/%s: %s", presentation_id, slide_id, exc)
        return _slides_agent_err(exc)


async def set_slide_background(
    presentation_id: str,
    slide_id: str,
    color_hex: str,
) -> dict[str, Any]:
    """Set a slide's background to a solid color.

    Use this tool when the user explicitly wants a slide background color changed.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.
        color_hex: Hex color string such as `#1A73E8`.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, api_error
    """
    try:
        return await _backend().set_slide_background(
            presentation_id=presentation_id,
            slide_id=slide_id,
            color_hex=color_hex,
        )
    except SlidesAgentError as exc:
        log.warning("set_slide_background failed for %s/%s: %s", presentation_id, slide_id, exc)
        return _slides_agent_err(exc)


async def insert_slide_image(
    presentation_id: str,
    slide_id: str,
    image_url: str = "",
    image_path: str = "",
    left_emu: float = 0.0,
    top_emu: float = 0.0,
    width_emu: float | None = None,
    height_emu: float | None = None,
) -> dict[str, Any]:
    """Insert an image element onto an existing slide.

    Use this tool when the user wants to place an image on a slide at a specific position
    or size.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.
        Exactly one of `image_url` or `image_path`.

    Optional:
        left_emu, top_emu: Position in EMUs.
        width_emu, height_emu: Size in EMUs.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, io_error, api_error

    Do NOT use this for Athena's existing full-bleed image-slide workflow. Use `add_image_slide`
    for that higher-level operation.
    """
    try:
        return await _backend().insert_slide_image(
            presentation_id=presentation_id,
            slide_id=slide_id,
            image_url=image_url,
            image_path=image_path,
            left_emu=left_emu,
            top_emu=top_emu,
            width_emu=width_emu,
            height_emu=height_emu,
        )
    except SlidesAgentError as exc:
        log.warning("insert_slide_image failed for %s/%s: %s", presentation_id, slide_id, exc)
        return _slides_agent_err(exc)


async def replace_slide_image_element(
    presentation_id: str,
    slide_id: str,
    element_id: str,
    image_url: str = "",
    image_path: str = "",
) -> dict[str, Any]:
    """Replace the content of an existing image element.

    Use this tool when the user wants to swap one image element without changing its size
    or position.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.
        element_id: Existing image element object ID.
        Exactly one of `image_url` or `image_path`.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, io_error, api_error

    Do NOT use this to replace an entire slide with a full-bleed image. Use
    `replace_slide_image` for that higher-level slide replacement.
    """
    try:
        return await _backend().replace_slide_image_element(
            presentation_id=presentation_id,
            slide_id=slide_id,
            element_id=element_id,
            image_url=image_url,
            image_path=image_path,
        )
    except SlidesAgentError as exc:
        log.warning(
            "replace_slide_image_element failed for %s/%s/%s: %s",
            presentation_id,
            slide_id,
            element_id,
            exc,
        )
        return _slides_agent_err(exc)


async def resize_slide_image(
    presentation_id: str,
    slide_id: str,
    element_id: str,
    left_emu: float | None = None,
    top_emu: float | None = None,
    width_emu: float | None = None,
    height_emu: float | None = None,
) -> dict[str, Any]:
    """Resize or reposition an existing image element.

    Use this tool when the user wants to move or resize an image without replacing it.

    Required:
        presentation_id: Google Slides presentation ID.
        slide_id: Slide object ID.
        element_id: Existing image element object ID.

    Optional:
        Any combination of `left_emu`, `top_emu`, `width_emu`, `height_emu`.

    Returns:
        Dict with `ok`, `applied_operations`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, api_error
    """
    try:
        return await _backend().resize_slide_image(
            presentation_id=presentation_id,
            slide_id=slide_id,
            element_id=element_id,
            left_emu=left_emu,
            top_emu=top_emu,
            width_emu=width_emu,
            height_emu=height_emu,
        )
    except SlidesAgentError as exc:
        log.warning(
            "resize_slide_image failed for %s/%s/%s: %s",
            presentation_id,
            slide_id,
            element_id,
            exc,
        )
        return _slides_agent_err(exc)


async def inspect_presentation_template(presentation_id: str) -> dict[str, Any]:
    """Inspect template placeholder tokens in an existing presentation.

    Use this tool when the user wants to know which template placeholders exist in a deck
    before filling them.

    Required:
        presentation_id: Google Slides presentation ID.

    Returns:
        Dict with `ok`, `tokens`, `token_count`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, api_error
    """
    try:
        return await _backend().inspect_presentation_template(
            presentation_id=presentation_id,
        )
    except SlidesAgentError as exc:
        log.warning("inspect_presentation_template failed for %s: %s", presentation_id, exc)
        return _slides_agent_err(exc)


async def fill_presentation_template(
    presentation_id: str,
    values_json: str,
) -> dict[str, Any]:
    """Fill template placeholders in an existing presentation.

    Use this tool when the user already has a deck with template tokens and provides
    replacement values as a JSON object string.

    Required:
        presentation_id: Google Slides presentation ID.
        values_json: JSON object string mapping token names to replacement values.

    Returns:
        Dict with `ok`, `applied_replacements`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, io_error, api_error

    Do NOT use this to create a new copy from a template presentation. Use
    `create_presentation_from_template` for that flow.
    """
    try:
        return await _backend().fill_presentation_template(
            presentation_id=presentation_id,
            values_json=values_json,
        )
    except SlidesAgentError as exc:
        log.warning("fill_presentation_template failed for %s: %s", presentation_id, exc)
        return _slides_agent_err(exc)


async def apply_presentation_theme(
    presentation_id: str,
    preset: str = "",
    theme_json: str = "",
) -> dict[str, Any]:
    """Apply a deck-wide style preset or explicit theme spec.

    Use this tool when the user wants to restyle an existing deck consistently across slides.

    Required:
        presentation_id: Google Slides presentation ID.
        Exactly one of `preset` or `theme_json`.

    Optional:
        preset: Built-in preset name such as `corporate-blue`.
        theme_json: JSON spec string for a custom theme.

    Returns:
        Dict with `ok`, `applied_spec`, `slides_affected`, `warnings`, and `errors`.

    Errors:
        slides_agent_unavailable, auth_error, not_found, validation_error, io_error, api_error

    Do NOT use this when the user only wants a single text edit or one slide background change.
    """
    try:
        return await _backend().apply_presentation_theme(
            presentation_id=presentation_id,
            preset=preset,
            theme_json=theme_json,
        )
    except SlidesAgentError as exc:
        log.warning("apply_presentation_theme failed for %s: %s", presentation_id, exc)
        return _slides_agent_err(exc)
