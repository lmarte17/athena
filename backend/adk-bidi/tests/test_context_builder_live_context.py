from app.context_builder import (
    clear_live_context,
    get_context,
    register_context,
    unregister_context,
    update_live_context,
)


def test_get_context_includes_live_context_sections():
    session_id = "session-live"

    register_context(session_id, "Base context")
    update_live_context(session_id, "Drive", "Q1 report summary")

    bundle = get_context(session_id)

    assert "Base context" in bundle
    assert "## Fresh live context loaded during this session" in bundle
    assert "### Drive" in bundle
    assert "Q1 report summary" in bundle

    unregister_context(session_id)


def test_update_live_context_replaces_existing_label_and_can_clear():
    session_id = "session-replace"

    register_context(session_id, "Base context")
    update_live_context(session_id, "Calendar", "Old summary")
    update_live_context(session_id, "Calendar", "New summary")

    bundle = get_context(session_id)
    assert "Old summary" not in bundle
    assert "New summary" in bundle

    clear_live_context(session_id)
    assert get_context(session_id) == "Base context"

    unregister_context(session_id)


def test_update_live_context_preserves_multiline_structure():
    session_id = "session-multiline"

    register_context(session_id, "Base context")
    update_live_context(
        session_id,
        "Calendar",
        "Calendar for today:\n- Q1 Overview\n- Sales Call - Peterman Account",
    )

    bundle = get_context(session_id)
    assert "Calendar for today:\n- Q1 Overview\n- Sales Call - Peterman Account" in bundle

    unregister_context(session_id)
