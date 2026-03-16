"""Helpers for forwarding client WS frames into Gemini Live request queue."""

import base64

from google.adk.agents import LiveRequestQueue
from google.genai import types

from app.ws_logic import parse_client_text_message


def dispatch_upstream_frame(raw: dict, queue: LiveRequestQueue) -> None:
    """Forward one WS frame (`ws.receive()` payload) to the live request queue."""
    if "bytes" in raw and raw["bytes"]:
        queue.send_realtime(
            types.Blob(
                data=raw["bytes"],
                mime_type="audio/pcm;rate=16000",
            )
        )
        return

    if "text" in raw and raw["text"]:
        kind, msg = parse_client_text_message(raw["text"])
        dispatch_client_message(kind, msg, queue)


def dispatch_client_message(kind: str | None, msg: dict, queue: LiveRequestQueue) -> None:
    """Handle parsed client JSON message by `type`."""
    if kind == "image":
        image_bytes = base64.b64decode(msg["data"])
        mime_type = msg.get("mime_type", "image/jpeg")
        queue.send_realtime(types.Blob(data=image_bytes, mime_type=mime_type))
    elif kind == "text":
        queue.send_content(
            types.Content(
                role="user",
                parts=[types.Part(text=msg["text"])],
            )
        )
    # activity_start / activity_end are NOT forwarded to the queue.
    # Server-side VAD is active; sending explicit activity control signals
    # causes a 1007 "Explicit activity control is not supported when automatic
    # activity detection is enabled" error.  The ws.py upstream handler still
    # reads activity_start to stamp last_user_audio_at for the silence monitor.
