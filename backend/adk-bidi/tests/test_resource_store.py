from app.resource_store import ResourceHandle, SessionResourceStore


def test_resource_store_replaces_snapshot_when_version_changes():
    store = SessionResourceStore()

    first = store.upsert_metadata(
        "session-1",
        ResourceHandle(
            source="gmail",
            kind="thread",
            id="thread-123",
            title="Budget follow-up",
            version="1772815800000",
            metadata={"sender": "Sarah", "snippet": "Initial note"},
        ),
    )

    second = store.upsert_metadata(
        "session-1",
        ResourceHandle(
            source="gmail",
            kind="thread",
            id="thread-123",
            title="Budget follow-up",
            version="1772820000000",
            metadata={"sender": "Sarah", "snippet": "Updated note"},
        ),
    )

    assert first.handle.version == "1772815800000"
    assert second.handle.version == "1772820000000"
    assert second.status == "metadata_ready"

    stored = store.get_snapshot(
        "session-1",
        source="gmail",
        kind="thread",
        resource_id="thread-123",
    )
    assert stored is not None
    assert stored.handle.version == "1772820000000"
    assert stored.metadata["snippet"] == "Updated note"


def test_resource_store_clear_session_isolated():
    store = SessionResourceStore()

    store.upsert_metadata(
        "session-a",
        ResourceHandle(source="docs", kind="document", id="file-123", title="Interview Notes"),
    )
    store.upsert_metadata(
        "session-b",
        ResourceHandle(source="calendar", kind="event", id="event-456", title="Weekly Sync"),
    )

    store.clear_session("session-a")

    assert store.list_handles("session-a") == []
    assert [handle.id for handle in store.list_handles("session-b")] == ["event-456"]
