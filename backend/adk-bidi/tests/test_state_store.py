from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.orchestrator.contracts import (
    Mode,
    OrchestratorDecision,
    PendingConfirmation,
    TaskResult,
    TaskSpec,
    TaskStatus,
    TurnEnvelope,
)
from app.orchestrator.state_store import StateStore


def test_state_store_round_trips_turn_envelope_and_decision():
    store = StateStore()
    session = SimpleNamespace(id="session-1", state={})

    envelope = TurnEnvelope(
        session_id="session-1",
        turn_id="turn-1",
        transcript="Find the latest deck",
        timestamp=datetime(2026, 3, 15, tzinfo=UTC),
        active_task_ids=[],
    )
    decision = OrchestratorDecision(
        decision_id="decision-1",
        session_id="session-1",
        source_event_id="turn-1",
        mode=Mode.noop,
    )

    store.set_turn_envelope(session, envelope)
    store.set_decision(session, decision)

    assert store.get_turn_envelope(session) == envelope
    assert store.get_decision(session) == decision

def test_state_store_round_trips_pending_task_specs():
    store = StateStore()
    session = SimpleNamespace(id="session-1", state={})
    specs = [
        TaskSpec(
            task_id="task-1",
            task_kind="gmail_search",
            goal="Check my email",
            input_payload={"user_request": "Check my email"},
        )
    ]

    store.set_pending_task_specs(session, specs)

    assert store.get_pending_task_specs(session) == specs


def test_state_store_round_trips_task_result():
    store = StateStore()
    session = SimpleNamespace(id="session-1", state={})
    result = TaskResult(
        task_id="task-1",
        session_id="session-1",
        status=TaskStatus.completed,
        summary="Done",
    )

    store.set_task_result(session, result)

    assert store.get_task_result(session) == result


def test_state_store_round_trips_pending_confirmation_and_suppressed_result():
    store = StateStore()
    session = SimpleNamespace(id="session-1", state={})
    confirmation = PendingConfirmation(
        confirmation_id="confirm-1",
        session_id="session-1",
        source_task_id="task-source",
        task_spec=TaskSpec(
            task_id="task-action",
            task_kind="action",
            goal="Execute the pending action",
            input_payload={"user_request": "Execute the pending action"},
        ),
        action_preview="Send the draft to Alice",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    store.set_pending_confirmation(session, confirmation)
    store.mark_task_result_suppressed(
        session,
        "task-old",
        replacement_task_id="task-new",
        reason="Superseded",
    )

    assert store.get_latest_pending_confirmation(session) == confirmation
    assert store.pop_pending_confirmation(session, "task-action") == confirmation
    assert store.consume_suppressed_task_result(session, "task-old") == {
        "replacement_task_id": "task-new",
        "reason": "Superseded",
    }
