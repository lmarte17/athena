import pytest

from app.agents.drive_hydrator import DriveHydrator
from app.resource_store import ResourceHandle


@pytest.mark.asyncio
async def test_drive_hydrator_normalizes_metadata_and_emits_workspace_relation(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("drive", "get", "file-123")
        assert kwargs == {}
        return {
            "id": "file-123",
            "name": "Quarterly Review",
            "mimeType": "application/vnd.google-apps.presentation",
            "modifiedTime": "2026-03-10T12:00:00Z",
            "webViewLink": "https://docs.google.com/presentation/d/file-123/edit",
            "owners": [{"displayName": "Athena"}],
            "description": "Board update deck.",
        }

    monkeypatch.setattr("app.agents.drive_hydrator.run_gog_json", fake_run_gog_json)

    result = await DriveHydrator().hydrate(
        ResourceHandle(
            source="drive",
            kind="file",
            id="file-123",
            title="",
        )
    )

    assert result is not None
    assert "Drive file: Quarterly Review" in result.normalized_text
    assert "Board update deck." in result.normalized_text
    assert result.handle.version == "2026-03-10T12:00:00Z"
    assert result.relations[0].source == "slides"
    assert result.relations[0].id == "file-123"
