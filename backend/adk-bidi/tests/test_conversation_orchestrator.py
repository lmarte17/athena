from __future__ import annotations

from contextlib import aclosing
from datetime import UTC, datetime, timedelta

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from app.jobs.models import WorkspaceJobResult
from app.jobs.queue import JobQueue, JobStore
from app.orchestrator.conversation_orchestrator import ConversationOrchestrator
from app.orchestrator.contracts import Mode, PendingConfirmation, TaskResult, TaskSpec, TaskStatus, TurnEnvelope
from app.orchestrator.state_store import StateStore
from app.orchestrator.task_manager import TaskManager
from app.orchestrator.workspace_spoke import WorkspaceSpoke


def _build_task_manager(state_store: StateStore) -> TaskManager:
    async def fake_backend(_request):
        return WorkspaceJobResult(status="completed", summary="done")

    return TaskManager(
        state_store=state_store,
        job_queue=JobQueue(),
        job_store=JobStore(),
        workspace_spoke=WorkspaceSpoke(fake_backend),
    )


@pytest.mark.asyncio
async def test_conversation_orchestrator_shell_stores_noop_decision_for_empty_turn():
    state_store = StateStore()
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-1",
            transcript="",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=[],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.noop
    assert decision.source_event_id == "turn-1"
    assert decision.session_id == session.id


@pytest.mark.asyncio
async def test_conversation_orchestrator_turns_workspace_request_into_task_spec():
    state_store = StateStore()
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-2",
            transcript="Find emails from Alice",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=[],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    pending_specs = state_store.get_pending_task_specs(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.respond_and_start_tasks
    assert decision.task_specs[0].task_kind == "gmail_search"
    assert decision.task_specs[0].input_payload["resource_hints"] == ["gmail"]
    assert pending_specs == decision.task_specs


@pytest.mark.asyncio
async def test_conversation_orchestrator_routes_slide_read_requests_to_slides():
    state_store = StateStore()
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-slide-read",
            transcript="Inspect the presentation and summarize slide 2",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=[],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.respond_and_start_tasks
    assert decision.task_specs[0].task_kind == "slides_read"
    assert decision.task_specs[0].input_payload["resource_hints"] == ["slides"]


@pytest.mark.asyncio
async def test_conversation_orchestrator_routes_slide_edit_requests_to_slides_create():
    state_store = StateStore()
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-slide-write",
            transcript="Reorder the presentation, replace the title text, and change the background color",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=[],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.respond_and_start_tasks
    assert decision.task_specs[0].task_kind == "slides_create"
    assert decision.task_specs[0].input_payload["resource_hints"] == ["slides"]


@pytest.mark.asyncio
async def test_conversation_orchestrator_routes_direct_question_to_respond_now():
    state_store = StateStore()
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-3",
            transcript="What time is it?",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=[],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.respond_now


@pytest.mark.asyncio
async def test_conversation_orchestrator_routes_vague_turn_to_ask_clarify():
    state_store = StateStore()
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-4",
            transcript="What about that one?",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=[],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.ask_clarify
    assert decision.clarification_request is not None


@pytest.mark.asyncio
async def test_conversation_orchestrator_turns_task_result_into_surface_plan():
    state_store = StateStore()
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_task_result(
        session,
        TaskResult(
            task_id="task-9",
            session_id=session.id,
            status=TaskStatus.completed,
            summary="Found the email",
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.respond_now
    assert decision.surface_plan is not None
    assert decision.surface_plan.response_plan.text.startswith("[Background workspace job completed]")


@pytest.mark.asyncio
async def test_conversation_orchestrator_tracks_pending_confirmation_from_task_result():
    state_store = StateStore()
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_task_result(
        session,
        TaskResult(
            task_id="task-10",
            session_id=session.id,
            status=TaskStatus.completed,
            summary="Draft is ready.",
            task_metadata={
                "action_proposals": [
                    {"description": "Send the draft to Alice"},
                ]
            },
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    pending = state_store.get_latest_pending_confirmation(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.await_confirmation
    assert decision.surface_plan is not None
    assert pending is not None
    assert pending.action_preview == "Send the draft to Alice"


@pytest.mark.asyncio
async def test_conversation_orchestrator_turns_confirmation_reply_into_action_task():
    state_store = StateStore()
    task_manager = _build_task_manager(state_store)
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
        task_manager=task_manager,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    state_store.set_pending_confirmation(
        session,
        PendingConfirmation(
            confirmation_id="confirm-1",
            session_id=session.id,
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
        ),
    )
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-5",
            transcript="Yes, send it.",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=[],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.respond_and_start_tasks
    assert decision.response_plan is not None
    assert decision.task_specs[0].task_kind == "action"
    assert "Send the draft to Alice" in decision.task_specs[0].goal
    assert state_store.get_latest_pending_confirmation(session) is None


@pytest.mark.asyncio
async def test_conversation_orchestrator_turns_correction_into_replacement_task():
    state_store = StateStore()
    task_manager = _build_task_manager(state_store)
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
        task_manager=task_manager,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    session.state["user:active_tasks"] = {
        "task-1": {
            "task_id": "task-1",
            "task_kind": "gmail_search",
            "goal": "Find emails from Alice this week",
            "input_payload": {
                "user_request": "Find emails from Alice this week",
                "resource_hints": ["gmail"],
            },
            "dependencies": [],
            "run_policy": "background",
            "confirmation_required": False,
            "surface_on_completion": True,
            "dedupe_key": "gmail_search:find emails from alice this week",
        }
    }
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-6",
            transcript="Actually, from last week instead.",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=["task-1"],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.respond_and_start_tasks
    assert decision.task_specs[0].input_payload["prior_task_id"] == "task-1"
    assert session.state["user:active_tasks"] == {}
    assert state_store.consume_suppressed_task_result(session, "task-1") is not None


@pytest.mark.asyncio
async def test_conversation_orchestrator_turns_continuation_into_follow_up_task():
    state_store = StateStore()
    task_manager = _build_task_manager(state_store)
    agent = ConversationOrchestrator(
        name="ConversationOrchestrator",
        state_store=state_store,
        task_manager=task_manager,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="athena",
        session_service=session_service,
    )
    session = await session_service.create_session(app_name="athena", user_id="local_user")
    session.state["user:completed_tasks"] = [
        {
            "task_id": "task-7",
            "session_id": session.id,
            "status": "completed",
            "summary": "Found the Q1 report in Drive",
            "artifacts": [],
            "resource_handles": [],
            "follow_up_questions": [],
            "error": None,
            "task_metadata": {
                "job_type_hint": "drive_search",
                "resource_hints": ["drive"],
            },
        }
    ]
    state_store.set_turn_envelope(
        session,
        TurnEnvelope(
            session_id=session.id,
            turn_id="turn-7",
            transcript="Turn that into a doc.",
            timestamp=datetime(2026, 3, 15, tzinfo=UTC),
            active_task_ids=[],
        ),
    )

    ctx = runner._new_invocation_context(session=session)
    async with aclosing(agent.run_async(ctx)) as agen:
        events = [event async for event in agen]

    decision = state_store.get_decision(session)
    assert events == []
    assert decision is not None
    assert decision.mode == Mode.respond_and_start_tasks
    assert "Follow-up:" in decision.task_specs[0].goal
