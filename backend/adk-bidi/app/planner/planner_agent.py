"""PlannerAgent — decomposes a WorkspaceJobRequest into an ExecutionPlan.

Uses gemini-3.1-pro-preview with JSON structured output to produce a DAG of
specialist steps. Falls back to a trivial single-step plan on any failure.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from google.genai import types

from app.jobs.models import JobTypeHint, WorkspaceJobRequest
from app.planner.models import ExecutionPlan, PlanStep
from app.tracing import atrace_span, base_metadata, create_gemini_client, finish_span

if TYPE_CHECKING:
    from app.job_workspace import JobWorkspaceStore
    from app.planner.skill_library import SkillLibrary

log = logging.getLogger("athena.planner.planner_agent")

_PLANNER_MODEL = os.getenv("ATHENA_PLANNER_MODEL", "gemini-3.1-pro-preview")

_AVAILABLE_SPECIALISTS = [
    "gmail",
    "drive",
    "docs",
    "calendar",
    "sheets",
    "slides",
    "retrieval",
    "action",
]

_SYSTEM_INSTRUCTION = """\
You are Athena's task planning agent. Your job is to decompose Google Workspace requests
into an optimised parallel/sequential execution plan.

## Available specialists

- gmail     — search, read, send, draft, archive email
- drive     — search, copy, move, rename, share, delete Drive files
- docs      — read, create, write, edit Google Docs
- calendar  — list, search, create, update, delete Calendar events
- sheets    — read, create, write Google Sheets
- slides    — read, create, inspect, and edit Google Slides presentations
- retrieval — look up resources already seen in the current session
- action    — execute confirmed write operations across any service

## Output format (strict JSON)

{
  "steps": [
    {
      "step_id": "step_1",
      "specialist": "<one of the specialists above>",
      "instruction": "<complete, self-contained instruction for this specialist>",
      "depends_on": [],
      "output_key": "step_1"
    }
  ],
  "is_trivial": true
}

## Planning rules

1. **is_trivial = true** when the entire request can be satisfied by a single specialist
   in one call. Provide exactly one step and set is_trivial=true.

2. **Multi-step jobs**: set is_trivial=false. Each step must:
   - Be assigned to exactly one specialist.
   - Have a unique step_id (step_1, step_2, …).
   - List depends_on = [] if it has no prerequisites, or list the step_ids it must wait for.
   - output_key = its step_id (used to reference its result in later step instructions).

3. **Parallel execution**: steps without dependencies on each other can share the same
   wave — list them with empty depends_on and they will run concurrently.

4. **Context passing**: if a later step needs results from an earlier one, reference the
   earlier step by output_key in the instruction, e.g.:
   "Using the email thread IDs from step_1, create a Google Doc summarising the findings."

5. Instructions must be specific. Include search terms, names, dates, or IDs extracted
   from the user's request. Do NOT leave instructions vague.

6. If recent job workspace context is provided and the request is a correction or continuation,
   prefer plans that reuse the referenced spreadsheet, document, or extracted state instead
   of recreating everything from scratch.

7. Use `slides` for creating presentations from source material like docs, notes, or prior
   step outputs. Reserve `action` for mechanical write execution only when the content is
   already fully prepared.

8. Output ONLY the JSON object. No markdown fences, no extra text.
"""


# Map job_type_hint → default specialist for trivial fallback plans
_HINT_TO_SPECIALIST: dict[str, str] = {
    "gmail_search": "gmail",
    "gmail_read": "gmail",
    "gmail_send": "action",
    "gmail_draft": "gmail",
    "drive_search": "drive",
    "drive_read": "drive",
    "drive_write": "action",
    "doc_read": "docs",
    "doc_create": "action",
    "doc_write": "action",
    "calendar_read": "calendar",
    "calendar_write": "action",
    "sheets_read": "sheets",
    "sheets_write": "action",
    "slides_read": "slides",
    "slides_create": "slides",
    "retrieval": "retrieval",
    "action": "action",
    "general": "retrieval",
}


def _trivial_plan(request: WorkspaceJobRequest) -> ExecutionPlan:
    """Build a single-step fallback plan."""
    specialist = _HINT_TO_SPECIALIST.get(str(request.job_type_hint), "retrieval")
    return ExecutionPlan(
        job_id=request.job_id,
        is_trivial=True,
        steps=[
            PlanStep(
                step_id="step_1",
                specialist=specialist,
                instruction=request.user_request,
                depends_on=[],
                output_key="step_1",
            )
        ],
    )


class PlannerAgent:
    """Produces an ExecutionPlan for a WorkspaceJobRequest.

    First checks the SkillLibrary for a matching template; if found, instantiates
    the plan from the skill. Otherwise calls the Gemini API for fresh decomposition.
    """

    def __init__(
        self,
        skill_library: SkillLibrary | None = None,
        job_workspace: "JobWorkspaceStore | None" = None,
    ) -> None:
        self._skill_library = skill_library
        self._job_workspace = job_workspace
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = create_gemini_client(
                "athena.planner",
                model=_PLANNER_MODEL,
                tags=["planner"],
            )
        return self._client

    async def plan(self, request: WorkspaceJobRequest) -> ExecutionPlan:
        """Return an ExecutionPlan for this job request."""
        async with atrace_span(
            "athena.planner.plan",
            inputs={"request": request.to_dict()},
            metadata=base_metadata(
                component="planner.plan",
                athena_session_id=request.session_id,
                job_id=request.job_id,
                model=_PLANNER_MODEL,
            ),
            tags=["planner"],
        ) as run:
            if self._skill_library:
                try:
                    skill = await self._skill_library.find_match(request.user_request)
                    if skill:
                        plan = _instantiate_from_skill(skill, request)
                        log.info(
                            "[planner] matched skill '%s' for job %s",
                            skill.name,
                            request.job_id[:8],
                        )
                        finish_span(
                            run,
                            outputs={
                                "source": "skill_library",
                                "skill_id": skill.skill_id,
                                "plan": plan.to_dict(),
                            },
                        )
                        return plan
                except Exception:
                    log.debug("[planner] skill lookup failed", exc_info=True)

            try:
                plan = await self._call_llm(request)
                log.info(
                    "[planner] job %s → %d step(s) is_trivial=%s",
                    request.job_id[:8],
                    len(plan.steps),
                    plan.is_trivial,
                )
                finish_span(
                    run,
                    outputs={
                        "source": "llm",
                        "plan": plan.to_dict(),
                    },
                )
                return plan
            except Exception as exc:
                log.exception("[planner] LLM planning failed — using trivial fallback")
                plan = _trivial_plan(request)
                finish_span(
                    run,
                    outputs={
                        "source": "fallback",
                        "error": str(exc),
                        "plan": plan.to_dict(),
                    },
                )
                return plan

    async def _call_llm(self, request: WorkspaceJobRequest) -> ExecutionPlan:
        prompt_parts = [
            f"Job type hint: {request.job_type_hint}",
            f"User request: {request.user_request}",
        ]

        if request.resource_hints:
            prompt_parts.append(f"Resource hints: {', '.join(request.resource_hints)}")

        if request.conversation_window:
            recent = request.conversation_window[-3:]
            window = "\n".join(
                f"{t.get('role', 'user').capitalize()}: {t.get('content', '')}"
                for t in recent
            )
            prompt_parts.append(f"Recent conversation:\n{window}")

        if self._job_workspace is not None:
            workspace_context = self._job_workspace.render_context(
                request.session_id,
                query=request.user_request,
                limit=2,
                max_chars=1200,
            )
            if workspace_context:
                prompt_parts.append(workspace_context)

        prompt_parts.append(
            "\nDecompose this request into an execution plan."
            " Output only JSON, no extra text."
        )

        prompt = "\n\n".join(prompt_parts)

        async with atrace_span(
            "athena.planner.call_llm",
            inputs={"prompt": prompt, "system_instruction": _SYSTEM_INSTRUCTION},
            metadata=base_metadata(
                component="planner.call_llm",
                athena_session_id=request.session_id,
                job_id=request.job_id,
                model=_PLANNER_MODEL,
            ),
            tags=["planner"],
        ) as run:
            response = await self._get_client().aio.models.generate_content(
                model=_PLANNER_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )

            raw = (response.text or "").strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                finish_span(run, error=f"Planner returned invalid JSON: {exc}")
                raise ValueError(f"Planner returned invalid JSON: {exc}") from exc

            steps_raw = data.get("steps") or []
            if not steps_raw:
                finish_span(run, error="Planner returned no steps")
                raise ValueError("Planner returned no steps")

            steps = []
            for s in steps_raw:
                specialist = str(s.get("specialist") or "retrieval")
                if specialist not in _AVAILABLE_SPECIALISTS:
                    specialist = "retrieval"
                steps.append(
                    PlanStep(
                        step_id=str(s.get("step_id") or f"step_{len(steps) + 1}"),
                        specialist=specialist,
                        instruction=str(s.get("instruction") or request.user_request),
                        depends_on=list(s.get("depends_on") or []),
                        output_key=str(s.get("output_key") or s.get("step_id") or "step_1"),
                    )
                )

            plan = ExecutionPlan(
                job_id=request.job_id,
                steps=steps,
                is_trivial=bool(data.get("is_trivial", len(steps) == 1)),
            )
            finish_span(run, outputs={"raw": raw, "plan": plan.to_dict()})
            return plan


def _instantiate_from_skill(skill, request: WorkspaceJobRequest) -> ExecutionPlan:
    """Inflate a skill template into a concrete ExecutionPlan for this request."""
    template = skill.plan_template or {}
    raw_steps = template.get("steps") or []

    steps = []
    for s in raw_steps:
        # Append the user's actual request as context to each step instruction
        base_instruction = str(s.get("instruction") or "")
        enriched = (
            f"{base_instruction}\n\nUser request: {request.user_request}"
            if base_instruction
            else request.user_request
        )
        steps.append(
            PlanStep(
                step_id=str(s.get("step_id") or f"step_{len(steps) + 1}"),
                specialist=str(s.get("specialist") or "retrieval"),
                instruction=enriched,
                depends_on=list(s.get("depends_on") or []),
                output_key=str(s.get("output_key") or s.get("step_id") or "step_1"),
            )
        )

    return ExecutionPlan(
        job_id=request.job_id,
        steps=steps,
        is_trivial=bool(template.get("is_trivial", len(steps) == 1)),
        skill_id=skill.skill_id,
    )
