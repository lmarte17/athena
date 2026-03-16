"""LoopAgent wrapper for correction and continuation task planning."""

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

from app.orchestrator.contracts import FollowUpTaskPlan, TurnEnvelope

log = logging.getLogger("athena.orchestrator.correction_loop")

_APP_NAME = "athena_correction"
_USER_ID = "correction"
_MODEL = os.getenv("ATHENA_CORRECTION_MODEL", os.getenv("ATHENA_DIRECT_RESPONSE_MODEL", "gemini-2.5-flash"))

_INSTRUCTION = """\
You are Athena's follow-up planning agent.

You receive a fresh user turn plus the prior task context for an in-flight correction
or continuation. Your job is to turn that into a standalone follow-up task plan.

Rules:
- Preserve the user's new constraint or correction exactly when it is explicit.
- Reuse prior request details when the user refers to earlier work with words like "that" or "it".
- Keep `acknowledgment` short, natural, and voice-friendly.
- Keep `task_kind` to a short routing hint such as `gmail_search`, `calendar_write`, or `action`.
- Add `resource_hints` only for services that are clearly implicated by the request or prior context.
- Output only JSON matching the `FollowUpTaskPlan` schema.
"""


class _CorrectionLoopExit(BaseAgent):
    """Ends the current loop iteration after one structured plan is produced."""

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
        raise NotImplementedError("CorrectionLoop does not run in live mode.")
        if False:
            yield


@dataclass(slots=True)
class CorrectionLoop:
    """Phase-7 follow-up planning foundation built around ADK LoopAgent."""

    model: str = _MODEL
    name: str = "CorrectionLoop"
    _agent: LoopAgent = field(init=False, repr=False)
    _session_service: InMemorySessionService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        planner_agent = LlmAgent(
            name="CorrectionPlannerAgent",
            description="Produces a standalone follow-up task plan for correction or continuation turns.",
            model=self.model,
            instruction=_INSTRUCTION,
            tools=[],
        )
        self._agent = LoopAgent(
            name=self.name,
            sub_agents=[
                planner_agent,
                _CorrectionLoopExit(name="CorrectionLoopExit"),
            ],
            max_iterations=2,
        )
        self._session_service = InMemorySessionService()

    async def invoke(
        self,
        turn_envelope: TurnEnvelope,
        *,
        follow_up_kind: str,
        base_request: str,
        base_task_kind: str = "general",
        resource_hints: list[str] | None = None,
        workspace_context: str = "",
    ) -> FollowUpTaskPlan:
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
            follow_up_kind=follow_up_kind,
            base_request=base_request,
            base_task_kind=base_task_kind,
            resource_hints=resource_hints or [],
            workspace_context=workspace_context,
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
            return _fallback_plan(
                follow_up_kind=follow_up_kind,
                user_turn=turn_envelope.transcript,
                base_request=base_request,
                base_task_kind=base_task_kind,
                resource_hints=resource_hints or [],
            )

        try:
            plan = FollowUpTaskPlan.model_validate_json(raw_text)
        except Exception:
            log.warning("CorrectionLoop returned non-JSON output; using fallback plan")
            return _fallback_plan(
                follow_up_kind=follow_up_kind,
                user_turn=turn_envelope.transcript,
                base_request=base_request,
                base_task_kind=base_task_kind,
                resource_hints=resource_hints or [],
            )

        user_request = plan.user_request.strip()
        acknowledgment = plan.acknowledgment.strip()
        if not user_request or not acknowledgment:
            return _fallback_plan(
                follow_up_kind=follow_up_kind,
                user_turn=turn_envelope.transcript,
                base_request=base_request,
                base_task_kind=base_task_kind,
                resource_hints=resource_hints or [],
            )

        deduped_hints = list(dict.fromkeys(str(item).strip() for item in plan.resource_hints if str(item).strip()))
        return plan.model_copy(
            update={
                "user_request": user_request,
                "acknowledgment": acknowledgment,
                "task_kind": plan.task_kind.strip() or base_task_kind or "general",
                "resource_hints": deduped_hints,
            }
        )


def _build_prompt(
    *,
    turn_envelope: TurnEnvelope,
    follow_up_kind: str,
    base_request: str,
    base_task_kind: str,
    resource_hints: list[str],
    workspace_context: str,
) -> str:
    recent_turns = "\n".join(
        f"{turn.role.capitalize()}: {turn.content}" for turn in turn_envelope.recent_turns[-6:]
    )
    parts = [
        f"Follow-up kind: {follow_up_kind}",
        f"User turn: {turn_envelope.transcript}",
        f"Prior request: {base_request}",
        f"Prior task kind: {base_task_kind}",
    ]
    if resource_hints:
        parts.append(f"Current resource hints: {', '.join(resource_hints)}")
    if recent_turns:
        parts.append(f"Recent conversation:\n{recent_turns}")
    if workspace_context:
        parts.append(workspace_context)
    parts.append("Return a standalone FollowUpTaskPlan.")
    return "\n\n".join(parts)


def _fallback_plan(
    *,
    follow_up_kind: str,
    user_turn: str,
    base_request: str,
    base_task_kind: str,
    resource_hints: list[str],
) -> FollowUpTaskPlan:
    prefix = "Correction" if follow_up_kind == "correction" else "Follow-up"
    acknowledgment = (
        "Okay, I'll use that correction."
        if follow_up_kind == "correction"
        else "Okay, I'll keep going with that."
    )
    prior = base_request.strip()
    if prior:
        user_request = f"{prior}\n\n{prefix}: {user_turn.strip()}"
    else:
        user_request = user_turn.strip()
    return FollowUpTaskPlan(
        user_request=user_request,
        acknowledgment=acknowledgment,
        task_kind=base_task_kind or "general",
        resource_hints=list(dict.fromkeys(resource_hints)),
    )
