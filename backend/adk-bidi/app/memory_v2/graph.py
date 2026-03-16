from __future__ import annotations

import re
from typing import Any


_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}|[A-Z]{2,}(?:\s+[A-Z]{2,})*)\b"
)
_ENTITY_STOPWORDS = {
    "Athena",
    "User",
    "The",
    "This",
    "That",
    "Today",
    "Tomorrow",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
}


def infer_entity_type(name: str) -> str:
    lowered = name.lower()
    if "doc" in lowered or "report" in lowered or "brief" in lowered:
        return "document"
    if "project" in lowered or "launch" in lowered:
        return "project"
    if "calendar" in lowered or "meeting" in lowered:
        return "event"
    if "gmail" in lowered or "drive" in lowered:
        return "workspace"
    if "team" in lowered:
        return "team"
    if "alex" in lowered or "sarah" in lowered or "john" in lowered:
        return "person"
    return "entity"


def extract_entities_from_text(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _ENTITY_RE.findall(text or ""):
        cleaned = " ".join(match.split()).strip()
        if len(cleaned) < 3 or cleaned in _ENTITY_STOPWORDS:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        found.append(cleaned)
    return found


def extract_profile_entities(key: str, value: Any) -> list[str]:
    entities: list[str] = []
    if isinstance(value, str):
        entities.extend(extract_entities_from_text(value))
        if key.endswith("_project") or "project" in key:
            entities.append(value.strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for entity in entities:
        candidate = entity.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def build_relations_for_profile(key: str, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, str):
        return []
    relations: list[dict[str, Any]] = []
    for entity in extract_profile_entities(key, value):
        relations.append(
            {
                "src": "user",
                "predicate": f"profile:{key}",
                "dst": entity,
                "metadata": {"value": value},
            }
        )
    return relations


def build_relations_from_entities(entities: list[str]) -> list[dict[str, Any]]:
    if len(entities) < 2:
        return []
    relations: list[dict[str, Any]] = []
    root = entities[0]
    for other in entities[1:]:
        relations.append(
            {
                "src": root,
                "predicate": "related_to",
                "dst": other,
                "metadata": {},
            }
        )
    return relations
