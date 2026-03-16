import base64

import pytest

from app.agents.gmail_hydrator import GmailHydrator
from app.resource_store import ResourceHandle


def _encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


@pytest.mark.asyncio
async def test_gmail_hydrator_normalizes_full_thread(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("gmail", "thread", "get", "thread-123")
        assert kwargs == {}
        return {
            "id": "thread-123",
            "historyId": "history-999",
            "messages": [
                {
                    "id": "message-1",
                    "internalDate": "1772812200000",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Sarah <sarah@example.com>"},
                            {"name": "To", "value": "Athena <athena@example.com>"},
                            {"name": "Date", "value": "Fri, 06 Mar 2026 13:30:00 -0500"},
                            {"name": "Subject", "value": "Budget follow-up"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": _encode("Here is the latest budget draft.")},
                    },
                },
                {
                    "id": "message-2",
                    "internalDate": "1772815800000",
                    "snippet": "Looks good to me.",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Athena <athena@example.com>"},
                            {"name": "To", "value": "Sarah <sarah@example.com>"},
                            {"name": "Date", "value": "Fri, 06 Mar 2026 02:30:00 PM -0500"},
                            {"name": "Subject", "value": "Re: Budget follow-up"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": _encode("Looks good to me.")},
                    },
                },
            ],
        }

    monkeypatch.setattr("app.agents.gmail_hydrator.run_gog_json", fake_run_gog_json)

    result = await GmailHydrator().hydrate(
        ResourceHandle(
            source="gmail",
            kind="thread",
            id="thread-123",
            title="Budget follow-up",
            version="1772815800000",
        )
    )

    assert result is not None
    assert result.handle.version == "history-999"
    assert result.metadata["message_count"] == 2
    assert "From: Sarah <sarah@example.com>" in result.normalized_text
    assert "Here is the latest budget draft." in result.normalized_text
    assert "Looks good to me." in result.normalized_text
