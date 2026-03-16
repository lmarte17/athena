"""Abstract WorkspaceBackend protocol — transport-agnostic typed workspace operations."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WorkspaceBackend(Protocol):
    """Transport-agnostic interface for Google Workspace read/write operations."""

    # ── Gmail — read ──────────────────────────────────────────────────────────

    async def search_gmail_threads(
        self, *, query: str, max_results: int = 5
    ) -> dict[str, Any]: ...

    async def get_gmail_thread(
        self,
        *,
        thread_id: str,
        format: str = "metadata",
        metadata_headers: list[str] | None = None,
    ) -> dict[str, Any]: ...

    # ── Gmail — write ─────────────────────────────────────────────────────────

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
    ) -> dict[str, Any]: ...

    async def create_email_draft(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        reply_to_message_id: str = "",
    ) -> dict[str, Any]: ...

    async def mark_email_read(self, *, message_ids: list[str]) -> dict[str, Any]: ...

    async def mark_email_unread(self, *, message_ids: list[str]) -> dict[str, Any]: ...

    async def archive_email(self, *, message_ids: list[str]) -> dict[str, Any]: ...

    async def trash_email(self, *, message_ids: list[str]) -> dict[str, Any]: ...

    # ── Drive — read ──────────────────────────────────────────────────────────

    async def search_drive_files(
        self,
        *,
        query: str,
        page_size: int = 5,
        order_by: str = "modifiedTime desc",
        fields: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_drive_file(
        self, *, file_id: str, fields: str | None = None
    ) -> dict[str, Any]: ...

    # ── Drive — write ─────────────────────────────────────────────────────────

    async def copy_drive_file(
        self, *, file_id: str, name: str, parent_folder_id: str = ""
    ) -> dict[str, Any]: ...

    async def move_drive_file(
        self, *, file_id: str, parent_folder_id: str
    ) -> dict[str, Any]: ...

    async def rename_drive_file(
        self, *, file_id: str, new_name: str
    ) -> dict[str, Any]: ...

    async def share_drive_file(
        self,
        *,
        file_id: str,
        to: str = "user",
        email: str = "",
        role: str = "reader",
        discoverable: bool = False,
    ) -> dict[str, Any]: ...

    async def create_drive_folder(
        self, *, name: str, parent_folder_id: str = ""
    ) -> dict[str, Any]: ...

    async def delete_drive_file(self, *, file_id: str) -> dict[str, Any]: ...

    # ── Docs — read ───────────────────────────────────────────────────────────

    async def export_google_doc_text(self, *, document_id: str) -> str: ...

    async def get_google_doc_info(self, *, document_id: str) -> dict[str, Any]: ...

    # ── Docs — write ──────────────────────────────────────────────────────────

    async def create_google_doc(self, *, title: str) -> dict[str, Any]: ...

    async def write_google_doc(
        self, *, document_id: str, text: str, append: bool = False
    ) -> dict[str, Any]: ...

    async def copy_google_doc(
        self, *, document_id: str, title: str, parent_folder_id: str = ""
    ) -> dict[str, Any]: ...

    async def find_replace_in_doc(
        self, *, document_id: str, find: str, replace: str
    ) -> dict[str, Any]: ...

    async def clear_google_doc(self, *, document_id: str) -> dict[str, Any]: ...

    async def batch_update_google_doc(
        self, *, document_id: str, requests: list[dict[str, Any]]
    ) -> dict[str, Any]: ...

    # ── Calendar — read ───────────────────────────────────────────────────────

    async def list_calendar_events(
        self,
        *,
        calendar_id: str = "primary",
        time_min: str,
        time_max: str,
        max_results: int = 6,
        single_events: bool = True,
        order_by: str = "startTime",
    ) -> dict[str, Any]: ...

    async def get_calendar_event(
        self, *, event_id: str, calendar_id: str = "primary"
    ) -> dict[str, Any]: ...

    async def search_calendar_events(
        self,
        *,
        query: str,
        calendar_id: str = "primary",
        time_from: str = "",
        time_to: str = "",
        max_results: int = 10,
    ) -> dict[str, Any]: ...

    async def get_calendar_freebusy(
        self,
        *,
        time_min: str,
        time_max: str,
        calendar_ids: list[str] | None = None,
    ) -> dict[str, Any]: ...

    # ── Calendar — write ──────────────────────────────────────────────────────

    async def create_calendar_event(
        self, *, calendar_id: str = "primary", body: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def update_calendar_event(
        self, *, event_id: str, calendar_id: str = "primary", body: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def delete_calendar_event(
        self, *, event_id: str, calendar_id: str = "primary"
    ) -> dict[str, Any]: ...

    # ── Sheets — read ─────────────────────────────────────────────────────────

    async def get_spreadsheet_metadata(
        self, *, spreadsheet_id: str
    ) -> dict[str, Any]: ...

    async def get_sheet_values(
        self, *, spreadsheet_id: str, range: str
    ) -> dict[str, Any]: ...

    # ── Sheets — write ────────────────────────────────────────────────────────

    async def create_spreadsheet(
        self,
        *,
        title: str,
        sheets: list[str] | None = None,
        parent_folder_id: str = "",
    ) -> dict[str, Any]: ...

    async def update_sheet_values(
        self, *, spreadsheet_id: str, range: str, values_json: str
    ) -> dict[str, Any]: ...

    async def append_sheet_values(
        self, *, spreadsheet_id: str, range: str, values_json: str
    ) -> dict[str, Any]: ...

    async def find_replace_in_sheet(
        self, *, spreadsheet_id: str, find: str, replace: str
    ) -> dict[str, Any]: ...

    # ── Slides — read ─────────────────────────────────────────────────────────

    async def get_presentation_info(
        self, *, presentation_id: str
    ) -> dict[str, Any]: ...

    async def list_presentation_slides(
        self, *, presentation_id: str
    ) -> dict[str, Any]: ...

    async def read_presentation_slide(
        self, *, presentation_id: str, slide_id: str
    ) -> dict[str, Any]: ...

    # ── Slides — write ────────────────────────────────────────────────────────

    async def create_presentation(
        self, *, title: str, parent_folder_id: str = "", template_id: str = ""
    ) -> dict[str, Any]: ...

    async def create_presentation_from_markdown(
        self, *, title: str, content: str, parent_folder_id: str = ""
    ) -> dict[str, Any]: ...

    async def create_presentation_from_template(
        self,
        *,
        template_id: str,
        title: str,
        replacements_json: str = "{}",
        parent_folder_id: str = "",
        exact_match: bool = False,
    ) -> dict[str, Any]: ...

    async def add_image_slide(
        self,
        *,
        presentation_id: str,
        image_path: str,
        speaker_notes: str = "",
        before_slide_id: str = "",
    ) -> dict[str, Any]: ...

    async def replace_slide_image(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        image_path: str,
        speaker_notes: str = "",
    ) -> dict[str, Any]: ...

    async def update_slide_notes(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        speaker_notes: str,
    ) -> dict[str, Any]: ...

    async def delete_presentation_slide(
        self, *, presentation_id: str, slide_id: str
    ) -> dict[str, Any]: ...

    async def copy_presentation(
        self, *, presentation_id: str, title: str, parent_folder_id: str = ""
    ) -> dict[str, Any]: ...

    # ── Slides — advanced inspect/edit (hybrid backend) ─────────────────────

    async def inspect_presentation(
        self, *, presentation_id: str
    ) -> dict[str, Any]: ...

    async def inspect_slide(
        self, *, presentation_id: str, slide_id: str
    ) -> dict[str, Any]: ...

    async def list_slide_elements(
        self, *, presentation_id: str, slide_id: str, element_type: str = ""
    ) -> dict[str, Any]: ...

    async def inspect_slide_element(
        self, *, presentation_id: str, slide_id: str, element_id: str
    ) -> dict[str, Any]: ...

    async def replace_text_in_presentation(
        self,
        *,
        presentation_id: str,
        find: str,
        replace: str,
        match_case: bool = True,
    ) -> dict[str, Any]: ...

    async def set_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
        text: str,
    ) -> dict[str, Any]: ...

    async def append_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
        text: str,
    ) -> dict[str, Any]: ...

    async def clear_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
    ) -> dict[str, Any]: ...

    async def get_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
    ) -> dict[str, Any]: ...

    async def get_slide_notes(
        self, *, presentation_id: str, slide_id: str
    ) -> dict[str, Any]: ...

    async def clear_slide_notes(
        self, *, presentation_id: str, slide_id: str
    ) -> dict[str, Any]: ...

    async def create_slide(
        self,
        *,
        presentation_id: str,
        insertion_index: int | None = None,
        layout: str = "",
    ) -> dict[str, Any]: ...

    async def duplicate_slide(
        self, *, presentation_id: str, slide_id: str
    ) -> dict[str, Any]: ...

    async def reorder_slide(
        self, *, presentation_id: str, slide_id: str, insertion_index: int
    ) -> dict[str, Any]: ...

    async def set_slide_background(
        self, *, presentation_id: str, slide_id: str, color_hex: str
    ) -> dict[str, Any]: ...

    async def insert_slide_image(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        image_url: str = "",
        image_path: str = "",
        left_emu: float = 0.0,
        top_emu: float = 0.0,
        width_emu: float | None = None,
        height_emu: float | None = None,
    ) -> dict[str, Any]: ...

    async def replace_slide_image_element(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
        image_url: str = "",
        image_path: str = "",
    ) -> dict[str, Any]: ...

    async def resize_slide_image(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
        left_emu: float | None = None,
        top_emu: float | None = None,
        width_emu: float | None = None,
        height_emu: float | None = None,
    ) -> dict[str, Any]: ...

    async def inspect_presentation_template(
        self, *, presentation_id: str
    ) -> dict[str, Any]: ...

    async def fill_presentation_template(
        self, *, presentation_id: str, values_json: str
    ) -> dict[str, Any]: ...

    async def apply_presentation_theme(
        self,
        *,
        presentation_id: str,
        preset: str = "",
        theme_json: str = "",
    ) -> dict[str, Any]: ...
