import pytest

from app.agents.sheets_hydrator import SheetsHydrator
from app.resource_store import ResourceHandle


@pytest.mark.asyncio
async def test_sheets_hydrator_normalizes_tabular_content(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert kwargs == {}
        if args == ("sheets", "metadata", "sheet-123"):
            return {
                "properties": {"title": "Hiring Plan"},
                "sheets": [
                    {"properties": {"title": "Pipeline"}},
                    {"properties": {"title": "Interviews"}},
                ],
            }
        if args == ("sheets", "get", "sheet-123", "'Pipeline'"):
            return {"values": [["Role", "Status"], ["EM", "Open"]]}
        if args == ("sheets", "get", "sheet-123", "'Interviews'"):
            return {"values": [["Candidate", "Stage"], ["Sam", "Onsite"]]}
        raise AssertionError(f"Unexpected args: {args}")

    monkeypatch.setattr("app.agents.sheets_hydrator.run_gog_json", fake_run_gog_json)

    result = await SheetsHydrator().hydrate(
        ResourceHandle(
            source="sheets",
            kind="spreadsheet",
            id="sheet-123",
            title="",
        )
    )

    assert result is not None
    assert "Spreadsheet: Hiring Plan" in result.normalized_text
    assert "Sheet: Pipeline" in result.normalized_text
    assert "Role | Status" in result.normalized_text
    assert "Sam | Onsite" in result.normalized_text
    assert result.metadata["sheet_count"] == 2
