"""Direct-response spoke for turn-complete conversational answers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.orchestrator.contracts import Mode, ResponsePlan, Tone, TurnEnvelope

log = logging.getLogger("athena.orchestrator.direct_response_spoke")

_APP_NAME = "athena_direct_response"
_USER_ID = "direct_response"
_MODEL = os.getenv("ATHENA_DIRECT_RESPONSE_MODEL", "gemini-2.5-flash")

_INSTRUCTION = """\
You are Athena's direct-response spoke.

You receive completed user turns after live voice capture is finished.
Your job is to return a structured `ResponsePlan` for voice rendering.

Rules:
- Answer only from the user turn and recent conversation supplied in the prompt.
- Never claim to have checked Gmail, Drive, Docs, Calendar, or any other live source.
- Keep answers concise, natural, and spoken-language friendly.
- Always respond in English.
- If the mode is `respond_now`, answer directly.
- If the mode is `respond_and_start_tasks`, give exactly one short bridge acknowledgment that work
  is starting. Do not answer the task itself, do not claim completion, and do not claim lack of access.
- If the mode is `ask_clarify`, ask exactly one short clarifying question.
- Output only JSON that matches the `ResponsePlan` schema.
- Use `channel=\"voice\"`.
"""


@dataclass(slots=True)
class DirectResponseSpoke:
    """Runs a structured-response LLM turn for direct conversational replies."""

    model: str = _MODEL
    name: str = "DirectResponseSpoke"
    _agent: LlmAgent = field(init=False, repr=False)
    _session_service: InMemorySessionService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = LlmAgent(
            name=self.name,
            description="Produces a structured voice response plan for direct questions.",
            model=self.model,
            instruction=_INSTRUCTION,
            tools=[],
        )
        self._session_service = InMemorySessionService()

    async def invoke(
        self,
        turn_envelope: TurnEnvelope,
        *,
        mode: Mode = Mode.respond_now,
        clarification_request: str | None = None,
    ) -> ResponsePlan:
        if mode not in {Mode.respond_now, Mode.respond_and_start_tasks, Mode.ask_clarify}:
            raise ValueError(f"Unsupported direct-response mode: {mode.value}")

        runner = Runner(
            agent=self._agent,
            app_name=_APP_NAME,
            session_service=self._session_service,
        )
        session = await self._session_service.create_session(
            app_name=_APP_NAME,
            user_id=_USER_ID,
        )
        prompt = _build_prompt(
            turn_envelope=turn_envelope,
            mode=mode,
            clarification_request=clarification_request,
        )

        raw_text = ""
        async for event in runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts or []:
                    if part.text:
                        raw_text += part.text

        raw_text = raw_text.strip()
        if not raw_text:
            return _fallback_response(mode)

        try:
            plan = ResponsePlan.model_validate_json(raw_text)
        except Exception:
            log.warning("DirectResponseSpoke returned non-JSON output; using text fallback")
            plan = ResponsePlan(
                text=raw_text,
                tone=_tone_for_mode(mode),
                channel="voice",
            )
        return _normalize_response_plan(plan, mode=mode)


def _build_prompt(
    *,
    turn_envelope: TurnEnvelope,
    mode: Mode,
    clarification_request: str | None,
) -> str:
    recent_turns = "\n".join(
        f"{turn.role.capitalize()}: {turn.content}" for turn in turn_envelope.recent_turns[-6:]
    )
    parts = [
        f"Mode: {mode.value}",
        f"User turn: {turn_envelope.transcript}",
    ]
    if recent_turns:
        parts.append(f"Recent conversation:\n{recent_turns}")
    if turn_envelope.active_task_ids:
        parts.append(f"Active task ids: {', '.join(turn_envelope.active_task_ids)}")
    if clarification_request:
        parts.append(f"Clarification guidance: {clarification_request}")
    if mode == Mode.respond_and_start_tasks:
        parts.append(
            "The user's request is being routed to backend work. Return one short bridge"
            " acknowledgment only, such as 'Okay, I'll handle that.' Do not mention"
            " limitations, tools, or lack of access."
        )
    parts.append(
        "Return a ResponsePlan for voice rendering. Keep it brief and natural."
    )
    return "\n\n".join(parts)


def _normalize_response_plan(plan: ResponsePlan, *, mode: Mode) -> ResponsePlan:
    tone = _tone_for_mode(mode)
    text = plan.text.strip()
    if not text:
        return _fallback_response(mode)

    return plan.model_copy(
        update={
            "text": text,
            "tone": tone,
            "channel": "voice",
            "interruptible": plan.interruptible,
            "priority": plan.priority if plan.priority > 0 else 6,
        }
    )


def _tone_for_mode(mode: Mode) -> Tone:
    if mode == Mode.ask_clarify:
        return Tone.clarification
    if mode == Mode.respond_and_start_tasks:
        return Tone.bridge
    return Tone.direct_answer


def _fallback_response(mode: Mode) -> ResponsePlan:
    if mode == Mode.ask_clarify:
        return ResponsePlan(
            text="What would you like me to focus on?",
            tone=Tone.clarification,
            channel="voice",
            priority=6,
        )
    if mode == Mode.respond_and_start_tasks:
        return ResponsePlan(
            text="Okay, I'll take care of that.",
            tone=Tone.bridge,
            channel="voice",
            priority=6,
        )
    return ResponsePlan(
        text="I'm not fully sure yet, so could you say that one more way?",
        tone=Tone.clarification,
        channel="voice",
        priority=6,
    )
