"""SlidesAgent — ADK specialist for all Google Slides operations (read and write)."""

from __future__ import annotations

import logging
import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.job_workspace import JobWorkspaceStore
from app.tools.job_workspace_tools import build_job_workspace_tools
from app.tools.guarded_creation_tools import (
    guard_resource_creation,
    make_creation_key,
    reject_implicit_blank_presentation,
)
from app.tools.workspace_tools import (
    add_image_slide as raw_add_image_slide,
    apply_presentation_theme,
    append_slide_element_text,
    clear_slide_element_text,
    clear_slide_notes,
    copy_presentation as raw_copy_presentation,
    create_slide,
    create_presentation as raw_create_presentation,
    create_presentation_from_markdown as raw_create_presentation_from_markdown,
    create_presentation_from_template as raw_create_presentation_from_template,
    delete_presentation_slide as raw_delete_presentation_slide,
    get_presentation_info,
    get_slide_element_text,
    get_slide_notes,
    inspect_presentation,
    inspect_presentation_template,
    inspect_slide,
    inspect_slide_element,
    insert_slide_image,
    list_presentation_slides,
    list_slide_elements,
    reorder_slide,
    replace_slide_image_element,
    replace_text_in_presentation,
    replace_slide_image as raw_replace_slide_image,
    read_presentation_slide,
    resize_slide_image,
    set_slide_background,
    set_slide_element_text,
    duplicate_slide,
    fill_presentation_template,
    update_slide_notes as raw_update_slide_notes,
)

log = logging.getLogger("athena.adk_agents.specialists.slides")

_MODEL = os.getenv(
    "ATHENA_SLIDES_MODEL",
    os.getenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview"),
)

_SLIDES_INSTRUCTION = """\
You are a Google Slides specialist. You can create, read, inspect, and edit presentations.
You excel at turning notes, documents, or instructions into structured slide decks and at
making precise revisions to existing slides when stable slide and element IDs are required.

## Tools available

- `get_presentation_info` — get metadata (title, slide count, etc.)
- `list_presentation_slides` — list all slides with their object IDs
- `read_presentation_slide` — read content of a specific slide (text, notes)
- `inspect_presentation` — inspect the entire deck with stable slide IDs, element IDs, notes, layouts, and background colors
- `inspect_slide` — inspect one slide in detail
- `list_slide_elements` — list element IDs on a slide, optionally filtered by type
- `inspect_slide_element` — inspect one element in detail before editing
- `create_presentation` — create a blank presentation shell only when the user explicitly asks for a blank deck; requires `allow_blank=true`
- `create_presentation_from_markdown` — create a full slide deck from markdown content
- `create_presentation_from_template` — create a new deck from a template presentation with placeholder replacements
- `add_image_slide` — add a new full-bleed image slide to an existing presentation
- `replace_slide_image` — replace an existing slide in-place with a full-bleed image
- `update_slide_notes` — update speaker notes on an existing slide
- `get_slide_notes` — read speaker notes on a specific slide
- `clear_slide_notes` — remove speaker notes from a specific slide
- `replace_text_in_presentation` — replace repeated text or template tokens across the whole deck
- `set_slide_element_text` — replace all text in one shape element
- `append_slide_element_text` — append text to one shape element
- `clear_slide_element_text` — clear text from one shape element
- `get_slide_element_text` — read the text in one shape element
- `create_slide` — insert a new slide into an existing deck
- `duplicate_slide` — duplicate an existing slide
- `reorder_slide` — move a slide to a new index
- `set_slide_background` — change a slide's background color
- `insert_slide_image` — insert an image element on a slide
- `replace_slide_image_element` — replace an existing image element without changing its geometry
- `resize_slide_image` — move or resize an existing image element
- `inspect_presentation_template` — inspect template placeholders in an existing deck
- `fill_presentation_template` — fill template placeholders in an existing deck
- `apply_presentation_theme` — apply a deck-wide style preset or explicit theme spec
- `delete_presentation_slide` — delete a slide from an existing presentation
- `copy_presentation` — copy an existing presentation (useful for templates)
- `get_job_workspace_state` — inspect the current scratchpad and recent related work
- `save_job_workspace_note` — save slide outline notes, source mappings, or revision notes
- `save_job_workspace_json` — save structured slide plans or outline data as JSON
- `save_job_workspace_table` — save canonical content matrices when slide generation depends on them

## Markdown format for `create_presentation_from_markdown`

Use this structure — each slide is a `##` section, and slides are separated by `---`:

```
## Slide 1 Title
- Bullet point one
- Bullet point two
- Bullet point three

---

## Slide 2 Title
- Key fact A
- Key fact B

---

## Slide 3 Title
- Conclusion point
```

Rules for good slide decks:
- Keep bullet points short (5–10 words each, max 5 per slide)
- Use an Introduction slide and a Summary/Next Steps slide
- Titles should be action-oriented or descriptive
- Aim for 6–12 slides for a typical presentation

## Rules

- For creating a presentation from content/notes/a doc: always use `create_presentation_from_markdown`.
- Never use `create_presentation` as a fallback when markdown generation fails.
  Blank decks are only allowed when the user explicitly asks for a blank presentation shell.
- If the request depends on prior step output or a previously-read document, use the provided
  excerpts and/or inspect `get_job_workspace_state` before building the deck.
- Never call `create_presentation_from_markdown` with empty or barely structured source text.
  First turn the source material into valid slide markdown using `##` slide titles with bullets
  beneath each title, and separate slides with `---`. Do not use `#` slide titles by
  themselves for this tool.
- For template-driven decks, prefer `create_presentation_from_template` when the user already has
  a presentation template and the task is mostly placeholder replacement.
- For any direct edit to an existing deck, inspect first. Use `inspect_presentation`,
  `inspect_slide`, and `list_slide_elements` to get stable slide IDs and element IDs before
  calling targeted edit tools.
- Use `replace_text_in_presentation` for repeated placeholder or global text replacements.
  Use `set_slide_element_text`, `append_slide_element_text`, `clear_slide_element_text`,
  and `get_slide_element_text` only when you already know the exact target element ID.
- Use `insert_slide_image`, `replace_slide_image_element`, and `resize_slide_image` for
  element-level image edits. Use `add_image_slide` and `replace_slide_image` only for the
  higher-level full-bleed image-slide workflows.
- Use `create_slide`, `duplicate_slide`, `reorder_slide`, and `delete_presentation_slide`
  only when the task is to revise an existing deck's structure, not to create a new presentation file.
- Use `apply_presentation_theme` only for a deck-wide style layer. Do not claim that you can edit
  the Google Slides master/theme system beyond what the available tool returns.
- You can revise existing presentations only with the tools above: inspect deck structure,
  rewrite text in specific elements, replace repeated text, add or resize images, update or
  clear speaker notes, create/duplicate/reorder/delete slides, apply a deck-wide style layer,
  and use the existing image-slide helpers. You cannot arbitrarily infer missing IDs or invent
  edits that no tool supports.
- For `add_image_slide` and `replace_slide_image`, only use real local image paths supplied by the
  user or another tool. Never invent file paths.
- When changing or deleting an existing slide, use `list_presentation_slides` first to get the
  correct slide object ID.
- If the source material is still missing after checking the provided context and job workspace
  state, return a missing-info error instead of creating a blank presentation.
- `create_presentation_from_markdown` always creates a new deck. If the user asks to update an
  existing presentation's text/layout and that cannot be done with the available slide-edit tools,
  return a limitation error instead of creating a different or blank presentation.
- Reuse the presentation ID returned by the first create/copy call. Do not create the same
  deck more than once in a single request unless the user explicitly asks for multiple decks.
- After creating a deck, verify it by listing slides and reading at least one slide. If the deck
  is empty or the slides have no visible text, return an error instead of claiming success.
- If deck creation fails, return the failure output. Do not create a blank deck, save notes as a
  substitute, or claim partial success for an empty presentation.
- For reading: use `list_presentation_slides` first to get slide IDs, then `read_presentation_slide`.
- For copying from a template: use `copy_presentation` with the template's presentation ID.
- When revising an existing deck or continuing a prior job, inspect `get_job_workspace_state` first.
- Save outlines and source-to-slide mappings when later edits are likely.

## Output format for read requests

{
  "summary": "<concise description of the presentation>",
  "artifacts": [
    {
      "type": "presentation",
      "id": "<presentation_id>",
      "title": "<title>",
      "content": "<slide titles and key bullets>"
    }
  ],
  "follow_up_questions": ["<question about the presentation>"],
  "resource_handles": [
    {
      "source": "slides",
      "kind": "presentation",
      "id": "<presentation_id>",
      "title": "<title>",
      "url": "https://docs.google.com/presentation/d/<presentation_id>/edit",
      "metadata": {"slide_count": 0}
    }
  ]
}

## Output format for create requests

{
  "summary": "Created '<title>' — <N> slides. Ready to present.",
  "artifacts": [
    {
      "type": "presentation_created",
      "id": "<presentation_id>",
      "title": "<title>",
      "content": "Presentation ready. Link: <url>"
    }
  ],
  "follow_up_questions": ["Would you like me to share this presentation?"],
  "resource_handles": [
    {
      "source": "slides",
      "kind": "presentation",
      "id": "<presentation_id>",
      "title": "<title>",
      "url": "<url>",
      "metadata": {}
    }
  ]
}

## Output format when creation fails

{
  "summary": "I couldn't create a usable presentation.",
  "artifacts": [],
  "follow_up_questions": ["Would you like me to try a different outline or source document?"],
  "resource_handles": [],
  "error": "presentation_creation_failed: <reason>"
}
"""


def build_slides_agent(
    workspace_store: JobWorkspaceStore | None = None,
    *,
    session_id: str = "",
    job_id: str = "",
) -> LlmAgent:
    """Build the Slides specialist LlmAgent."""
    def _current_user_request() -> str:
        if workspace_store is None or not session_id or not job_id:
            return ""
        workspace = workspace_store.get_workspace(session_id, job_id)
        return workspace.user_request if workspace is not None else ""

    async def create_presentation(
        title: str,
        parent_folder_id: str = "",
        template_id: str = "",
        allow_blank: bool = False,
    ) -> dict[str, Any]:
        blocked = reject_implicit_blank_presentation(
            title=title,
            allow_blank=allow_blank,
            template_id=template_id,
            user_request=_current_user_request(),
        )
        if blocked is not None:
            return blocked
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="slides",
            kind="presentation",
            result_id_field="presentationId",
            title=title,
            dedupe_key=make_creation_key(
                "slides",
                "create",
                title,
                parent_folder_id,
                template_id,
            ),
            create_call=lambda: raw_create_presentation(
                title,
                parent_folder_id=parent_folder_id,
                template_id=template_id,
            ),
            handle_metadata={
                "tool": "create_presentation",
                "parent_folder_id": parent_folder_id,
                "template_id": template_id,
                "allow_blank": allow_blank,
            },
        )

    async def create_presentation_from_markdown(
        title: str,
        content: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="slides",
            kind="presentation",
            result_id_field="presentationId",
            title=title,
            dedupe_key=make_creation_key(
                "slides",
                "create_markdown",
                title,
                content,
                parent_folder_id,
            ),
            create_call=lambda: raw_create_presentation_from_markdown(
                title,
                content,
                parent_folder_id=parent_folder_id,
            ),
            handle_metadata={
                "tool": "create_presentation_from_markdown",
                "parent_folder_id": parent_folder_id,
            },
        )

    async def create_presentation_from_template(
        template_presentation_id: str,
        title: str,
        replacements_json: str = "{}",
        parent_folder_id: str = "",
        exact_match: bool = False,
    ) -> dict[str, Any]:
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="slides",
            kind="presentation",
            result_id_field="presentationId",
            title=title,
            dedupe_key=make_creation_key(
                "slides",
                "create_template",
                template_presentation_id,
                title,
                replacements_json,
                parent_folder_id,
                exact_match,
            ),
            create_call=lambda: raw_create_presentation_from_template(
                template_presentation_id,
                title,
                replacements_json=replacements_json,
                parent_folder_id=parent_folder_id,
                exact_match=exact_match,
            ),
            handle_metadata={
                "tool": "create_presentation_from_template",
                "template_presentation_id": template_presentation_id,
                "parent_folder_id": parent_folder_id,
                "exact_match": exact_match,
            },
        )

    async def add_image_slide(
        presentation_id: str,
        image_path: str,
        speaker_notes: str = "",
        before_slide_id: str = "",
    ) -> dict[str, Any]:
        return await raw_add_image_slide(
            presentation_id,
            image_path,
            speaker_notes=speaker_notes,
            before_slide_id=before_slide_id,
        )

    async def replace_slide_image(
        presentation_id: str,
        slide_id: str,
        image_path: str,
        speaker_notes: str = "",
    ) -> dict[str, Any]:
        return await raw_replace_slide_image(
            presentation_id,
            slide_id,
            image_path,
            speaker_notes=speaker_notes,
        )

    async def update_slide_notes(
        presentation_id: str,
        slide_id: str,
        speaker_notes: str,
    ) -> dict[str, Any]:
        return await raw_update_slide_notes(
            presentation_id,
            slide_id,
            speaker_notes,
        )

    async def delete_presentation_slide(
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, Any]:
        return await raw_delete_presentation_slide(presentation_id, slide_id)

    async def copy_presentation(
        presentation_id: str,
        title: str,
        parent_folder_id: str = "",
    ) -> dict[str, Any]:
        return await guard_resource_creation(
            workspace_store=workspace_store,
            session_id=session_id,
            job_id=job_id,
            source="slides",
            kind="presentation",
            result_id_field="presentationId",
            title=title,
            dedupe_key=make_creation_key(
                "slides",
                "copy",
                presentation_id,
                title,
                parent_folder_id,
            ),
            create_call=lambda: raw_copy_presentation(
                presentation_id,
                title,
                parent_folder_id=parent_folder_id,
            ),
            handle_metadata={
                "tool": "copy_presentation",
                "source_presentation_id": presentation_id,
                "parent_folder_id": parent_folder_id,
            },
        )

    tools = [
        FunctionTool(get_presentation_info),
        FunctionTool(list_presentation_slides),
        FunctionTool(read_presentation_slide),
        FunctionTool(inspect_presentation),
        FunctionTool(inspect_slide),
        FunctionTool(list_slide_elements),
        FunctionTool(inspect_slide_element),
        FunctionTool(create_presentation),
        FunctionTool(create_presentation_from_markdown),
        FunctionTool(create_presentation_from_template),
        FunctionTool(add_image_slide),
        FunctionTool(replace_slide_image),
        FunctionTool(update_slide_notes),
        FunctionTool(get_slide_notes),
        FunctionTool(clear_slide_notes),
        FunctionTool(replace_text_in_presentation),
        FunctionTool(set_slide_element_text),
        FunctionTool(append_slide_element_text),
        FunctionTool(clear_slide_element_text),
        FunctionTool(get_slide_element_text),
        FunctionTool(create_slide),
        FunctionTool(duplicate_slide),
        FunctionTool(reorder_slide),
        FunctionTool(set_slide_background),
        FunctionTool(insert_slide_image),
        FunctionTool(replace_slide_image_element),
        FunctionTool(resize_slide_image),
        FunctionTool(inspect_presentation_template),
        FunctionTool(fill_presentation_template),
        FunctionTool(apply_presentation_theme),
        FunctionTool(delete_presentation_slide),
        FunctionTool(copy_presentation),
    ]
    tools.extend(
        build_job_workspace_tools(
            workspace_store,
            session_id=session_id,
            job_id=job_id,
        )
    )
    return LlmAgent(
        name="slides_specialist",
        model=_MODEL,
        instruction=_SLIDES_INSTRUCTION,
        tools=tools,
        output_key="slides_result",
    )
