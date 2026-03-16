from __future__ import annotations

from app.memory_v2.models import CandidateMemory


class MemoryConsolidator:
    def __init__(self, service) -> None:
        self.service = service

    def promote_session_candidates(self, session_id: str) -> list[CandidateMemory]:
        return self.service._promote_staged_candidates(session_id)

    def apply_reflection(
        self,
        *,
        session_id: str,
        transcript: list[dict],
        summary_md: str,
        profile_updates: dict,
        open_loops: list[str],
        decisions: list[str],
    ) -> None:
        self.service._apply_reflection(
            session_id=session_id,
            transcript=transcript,
            summary_md=summary_md,
            profile_updates=profile_updates,
            open_loops=open_loops,
            decisions=decisions,
        )
