import json
from types import SimpleNamespace

import pytest

from app.ws_downstream import DownstreamEventProcessor


def _make_part(*, text=None, data=None, thought=False):
    inline_data = SimpleNamespace(data=data) if data is not None else None
    return SimpleNamespace(text=text, inline_data=inline_data, thought=thought)


def _make_event(
    *,
    parts=None,
    input_text=None,
    output_text=None,
    interrupted=False,
    turn_complete=False,
):
    content = SimpleNamespace(parts=parts) if parts is not None else None
    input_tx = SimpleNamespace(text=input_text) if input_text is not None else None
    output_tx = SimpleNamespace(text=output_text) if output_text is not None else None
    return SimpleNamespace(
        content=content,
        input_transcription=input_tx,
        output_transcription=output_tx,
        interrupted=interrupted,
        turn_complete=turn_complete,
    )


@pytest.mark.asyncio
async def test_downstream_processor_forwards_transcripts_and_turn_complete():
    sent_text: list[dict] = []
    sent_bytes: list[bytes] = []
    broadcast: list[dict] = []
    completed: list[tuple[str, str, str]] = []

    async def send_text(raw: str):
        sent_text.append(json.loads(raw))

    async def send_bytes(data: bytes):
        sent_bytes.append(data)

    def on_broadcast(payload: dict):
        broadcast.append(payload)

    async def on_turn_complete(transcript_in: str, transcript_out: str):
        completed.append(("turn-1", transcript_in, transcript_out))
        return "turn-1"

    processor = DownstreamEventProcessor(
        send_text=send_text,
        send_bytes=send_bytes,
        broadcast=on_broadcast,
        on_turn_complete=on_turn_complete,
    )

    event = _make_event(
        parts=[_make_part(text="from-content"), _make_part(data=b"\x00\x01")],
        input_text="from-input",
        output_text="from-output",
        turn_complete=True,
    )
    await processor.process_event(event)

    assert sent_bytes == [b"\x00\x01"]
    assert completed == [("turn-1", "from-input", "from-content from-output")]
    assert sent_text[-1] == {"type": "turn_complete", "turn_id": "turn-1"}
    assert {"type": "transcript_in", "text": "from-input", "finished": False} in sent_text
    assert {"type": "transcript_out", "text": "from-content"} in sent_text
    assert {"type": "transcript_out", "text": "from-output"} in sent_text
    assert {
        "type": "turn_complete",
        "turn_id": "turn-1",
        "transcript_in": "from-input",
        "transcript_out": "from-content from-output",
    } in broadcast


@pytest.mark.asyncio
async def test_downstream_processor_interrupted_clears_partial_turn():
    sent_text: list[dict] = []
    broadcast: list[dict] = []
    completed: list[tuple[str, str, str]] = []

    async def send_text(raw: str):
        sent_text.append(json.loads(raw))

    async def send_bytes(_data: bytes):
        return None

    def on_broadcast(payload: dict):
        broadcast.append(payload)

    async def on_turn_complete(transcript_in: str, transcript_out: str):
        completed.append(("turn-2", transcript_in, transcript_out))
        return "turn-2"

    processor = DownstreamEventProcessor(
        send_text=send_text,
        send_bytes=send_bytes,
        broadcast=on_broadcast,
        on_turn_complete=on_turn_complete,
    )

    await processor.process_event(_make_event(input_text="partial", output_text="partial"))
    await processor.process_event(_make_event(interrupted=True))
    await processor.process_event(_make_event(turn_complete=True))

    assert {"type": "interrupted"} in sent_text
    assert {"type": "interrupted"} in broadcast
    assert completed[-1] == ("turn-2", "", "")


@pytest.mark.asyncio
async def test_downstream_processor_ignores_thought_tokens():
    sent_text: list[dict] = []
    completed: list[tuple[str, str, str]] = []
    thoughts: list[str] = []

    async def send_text(raw: str):
        sent_text.append(json.loads(raw))

    async def send_bytes(_data: bytes):
        return None

    def on_broadcast(_payload: dict):
        return None

    async def on_turn_complete(transcript_in: str, transcript_out: str):
        completed.append(("turn-3", transcript_in, transcript_out))
        return "turn-3"

    def on_thought(text: str):
        thoughts.append(text)

    processor = DownstreamEventProcessor(
        send_text=send_text,
        send_bytes=send_bytes,
        broadcast=on_broadcast,
        on_turn_complete=on_turn_complete,
        on_thought=on_thought,
    )

    await processor.process_event(_make_event(parts=[_make_part(text="hidden", thought=True)]))
    await processor.process_event(_make_event(turn_complete=True))

    assert completed[-1] == ("turn-3", "", "")
    assert thoughts == ["hidden"]
    assert sent_text[-1] == {"type": "turn_complete", "turn_id": "turn-3"}
