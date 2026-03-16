"""LangSmith tracing helpers for Athena."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from google import genai
from langsmith import trace
from langsmith.integrations.google_adk import configure_google_adk
from langsmith.wrappers import wrap_gemini

TRACE_PROVIDER = "langsmith"
TRACE_SERVICE = "athena-backend"
TRACE_TAG = "athena"
TRACE_PREVIEW_CHARS = int(os.getenv("ATHENA_TRACE_PREVIEW_CHARS", "16000"))

_BOOTSTRAPPED = False
_TRACE_STATUS: "TracingStatus | None" = None


@dataclass(frozen=True)
class TracingStatus:
    enabled: bool
    provider: str
    project_name: str
    environment: str
    thought_capture_enabled: bool
    endpoint: str | None
    disabled_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def trace_environment() -> str:
    if explicit := os.getenv("ATHENA_TRACE_ENV"):
        return explicit
    return "cloudrun" if os.getenv("K_SERVICE") else "local"


def default_project_name() -> str:
    return "athena-cloudrun" if trace_environment() == "cloudrun" else "athena-local"


def provider_mode() -> str:
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"
    return "vertex" if use_vertex else "ai_studio"


def thought_capture_enabled() -> bool:
    raw = os.getenv("ATHENA_TRACE_CAPTURE_THOUGHTS", "TRUE").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _project_name() -> str:
    return os.getenv("LANGSMITH_PROJECT", default_project_name())


def bootstrap_tracing() -> TracingStatus:
    """Initialize LangSmith tracing once per process."""
    global _BOOTSTRAPPED, _TRACE_STATUS
    if _BOOTSTRAPPED and _TRACE_STATUS is not None:
        return _TRACE_STATUS

    environment = trace_environment()
    project_name = _project_name()
    endpoint = os.getenv("LANGSMITH_ENDPOINT")
    capture_thoughts = thought_capture_enabled()

    api_key = os.getenv("LANGSMITH_API_KEY")
    if not api_key:
        _TRACE_STATUS = TracingStatus(
            enabled=False,
            provider=TRACE_PROVIDER,
            project_name=project_name,
            environment=environment,
            thought_capture_enabled=capture_thoughts,
            endpoint=endpoint,
            disabled_reason="LANGSMITH_API_KEY not set",
        )
        _BOOTSTRAPPED = True
        return _TRACE_STATUS

    os.environ.setdefault("LANGSMITH_PROJECT", project_name)
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_TRACING_V2", "true")

    configured = configure_google_adk(
        name="athena.adk",
        project_name=project_name,
        metadata=base_metadata(component="google_adk"),
        tags=base_tags("google-adk"),
    )

    _TRACE_STATUS = TracingStatus(
        enabled=bool(configured),
        provider=TRACE_PROVIDER,
        project_name=project_name,
        environment=environment,
        thought_capture_enabled=capture_thoughts,
        endpoint=endpoint,
        disabled_reason=None if configured else "configure_google_adk returned false",
    )
    _BOOTSTRAPPED = True
    return _TRACE_STATUS


def tracing_status() -> TracingStatus:
    return bootstrap_tracing()


def tracing_enabled() -> bool:
    return tracing_status().enabled


def filter_nones(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    if not mapping:
        return {}
    return {key: value for key, value in mapping.items() if value is not None}


def base_tags(*extra: str) -> list[str]:
    tags = [TRACE_TAG, trace_environment(), provider_mode()]
    tags.extend(tag for tag in extra if tag)
    return list(dict.fromkeys(tags))


def base_metadata(
    *,
    component: str,
    athena_session_id: str | None = None,
    turn_id: str | None = None,
    decision_id: str | None = None,
    task_id: str | None = None,
    source_event_id: str | None = None,
    surface_plan_id: str | None = None,
    source_turn_id: str | None = None,
    job_id: str | None = None,
    step_id: str | None = None,
    specialist: str | None = None,
    model: str | None = None,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return filter_nones(
        {
            "athena_service": TRACE_SERVICE,
            "athena_environment": trace_environment(),
            "athena_provider_mode": provider_mode(),
            "athena_component": component,
            "athena_session_id": athena_session_id,
            "athena_turn_id": turn_id,
            "athena_decision_id": decision_id,
            "athena_task_id": task_id,
            "athena_source_event_id": source_event_id,
            "athena_surface_plan_id": surface_plan_id,
            "athena_source_turn_id": source_turn_id,
            "athena_job_id": job_id,
            "athena_step_id": step_id,
            "athena_specialist": specialist,
            "athena_model": model,
            "athena_error": error,
            **extra,
        }
    )


def preview_text(text: str, *, max_chars: int = TRACE_PREVIEW_CHARS) -> dict[str, Any]:
    if len(text) <= max_chars:
        return {"text": text, "chars": len(text), "truncated": False}
    return {
        "text": text[: max_chars - 1].rstrip() + "…",
        "chars": len(text),
        "truncated": True,
    }


def preview_value(value: Any, *, max_chars: int = TRACE_PREVIEW_CHARS) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return preview_text(value, max_chars=max_chars)
    if isinstance(value, bytes):
        decoded = value.decode(errors="replace")
        return preview_text(decoded, max_chars=max_chars)
    if isinstance(value, Mapping):
        return {key: preview_value(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [preview_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, tuple):
        return [preview_value(item, max_chars=max_chars) for item in value]
    if hasattr(value, "to_dict"):
        return preview_value(value.to_dict(), max_chars=max_chars)
    return value


@contextmanager
def trace_span(
    name: str,
    *,
    run_type: str = "chain",
    inputs: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    tags: list[str] | None = None,
):
    status = tracing_status()
    if not status.enabled:
        yield None
        return

    with trace(
        name=name,
        run_type=run_type,
        project_name=status.project_name,
        inputs=filter_nones(dict(inputs or {})),
        metadata=base_metadata(component=name, **filter_nones(metadata)),
        tags=base_tags(*(tags or [])),
    ) as run:
        yield run


@asynccontextmanager
async def atrace_span(
    name: str,
    *,
    run_type: str = "chain",
    inputs: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    tags: list[str] | None = None,
):
    status = tracing_status()
    if not status.enabled:
        yield None
        return

    async with trace(
        name=name,
        run_type=run_type,
        project_name=status.project_name,
        inputs=filter_nones(dict(inputs or {})),
        metadata=base_metadata(component=name, **filter_nones(metadata)),
        tags=base_tags(*(tags or [])),
    ) as run:
        yield run


def finish_span(run: Any, *, outputs: Mapping[str, Any] | None = None, error: str | None = None) -> None:
    if run is None:
        return
    if error is not None:
        run.end(error=error)
        return
    run.end(outputs=filter_nones(dict(outputs or {})) or None)


def create_gemini_client(
    component: str,
    *,
    model: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    tags: list[str] | None = None,
) -> genai.Client:
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"
    if use_vertex:
        client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
    else:
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

    if not tracing_enabled():
        return client

    wrapped_metadata = base_metadata(
        component=component,
        model=model,
        **filter_nones(metadata),
    )
    return wrap_gemini(
        client,
        chat_name=component,
        tracing_extra={
            "metadata": wrapped_metadata,
            "tags": base_tags(component, *(tags or [])),
        },
    )


def record_surfaced_thought(
    *,
    athena_session_id: str,
    text: str,
    source: str,
    job_id: str | None = None,
) -> None:
    if not tracing_enabled() or not thought_capture_enabled() or not text:
        return
    with trace_span(
        "athena.live.thought",
        inputs={"text": text},
        metadata=base_metadata(
            component="live.thought",
            athena_session_id=athena_session_id,
            job_id=job_id,
            source=source,
        ),
        tags=["thought"],
    ) as run:
        finish_span(
            run,
            outputs={"captured": True, "chars": len(text), "source": source},
        )
