import asyncio
import logging
import time
import uuid
from contextlib import aclosing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from google.adk.agents import LiveRequestQueue
from google.adk.agents import RunConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from app.adk_agents.live_voice import build_live_voice_agent
from app.adk_agents.workspace_coordinator import build_workspace_coordinator
from app.context_builder import (
    ContextBuilder,
    enable_dynamic_context,
    register_context,
    set_context_provider,
    unregister_context,
)
from app.hydration_scheduler import HydrationScheduler
from app.job_workspace import JobWorkspaceStore
from app.jobs.dispatcher import JobDispatcher
from app.jobs.injector import PendingInjection, ResultInjector
from app.jobs.queue import JobQueue, JobStore
from app.orchestrator.conversation_orchestrator import ConversationOrchestrator
from app.orchestrator.contracts import (
    Mode,
    OrchestratorDecision,
    SurfaceMode,
    TaskResult,
    TurnEnvelope,
    TurnRecord,
)
from app.orchestrator.clarification_loop import ClarificationLoop
from app.orchestrator.correction_loop import CorrectionLoop
from app.orchestrator.direct_response_spoke import DirectResponseSpoke
from app.orchestrator.result_broker import ResultBroker
from app.orchestrator.state_store import StateStore
from app.orchestrator.task_manager import TaskManager
from app.orchestrator.tracing_plugin import TracingPlugin
from app.orchestrator.voice_egress_adapter import VoiceEgressAdapter
from app.orchestrator.workspace_spoke import WorkspaceSpoke
from app.memory_service import MemoryService
from app.planner.orchestrator import build_workspace_backend
from app.planner.skill_library import SkillLibrary
from app.reflection_agent import ReflectionAgent
from app.resource_store import ResourceSnapshot, SessionResourceStore
from app.retrieval import SemanticRetrieval
from app.tap_agent import IncrementalTapAgent

APP_NAME = "athena"
USER_ID = "local_user"

log = logging.getLogger("athena.session_manager")


@dataclass
class LiveSessionRuntime:
    queue: LiveRequestQueue
    send_event: Callable[[dict], Awaitable[None]]
    # Monotonic timestamp of the last audio bytes received from the user.
    # Updated by ws.py on every audio frame; used by ResultInjector for proactive
    # injection gating (don't inject while the user is actively speaking).
    last_user_audio_at: float = field(default_factory=time.monotonic)
    # Monotonic timestamp of the last audio bytes sent TO the tray (model output).
    # Updated by ws.py every time a binary audio frame is forwarded downstream.
    # Used to prevent injection while the model is actively speaking — injecting
    # mid-stream causes the model to generate a second overlapping response.
    last_audio_sent_at: float = field(default_factory=time.monotonic)
    # Results queued while the user was speaking; drained by the silence monitor.
    pending_results: asyncio.Queue = field(default_factory=asyncio.Queue)
    # Results intentionally deferred until the next completed user turn.
    deferred_results: list[PendingInjection] = field(default_factory=list)


def _db_url() -> str:
    path = Path.home() / ".athena" / "sessions.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path}"


class SessionManager:
    """
    Orchestrates session lifecycle, background jobs, memory, and retrieval.

    Architecture:
    - LiveVoiceAgent is the single streaming agent.
    - Workspace jobs flow through JobQueue -> JobDispatcher -> Orchestrator.
    - ResultInjector delivers completed job results back into the live session.
    - SessionResourceStore and HydrationScheduler manage session-scoped resources.
    - Memory agents (TapAgent, ReflectionAgent) handle persistence outside the live turn.
    """

    def __init__(self) -> None:
        self.session_service = DatabaseSessionService(_db_url())
        self._subscribers: list[asyncio.Queue] = []

        # Memory agents
        self.memory_service = MemoryService()
        self.reflection_agent = ReflectionAgent(self.memory_service)
        self.tap_agent = IncrementalTapAgent(self.memory_service)

        # Context builder
        self.context_builder = ContextBuilder(self.memory_service)
        set_context_provider(
            lambda session_id, bundle, query: self.context_builder.build(
                session_id=session_id,
                query=query,
            )
            or bundle
        )

        # Tap queue
        self._tap_queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(self._tap_queue)

        # Background task handles
        self._drain_task: asyncio.Task | None = None
        self._live_runtimes: dict[str, LiveSessionRuntime] = {}
        self._sessions: dict[str, object] = {}
        self._recent_turns: dict[str, list[dict[str, str]]] = {}

        # Semantic retrieval — embedding-based chunk search
        self.semantic = SemanticRetrieval()
        self.job_workspace = JobWorkspaceStore()

        # Resource store and hydration
        self.resource_store = SessionResourceStore()
        self.hydration_scheduler = HydrationScheduler(self.resource_store)

        # Auto-index resources into the semantic store when they become content_ready
        async def _index_on_ready(snapshot: ResourceSnapshot) -> None:
            try:
                await self.semantic.index_resource_snapshot(snapshot)
            except Exception:
                log.warning("Semantic indexing failed for %s", snapshot.handle.id, exc_info=True)

        self.resource_store.set_content_ready_callback(_index_on_ready)

        # Job system
        self.job_queue = JobQueue()
        self.job_store = JobStore()

        # Result injector — injects completed job results back into live sessions
        self.result_injector = ResultInjector(
            resource_store=self.resource_store,
            runtime_lookup=self.get_live_runtime,
            hydration_scheduler=self.hydration_scheduler,
            on_injected=self._mark_synthetic_turn_expected,
        )
        self.result_broker = ResultBroker()

        # Job dispatcher — drained in startup()
        self.job_dispatcher = JobDispatcher(
            queue=self.job_queue,
            store=self.job_store,
            workspace=self.job_workspace,
        )

        # Workspace coordinator — base single-shot router (used as fallback)
        self._coordinator = build_workspace_coordinator(
            resource_store=self.resource_store,
            semantic=self.semantic,
            workspace=self.job_workspace,
        )

        # Skill library — SQLite-backed reusable plan templates.
        # Started in startup() so the DB connection opens inside the event loop.
        self.skill_library = SkillLibrary()

        # Orchestrator — PlannerAgent + ExecutionEngine wrapping the coordinator.
        # Replaces raw coordinator.run as the dispatcher's runner.
        self._workspace_backend = build_workspace_backend(
            coordinator=self._coordinator,
            skill_library=self.skill_library,
            workspace=self.job_workspace,
        )
        self._workspace_spoke = WorkspaceSpoke(self._workspace_backend)
        self._direct_response_spoke = DirectResponseSpoke()
        self._clarification_loop = ClarificationLoop()
        self._correction_loop = CorrectionLoop()
        self._voice_egress_adapter = VoiceEgressAdapter(
            runtime_lookup=self.get_live_runtime,
            on_rendered=self._mark_synthetic_turn_expected,
        )

        # Live voice agent — created once, shared across sessions
        # (instruction callable reads per-session context on every turn)
        self._live_voice_agent = build_live_voice_agent(
            job_queue=self.job_queue,
            job_store=self.job_store,
            conversation_window_lookup=self.get_recent_turns,
        )

        # Conversation spine state and task lifecycle management.
        self._conversation_state_store = StateStore()
        self._task_manager = TaskManager(
            state_store=self._conversation_state_store,
            job_queue=self.job_queue,
            job_store=self.job_store,
            workspace_spoke=self._workspace_spoke,
        )
        self.job_dispatcher.set_task_executor(self._task_manager.execute_workspace_request)
        self.job_dispatcher.set_result_adapter(self._task_manager.task_result_to_workspace_job_result)
        self.job_dispatcher.set_task_result_callback(self.result_broker.publish)
        self._conversation_orchestrator = ConversationOrchestrator(
            name="ConversationOrchestrator",
            description="Deterministic conversation spine for Athena sessions.",
            task_manager=self._task_manager,
            spokes={
                "workspace": self._workspace_spoke,
                "direct_response": self._direct_response_spoke,
                "clarification": self._clarification_loop,
                "correction": self._correction_loop,
            },
            state_store=self._conversation_state_store,
            workspace_store=self.job_workspace,
        )
        self._conversation_orchestrator_runner = Runner(
            agent=self._conversation_orchestrator,
            app_name=APP_NAME,
            plugins=[TracingPlugin()],
            session_service=self.session_service,
        )

    async def startup(self) -> None:
        """Start background tasks. Call from FastAPI lifespan handler."""
        self._drain_task = asyncio.create_task(self._drain_tap(), name="tap-drain")
        await self.result_broker.startup(self._handle_brokered_task_result)
        await self.semantic.startup()
        await self.skill_library.startup()
        await self.job_dispatcher.startup()
        log.info("SessionManager started (tap drain + job dispatcher running)")
        print("[athena] SessionManager startup complete — tap drain + job dispatcher running", flush=True)

    async def shutdown(self) -> None:
        """Graceful shutdown of background tasks."""
        await self.job_dispatcher.shutdown()
        await self.result_broker.shutdown()
        await self.skill_library.shutdown()
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

    async def _drain_tap(self) -> None:
        """Drain turn_complete events and run incremental memory extraction."""
        while True:
            try:
                event = await self._tap_queue.get()
                if event.get("type") != "turn_complete":
                    continue
                transcript_in = event.get("transcript_in", "")
                transcript_out = event.get("transcript_out", "")
                if not transcript_in.strip() and not transcript_out.strip():
                    continue
                entries = await self.tap_agent.extract(transcript_in, transcript_out)
                if entries:
                    self.memory_service.stage_candidates(
                        entries,
                        source_session_id=event.get("session_id"),
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Tap drain error: {e}")

    def build_runner(self) -> Runner:
        return Runner(
            agent=self._live_voice_agent,
            app_name=APP_NAME,
            plugins=[TracingPlugin()],
            session_service=self.session_service,
        )

    def build_run_config(self) -> RunConfig:
        return RunConfig(
            response_modalities=["AUDIO"],
            streaming_mode=StreamingMode.BIDI,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
                ),
                language_code="en-US",
            ),
            realtime_input_config=types.RealtimeInputConfig(
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Prevents context-window errors on long sessions by sliding the
            # window once it approaches the token limit.
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=100_000,
                sliding_window=types.SlidingWindow(target_tokens=50_000),
            ),
        )

    def register_live_runtime(self, session_id: str, runtime: LiveSessionRuntime) -> None:
        self._live_runtimes[session_id] = runtime

    def get_live_runtime(self, session_id: str) -> LiveSessionRuntime | None:
        return self._live_runtimes.get(session_id)

    def unregister_live_runtime(self, session_id: str) -> None:
        self._live_runtimes.pop(session_id, None)
        self._sessions.pop(session_id, None)
        self.job_store.clear_session(session_id)
        self._recent_turns.pop(session_id, None)
        self.job_workspace.clear_session(session_id)

    def record_turn(self, session_id: str, transcript_in: str, transcript_out: str) -> None:
        turns = self._recent_turns.setdefault(session_id, [])
        if transcript_in:
            turns.append({"role": "user", "content": transcript_in})
        if transcript_out:
            turns.append({"role": "assistant", "content": transcript_out})
        if len(turns) > 12:
            del turns[:-12]

    def get_recent_turns(self, session_id: str, limit: int = 6) -> list[dict[str, str]]:
        turns = self._recent_turns.get(session_id, [])
        return [dict(item) for item in turns[-limit:]]

    def build_turn_envelope(
        self,
        session,
        *,
        turn_id: str,
        transcript_in: str,
        transcript_out: str,
    ) -> TurnEnvelope:
        recent_turns = [
            TurnRecord(role=item["role"], content=item["content"])
            for item in self.get_recent_turns(session.id)
            if item.get("role") in {"user", "assistant"}
        ]
        transcript = transcript_in.strip() or transcript_out.strip()
        return TurnEnvelope(
            session_id=session.id,
            turn_id=turn_id,
            transcript=transcript,
            timestamp=datetime.now(timezone.utc),
            recent_turns=recent_turns,
            active_task_ids=self._conversation_state_store.get_active_task_ids(session),
            source="voice",
        )

    async def handle_completed_turn(
        self,
        session,
        *,
        turn_id: str,
        transcript_in: str,
        transcript_out: str,
    ) -> OrchestratorDecision | None:
        self._conversation_state_store.clear_turn_envelope(session)
        self._conversation_state_store.clear_decision(session)
        self._conversation_state_store.clear_pending_task_specs(session)
        self._conversation_state_store.clear_task_result(session)

        has_content = bool(transcript_in.strip() or transcript_out.strip())
        if (
            has_content
            and not transcript_in.strip()
            and self._conversation_state_store.consume_assistant_only_turn_skip(session)
        ):
            return None
        if not has_content:
            return None

        envelope = self.build_turn_envelope(
            session,
            turn_id=turn_id,
            transcript_in=transcript_in,
            transcript_out=transcript_out,
        )
        self._conversation_state_store.set_turn_envelope(session, envelope)
        session.state["temp:turn_id"] = turn_id

        decision: OrchestratorDecision | None = None
        try:
            decision = await self._run_conversation_orchestrator(session)
        except Exception:
            log.exception(
                "ConversationOrchestrator shell failed for session %s turn %s",
                session.id,
                turn_id,
            )
        finally:
            session.state.pop("temp:turn_id", None)

        if decision is not None:
            await self._task_manager.submit_pending_tasks(session)
            await self._maybe_render_direct_response(session, decision)
            await self._persist_orchestrator_state(session)
        await self._flush_deferred_results(session.id)
        return decision

    async def _run_conversation_orchestrator(self, session) -> OrchestratorDecision | None:
        # Phase 1 deliberately avoids Runner.run_async() because it requires a
        # synthetic user message, which would pollute the existing live session
        # history before the orchestrator actually owns ingress.
        invocation_context = self._conversation_orchestrator_runner._new_invocation_context(
            session=session,
        )
        async with aclosing(self._conversation_orchestrator.run_async(invocation_context)) as agen:
            async for _event in agen:
                pass
        return self._conversation_state_store.get_decision(session)

    async def _run_conversation_orchestrator_for_task_result(
        self,
        session,
        task_result: TaskResult,
    ) -> OrchestratorDecision | None:
        self._conversation_state_store.clear_turn_envelope(session)
        self._conversation_state_store.clear_decision(session)
        self._conversation_state_store.clear_pending_task_specs(session)
        self._conversation_state_store.set_task_result(session, task_result)
        try:
            return await self._run_conversation_orchestrator(session)
        finally:
            self._conversation_state_store.clear_task_result(session)

    async def _handle_brokered_task_result(self, task_result: TaskResult) -> None:
        workspace_result = self._task_manager.task_result_to_workspace_job_result(task_result)
        session = self._sessions.get(task_result.session_id)
        if (
            session is not None
            and self._conversation_state_store.consume_suppressed_task_result(session, task_result.task_id)
            is not None
        ):
            log.info(
                "[task:%s] skipping suppressed task result for session %s",
                task_result.task_id[:8],
                task_result.session_id,
            )
            await self._persist_orchestrator_state(session)
            return
        if session is not None:
            self._task_manager.complete_task(session, task_result)

        if session is None:
            log.info(
                "[task:%s] session %s no longer active — skip orchestrated surfacing",
                task_result.task_id[:8],
                task_result.session_id,
            )
            return

        decision = await self._run_conversation_orchestrator_for_task_result(session, task_result)
        if decision is None or decision.surface_plan is None:
            await self._persist_orchestrator_state(session)
            return
        await self.apply_surface_plan(task_result, decision.surface_plan)
        await self._persist_orchestrator_state(session)

    async def _maybe_render_direct_response(
        self,
        session,
        decision: OrchestratorDecision,
    ) -> None:
        if decision.mode not in {
            Mode.respond_now,
            Mode.ask_clarify,
            Mode.respond_and_start_tasks,
            Mode.await_confirmation,
        }:
            return

        # For respond_and_start_tasks with no pre-built response plan, the live
        # model has already bridged in real-time ("On it.", "Let me check.", etc.).
        # Calling DirectResponseSpoke and injecting a second bridge phrase creates
        # an awkward double-response ~1-2 s after the model already spoke.
        # Skip the injection; rely on the live model's instruction for bridging.
        # (Pre-built plans from follow-up/correction flows are still rendered
        # because they carry context the live model didn't have when it responded.)
        if decision.mode == Mode.respond_and_start_tasks and decision.response_plan is None:
            return

        turn_envelope = self._conversation_state_store.get_turn_envelope(session)
        if turn_envelope is None or not turn_envelope.transcript.strip():
            return

        if decision.response_plan is None:
            if decision.mode == Mode.ask_clarify:
                response_plan = await self._clarification_loop.invoke(
                    turn_envelope,
                    clarification_request=decision.clarification_request,
                )
            elif decision.mode == Mode.await_confirmation:
                return
            else:
                response_plan = await self._direct_response_spoke.invoke(
                    turn_envelope,
                    mode=decision.mode,
                    clarification_request=decision.clarification_request,
                )
            decision = decision.model_copy(update={"response_plan": response_plan})
            self._conversation_state_store.set_decision(session, decision)

        await self._voice_egress_adapter.render(session.id, decision.response_plan)

    async def apply_surface_plan(self, task_result: TaskResult, surface_plan) -> None:
        workspace_result = self._task_manager.task_result_to_workspace_job_result(task_result)
        prompt_override = surface_plan.response_plan.text
        runtime = self.get_live_runtime(task_result.session_id)
        self.result_injector.prepare_result(workspace_result)

        if surface_plan.surface_mode == SurfaceMode.silent_state_only:
            return
        if runtime is None:
            log.info(
                "[task:%s] session %s no longer active — skip surface plan",
                task_result.task_id[:8],
                task_result.session_id,
            )
            return
        if surface_plan.surface_mode == SurfaceMode.immediate:
            await self.result_injector.inject_direct(
                runtime,
                workspace_result,
                prompt_override=prompt_override,
                handles_prepared=True,
            )
            return
        if surface_plan.surface_mode == SurfaceMode.next_turn:
            runtime.deferred_results.append(
                PendingInjection(
                    result=workspace_result,
                    prompt_override=prompt_override,
                )
            )
            return

        await self.result_injector.inject(
            workspace_result,
            prompt_override=prompt_override,
            handles_prepared=True,
        )

    async def _flush_deferred_results(self, session_id: str) -> None:
        runtime = self.get_live_runtime(session_id)
        if runtime is None or not runtime.deferred_results:
            return

        pending = list(runtime.deferred_results)
        runtime.deferred_results.clear()
        for item in pending:
            await self.result_injector.inject_direct(
                runtime,
                item,
                handles_prepared=True,
            )

    def _mark_synthetic_turn_expected(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        self._conversation_state_store.increment_assistant_only_turn_skip(session)

    async def _persist_orchestrator_state(self, session) -> None:
        if (
            session is None
            or not hasattr(session, "app_name")
            or not hasattr(session, "user_id")
            or not hasattr(self.session_service, "append_event")
        ):
            return

        state_delta = {
            key: value
            for key, value in getattr(session, "state", {}).items()
            if isinstance(key, str) and key.startswith("user:")
        }
        if not state_delta:
            return

        await self.session_service.append_event(
            session,
            Event(
                author="athena_state_sync",
                invocation_id=f"state-sync-{uuid.uuid4()}",
                actions=EventActions(state_delta=state_delta),
            ),
        )

    async def prepare_session(self, session_id: str | None = None):
        """Create a fresh session for every websocket connection."""
        if session_id:
            log.info("Ignoring requested session %s and creating a fresh session", session_id)

        session = await self.session_service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
        )
        self._sessions[session.id] = session
        log.info(f"Created session {session.id}")

        try:
            bundle = self.context_builder.build(session.id)
        except TypeError:
            bundle = self.context_builder.build()
        register_context(session.id, bundle)
        enable_dynamic_context(session.id)
        log.info(
            f"Context registered for {session.id} "
            f"({'with memory' if bundle else 'no memory yet'})"
        )
        return session

    def broadcast(self, event: dict) -> None:
        """Fan-out a structured event to all subscribers."""
        for q in self._subscribers:
            q.put_nowait(event)

    async def _run_reflection_safely(
        self,
        session_id: str,
        transcript: list[dict],
        source: str,
    ) -> None:
        try:
            await self.reflection_agent.run(session_id=session_id, transcript=transcript)
        except Exception:
            log.exception(
                f"Reflection task crashed (source={source}, session={session_id})"
            )

    async def on_session_end(self, session_id: str, transcript: list[dict]) -> None:
        """Post-session hook — runs reflection and releases session resources."""
        log.info(f"Session ended: {session_id} ({len(transcript)} turns)")
        self.unregister_live_runtime(session_id)
        unregister_context(session_id)
        self.resource_store.clear_session(session_id)
        self.hydration_scheduler.cancel_session(session_id)
        # Await reflection directly so it completes before Cloud Run terminates
        # the container. The WS is already closed so this doesn't affect the user.
        try:
            await asyncio.wait_for(
                self._run_reflection_safely(
                    session_id=session_id,
                    transcript=transcript,
                    source="session_end",
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            log.warning(f"Reflection timed out after 30s for session {session_id}")
