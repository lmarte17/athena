import pytest

from app.tools import workspace_tools


class _FakeSlidesBackend:
    def __init__(
        self,
        *,
        slide_lists: list[dict[str, object]] | None = None,
        slide_payloads: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.calls: list[dict[str, str]] = []
        self.template_calls: list[dict[str, object]] = []
        self.add_calls: list[dict[str, str]] = []
        self.replace_calls: list[dict[str, str]] = []
        self.notes_calls: list[dict[str, str]] = []
        self.delete_calls: list[dict[str, str]] = []
        self.list_calls: list[str] = []
        self.read_calls: list[tuple[str, str]] = []
        self._slide_lists = slide_lists or [{"slides": [{"objectId": "slide-1"}]}]
        self._slide_payloads = slide_payloads or {"slide-1": {"text": "Deck overview"}}
        self._list_index = 0

    async def create_presentation_from_markdown(
        self,
        *,
        title: str,
        content: str,
        parent_folder_id: str = "",
    ) -> dict[str, str]:
        self.calls.append(
            {
                "title": title,
                "content": content,
                "parent_folder_id": parent_folder_id,
            }
        )
        return {"presentationId": "pres-123"}

    async def create_presentation_from_template(
        self,
        *,
        template_id: str,
        title: str,
        replacements_json: str = "{}",
        parent_folder_id: str = "",
        exact_match: bool = False,
    ) -> dict[str, str]:
        self.template_calls.append(
            {
                "template_id": template_id,
                "title": title,
                "replacements_json": replacements_json,
                "parent_folder_id": parent_folder_id,
                "exact_match": exact_match,
            }
        )
        return {"presentationId": "templ-123"}

    async def list_presentation_slides(self, *, presentation_id: str) -> dict[str, object]:
        self.list_calls.append(presentation_id)
        index = min(self._list_index, len(self._slide_lists) - 1)
        self._list_index += 1
        return self._slide_lists[index]

    async def read_presentation_slide(
        self,
        *,
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, object]:
        self.read_calls.append((presentation_id, slide_id))
        return self._slide_payloads.get(slide_id, {})

    async def add_image_slide(
        self,
        *,
        presentation_id: str,
        image_path: str,
        speaker_notes: str = "",
        before_slide_id: str = "",
    ) -> dict[str, str]:
        self.add_calls.append(
            {
                "presentation_id": presentation_id,
                "image_path": image_path,
                "speaker_notes": speaker_notes,
                "before_slide_id": before_slide_id,
            }
        )
        return {"status": "ok"}

    async def replace_slide_image(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        image_path: str,
        speaker_notes: str = "",
    ) -> dict[str, str]:
        self.replace_calls.append(
            {
                "presentation_id": presentation_id,
                "slide_id": slide_id,
                "image_path": image_path,
                "speaker_notes": speaker_notes,
            }
        )
        return {"status": "ok"}

    async def update_slide_notes(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        speaker_notes: str,
    ) -> dict[str, str]:
        self.notes_calls.append(
            {
                "presentation_id": presentation_id,
                "slide_id": slide_id,
                "speaker_notes": speaker_notes,
            }
        )
        return {"status": "ok"}

    async def delete_presentation_slide(
        self,
        *,
        presentation_id: str,
        slide_id: str,
    ) -> dict[str, str]:
        self.delete_calls.append(
            {
                "presentation_id": presentation_id,
                "slide_id": slide_id,
            }
        )
        return {"status": "ok"}


class _FakeDocsBackend:
    async def create_google_doc(self, *, title: str) -> dict[str, str]:
        return {"title": title}


@pytest.mark.asyncio
async def test_create_presentation_from_markdown_rejects_empty_content(monkeypatch):
    backend = _FakeSlidesBackend()
    monkeypatch.setattr(workspace_tools, "_backend", lambda: backend)

    payload = await workspace_tools.create_presentation_from_markdown(
        title="Board Update",
        content="  \n\n  ",
    )

    assert payload == {
        "presentationId": "",
        "title": "Board Update",
        "error": "presentation_content_empty",
    }
    assert backend.calls == []


@pytest.mark.asyncio
async def test_create_presentation_from_markdown_wraps_plain_text_into_slide_markdown(monkeypatch):
    backend = _FakeSlidesBackend()
    monkeypatch.setattr(workspace_tools, "_backend", lambda: backend)

    payload = await workspace_tools.create_presentation_from_markdown(
        title="Quarterly Update",
        content=(
            "Revenue grew 15% year over year. Retention improved to 92%.\n"
            "Hiring remains paused until Q3."
        ),
        parent_folder_id="folder-1",
    )

    assert payload["presentationId"] == "pres-123"
    assert payload["url"] == "https://docs.google.com/presentation/d/pres-123/edit"
    assert backend.calls == [
        {
            "title": "Quarterly Update",
            "content": (
                "## Quarterly Update\n"
                "- Revenue grew 15% year over year.\n"
                "- Retention improved to 92%.\n"
                "- Hiring remains paused until Q3."
            ),
            "parent_folder_id": "folder-1",
        }
    ]


@pytest.mark.asyncio
async def test_create_presentation_from_markdown_normalizes_headed_sections(monkeypatch):
    backend = _FakeSlidesBackend()
    monkeypatch.setattr(workspace_tools, "_backend", lambda: backend)

    await workspace_tools.create_presentation_from_markdown(
        title="Ignored Fallback Title",
        content=(
            "## Overview\n"
            "Revenue grew 15% year over year. Retention improved to 92%.\n\n"
            "## Risks\n"
            "- Hiring remains paused\n"
        ),
    )

    assert backend.calls == [
        {
            "title": "Ignored Fallback Title",
            "content": (
                "## Overview\n"
                "- Revenue grew 15% year over year.\n"
                "- Retention improved to 92%.\n\n"
                "---\n\n"
                "## Risks\n"
                "- Hiring remains paused"
            ),
            "parent_folder_id": "",
        }
    ]


@pytest.mark.asyncio
async def test_create_presentation_from_markdown_rewrites_h1_slides_to_h2(monkeypatch):
    backend = _FakeSlidesBackend()
    monkeypatch.setattr(workspace_tools, "_backend", lambda: backend)

    await workspace_tools.create_presentation_from_markdown(
        title="Migration Deck",
        content=(
            "# Overview\n"
            "- Modernize network infrastructure\n\n"
            "# Current State\n"
            "- Legacy grey optics remain in service\n"
        ),
    )

    assert backend.calls == [
        {
            "title": "Migration Deck",
            "content": (
                "## Overview\n"
                "- Modernize network infrastructure\n\n"
                "---\n\n"
                "## Current State\n"
                "- Legacy grey optics remain in service"
            ),
            "parent_folder_id": "",
        }
    ]


@pytest.mark.asyncio
async def test_create_presentation_from_template_normalizes_response(monkeypatch):
    backend = _FakeSlidesBackend()
    monkeypatch.setattr(workspace_tools, "_backend", lambda: backend)

    payload = await workspace_tools.create_presentation_from_template(
        template_presentation_id="template-123",
        title="QBR Deck",
        replacements_json='{"title":"QBR"}',
        parent_folder_id="folder-1",
        exact_match=True,
    )

    assert payload == {
        "presentationId": "templ-123",
        "title": "QBR Deck",
        "url": "https://docs.google.com/presentation/d/templ-123/edit",
    }
    assert backend.template_calls == [
        {
            "template_id": "template-123",
            "title": "QBR Deck",
            "replacements_json": '{"title":"QBR"}',
            "parent_folder_id": "folder-1",
            "exact_match": True,
        }
    ]


@pytest.mark.asyncio
async def test_create_presentation_from_markdown_returns_error_for_empty_created_deck(monkeypatch):
    backend = _FakeSlidesBackend(slide_payloads={"slide-1": {}})
    monkeypatch.setattr(workspace_tools, "_backend", lambda: backend)
    monkeypatch.setattr(workspace_tools.asyncio, "sleep", _noop_sleep)

    payload = await workspace_tools.create_presentation_from_markdown(
        title="Quarterly Update",
        content="Revenue grew 15% year over year.",
    )

    assert payload["presentationId"] == "pres-123"
    assert payload["error"] == "presentation_created_empty"
    assert backend.list_calls == ["pres-123", "pres-123", "pres-123"]
    assert backend.read_calls == [
        ("pres-123", "slide-1"),
        ("pres-123", "slide-1"),
        ("pres-123", "slide-1"),
    ]


@pytest.mark.asyncio
async def test_create_presentation_from_markdown_retries_until_slide_text_is_visible(monkeypatch):
    backend = _FakeSlidesBackend(
        slide_lists=[
            {"slides": []},
            {"slides": [{"objectId": "slide-1"}]},
        ],
        slide_payloads={"slide-1": {"text": "Deck overview"}},
    )
    monkeypatch.setattr(workspace_tools, "_backend", lambda: backend)
    monkeypatch.setattr(workspace_tools.asyncio, "sleep", _noop_sleep)

    payload = await workspace_tools.create_presentation_from_markdown(
        title="Quarterly Update",
        content="Revenue grew 15% year over year.",
    )

    assert payload["presentationId"] == "pres-123"
    assert "error" not in payload
    assert backend.list_calls == ["pres-123", "pres-123"]
    assert backend.read_calls == [("pres-123", "slide-1")]


@pytest.mark.asyncio
async def test_create_presentation_from_markdown_skips_root_slide_id_when_real_slides_exist(
    monkeypatch,
):
    backend = _FakeSlidesBackend(
        slide_lists=[
            {
                "slides": [
                    {"objectId": "p"},
                    {"objectId": "slide-1"},
                ]
            }
        ],
        slide_payloads={
            "p": {},
            "slide-1": {"text": "Deck overview"},
        },
    )
    monkeypatch.setattr(workspace_tools, "_backend", lambda: backend)

    payload = await workspace_tools.create_presentation_from_markdown(
        title="Quarterly Update",
        content="Revenue grew 15% year over year.",
    )

    assert payload["presentationId"] == "pres-123"
    assert "error" not in payload
    assert backend.read_calls == [("pres-123", "slide-1")]


@pytest.mark.asyncio
async def test_create_google_doc_returns_error_when_backend_omits_document_id(monkeypatch):
    monkeypatch.setattr(workspace_tools, "_backend", lambda: _FakeDocsBackend())

    payload = await workspace_tools.create_google_doc("JFK Metro Migration Plan - Sections 2 & 3 Context")

    assert payload == {
        "documentId": "",
        "title": "JFK Metro Migration Plan - Sections 2 & 3 Context",
        "error": "create_google_doc_missing_document_id",
    }


async def _noop_sleep(_: float) -> None:
    return None
