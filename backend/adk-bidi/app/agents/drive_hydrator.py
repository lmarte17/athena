"""Background hydration for Drive file metadata and linked Workspace resources."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.gog_client import run_gog_json
from app.hydration_types import HydrationResult
from app.resource_store import ResourceHandle

_WORKSPACE_MIME_TYPES: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("docs", "document"),
    "application/vnd.google-apps.spreadsheet": ("sheets", "spreadsheet"),
    "application/vnd.google-apps.presentation": ("slides", "presentation"),
}


class DriveHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "drive"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        payload = await run_gog_json("drive", "get", handle.id)

        title = str(payload.get("name") or handle.title).strip()
        mime_type = str(payload.get("mimeType") or handle.metadata.get("mime_type") or "").strip()
        modified_time = str(
            payload.get("modifiedTime") or handle.metadata.get("modified_time") or ""
        ).strip()
        description = str(payload.get("description") or "").strip()
        owners = _owner_names(payload.get("owners"))
        url = str(payload.get("webViewLink") or handle.url or "").strip()

        lines = [f"Drive file: {title or handle.id}"]
        if mime_type:
            lines.append(f"Type: {mime_type}")
        if modified_time:
            lines.append(f"Modified: {modified_time}")
        if owners:
            lines.append(f"Owners: {', '.join(owners)}")
        if description:
            lines.extend(["", "Description:", description])
        if url:
            lines.extend(["", f"Link: {url}"])

        normalized_text = "\n".join(lines).strip()
        if not normalized_text:
            return None

        metadata = {
            "mime_type": mime_type,
            "modified_time": modified_time,
            "owners": owners,
            "description": description,
        }

        relations: tuple[ResourceHandle, ...] = ()
        if mime_type in _WORKSPACE_MIME_TYPES:
            source, kind = _WORKSPACE_MIME_TYPES[mime_type]
            relations = (
                ResourceHandle(
                    source=source,  # type: ignore[arg-type]
                    kind=kind,
                    id=handle.id,
                    title=title or handle.title,
                    url=url or None,
                    metadata={"discovered_via": "drive_metadata"},
                ),
            )

        return HydrationResult(
            handle=replace(
                handle,
                title=title or handle.title,
                url=url or handle.url,
                version=modified_time or handle.version,
                metadata={**handle.metadata, **metadata},
            ),
            normalized_text=normalized_text,
            metadata=metadata,
            relations=relations,
        )


def _owner_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    owners: list[str] = []
    for owner in value:
        if not isinstance(owner, dict):
            continue
        display_name = str(owner.get("displayName") or "").strip()
        email = str(owner.get("emailAddress") or "").strip()
        if display_name:
            owners.append(display_name)
        elif email:
            owners.append(email)
    return owners
