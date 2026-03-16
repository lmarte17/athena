import pytest

from app.agents.slides_hydrator import SlidesHydrator
from app.resource_store import ResourceHandle
from app.slides_agent_client import SlidesAgentUnavailableError


@pytest.mark.asyncio
async def test_slides_hydrator_prefers_slides_agent_inspect(monkeypatch):
    async def fake_run_slides_agent_json(*args, **kwargs):
        assert kwargs == {}
        assert args == ("deck", "inspect", "--presentation-id", "deck-123")
        return {
            "ok": True,
            "presentation": {
                "presentation_id": "deck-123",
                "title": "Quarterly Review",
                "slide_count": 2,
                "slides": [
                    {
                        "slide_id": "slide-1",
                        "slide_index": 0,
                        "layout_name": "Title and Body",
                        "notes_text": "Open with customer story",
                        "elements": [
                            {
                                "element_id": "title-1",
                                "element_type": "shape",
                                "placeholder_type": "TITLE",
                                "text": {"raw_text": "Overview"},
                            },
                            {
                                "element_id": "body-1",
                                "element_type": "shape",
                                "placeholder_type": "BODY",
                                "text": {"raw_text": "Revenue up 20%\nMargin improved"},
                            },
                        ],
                    },
                    {
                        "slide_id": "slide-2",
                        "slide_index": 1,
                        "layout_name": "Title and Body",
                        "notes_text": "Call out risks",
                        "elements": [
                            {
                                "element_id": "title-2",
                                "element_type": "shape",
                                "placeholder_type": "TITLE",
                                "text": {"raw_text": "Next Steps"},
                            },
                            {
                                "element_id": "body-2",
                                "element_type": "shape",
                                "placeholder_type": "BODY",
                                "text": {"raw_text": "Launch hiring plan"},
                            },
                        ],
                    },
                ],
            },
            "warnings": [],
            "errors": [],
        }

    async def fail_if_gog_called(*args, **kwargs):
        raise AssertionError(f"Unexpected gog fallback call: {args}, {kwargs}")

    monkeypatch.setattr("app.agents.slides_hydrator.run_slides_agent_json", fake_run_slides_agent_json)
    monkeypatch.setattr("app.agents.slides_hydrator.run_gog_json", fail_if_gog_called)

    result = await SlidesHydrator().hydrate(
        ResourceHandle(
            source="slides",
            kind="presentation",
            id="deck-123",
            title="",
        )
    )

    assert result is not None
    assert "Presentation: Quarterly Review" in result.normalized_text
    assert "Slide 1: Overview" in result.normalized_text
    assert "Revenue up 20%" in result.normalized_text
    assert "Notes:" in result.normalized_text
    assert "Call out risks" in result.normalized_text
    assert result.metadata["slide_count"] == 2


@pytest.mark.asyncio
async def test_slides_hydrator_falls_back_to_gog_when_slides_agent_unavailable(monkeypatch):
    async def fake_run_slides_agent_json(*args, **kwargs):
        del args, kwargs
        raise SlidesAgentUnavailableError("slides-agent not found")

    async def fake_run_gog_json(*args, **kwargs):
        assert kwargs == {}
        if args == ("slides", "info", "deck-123"):
            return {"title": "Quarterly Review", "revisionId": "rev-9"}
        if args == ("slides", "list-slides", "deck-123"):
            return {
                "slides": [
                    {"objectId": "slide-1", "title": "Overview"},
                    {"objectId": "slide-2", "title": "Next Steps"},
                ]
            }
        if args == ("slides", "read-slide", "deck-123", "slide-1"):
            return {"slide": {"text": ["Revenue up 20%", "Margin improved"], "notes": "Open with customer story"}}
        if args == ("slides", "read-slide", "deck-123", "slide-2"):
            return {"text": "Launch hiring plan", "speakerNotes": "Call out risks"}
        raise AssertionError(f"Unexpected args: {args}")

    monkeypatch.setattr("app.agents.slides_hydrator.run_slides_agent_json", fake_run_slides_agent_json)
    monkeypatch.setattr("app.agents.slides_hydrator.run_gog_json", fake_run_gog_json)

    result = await SlidesHydrator().hydrate(
        ResourceHandle(
            source="slides",
            kind="presentation",
            id="deck-123",
            title="",
        )
    )

    assert result is not None
    assert "Presentation: Quarterly Review" in result.normalized_text
    assert "Slide 1: Overview" in result.normalized_text
    assert "Revenue up 20%" in result.normalized_text
    assert "Notes:" in result.normalized_text
    assert "Call out risks" in result.normalized_text
    assert result.handle.version == "rev-9"
    assert result.metadata["slide_count"] == 2
