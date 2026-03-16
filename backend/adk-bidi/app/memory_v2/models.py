from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SessionSummary:
    filename: str
    content: str
    session_id: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Commitment:
    id: str
    text: str
    status: str = "open"
    created_at: str | None = None
    updated_at: str | None = None
    source_session_id: str | None = None
    confidence: float = 0.7
    approval_status: str = "auto"
    due_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CandidateMemory:
    id: str
    type: str
    namespace: str
    text: str
    body: str = ""
    created_at: str | None = None
    confidence: float = 0.7
    approval_status: str = "auto"
    source: str = "tap"
    source_session_id: str | None = None
    source_turn: int | None = None
    keywords: list[str] = field(default_factory=list)
    entity_refs: list[str] = field(default_factory=list)
    relation_refs: list[dict[str, Any]] = field(default_factory=list)
    structured: dict[str, Any] = field(default_factory=dict)
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MemorySearchHit:
    doc_key: str
    namespace: str
    title: str
    path: str
    snippet: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MemorySnapshot:
    profile: dict[str, Any]
    profile_yaml: str
    commitments: list[Commitment]
    sessions: list[SessionSummary]
    soul: str
    pending_candidates: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "profile_yaml": self.profile_yaml,
            "commitments": [item.to_dict() for item in self.commitments],
            "sessions": [item.to_dict() for item in self.sessions],
            "soul": self.soul,
            "pending_candidates": self.pending_candidates,
        }
