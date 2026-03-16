"""Thin wrapper around ADK session state for orchestrator-owned keys."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.orchestrator.contracts import (
    OrchestratorDecision,
    PendingConfirmation,
    TaskResult,
    TaskSpec,
    TurnEnvelope,
)

CURRENT_MODE_STATE_KEY = "user:current_mode"
LAST_DECISION_ID_STATE_KEY = "user:last_orchestrator_decision_id"
LAST_USER_TURN_ID_STATE_KEY = "user:last_user_turn_id"
ACTIVE_TASKS_STATE_KEY = "user:active_tasks"
COMPLETED_TASKS_STATE_KEY = "user:completed_tasks"
PENDING_CONFIRMATIONS_STATE_KEY = "user:pending_confirmations"
SUPPRESSED_TASK_RESULTS_STATE_KEY = "user:suppressed_task_results"
TURN_ENVELOPE_STATE_KEY = "temp:turn_envelope"
DECISION_STATE_KEY = "temp:decision"
PENDING_TASK_SPECS_STATE_KEY = "temp:pending_task_specs"
TASK_RESULT_STATE_KEY = "temp:task_result"
ASSISTANT_ONLY_TURN_SKIP_COUNT_STATE_KEY = "temp:skip_orchestrator_assistant_only_turns"


class SessionMode(str, Enum):
    idle = "idle"
    listening = "listening"
    responding = "responding"
    tasks_running = "tasks_running"
    awaiting_confirmation = "awaiting_confirmation"


@dataclass(slots=True)
class StateStore:
    """Typed accessors over ADK session state.

    The wrapper accepts either a session object (`session.state`) or an
    invocation context (`ctx.session.state`) so it can be used from both the
    websocket ingress path and the orchestrator agent.
    """

    def get_current_mode(self, session_or_ctx: Any) -> SessionMode:
        raw = self._state(session_or_ctx).get(CURRENT_MODE_STATE_KEY, SessionMode.idle.value)
        try:
            return SessionMode(str(raw))
        except ValueError:
            return SessionMode.idle

    def set_current_mode(self, session_or_ctx: Any, mode: SessionMode) -> None:
        self._state(session_or_ctx)[CURRENT_MODE_STATE_KEY] = mode.value

    def get_turn_envelope(self, session_or_ctx: Any) -> TurnEnvelope | None:
        raw = self._state(session_or_ctx).get(TURN_ENVELOPE_STATE_KEY)
        if not raw:
            return None
        return TurnEnvelope.model_validate(raw)

    def set_turn_envelope(self, session_or_ctx: Any, envelope: TurnEnvelope) -> None:
        state = self._state(session_or_ctx)
        state[TURN_ENVELOPE_STATE_KEY] = envelope.model_dump(mode="json")
        state[LAST_USER_TURN_ID_STATE_KEY] = envelope.turn_id

    def clear_turn_envelope(self, session_or_ctx: Any) -> None:
        self._state(session_or_ctx).pop(TURN_ENVELOPE_STATE_KEY, None)

    def get_decision(self, session_or_ctx: Any) -> OrchestratorDecision | None:
        raw = self._state(session_or_ctx).get(DECISION_STATE_KEY)
        if not raw:
            return None
        return OrchestratorDecision.model_validate(raw)

    def set_decision(self, session_or_ctx: Any, decision: OrchestratorDecision) -> None:
        state = self._state(session_or_ctx)
        state[DECISION_STATE_KEY] = decision.model_dump(mode="json")
        state[LAST_DECISION_ID_STATE_KEY] = decision.decision_id

    def clear_decision(self, session_or_ctx: Any) -> None:
        self._state(session_or_ctx).pop(DECISION_STATE_KEY, None)

    def get_last_decision_id(self, session_or_ctx: Any) -> str | None:
        raw = self._state(session_or_ctx).get(LAST_DECISION_ID_STATE_KEY)
        return str(raw) if raw else None

    def get_pending_task_specs(self, session_or_ctx: Any) -> list[TaskSpec]:
        raw = self._state(session_or_ctx).get(PENDING_TASK_SPECS_STATE_KEY, [])
        if not isinstance(raw, list):
            return []
        specs: list[TaskSpec] = []
        for item in raw:
            try:
                specs.append(TaskSpec.model_validate(item))
            except Exception:
                continue
        return specs

    def set_pending_task_specs(self, session_or_ctx: Any, specs: list[TaskSpec]) -> None:
        self._state(session_or_ctx)[PENDING_TASK_SPECS_STATE_KEY] = [
            spec.model_dump(mode="json") for spec in specs
        ]

    def clear_pending_task_specs(self, session_or_ctx: Any) -> None:
        self._state(session_or_ctx).pop(PENDING_TASK_SPECS_STATE_KEY, None)

    def get_task_result(self, session_or_ctx: Any) -> TaskResult | None:
        raw = self._state(session_or_ctx).get(TASK_RESULT_STATE_KEY)
        if not raw:
            return None
        return TaskResult.model_validate(raw)

    def set_task_result(self, session_or_ctx: Any, result: TaskResult) -> None:
        self._state(session_or_ctx)[TASK_RESULT_STATE_KEY] = result.model_dump(mode="json")

    def clear_task_result(self, session_or_ctx: Any) -> None:
        self._state(session_or_ctx).pop(TASK_RESULT_STATE_KEY, None)

    def get_active_tasks(self, session_or_ctx: Any) -> dict[str, TaskSpec]:
        raw = self._state(session_or_ctx).get(ACTIVE_TASKS_STATE_KEY, {})
        if not isinstance(raw, dict):
            return {}
        tasks: dict[str, TaskSpec] = {}
        for task_id, payload in raw.items():
            try:
                tasks[str(task_id)] = TaskSpec.model_validate(payload)
            except Exception:
                continue
        return tasks

    def get_active_task_ids(self, session_or_ctx: Any) -> list[str]:
        return list(self.get_active_tasks(session_or_ctx).keys())

    def get_completed_tasks(self, session_or_ctx: Any) -> list[TaskResult]:
        raw = self._state(session_or_ctx).get(COMPLETED_TASKS_STATE_KEY, [])
        if not isinstance(raw, list):
            return []
        results: list[TaskResult] = []
        for item in raw:
            try:
                results.append(TaskResult.model_validate(item))
            except Exception:
                continue
        return results

    def get_pending_confirmations(self, session_or_ctx: Any) -> dict[str, PendingConfirmation]:
        raw = self._state(session_or_ctx).get(PENDING_CONFIRMATIONS_STATE_KEY, {})
        if not isinstance(raw, dict):
            return {}

        confirmations: dict[str, PendingConfirmation] = {}
        for task_id, payload in raw.items():
            try:
                confirmations[str(task_id)] = PendingConfirmation.model_validate(payload)
            except Exception:
                continue
        return confirmations

    def get_latest_pending_confirmation(
        self,
        session_or_ctx: Any,
    ) -> PendingConfirmation | None:
        confirmations = list(self.get_pending_confirmations(session_or_ctx).values())
        if not confirmations:
            return None
        return max(
            confirmations,
            key=lambda item: (
                item.created_at.timestamp(),
                item.expires_at.timestamp() if item.expires_at is not None else float("-inf"),
            ),
        )

    def set_pending_confirmation(
        self,
        session_or_ctx: Any,
        confirmation: PendingConfirmation,
    ) -> None:
        state = self._state(session_or_ctx)
        raw = state.get(PENDING_CONFIRMATIONS_STATE_KEY, {})
        confirmations = dict(raw) if isinstance(raw, dict) else {}
        confirmations[confirmation.task_spec.task_id] = confirmation.model_dump(mode="json")
        state[PENDING_CONFIRMATIONS_STATE_KEY] = confirmations

    def pop_pending_confirmation(
        self,
        session_or_ctx: Any,
        task_id: str,
    ) -> PendingConfirmation | None:
        state = self._state(session_or_ctx)
        raw = state.get(PENDING_CONFIRMATIONS_STATE_KEY, {})
        if not isinstance(raw, dict):
            return None

        confirmations = dict(raw)
        payload = confirmations.pop(task_id, None)
        if confirmations:
            state[PENDING_CONFIRMATIONS_STATE_KEY] = confirmations
        else:
            state.pop(PENDING_CONFIRMATIONS_STATE_KEY, None)
        if payload is None:
            return None
        try:
            return PendingConfirmation.model_validate(payload)
        except Exception:
            return None

    def clear_pending_confirmations(self, session_or_ctx: Any) -> None:
        self._state(session_or_ctx).pop(PENDING_CONFIRMATIONS_STATE_KEY, None)

    def mark_task_result_suppressed(
        self,
        session_or_ctx: Any,
        task_id: str,
        *,
        replacement_task_id: str = "",
        reason: str = "",
    ) -> None:
        state = self._state(session_or_ctx)
        raw = state.get(SUPPRESSED_TASK_RESULTS_STATE_KEY, {})
        suppressed = dict(raw) if isinstance(raw, dict) else {}
        suppressed[task_id] = {
            "replacement_task_id": replacement_task_id,
            "reason": reason,
        }
        state[SUPPRESSED_TASK_RESULTS_STATE_KEY] = suppressed

    def consume_suppressed_task_result(
        self,
        session_or_ctx: Any,
        task_id: str,
    ) -> dict[str, Any] | None:
        state = self._state(session_or_ctx)
        raw = state.get(SUPPRESSED_TASK_RESULTS_STATE_KEY, {})
        if not isinstance(raw, dict):
            return None

        suppressed = dict(raw)
        payload = suppressed.pop(task_id, None)
        if suppressed:
            state[SUPPRESSED_TASK_RESULTS_STATE_KEY] = suppressed
        else:
            state.pop(SUPPRESSED_TASK_RESULTS_STATE_KEY, None)
        return payload if isinstance(payload, dict) else None

    def increment_assistant_only_turn_skip(
        self,
        session_or_ctx: Any,
        *,
        count: int = 1,
    ) -> None:
        state = self._state(session_or_ctx)
        current = int(state.get(ASSISTANT_ONLY_TURN_SKIP_COUNT_STATE_KEY, 0) or 0)
        state[ASSISTANT_ONLY_TURN_SKIP_COUNT_STATE_KEY] = max(0, current + count)

    def consume_assistant_only_turn_skip(self, session_or_ctx: Any) -> bool:
        state = self._state(session_or_ctx)
        current = int(state.get(ASSISTANT_ONLY_TURN_SKIP_COUNT_STATE_KEY, 0) or 0)
        if current <= 0:
            return False
        if current == 1:
            state.pop(ASSISTANT_ONLY_TURN_SKIP_COUNT_STATE_KEY, None)
        else:
            state[ASSISTANT_ONLY_TURN_SKIP_COUNT_STATE_KEY] = current - 1
        return True

    def _state(self, session_or_ctx: Any) -> Any:
        if hasattr(session_or_ctx, "session") and hasattr(session_or_ctx.session, "state"):
            return session_or_ctx.session.state
        if hasattr(session_or_ctx, "state"):
            return session_or_ctx.state
        raise TypeError("session_or_ctx must expose .state or .session.state")
