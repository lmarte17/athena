"""Async wrapper around the `gog` CLI with structured JSON output."""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from app.tracing import atrace_span, base_metadata, finish_span, preview_value

log = logging.getLogger("athena.gog")


class GogError(RuntimeError):
    """Base error raised by the gog wrapper."""


class GogUnavailableError(GogError):
    """Raised when the gog binary is missing."""


class GogAuthError(GogError):
    """Raised when gog is installed but not authenticated."""


class GogInvocationError(GogError):
    """Raised when gog returns a non-auth failure."""

def gog_binary() -> str:
    configured = os.getenv("ATHENA_GOG_BINARY")
    if configured:
        return configured

    on_path = shutil.which("gog")
    if on_path:
        return on_path

    local_venv = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "gog"
    if local_venv.exists():
        return str(local_venv)

    return "gog"


def gog_timeout_secs() -> float:
    return float(os.getenv("ATHENA_GOG_TIMEOUT_SECS", "15"))


async def run_gog_json(*args: str) -> dict:
    """Run `gog` with --json flag and return the decoded payload."""
    command = [gog_binary(), *args, "--json"]
    raw_stdout, _raw_stderr = await _run_gog_command(command)

    if not raw_stdout:
        return {}

    try:
        payload = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise GogInvocationError("gog returned invalid JSON") from exc

    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"items": payload, "count": len(payload)}

    raise GogInvocationError("gog returned an unsupported JSON payload")


async def run_gog_text(*args: str) -> str:
    """Run `gog` and return plain text output (for commands that don't support --json)."""
    command = [gog_binary(), *args]
    raw_stdout, _raw_stderr = await _run_gog_command(command)
    return raw_stdout


async def _run_gog_command(command: list[str]) -> tuple[str, str]:
    log.debug("Running gog command: %s", command)

    # Inject account env vars for headless/multi-account use if configured.
    env: dict[str, str] | None = None
    gog_account = os.getenv("GOG_ACCOUNT")
    gog_keyring_backend = os.getenv("GOG_KEYRING_BACKEND")
    gog_keyring_password = os.getenv("GOG_KEYRING_PASSWORD")
    if gog_account or gog_keyring_backend or gog_keyring_password:
        env = dict(os.environ)
        if gog_account:
            env["GOG_ACCOUNT"] = gog_account
        if gog_keyring_backend:
            env["GOG_KEYRING_BACKEND"] = gog_keyring_backend
        if gog_keyring_password:
            env["GOG_KEYRING_PASSWORD"] = gog_keyring_password

    async with atrace_span(
        "athena.gog.command",
        run_type="tool",
        inputs={
            "command": command,
            "timeout_secs": gog_timeout_secs(),
        },
        metadata=base_metadata(
            component="gog.command",
            gog_account=gog_account,
            gog_keyring_backend=gog_keyring_backend,
            has_gog_keyring_password=bool(gog_keyring_password),
        ),
        tags=["gog", "workspace"],
    ) as run:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            finish_span(run, error=f"{command[0]} not found")
            raise GogUnavailableError(f"{command[0]} not found") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=gog_timeout_secs(),
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            finish_span(run, error=f"{command[0]} timed out")
            raise GogInvocationError(f"{command[0]} timed out") from exc

        raw_stdout = stdout.decode(errors="replace").strip()
        raw_stderr = stderr.decode(errors="replace").strip()

        outputs = {
            "returncode": process.returncode,
            "stdout": preview_value(raw_stdout),
            "stderr": preview_value(raw_stderr),
        }

        if process.returncode != 0:
            message = raw_stderr or raw_stdout or f"{command[0]} exited with {process.returncode}"
            finish_span(run, outputs=outputs, error=message)
            if _looks_like_auth_error(message):
                raise GogAuthError(message)
            raise GogInvocationError(message)

        finish_span(run, outputs=outputs)
        return raw_stdout, raw_stderr


def _looks_like_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "not authenticated",
            "unauthenticated",
            "invalid_grant",
            "oauth",
            "login required",
            "token",
            "credentials",
            "auth login",
            "gog auth",
            "please authenticate",
            "sign in",
        )
    )
