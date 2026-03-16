from pathlib import Path

import pytest

from app.gog_client import GogInvocationError
from app.tools.gog_backend import GogWorkspaceBackend


@pytest.mark.asyncio
async def test_gog_backend_create_google_doc_extracts_id_from_file_payload(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("docs", "create", "test", "--no-input")
        assert kwargs == {}
        return {
            "file": {
                "id": "1GqIeTY9berTQLLlNl-_5ewcCHp-6SCf90CowzEavDww",
                "mimeType": "application/vnd.google-apps.document",
                "name": "test",
                "webViewLink": (
                    "https://docs.google.com/document/d/"
                    "1GqIeTY9berTQLLlNl-_5ewcCHp-6SCf90CowzEavDww/edit?usp=drivesdk"
                ),
            }
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().create_google_doc(title="test")

    assert payload["documentId"] == "1GqIeTY9berTQLLlNl-_5ewcCHp-6SCf90CowzEavDww"
    assert payload["title"] == "test"
    assert payload["url"] == (
        "https://docs.google.com/document/d/"
        "1GqIeTY9berTQLLlNl-_5ewcCHp-6SCf90CowzEavDww/edit?usp=drivesdk"
    )


@pytest.mark.asyncio
async def test_gog_backend_create_google_doc_extracts_id_from_nested_url_payload(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("docs", "create", "Migration Plan", "--no-input")
        assert kwargs == {}
        return {
            "result": {
                "document": {
                    "title": "Migration Plan",
                    "url": "https://docs.google.com/document/d/doc-123/edit",
                }
            }
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().create_google_doc(title="Migration Plan")

    assert payload["documentId"] == "doc-123"
    assert payload["title"] == "Migration Plan"
    assert payload["url"] == "https://docs.google.com/document/d/doc-123/edit"


@pytest.mark.asyncio
async def test_gog_backend_create_spreadsheet_extracts_nested_id(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("sheets", "create", "Q1 Sheet", "--no-input")
        assert kwargs == {}
        return {
            "spreadsheet": {
                "spreadsheetId": "sheet-123",
                "title": "Q1 Sheet",
                "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet-123/edit",
            }
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().create_spreadsheet(title="Q1 Sheet")

    assert payload["spreadsheetId"] == "sheet-123"
    assert payload["title"] == "Q1 Sheet"
    assert payload["url"] == "https://docs.google.com/spreadsheets/d/sheet-123/edit"


@pytest.mark.asyncio
async def test_gog_backend_create_spreadsheet_handles_real_top_level_payload(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("sheets", "create", "test", "--no-input")
        assert kwargs == {}
        return {
            "spreadsheetId": "1prPoIREbLYYvNGwtXZ8C9GtwXIb9uhDWw1Or8cal7QQ",
            "spreadsheetUrl": (
                "https://docs.google.com/spreadsheets/d/"
                "1prPoIREbLYYvNGwtXZ8C9GtwXIb9uhDWw1Or8cal7QQ/edit?ouid=115792517547125851203"
            ),
            "title": "test",
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().create_spreadsheet(title="test")

    assert payload["spreadsheetId"] == "1prPoIREbLYYvNGwtXZ8C9GtwXIb9uhDWw1Or8cal7QQ"
    assert payload["title"] == "test"
    assert payload["url"] == (
        "https://docs.google.com/spreadsheets/d/"
        "1prPoIREbLYYvNGwtXZ8C9GtwXIb9uhDWw1Or8cal7QQ/edit?ouid=115792517547125851203"
    )


@pytest.mark.asyncio
async def test_gog_backend_create_presentation_handles_real_file_payload(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("slides", "create", "test", "--no-input")
        assert kwargs == {}
        return {
            "file": {
                "id": "1xMgTXsP_oL8Yt5PmGiJdVogGy1uVSmeIjG8d-ALifE0",
                "mimeType": "application/vnd.google-apps.presentation",
                "name": "test",
                "webViewLink": (
                    "https://docs.google.com/presentation/d/"
                    "1xMgTXsP_oL8Yt5PmGiJdVogGy1uVSmeIjG8d-ALifE0/edit?usp=drivesdk"
                ),
            }
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().create_presentation(title="test")

    assert payload["presentationId"] == "1xMgTXsP_oL8Yt5PmGiJdVogGy1uVSmeIjG8d-ALifE0"
    assert payload["title"] == "test"
    assert payload["url"] == (
        "https://docs.google.com/presentation/d/"
        "1xMgTXsP_oL8Yt5PmGiJdVogGy1uVSmeIjG8d-ALifE0/edit?usp=drivesdk"
    )


@pytest.mark.asyncio
async def test_gog_backend_create_presentation_from_markdown_writes_temp_file(monkeypatch):
    seen_content: list[str] = []
    seen_path: list[str] = []

    async def fake_run_gog_json(*args, **kwargs):
        assert args[:4] == (
            "slides",
            "create-from-markdown",
            "Board Deck",
            "--content-file",
        )
        content_path = args[4]
        assert isinstance(content_path, str)
        seen_path.append(content_path)
        seen_content.append(Path(content_path).read_text(encoding="utf-8"))
        assert args[5:] == ("--no-input",)
        assert kwargs == {}
        return {
            "created": {
                "fileId": "deck-123",
                "name": "Board Deck",
                "webViewLink": "https://docs.google.com/presentation/d/deck-123/edit",
            }
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().create_presentation_from_markdown(
        title="Board Deck",
        content="# Intro\n- Point",
    )

    assert payload["presentationId"] == "deck-123"
    assert payload["title"] == "Board Deck"
    assert payload["url"] == "https://docs.google.com/presentation/d/deck-123/edit"
    assert seen_content == ["# Intro\n- Point"]
    assert seen_path
    assert not Path(seen_path[0]).exists()


@pytest.mark.asyncio
async def test_gog_backend_create_presentation_from_markdown_falls_back_to_inline_content(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_run_gog_json(*args, **kwargs):
        calls.append(args)
        assert kwargs == {}
        if "--content-file" in args:
            raise GogInvocationError("unknown flag: --content-file")
        assert args == (
            "slides",
            "create-from-markdown",
            "Board Deck",
            "--content",
            "# Intro\n- Point",
            "--no-input",
        )
        return {
            "created": {
                "fileId": "deck-legacy",
                "name": "Board Deck",
                "webViewLink": "https://docs.google.com/presentation/d/deck-legacy/edit",
            }
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().create_presentation_from_markdown(
        title="Board Deck",
        content="# Intro\n- Point",
    )

    assert payload["presentationId"] == "deck-legacy"
    assert payload["title"] == "Board Deck"
    assert payload["url"] == "https://docs.google.com/presentation/d/deck-legacy/edit"
    assert len(calls) == 2
    assert "--content-file" in calls[0]
    assert "--content" in calls[1]


@pytest.mark.asyncio
async def test_gog_backend_create_presentation_from_template_writes_replacements_file(monkeypatch):
    seen_payloads: list[str] = []
    seen_path: list[str] = []

    async def fake_run_gog_json(*args, **kwargs):
        assert args[:5] == (
            "slides",
            "create-from-template",
            "template-123",
            "Board Deck",
            "--replacements",
        )
        replacements_path = args[5]
        seen_path.append(replacements_path)
        seen_payloads.append(Path(replacements_path).read_text(encoding="utf-8"))
        assert args[6:] == ("--no-input", "--parent", "folder-1", "--exact")
        assert kwargs == {}
        return {
            "created": {
                "fileId": "deck-template-123",
                "name": "Board Deck",
                "webViewLink": "https://docs.google.com/presentation/d/deck-template-123/edit",
            }
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().create_presentation_from_template(
        template_id="template-123",
        title="Board Deck",
        replacements_json='{"title":"Board","owner":"Athena"}',
        parent_folder_id="folder-1",
        exact_match=True,
    )

    assert payload["presentationId"] == "deck-template-123"
    assert payload["title"] == "Board Deck"
    assert payload["url"] == "https://docs.google.com/presentation/d/deck-template-123/edit"
    assert seen_payloads == ['{"owner": "Athena", "title": "Board"}']
    assert seen_path
    assert not Path(seen_path[0]).exists()


@pytest.mark.asyncio
async def test_gog_backend_add_image_slide_maps_arguments(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == (
            "slides",
            "add-slide",
            "deck-123",
            "/tmp/slide.png",
            "--no-input",
            "--notes",
            "Speaker notes",
            "--before",
            "slide-9",
        )
        assert kwargs == {}
        return {"status": "ok"}

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().add_image_slide(
        presentation_id="deck-123",
        image_path="/tmp/slide.png",
        speaker_notes="Speaker notes",
        before_slide_id="slide-9",
    )

    assert payload == {"status": "ok"}


@pytest.mark.asyncio
async def test_gog_backend_replace_slide_image_maps_arguments(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == (
            "slides",
            "replace-slide",
            "deck-123",
            "slide-2",
            "/tmp/replacement.png",
            "--no-input",
            "--notes",
            "Updated notes",
        )
        assert kwargs == {}
        return {"status": "ok"}

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().replace_slide_image(
        presentation_id="deck-123",
        slide_id="slide-2",
        image_path="/tmp/replacement.png",
        speaker_notes="Updated notes",
    )

    assert payload == {"status": "ok"}


@pytest.mark.asyncio
async def test_gog_backend_update_slide_notes_maps_arguments(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == (
            "slides",
            "update-notes",
            "deck-123",
            "slide-2",
            "--notes",
            "Fresh notes",
            "--no-input",
        )
        assert kwargs == {}
        return {"status": "ok"}

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().update_slide_notes(
        presentation_id="deck-123",
        slide_id="slide-2",
        speaker_notes="Fresh notes",
    )

    assert payload == {"status": "ok"}


@pytest.mark.asyncio
async def test_gog_backend_delete_presentation_slide_maps_arguments(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == (
            "slides",
            "delete-slide",
            "deck-123",
            "slide-2",
            "--no-input",
        )
        assert kwargs == {}
        return {"status": "ok"}

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().delete_presentation_slide(
        presentation_id="deck-123",
        slide_id="slide-2",
    )

    assert payload == {"status": "ok"}


@pytest.mark.asyncio
async def test_gog_backend_get_gmail_thread_metadata_filters_headers_and_strips_body(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == ("gmail", "thread", "get", "thread-123")
        assert kwargs == {}
        return {
            "id": "thread-123",
            "messages": [
                {
                    "id": "message-1",
                    "snippet": "Hello",
                    "payload": {
                        "mimeType": "multipart/alternative",
                        "headers": [
                            {"name": "From", "value": "Sarah <sarah@example.com>"},
                            {"name": "Subject", "value": "Budget"},
                            {"name": "Date", "value": "Fri, 06 Mar 2026 13:30:00 -0500"},
                        ],
                        "body": {"data": "abc"},
                        "parts": [{"body": {"data": "def"}}],
                    },
                }
            ],
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().get_gmail_thread(
        thread_id="thread-123",
        format="metadata",
        metadata_headers=["From", "Subject"],
    )

    message = payload["messages"][0]
    assert message["payload"]["headers"] == [
        {"name": "From", "value": "Sarah <sarah@example.com>"},
        {"name": "Subject", "value": "Budget"},
    ]
    assert "body" not in message["payload"]
    assert "parts" not in message["payload"]


@pytest.mark.asyncio
async def test_gog_backend_search_drive_files_orders_and_projects_fields(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == (
            "drive",
            "search",
            "name contains 'budget'",
            "--raw-query",
            "--max",
            "10",
        )
        assert kwargs == {}
        return {
            "files": [
                {
                    "id": "file-1",
                    "name": "Older",
                    "modifiedTime": "2026-03-10T10:00:00Z",
                    "webViewLink": "https://drive.google.com/file/d/file-1/view",
                    "mimeType": "text/plain",
                },
                {
                    "id": "file-2",
                    "name": "Newer",
                    "modifiedTime": "2026-03-11T10:00:00Z",
                    "webViewLink": "https://drive.google.com/file/d/file-2/view",
                    "mimeType": "text/plain",
                },
            ]
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().search_drive_files(
        query="name contains 'budget'",
        page_size=2,
        order_by="modifiedTime desc",
        fields="files(id,name,modifiedTime)",
    )

    assert payload["files"] == [
        {
            "id": "file-2",
            "name": "Newer",
            "modifiedTime": "2026-03-11T10:00:00Z",
        },
        {
            "id": "file-1",
            "name": "Older",
            "modifiedTime": "2026-03-10T10:00:00Z",
        },
    ]


@pytest.mark.asyncio
async def test_gog_backend_list_calendar_events_sorts_and_limits(monkeypatch):
    async def fake_run_gog_json(*args, **kwargs):
        assert args == (
            "calendar",
            "events",
            "primary",
            "--from",
            "2026-03-10T00:00:00Z",
            "--to",
            "2026-03-12T00:00:00Z",
        )
        assert kwargs == {}
        return {
            "items": [
                {"id": "event-2", "start": {"dateTime": "2026-03-11T12:00:00Z"}},
                {"id": "event-1", "start": {"dateTime": "2026-03-10T09:00:00Z"}},
            ]
        }

    monkeypatch.setattr("app.tools.gog_backend.run_gog_json", fake_run_gog_json)

    payload = await GogWorkspaceBackend().list_calendar_events(
        calendar_id="primary",
        time_min="2026-03-10T00:00:00Z",
        time_max="2026-03-12T00:00:00Z",
        max_results=1,
        order_by="startTime",
    )

    assert payload["items"] == [
        {"id": "event-1", "start": {"dateTime": "2026-03-10T09:00:00Z"}}
    ]
