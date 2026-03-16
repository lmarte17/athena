from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.memory_v2.assembler import MemoryAssembler
from app.memory_v2.candidates import normalize_candidate
from app.memory_v2.consolidator import MemoryConsolidator
from app.memory_v2.governance import build_audit_entry, make_id, stable_hash, utc_now
from app.memory_v2.graph import build_relations_for_profile, extract_profile_entities, infer_entity_type
from app.memory_v2.index_db import MemoryIndex
from app.memory_v2.migration import migrate_legacy_memory
from app.memory_v2.models import CandidateMemory, Commitment, MemorySnapshot, SessionSummary
from app.memory_v2.vault import MemoryVault

log = logging.getLogger("athena.memory_v2.service")


class UserMemoryService:
    def __init__(self, base_dir: Path) -> None:
        self.base = base_dir
        self.vault = MemoryVault(base_dir)
        migrated = migrate_legacy_memory(self.vault)
        self.index = MemoryIndex(self.vault.index_path)
        self.assembler = MemoryAssembler(self)
        self.consolidator = MemoryConsolidator(self)
        self.rebuild_index()
        if migrated:
            self.vault.add_audit_event("migrated_legacy_memory", "system", {"base": str(base_dir)})

    def build_context(self, *, session_id: str | None = None, query: str | None = None) -> str:
        return self.assembler.build(session_id=session_id, query=query)

    def should_retrieve(self, query: str) -> bool:
        lowered = query.lower()
        trigger_words = (
            "remember",
            "recall",
            "what happened",
            "did we",
            "when did",
            "who",
            "which project",
            "preference",
            "prefer",
            "last time",
            "my ",
            "our ",
        )
        return any(marker in lowered for marker in trigger_words)

    def snapshot(self, *, recent_sessions: int = 3) -> MemorySnapshot:
        profile = self.read_profile()
        profile_yaml = yaml.dump(
            {
                key: value
                for key, value in profile.items()
                if not key.startswith("_")
            },
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        return MemorySnapshot(
            profile=profile,
            profile_yaml=profile_yaml or "(no profile yet)",
            commitments=self.read_commitments(),
            sessions=self.read_recent_sessions(recent_sessions),
            soul=self.read_soul(),
            pending_candidates=len(self.pending_candidates()),
        )

    def read_soul(self) -> str:
        return self.vault.read_soul()

    def update_soul(
        self,
        patch_text: str,
        *,
        rationale: str,
        source_session_id: str | None = None,
        approved: bool = False,
    ) -> str:
        candidate = CandidateMemory(
            id=make_id("soul"),
            type="identity_patch",
            namespace="identity",
            text=rationale,
            body=patch_text,
            created_at=utc_now(),
            confidence=1.0,
            approval_status="approved" if approved else "pending",
            source="manual",
            source_session_id=source_session_id,
        )
        self.vault.stage_candidate(candidate)
        self.index.stage_candidate(candidate)
        if approved:
            self._apply_identity_patch(candidate)
            self.vault.remove_staged_candidates({candidate.id})
            self.index.remove_candidates({candidate.id})
        self.vault.add_audit_event(
            "identity_patch_staged" if not approved else "identity_patch_applied",
            "identity",
            {"candidate_id": candidate.id, "rationale": rationale},
            source_session_id=source_session_id,
        )
        return candidate.id

    def approve_candidate(self, candidate_id: str) -> bool:
        staged = [item for item in self.vault.read_staged_candidates() if item.id == candidate_id]
        if not staged:
            return False
        candidate = staged[0]
        if candidate.namespace == "identity":
            self._apply_identity_patch(candidate)
        self.index.update_candidate_status(candidate_id, "approved")
        self.vault.add_audit_event(
            "candidate_approved",
            candidate.namespace,
            {"candidate_id": candidate_id, "type": candidate.type},
            source_session_id=candidate.source_session_id,
        )
        self.vault.remove_staged_candidates({candidate_id})
        self.index.remove_candidates({candidate_id})
        self.rebuild_index()
        return True

    def _apply_identity_patch(self, candidate: CandidateMemory) -> None:
        soul = self.read_soul().rstrip()
        patch = candidate.body.strip() or candidate.text.strip()
        updated = (
            f"{soul}\n\n## Approved evolution patch ({candidate.created_at or utc_now()})\n\n{patch}\n"
        )
        self.vault.write_soul(updated)

    def read_profile(self) -> dict[str, Any]:
        return self.vault.read_profile()

    def write_profile(self, data: dict[str, Any]) -> None:
        self.vault.write_profile(data)
        self.rebuild_index()

    def merge_profile(self, updates: dict[str, Any], *, source_session_id: str | None = None) -> None:
        profile = self.read_profile()
        confidence = dict(profile.get("_confidence", {}))
        history = dict(profile.get("_history", {}))

        for key, update in updates.items():
            if isinstance(update, dict) and "value" in update:
                new_value = update["value"]
                try:
                    new_confidence = float(update.get("confidence", 0.7))
                except (TypeError, ValueError):
                    new_confidence = 0.7
            else:
                new_value = update
                new_confidence = 0.7

            existing_confidence = float(confidence.get(key, 0.0))
            if new_confidence < existing_confidence - 0.1:
                continue
            if key in profile and profile[key] != new_value:
                history.setdefault(key, []).append(
                    {
                        "value": profile[key],
                        "replaced_at": utc_now(),
                        "confidence": existing_confidence,
                    }
                )
            profile[key] = new_value
            confidence[key] = round(max(existing_confidence, new_confidence), 2)

        profile["_confidence"] = confidence
        profile["_history"] = history
        profile["updated"] = utc_now()[:10]
        self.vault.write_profile(profile)
        self.vault.add_audit_event(
            "profile_merged",
            "semantic",
            {"keys": sorted(updates.keys())},
            source_session_id=source_session_id,
        )
        self.rebuild_index()

    def read_commitments(self) -> list[Commitment]:
        return self.vault.read_commitments()

    def write_commitments(self, commitments: list[Commitment]) -> None:
        self.vault.write_commitments(commitments)
        self.rebuild_index()

    def read_recent_sessions(self, n: int = 3) -> list[SessionSummary]:
        return self.vault.read_recent_sessions(n)

    def write_session_summary(
        self,
        summary: str,
        *,
        session_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        filename = self.vault.write_session_summary(summary, session_id=session_id, created_at=created_at)
        self.vault.add_audit_event(
            "session_summary_written",
            "episodic",
            {"filename": filename},
            source_session_id=session_id,
        )
        self.rebuild_index()
        return filename

    def read_ongoing(self) -> str:
        return self.vault.render_ongoing_markdown(self.read_commitments())

    def write_ongoing(self, content: str) -> None:
        commitments = self.vault.parse_ongoing_markdown(content)
        self.write_commitments(commitments)

    def append_log(self, entries: list[dict[str, Any]]) -> None:
        self.vault.append_legacy_log(entries)
        audit_entries = [
            build_audit_entry("legacy_log_append", "audit", entry, source_session_id=entry.get("session"))
            for entry in entries
        ]
        self.vault.append_audit(audit_entries)

    def stage_candidates(
        self,
        entries: list[dict[str, Any]],
        *,
        source_session_id: str | None = None,
    ) -> list[CandidateMemory]:
        staged: list[CandidateMemory] = []
        for entry in entries:
            candidate = normalize_candidate(entry, source_session_id=source_session_id, source=str(entry.get("source", "tap")))
            if candidate is None:
                continue
            self.vault.stage_candidate(candidate)
            self.index.stage_candidate(candidate)
            staged.append(candidate)
        if staged:
            self.vault.append_audit(
                [
                    build_audit_entry(
                        "candidate_staged",
                        candidate.namespace,
                        candidate.to_dict(),
                        source_session_id=candidate.source_session_id,
                    )
                    for candidate in staged
                ]
            )
        return staged

    def pending_candidates(self) -> list[CandidateMemory]:
        return self.vault.read_staged_candidates()

    def search_memory(
        self,
        query: str,
        *,
        limit: int = 5,
        namespaces: list[str] | None = None,
    ):
        return self.index.search(query, limit=limit, namespaces=namespaces)

    def read_notes_index(self) -> str:
        return self.vault.read_notes_index()

    def rebuild_index(self) -> None:
        docs: list[dict[str, Any]] = []
        profile = self.read_profile()
        profile_entities: list[dict[str, Any]] = []
        profile_relations: list[dict[str, Any]] = []
        for key, value in profile.items():
            if key.startswith("_") or key == "updated":
                continue
            for entity in extract_profile_entities(key, value):
                profile_entities.append({"name": entity, "type": infer_entity_type(entity), "metadata": {"source": key}})
            profile_relations.extend(build_relations_for_profile(key, value))

        for doc in self.vault.iter_documents():
            if doc["doc_key"] == "notes:index":
                doc["entities"] = []
                doc["relations"] = []
            elif doc["doc_key"].startswith("semantic:profile:"):
                doc["entities"] = profile_entities
                doc["relations"] = profile_relations
            else:
                doc["entities"] = [
                    {"name": entity, "type": infer_entity_type(entity), "metadata": {}}
                    for entity in self._extract_doc_entities(doc)
                ]
                doc["relations"] = [
                    {"id": make_id("rel"), **relation}
                    for relation in self._extract_doc_relations(doc)
                ]
            docs.append(doc)
        self.index.rebuild_documents(docs)
        self._refresh_index_md()

    def _refresh_index_md(self) -> None:
        snapshot = self.snapshot(recent_sessions=3)
        self.vault.rebuild_notes_index(
            profile=snapshot.profile,
            commitments=snapshot.commitments,
            sessions=snapshot.sessions,
            pending_candidates=snapshot.pending_candidates,
        )
        docs = self.vault.iter_documents()
        self.index.rebuild_documents(
            [
                {
                    **doc,
                    "entities": doc.get("entities", []),
                    "relations": doc.get("relations", []),
                }
                for doc in docs
            ]
        )

    def _extract_doc_entities(self, doc: dict[str, Any]) -> list[str]:
        from app.memory_v2.graph import extract_entities_from_text

        return extract_entities_from_text(doc.get("content", ""))

    def _extract_doc_relations(self, doc: dict[str, Any]) -> list[dict[str, Any]]:
        from app.memory_v2.graph import build_relations_from_entities

        entities = self._extract_doc_entities(doc)
        return build_relations_from_entities(entities)

    def _promote_staged_candidates(self, session_id: str) -> list[CandidateMemory]:
        staged = [item for item in self.vault.read_staged_candidates() if item.source_session_id == session_id]
        if not staged:
            return []
        commitments = self.read_commitments()
        by_text = {item.text.lower(): item for item in commitments}
        promoted: list[CandidateMemory] = []
        for candidate in staged:
            if candidate.approval_status == "pending":
                continue
            if candidate.namespace == "commitments":
                key = candidate.text.lower()
                current = by_text.get(key)
                if current is None:
                    commitment = Commitment(
                        id=f"commit_{stable_hash(candidate.text)}",
                        text=candidate.text,
                        status="open",
                        created_at=candidate.created_at,
                        updated_at=utc_now(),
                        source_session_id=session_id,
                        confidence=candidate.confidence,
                        approval_status=candidate.approval_status,
                    )
                    commitments.append(commitment)
                    by_text[key] = commitment
                else:
                    current.updated_at = utc_now()
            elif candidate.namespace in {"notes", "reflective", "procedural", "semantic"}:
                metadata = {
                    "candidate_id": candidate.id,
                    "confidence": candidate.confidence,
                    "created_at": candidate.created_at,
                }
                self.vault.write_topic_note(
                    title=candidate.text[:80],
                    body=candidate.body or candidate.text,
                    namespace=candidate.namespace,
                    source_session_id=session_id,
                    metadata=metadata,
                )
            promoted.append(candidate)
        self.vault.write_commitments(commitments)
        self.vault.remove_staged_candidates({item.id for item in promoted})
        self.index.remove_candidates({item.id for item in promoted})
        if promoted:
            self.vault.append_audit(
                [
                    build_audit_entry(
                        "candidate_promoted",
                        item.namespace,
                        {"candidate_id": item.id, "type": item.type, "text": item.text},
                        source_session_id=item.source_session_id,
                    )
                    for item in promoted
                ]
            )
            self.rebuild_index()
        return promoted

    def consolidate_reflection(
        self,
        *,
        session_id: str,
        transcript: list[dict[str, Any]],
        summary_md: str,
        profile_updates: dict[str, Any],
        open_loops: list[str],
        decisions: list[str],
    ) -> None:
        self.consolidator.apply_reflection(
            session_id=session_id,
            transcript=transcript,
            summary_md=summary_md,
            profile_updates=profile_updates,
            open_loops=open_loops,
            decisions=decisions,
        )

    def _apply_reflection(
        self,
        *,
        session_id: str,
        transcript: list[dict[str, Any]],
        summary_md: str,
        profile_updates: dict[str, Any],
        open_loops: list[str],
        decisions: list[str],
    ) -> None:
        self._promote_staged_candidates(session_id)

        if summary_md.strip():
            self.write_session_summary(summary_md, session_id=session_id)

        if profile_updates:
            self.merge_profile(profile_updates, source_session_id=session_id)

        if open_loops:
            commitments = self.read_commitments()
            by_text = {item.text.lower(): item for item in commitments}
            for text in open_loops:
                normalized = text.strip()
                if not normalized:
                    continue
                key = normalized.lower()
                if key in by_text:
                    by_text[key].updated_at = utc_now()
                    continue
                commitments.append(
                    Commitment(
                        id=f"commit_{stable_hash(normalized)}",
                        text=normalized,
                        status="open",
                        created_at=utc_now(),
                        updated_at=utc_now(),
                        source_session_id=session_id,
                    )
                )
            self.write_commitments(commitments)

        legacy_log_rows: list[dict[str, Any]] = []
        for decision in decisions:
            cleaned = decision.strip()
            if not cleaned:
                continue
            self.vault.write_topic_note(
                title=cleaned[:80],
                body=cleaned,
                namespace="reflective",
                source_session_id=session_id,
                metadata={"kind": "decision"},
            )
            legacy_log_rows.append(
                {
                    "type": "decision",
                    "text": cleaned,
                    "session": session_id,
                    "source": "reflection",
                }
            )

        for item in open_loops:
            if item.strip():
                legacy_log_rows.append(
                    {
                        "type": "open_loop",
                        "text": item.strip(),
                        "session": session_id,
                        "source": "reflection",
                    }
                )

        if legacy_log_rows:
            self.append_log(legacy_log_rows)

        self.vault.append_audit(
            [
                build_audit_entry(
                    "reflection_applied",
                    "reflection",
                    {
                        "session_id": session_id,
                        "turn_count": len(transcript),
                        "profile_keys": sorted(profile_updates.keys()),
                        "open_loops": open_loops,
                        "decisions": decisions,
                    },
                    source_session_id=session_id,
                )
            ]
        )
        self.rebuild_index()

    def forget_fact(self, key: str) -> bool:
        profile = self.read_profile()
        if key not in profile:
            return False
        profile.pop(key, None)
        confidence = dict(profile.get("_confidence", {}))
        confidence.pop(key, None)
        history = dict(profile.get("_history", {}))
        history.pop(key, None)
        if confidence:
            profile["_confidence"] = confidence
        if history:
            profile["_history"] = history
        self.vault.write_profile(profile)
        self.vault.add_audit_event("profile_key_forgotten", "semantic", {"key": key})
        self.rebuild_index()
        return True

    def clear_user_memory(self) -> str:
        archive_name = self.vault.clear_all()
        self.index.reset()
        self.rebuild_index()
        self.vault.add_audit_event("memory_cleared", "system", {"archive": archive_name})
        return archive_name
