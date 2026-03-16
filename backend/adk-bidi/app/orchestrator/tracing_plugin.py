"""ADK tracing plugin and ID helpers for the orchestrator runtime."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

log = logging.getLogger("athena.orchestrator.tracing_plugin")

TURN_ID_STATE_KEY = "temp:turn_id"
AUDITED_LIVE_TOOL_NAMES = frozenset()


def ensure_turn_id(state: Any | None) -> str:
    """Return the active turn id, creating one when the current turn has none."""
    if state is None:
        return str(uuid.uuid4())

    try:
        existing = str(state.get(TURN_ID_STATE_KEY) or "").strip()
    except Exception:
        existing = ""
    if existing:
        return existing

    turn_id = str(uuid.uuid4())
    try:
        state[TURN_ID_STATE_KEY] = turn_id
    except Exception:
        pass
    return turn_id


def consume_turn_id(state: Any | None) -> str:
    """Return and clear the active turn id once a live turn has completed."""
    if state is None:
        return str(uuid.uuid4())

    turn_id = ""
    try:
        turn_id = str(state.pop(TURN_ID_STATE_KEY, "") or "").strip()
    except Exception:
        turn_id = str(getattr(state, "get", lambda *_args, **_kwargs: "")(TURN_ID_STATE_KEY) or "").strip()
    return turn_id or str(uuid.uuid4())


def _state_value(state: Any | None, key: str) -> str:
    if state is None:
        return ""
    try:
        value = state.get(key)
    except Exception:
        return ""
    return str(value or "")


class TracingPlugin(BasePlugin):
    """Cross-cutting ADK trace logging for the orchestrator runtime."""

    def __init__(self) -> None:
        super().__init__(name="athena_tracing")

    async def on_user_message_callback(self, *, invocation_context, user_message):
        del user_message
        ensure_turn_id(getattr(invocation_context.session, "state", None))
        return None

    async def before_agent_callback(self, *, agent, callback_context):
        turn_id = ensure_turn_id(getattr(callback_context, "state", None))
        log.info(
            "agent_start agent=%s session_id=%s turn_id=%s decision_id=%s",
            getattr(agent, "name", agent.__class__.__name__),
            getattr(getattr(callback_context, "session", None), "id", ""),
            turn_id,
            _state_value(getattr(callback_context, "state", None), "user:last_orchestrator_decision_id"),
        )
        return None

    async def after_agent_callback(self, *, agent, callback_context):
        log.info(
            "agent_complete agent=%s session_id=%s turn_id=%s decision_id=%s",
            getattr(agent, "name", agent.__class__.__name__),
            getattr(getattr(callback_context, "session", None), "id", ""),
            _state_value(getattr(callback_context, "state", None), TURN_ID_STATE_KEY),
            _state_value(getattr(callback_context, "state", None), "user:last_orchestrator_decision_id"),
        )
        return None

    async def before_model_callback(self, *, callback_context, llm_request):
        log.info(
            "model_start session_id=%s turn_id=%s model=%s",
            getattr(getattr(callback_context, "session", None), "id", ""),
            ensure_turn_id(getattr(callback_context, "state", None)),
            getattr(llm_request, "model", None),
        )
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        log.info(
            "model_complete session_id=%s turn_id=%s finish_reason=%s",
            getattr(getattr(callback_context, "session", None), "id", ""),
            _state_value(getattr(callback_context, "state", None), TURN_ID_STATE_KEY),
            getattr(llm_response, "finish_reason", None),
        )
        return None
