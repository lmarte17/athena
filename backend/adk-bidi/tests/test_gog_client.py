from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.gog_client import GogAuthError, GogInvocationError, _run_gog_command


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
    from app import gog_client

    spans: list[dict] = []

    @asynccontextmanager
    async def fake_atrace_span(name: str, **kwargs):
        run = {"name": name, **kwargs}
        spans.append(run)
        yield run

    def fake_finish_span(run, *, outputs=None, error=None):
        run["outputs"] = outputs
        run["error"] = error

    monkeypatch.setattr(gog_client, "atrace_span", fake_atrace_span)
    monkeypatch.setattr(gog_client, "finish_span", fake_finish_span)
    return spans


@pytest.mark.asyncio
async def test_run_gog_command_records_success_span(monkeypatch):
    from app import gog_client

    spans = _install_span_recorder(monkeypatch)
    process = _FakeProcess(returncode=0, stdout=b'{"ok": true}\n')

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(gog_client.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    stdout, stderr = await _run_gog_command(["gog", "docs", "list"])

    assert stdout == '{"ok": true}'
    assert stderr == ""
    assert spans[0]["name"] == "athena.gog.command"
    assert spans[0]["inputs"]["command"] == ["gog", "docs", "list"]
    assert spans[0]["outputs"]["returncode"] == 0
    assert spans[0]["error"] is None


@pytest.mark.asyncio
async def test_run_gog_command_raises_auth_error_and_records_span(monkeypatch):
    from app import gog_client

    spans = _install_span_recorder(monkeypatch)
    process = _FakeProcess(returncode=1, stderr=b"not authenticated")

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(gog_client.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(GogAuthError):
        await _run_gog_command(["gog", "gmail", "list"])

    assert spans[0]["outputs"]["returncode"] == 1
    assert spans[0]["error"] == "not authenticated"


@pytest.mark.asyncio
async def test_run_gog_command_raises_non_auth_error_and_records_span(monkeypatch):
    from app import gog_client

    spans = _install_span_recorder(monkeypatch)
    process = _FakeProcess(returncode=2, stderr=b"permission denied")

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(gog_client.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(GogInvocationError):
        await _run_gog_command(["gog", "drive", "search"])

    assert spans[0]["outputs"]["returncode"] == 2
    assert spans[0]["error"] == "permission denied"


@pytest.mark.asyncio
async def test_run_gog_command_records_timeout_span(monkeypatch):
    from app import gog_client

    spans = _install_span_recorder(monkeypatch)
    process = _FakeProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    async def fake_wait_for(awaitable, timeout):
        del timeout
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(gog_client.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(gog_client.asyncio, "wait_for", fake_wait_for)

    with pytest.raises(GogInvocationError, match="gog timed out"):
        await _run_gog_command(["gog", "slides", "get", "deck-1"])

    assert process.killed is True
    assert spans[0]["error"] == "gog timed out"
