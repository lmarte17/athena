"""Background hydration for Google Slides presentation content."""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Any

from app.gog_client import run_gog_json
from app.hydration_types import HydrationResult
from app.resource_store import ResourceHandle
from app.slides_agent_client import SlidesAgentError, run_slides_agent_json

log = logging.getLogger("athena.hydration.slides")


class SlidesHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "slides" and handle.kind == "presentation"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        try:
            result = await self._hydrate_with_slides_agent(handle)
        except SlidesAgentError as exc:
            log.debug("slides-agent hydration unavailable for %s: %s", handle.id, exc)
            result = None
        except Exception:
            log.debug("slides-agent hydration failed for %s", handle.id, exc_info=True)
            result = None

        if result is not None:
            return result

        return await self._hydrate_with_gog(handle)

    async def _hydrate_with_slides_agent(self, handle: ResourceHandle) -> HydrationResult | None:
        payload = await run_slides_agent_json(
            "deck",
            "inspect",
            "--presentation-id",
            handle.id,
        )
        presentation = payload.get("presentation")
        if not isinstance(presentation, dict):
            return None

        title = str(presentation.get("title") or handle.title or handle.id).strip()
        slides = presentation.get("slides") or []
        if not isinstance(slides, list):
            slides = []

        sections: list[str] = [f"Presentation: {title or handle.id}"]
        max_slides = int(os.getenv("ATHENA_SLIDES_HYDRATION_MAX_SLIDES", "20"))

        for index, slide in enumerate(slides[:max_slides], start=1):
            if not isinstance(slide, dict):
                continue
            body = _slide_body_from_inspect(slide)
            notes_text = str(slide.get("notes_text") or "").strip()
            notes = [line for line in notes_text.splitlines() if line.strip()]
            sections.append(_render_slide(index, _slide_title_from_inspect(slide, index), body, notes))

        normalized_text = "\n\n".join(section for section in sections if section).strip()
        if not normalized_text:
            return None

        slide_count = int(presentation.get("slide_count") or len(slides))
        metadata = {"slide_count": slide_count}

        return HydrationResult(
            handle=replace(
                handle,
                title=title or handle.title,
                metadata={**handle.metadata, **metadata},
            ),
            normalized_text=normalized_text,
            metadata=metadata,
        )

    async def _hydrate_with_gog(self, handle: ResourceHandle) -> HydrationResult | None:
        info_payload = await run_gog_json("slides", "info", handle.id)
        title = _presentation_title(info_payload, handle.title)
        slides_payload = await run_gog_json("slides", "list-slides", handle.id)
        slides = _extract_slides(slides_payload)

        sections: list[str] = [f"Presentation: {title or handle.id}"]
        max_slides = int(os.getenv("ATHENA_SLIDES_HYDRATION_MAX_SLIDES", "20"))

        for index, slide in enumerate(slides[:max_slides], start=1):
            slide_payload = await run_gog_json("slides", "read-slide", handle.id, slide["id"])
            body = _dedupe_lines(_collect_text(slide_payload, exclude_notes=True))
            notes = _dedupe_lines(_collect_text(slide_payload, exclude_notes=False, notes_only=True))
            sections.append(_render_slide(index, slide["title"], body, notes))

        normalized_text = "\n\n".join(section for section in sections if section).strip()
        if not normalized_text:
            return None

        metadata = {"slide_count": len(slides)}
        version = str(
            info_payload.get("revisionId")
            or info_payload.get("updated")
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


def _presentation_title(payload: dict[str, Any], fallback: str) -> str:
    title = str(payload.get("title") or "").strip()
    if title:
        return title
    if isinstance(payload.get("presentation"), dict):
        return str(payload["presentation"].get("title") or fallback).strip()
    return fallback


def _extract_slides(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw = payload.get("slides") or payload.get("items") or []
    slides: list[dict[str, str]] = []
    for index, value in enumerate(raw, start=1):
        if not isinstance(value, dict):
            continue
        slide_id = str(
            value.get("objectId") or value.get("id") or value.get("slideId") or ""
        ).strip()
        if not slide_id:
            continue
        title = str(
            value.get("title") or value.get("layoutTitle") or f"Slide {index}"
        ).strip()
        slides.append({"id": slide_id, "title": title})
    return slides


def _render_slide(index: int, title: str, body: list[str], notes: list[str]) -> str:
    lines = [f"Slide {index}: {title or f'Slide {index}'}"]
    if body:
        lines.extend(body)
    if notes:
        lines.extend(["Notes:"] + notes)
    return "\n".join(lines).strip()


def _collect_text(
    node: Any,
    *,
    exclude_notes: bool = False,
    notes_only: bool = False,
) -> list[str]:
    fragments: list[str] = []

    def walk(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = key.lower()
                if notes_only and lowered in {"notes", "speakernotes", "speaker_notes"}:
                    fragments.extend(_string_values(child))
                    continue
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
    return fragments


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


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        cleaned = " ".join(line.replace("\r\n", "\n").split()).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def _slide_title_from_inspect(slide: dict[str, Any], index: int) -> str:
    for element in slide.get("elements") or []:
        if not isinstance(element, dict):
            continue
        placeholder = str(element.get("placeholder_type") or "").upper()
        text = _inspect_element_text(element)
        if placeholder in {"TITLE", "CENTERED_TITLE", "SECTION_HEADER", "SUBTITLE"} and text:
            return text.splitlines()[0].strip()
    for element in slide.get("elements") or []:
        if not isinstance(element, dict):
            continue
        text = _inspect_element_text(element)
        if text:
            return text.splitlines()[0].strip()
    return str(slide.get("layout_name") or f"Slide {index}").strip()


def _slide_body_from_inspect(slide: dict[str, Any]) -> list[str]:
    title = _slide_title_from_inspect(slide, int(slide.get("slide_index") or 0) + 1)
    body: list[str] = []
    for element in slide.get("elements") or []:
        if not isinstance(element, dict):
            continue
        text = _inspect_element_text(element)
        if not text:
            continue
        for line in text.splitlines():
            cleaned = " ".join(line.split()).strip()
            if not cleaned or cleaned == title:
                continue
            body.append(cleaned)
    return _dedupe_lines(body)


def _inspect_element_text(element: dict[str, Any]) -> str:
    text = element.get("text")
    if not isinstance(text, dict):
        return ""
    return str(text.get("raw_text") or "").strip()
