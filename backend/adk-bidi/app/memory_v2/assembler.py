from __future__ import annotations

import logging

import yaml

from app.memory_v2.models import MemorySearchHit

log = logging.getLogger("athena.memory_v2.assembler")


class MemoryAssembler:
    def __init__(self, service) -> None:
        self.service = service

    def build(self, *, session_id: str | None = None, query: str | None = None) -> str:
        parts: list[str] = []

        soul = self.service.read_soul()
        if soul:
            parts.append(soul)

        profile = self.service.read_profile()
        visible_profile = {
            key: value
            for key, value in profile.items()
            if not key.startswith("_") and key != "updated"
        }
        if visible_profile:
            profile_yaml = yaml.dump(visible_profile, default_flow_style=False, allow_unicode=True).strip()
            parts.append(f"## What you know about this person\n\n```yaml\n{profile_yaml}\n```")

        commitments = [item for item in self.service.read_commitments() if item.status != "done"][:6]
        if commitments:
            body = "\n".join(f"- [ ] {item.text}" for item in commitments)
            parts.append(f"## Active commitments\n\n{body}")

        notes_index = self.service.read_notes_index()
        if notes_index:
            parts.append(notes_index)

        if query and self.service.should_retrieve(query):
            hits = self.service.search_memory(query, limit=3)
            retrieved = self._render_hits(hits)
            if retrieved:
                parts.append(retrieved)

        bundle = "\n\n---\n\n".join(part.strip() for part in parts if part.strip())
        log.info("MemoryAssembler built bundle for %s (%d chars)", session_id or "unknown", len(bundle))
        return bundle

    def _render_hits(self, hits: list[MemorySearchHit]) -> str:
        if not hits:
            return ""
        lines = ["## Relevant memory for this turn", ""]
        for hit in hits:
            lines.append(f"### {hit.title}")
            lines.append(hit.snippet.strip())
            lines.append("")
        return "\n".join(lines).strip()
