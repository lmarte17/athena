"""Result injector — delivers completed job results back into live sessions.

Proactive injection (Phase D):
- If the user has been silent for at least PROACTIVE_SILENCE_SECS, inject the
  result immediately into the live ADK queue.
- Otherwise queue the result on `runtime.pending_results`; the per-session
  silence-monitor task in ws.py will drain it once silence is detected.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.genai import types

from app.hydration_scheduler import HydrationScheduler
from app.jobs.models import WorkspaceJobResult
from app.resource_store import ResourceHandle, SessionResourceStore
from app.tracing import atrace_span, base_metadata, finish_span, preview_value

log = logging.getLogger("athena.jobs.injector")

# Seconds of user silence required before injecting proactively.
PROACTIVE_SILENCE_SECS = float(os.getenv("PROACTIVE_SILENCE_SECS", "1.5"))
# Seconds since the last audio chunk was sent to the client before we consider
# the model silent. Prevents injecting a new prompt while the model is still
# streaming audio — which would create two overlapping responses at the client.
MODEL_AUDIO_SILENCE_SECS = float(os.getenv("MODEL_AUDIO_SILENCE_SECS", "0.4"))


@dataclass(slots=True)
class PendingInjection:
    result: WorkspaceJobResult
    prompt_override: str | None = None


class ResultInjector:
    """Injects WorkspaceJobResult payloads into live ADK sessions.

    Owned by SessionManager. Called when the job dispatcher reports a completed
    result. Checks for user silence before injecting; queues if the user is
    actively speaking.
    """

    def __init__(
        self,
        resource_store: SessionResourceStore,
        runtime_lookup: Callable[[str], Any | None],
        hydration_scheduler: HydrationScheduler | None = None,
        on_injected: Callable[[str], None] | None = None,
    ) -> None:
        self._store = resource_store
        self._runtime_lookup = runtime_lookup
        self._hydration_scheduler = hydration_scheduler
        self._on_injected = on_injected

    def prepare_result(self, result: WorkspaceJobResult) -> None:
        self._upsert_handles(result)

    async def inject(
        self,
        result: WorkspaceJobResult,
        *,
        prompt_override: str | None = None,
        handles_prepared: bool = False,
    ) -> None:
        """Smart inject: immediate if user is silent, queued otherwise."""
        session_id = result.session_id

        async with atrace_span(
            "athena.result_injector.inject",
            inputs={"result": result.to_dict()},
            metadata=base_metadata(
                component="result_injector.inject",
                athena_session_id=session_id,
                turn_id=result.source_turn_id,
                job_id=result.job_id,
                task_id=result.job_id,
                source_turn_id=result.source_turn_id,
            ),
            tags=["injector"],
        ) as run:
            if not handles_prepared:
                self._upsert_handles(result)

            runtime = self._runtime_lookup(session_id)
            if runtime is None:
                log.info(
                    "[job:%s] session %s no longer active — skip injection",
                    result.job_id[:8],
                    session_id,
                )
                finish_span(run, outputs={"injected": False, "reason": "runtime_missing"})
                return

            now = time.monotonic()
            user_elapsed = now - runtime.last_user_audio_at
            model_elapsed = now - getattr(runtime, "last_audio_sent_at", 0.0)
            if user_elapsed >= PROACTIVE_SILENCE_SECS and model_elapsed >= MODEL_AUDIO_SILENCE_SECS:
                log.info(
                    "[job:%s] injecting immediately (user silent %.1fs, model silent %.1fs)",
                    result.job_id[:8],
                    user_elapsed,
                    model_elapsed,
                )
                await self._do_inject(runtime, result, prompt_override=prompt_override)
                finish_span(
                    run,
                    outputs={"injected": True, "mode": "immediate", "user_elapsed": user_elapsed, "model_elapsed": model_elapsed},
                )
            else:
                log.info(
                    "[job:%s] queuing for proactive injection (user_elapsed=%.1fs model_elapsed=%.1fs)",
                    result.job_id[:8],
                    user_elapsed,
                    model_elapsed,
                )
                await runtime.pending_results.put(
                    PendingInjection(result=result, prompt_override=prompt_override)
                )
                finish_span(
                    run,
                    outputs={"injected": False, "mode": "queued", "user_elapsed": user_elapsed, "model_elapsed": model_elapsed},
                )

    async def inject_direct(
        self,
        runtime: Any,
        result: WorkspaceJobResult | PendingInjection,
        *,
        prompt_override: str | None = None,
        handles_prepared: bool = False,
    ) -> None:
        """Bypass silence check and inject immediately.

        Called by the silence-monitor task in ws.py once it confirms the user
        has gone quiet.
        """
        if isinstance(result, PendingInjection):
            prompt_override = result.prompt_override
            result = result.result

        async with atrace_span(
            "athena.result_injector.inject_direct",
            inputs={"result": result.to_dict()},
            metadata=base_metadata(
                component="result_injector.inject_direct",
                athena_session_id=result.session_id,
                turn_id=result.source_turn_id,
                job_id=result.job_id,
                task_id=result.job_id,
                source_turn_id=result.source_turn_id,
            ),
            tags=["injector"],
        ) as run:
            if not handles_prepared:
                self._upsert_handles(result)
            await self._do_inject(runtime, result, prompt_override=prompt_override)
            finish_span(run, outputs={"injected": True, "mode": "direct"})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_handles(self, result: WorkspaceJobResult) -> None:
        if not result.resource_handles:
            return
        try:
            handles = _deserialize_handles(result.resource_handles)
            if not handles:
                return

            stored = self._store.upsert_resources(result.session_id, handles)
            log.info(
                "[job:%s] stored %d resource handle(s) for session %s",
                result.job_id[:8],
                len(stored),
                result.session_id,
            )
            if self._hydration_scheduler is not None:
                scheduled = self._hydration_scheduler.schedule_resources(
                    result.session_id,
                    handles,
                )
                log.info(
                    "[job:%s] scheduled hydration for %d resource(s) in session %s",
                    result.job_id[:8],
                    scheduled,
                    result.session_id,
                )
        except Exception:
            log.exception(
                "[job:%s] resource-handle processing failed for session %s",
                result.job_id[:8],
                result.session_id,
            )

    async def _do_inject(
        self,
        runtime: Any,
        result: WorkspaceJobResult,
        *,
        prompt_override: str | None = None,
    ) -> None:
        """Build prompt and push it into the live ADK queue."""
        async with atrace_span(
            "athena.result_injector.do_inject",
            inputs={"result": result.to_dict()},
            metadata=base_metadata(
                component="result_injector.do_inject",
                athena_session_id=result.session_id,
                turn_id=result.source_turn_id,
                job_id=result.job_id,
                task_id=result.job_id,
                source_turn_id=result.source_turn_id,
            ),
            tags=["injector"],
        ) as run:
            payload = prompt_override if prompt_override is not None else _build_injection_prompt(result)
            if not payload:
                finish_span(run, outputs={"payload_created": False})
                return

            try:
                runtime.queue.send_content(
                    types.Content(
                        role="user",
                        parts=[types.Part(text=payload)],
                    )
                )
                log.info(
                    "[job:%s] injected into session %s (%d chars)",
                    result.job_id[:8],
                    result.session_id,
                    len(payload),
                )
                if self._on_injected is not None:
                    self._on_injected(result.session_id)
            except Exception as exc:
                log.exception(
                    "[job:%s] failed to inject into session %s",
                    result.job_id[:8],
                    result.session_id,
                )
                finish_span(run, error=str(exc))
                return

            try:
                await runtime.send_event({"type": "ready", "job_id": result.job_id})
            except Exception:
                log.debug(
                    "[job:%s] failed to emit ready event",
                    result.job_id[:8],
                    exc_info=True,
                )

            finish_span(
                run,
                outputs={
                    "payload": preview_value(payload),
                    "payload_chars": len(payload),
                    "ready_event_sent": True,
                },
            )


def _build_injection_prompt(result: WorkspaceJobResult) -> str:
    """Build the structured prompt injected into the live session."""
    if result.status == "failed":
        error_msg = result.error or "unknown error"
        return (
            "[Job result — status: failed]\n"
            "The background workspace job could not be completed.\n"
            f"Reason: {error_msg}\n\n"
            "Tell the user plainly what failed using the concrete reason above. "
            "Do not collapse it into a generic 'technical difficulty'. "
            "If a resource was created but unusable, say that directly. "
            "Then suggest one clear next step."
        )

    sections: list[str] = []

    if result.summary:
        sections.append(f"Summary: {result.summary}")

    for artifact in result.artifacts:
        artifact_type = str(artifact.get("type") or "result")
        title = str(artifact.get("title") or "")
        content = str(artifact.get("content") or "")
        if content:
            header = f"[{artifact_type}]" + (f" {title}" if title else "")
            sections.append(f"{header}\n{content}")

    if result.follow_up_questions:
        qs = "\n".join(f"- {q}" for q in result.follow_up_questions)
        sections.append(f"Suggested follow-ups:\n{qs}")

    if result.action_proposals:
        proposals = "\n".join(
            f"- {p.get('description', 'pending action')}" for p in result.action_proposals
        )
        sections.append(f"Pending actions requiring confirmation:\n{proposals}")

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return (
        "[Background workspace job completed]\n\n"
        f"{body}\n\n"
        "Use this result to answer the user's most recent request. "
        "If the result conflicts with anything said earlier, correct yourself plainly. "
        "Do not invent details not present in this result."
    )


def _deserialize_handles(raw_handles: list[dict[str, Any]]) -> list[ResourceHandle]:
    handles: list[ResourceHandle] = []
    for raw in raw_handles:
        try:
            handle = ResourceHandle(
                source=raw["source"],
                kind=raw["kind"],
                id=raw["id"],
                title=str(raw.get("title") or ""),
                url=raw.get("url"),
                version=raw.get("version"),
                metadata=dict(raw.get("metadata") or {}),
            )
            handles.append(handle)
        except (KeyError, TypeError) as exc:
            log.debug("Skipping malformed resource handle: %s — %s", raw, exc)
    return handles
