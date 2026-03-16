from __future__ import annotations

import re
from typing import Any

from app.memory_v2.governance import approval_status_for, make_id, utc_now
from app.memory_v2.graph import build_relations_from_entities, extract_entities_from_text
from app.memory_v2.models import CandidateMemory

_KEYWORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def namespace_for_candidate_type(candidate_type: str) -> str:
    mapping = {
        "task": "commitments",
        "commitment": "commitments",
        "open_loop": "commitments",
        "fact": "semantic",
        "decision": "reflective",
        "procedural": "procedural",
        "identity_patch": "identity",
        "reflection_summary": "episodic",
    }
    return mapping.get(candidate_type, "notes")


def extract_keywords(text: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in _KEYWORD_RE.findall(text.lower()):
        if raw in seen or raw in {"that", "this", "with", "from", "have", "will"}:
            continue
        seen.add(raw)
        keywords.append(raw)
    return keywords[:8]


def normalize_candidate(
    raw: dict[str, Any],
    *,
    source_session_id: str | None = None,
    source_turn: int | None = None,
    source: str = "tap",
) -> CandidateMemory | None:
    candidate_type = str(raw.get("type", "")).strip()
    text = str(raw.get("text", "")).strip()
    if not candidate_type or not text:
        return None

    confidence_raw = raw.get("confidence", 0.7)
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        confidence = 0.7

    namespace = namespace_for_candidate_type(candidate_type)
    sensitive = bool(raw.get("sensitive", False))
    entities = list(raw.get("entity_refs") or extract_entities_from_text(text))
    relations = list(raw.get("relation_refs") or build_relations_from_entities(entities))

    return CandidateMemory(
        id=str(raw.get("id") or make_id("cand")),
        type=candidate_type,
        namespace=namespace,
        text=text,
        body=str(raw.get("body", "")).strip(),
        created_at=str(raw.get("created_at") or utc_now()),
        confidence=confidence,
        approval_status=str(raw.get("approval_status") or approval_status_for(namespace, sensitive=sensitive)),
        source=str(raw.get("source") or source),
        source_session_id=str(raw.get("source_session_id") or source_session_id or "") or None,
        source_turn=raw.get("source_turn", source_turn),
        keywords=list(raw.get("keywords") or extract_keywords(text)),
        entity_refs=entities,
        relation_refs=relations,
        structured=dict(raw.get("structured") or {}),
        sensitive=sensitive,
    )
