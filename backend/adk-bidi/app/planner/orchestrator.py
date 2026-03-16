"""Orchestrator — top-level job runner that wires PlannerAgent → ExecutionEngine.

Call flow:
  1. PlannerAgent decides whether the job is trivial or multi-step (DAG).
  2. Trivial  → coordinator.run() directly (unchanged path).
  3. Complex  → ExecutionEngine.execute() runs steps in parallel waves.
  4. On plan/execute failure → fallback to coordinator (always safe).
  5. After a successful complex job, SkillLibrary.distill() fires in background.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.planner.execution_engine import ExecutionEngine
from app.planner.planner_agent import PlannerAgent
from app.planner.skill_library import SkillLibrary
from app.tracing import atrace_span, base_metadata, finish_span

log = logging.getLogger("athena.planner.orchestrator")


class WorkspaceBackend:
    """Wraps the planner + engine into a single workspace-backend callable."""

    def __init__(
        self,
        planner: PlannerAgent,
        engine: ExecutionEngine,
        fallback: Callable[[WorkspaceJobRequest], Awaitable[WorkspaceJobResult]],
        skill_library: SkillLibrary | None = None,
    ) -> None:
        self._planner = planner
        self._engine = engine
        self._fallback = fallback
        self._skill_library = skill_library

    async def run(self, request: WorkspaceJobRequest) -> WorkspaceJobResult:
        """Plan and execute a workspace job, returning a WorkspaceJobResult."""
        async with atrace_span(
            "athena.orchestrator.run",
            inputs={"request": request.to_dict()},
            metadata=base_metadata(
                component="orchestrator.run",
                athena_session_id=request.session_id,
                job_id=request.job_id,
            ),
            tags=["job", "orchestrator"],
        ) as run:
            try:
                plan = await self._planner.plan(request)
            except Exception as exc:
                log.exception(
                    "[orchestrator] planner failed for job %s — coordinator fallback",
                    request.job_id[:8],
                )
                result = await self._fallback(request)
                finish_span(
                    run,
                    outputs={
                        "fallback": "planner_failure",
                        "error": str(exc),
                        "result": result.to_dict(),
                    },
                )
                return result

            try:
                if plan.is_trivial:
                    result = await self._fallback(request)
                else:
                    result = await self._engine.execute(plan, request)
            except Exception as exc:
                log.exception(
                    "[orchestrator] execution failed for job %s — coordinator fallback",
                    request.job_id[:8],
                )
                result = await self._fallback(request)
                finish_span(
                    run,
                    outputs={
                        "fallback": "execution_failure",
                        "error": str(exc),
                        "plan": plan.to_dict(),
                        "result": result.to_dict(),
                    },
                )
                return result

            if (
                self._skill_library is not None
                and not plan.is_trivial
                and result.status == "completed"
            ):
                asyncio.create_task(
                    self._skill_library.distill(request, plan, result),
                    name=f"distill-{request.job_id[:8]}",
                )

            finish_span(
                run,
                outputs={
                    "plan": plan.to_dict(),
                    "result": result.to_dict(),
                },
            )
            return result


def build_workspace_backend(
    coordinator,
    skill_library: SkillLibrary | None = None,
    workspace=None,
) -> WorkspaceBackend:
    """Construct a fully-wired WorkspaceBackend using the coordinator as fallback.

    Args:
        coordinator: An object with a `.run(request)` async method (WorkspaceCoordinator).
        skill_library: Optional SkillLibrary instance. Pass None to disable skills.
    """
    planner = PlannerAgent(skill_library=skill_library, job_workspace=workspace)
    engine = ExecutionEngine(
        fallback_coordinator=coordinator.run,
        resource_store=getattr(coordinator, "_resource_store", None),
        semantic=getattr(coordinator, "_semantic", None),
        workspace=workspace,
    )
    return WorkspaceBackend(
        planner=planner,
        engine=engine,
        fallback=coordinator.run,
        skill_library=skill_library,
    )


Orchestrator = WorkspaceBackend
build_orchestrator = build_workspace_backend
