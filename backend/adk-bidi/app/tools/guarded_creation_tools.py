"""Helpers that make create/copy tool calls idempotent within a single job."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.job_workspace import JobWorkspaceStore


def make_creation_key(*parts: Any) -> str:
    normalized = [_normalize_key_part(part) for part in parts]
    return "|".join(part for part in normalized if part)


async def guard_resource_creation(
    *,
    workspace_store: JobWorkspaceStore | None,
    session_id: str,
    job_id: str,
    source: str,
    kind: str,
    result_id_field: str,
    title: str,
    dedupe_key: str,
    create_call: Callable[[], Awaitable[dict[str, Any]]],
    handle_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if workspace_store is not None and session_id and job_id and dedupe_key:
        existing = workspace_store.lookup_generated_resource(
            session_id,
            job_id,
            dedupe_key=dedupe_key,
        )
        if existing is not None:
            return {
                result_id_field: existing["id"],
                "title": existing.get("title") or title,
                "url": existing.get("url"),
                "reused": True,
            }

    result = await create_call()
    resource_id = str(result.get(result_id_field) or "").strip()
    if (
        workspace_store is not None
        and session_id
        and job_id
        and dedupe_key
        and resource_id
        and not result.get("error")
    ):
        workspace_store.remember_generated_resource(
            session_id,
            job_id,
            dedupe_key=dedupe_key,
            source=source,
            kind=kind,
            resource_id=resource_id,
            title=str(result.get("title") or title or ""),
            url=result.get("url"),
            metadata=handle_metadata,
        )
    return result


def reject_implicit_blank_presentation(
    *,
    title: str,
    allow_blank: bool = False,
    template_id: str = "",
    user_request: str = "",
) -> dict[str, Any] | None:
    if str(template_id or "").strip():
        return None
    if allow_blank and _request_explicitly_allows_blank_presentation(user_request):
        return None
    return {
        "presentationId": "",
        "title": title,
        "error": "blank_presentation_requires_allow_blank",
    }


def _normalize_key_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        compact = re.sub(r"\s+", " ", value).strip().casefold()
        if len(compact) <= 80:
            return compact
        return hashlib.sha1(compact.encode("utf-8")).hexdigest()[:16]
    if isinstance(value, (list, tuple, set, dict)):
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True)
        if len(encoded) <= 80:
            return encoded
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:16]
    return str(value)


def _request_explicitly_allows_blank_presentation(user_request: str) -> bool:
    text = " ".join(str(user_request or "").split()).casefold()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "blank presentation",
            "blank slide deck",
            "blank deck",
            "empty presentation",
            "empty slide deck",
            "empty deck",
            "presentation shell",
            "slide deck shell",
            "deck shell",
            "placeholder deck",
        )
    )
