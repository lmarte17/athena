"""LoopAgent wrapper for clarification-question generation."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.orchestrator.contracts import ResponsePlan, Tone, TurnEnvelope

log = logging.getLogger("athena.orchestrator.clarification_loop")

_APP_NAME = "athena_clarification"
_USER_ID = "clarification"
_MODEL = os.getenv("ATHENA_DIRECT_RESPONSE_MODEL", "gemini-2.5-flash")

_INSTRUCTION = """\
You are Athena's clarification agent.

You receive a user turn that is too ambiguous to answer safely.
Return a single short clarification question as a `ResponsePlan`.

Rules:
- Ask exactly one question.
- Do not answer the request yet.
- Do not mention internal tools, orchestration, or technical details.
- Keep the text short and natural for voice.
- Always use `tone=\"clarification\"` and `channel=\"voice\"`.
- Output only JSON matching the `ResponsePlan` schema.
"""


class _ClarificationLoopExit(BaseAgent):
    """Ends the current loop iteration after the question is generated."""

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            actions=EventActions(escalate=True),
        )

    async def _run_live_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        raise NotImplementedError("ClarificationLoop does not run in live mode.")
        if False:
            yield


@dataclass(slots=True)
class ClarificationLoop:
    """Phase-5 clarification foundation built around ADK LoopAgent."""

    model: str = _MODEL
    name: str = "ClarificationLoop"
    _agent: LoopAgent = field(init=False, repr=False)
    _session_service: InMemorySessionService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        clarification_agent = LlmAgent(
            name="ClarificationQuestionAgent",
            description="Produces one concise clarification question as a response plan.",
            model=self.model,
            instruction=_INSTRUCTION,
            tools=[],
        )
        self._agent = LoopAgent(
            name=self.name,
            sub_agents=[
                clarification_agent,
                _ClarificationLoopExit(name="ClarificationLoopExit"),
            ],
            max_iterations=3,
        )
        self._session_service = InMemorySessionService()

    async def invoke(
        self,
        turn_envelope: TurnEnvelope,
        *,
        clarification_request: str | None = None,
    ) -> ResponsePlan:
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
                        raw_text = part.text

        raw_text = raw_text.strip()
        if not raw_text:
            return _fallback_response()

        try:
            plan = ResponsePlan.model_validate_json(raw_text)
        except Exception:
            log.warning("ClarificationLoop returned non-JSON output; using text fallback")
            plan = ResponsePlan(text=raw_text, tone=Tone.clarification, channel="voice")

        text = plan.text.strip()
        if not text:
            return _fallback_response()
        return plan.model_copy(
            update={
                "text": text,
                "tone": Tone.clarification,
                "channel": "voice",
                "priority": plan.priority if plan.priority > 0 else 6,
            }
        )


def _build_prompt(
    *,
    turn_envelope: TurnEnvelope,
    clarification_request: str | None,
) -> str:
    recent_turns = "\n".join(
        f"{turn.role.capitalize()}: {turn.content}" for turn in turn_envelope.recent_turns[-6:]
    )
    parts = [
        f"User turn: {turn_envelope.transcript}",
    ]
    if recent_turns:
        parts.append(f"Recent conversation:\n{recent_turns}")
    if clarification_request:
        parts.append(f"Why this needs clarification: {clarification_request}")
    parts.append("Return one short clarification question as a ResponsePlan.")
    return "\n\n".join(parts)


def _fallback_response() -> ResponsePlan:
    return ResponsePlan(
        text="What exactly would you like me to help with?",
        tone=Tone.clarification,
        channel="voice",
        priority=6,
    )
