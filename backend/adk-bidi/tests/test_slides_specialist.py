import importlib

from app.adk_agents.specialists import slides as slides_module


def test_slides_specialist_supports_dedicated_model_override(monkeypatch):
    monkeypatch.setenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview")
    monkeypatch.setenv("ATHENA_SLIDES_MODEL", "gemini-3.1-pro-preview")

    reloaded = importlib.reload(slides_module)

    assert reloaded._MODEL == "gemini-3.1-pro-preview"
    assert "verify it by listing slides and reading at least one slide" in reloaded._SLIDES_INSTRUCTION
    assert "requires `allow_blank=true`" in reloaded._SLIDES_INSTRUCTION
    assert "Never use `create_presentation` as a fallback" in reloaded._SLIDES_INSTRUCTION
    assert "slides are separated by `---`" in reloaded._SLIDES_INSTRUCTION
    assert "Do not use `#` slide titles by" in reloaded._SLIDES_INSTRUCTION
    assert "`create_presentation_from_template`" in reloaded._SLIDES_INSTRUCTION
    assert "`replace_slide_image`" in reloaded._SLIDES_INSTRUCTION
    assert "`inspect_presentation`" in reloaded._SLIDES_INSTRUCTION
    assert "`set_slide_element_text`" in reloaded._SLIDES_INSTRUCTION
    assert "`replace_text_in_presentation`" in reloaded._SLIDES_INSTRUCTION
    assert "`apply_presentation_theme`" in reloaded._SLIDES_INSTRUCTION
    assert "{{" not in reloaded._SLIDES_INSTRUCTION
    assert "You can revise existing presentations only with the tools above" in (
        reloaded._SLIDES_INSTRUCTION
    )

    monkeypatch.delenv("ATHENA_SLIDES_MODEL", raising=False)
    restored = importlib.reload(reloaded)

    assert restored._MODEL == "gemini-3.1-flash-lite-preview"
