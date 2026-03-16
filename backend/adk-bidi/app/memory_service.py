"""
MemoryService compatibility facade over Athena Memory V2.

The V2 implementation owns the canonical vault, SQLite index, staging, graph,
governance, and retrieval behavior. This facade preserves the existing call
surface used by the rest of the backend while routing all durable state through
UserMemoryService.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.memory_v2.service import UserMemoryService

ATHENA_DIR = Path.home() / ".athena"


class MemoryService:
    def __init__(self, base_dir: Path = ATHENA_DIR) -> None:
        self.base = base_dir
        self._v2 = UserMemoryService(base_dir)

    # --- Snapshot / retrieval ---

    def snapshot(self):
        return self._v2.snapshot()

    def build_context(self, *, session_id: str | None = None, query: str | None = None) -> str:
        return self._v2.build_context(session_id=session_id, query=query)

    def search_memory(
        self,
        query: str,
        *,
        limit: int = 5,
        namespaces: list[str] | None = None,
    ):
        return self._v2.search_memory(query, limit=limit, namespaces=namespaces)

    def pending_candidates(self):
        return self._v2.pending_candidates()

    def approve_candidate(self, candidate_id: str) -> bool:
        return self._v2.approve_candidate(candidate_id)

    def update_soul(
        self,
        patch_text: str,
        *,
        rationale: str,
        source_session_id: str | None = None,
        approved: bool = False,
    ) -> str:
        return self._v2.update_soul(
            patch_text,
            rationale=rationale,
            source_session_id=source_session_id,
            approved=approved,
        )

    # --- Profile (semantic memory) ---

    def read_profile(self) -> dict[str, Any]:
        return self._v2.read_profile()

    def write_profile(self, data: dict[str, Any]) -> None:
        self._v2.write_profile(data)

    def merge_profile(self, updates: dict[str, Any]) -> None:
        self._v2.merge_profile(updates)

    # --- Session summaries (episodic memory) ---

    def write_session_summary(self, summary: str) -> str:
        return self._v2.write_session_summary(summary)

    def read_recent_sessions(self, n: int = 3) -> list[str]:
        return [item.content for item in self._v2.read_recent_sessions(n)]

    def read_recent_session_summaries(self, n: int = 3):
        return self._v2.read_recent_sessions(n)

    # --- Ongoing / open loops ---

    def read_ongoing(self) -> str:
        return self._v2.read_ongoing()

    def write_ongoing(self, content: str) -> None:
        self._v2.write_ongoing(content)

    # --- Memory control ---

    def forget_key(self, key: str) -> bool:
        return self._v2.forget_fact(key)

    def archive_and_reset(self) -> str:
        return self._v2.clear_user_memory()

    # --- Event log / staging ---

    def append_log(self, entries: list[dict]) -> None:
        self._v2.append_log(entries)

    def stage_candidates(
        self,
        entries: list[dict],
        *,
        source_session_id: str | None = None,
    ):
        return self._v2.stage_candidates(entries, source_session_id=source_session_id)

    def consolidate_reflection(
        self,
        *,
        session_id: str,
        transcript: list[dict],
        summary_md: str,
        profile_updates: dict[str, Any],
        open_loops: list[str],
        decisions: list[str],
    ) -> None:
        self._v2.consolidate_reflection(
            session_id=session_id,
            transcript=transcript,
            summary_md=summary_md,
            profile_updates=profile_updates,
            open_loops=open_loops,
            decisions=decisions,
        )
