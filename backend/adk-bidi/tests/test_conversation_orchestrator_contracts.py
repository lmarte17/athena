from __future__ import annotations

from datetime import UTC, datetime

from app.orchestrator.contracts import (
    FollowUpTaskPlan,
    Mode,
    OrchestratorDecision,
    PendingConfirmation,
    ResponsePlan,
    SurfaceMode,
    SurfacePlan,
    TaskSpec,
    Tone,
    TurnEnvelope,
    TurnRecord,
)


def test_task_spec_round_trip():
    spec = TaskSpec(task_id="t1", task_kind="gmail_search", goal="find emails")

    assert TaskSpec(**spec.model_dump()) == spec


def test_follow_up_and_pending_confirmation_round_trip():
    spec = TaskSpec(task_id="t2", task_kind="action", goal="send the draft")
    confirmation = PendingConfirmation(
        confirmation_id="c1",
        session_id="s1",
        source_task_id="source-1",
        task_spec=spec,
        action_preview="Send the draft",
        created_at=datetime(2026, 3, 15, tzinfo=UTC),
    )
    follow_up = FollowUpTaskPlan(
        user_request="Find the emails from last week instead",
        acknowledgment="Okay, I'll use that correction.",
        task_kind="gmail_search",
        resource_hints=["gmail"],
    )

    assert PendingConfirmation(**confirmation.model_dump()) == confirmation
    assert FollowUpTaskPlan(**follow_up.model_dump()) == follow_up


def test_orchestrator_decision_mode_values():
    for mode in Mode:
        decision = OrchestratorDecision(
            decision_id="d1",
            session_id="s1",
            source_event_id="e1",
            mode=mode,
        )
        assert decision.mode == mode


def test_turn_envelope_and_surface_plan_serialize_with_nested_models():
    plan = SurfacePlan(
        surface_mode=SurfaceMode.wait_for_silence,
        response_plan=ResponsePlan(text="Checking now.", tone=Tone.bridge),
        coalesce_with_task_ids=["t1", "t2"],
    )

    envelope = TurnEnvelope(
        session_id="session-1",
        turn_id="turn-1",
        transcript="Find emails from Alice",
        timestamp=datetime(2026, 3, 15, tzinfo=UTC),
        recent_turns=[TurnRecord(role="user", content="Find emails from Alice")],
        active_task_ids=["t1"],
    )

    payload = {"decision_id": "d2", "session_id": "session-1", "source_event_id": "turn-1", "mode": Mode.start_tasks, "surface_plan": plan}
    decision = OrchestratorDecision(**payload)

    assert decision.surface_plan == plan
    assert TurnEnvelope(**envelope.model_dump()) == envelope
