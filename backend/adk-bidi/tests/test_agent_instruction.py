from types import SimpleNamespace

from google.adk.models.llm_request import LlmRequest

from app.adk_agents.live_voice import _before_model_callback, _build_instruction


def test_build_instruction_wraps_context_with_operating_rules(monkeypatch):
    monkeypatch.setattr("app.context_builder.get_context", lambda _session_id: "Memory bundle")

    instruction = _build_instruction(SimpleNamespace(session=SimpleNamespace(id="session-1")))

    assert "## Role" in instruction
    assert "Do not decide when to start backend work" in instruction
    assert "[Direct response plan]" in instruction
    assert "Memory bundle" in instruction


def test_build_instruction_includes_current_mode_and_active_tasks(monkeypatch):
    monkeypatch.setattr("app.context_builder.get_context", lambda _session_id: "Memory bundle")

    instruction = _build_instruction(
        SimpleNamespace(
            session=SimpleNamespace(id="session-1"),
            state={
                "user:current_mode": "tasks_running",
                "user:active_tasks": {
                    "task-1": {"task_kind": "gmail_search"},
                },
            },
        )
    )

    assert "Do not decide when to start backend work" in instruction
    assert "Current mode: tasks_running" in instruction
    assert "Active tasks: 1 (gmail_search)" in instruction
    assert "Memory bundle" in instruction


def test_before_model_callback_injects_current_instruction(monkeypatch):
    monkeypatch.setattr("app.context_builder.get_context", lambda _session_id: "Memory bundle")

    llm_request = LlmRequest()
    callback_context = SimpleNamespace(
        session=SimpleNamespace(id="session-1"),
        state={
            "user:current_mode": "tasks_running",
            "user:active_tasks": {
                "task-1": {"task_kind": "gmail_search"},
                "task-2": {"task_kind": "calendar_read"},
            },
        },
    )

    result = _before_model_callback(callback_context, llm_request)

    assert result is None
    assert "Current mode: tasks_running" in llm_request.config.system_instruction
    assert "Active tasks: 2 (gmail_search, calendar_read)" in llm_request.config.system_instruction
    assert "Memory bundle" in llm_request.config.system_instruction
