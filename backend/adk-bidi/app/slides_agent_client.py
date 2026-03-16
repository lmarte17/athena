"""Async wrapper around the `slides-agent` CLI with structured JSON output."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from app.tracing import atrace_span, base_metadata, finish_span, preview_value

log = logging.getLogger("athena.slides_agent")


class SlidesAgentError(RuntimeError):
    """Base error raised by the slides-agent wrapper."""

    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


class SlidesAgentUnavailableError(SlidesAgentError):
    """Raised when the slides-agent binary is missing."""


class SlidesAgentAuthError(SlidesAgentError):
    """Raised when slides-agent is installed but not authenticated."""


class SlidesAgentInvocationError(SlidesAgentError):
    """Raised when slides-agent returns a non-auth failure."""


def slides_agent_binary() -> str:
    configured = os.getenv("ATHENA_SLIDES_AGENT_BINARY")
    if configured:
        return configured

    on_path = shutil.which("slides-agent")
    if on_path:
        return on_path

    local_venv = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "slides-agent"
    if local_venv.exists():
        return str(local_venv)

    return "slides-agent"


def slides_agent_timeout_secs() -> float:
    return float(os.getenv("ATHENA_SLIDES_AGENT_TIMEOUT_SECS", "20"))


async def run_slides_agent_json(*args: str) -> dict[str, Any]:
    """Run `slides-agent` and return the decoded JSON payload."""
    command = [slides_agent_binary(), *args]
    raw_stdout, _raw_stderr = await _run_slides_agent_command(command)

    if not raw_stdout:
        return {}

    try:
        payload = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise SlidesAgentInvocationError("slides-agent returned invalid JSON") from exc

    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"items": payload, "count": len(payload)}

    raise SlidesAgentInvocationError("slides-agent returned an unsupported JSON payload")


async def _run_slides_agent_command(command: list[str]) -> tuple[str, str]:
    log.debug("Running slides-agent command: %s", command)

    async with atrace_span(
        "athena.slides_agent.command",
        run_type="tool",
        inputs={
            "command": command,
            "timeout_secs": slides_agent_timeout_secs(),
        },
        metadata=base_metadata(
            component="slides_agent.command",
            slides_agent_credentials=bool(os.getenv("SLIDES_AGENT_CREDENTIALS")),
            slides_agent_token_file=bool(os.getenv("SLIDES_AGENT_TOKEN_FILE")),
        ),
        tags=["slides-agent", "workspace"],
    ) as run:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            finish_span(run, error=f"{command[0]} not found")
            raise SlidesAgentUnavailableError(f"{command[0]} not found") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=slides_agent_timeout_secs(),
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            finish_span(run, error=f"{command[0]} timed out")
            raise SlidesAgentInvocationError(f"{command[0]} timed out") from exc

        raw_stdout = stdout.decode(errors="replace").strip()
        raw_stderr = stderr.decode(errors="replace").strip()
        payload = _parse_error_payload(raw_stdout) or _parse_error_payload(raw_stderr)
        message = _error_message(payload, raw_stdout, raw_stderr, command[0], process.returncode)

        outputs = {
            "returncode": process.returncode,
            "stdout": preview_value(raw_stdout),
            "stderr": preview_value(raw_stderr),
        }

        if process.returncode != 0:
            finish_span(run, outputs=outputs, error=message)
            if _error_code(payload) == "auth_error" or _looks_like_auth_error(message):
                raise SlidesAgentAuthError(message, payload=payload)
            raise SlidesAgentInvocationError(message, payload=payload)

        finish_span(run, outputs=outputs)
        return raw_stdout, raw_stderr


def _parse_error_payload(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _error_code(payload: dict[str, Any] | None) -> str:
    return str((payload or {}).get("error_code") or "").strip().lower()


def _error_message(
    payload: dict[str, Any] | None,
    raw_stdout: str,
    raw_stderr: str,
    binary: str,
    returncode: int,
) -> str:
    if payload:
        detail = str(payload.get("detail") or "").strip()
        if detail:
            return detail
        code = _error_code(payload)
        if code:
            return code
    return raw_stderr or raw_stdout or f"{binary} exited with {returncode}"


def _looks_like_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "auth_error",
            "not authenticated",
            "no valid credentials found",
            "credentials",
            "slides-agent auth login",
            "oauth",
            "token",
            "sign in",
        )
    )
