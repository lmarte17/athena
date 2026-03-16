import pytest

from app.agents.doc_hydrator import DocHydrator
from app.resource_store import ResourceHandle


@pytest.mark.asyncio
async def test_doc_hydrator_exports_and_normalizes_plain_text(monkeypatch):
    async def fake_run_gog_text(*args, **kwargs):
        assert args == ("docs", "cat", "file-123")
        assert kwargs == {}
        return "Action items\r\n\r\n- Hire coordinator\r\n\r\n"

    monkeypatch.setattr("app.agents.doc_hydrator.run_gog_text", fake_run_gog_text)

    result = await DocHydrator().hydrate(
        ResourceHandle(
            source="docs",
            kind="document",
            id="file-123",
            title="Interview Notes",
        )
    )

    assert result is not None
    assert result.normalized_text == "Action items\n\n- Hire coordinator"
    assert result.handle.metadata["export_mime_type"] == "text/plain"
