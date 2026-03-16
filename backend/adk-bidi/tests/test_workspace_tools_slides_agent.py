import pytest

from app.slides_agent_client import SlidesAgentUnavailableError
from app.tools import workspace_tools


class _FakeAdvancedSlidesBackend:
    async def inspect_presentation(self, *, presentation_id: str) -> dict[str, object]:
        return {
            "ok": True,
            "presentation": {
                "presentation_id": presentation_id,
                "slides": [{"slide_id": "slide-1"}],
            },
            "warnings": [],
            "errors": [],
        }

    async def set_slide_element_text(
        self,
        *,
        presentation_id: str,
        slide_id: str,
        element_id: str,
        text: str,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "presentation_id": presentation_id,
            "applied_operations": [
                {
                    "type": "update_text",
                    "slide_id": slide_id,
                    "element_id": element_id,
                    "text": text,
                }
            ],
            "warnings": [],
            "errors": [],
        }

    async def apply_presentation_theme(
        self,
        *,
        presentation_id: str,
        preset: str = "",
        theme_json: str = "",
    ) -> dict[str, object]:
        return {
            "ok": True,
            "presentation_id": presentation_id,
            "applied_spec": preset or theme_json,
            "warnings": [],
            "errors": [],
        }


@pytest.mark.asyncio
async def test_inspect_presentation_passes_through_structured_payload(monkeypatch):
    monkeypatch.setattr(workspace_tools, "_backend", lambda: _FakeAdvancedSlidesBackend())

    payload = await workspace_tools.inspect_presentation("deck-123")

    assert payload["ok"] is True
    assert payload["presentation"]["presentation_id"] == "deck-123"


@pytest.mark.asyncio
async def test_set_slide_element_text_passes_through_structured_payload(monkeypatch):
    monkeypatch.setattr(workspace_tools, "_backend", lambda: _FakeAdvancedSlidesBackend())

    payload = await workspace_tools.set_slide_element_text(
        "deck-123",
        "slide-1",
        "title-1",
        "Updated title",
    )

    assert payload["ok"] is True
    assert payload["applied_operations"][0]["element_id"] == "title-1"


@pytest.mark.asyncio
async def test_apply_presentation_theme_supports_preset(monkeypatch):
    monkeypatch.setattr(workspace_tools, "_backend", lambda: _FakeAdvancedSlidesBackend())

    payload = await workspace_tools.apply_presentation_theme(
        "deck-123",
        preset="corporate-blue",
    )

    assert payload == {
        "ok": True,
        "presentation_id": "deck-123",
        "applied_spec": "corporate-blue",
        "warnings": [],
        "errors": [],
    }


@pytest.mark.asyncio
async def test_advanced_slides_wrapper_returns_structured_unavailable_error(monkeypatch):
    class _FailingBackend:
        async def inspect_presentation(self, *, presentation_id: str) -> dict[str, object]:
            raise SlidesAgentUnavailableError("slides-agent not found")

    monkeypatch.setattr(workspace_tools, "_backend", lambda: _FailingBackend())

    payload = await workspace_tools.inspect_presentation("deck-123")

    assert payload == {
        "ok": False,
        "error_code": "slides_agent_unavailable",
        "detail": "slides-agent not found",
        "isRetryable": False,
        "suggestedAction": "install_slides_agent",
    }
