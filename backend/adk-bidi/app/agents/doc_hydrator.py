"""Background hydration for Google Docs selected through Drive."""

from __future__ import annotations

from dataclasses import replace

from app.gog_client import run_gog_text
from app.hydration_types import HydrationResult
from app.resource_store import ResourceHandle


class DocHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "docs" and handle.kind == "document"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        exported = await run_gog_text("docs", "cat", handle.id)
        normalized = _normalize_text(exported)
        if not normalized:
            return None

        return HydrationResult(
            handle=replace(
                handle,
                metadata={
                    **handle.metadata,
                    "export_mime_type": "text/plain",
                },
            ),
            normalized_text=normalized,
            metadata={"export_mime_type": "text/plain"},
        )


def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").splitlines()]
    normalized: list[str] = []
    blank_run = 0

    for line in lines:
        if line.strip():
            blank_run = 0
            normalized.append(line.strip())
            continue

        blank_run += 1
        if blank_run == 1:
            normalized.append("")

    return "\n".join(normalized).strip()
