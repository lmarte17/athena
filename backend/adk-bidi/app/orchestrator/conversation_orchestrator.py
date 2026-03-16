"""Deterministic conversation spine for Athena sessions."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from pydantic import Field

from app.orchestrator.contracts import (
    FollowUpTaskPlan,
    Mode,
    OrchestratorDecision,
    PendingConfirmation,
    ResponsePlan,
    SurfaceMode,
    SurfacePlan,
    TaskResult,
    TaskSpec,
    TaskStatus,
    Tone,
    TurnEnvelope,
)
from app.orchestrator.state_store import SessionMode, StateStore

log = logging.getLogger("athena.orchestrator.conversation_orchestrator")


class ConversationOrchestrator(BaseAgent):
    """Deterministic conversation spine for turn routing and task lifecycle."""

    task_manager: Any | None = None
    spokes: dict[str, Any] = Field(default_factory=dict)
    state_store: StateStore
    workspace_store: Any | None = None
    confirmation_ttl_seconds: int = 300

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        turn_envelope = self.state_store.get_turn_envelope(ctx)
        task_result = self.state_store.get_task_result(ctx)
        decision = await self._decide_async(turn_envelope, task_result, ctx)
        self.state_store.set_decision(ctx, decision)
        if decision.task_specs:
            self.state_store.set_pending_task_specs(ctx, decision.task_specs)
        else:
            self.state_store.clear_pending_task_specs(ctx)

        log.info(
            "shell_decision session_id=%s turn_id=%s decision_id=%s mode=%s",
            decision.session_id,
            turn_envelope.turn_id if turn_envelope else "",
            decision.decision_id,
            decision.mode.value,
        )

        if False:
            yield  # pragma: no cover

    async def _run_live_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        raise NotImplementedError("ConversationOrchestrator does not run in live mode.")
        if False:
            yield  # pragma: no cover

    async def _decide_async(
        self,
        turn_envelope: TurnEnvelope | None,
        task_result: TaskResult | None,
        ctx: InvocationContext,
    ) -> OrchestratorDecision:
        if task_result is not None:
            return self._decide_for_task_result(task_result, ctx)

        source_event_id = turn_envelope.turn_id if turn_envelope is not None else ctx.invocation_id
        session_id = turn_envelope.session_id if turn_envelope is not None else ctx.session.id
        pending_confirmation = self._current_pending_confirmation(ctx)

        if turn_envelope is not None and pending_confirmation is not None:
            normalized_text = self._normalize_text(turn_envelope.transcript)
            if self._is_confirmation_acceptance(normalized_text):
                return self._confirm_pending_task(
                    turn_envelope,
                    pending_confirmation,
                    ctx,
                )
            if self._is_confirmation_rejection(normalized_text):
                return self._reject_pending_task(
                    turn_envelope,
                    pending_confirmation,
                    ctx,
                )

        follow_up_kind = self._classify_follow_up_turn(
            turn_envelope,
            ctx,
            pending_confirmation,
        )
        if turn_envelope is not None and follow_up_kind is not None:
            return await self._decide_for_follow_up(
                turn_envelope,
                follow_up_kind=follow_up_kind,
                pending_confirmation=pending_confirmation,
                ctx=ctx,
            )

        return self._decide_base(
            turn_envelope,
            ctx,
            session_id=session_id,
            source_event_id=source_event_id,
        )

    def _decide_base(
        self,
        turn_envelope: TurnEnvelope | None,
        ctx: InvocationContext,
        *,
        session_id: str,
        source_event_id: str,
    ) -> OrchestratorDecision:
        del ctx
        task_specs = self._task_specs_from_turn_envelope(turn_envelope) if turn_envelope is not None else []
        if task_specs:
            return OrchestratorDecision(
                decision_id=str(uuid.uuid4()),
                session_id=session_id,
                source_event_id=source_event_id,
                mode=Mode.respond_and_start_tasks,
                task_specs=task_specs,
            )
        if turn_envelope is None or not turn_envelope.transcript.strip():
            return OrchestratorDecision(
                decision_id=str(uuid.uuid4()),
                session_id=session_id,
                source_event_id=source_event_id,
                mode=Mode.noop,
            )
        if self._should_clarify(turn_envelope):
            return OrchestratorDecision(
                decision_id=str(uuid.uuid4()),
                session_id=session_id,
                source_event_id=source_event_id,
                mode=Mode.ask_clarify,
                clarification_request=self._clarification_reason(turn_envelope),
            )
        return OrchestratorDecision(
            decision_id=str(uuid.uuid4()),
            session_id=session_id,
            source_event_id=source_event_id,
            mode=Mode.respond_now,
        )

    async def _decide_for_follow_up(
        self,
        turn_envelope: TurnEnvelope,
        *,
        follow_up_kind: str,
        pending_confirmation: PendingConfirmation | None,
        ctx: InvocationContext,
    ) -> OrchestratorDecision:
        follow_up_seed = self._follow_up_seed(turn_envelope.session_id, ctx, pending_confirmation)
        if follow_up_seed is None:
            return self._decide_base(
                turn_envelope,
                ctx,
                session_id=turn_envelope.session_id,
                source_event_id=turn_envelope.turn_id,
            )

        task_id = str(uuid.uuid4())
        if follow_up_kind == "correction" and follow_up_seed["active_task_id"]:
            self._cancel_superseded_task(
                ctx,
                follow_up_seed["active_task_id"],
                replacement_task_id=task_id,
            )

        if pending_confirmation is not None:
            self.state_store.pop_pending_confirmation(ctx, pending_confirmation.task_spec.task_id)
            self.state_store.set_current_mode(ctx, self._mode_without_confirmation(ctx))

        plan = await self._plan_follow_up_task(
            turn_envelope,
            follow_up_kind=follow_up_kind,
            base_request=str(follow_up_seed["base_request"]),
            base_task_kind=str(follow_up_seed["task_kind"]),
            resource_hints=list(follow_up_seed["resource_hints"]),
            workspace_context=str(follow_up_seed["workspace_context"]),
        )

        task_kind = plan.task_kind.strip() or str(follow_up_seed["task_kind"]) or "general"
        dedupe_key = (
            str(follow_up_seed["dedupe_key"])
            if follow_up_kind == "correction" and follow_up_seed["dedupe_key"]
            else self._dedupe_key(task_kind, plan.user_request)
        )
        task_spec = TaskSpec(
            task_id=task_id,
            task_kind=task_kind,
            goal=plan.user_request,
            input_payload={
                "user_request": plan.user_request,
                "job_type_hint": task_kind,
                "resource_hints": list(plan.resource_hints),
                "session_id": turn_envelope.session_id,
                "source_event_id": turn_envelope.turn_id,
                "follow_up_kind": follow_up_kind,
                "prior_task_id": str(follow_up_seed["prior_task_id"]),
            },
            dedupe_key=dedupe_key,
        )
        response_plan = ResponsePlan(
            text=plan.acknowledgment,
            tone=Tone.correction if follow_up_kind == "correction" else Tone.bridge,
            interruptible=True,
            priority=6,
            channel="voice",
        )
        return OrchestratorDecision(
            decision_id=str(uuid.uuid4()),
            session_id=turn_envelope.session_id,
            source_event_id=turn_envelope.turn_id,
            mode=Mode.respond_and_start_tasks,
            response_plan=response_plan,
            task_specs=[task_spec],
            supersedes_decision_id=self.state_store.get_last_decision_id(ctx),
        )

    def _decide_for_task_result(
        self,
        task_result: TaskResult,
        ctx: InvocationContext,
    ) -> OrchestratorDecision:
        if task_result.status == TaskStatus.completed:
            pending_confirmation = self._pending_confirmation_from_task_result(task_result)
            if pending_confirmation is not None:
                self.state_store.set_pending_confirmation(ctx, pending_confirmation)
                self.state_store.set_current_mode(ctx, SessionMode.awaiting_confirmation)
                response_plan = ResponsePlan(
                    text=self._build_confirmation_surface_text(task_result, pending_confirmation),
                    tone=Tone.clarification,
                    interruptible=True,
                    priority=8,
                    channel="voice",
                )
                return OrchestratorDecision(
                    decision_id=str(uuid.uuid4()),
                    session_id=task_result.session_id,
                    source_event_id=task_result.task_id,
                    mode=Mode.await_confirmation,
                    response_plan=response_plan,
                    surface_plan=SurfacePlan(
                        surface_mode=self._surface_mode_for_task_result(task_result),
                        response_plan=response_plan,
                        coalesce_with_task_ids=[task_result.task_id],
                    ),
                )

        response_text = self._build_task_result_surface_text(task_result)
        if not response_text or task_result.status == TaskStatus.cancelled:
            return OrchestratorDecision(
                decision_id=str(uuid.uuid4()),
                session_id=task_result.session_id,
                source_event_id=task_result.task_id,
                mode=Mode.noop,
                surface_plan=SurfacePlan(
                    surface_mode=SurfaceMode.silent_state_only,
                    response_plan=ResponsePlan(text="", tone=Tone.completion),
                    coalesce_with_task_ids=[task_result.task_id],
                ),
            )

        tone = Tone.correction if task_result.status == TaskStatus.failed else Tone.completion
        response_plan = ResponsePlan(
            text=response_text,
            tone=tone,
            interruptible=True,
            priority=8,
            channel="voice",
        )
        return OrchestratorDecision(
            decision_id=str(uuid.uuid4()),
            session_id=task_result.session_id,
            source_event_id=task_result.task_id,
            mode=Mode.respond_now,
            response_plan=response_plan,
            surface_plan=SurfacePlan(
                surface_mode=self._surface_mode_for_task_result(task_result),
                response_plan=response_plan,
                coalesce_with_task_ids=[task_result.task_id],
            ),
        )

    def _confirm_pending_task(
        self,
        turn_envelope: TurnEnvelope,
        pending_confirmation: PendingConfirmation,
        ctx: InvocationContext,
    ) -> OrchestratorDecision:
        self.state_store.pop_pending_confirmation(ctx, pending_confirmation.task_spec.task_id)
        confirmed_request = self._confirmed_action_user_request(
            pending_confirmation,
            turn_envelope.transcript,
        )
        payload = dict(pending_confirmation.task_spec.input_payload)
        payload["user_request"] = confirmed_request
        payload["confirmation_turn_id"] = turn_envelope.turn_id

        task_spec = pending_confirmation.task_spec.model_copy(
            update={
                "goal": confirmed_request,
                "input_payload": payload,
            }
        )
        return OrchestratorDecision(
            decision_id=str(uuid.uuid4()),
            session_id=turn_envelope.session_id,
            source_event_id=turn_envelope.turn_id,
            mode=Mode.respond_and_start_tasks,
            response_plan=ResponsePlan(
                text="Okay, I'll do that now.",
                tone=Tone.bridge,
                interruptible=True,
                priority=6,
                channel="voice",
            ),
            task_specs=[task_spec],
            supersedes_decision_id=self.state_store.get_last_decision_id(ctx),
        )

    def _reject_pending_task(
        self,
        turn_envelope: TurnEnvelope,
        pending_confirmation: PendingConfirmation,
        ctx: InvocationContext,
    ) -> OrchestratorDecision:
        self.state_store.pop_pending_confirmation(ctx, pending_confirmation.task_spec.task_id)
        self.state_store.set_current_mode(ctx, self._mode_without_confirmation(ctx))
        return OrchestratorDecision(
            decision_id=str(uuid.uuid4()),
            session_id=turn_envelope.session_id,
            source_event_id=turn_envelope.turn_id,
            mode=Mode.respond_now,
            response_plan=ResponsePlan(
                text="Okay, I won't do that.",
                tone=Tone.correction,
                interruptible=True,
                priority=6,
                channel="voice",
            ),
            supersedes_decision_id=self.state_store.get_last_decision_id(ctx),
        )

    def _task_specs_from_turn_envelope(self, turn_envelope: TurnEnvelope) -> list[TaskSpec]:
        inferred = self._infer_workspace_tool_args(turn_envelope.transcript)
        if inferred is None:
            return []
        spec = self.create_task_from_args(
            tool_args=inferred,
            session_id=turn_envelope.session_id,
            source_event_id=turn_envelope.turn_id,
        )
        return [spec] if spec is not None else []

    def create_task_from_args(
        self,
        *,
        tool_args: dict[str, Any],
        session_id: str,
        source_event_id: str,
        task_id: str = "",
    ) -> TaskSpec | None:
        user_request = str(tool_args.get("user_request") or "").strip()
        if not user_request:
            return None

        task_kind = str(tool_args.get("job_type_hint") or "general").strip() or "general"
        resource_hints = [str(item) for item in tool_args.get("resource_hints", [])]
        return TaskSpec(
            task_id=task_id or str(uuid.uuid4()),
            task_kind=task_kind,
            goal=user_request,
            input_payload={
                "user_request": user_request,
                "job_type_hint": task_kind,
                "resource_hints": resource_hints,
                "session_id": session_id,
                "source_event_id": source_event_id,
            },
            dedupe_key=self._dedupe_key(task_kind, user_request),
        )

    def _dedupe_key(self, task_kind: str, user_request: str) -> str:
        normalized_request = " ".join(user_request.lower().split())
        return f"{task_kind}:{normalized_request}"

    def _infer_workspace_tool_args(self, transcript: str) -> dict[str, Any] | None:
        text = " ".join(transcript.lower().split())
        if not text:
            return None

        resource_hints: list[str] = []
        task_kind = "general"

        gmail_terms = {"email", "emails", "gmail", "inbox", "mail", "thread", "threads", "message", "messages"}
        calendar_terms = {"calendar", "meeting", "meetings", "schedule", "event", "events", "invite", "availability"}
        slides_terms = {"slide", "slides", "presentation", "presentations", "deck", "decks", "speaker notes", "placeholder"}
        drive_terms = {"drive", "folder", "file", "files", "document", "documents", "doc", "docs", "spreadsheet", "sheet", "pdf"}
        netbox_terms = {
            # explicit tool references — include "net box" for voice-transcription variance
            "netbox", "net box", "nbx", "nb-cli",
            # device types — specific enough to mean network hardware
            "router", "routers", "firewall", "firewalls",
            "network switch", "network switches",
            # network objects — low collision risk
            "vlan", "vlans", "vrf", "vrfs",
            "subnet", "subnets", "ip prefix", "ip prefixes",
            "ip address", "ip addresses",
            # rack / site infra
            "rack", "racks", "network device", "network devices",
        }

        if any(term in text for term in gmail_terms):
            resource_hints.append("gmail")
            if any(verb in text for verb in {"send", "draft", "reply", "compose"}):
                task_kind = "action"
            elif any(verb in text for verb in {"read", "open"}):
                task_kind = "gmail_read"
            else:
                task_kind = "gmail_search"

        if any(term in text for term in calendar_terms):
            resource_hints.append("calendar")
            calendar_kind = (
                "calendar_write"
                if any(verb in text for verb in {"schedule", "add", "create", "move", "update", "cancel", "delete"})
                else "calendar_read"
            )
            task_kind = calendar_kind if task_kind == "general" else "general"

        if any(term in text for term in slides_terms):
            resource_hints.append("slides")
            slides_kind = (
                "slides_create"
                if any(
                    verb in text
                    for verb in {
                        "create",
                        "build",
                        "make",
                        "turn",
                        "convert",
                        "draft",
                        "edit",
                        "update",
                        "rewrite",
                        "revise",
                        "reorder",
                        "move",
                        "duplicate",
                        "delete",
                        "remove",
                        "insert",
                        "replace",
                        "resize",
                        "restyle",
                        "style",
                        "theme",
                        "fill",
                        "change",
                    }
                )
                else "slides_read"
            )
            task_kind = slides_kind if task_kind == "general" else "general"

        if any(term in text for term in drive_terms):
            resource_hints.append("drive")
            if any(verb in text for verb in {"create", "draft", "write"}):
                drive_kind = "doc_create"
            elif any(verb in text for verb in {"edit", "update", "rewrite"}):
                drive_kind = "doc_write"
            elif any(verb in text for verb in {"read", "open", "summarize"}):
                drive_kind = "doc_read"
            else:
                drive_kind = "drive_search"
            task_kind = drive_kind if task_kind == "general" else "general"

        if any(term in text for term in netbox_terms):
            resource_hints.append("netbox")
            netbox_kind = (
                "netbox_update"
                if any(verb in text for verb in {"update", "set", "change", "disable", "enable", "decommission"})
                else "netbox_query"
            )
            task_kind = netbox_kind if task_kind == "general" else "general"

        if not resource_hints:
            log.debug("_infer_workspace_tool_args no match text=%r", text)
            return None

        deduped_hints = list(dict.fromkeys(resource_hints))
        log.debug(
            "_infer_workspace_tool_args matched text=%r hints=%r kind=%r",
            text,
            deduped_hints,
            task_kind,
        )
        return {
            "user_request": transcript.strip(),
            "job_type_hint": task_kind,
            "resource_hints": deduped_hints,
        }

    def _should_clarify(self, turn_envelope: TurnEnvelope) -> bool:
        text = " ".join(turn_envelope.transcript.lower().split())
        if not text:
            return False

        ambiguous_starts = (
            "that",
            "this",
            "it",
            "them",
            "those",
            "these",
            "what about",
            "how about",
            "and then",
            "also",
            "instead",
        )
        if text.startswith(ambiguous_starts):
            return True

        vague_commands = {
            "do that",
            "do it",
            "use that",
            "use it",
            "send that",
            "send it",
            "open that",
            "open it",
        }
        if text in vague_commands:
            return True

        words = text.split()
        if len(words) <= 2 and text not in {"why", "how", "when", "where", "who", "what time is it"}:
            return True
        return False

    def _clarification_reason(self, turn_envelope: TurnEnvelope) -> str:
        text = " ".join(turn_envelope.transcript.lower().split())
        if text.startswith(("that", "this", "it", "them", "those", "these")):
            return "The user referred to prior context without naming the specific subject."
        return "The request is too underspecified to answer directly."

    def _surface_mode_for_task_result(self, task_result: TaskResult) -> SurfaceMode:
        raw = str(task_result.task_metadata.get("surface_mode") or "").strip()
        if raw:
            try:
                return SurfaceMode(raw)
            except ValueError:
                pass
        return SurfaceMode.wait_for_silence

    def _build_task_result_surface_text(self, task_result: TaskResult) -> str:
        if task_result.status == TaskStatus.failed:
            error_msg = task_result.error or "unknown error"
            return (
                "[Job result - status: failed]\n"
                "The background workspace job could not be completed.\n"
                f"Reason: {error_msg}\n\n"
                "Tell the user plainly what failed using the concrete reason above. "
                "Do not collapse it into a generic technical difficulty. "
                "If a resource was created but unusable, say that directly. "
                "Then suggest one clear next step."
            )

        sections: list[str] = []
        if task_result.summary:
            sections.append(f"Summary: {task_result.summary}")

        for artifact in task_result.artifacts:
            artifact_type = str(artifact.get("type") or "result")
            title = str(artifact.get("title") or "")
            content = str(artifact.get("content") or "")
            if not content:
                continue
            header = f"[{artifact_type}]" + (f" {title}" if title else "")
            sections.append(f"{header}\n{content}")

        if task_result.follow_up_questions:
            prompts = "\n".join(f"- {question}" for question in task_result.follow_up_questions)
            sections.append(f"Suggested follow-ups:\n{prompts}")

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

    async def _plan_follow_up_task(
        self,
        turn_envelope: TurnEnvelope,
        *,
        follow_up_kind: str,
        base_request: str,
        base_task_kind: str,
        resource_hints: list[str],
        workspace_context: str,
    ) -> FollowUpTaskPlan:
        correction_spoke = self.spokes.get("correction")
        if correction_spoke is not None:
            try:
                return await correction_spoke.invoke(
                    turn_envelope,
                    follow_up_kind=follow_up_kind,
                    base_request=base_request,
                    base_task_kind=base_task_kind,
                    resource_hints=resource_hints,
                    workspace_context=workspace_context,
                )
            except Exception:
                log.warning("Correction spoke failed; using deterministic follow-up fallback", exc_info=True)
        return self._fallback_follow_up_plan(
            turn_envelope,
            follow_up_kind=follow_up_kind,
            base_request=base_request,
            base_task_kind=base_task_kind,
            resource_hints=resource_hints,
        )

    def _fallback_follow_up_plan(
        self,
        turn_envelope: TurnEnvelope,
        *,
        follow_up_kind: str,
        base_request: str,
        base_task_kind: str,
        resource_hints: list[str],
    ) -> FollowUpTaskPlan:
        inferred = self._infer_workspace_tool_args(turn_envelope.transcript)
        fallback_task_kind = str(
            (inferred or {}).get("job_type_hint")
            or base_task_kind
            or "general"
        )
        fallback_hints = list(
            dict.fromkeys(
                list((inferred or {}).get("resource_hints") or [])
                or list(resource_hints)
            )
        )
        prefix = "Correction" if follow_up_kind == "correction" else "Follow-up"
        user_request = turn_envelope.transcript.strip()
        if base_request.strip():
            user_request = f"{base_request.strip()}\n\n{prefix}: {turn_envelope.transcript.strip()}"
        acknowledgment = (
            "Okay, I'll use that correction."
            if follow_up_kind == "correction"
            else "Okay, I'll keep going with that."
        )
        return FollowUpTaskPlan(
            user_request=user_request,
            acknowledgment=acknowledgment,
            task_kind=fallback_task_kind,
            resource_hints=fallback_hints,
        )

    def _follow_up_seed(
        self,
        session_id: str,
        ctx: InvocationContext,
        pending_confirmation: PendingConfirmation | None,
    ) -> dict[str, Any] | None:
        if pending_confirmation is not None:
            input_payload = dict(pending_confirmation.task_spec.input_payload)
            return {
                "base_request": pending_confirmation.task_spec.goal or pending_confirmation.action_preview,
                "task_kind": pending_confirmation.task_spec.task_kind or "action",
                "resource_hints": list(input_payload.get("resource_hints") or []),
                "dedupe_key": pending_confirmation.task_spec.dedupe_key,
                "active_task_id": "",
                "prior_task_id": pending_confirmation.source_task_id,
                "workspace_context": self._workspace_context(session_id, pending_confirmation.action_preview),
            }

        active_task = self._latest_active_task(ctx)
        if active_task is not None:
            input_payload = dict(active_task.input_payload)
            return {
                "base_request": active_task.goal,
                "task_kind": active_task.task_kind,
                "resource_hints": list(input_payload.get("resource_hints") or []),
                "dedupe_key": active_task.dedupe_key,
                "active_task_id": active_task.task_id,
                "prior_task_id": active_task.task_id,
                "workspace_context": self._workspace_context(session_id, active_task.goal),
            }

        workspace = self._workspace_snapshot(session_id)
        completed_task = self._latest_completed_task(ctx)
        if workspace is None and completed_task is None:
            return None

        return {
            "base_request": (
                workspace.user_request
                if workspace is not None
                else (completed_task.summary if completed_task is not None else "")
            ),
            "task_kind": self._completed_task_kind(completed_task),
            "resource_hints": self._completed_task_resource_hints(completed_task),
            "dedupe_key": None,
            "active_task_id": "",
            "prior_task_id": (
                workspace.job_id
                if workspace is not None
                else (completed_task.task_id if completed_task is not None else "")
            ),
            "workspace_context": self._workspace_context(
                session_id,
                workspace.user_request if workspace is not None else "",
            ),
        }

    def _pending_confirmation_from_task_result(
        self,
        task_result: TaskResult,
    ) -> PendingConfirmation | None:
        proposals = list(task_result.task_metadata.get("action_proposals") or [])
        if not proposals:
            return None

        preview = self._action_preview(proposals)
        if not preview:
            return None

        confirmed_task_id = str(uuid.uuid4())
        resource_hints = list(self._resource_hints_from_task_result(task_result))
        task_spec = TaskSpec(
            task_id=confirmed_task_id,
            task_kind="action",
            goal=self._confirmed_action_user_request_from_result(task_result, preview),
            input_payload={
                "user_request": self._confirmed_action_user_request_from_result(task_result, preview),
                "job_type_hint": "action",
                "resource_hints": resource_hints,
                "session_id": task_result.session_id,
                "source_event_id": task_result.task_id,
                "source_task_id": task_result.task_id,
                "action_proposals": proposals,
                "source_summary": task_result.summary,
            },
            dedupe_key=self._dedupe_key("action", f"{task_result.task_id}:{preview}"),
        )
        now = datetime.now(timezone.utc)
        return PendingConfirmation(
            confirmation_id=str(uuid.uuid4()),
            session_id=task_result.session_id,
            source_task_id=task_result.task_id,
            task_spec=task_spec,
            action_preview=preview,
            proposals=proposals,
            source_summary=task_result.summary,
            created_at=now,
            expires_at=now + timedelta(seconds=self.confirmation_ttl_seconds),
        )

    def _build_confirmation_surface_text(
        self,
        task_result: TaskResult,
        pending_confirmation: PendingConfirmation,
    ) -> str:
        summary = task_result.summary or pending_confirmation.source_summary or "A follow-up action is ready."
        proposal_lines = "\n".join(
            f"- {proposal.get('description', pending_confirmation.action_preview)}"
            for proposal in pending_confirmation.proposals
        )
        if not proposal_lines:
            proposal_lines = f"- {pending_confirmation.action_preview}"
        return (
            "[Background workspace job awaiting confirmation]\n\n"
            f"Summary: {summary}\n\n"
            "Pending action requiring confirmation:\n"
            f"{proposal_lines}\n\n"
            "Ask the user one short yes-or-no question about whether Athena should do that action now. "
            "Do not imply the action has already happened."
        )

    def _confirmed_action_user_request_from_result(
        self,
        task_result: TaskResult,
        action_preview: str,
    ) -> str:
        parts = [
            "Execute the pending action after the user confirms it.",
            f"Proposed action: {action_preview}",
        ]
        if task_result.summary:
            parts.append(f"Prior result summary: {task_result.summary}")
        return "\n".join(parts)

    def _confirmed_action_user_request(
        self,
        pending_confirmation: PendingConfirmation,
        user_confirmation: str,
    ) -> str:
        parts = [
            "Execute the previously proposed action that the user has now confirmed.",
            f"Confirmed action: {pending_confirmation.action_preview}",
            f"User confirmation: {user_confirmation.strip()}",
        ]
        if pending_confirmation.source_summary:
            parts.append(f"Prior result summary: {pending_confirmation.source_summary}")
        return "\n".join(parts)

    def _action_preview(self, proposals: list[dict[str, Any]]) -> str:
        descriptions = [
            str(proposal.get("description") or "").strip()
            for proposal in proposals
            if str(proposal.get("description") or "").strip()
        ]
        return "; ".join(descriptions)

    def _resource_hints_from_task_result(self, task_result: TaskResult) -> list[str]:
        hints: list[str] = []
        raw_handles = list(task_result.task_metadata.get("resource_handles_raw") or [])
        for handle in raw_handles:
            source = str(handle.get("source") or "").strip()
            if source:
                hints.append(source)
        return list(dict.fromkeys(hints))

    def _classify_follow_up_turn(
        self,
        turn_envelope: TurnEnvelope | None,
        ctx: InvocationContext,
        pending_confirmation: PendingConfirmation | None,
    ) -> str | None:
        if turn_envelope is None or not turn_envelope.transcript.strip():
            return None
        if not self._has_follow_up_context(turn_envelope.session_id, ctx, pending_confirmation):
            return None

        normalized_text = self._normalize_text(turn_envelope.transcript)
        if self._is_correction_turn(normalized_text):
            return "correction"
        if self._is_continuation_turn(normalized_text):
            return "continuation"
        return None

    def _has_follow_up_context(
        self,
        session_id: str,
        ctx: InvocationContext,
        pending_confirmation: PendingConfirmation | None,
    ) -> bool:
        return any(
            (
                pending_confirmation is not None,
                self._latest_active_task(ctx) is not None,
                self._latest_completed_task(ctx) is not None,
                self._workspace_snapshot(session_id) is not None,
            )
        )

    def _is_correction_turn(self, text: str) -> bool:
        if not text or text in {"no", "nope", "nah"}:
            return False
        correction_prefixes = (
            "actually",
            "instead",
            "sorry",
            "wait",
            "correction",
            "make that",
            "change that",
            "change it",
            "rather",
            "on second thought",
        )
        if text.startswith(correction_prefixes):
            return True
        return text.startswith("no ") or text.startswith("no,")

    def _is_continuation_turn(self, text: str) -> bool:
        if not text:
            return False
        continuation_prefixes = (
            "also",
            "and ",
            "then",
            "next",
            "after that",
            "using that",
            "based on that",
            "with that",
            "turn that",
            "turn it",
            "send that",
            "send it",
            "open that",
            "open it",
            "share that",
            "share it",
            "add that",
            "add it",
            "put that",
            "put it",
        )
        if text.startswith(continuation_prefixes):
            return True

        action_verbs = {"send", "open", "share", "add", "put", "turn", "make", "draft", "reply", "update", "create"}
        pronouns = {"that", "it", "them", "those", "these"}
        words = set(text.split())
        return bool(action_verbs & words and pronouns & words)

    def _is_confirmation_acceptance(self, text: str) -> bool:
        if not text:
            return False
        accepted_phrases = (
            "yes",
            "yes please",
            "yeah",
            "yep",
            "sure",
            "sure please",
            "go ahead",
            "do it",
            "do that",
            "send it",
            "send that",
            "confirm",
            "approved",
            "sounds good",
            "ok do it",
            "okay do it",
            "please do",
        )
        return any(text == phrase or text.startswith(f"{phrase} ") for phrase in accepted_phrases)

    def _is_confirmation_rejection(self, text: str) -> bool:
        if not text:
            return False
        rejected_phrases = (
            "no",
            "no thanks",
            "no thank you",
            "don't",
            "do not",
            "cancel that",
            "never mind",
            "not now",
            "stop",
        )
        return any(text == phrase or text.startswith(f"{phrase} ") for phrase in rejected_phrases)

    def _current_pending_confirmation(
        self,
        ctx: InvocationContext,
    ) -> PendingConfirmation | None:
        pending_confirmation = self.state_store.get_latest_pending_confirmation(ctx)
        if pending_confirmation is None:
            return None

        if (
            pending_confirmation.expires_at is not None
            and pending_confirmation.expires_at < datetime.now(timezone.utc)
        ):
            self.state_store.pop_pending_confirmation(ctx, pending_confirmation.task_spec.task_id)
            self.state_store.set_current_mode(ctx, self._mode_without_confirmation(ctx))
            return None
        return pending_confirmation

    def _mode_without_confirmation(self, ctx: InvocationContext) -> SessionMode:
        return (
            SessionMode.tasks_running
            if self.state_store.get_active_task_ids(ctx)
            else SessionMode.idle
        )

    def _cancel_superseded_task(
        self,
        ctx: InvocationContext,
        task_id: str,
        *,
        replacement_task_id: str,
    ) -> None:
        if self.task_manager is None:
            return
        cancel_task = getattr(self.task_manager, "cancel_task", None)
        if callable(cancel_task):
            cancel_task(
                ctx,
                task_id,
                reason="Superseded by a user correction.",
                replacement_task_id=replacement_task_id,
            )

    def _latest_active_task(self, ctx: InvocationContext) -> TaskSpec | None:
        if self.task_manager is None:
            return None
        getter = getattr(self.task_manager, "get_latest_active_task", None)
        if callable(getter):
            return getter(ctx)
        return None

    def _latest_completed_task(self, ctx: InvocationContext) -> TaskResult | None:
        if self.task_manager is None:
            return None
        getter = getattr(self.task_manager, "get_latest_completed_task", None)
        if callable(getter):
            return getter(ctx)
        return None

    def _completed_task_kind(self, completed_task: TaskResult | None) -> str:
        if completed_task is None:
            return "general"
        return str(completed_task.task_metadata.get("job_type_hint") or "general")

    def _completed_task_resource_hints(self, completed_task: TaskResult | None) -> list[str]:
        if completed_task is None:
            return []
        raw = list(completed_task.task_metadata.get("resource_hints") or [])
        return [str(item) for item in raw if str(item).strip()]

    def _workspace_snapshot(self, session_id: str) -> Any | None:
        if self.workspace_store is None or not session_id:
            return None
        getter = getattr(self.workspace_store, "get_workspace", None)
        if callable(getter):
            return getter(session_id)
        return None

    def _workspace_context(self, session_id: str, query: str) -> str:
        if self.workspace_store is None or not session_id:
            return ""
        renderer = getattr(self.workspace_store, "render_context", None)
        if callable(renderer):
            return str(renderer(session_id, query=query, limit=1, max_chars=800) or "")
        return ""

    def _normalize_text(self, text: str) -> str:
        normalized = " ".join(text.lower().split())
        for char in ",.!?":
            normalized = normalized.replace(char, "")
        return normalized
