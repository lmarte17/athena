"""ExecutionEngine — runs an ExecutionPlan by dispatching specialist agents
in dependency order, parallelising independent steps with asyncio.gather.

For trivial (single-step) plans the engine delegates straight to the fallback
coordinator so existing routing logic is reused unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.job_workspace import JobWorkspaceStore
from app.adk_agents.specialists.action import build_action_agent
from app.adk_agents.specialists.calendar import build_calendar_agent
from app.adk_agents.specialists.docs import build_docs_agent
from app.adk_agents.specialists.drive import build_drive_agent
from app.adk_agents.specialists.gmail import build_gmail_agent
from app.adk_agents.specialists.retrieval import build_retrieval_agent
from app.adk_agents.specialists.sheets import build_sheets_agent
from app.adk_agents.specialists.slides import build_slides_agent
from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.planner.models import ExecutionPlan, PlanStep
from app.resource_store import SessionResourceStore
from app.tracing import atrace_span, base_metadata, finish_span

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.retrieval import SemanticRetrieval

log = logging.getLogger("athena.planner.execution_engine")

_APP_NAME = "athena_engine"
_USER_ID = "engine"

# Callable[[], LlmAgent] — built fresh per step to avoid shared state.
# Retrieval is excluded here because it requires runtime args (resource_store,
# session_id) that are injected by ExecutionEngine._run_step directly.
_SPECIALIST_BUILDERS: dict[str, Any] = {
    "gmail": build_gmail_agent,
    "drive": build_drive_agent,
    "docs": build_docs_agent,
    "calendar": build_calendar_agent,
    "sheets": build_sheets_agent,
    "slides": build_slides_agent,
    "action": build_action_agent,
}


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

def _topological_sort(steps: list[PlanStep]) -> list[list[PlanStep]]:
    """Return steps grouped into sequential execution waves.

    Steps within the same wave have no interdependencies and can run in
    parallel via asyncio.gather.
    """
    step_map = {s.step_id: s for s in steps}
    # Map step_id → set of direct dependency step_ids
    deps: dict[str, set[str]] = {s.step_id: set(s.depends_on) for s in steps}

    waves: list[list[PlanStep]] = []
    completed: set[str] = set()
    remaining: set[str] = set(step_map.keys())

    while remaining:
        # Steps whose every dependency is already completed
        ready = {sid for sid in remaining if deps[sid].issubset(completed)}
        if not ready:
            # Circular dependency or malformed plan — schedule everything remaining
            log.warning(
                "[engine] cycle detected in plan — scheduling %d remaining step(s) together",
                len(remaining),
            )
            ready = remaining.copy()

        wave = [step_map[sid] for sid in sorted(ready)]
        waves.append(wave)
        completed.update(ready)
        remaining -= ready

    return waves


# ---------------------------------------------------------------------------
# Result parsing helpers
# ---------------------------------------------------------------------------

def _parse_specialist_result(raw_text: str) -> dict[str, Any]:
    """Extract structured fields from a specialist agent's text output."""
    parsed: dict[str, Any] = {}
    match = re.search(r"\{[\s\S]*\}", raw_text)
    if match:
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            pass
    if not parsed:
        parsed = {"summary": (raw_text[:500] if raw_text else "Done.")}

    return {
        "summary": str(parsed.get("summary") or ""),
        "artifacts": list(parsed.get("artifacts") or []),
        "resource_handles": list(parsed.get("resource_handles") or []),
        "follow_up_questions": list(parsed.get("follow_up_questions") or []),
        "error": parsed.get("error"),
    }


def _enrich_instruction(step: PlanStep, prior: dict[str, dict[str, Any]]) -> str:
    """Prepend context from completed dependency steps to the step instruction."""
    if not step.depends_on or not prior:
        return step.instruction

    context_lines: list[str] = []
    has_source_content = False
    for dep_key in step.depends_on:
        dep = prior.get(dep_key)
        if not dep:
            continue
        summary = dep.get("summary", "")
        if summary:
            context_lines.append(f"Result from {dep_key}: {summary}")
        for artifact in dep.get("artifacts") or []:
            title = artifact.get("title", "")
            res_id = artifact.get("id", "")
            artifact_type = artifact.get("type", "artifact")
            if res_id:
                context_lines.append(f"  - {title or artifact_type or 'resource'} [id={res_id}]")
            content = str(artifact.get("content") or "").strip()
            if content:
                has_source_content = True
                excerpt = _truncate_context_block(content, max_chars=2400)
                label = title or artifact_type or dep_key
                context_lines.append(f"Source excerpt from {dep_key} / {label}:")
                context_lines.append(excerpt)

        for handle in dep.get("resource_handles") or []:
            source = str(handle.get("source") or "").strip()
            kind = str(handle.get("kind") or "").strip()
            ident = str(handle.get("id") or "").strip()
            title = str(handle.get("title") or "").strip()
            url = str(handle.get("url") or "").strip()
            if ident:
                line = f"Resource handle from {dep_key}: {source}/{kind} {title} [id={ident}]"
                if url:
                    line += f" {url}"
                context_lines.append(line)

    if not context_lines:
        return step.instruction

    header = []
    if has_source_content:
        header.append(
            "Use the dependency excerpts below as source material for this step."
        )

    enriched = "\n".join([*header, *context_lines]).strip()
    return enriched + "\n\nTask: " + step.instruction


def _truncate_context_block(text: str, *, max_chars: int) -> str:
    compact = text.strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


# ---------------------------------------------------------------------------
# Low-level ADK runner helper
# ---------------------------------------------------------------------------

async def _run_agent_to_text(runner: Runner, session_id: str, prompt: str) -> str:
    """Run a headless agent turn and collect the final text response."""
    message = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""
    async for event in runner.run_async(
        user_id=_USER_ID,
        session_id=session_id,
        new_message=message,
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts or []:
                if hasattr(part, "text") and part.text:
                    final_text += part.text
    return final_text.strip()


# ---------------------------------------------------------------------------
# ExecutionEngine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """Executes an ExecutionPlan by running specialist agents in DAG order.

    Args:
        fallback_coordinator: Called for trivial plans to avoid duplicating
            the coordinator's routing logic.
        resource_store: Session resource store — required to build the retrieval
            specialist directly rather than routing it through the coordinator.
        semantic: Semantic retrieval instance passed to the retrieval specialist.
    """

    def __init__(
        self,
        fallback_coordinator: Callable[
            [WorkspaceJobRequest], Coroutine[Any, Any, WorkspaceJobResult]
        ],
        resource_store: "SessionResourceStore | None" = None,
        semantic: "SemanticRetrieval | None" = None,
        workspace: JobWorkspaceStore | None = None,
    ) -> None:
        self._fallback = fallback_coordinator
        self._resource_store = resource_store
        self._semantic = semantic
        self._workspace = workspace

    async def execute(
        self,
        plan: ExecutionPlan,
        request: WorkspaceJobRequest,
    ) -> WorkspaceJobResult:
        """Execute the plan and return a consolidated WorkspaceJobResult."""
        async with atrace_span(
            "athena.engine.execute",
            inputs={"plan": plan.to_dict(), "request": request.to_dict()},
            metadata=base_metadata(
                component="engine.execute",
                athena_session_id=request.session_id,
                job_id=request.job_id,
            ),
            tags=["engine", "workspace"],
        ) as run:
            if plan.is_trivial or len(plan.steps) <= 1:
                result = await self._fallback(request)
                finish_span(run, outputs={"fallback": True, "result": result.to_dict()})
                return result

            log.info(
                "[engine] executing %d-step plan for job %s",
                len(plan.steps),
                request.job_id[:8],
            )

            waves = _topological_sort(plan.steps)
            step_results: dict[str, dict[str, Any]] = {}
            all_artifacts: list[dict] = []
            all_handles: list[dict] = []
            all_follow_ups: list[str] = []
            error_count = 0

            for wave_idx, wave in enumerate(waves):
                log.debug(
                    "[engine] wave %d: %s",
                    wave_idx + 1,
                    [s.step_id for s in wave],
                )
                tasks = [self._run_step(step, request, step_results) for step in wave]
                outputs = await asyncio.gather(*tasks, return_exceptions=True)

                for step, output in zip(wave, outputs):
                    if isinstance(output, Exception):
                        log.error(
                            "[engine] step %s failed: %s",
                            step.step_id,
                            output,
                        )
                        step_results[step.output_key] = {
                            "summary": f"Step {step.step_id} failed.",
                            "artifacts": [],
                            "resource_handles": [],
                            "follow_up_questions": [],
                            "error": str(output),
                        }
                        error_count += 1
                    else:
                        step_results[step.output_key] = output
                        if self._workspace is not None:
                            self._workspace.record_step_result(
                                request,
                                step_id=step.step_id,
                                specialist=step.specialist,
                                instruction=instruction_preview(step),
                                output=output,
                            )
                        all_artifacts.extend(output.get("artifacts") or [])
                        all_handles.extend(output.get("resource_handles") or [])
                        all_follow_ups.extend(output.get("follow_up_questions") or [])

            summaries = [
                v["summary"]
                for v in step_results.values()
                if v.get("summary") and not v.get("error")
            ]
            summary = " ".join(summaries) if summaries else "Task completed."
            errors = [v["error"] for v in step_results.values() if v.get("error")]
            all_failed = error_count == len(plan.steps)

            result = WorkspaceJobResult(
                job_id=request.job_id,
                session_id=request.session_id,
                status="failed" if all_failed else "completed",
                completed_at=datetime.now(timezone.utc),
                summary=summary,
                artifacts=all_artifacts,
                resource_handles=all_handles,
                follow_up_questions=list(dict.fromkeys(all_follow_ups)),
                error="; ".join(errors) if errors else None,
            )
            finish_span(
                run,
                outputs={
                    "waves": len(waves),
                    "step_results": step_results,
                    "result": result.to_dict(),
                },
            )
            return result

    async def _run_step(
        self,
        step: PlanStep,
        request: WorkspaceJobRequest,
        prior_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Run a single plan step against its designated specialist agent."""
        instruction = _enrich_instruction(step, prior_results)

        async with atrace_span(
            "athena.engine.step",
            inputs={
                "step": {
                    "step_id": step.step_id,
                    "specialist": step.specialist,
                    "depends_on": step.depends_on,
                    "output_key": step.output_key,
                },
                "instruction": instruction,
            },
            metadata=base_metadata(
                component="engine.step",
                athena_session_id=request.session_id,
                job_id=request.job_id,
                step_id=step.step_id,
                specialist=step.specialist,
            ),
            tags=["engine", "step", step.specialist],
        ) as run:
            if step.specialist == "retrieval":
                agent = build_retrieval_agent(
                    self._resource_store,
                    request.session_id,
                    semantic=self._semantic,
                    workspace_store=self._workspace,
                    job_id=request.job_id,
                )
            else:
                builder = _SPECIALIST_BUILDERS.get(step.specialist)
                if builder is None:
                    log.warning(
                        "[engine] unknown specialist %r for step %s — using coordinator fallback",
                        step.specialist,
                        step.step_id,
                    )
                    sub = WorkspaceJobRequest(
                        session_id=request.session_id,
                        user_request=instruction,
                        conversation_window=request.conversation_window,
                    )
                    result = await self._fallback(sub)
                    payload = {
                        "summary": result.summary,
                        "artifacts": result.artifacts,
                        "resource_handles": result.resource_handles,
                        "follow_up_questions": result.follow_up_questions,
                        "error": result.error,
                    }
                    finish_span(run, outputs={"fallback": True, "output": payload})
                    return payload
                if step.specialist in {"gmail", "drive", "docs", "calendar", "sheets", "slides", "action"}:
                    agent = builder(
                        self._workspace,
                        session_id=request.session_id,
                        job_id=request.job_id,
                    )
                else:
                    agent = builder()

            session_service = InMemorySessionService()
            runner = Runner(
                agent=agent,
                app_name=_APP_NAME,
                session_service=session_service,
            )
            session = await session_service.create_session(
                app_name=_APP_NAME,
                user_id=_USER_ID,
            )

            log.debug(
                "[engine] step %s → specialist=%s session=%s",
                step.step_id,
                step.specialist,
                session.id[:8],
            )

            raw_text = await _run_agent_to_text(runner, session.id, instruction)
            output = _parse_specialist_result(raw_text)
            finish_span(
                run,
                outputs={
                    "adk_session_id": session.id,
                    "raw_text": raw_text,
                    "output": output,
                },
            )
            return output


def instruction_preview(step: PlanStep) -> str:
    text = " ".join(step.instruction.split())
    if len(text) <= 240:
        return text
    return text[:237].rstrip() + "..."
