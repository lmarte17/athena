"""SlidesAgentWorkspaceBackend — advanced Google Slides operations via slides-agent."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from app.slides_agent_client import run_slides_agent_json


class SlidesAgentWorkspaceBackend:
    """Advanced Slides operations backed by the slides-agent CLI."""

    async def inspect_presentation(
        self,
        *,
        presentation_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "deck",
            "inspect",
            "--presentation-id",
            presentation_id,
        )

    async def inspect_slide(
        self,
        *,
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "slide",
            "inspect",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
        )

    async def list_slide_elements(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_type: str = "",
    ) -> dict[str, Any]:
        args = [
            "element",
            "list",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
        ]
        if element_type:
            args += ["--type", element_type]
        return await run_slides_agent_json(*args)

    async def inspect_slide_element(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "element",
            "inspect",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--element-id",
            element_id,
        )

    async def replace_text_in_presentation(
        self,
        *,
        presentation_id: str,
        find: str,
        replace: str,
        match_case: bool = True,
    ) -> dict[str, Any]:
        args = [
            "text",
            "replace",
            "--presentation-id",
            presentation_id,
            "--find",
            find,
            "--replace",
            replace,
        ]
        if not match_case:
            args.append("--no-match-case")
        return await run_slides_agent_json(*args)

    async def set_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
        text: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "text",
            "set",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--element-id",
            element_id,
            "--text",
            text,
        )

    async def append_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
        text: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "text",
            "append",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--element-id",
            element_id,
            "--text",
            text,
        )

    async def clear_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "text",
            "clear",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--element-id",
            element_id,
        )

    async def get_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "text",
            "get",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--element-id",
            element_id,
        )

    async def get_slide_notes(
        self,
        *,
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "notes",
            "get",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
        )

    async def clear_slide_notes(
        self,
        *,
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "notes",
            "clear",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
        )

    async def create_slide(
        self,
        *,
        presentation_id: str,
        insertion_index: int | None = None,
        layout: str = "",
    ) -> dict[str, Any]:
        args = [
            "slide",
            "create",
            "--presentation-id",
            presentation_id,
        ]
        if insertion_index is not None:
            args += ["--insertion-index", str(insertion_index)]
        if layout:
            args += ["--layout", layout]
        return await run_slides_agent_json(*args)

    async def duplicate_slide(
        self,
        *,
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "slide",
            "duplicate",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
        )

    async def reorder_slide(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        insertion_index: int,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "slide",
            "reorder",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--insertion-index",
            str(insertion_index),
        )

    async def set_slide_background(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        color_hex: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "slide",
            "background",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--color",
            color_hex,
        )

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
    ) -> dict[str, Any]:
        args = [
            "image",
            "insert",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--left",
            str(left_emu),
            "--top",
            str(top_emu),
        ]
        if image_url:
            args += ["--url", image_url]
        if image_path:
            args += ["--file", image_path]
        if width_emu is not None:
            args += ["--width", str(width_emu)]
        if height_emu is not None:
            args += ["--height", str(height_emu)]
        return await run_slides_agent_json(*args)

    async def replace_slide_image_element(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
        image_url: str = "",
        image_path: str = "",
    ) -> dict[str, Any]:
        args = [
            "image",
            "replace",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--element-id",
            element_id,
        ]
        if image_url:
            args += ["--url", image_url]
        if image_path:
            args += ["--file", image_path]
        return await run_slides_agent_json(*args)

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
    ) -> dict[str, Any]:
        args = [
            "image",
            "resize",
            "--presentation-id",
            presentation_id,
            "--slide-id",
            slide_id,
            "--element-id",
            element_id,
        ]
        if left_emu is not None:
            args += ["--left", str(left_emu)]
        if top_emu is not None:
            args += ["--top", str(top_emu)]
        if width_emu is not None:
            args += ["--width", str(width_emu)]
        if height_emu is not None:
            args += ["--height", str(height_emu)]
        return await run_slides_agent_json(*args)

    async def inspect_presentation_template(
        self,
        *,
        presentation_id: str,
    ) -> dict[str, Any]:
        return await run_slides_agent_json(
            "template",
            "inspect",
            "--presentation-id",
            presentation_id,
        )

    async def fill_presentation_template(
        self,
        *,
        presentation_id: str,
        values_json: str,
    ) -> dict[str, Any]:
        values_path = ""
        try:
            values_path = _write_json_file(values_json or "{}")
            return await run_slides_agent_json(
                "template",
                "fill",
                "--presentation-id",
                presentation_id,
                "--values-file",
                values_path,
            )
        finally:
            if values_path:
                Path(values_path).unlink(missing_ok=True)

    async def apply_presentation_theme(
        self,
        *,
        presentation_id: str,
        preset: str = "",
        theme_json: str = "",
    ) -> dict[str, Any]:
        args = [
            "theme",
            "apply",
            "--presentation-id",
            presentation_id,
        ]
        theme_path = ""
        try:
            if preset:
                args += ["--preset", preset]
            if theme_json:
                theme_path = _write_json_file(theme_json)
                args += ["--spec-file", theme_path]
            return await run_slides_agent_json(*args)
        finally:
            if theme_path:
                Path(theme_path).unlink(missing_ok=True)


def _write_json_file(raw_json: str) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        encoding="utf-8",
        delete=False,
    ) as handle:
        payload = raw_json.strip() or "{}"
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            handle.write(payload)
        else:
            json.dump(parsed, handle, ensure_ascii=True, sort_keys=True)
        return handle.name
