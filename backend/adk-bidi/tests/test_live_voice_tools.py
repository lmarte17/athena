from __future__ import annotations

from app.adk_agents.live_voice import build_live_voice_agent


def test_live_voice_agent_has_no_workspace_tools():
    agent = build_live_voice_agent()

    assert agent.tools == []
