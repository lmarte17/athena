import pytest

from app.agents.calendar_hydrator import CalendarHydrator
from app.resource_store import ResourceHandle


@pytest.mark.asyncio
async def test_calendar_hydrator_normalizes_event_and_extracts_doc_relation(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("calendar", "get", "primary", "event-123")
        assert kwargs == {}
        return {
            "event": {
                "id": "event-123",
                "summary": "Interview Loop",
                "start": {"dateTime": "2026-03-08T15:00:00-05:00"},
                "end": {"dateTime": "2026-03-08T16:00:00-05:00"},
                "location": "Conference Room A",
                "updated": "2026-03-08T12:00:00Z",
                "htmlLink": "https://calendar.google.com/event?eid=event-123",
                "organizer": {"displayName": "Athena", "email": "athena@example.com"},
                "attendees": [
                    {"displayName": "Sarah", "email": "sarah@example.com", "responseStatus": "accepted"},
                ],
                "description": (
                    "Agenda doc: https://docs.google.com/document/d/doc-123/edit\n"
                    "Discuss hiring plan."
                ),
            },
        }

    monkeypatch.setattr("app.agents.calendar_hydrator.run_gog_json", fake_run_gog_json)

    result = await CalendarHydrator().hydrate(
        ResourceHandle(
            source="calendar",
            kind="event",
            id="event-123",
            title="Interview Loop",
            metadata={"calendar_id": "primary"},
        )
    )

    assert result is not None
    assert result.handle.version == "2026-03-08T12:00:00Z"
    assert "Event: Interview Loop" in result.normalized_text
    assert "Conference Room A" in result.normalized_text
    assert "Discuss hiring plan." in result.normalized_text
    assert result.relations[0].source == "docs"
    assert result.relations[0].id == "doc-123"
