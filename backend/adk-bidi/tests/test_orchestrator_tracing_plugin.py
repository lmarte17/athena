from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.orchestrator.tracing_plugin import (
    TURN_ID_STATE_KEY,
    TracingPlugin,
    consume_turn_id,
    ensure_turn_id,
)


def test_ensure_turn_id_reuses_existing_value():
    state = {TURN_ID_STATE_KEY: "turn-1"}

    assert ensure_turn_id(state) == "turn-1"


def test_consume_turn_id_clears_completed_turn():
    state = {TURN_ID_STATE_KEY: "turn-2"}

    assert consume_turn_id(state) == "turn-2"
    assert TURN_ID_STATE_KEY not in state


@pytest.mark.asyncio
async def test_tracing_plugin_logs_agent_lifecycle(caplog):
    plugin = TracingPlugin()
    callback_context = SimpleNamespace(
        session=SimpleNamespace(id="session-1"),
        state={},
    )
    agent = SimpleNamespace(name="ConversationOrchestrator")

    with caplog.at_level(logging.INFO, logger="athena.orchestrator.tracing_plugin"):
        await plugin.before_agent_callback(agent=agent, callback_context=callback_context)
        await plugin.after_agent_callback(agent=agent, callback_context=callback_context)

    assert "agent_start agent=ConversationOrchestrator session_id=session-1" in caplog.text
    assert "agent_complete agent=ConversationOrchestrator session_id=session-1" in caplog.text
