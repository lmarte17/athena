from types import SimpleNamespace

import pytest

import app.tracing as tracing
from app.routes import system as system_routes


def _reset_tracing_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_BOOTSTRAPPED", False)
    monkeypatch.setattr(tracing, "_TRACE_STATUS", None)


def test_bootstrap_tracing_disabled_without_langsmith_api_key(monkeypatch):
    _reset_tracing_state(monkeypatch)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING_V2", raising=False)
    monkeypatch.delenv("ATHENA_TRACE_ENV", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)

    status = tracing.bootstrap_tracing()

    assert status.enabled is False
    assert status.project_name == "athena-local"
    assert status.environment == "local"
    assert status.disabled_reason == "LANGSMITH_API_KEY not set"


def test_bootstrap_tracing_configures_google_adk_with_cloudrun_defaults(monkeypatch):
    _reset_tracing_state(monkeypatch)
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://langsmith.example")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING_V2", raising=False)
    monkeypatch.delenv("ATHENA_TRACE_ENV", raising=False)
    monkeypatch.setenv("K_SERVICE", "athena-backend")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    monkeypatch.setenv("ATHENA_TRACE_CAPTURE_THOUGHTS", "FALSE")

    calls: list[dict] = []

    def fake_configure_google_adk(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(tracing, "configure_google_adk", fake_configure_google_adk)

    status = tracing.bootstrap_tracing()

    assert status.enabled is True
    assert status.project_name == "athena-cloudrun"
    assert status.environment == "cloudrun"
    assert status.endpoint == "https://langsmith.example"
    assert status.thought_capture_enabled is False
    assert calls == [
        {
            "name": "athena.adk",
            "project_name": "athena-cloudrun",
            "metadata": tracing.base_metadata(component="google_adk"),
            "tags": tracing.base_tags("google-adk"),
        }
    ]
    assert tracing.os.environ["LANGSMITH_PROJECT"] == "athena-cloudrun"
    assert tracing.os.environ["LANGSMITH_TRACING"] == "true"
    assert tracing.os.environ["LANGSMITH_TRACING_V2"] == "true"


@pytest.mark.asyncio
async def test_debug_route_reports_tracing_status(monkeypatch):
    class _FakeDrainTask:
        def done(self) -> bool:
            return False

    monkeypatch.setattr(
        system_routes,
        "session_manager",
        SimpleNamespace(
            _drain_task=_FakeDrainTask(),
            _tap_queue=SimpleNamespace(qsize=lambda: 3),
            _subscribers=["a", "b"],
        ),
    )
    monkeypatch.setattr(
        system_routes,
        "tracing_status",
        lambda: tracing.TracingStatus(
            enabled=True,
            provider="langsmith",
            project_name="athena-local",
            environment="local",
            thought_capture_enabled=True,
            endpoint=None,
            disabled_reason=None,
        ),
    )

    payload = await system_routes.debug()

    assert payload["startup_ran"] is True
    assert payload["drain_task_running"] is True
    assert payload["tap_queue_size"] == 3
    assert payload["subscriber_count"] == 2
    assert payload["tracing"] == {
        "enabled": True,
        "provider": "langsmith",
        "project_name": "athena-local",
        "environment": "local",
        "thought_capture_enabled": True,
        "endpoint": None,
        "disabled_reason": None,
    }
