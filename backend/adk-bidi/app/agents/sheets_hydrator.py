"""Background hydration for Google Sheets spreadsheet content."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from app.gog_client import run_gog_json
from app.hydration_types import HydrationResult
from app.resource_store import ResourceHandle


class SheetsHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "sheets" and handle.kind == "spreadsheet"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        metadata_payload = await run_gog_json("sheets", "metadata", handle.id)
        title = _spreadsheet_title(metadata_payload, handle.title)
        sheet_names = _sheet_names(metadata_payload, handle)

        sections: list[str] = [f"Spreadsheet: {title or handle.id}"]
        max_sheets = int(os.getenv("ATHENA_SHEETS_HYDRATION_MAX_SHEETS", "4"))

        for sheet_name in sheet_names[:max_sheets]:
            values_payload = await run_gog_json(
                "sheets",
                "get",
                handle.id,
                _sheet_range(sheet_name),
            )
            rows = _sheet_rows(values_payload)
            sections.append(_render_sheet(sheet_name, rows))

        normalized_text = "\n\n".join(section for section in sections if section).strip()
        if not normalized_text:
            return None

        metadata = {
            "sheet_names": sheet_names,
            "sheet_count": len(sheet_names),
        }

        version = str(
            metadata_payload.get("modifiedTime")
            or metadata_payload.get("updated")
            or handle.version
            or ""
        ).strip() or handle.version

        return HydrationResult(
            handle=replace(
                handle,
                title=title or handle.title,
                version=version,
                metadata={**handle.metadata, **metadata},
            ),
            normalized_text=normalized_text,
            metadata=metadata,
        )


def _spreadsheet_title(payload: dict[str, Any], fallback: str) -> str:
    if payload.get("properties") and isinstance(payload["properties"], dict):
        title = str(payload["properties"].get("title") or "").strip()
        if title:
            return title
    return str(payload.get("title") or fallback).strip()


def _sheet_names(payload: dict[str, Any], handle: ResourceHandle) -> list[str]:
    sheets = payload.get("sheets")
    if isinstance(sheets, list):
        titles = [_sheet_title(entry) for entry in sheets]
        titles = [title for title in titles if title]
        if titles:
            return titles

    raw_names = payload.get("sheetNames") or handle.metadata.get("sheets")
    if isinstance(raw_names, list):
        names = [str(name).strip() for name in raw_names if str(name).strip()]
        if names:
            return names

    return ["Sheet1"]


def _sheet_title(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("properties"), dict):
        title = str(value["properties"].get("title") or "").strip()
        if title:
            return title
    return str(value.get("title") or value.get("name") or "").strip()


def _sheet_range(sheet_name: str) -> str:
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def _sheet_rows(payload: dict[str, Any]) -> list[list[str]]:
    values = payload.get("values")
    if isinstance(values, list):
        return _normalize_rows(values)

    items = payload.get("items")
    if isinstance(items, list) and items and isinstance(items[0], list):
        return _normalize_rows(items)

    return []


def _normalize_rows(rows: list[Any]) -> list[list[str]]:
    normalized: list[list[str]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        normalized.append([str(cell) for cell in row])
    return normalized


def _render_sheet(sheet_name: str, rows: list[list[str]]) -> str:
    max_rows = int(os.getenv("ATHENA_SHEETS_HYDRATION_MAX_ROWS", "40"))
    max_cols = int(os.getenv("ATHENA_SHEETS_HYDRATION_MAX_COLS", "12"))

    lines = [f"Sheet: {sheet_name}"]
    if not rows:
        lines.append("(empty)")
        return "\n".join(lines)

    for row in rows[:max_rows]:
        cells = [cell.strip() for cell in row[:max_cols]]
        if len(row) > max_cols:
            cells.append("...")
        lines.append(" | ".join(cells).rstrip())

    if len(rows) > max_rows:
        lines.append("...")

    return "\n".join(lines).strip()
