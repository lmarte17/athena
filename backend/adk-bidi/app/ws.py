"""WebSocket route for Athena's bidi audio session."""

import asyncio
import json
import logging
import os
import time
import traceback
from contextlib import contextmanager
from unittest import mock

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from google.adk.agents import LiveRequestQueue
from google.genai.errors import APIError

from app.dependencies import session_manager
from app.jobs.injector import PendingInjection
from app.orchestrator.tracing_plugin import consume_turn_id
from app.session_manager import LiveSessionRuntime
from app.context_builder import clear_active_query, set_active_query
from app.tracing import atrace_span, base_metadata, finish_span, record_surfaced_thought
from app.ws_downstream import DownstreamEventProcessor
from app.ws_logic import has_turn_content
from app.ws_session import IdleReflectionTracker
from app.ws_upstream import dispatch_upstream_frame

log = logging.getLogger("athena")

# How long to wait with no new turns before triggering idle reflection.
# Keeps memory fresh even when the tray stays connected for hours.
REFLECTION_IDLE_SECS = int(os.getenv("REFLECTION_IDLE_SECS", "120"))  # 2 min default

# How long (seconds) to wait after the last user audio before injecting a
# queued job result. Must match PROACTIVE_SILENCE_SECS in injector.py.
_SILENCE_POLL_INTERVAL = 0.1   # seconds between silence checks in monitor loop
_SILENCE_THRESHOLD = float(os.getenv("PROACTIVE_SILENCE_SECS", "1.5"))
# How long after the last audio-out chunk to wait before injecting a queued result.
# Prevents injecting a new prompt while the model is still generating its previous
# response — which would cause two overlapping audio streams at the client.
_MODEL_AUDIO_SILENCE_SECS = float(os.getenv("MODEL_AUDIO_SILENCE_SECS", "0.4"))

# Periodic keepalive ping to prevent Cloud Run / load-balancer idle-timeout
# from dropping the WebSocket connection without a proper close handshake.
# Set to 0 to disable. Default 30 s is well inside the GCP LB 600 s idle limit.
_WS_KEEPALIVE_SECS = float(os.getenv("WS_KEEPALIVE_SECS", "30"))

@contextmanager
def force_ai_studio():
    """Temporarily forces google.genai instantiation to use AI Studio.
    
    The google.genai and google.adk libraries read os.environ directly during 
    Runner/Client instantiation. By temporarily disabling the Vertex AI flag,
    we allow this specific connection to hit AI Studio while the rest of the
    app remains on Vertex AI.
    """
    if os.getenv("LIVE_VOICE_FORCE_AI_STUDIO") == "TRUE":
        with mock.patch.dict(os.environ, {"GOOGLE_GENAI_USE_VERTEXAI": "FALSE"}):
            yield
    else:
        yield

router = APIRouter()


def _is_benign_live_cancel(exc: Exception) -> bool:
    text = str(exc)
    return (
        "Thread was cancelled when writing StartStep status to channel" in text
        or "status = CANCELLED" in text
    )


@router.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    session_id: str | None = Query(default=None),
):
    await ws.accept()
    print(f"[athena] WS accepted (session_id param={session_id!r})", flush=True)

    try:
        session = await session_manager.prepare_session(session_id)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[athena] prepare_session FAILED: {e}", flush=True)
        log.error(f"Session setup failed: {e}\n{tb}")
        await ws.send_text(json.dumps({"type": "error", "error": f"Session setup failed: {e}"}))
        await ws.close()
        return

    runner = session_manager.build_runner()
    run_config = session_manager.build_run_config()
    queue = LiveRequestQueue()

    async def send_runtime_event(payload: dict) -> None:
        await ws.send_text(json.dumps(payload))

    session_manager.register_live_runtime(
        session.id,
        LiveSessionRuntime(queue=queue, send_event=send_runtime_event),
    )

    async def _run_idle_reflection(turns: list[dict]) -> None:
        log.info(f"Idle reflection triggered for {session.id} ({len(turns)} turns)")
        await session_manager._run_reflection_safely(
            session_id=session.id,
            transcript=turns,
            source="idle_timer",
        )

    reflection_tracker = IdleReflectionTracker(
        session_id=session.id,
        idle_seconds=REFLECTION_IDLE_SECS,
        on_idle_reflection=_run_idle_reflection,
    )

    async def upstream():
        """Receive from the tray client, forward to Gemini Live."""
        try:
            while True:
                raw = await ws.receive()
                # Stamp the speech clock only when the Rust VAD signals activity_start.
                # Audio is now streamed continuously (server-side VAD re-enabled), so
                # stamping on every binary frame would keep last_user_audio_at perpetually
                # fresh and prevent the silence monitor from ever injecting job results.
                if raw.get("text"):
                    try:
                        msg = json.loads(raw["text"])
                        if msg.get("type") == "activity_start":
                            runtime = session_manager.get_live_runtime(session.id)
                            if runtime is not None:
                                runtime.last_user_audio_at = time.monotonic()
                    except Exception:
                        pass
                dispatch_upstream_frame(raw, queue)

        except WebSocketDisconnect:
            log.info("Client disconnected (upstream)")
        except Exception as e:
            log.error(f"Upstream error: {e}")
        finally:
            queue.close()

    async def keepalive():
        """Send periodic ping frames to prevent LB idle-timeout disconnects."""
        if _WS_KEEPALIVE_SECS <= 0:
            return
        try:
            while True:
                await asyncio.sleep(_WS_KEEPALIVE_SECS)
                await ws.send_text(json.dumps({"type": "keepalive"}))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # WS already closed; upstream/downstream will handle it

    async def silence_monitor():
        """Drain pending job results once the user falls silent.

        The ResultInjector queues results here when the user was speaking at
        injection time. This task polls the pending_results queue and injects
        each result as soon as a silence window opens.
        """
        runtime = session_manager.get_live_runtime(session.id)
        if runtime is None:
            return
        try:
            while True:
                # Non-blocking peek — avoids blocking when the queue is empty.
                try:
                    pending = runtime.pending_results.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(_SILENCE_POLL_INTERVAL)
                    continue

                # Got a pending result — wait until both the user AND model are silent.
                # Injecting while the model is still producing audio causes a second
                # overlapping response whose chunks interleave with the first in the
                # client's audio queue, making words sound jumbled.
                while True:
                    now = time.monotonic()
                    user_elapsed = now - runtime.last_user_audio_at
                    model_elapsed = now - runtime.last_audio_sent_at
                    if user_elapsed >= _SILENCE_THRESHOLD and model_elapsed >= _MODEL_AUDIO_SILENCE_SECS:
                        break
                    await asyncio.sleep(_SILENCE_POLL_INTERVAL)

                log.info(
                    "[silence_monitor] injecting queued job %s for session %s",
                    (
                        pending.result.job_id[:8]
                        if isinstance(pending, PendingInjection)
                        else pending.job_id[:8]
                    ),
                    session.id,
                )
                await session_manager.result_injector.inject_direct(runtime, pending, handles_prepared=True)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Silence monitor error for session %s: %s", session.id, e)

    async def downstream():
        """Receive from Gemini Live, forward to the tray client."""
        async def on_turn_complete(transcript_in: str, transcript_out: str) -> str:
            turn_id = consume_turn_id(getattr(session, "state", None))
            async with atrace_span(
                "athena.live.turn",
                inputs={
                    "turn_id": turn_id,
                    "transcript_in": transcript_in,
                    "transcript_out": transcript_out,
                },
                metadata=base_metadata(
                    component="live.turn",
                    athena_session_id=session.id,
                    turn_id=turn_id,
                    model=getattr(session_manager._live_voice_agent, "model", None),
                ),
                tags=["live", "turn"],
            ) as run:
                clear_active_query(session.id)
                print(
                    f"[athena] turn_complete "
                    f"id={turn_id} "
                    f"in={repr(transcript_in[:60]) if transcript_in else '(empty)'} "
                    f"out={repr(transcript_out[:60]) if transcript_out else '(empty)'}",
                    flush=True,
                )

                await session_manager.handle_completed_turn(
                    session,
                    turn_id=turn_id,
                    transcript_in=transcript_in,
                    transcript_out=transcript_out,
                )

                if has_turn_content(transcript_in, transcript_out):
                    session_manager.record_turn(session.id, transcript_in, transcript_out)
                    reflection_tracker.record_turn(transcript_in, transcript_out)
                finish_span(
                    run,
                    outputs={
                        "has_content": has_turn_content(transcript_in, transcript_out),
                        "turn_id": turn_id,
                    },
                )
            return turn_id

        async def _send_audio_bytes(data: bytes) -> None:
            """Forward audio to the tray and stamp last_audio_sent_at."""
            rt = session_manager.get_live_runtime(session.id)
            if rt is not None:
                rt.last_audio_sent_at = time.monotonic()
            await ws.send_bytes(data)

        processor = DownstreamEventProcessor(
            send_text=ws.send_text,
            send_bytes=_send_audio_bytes,
            broadcast=lambda payload: session_manager.broadcast({**payload, "session_id": session.id}),
            on_turn_complete=on_turn_complete,
            on_input_transcription=lambda text: set_active_query(session.id, text),
            on_thought=lambda text: record_surfaced_thought(
                athena_session_id=session.id,
                text=text,
                source="live_content_part",
            ),
        )

        try:
            async with atrace_span(
                "athena.live.run",
                inputs={"session_id": session.id},
                metadata=base_metadata(
                    component="live.run",
                    athena_session_id=session.id,
                    model=getattr(session_manager._live_voice_agent, "model", None),
                ),
                tags=["live"],
            ) as run:
                with force_ai_studio():
                    async for event in runner.run_live(
                        session=session,
                        live_request_queue=queue,
                        run_config=run_config,
                    ):
                        await processor.process_event(event)
                finish_span(run, outputs={"session_id": session.id})

        except WebSocketDisconnect:
            log.info("Client disconnected (downstream)")
        except APIError as e:
            if _is_benign_live_cancel(e):
                log.info("Live stream cancelled cleanly for session %s: %s", session.id, e)
                return
            tb = traceback.format_exc()
            log.error(f"Downstream API error: {e}\n{tb}")
            try:
                await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
            except Exception:
                pass
        except Exception as e:
            tb = traceback.format_exc()
            log.error(f"Downstream error: {e}\n{tb}")
            try:
                await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
            except Exception:
                pass

    silence_task: asyncio.Task | None = None
    keepalive_task: asyncio.Task | None = None
    async with atrace_span(
        "athena.ws.session",
        inputs={
            "requested_session_id": session_id,
            "athena_session_id": session.id,
        },
        metadata=base_metadata(
            component="ws.session",
            athena_session_id=session.id,
        ),
        tags=["ws", "live"],
    ) as session_run:
        try:
            await ws.send_text(json.dumps({
                "type": "status",
                "status": "connected",
                "session_id": session.id,
            }))
            print(f"[athena] connected session={session.id}", flush=True)

            silence_task = asyncio.create_task(
                silence_monitor(),
                name=f"silence-monitor-{session.id[:8]}",
            )
            keepalive_task = asyncio.create_task(
                keepalive(),
                name=f"ws-keepalive-{session.id[:8]}",
            )
            upstream_task = asyncio.create_task(upstream(), name=f"ws-upstream-{session.id[:8]}")
            downstream_task = asyncio.create_task(downstream(), name=f"ws-downstream-{session.id[:8]}")
            done, pending = await asyncio.wait(
                {upstream_task, downstream_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    raise exc
            finish_span(session_run, outputs={"closed_cleanly": True})
        finally:
            for bg_task in (silence_task, keepalive_task):
                if bg_task is not None and not bg_task.done():
                    bg_task.cancel()
                    try:
                        await bg_task
                    except asyncio.CancelledError:
                        pass

            pending_turns = await reflection_tracker.finalize()
            log.info("WebSocket closed")
            await session_manager.on_session_end(session.id, pending_turns)
