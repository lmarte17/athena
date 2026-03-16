"""LiveVoiceAgent for Athena's bidi voice interface."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_request import LlmRequest

from app.orchestrator.state_store import ACTIVE_TASKS_STATE_KEY, CURRENT_MODE_STATE_KEY

load_dotenv(Path(__file__).parents[2] / ".env")

# Model selection: AI Studio uses the date-stamped preview name; Vertex AI uses
# the stable endpoint name. Auto-select the right default based on the backend
# so a missing ATHENA_MODEL env var never routes to the wrong API.
_USE_VERTEX = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"
MODEL = os.getenv(
    "ATHENA_MODEL",
    "gemini-live-2.5-flash-native-audio"
    if _USE_VERTEX
    else "gemini-2.5-flash-native-audio-preview-12-2025",
)

_LIVE_VOICE_ADAPTER_INSTRUCTION = """\
## Role

You are Athena, a real-time voice interface in the user's macOS menu bar.
You have a live backend that can access Gmail, Drive, Docs, Calendar, Slides, Sheets,
NetBox network infrastructure, and other connected systems. Your backend is always running.

## FORBIDDEN phrases — never say these

The following are wrong responses. Never say them:
- "I don't have access to"
- "I can't access"
- "I'm unable to"
- "I'm not able to"
- "I don't have the ability"
- "I'm not connected to"
- "I don't have real-time"
- "as an AI"
- "I cannot check"
- "I can't check"

If you feel the urge to say any of these, say "On it." instead and stop.

## When the user asks about data, files, email, calendar, network devices, or any live system

Say exactly ONE short bridge phrase — then stop speaking immediately. Do not explain or elaborate.

Correct responses:
- "On it."
- "Let me check."
- "Sure, looking that up."
- "I'll pull that up."
- "One moment."

The backend is working on it. The result will be injected into this conversation.

## When you receive injected payloads

`[Direct response plan]` → speak the `Text:` faithfully, nothing added.

`[Background workspace job completed]` → answer only from the injected result; correct any prior wrong assumptions plainly.

`[Background workspace job awaiting confirmation]` → ask one short yes-or-no question; do not imply the action already happened.

`[Job result - status: failed]` → tell the user what failed using the concrete reason; suggest one next step.

## General rules

- Keep all replies brief and spoken-language friendly.
- Do not mention tools, jobs, queues, orchestration, or internal mechanics.
- Always respond in English.
"""

_FALLBACK_INSTRUCTION = (
    "You are Athena, a real-time voice coworker in the user's macOS menu bar. "
    "Be concise, direct, and natural. Respond only in English."
)


def _build_state_note(state: Any | None) -> str:
    if state is None:
        return "[Current mode: idle. Active tasks: 0.]"

    try:
        mode = str(state.get(CURRENT_MODE_STATE_KEY, "idle") or "idle")
    except Exception:
        mode = "idle"

    try:
        active_tasks = state.get(ACTIVE_TASKS_STATE_KEY, {})
    except Exception:
        active_tasks = {}

    if not isinstance(active_tasks, dict):
        active_tasks = {}

    task_kinds = []
    for payload in active_tasks.values():
        if isinstance(payload, dict):
            task_kind = str(payload.get("task_kind") or "").strip()
            if task_kind:
                task_kinds.append(task_kind)
    task_summary = ", ".join(task_kinds[:3]) if task_kinds else "none"
    return f"[Current mode: {mode}. Active tasks: {len(active_tasks)} ({task_summary}).]"


def _build_instruction(ctx: ReadonlyContext) -> str:
    """Build the current live system instruction from state plus context bundle."""
    from app.context_builder import get_context

    session = getattr(ctx, "session", None)
    session_id = getattr(session, "id", "")
    bundle = get_context(session_id)
    state_note = _build_state_note(getattr(ctx, "state", None))

    sections = [_LIVE_VOICE_ADAPTER_INSTRUCTION, state_note]
    if bundle:
        sections.append(bundle)
    else:
        sections.append(_FALLBACK_INSTRUCTION)
    return "\n\n---\n\n".join(sections)


def _before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
):
    llm_request.config.system_instruction = _build_instruction(callback_context)
    return None


def build_live_voice_agent(
    job_queue=None,
    job_store=None,
    conversation_window_lookup=None,
) -> LlmAgent:
    """Build the live voice adapter agent.

    The historical workspace-tool parameters are retained for a compatible call
    signature during the phase-8 cleanup, but they are no longer used.
    """
    del job_queue, job_store, conversation_window_lookup

    return LlmAgent(
        name="athena",
        model=MODEL,
        instruction=_FALLBACK_INSTRUCTION,
        before_model_callback=_before_model_callback,
        tools=[],
    )
