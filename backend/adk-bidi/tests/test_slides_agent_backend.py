from pathlib import Path

import pytest

from app.tools.slides_agent_backend import SlidesAgentWorkspaceBackend


@pytest.mark.asyncio
async def test_slides_agent_backend_inspect_presentation_maps_arguments(monkeypatch):
    async def fake_run_slides_agent_json(*args, **kwargs):
        assert args == ("deck", "inspect", "--presentation-id", "deck-123")
        assert kwargs == {}
        return {"ok": True, "presentation": {"presentation_id": "deck-123"}}

    monkeypatch.setattr(
        "app.tools.slides_agent_backend.run_slides_agent_json",
        fake_run_slides_agent_json,
    )

    payload = await SlidesAgentWorkspaceBackend().inspect_presentation(presentation_id="deck-123")

    assert payload["presentation"]["presentation_id"] == "deck-123"


@pytest.mark.asyncio
async def test_slides_agent_backend_replace_text_maps_no_match_case(monkeypatch):
    async def fake_run_slides_agent_json(*args, **kwargs):
        assert args == (
            "text",
            "replace",
            "--presentation-id",
            "deck-123",
            "--find",
            "{{customer}}",
            "--replace",
            "Acme",
            "--no-match-case",
        )
        assert kwargs == {}
        return {"ok": True}

    monkeypatch.setattr(
        "app.tools.slides_agent_backend.run_slides_agent_json",
        fake_run_slides_agent_json,
    )

    payload = await SlidesAgentWorkspaceBackend().replace_text_in_presentation(
        presentation_id="deck-123",
        find="{{customer}}",
        replace="Acme",
        match_case=False,
    )

    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_slides_agent_backend_create_slide_maps_optional_arguments(monkeypatch):
    async def fake_run_slides_agent_json(*args, **kwargs):
        assert args == (
            "slide",
            "create",
            "--presentation-id",
            "deck-123",
            "--insertion-index",
            "2",
            "--layout",
            "TITLE_AND_BODY",
        )
        assert kwargs == {}
        return {"ok": True}

    monkeypatch.setattr(
        "app.tools.slides_agent_backend.run_slides_agent_json",
        fake_run_slides_agent_json,
    )

    payload = await SlidesAgentWorkspaceBackend().create_slide(
        presentation_id="deck-123",
        insertion_index=2,
        layout="TITLE_AND_BODY",
    )

    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_slides_agent_backend_fill_presentation_template_writes_temp_file(monkeypatch):
    seen_path: list[str] = []
    seen_content: list[str] = []

    async def fake_run_slides_agent_json(*args, **kwargs):
        assert args[:4] == (
            "template",
            "fill",
            "--presentation-id",
            "deck-123",
        )
        assert args[4] == "--values-file"
        values_path = args[5]
        assert isinstance(values_path, str)
        seen_path.append(values_path)
        seen_content.append(Path(values_path).read_text(encoding="utf-8"))
        assert kwargs == {}
        return {"ok": True}

    monkeypatch.setattr(
        "app.tools.slides_agent_backend.run_slides_agent_json",
        fake_run_slides_agent_json,
    )

    payload = await SlidesAgentWorkspaceBackend().fill_presentation_template(
        presentation_id="deck-123",
        values_json='{"customer":"Acme","date":"2026-03-16"}',
    )

    assert payload["ok"] is True
    assert seen_content == ['{"customer": "Acme", "date": "2026-03-16"}']
    assert seen_path
    assert not Path(seen_path[0]).exists()


@pytest.mark.asyncio
async def test_slides_agent_backend_apply_theme_writes_temp_file(monkeypatch):
    seen_path: list[str] = []
    seen_content: list[str] = []

    async def fake_run_slides_agent_json(*args, **kwargs):
        assert args[:4] == (
            "theme",
            "apply",
            "--presentation-id",
            "deck-123",
        )
        assert args[4] == "--spec-file"
        spec_path = args[5]
        assert isinstance(spec_path, str)
        seen_path.append(spec_path)
        seen_content.append(Path(spec_path).read_text(encoding="utf-8"))
        assert kwargs == {}
        return {"ok": True}

    monkeypatch.setattr(
        "app.tools.slides_agent_backend.run_slides_agent_json",
        fake_run_slides_agent_json,
    )

    payload = await SlidesAgentWorkspaceBackend().apply_presentation_theme(
        presentation_id="deck-123",
        theme_json='{"background_color":"#1A73E8"}',
    )

    assert payload["ok"] is True
    assert seen_content == ['{"background_color": "#1A73E8"}']
    assert seen_path
    assert not Path(seen_path[0]).exists()
