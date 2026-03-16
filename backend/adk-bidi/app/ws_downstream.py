"""Helpers for forwarding Gemini Live events to the tray WS client."""

import json
from collections.abc import Awaitable, Callable

from app.ws_logic import TurnAccumulator


class DownstreamEventProcessor:
    """Processes one live event at a time and emits tray/broadcast payloads."""

    def __init__(
        self,
        send_text: Callable[[str], Awaitable[None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
        broadcast: Callable[[dict], None],
        on_turn_complete: Callable[[str, str], Awaitable[str]],
        on_input_transcription: Callable[[str], None] | None = None,
        on_thought: Callable[[str], None] | None = None,
    ) -> None:
        self._send_text = send_text
        self._send_bytes = send_bytes
        self._broadcast = broadcast
        self._on_turn_complete = on_turn_complete
        self._on_input_transcription = on_input_transcription
        self._on_thought = on_thought
        self._turn_acc = TurnAccumulator()

    async def process_event(self, event) -> None:
        await self._forward_content_parts(event)
        await self._forward_input_transcription(event)
        await self._forward_output_transcription(event)
        await self._forward_interrupted(event)
        await self._forward_turn_complete(event)

    async def _forward_content_parts(self, event) -> None:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None)
        if not parts:
            return

        for part in parts:
            inline_data = getattr(part, "inline_data", None)
            data = getattr(inline_data, "data", None) if inline_data else None
            text = getattr(part, "text", None)
            thought = bool(getattr(part, "thought", False))

            if data:
                await self._send_bytes(data)
            elif text:
                if thought:
                    if self._on_thought is not None:
                        self._on_thought(text)
                    continue
                payload = {"type": "transcript_out", "text": text}
                await self._send_text(json.dumps(payload))
                self._turn_acc.add_output(text)

    async def _forward_input_transcription(self, event) -> None:
        input_tx = getattr(event, "input_transcription", None)
        text = getattr(input_tx, "text", None) if input_tx else None
        if not text:
            return
        finished = bool(getattr(input_tx, "finished", False))
        # Forward all partial updates to the tray so it can show live streaming
        # text while the user speaks.  Only the final text (finished=True) gets
        # committed to the turn accumulator's completed-segments list; partials
        # update the in-progress slot and are discarded if a newer partial arrives.
        payload = {"type": "transcript_in", "text": text, "finished": finished}
        await self._send_text(json.dumps(payload))
        self._broadcast(payload)
        if self._on_input_transcription is not None:
            self._on_input_transcription(text)
        self._turn_acc.add_input(text, finished=finished)

    async def _forward_output_transcription(self, event) -> None:
        output_tx = getattr(event, "output_transcription", None)
        text = getattr(output_tx, "text", None) if output_tx else None
        if not text:
            return
        payload = {"type": "transcript_out", "text": text}
        await self._send_text(json.dumps(payload))
        self._broadcast(payload)
        self._turn_acc.add_output(text)

    async def _forward_interrupted(self, event) -> None:
        if not getattr(event, "interrupted", False):
            return
        self._turn_acc.interrupt()
        payload = {"type": "interrupted"}
        await self._send_text(json.dumps(payload))
        self._broadcast(payload)

    async def _forward_turn_complete(self, event) -> None:
        if not getattr(event, "turn_complete", False):
            return

        transcript_in, transcript_out = self._turn_acc.finalize()
        turn_id = await self._on_turn_complete(transcript_in, transcript_out)

        payload = {
            "type": "turn_complete",
            "turn_id": turn_id,
            "transcript_in": transcript_in,
            "transcript_out": transcript_out,
        }
        self._broadcast(payload)
        await self._send_text(json.dumps({"type": "turn_complete", "turn_id": turn_id}))
