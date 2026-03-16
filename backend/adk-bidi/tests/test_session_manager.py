import asyncio
from types import SimpleNamespace

import pytest
from google.adk.sessions import DatabaseSessionService

from app.jobs.models import WorkspaceJobRequest
from app.jobs.models import WorkspaceJobResult
from app.orchestrator.contracts import (
    Mode,
    OrchestratorDecision,
    ResponsePlan,
    SurfaceMode,
    TaskResult,
    TaskSpec,
    TaskStatus,
    Tone,
)
from app.orchestrator.tracing_plugin import TracingPlugin
from app.resource_store import ResourceHandle
from app.session_manager import SessionManager


@pytest.mark.asyncio
async def test_prepare_session_ignores_requested_session_id_and_creates_fresh_session(monkeypatch):
    manager = SessionManager()

    calls = {"create": 0, "get": 0}

    async def fake_create_session(*, app_name: str, user_id: str):
        calls["create"] += 1
        assert app_name == "athena"
        assert user_id == "local_user"
        return SimpleNamespace(id="fresh-session-id")

    async def fake_get_session(*args, **kwargs):
        del args, kwargs
        calls["get"] += 1
        return SimpleNamespace(id="old-session-id")

    registered: list[tuple[str, str]] = []

    monkeypatch.setattr(
        manager,
        "session_service",
        SimpleNamespace(create_session=fake_create_session, get_session=fake_get_session),
    )
    monkeypatch.setattr(manager.context_builder, "build", lambda: "memory bundle")
    monkeypatch.setattr(
        "app.session_manager.register_context",
        lambda sid, bundle: registered.append((sid, bundle)),
    )

    session = await manager.prepare_session("old-session-id")

    assert session.id == "fresh-session-id"
    assert calls == {"create": 1, "get": 0}
    assert registered == [("fresh-session-id", "memory bundle")]


def test_unregister_live_runtime_clears_runtime_and_job_store():
    manager = SessionManager()
    runtime = SimpleNamespace(queue=SimpleNamespace(), send_event=None)
    manager.register_live_runtime("session-1", runtime)

    request = WorkspaceJobRequest(job_id="job-1", session_id="session-1")
    result = WorkspaceJobResult(job_id="job-1", session_id="session-1", status="completed")
    manager.job_store.put_request(request)
    manager.job_store.put_result(result)

    assert manager.get_live_runtime("session-1") is runtime
    assert manager.job_store.get_request("job-1") is not None
    assert manager.job_store.get_result("job-1") is not None

    manager.unregister_live_runtime("session-1")

    assert manager.get_live_runtime("session-1") is None
    assert manager.job_store.get_request("job-1") is None
    assert manager.job_store.get_result("job-1") is None


@pytest.mark.asyncio
async def test_on_session_end_clears_session_resources_and_unregisters_context(monkeypatch):
    manager = SessionManager()
    manager.resource_store.upsert_metadata(
        "session-1",
        ResourceHandle(
            source="gmail",
            kind="thread",
            id="thread-123",
            title="Budget follow-up",
            version="1772815800000",
        ),
    )

    cancelled: list[str] = []
    unregistered: list[str] = []
    scheduled: list[str] = []

    async def fake_reflection(*, session_id: str, transcript: list[dict], source: str) -> None:
        scheduled.append(f"{session_id}:{source}:{len(transcript)}")

    def fake_create_task(coro, name=None):
        coro.close()
        scheduled.append(name or "unnamed")
        return SimpleNamespace()

    monkeypatch.setattr(manager.hydration_scheduler, "cancel_session", cancelled.append)
    monkeypatch.setattr("app.session_manager.unregister_context", unregistered.append)
    monkeypatch.setattr(manager, "_run_reflection_safely", fake_reflection)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    await manager.on_session_end("session-1", [{"user": "hi", "athena": "hello"}])

    assert cancelled == ["session-1"]
    assert unregistered == ["session-1"]
    assert manager.resource_store.list_handles("session-1") == []
    assert scheduled == ["reflect-session-"]


def test_build_runner_registers_tracing_plugin(monkeypatch):
    manager = SessionManager()
    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("app.session_manager.Runner", FakeRunner)

    manager.build_runner()

    assert captured["agent"] is manager._live_voice_agent
    assert captured["app_name"] == "athena"
    assert captured["session_service"] is manager.session_service
    assert len(captured["plugins"]) == 1
    assert isinstance(captured["plugins"][0], TracingPlugin)


def test_build_runner_uses_plain_tracing_plugin(monkeypatch):
    manager = SessionManager()
    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("app.session_manager.Runner", FakeRunner)

    manager.build_runner()

    assert manager._conversation_orchestrator.spokes["workspace"] is manager._workspace_spoke


def test_build_turn_envelope_includes_recent_turns_and_active_tasks():
    manager = SessionManager()
    manager.record_turn("session-1", "Earlier question", "Earlier answer")
    session = SimpleNamespace(
        id="session-1",
        state={
            "user:active_tasks": {
                "task-1": {
                    "task_id": "task-1",
                    "task_kind": "gmail_search",
                    "goal": "search inbox",
                    "input_payload": {"user_request": "search inbox"},
                    "dependencies": [],
                    "run_policy": "background",
                    "confirmation_required": False,
                    "surface_on_completion": True,
                }
            }
        },
    )

    envelope = manager.build_turn_envelope(
        session,
        turn_id="turn-1",
        transcript_in="Check my email",
        transcript_out="I'm checking.",
    )

    assert envelope.turn_id == "turn-1"
    assert envelope.transcript == "Check my email"
    assert envelope.active_task_ids == ["task-1"]
    assert [item.role for item in envelope.recent_turns] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_handle_completed_turn_runs_orchestrator_shell(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(id="session-1", state={})
    captured = {}

    async def fake_run(session_arg):
        captured["turn_envelope"] = manager._conversation_state_store.get_turn_envelope(session_arg)
        decision = OrchestratorDecision(
            decision_id="decision-1",
            session_id=session_arg.id,
            source_event_id="turn-1",
            mode=Mode.noop,
        )
        manager._conversation_state_store.set_decision(session_arg, decision)
        return decision

    monkeypatch.setattr(manager, "_run_conversation_orchestrator", fake_run)

    decision = await manager.handle_completed_turn(
        session,
        turn_id="turn-1",
        transcript_in="Check my email",
        transcript_out="I'm checking.",
    )

    assert decision is not None
    assert decision.mode == Mode.noop
    assert captured["turn_envelope"].turn_id == "turn-1"
    assert session.state["temp:decision"]["decision_id"] == "decision-1"


@pytest.mark.asyncio
async def test_handle_completed_turn_submits_pending_tasks(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(id="session-1", state={})

    async def fake_run(session_arg):
        decision = OrchestratorDecision(
            decision_id="decision-2",
            session_id=session_arg.id,
            source_event_id="turn-2",
            mode=Mode.respond_and_start_tasks,
            task_specs=[
                TaskSpec(
                    task_id="task-1",
                    task_kind="gmail_search",
                    goal="Check email",
                    input_payload={"user_request": "Check email", "job_type_hint": "gmail_search"},
                )
            ],
        )
        manager._conversation_state_store.set_decision(session_arg, decision)
        manager._conversation_state_store.set_pending_task_specs(session_arg, decision.task_specs)
        return decision

    monkeypatch.setattr(manager, "_run_conversation_orchestrator", fake_run)

    decision = await manager.handle_completed_turn(
        session,
        turn_id="turn-2",
        transcript_in="Check email",
        transcript_out="I'm checking.",
    )

    assert decision is not None
    assert decision.mode == Mode.respond_and_start_tasks
    assert manager.job_store.get_request("task-1") is not None
    assert manager.job_queue.qsize() == 1


@pytest.mark.asyncio
async def test_handle_completed_turn_uses_direct_response_spoke_for_task_starts(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(id="session-1", state={})
    rendered = []

    async def fake_run(session_arg):
        decision = OrchestratorDecision(
            decision_id="decision-bridge",
            session_id=session_arg.id,
            source_event_id="turn-bridge",
            mode=Mode.respond_and_start_tasks,
            task_specs=[
                TaskSpec(
                    task_id="task-bridge",
                    task_kind="slides_create",
                    goal="Create a presentation",
                    input_payload={
                        "user_request": "Create a presentation",
                        "job_type_hint": "slides_create",
                    },
                )
            ],
        )
        manager._conversation_state_store.set_decision(session_arg, decision)
        manager._conversation_state_store.set_pending_task_specs(session_arg, decision.task_specs)
        return decision

    async def fake_invoke(turn_envelope, *, mode, clarification_request=None):
        assert turn_envelope.transcript == "Create a presentation"
        assert mode == Mode.respond_and_start_tasks
        assert clarification_request is None
        return ResponsePlan(text="Okay, I'll handle that.", tone=Tone.bridge)

    async def fake_render(session_id, response_plan):
        rendered.append((session_id, response_plan.text, response_plan.tone))
        return True

    monkeypatch.setattr(manager, "_run_conversation_orchestrator", fake_run)
    monkeypatch.setattr(manager, "_direct_response_spoke", SimpleNamespace(invoke=fake_invoke))
    monkeypatch.setattr(manager, "_voice_egress_adapter", SimpleNamespace(render=fake_render))

    decision = await manager.handle_completed_turn(
        session,
        turn_id="turn-bridge",
        transcript_in="Create a presentation",
        transcript_out="Let me think.",
    )

    assert decision is not None
    assert decision.mode == Mode.respond_and_start_tasks
    assert rendered == [("session-1", "Okay, I'll handle that.", Tone.bridge)]
    assert manager.job_store.get_request("task-bridge") is not None


@pytest.mark.asyncio
async def test_handle_completed_turn_renders_direct_response(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(id="session-1", state={})
    rendered = []

    async def fake_run(session_arg):
        decision = OrchestratorDecision(
            decision_id="decision-3",
            session_id=session_arg.id,
            source_event_id="turn-3",
            mode=Mode.respond_now,
        )
        manager._conversation_state_store.set_decision(session_arg, decision)
        return decision

    async def fake_invoke(turn_envelope, *, mode, clarification_request=None):
        assert turn_envelope.transcript == "What time is it?"
        assert mode == Mode.respond_now
        assert clarification_request is None
        return ResponsePlan(text="It is 3 PM.", tone=Tone.direct_answer)

    async def fake_render(session_id, response_plan):
        rendered.append((session_id, response_plan.text, response_plan.tone))
        return True

    monkeypatch.setattr(manager, "_run_conversation_orchestrator", fake_run)
    monkeypatch.setattr(manager, "_direct_response_spoke", SimpleNamespace(invoke=fake_invoke))
    monkeypatch.setattr(manager, "_voice_egress_adapter", SimpleNamespace(render=fake_render))

    decision = await manager.handle_completed_turn(
        session,
        turn_id="turn-3",
        transcript_in="What time is it?",
        transcript_out="One second.",
    )

    assert decision is not None
    assert decision.mode == Mode.respond_now
    assert rendered == [("session-1", "It is 3 PM.", Tone.direct_answer)]
    assert session.state["temp:decision"]["response_plan"]["text"] == "It is 3 PM."


@pytest.mark.asyncio
async def test_handle_completed_turn_uses_clarification_loop(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(id="session-1", state={})
    rendered = []

    async def fake_run(session_arg):
        decision = OrchestratorDecision(
            decision_id="decision-4",
            session_id=session_arg.id,
            source_event_id="turn-4",
            mode=Mode.ask_clarify,
            clarification_request="The subject is missing.",
        )
        manager._conversation_state_store.set_decision(session_arg, decision)
        return decision

    async def fake_invoke(turn_envelope, *, clarification_request=None):
        assert turn_envelope.transcript == "What about that one?"
        assert clarification_request == "The subject is missing."
        return ResponsePlan(text="Which one do you mean?", tone=Tone.clarification)

    async def fake_render(session_id, response_plan):
        rendered.append((session_id, response_plan.text, response_plan.tone))
        return True

    monkeypatch.setattr(manager, "_run_conversation_orchestrator", fake_run)
    monkeypatch.setattr(manager, "_clarification_loop", SimpleNamespace(invoke=fake_invoke))
    monkeypatch.setattr(manager, "_voice_egress_adapter", SimpleNamespace(render=fake_render))

    decision = await manager.handle_completed_turn(
        session,
        turn_id="turn-4",
        transcript_in="What about that one?",
        transcript_out="Tell me a little more.",
    )

    assert decision is not None
    assert decision.mode == Mode.ask_clarify
    assert rendered == [("session-1", "Which one do you mean?", Tone.clarification)]
    assert session.state["temp:decision"]["response_plan"]["text"] == "Which one do you mean?"


@pytest.mark.asyncio
async def test_handle_completed_turn_renders_confirmation_response_plan(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(id="session-1", state={})
    rendered = []

    async def fake_run(session_arg):
        decision = OrchestratorDecision(
            decision_id="decision-confirm",
            session_id=session_arg.id,
            source_event_id="turn-confirm",
            mode=Mode.await_confirmation,
            response_plan=ResponsePlan(
                text="Do you want me to send that now?",
                tone=Tone.clarification,
            ),
        )
        manager._conversation_state_store.set_decision(session_arg, decision)
        return decision

    async def fake_render(session_id, response_plan):
        rendered.append((session_id, response_plan.text, response_plan.tone))
        return True

    monkeypatch.setattr(manager, "_run_conversation_orchestrator", fake_run)
    monkeypatch.setattr(manager, "_voice_egress_adapter", SimpleNamespace(render=fake_render))

    decision = await manager.handle_completed_turn(
        session,
        turn_id="turn-confirm",
        transcript_in="Yes, send it.",
        transcript_out="Let me confirm that.",
    )

    assert decision is not None
    assert decision.mode == Mode.await_confirmation
    assert rendered == [("session-1", "Do you want me to send that now?", Tone.clarification)]


@pytest.mark.asyncio
async def test_persist_orchestrator_state_flushes_user_state(tmp_path):
    manager = SessionManager()
    manager.session_service = DatabaseSessionService(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    session = await manager.session_service.create_session(app_name="athena", user_id="local_user")
    session.state["user:current_mode"] = "tasks_running"
    session.state["user:active_tasks"] = {
        "task-1": {
            "task_id": "task-1",
            "task_kind": "gmail_search",
            "goal": "Check email",
            "input_payload": {"user_request": "Check email"},
            "dependencies": [],
            "run_policy": "background",
            "confirmation_required": False,
            "surface_on_completion": True,
        }
    }

    await manager._persist_orchestrator_state(session)

    fetched = await manager.session_service.get_session(
        app_name="athena",
        user_id="local_user",
        session_id=session.id,
    )

    assert fetched is not None
    assert fetched.state["user:current_mode"] == "tasks_running"
    assert "task-1" in fetched.state["user:active_tasks"]


@pytest.mark.asyncio
async def test_handle_brokered_task_result_routes_surface_plan_through_injector(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(id="session-1", state={})
    manager._sessions["session-1"] = session
    manager.register_live_runtime(
        "session-1",
        SimpleNamespace(
            queue=SimpleNamespace(send_content=lambda _content: None),
            send_event=lambda _payload: None,
            last_user_audio_at=0.0,
            pending_results=__import__("asyncio").Queue(),
            deferred_results=[],
        ),
    )
    calls = []

    async def fake_run_for_task_result(session_arg, task_result):
        assert session_arg is session
        assert task_result.task_id == "task-1"
        return OrchestratorDecision(
            decision_id="decision-result",
            session_id=session_arg.id,
            source_event_id=task_result.task_id,
            mode=Mode.respond_now,
            surface_plan=__import__("app.orchestrator.contracts", fromlist=["SurfacePlan"]).SurfacePlan(
                surface_mode=SurfaceMode.wait_for_silence,
                response_plan=__import__("app.orchestrator.contracts", fromlist=["ResponsePlan", "Tone"]).ResponsePlan(
                    text="surface prompt",
                    tone=__import__("app.orchestrator.contracts", fromlist=["Tone"]).Tone.completion,
                ),
            ),
        )

    async def fake_inject(result, **kwargs):
        calls.append((result.job_id, kwargs["prompt_override"]))

    monkeypatch.setattr(manager, "_run_conversation_orchestrator_for_task_result", fake_run_for_task_result)
    monkeypatch.setattr(manager.result_injector, "inject", fake_inject)

    await manager._handle_brokered_task_result(
        TaskResult(
            task_id="task-1",
            session_id="session-1",
            status=TaskStatus.completed,
            summary="Found it",
        )
    )

    assert session.state["user:completed_tasks"][0]["task_id"] == "task-1"
    assert calls == [("task-1", "surface prompt")]


@pytest.mark.asyncio
async def test_handle_brokered_task_result_skips_suppressed_tasks(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(
        id="session-1",
        state={
            "user:suppressed_task_results": {
                "task-1": {"replacement_task_id": "task-2", "reason": "Superseded"}
            }
        },
    )
    manager._sessions["session-1"] = session

    injected = []

    async def fake_inject(_result, **kwargs):
        injected.append(kwargs)

    monkeypatch.setattr(manager.result_injector, "inject", fake_inject)

    await manager._handle_brokered_task_result(
        TaskResult(
            task_id="task-1",
            session_id="session-1",
            status=TaskStatus.completed,
            summary="Late result",
        )
    )

    assert injected == []
    assert "user:completed_tasks" not in session.state
    assert "user:suppressed_task_results" not in session.state


@pytest.mark.asyncio
async def test_apply_surface_plan_defers_until_next_turn(monkeypatch):
    manager = SessionManager()
    runtime = SimpleNamespace(
        queue=SimpleNamespace(send_content=lambda _content: None),
        send_event=lambda _payload: None,
        last_user_audio_at=0.0,
        pending_results=__import__("asyncio").Queue(),
        deferred_results=[],
    )
    manager.register_live_runtime("session-1", runtime)

    prepared = []
    injected = []

    monkeypatch.setattr(manager.result_injector, "prepare_result", lambda result: prepared.append(result.job_id))

    async def fake_inject_direct(runtime_arg, pending, **kwargs):
        injected.append((runtime_arg, pending.result.job_id, pending.prompt_override, kwargs["handles_prepared"]))

    monkeypatch.setattr(manager.result_injector, "inject_direct", fake_inject_direct)

    task_result = TaskResult(
        task_id="task-2",
        session_id="session-1",
        status=TaskStatus.completed,
        summary="Found it",
    )
    surface_plan = __import__("app.orchestrator.contracts", fromlist=["SurfacePlan", "ResponsePlan", "Tone"]).SurfacePlan(
        surface_mode=SurfaceMode.next_turn,
        response_plan=__import__("app.orchestrator.contracts", fromlist=["ResponsePlan", "Tone"]).ResponsePlan(
            text="surface later",
            tone=__import__("app.orchestrator.contracts", fromlist=["Tone"]).Tone.completion,
        ),
    )

    await manager.apply_surface_plan(task_result, surface_plan)
    await manager._flush_deferred_results("session-1")

    assert prepared == ["task-2"]
    assert injected == [(runtime, "task-2", "surface later", True)]


@pytest.mark.asyncio
async def test_handle_completed_turn_skips_synthetic_assistant_only_turn(monkeypatch):
    manager = SessionManager()
    session = SimpleNamespace(
        id="session-1",
        state={"temp:skip_orchestrator_assistant_only_turns": 1},
    )
    called = {"orchestrator": 0}

    async def fake_run(_session_arg):
        called["orchestrator"] += 1
        raise AssertionError("synthetic turn should not reach orchestrator")

    monkeypatch.setattr(manager, "_run_conversation_orchestrator", fake_run)

    decision = await manager.handle_completed_turn(
        session,
        turn_id="turn-synth",
        transcript_in="",
        transcript_out="It is 3 PM.",
    )

    assert decision is None
    assert called["orchestrator"] == 0
    assert "temp:skip_orchestrator_assistant_only_turns" not in session.state
