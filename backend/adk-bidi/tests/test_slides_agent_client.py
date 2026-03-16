from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.slides_agent_client import (
    SlidesAgentAuthError,
    SlidesAgentInvocationError,
    _run_slides_agent_command,
    run_slides_agent_json,
    slides_agent_binary,
)


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


def _install_span_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from app import slides_agent_client

    spans: list[dict] = []

    @asynccontextmanager
    async def fake_atrace_span(name: str, **kwargs):
        run = {"name": name, **kwargs}
        spans.append(run)
        yield run

    def fake_finish_span(run, *, outputs=None, error=None):
        run["outputs"] = outputs
        run["error"] = error

    monkeypatch.setattr(slides_agent_client, "atrace_span", fake_atrace_span)
    monkeypatch.setattr(slides_agent_client, "finish_span", fake_finish_span)
    return spans


def test_slides_agent_binary_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("ATHENA_SLIDES_AGENT_BINARY", "/tmp/custom-slides-agent")
    assert slides_agent_binary() == "/tmp/custom-slides-agent"


@pytest.mark.asyncio
async def test_run_slides_agent_command_records_success_span(monkeypatch):
    from app import slides_agent_client

    spans = _install_span_recorder(monkeypatch)
    process = _FakeProcess(returncode=0, stdout=b'{"ok": true}\n')

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(
        slides_agent_client.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    stdout, stderr = await _run_slides_agent_command(["slides-agent", "deck", "inspect"])

    assert stdout == '{"ok": true}'
    assert stderr == ""
    assert spans[0]["name"] == "athena.slides_agent.command"
    assert spans[0]["inputs"]["command"] == ["slides-agent", "deck", "inspect"]
    assert spans[0]["outputs"]["returncode"] == 0
    assert spans[0]["error"] is None


@pytest.mark.asyncio
async def test_run_slides_agent_command_raises_auth_error_from_structured_payload(monkeypatch):
    from app import slides_agent_client

    spans = _install_span_recorder(monkeypatch)
    process = _FakeProcess(
        returncode=1,
        stdout=(
            b'{"ok": false, "error_code": "auth_error", '
            b'"detail": "No valid credentials found."}'
        ),
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(
        slides_agent_client.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(SlidesAgentAuthError, match="No valid credentials found."):
        await _run_slides_agent_command(["slides-agent", "deck", "inspect"])

    assert spans[0]["outputs"]["returncode"] == 1
    assert spans[0]["error"] == "No valid credentials found."


@pytest.mark.asyncio
async def test_run_slides_agent_command_raises_non_auth_error_and_records_span(monkeypatch):
    from app import slides_agent_client

    spans = _install_span_recorder(monkeypatch)
    process = _FakeProcess(returncode=2, stderr=b"permission denied")

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(
        slides_agent_client.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(SlidesAgentInvocationError):
        await _run_slides_agent_command(["slides-agent", "theme", "apply"])

    assert spans[0]["outputs"]["returncode"] == 2
    assert spans[0]["error"] == "permission denied"


@pytest.mark.asyncio
async def test_run_slides_agent_command_records_timeout_span(monkeypatch):
    from app import slides_agent_client

    spans = _install_span_recorder(monkeypatch)
    process = _FakeProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return process

    async def fake_wait_for(awaitable, timeout):
        del timeout
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        slides_agent_client.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(slides_agent_client.asyncio, "wait_for", fake_wait_for)

    with pytest.raises(SlidesAgentInvocationError, match="slides-agent timed out"):
        await _run_slides_agent_command(["slides-agent", "deck", "inspect"])

    assert process.killed is True
    assert spans[0]["error"] == "slides-agent timed out"


@pytest.mark.asyncio
async def test_run_slides_agent_json_rejects_invalid_json(monkeypatch):
    from app import slides_agent_client

    async def fake_run(*command):
        del command
        return "not-json", ""

    monkeypatch.setattr(slides_agent_client, "_run_slides_agent_command", fake_run)

    with pytest.raises(SlidesAgentInvocationError, match="invalid JSON"):
        await run_slides_agent_json("deck", "inspect", "--presentation-id", "deck-1")
