"""GogWorkspaceBackend — implements WorkspaceBackend over the gog CLI."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from typing import Any
from pathlib import Path

from app.gog_client import GogInvocationError, run_gog_json, run_gog_text

log = logging.getLogger("athena.tools.gog_backend")


class GogWorkspaceBackend:
    """Workspace operations backed by the gog CLI.

    Maps WorkspaceBackend method calls to gog subcommands.

    gog command reference:
      gmail search '<query>' --max N --json
      gmail thread get <id> --json
      gmail send --to <addr> --subject <s> --body <b> --json
      gmail drafts create --to <addr> --subject <s> --body <b> --json
      gmail mark-read <msgId...> --json
      gmail unread <msgId...> --json
      gmail archive <msgId...> --json
      gmail trash <msgId...> --json
      drive search '<term>' --max N [--raw-query] --json
      drive get <fileId> --json
      drive copy <fileId> <name> [--parent <folderId>] --json
      drive move <fileId> --parent <folderId> --json
      drive rename <fileId> <newName> --json
      drive share <fileId> --to <user|anyone> [--email <addr>] --role <reader|writer> --json
      drive mkdir <name> [--parent <folderId>] --json
      drive delete <fileId> --force --json
      docs cat <docId>
      docs create 'Title' --json
      docs write <docId> --text <content> [--append] --json
      docs copy <docId> <title> --json
      docs find-replace <docId> <find> <replace> --json
      docs clear <docId> --json
      docs info <docId> --json
      calendar events <calId> --from <rfc3339> --to <rfc3339> --json
      calendar get <calId> <eventId> --json
      calendar create <calId> --summary ... --from ... --to ... --json
      calendar update <calId> <eventId> --summary ... --json
      calendar delete <calId> <eventId> --force --json
      calendar search <query> --calendar <calId> --from ... --to ... --json
      calendar freebusy [<calIds>] --from <rfc3339> --to <rfc3339> --json
      sheets create <title> --json
      sheets get <spreadsheetId> <range> --json
      sheets update <spreadsheetId> <range> --values-json <json> --json
      sheets append <spreadsheetId> <range> --values-json <json> --json
      sheets metadata <spreadsheetId> --json
      sheets find-replace <spreadsheetId> <find> <replace> --json
      slides create <title> --json
      slides create-from-markdown <title> --content-file <path> --json
      slides create-from-template <templateId> <title> --replacements <path> --json
      slides info <presentationId> --json
      slides list-slides <presentationId> --json
      slides read-slide <presentationId> <slideId> --json
      slides add-slide <presentationId> <image> [--notes <text>] --json
      slides replace-slide <presentationId> <slideId> <image> [--notes <text>] --json
      slides update-notes <presentationId> <slideId> --notes <text> --json
      slides delete-slide <presentationId> <slideId> --json
      slides copy <presentationId> <title> --json
    """

    # ── Gmail ─────────────────────────────────────────────────────────────────

    async def search_gmail_threads(
        self,
        *,
        query: str,
        max_results: int = 5,
    ) -> dict[str, Any]:
        return await run_gog_json("gmail", "search", query, "--max", str(max_results))

    async def get_gmail_thread(
        self,
        *,
        thread_id: str,
        format: str = "metadata",
        metadata_headers: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = await run_gog_json("gmail", "thread", "get", thread_id)
        return _project_gmail_thread(
            payload,
            format=format,
            metadata_headers=metadata_headers,
        )

    async def send_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        reply_to_message_id: str = "",
        thread_id: str = "",
    ) -> dict[str, Any]:
        args = ["gmail", "send", "--subject", subject, "--body", body, "--no-input"]
        if to:
            args += ["--to", to]
        if cc:
            args += ["--cc", cc]
        if bcc:
            args += ["--bcc", bcc]
        if reply_to_message_id:
            args += ["--reply-to-message-id", reply_to_message_id]
        elif thread_id:
            args += ["--thread-id", thread_id]
        return await run_gog_json(*args)

    async def create_email_draft(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        reply_to_message_id: str = "",
    ) -> dict[str, Any]:
        args = ["gmail", "drafts", "create", "--subject", subject, "--body", body, "--no-input"]
        if to:
            args += ["--to", to]
        if cc:
            args += ["--cc", cc]
        if bcc:
            args += ["--bcc", bcc]
        if reply_to_message_id:
            args += ["--reply-to-message-id", reply_to_message_id]
        return await run_gog_json(*args)

    async def mark_email_read(self, *, message_ids: list[str]) -> dict[str, Any]:
        return await run_gog_json("gmail", "mark-read", *message_ids, "--no-input")

    async def mark_email_unread(self, *, message_ids: list[str]) -> dict[str, Any]:
        return await run_gog_json("gmail", "unread", *message_ids, "--no-input")

    async def archive_email(self, *, message_ids: list[str]) -> dict[str, Any]:
        return await run_gog_json("gmail", "archive", *message_ids, "--no-input")

    async def trash_email(self, *, message_ids: list[str]) -> dict[str, Any]:
        return await run_gog_json("gmail", "trash", *message_ids, "--force", "--no-input")

    # ── Drive ─────────────────────────────────────────────────────────────────

    async def search_drive_files(
        self,
        *,
        query: str,
        page_size: int = 5,
        order_by: str = "modifiedTime desc",
        fields: str | None = None,
    ) -> dict[str, Any]:
        # query is a Drive API query string — pass as raw Drive API query via --raw-query.
        fetch_size = max(page_size, min(page_size * 5, 50))
        payload = await run_gog_json(
            "drive", "search", query, "--raw-query", "--max", str(fetch_size)
        )
        files, key = _extract_collection(payload, preferred_keys=("files", "items"))
        if files is None or key is None:
            return payload

        ordered = _sort_drive_entries(files, order_by)
        projected = [_project_drive_entry(item, fields) for item in ordered[:page_size]]
        return _replace_collection(payload, key, projected)

    async def get_drive_file(
        self,
        *,
        file_id: str,
        fields: str | None = None,
    ) -> dict[str, Any]:
        payload = await run_gog_json("drive", "get", file_id)
        return _project_drive_entry(payload, fields)

    async def copy_drive_file(
        self,
        *,
        file_id: str,
        name: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        args = ["drive", "copy", file_id, name, "--no-input"]
        if parent_folder_id:
            args += ["--parent", parent_folder_id]
        return await run_gog_json(*args)

    async def move_drive_file(
        self,
        *,
        file_id: str,
        parent_folder_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json(
            "drive", "move", file_id, "--parent", parent_folder_id, "--no-input"
        )

    async def rename_drive_file(
        self,
        *,
        file_id: str,
        new_name: str,
    ) -> dict[str, Any]:
        return await run_gog_json("drive", "rename", file_id, new_name)

    async def share_drive_file(
        self,
        *,
        file_id: str,
        to: str = "user",
        email: str = "",
        role: str = "reader",
        discoverable: bool = False,
    ) -> dict[str, Any]:
        args = ["drive", "share", file_id, "--to", to, "--role", role, "--no-input"]
        if email and to == "user":
            args += ["--email", email]
        if discoverable:
            args.append("--discoverable")
        return await run_gog_json(*args)

    async def create_drive_folder(
        self,
        *,
        name: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        args = ["drive", "mkdir", name, "--no-input"]
        if parent_folder_id:
            args += ["--parent", parent_folder_id]
        return await run_gog_json(*args)

    async def delete_drive_file(
        self,
        *,
        file_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json("drive", "delete", file_id, "--force", "--no-input")

    # ── Docs ──────────────────────────────────────────────────────────────────

    async def export_google_doc_text(
        self,
        *,
        document_id: str,
    ) -> str:
        return await run_gog_text("docs", "cat", document_id)

    async def create_google_doc(
        self,
        *,
        title: str,
    ) -> dict[str, Any]:
        result = await run_gog_json("docs", "create", title, "--no-input")
        return _normalize_created_resource(
            result,
            id_field="documentId",
            id_aliases=("documentId", "docId", "document_id", "doc_id", "fileId", "file_id", "id"),
            title=title,
            kind="docs",
        )

    async def write_google_doc(
        self,
        *,
        document_id: str,
        text: str,
        append: bool = False,
    ) -> dict[str, Any]:
        """Write (or append) content to a Google Doc, replacing body by default."""
        args = ["docs", "write", document_id, "--text", text, "--no-input"]
        if append:
            args.append("--append")
        return await run_gog_json(*args)

    async def copy_google_doc(
        self,
        *,
        document_id: str,
        title: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        args = ["docs", "copy", document_id, title, "--no-input"]
        if parent_folder_id:
            args += ["--parent", parent_folder_id]
        result = await run_gog_json(*args)
        return _normalize_created_resource(
            result,
            id_field="documentId",
            id_aliases=("documentId", "docId", "document_id", "doc_id", "fileId", "file_id", "id"),
            title=title,
            kind="docs",
        )

    async def find_replace_in_doc(
        self,
        *,
        document_id: str,
        find: str,
        replace: str,
    ) -> dict[str, Any]:
        return await run_gog_json(
            "docs", "find-replace", document_id, find, replace, "--no-input"
        )

    async def clear_google_doc(
        self,
        *,
        document_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json("docs", "clear", document_id, "--force", "--no-input")

    async def get_google_doc_info(
        self,
        *,
        document_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json("docs", "info", document_id)

    async def batch_update_google_doc(
        self,
        *,
        document_id: str,
        requests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Extract text from insertText requests and write via gog docs write --append.
        text_parts: list[str] = []
        for req in requests:
            text = (req.get("insertText") or {}).get("text", "")
            if text:
                text_parts.append(text)
        combined = "".join(text_parts)
        if combined:
            await run_gog_json("docs", "write", document_id, "--text", combined)
        return {}

    # ── Calendar ──────────────────────────────────────────────────────────────

    async def list_calendar_events(
        self,
        *,
        calendar_id: str = "primary",
        time_min: str,
        time_max: str,
        max_results: int = 6,
        single_events: bool = True,
        order_by: str = "startTime",
    ) -> dict[str, Any]:
        payload = await run_gog_json(
            "calendar", "events", calendar_id,
            "--from", time_min,
            "--to", time_max,
        )
        items, key = _extract_collection(payload, preferred_keys=("items",))
        if items is None or key is None:
            return payload

        ordered = _sort_calendar_events(items, order_by)
        return _replace_collection(payload, key, ordered[:max_results])

    async def get_calendar_event(
        self,
        *,
        event_id: str,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        result = await run_gog_json("calendar", "get", calendar_id, event_id)
        # gog returns {"event": {...}} — unwrap to return event directly.
        return result.get("event", result)

    async def create_calendar_event(
        self,
        *,
        calendar_id: str = "primary",
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a calendar event from a body dict (Calendar API format)."""
        args = ["calendar", "create", calendar_id]

        summary = str(body.get("summary") or "").strip()
        if summary:
            args += ["--summary", summary]

        location = str(body.get("location") or "").strip()
        if location:
            args += ["--location", location]

        description = str(body.get("description") or "").strip()
        if description:
            args += ["--description", description]

        start_str = _extract_datetime_str(body.get("start"))
        end_str = _extract_datetime_str(body.get("end"))
        if start_str:
            args += ["--from", start_str]
        if end_str:
            args += ["--to", end_str]

        attendees = body.get("attendees") or []
        if attendees:
            emails = ",".join(
                a["email"] for a in attendees if isinstance(a, dict) and "email" in a
            )
            if emails:
                args += ["--attendees", emails]

        args += ["--send-updates", "all", "--no-input"]
        result = await run_gog_json(*args)
        return result.get("event", result)

    async def update_calendar_event(
        self,
        *,
        event_id: str,
        calendar_id: str = "primary",
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Patch a calendar event; only provided fields are changed."""
        args = ["calendar", "update", calendar_id, event_id, "--no-input"]

        if "summary" in body:
            args += ["--summary", str(body["summary"])]

        start_str = _extract_datetime_str(body.get("start"))
        end_str = _extract_datetime_str(body.get("end"))
        if start_str:
            args += ["--from", start_str]
        if end_str:
            args += ["--to", end_str]

        if "description" in body:
            args += ["--description", str(body["description"])]

        if "location" in body:
            args += ["--location", str(body["location"])]

        # Full attendee replacement if "attendees" key present; add-only via "add_attendees".
        attendees = body.get("attendees")
        if attendees is not None:
            emails = ",".join(
                a["email"] for a in attendees if isinstance(a, dict) and "email" in a
            )
            args += ["--attendees", emails]
        else:
            add_attendees = body.get("add_attendees") or []
            if add_attendees:
                emails = ",".join(
                    a["email"] if isinstance(a, dict) else str(a)
                    for a in add_attendees
                )
                args += ["--add-attendee", emails]

        # Default: notify all attendees on updates.
        args += ["--send-updates", "all"]

        result = await run_gog_json(*args)
        return result.get("event", result)

    async def delete_calendar_event(
        self,
        *,
        event_id: str,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        return await run_gog_json(
            "calendar", "delete", calendar_id, event_id, "--force", "--no-input"
        )

    async def search_calendar_events(
        self,
        *,
        query: str,
        calendar_id: str = "primary",
        time_from: str = "",
        time_to: str = "",
        max_results: int = 10,
    ) -> dict[str, Any]:
        args = [
            "calendar", "search", query,
            "--calendar", calendar_id,
            "--max", str(max_results),
        ]
        if time_from:
            args += ["--from", time_from]
        if time_to:
            args += ["--to", time_to]
        return await run_gog_json(*args)

    async def get_calendar_freebusy(
        self,
        *,
        time_min: str,
        time_max: str,
        calendar_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        args = ["calendar", "freebusy", "--from", time_min, "--to", time_max]
        if calendar_ids:
            args.append(",".join(calendar_ids))
        else:
            # Default to primary calendar only to avoid excessive output.
            args.append("primary")
        return await run_gog_json(*args)

    # ── Sheets ────────────────────────────────────────────────────────────────

    async def create_spreadsheet(
        self,
        *,
        title: str,
        sheets: list[str] | None = None,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        args = ["sheets", "create", title, "--no-input"]
        if sheets:
            args += ["--sheets", ",".join(sheets)]
        if parent_folder_id:
            args += ["--parent", parent_folder_id]
        result = await run_gog_json(*args)
        return _normalize_created_resource(
            result,
            id_field="spreadsheetId",
            id_aliases=("spreadsheetId", "sheetId", "spreadsheet_id", "sheet_id", "fileId", "file_id", "id"),
            title=title,
            kind="sheets",
        )

    async def get_sheet_values(
        self,
        *,
        spreadsheet_id: str,
        range: str,
    ) -> dict[str, Any]:
        return await run_gog_json("sheets", "get", spreadsheet_id, range)

    async def update_sheet_values(
        self,
        *,
        spreadsheet_id: str,
        range: str,
        values_json: str,
    ) -> dict[str, Any]:
        """Update a range with a JSON 2D array, e.g. '[["Name","Score"],["Alice","95"]]'."""
        return await run_gog_json(
            "sheets", "update", spreadsheet_id, range,
            "--values-json", values_json, "--no-input",
        )

    async def append_sheet_values(
        self,
        *,
        spreadsheet_id: str,
        range: str,
        values_json: str,
    ) -> dict[str, Any]:
        """Append rows to a sheet range. values_json is a JSON 2D array."""
        return await run_gog_json(
            "sheets", "append", spreadsheet_id, range,
            "--values-json", values_json, "--no-input",
        )

    async def get_spreadsheet_metadata(
        self,
        *,
        spreadsheet_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json("sheets", "metadata", spreadsheet_id)

    async def find_replace_in_sheet(
        self,
        *,
        spreadsheet_id: str,
        find: str,
        replace: str,
    ) -> dict[str, Any]:
        return await run_gog_json(
            "sheets", "find-replace", spreadsheet_id, find, replace, "--no-input"
        )

    # ── Slides ────────────────────────────────────────────────────────────────

    async def create_presentation(
        self,
        *,
        title: str,
        parent_folder_id: str = "",
        template_id: str = "",
    ) -> dict[str, Any]:
        args = ["slides", "create", title, "--no-input"]
        if parent_folder_id:
            args += ["--parent", parent_folder_id]
        if template_id:
            args += ["--template", template_id]
        result = await run_gog_json(*args)
        return _normalize_created_resource(
            result,
            id_field="presentationId",
            id_aliases=("presentationId", "deckId", "presentation_id", "deck_id", "fileId", "file_id", "id"),
            title=title,
            kind="slides",
        )

    async def create_presentation_from_markdown(
        self,
        *,
        title: str,
        content: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        """Create a Slides deck from markdown. gog expects slide sections to start at H2."""
        content_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".md",
                encoding="utf-8",
                delete=False,
            ) as handle:
                handle.write(content)
                content_path = handle.name

            args = [
                "slides",
                "create-from-markdown",
                title,
                "--content-file",
                content_path,
                "--no-input",
            ]
            if parent_folder_id:
                args += ["--parent", parent_folder_id]
            try:
                result = await run_gog_json(*args)
            except GogInvocationError as exc:
                if not _looks_like_unknown_flag_error(str(exc), "--content-file"):
                    raise
                fallback_args = [
                    "slides",
                    "create-from-markdown",
                    title,
                    "--content",
                    content,
                    "--no-input",
                ]
                if parent_folder_id:
                    fallback_args += ["--parent", parent_folder_id]
                result = await run_gog_json(*fallback_args)
        finally:
            if content_path:
                Path(content_path).unlink(missing_ok=True)
        return _normalize_created_resource(
            result,
            id_field="presentationId",
            id_aliases=("presentationId", "deckId", "presentation_id", "deck_id", "fileId", "file_id", "id"),
            title=title,
            kind="slides",
        )

    async def create_presentation_from_template(
        self,
        *,
        template_id: str,
        title: str,
        replacements_json: str = "{}",
        parent_folder_id: str = "",
        exact_match: bool = False,
    ) -> dict[str, Any]:
        replacements_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                encoding="utf-8",
                delete=False,
            ) as handle:
                payload = replacements_json.strip() or "{}"
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = payload
                if isinstance(parsed, dict):
                    json.dump(parsed, handle, ensure_ascii=True, sort_keys=True)
                else:
                    handle.write(payload)
                replacements_path = handle.name

            args = [
                "slides",
                "create-from-template",
                template_id,
                title,
                "--replacements",
                replacements_path,
                "--no-input",
            ]
            if parent_folder_id:
                args += ["--parent", parent_folder_id]
            if exact_match:
                args.append("--exact")
            result = await run_gog_json(*args)
        finally:
            if replacements_path:
                Path(replacements_path).unlink(missing_ok=True)

        return _normalize_created_resource(
            result,
            id_field="presentationId",
            id_aliases=("presentationId", "deckId", "presentation_id", "deck_id", "fileId", "file_id", "id"),
            title=title,
            kind="slides",
        )

    async def get_presentation_info(
        self,
        *,
        presentation_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json("slides", "info", presentation_id)

    async def list_presentation_slides(
        self,
        *,
        presentation_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json("slides", "list-slides", presentation_id)

    async def read_presentation_slide(
        self,
        *,
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json("slides", "read-slide", presentation_id, slide_id)

    async def add_image_slide(
        self,
        *,
        presentation_id: str,
        image_path: str,
        speaker_notes: str = "",
        before_slide_id: str = "",
    ) -> dict[str, Any]:
        args = ["slides", "add-slide", presentation_id, image_path, "--no-input"]
        if speaker_notes:
            args += ["--notes", speaker_notes]
        if before_slide_id:
            args += ["--before", before_slide_id]
        return await run_gog_json(*args)

    async def replace_slide_image(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        image_path: str,
        speaker_notes: str = "",
    ) -> dict[str, Any]:
        args = [
            "slides",
            "replace-slide",
            presentation_id,
            slide_id,
            image_path,
            "--no-input",
        ]
        if speaker_notes:
            args += ["--notes", speaker_notes]
        return await run_gog_json(*args)

    async def update_slide_notes(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        speaker_notes: str,
    ) -> dict[str, Any]:
        return await run_gog_json(
            "slides",
            "update-notes",
            presentation_id,
            slide_id,
            "--notes",
            speaker_notes,
            "--no-input",
        )

    async def delete_presentation_slide(
        self,
        *,
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, Any]:
        return await run_gog_json(
            "slides",
            "delete-slide",
            presentation_id,
            slide_id,
            "--no-input",
        )

    async def copy_presentation(
        self,
        *,
        presentation_id: str,
        title: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        args = ["slides", "copy", presentation_id, title, "--no-input"]
        if parent_folder_id:
            args += ["--parent", parent_folder_id]
        result = await run_gog_json(*args)
        return _normalize_created_resource(
            result,
            id_field="presentationId",
            id_aliases=("presentationId", "deckId", "presentation_id", "deck_id", "fileId", "file_id", "id"),
            title=title,
            kind="slides",
        )


def _extract_datetime_str(value: Any) -> str:
    """Extract a datetime/date string from a Calendar API start/end object."""
    if isinstance(value, dict):
        return str(value.get("dateTime") or value.get("date") or "")
    return str(value or "")


def _looks_like_unknown_flag_error(message: str, flag: str) -> bool:
    lowered = message.casefold()
    normalized_flag = flag.casefold()
    return normalized_flag in lowered and (
        "unknown flag" in lowered
        or "flag provided but not defined" in lowered
        or "unknown shorthand flag" in lowered
    )


def _normalize_created_resource(
    payload: dict[str, Any],
    *,
    id_field: str,
    id_aliases: tuple[str, ...],
    title: str,
    kind: str,
) -> dict[str, Any]:
    resource = _best_resource_mapping(payload, id_aliases=id_aliases)
    normalized = dict(resource)

    resource_title = _first_present(resource, ("title", "name")) or _first_present(
        payload,
        ("title", "name"),
    ) or title
    resource_url = _first_present(
        resource,
        ("url", "webViewLink", "htmlLink", "link", "documentUrl", "spreadsheetUrl", "presentationUrl"),
    ) or _first_present(
        payload,
        ("url", "webViewLink", "htmlLink", "link", "documentUrl", "spreadsheetUrl", "presentationUrl"),
    )
    resource_id = _first_present(resource, id_aliases) or _first_present(payload, id_aliases)
    if not resource_id and resource_url:
        resource_id = _extract_google_resource_id_from_url(resource_url, kind)

    normalized[id_field] = resource_id
    normalized["title"] = resource_title
    if resource_url:
        normalized.setdefault("url", resource_url)
    return normalized


def _best_resource_mapping(
    payload: dict[str, Any],
    *,
    id_aliases: tuple[str, ...],
) -> dict[str, Any]:
    candidates = [candidate for candidate in _walk_mappings(payload)]
    if not candidates:
        return payload

    best = payload
    best_score = _mapping_score(payload, id_aliases=id_aliases)
    for candidate in candidates[1:]:
        score = _mapping_score(candidate, id_aliases=id_aliases)
        if score > best_score:
            best = candidate
            best_score = score
    return best


def _walk_mappings(node: Any) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    queue: list[Any] = [node]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            mappings.append(current)
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return mappings


def _mapping_score(mapping: dict[str, Any], *, id_aliases: tuple[str, ...]) -> int:
    score = 0
    specific_aliases = [alias for alias in id_aliases if alias != "id"]
    if any(_has_value(mapping.get(alias)) for alias in specific_aliases):
        score += 20
    if _has_value(mapping.get("id")) and "id" in id_aliases:
        score += 10
    if any(_has_value(mapping.get(key)) for key in ("title", "name")):
        score += 2
    if any(
        _has_value(mapping.get(key))
        for key in ("url", "webViewLink", "htmlLink", "link", "documentUrl", "spreadsheetUrl", "presentationUrl")
    ):
        score += 1
    return score


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if _has_value(value):
            return str(value).strip()
    return ""


def _has_value(value: Any) -> bool:
    return bool(str(value).strip()) if value is not None else False


def _extract_google_resource_id_from_url(url: str, kind: str) -> str:
    patterns = {
        "docs": r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)",
        "sheets": r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)",
        "slides": r"docs\.google\.com/presentation/d/([A-Za-z0-9_-]+)",
    }
    pattern = patterns.get(kind)
    if not pattern:
        return ""
    match = re.search(pattern, str(url))
    return match.group(1) if match else ""


def _project_gmail_thread(
    payload: dict[str, Any],
    *,
    format: str,
    metadata_headers: list[str] | None,
) -> dict[str, Any]:
    if format != "metadata":
        return payload

    allowed_headers = {header.lower() for header in metadata_headers or []}
    messages: list[dict[str, Any]] = []

    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        projected = {
            key: value
            for key, value in message.items()
            if key in {"id", "threadId", "labelIds", "snippet", "historyId", "internalDate", "sizeEstimate"}
        }
        msg_payload = message.get("payload")
        if isinstance(msg_payload, dict):
            headers = []
            for header in msg_payload.get("headers") or []:
                if not isinstance(header, dict):
                    continue
                name = str(header.get("name") or "").strip()
                if allowed_headers and name.lower() not in allowed_headers:
                    continue
                headers.append(header)
            projected["payload"] = {"headers": headers}
            if msg_payload.get("mimeType"):
                projected["payload"]["mimeType"] = msg_payload["mimeType"]
        messages.append(projected)

    result = dict(payload)
    result["messages"] = messages
    return result


def _extract_collection(
    payload: dict[str, Any],
    *,
    preferred_keys: tuple[str, ...],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    for key in preferred_keys:
        value = payload.get(key)
        if isinstance(value, list):
            items = [item for item in value if isinstance(item, dict)]
            return items, key
    return None, None


def _replace_collection(
    payload: dict[str, Any],
    key: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    result = dict(payload)
    result[key] = items
    if "count" in result:
        result["count"] = len(items)
    return result


def _sort_drive_entries(
    items: list[dict[str, Any]],
    order_by: str,
) -> list[dict[str, Any]]:
    ordered = list(items)
    clauses = [clause.strip() for clause in order_by.split(",") if clause.strip()]
    for clause in reversed(clauses):
        parts = clause.split()
        field = parts[0]
        reverse = len(parts) > 1 and parts[1].lower() == "desc"
        ordered.sort(key=lambda item, field=field: _sort_value(item.get(field)), reverse=reverse)
    return ordered


def _project_drive_entry(payload: dict[str, Any], fields: str | None) -> dict[str, Any]:
    requested = _requested_fields(fields)
    if not requested:
        return payload
    return {key: value for key, value in payload.items() if key in requested}


def _requested_fields(fields: str | None) -> set[str]:
    if not fields:
        return set()

    raw = fields.strip()
    if raw.startswith("files(") and raw.endswith(")"):
        raw = raw[len("files("):-1]

    requested: set[str] = set()
    for token in raw.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        requested.add(cleaned.split("(")[0].strip())
    return requested


def _sort_calendar_events(
    items: list[dict[str, Any]],
    order_by: str,
) -> list[dict[str, Any]]:
    if order_by == "updated":
        return sorted(items, key=lambda item: _sort_value(item.get("updated")))
    return sorted(items, key=lambda item: _sort_value(_calendar_start_value(item)))


def _calendar_start_value(item: dict[str, Any]) -> Any:
    start = item.get("start")
    if isinstance(start, dict):
        return start.get("dateTime") or start.get("date") or ""
    return start or ""


def _sort_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


# Module-level singleton.
_default_backend: GogWorkspaceBackend | None = None


def get_gog_backend() -> GogWorkspaceBackend:
    global _default_backend
    if _default_backend is None:
        _default_backend = GogWorkspaceBackend()
    return _default_backend
