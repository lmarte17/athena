from app.adk_agents.workspace_coordinator import _COORDINATOR_INSTRUCTION
from app.jobs.models import WorkspaceJobRequest
from app.planner.planner_agent import _trivial_plan


def test_trivial_plan_maps_slides_create_to_slides_specialist():
    request = WorkspaceJobRequest(
        job_id="job-1",
        session_id="session-1",
        user_request="Turn the QBR document into a presentation.",
        job_type_hint="slides_create",
    )

    plan = _trivial_plan(request)

    assert plan.is_trivial is True
    assert len(plan.steps) == 1
    assert plan.steps[0].specialist == "slides"


def test_coordinator_instruction_calls_docs_before_slides_for_doc_to_deck_requests():
    assert "docs_specialist (read/export the doc text) → 2. slides_specialist" in (
        _COORDINATOR_INSTRUCTION
    )
