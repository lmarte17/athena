"""
ContextBuilder for Memory V2.

The base memory context is assembled dynamically through MemoryService so the
active session can reflect memory changes immediately. A lightweight per-process
registry still tracks per-session fallbacks and live context fragments.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.memory_service import MemoryService

log = logging.getLogger("athena.context")

_session_contexts: dict[str, str] = {}
_live_contexts: dict[str, dict[str, str]] = {}
_active_queries: dict[str, str] = {}
_dynamic_sessions: set[str] = set()
_context_provider: Callable[[str, str, str], str] | None = None

_MAX_LIVE_CONTEXT_ITEMS = 3
_MAX_LIVE_CONTEXT_CHARS = 1200
_MAX_LIVE_ENTRY_CHARS = 400


def set_context_provider(provider: Callable[[str, str, str], str] | None) -> None:
    global _context_provider
    _context_provider = provider


def register_context(session_id: str, bundle: str) -> None:
    _session_contexts[session_id] = bundle


def enable_dynamic_context(session_id: str) -> None:
    _dynamic_sessions.add(session_id)


def update_live_context(session_id: str, label: str, content: str) -> None:
    normalized = _normalize_live_content(content)
    if not normalized:
        return
    if len(normalized) > _MAX_LIVE_ENTRY_CHARS:
        normalized = normalized[: _MAX_LIVE_ENTRY_CHARS - 3].rstrip() + "..."
    session_live = _live_contexts.setdefault(session_id, {})
    session_live.pop(label, None)
    session_live[label] = normalized
    while len(session_live) > _MAX_LIVE_CONTEXT_ITEMS:
        oldest = next(iter(session_live))
        session_live.pop(oldest, None)


def set_active_query(session_id: str, text: str) -> None:
    collapsed = " ".join(text.split()).strip()
    if collapsed:
        _active_queries[session_id] = collapsed


def clear_active_query(session_id: str) -> None:
    _active_queries.pop(session_id, None)


def clear_live_context(session_id: str) -> None:
    _live_contexts.pop(session_id, None)


def get_context(session_id: str) -> str:
    base = _session_contexts.get(session_id, "")
    query = _active_queries.get(session_id, "")
    if _context_provider is not None and session_id in _dynamic_sessions:
        try:
            dynamic = _context_provider(session_id, base, query)
            if dynamic:
                base = dynamic
        except Exception:
            log.exception("Context provider failed for %s", session_id)
    live = _format_live_context(_live_contexts.get(session_id, {}))
    if base and live:
        return f"{base}\n\n---\n\n{live}"
    return base or live


def unregister_context(session_id: str) -> None:
    _session_contexts.pop(session_id, None)
    _active_queries.pop(session_id, None)
    _dynamic_sessions.discard(session_id)
    clear_live_context(session_id)


def _format_live_context(items: dict[str, str]) -> str:
    if not items:
        return ""
    rendered: list[str] = []
    total_chars = 0
    for label, content in reversed(list(items.items())):
        section = f"### {label}\n{content}"
        if rendered and total_chars + len(section) > _MAX_LIVE_CONTEXT_CHARS:
            break
        rendered.append(section)
        total_chars += len(section)
    if not rendered:
        return ""
    rendered.reverse()
    body = "\n\n".join(rendered)
    return (
        "## Fresh live context loaded during this session\n\n"
        "Treat this as the freshest external source of truth for the current conversation.\n\n"
        f"{body}"
    )


def _normalize_live_content(content: str) -> str:
    lines: list[str] = []
    for raw_line in content.splitlines():
        collapsed = " ".join(raw_line.split()).strip()
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines)


class ContextBuilder:
    def __init__(self, memory_service: MemoryService) -> None:
        self.memory = memory_service

    def build(self, session_id: str | None = None, query: str | None = None) -> str:
        return self.memory.build_context(session_id=session_id, query=query)
