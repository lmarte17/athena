"""Voice egress adapter for orchestrator-owned response plans."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.genai import types

from app.orchestrator.contracts import ResponsePlan

log = logging.getLogger("athena.orchestrator.voice_egress_adapter")


@dataclass(slots=True)
class VoiceEgressAdapter:
    """Renders orchestrator response plans back through the live voice session."""

    runtime_lookup: Callable[[str], Any | None]
    on_rendered: Callable[[str], None] | None = None

    async def render(self, session_id: str, response_plan: ResponsePlan) -> bool:
        if response_plan.channel == "text" or not response_plan.text.strip():
            return False

        runtime = self.runtime_lookup(session_id)
        if runtime is None:
            log.info("Session %s no longer active; skip direct-response egress", session_id)
            return False

        payload = build_voice_egress_prompt(response_plan)
        try:
            runtime.queue.send_content(
                types.Content(role="user", parts=[types.Part(text=payload)])
            )
        except Exception:
            log.exception("Failed to render direct response for session %s", session_id)
            return False

        if self.on_rendered is not None:
            self.on_rendered(session_id)
        return True


def build_voice_egress_prompt(response_plan: ResponsePlan) -> str:
    return (
        "[Direct response plan]\n"
        f"Tone: {response_plan.tone.value}\n"
        f"Interruptible: {'yes' if response_plan.interruptible else 'no'}\n\n"
        "Speak the following text to the user faithfully. "
        "Do not add facts, extra framing, or tool calls.\n\n"
        f"Text:\n{response_plan.text.strip()}"
    )
