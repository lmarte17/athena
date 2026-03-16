from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from app.memory_v2.governance import json_dumps, make_id, utc_now
from app.memory_v2.models import CandidateMemory, MemorySearchHit

log = logging.getLogger("athena.memory_v2.index")


class MemoryIndex:
    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    doc_key TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    doc_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts
                USING fts5(doc_key UNINDEXED, namespace UNINDEXED, title, content);

                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    text TEXT NOT NULL,
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 0.7,
                    approval_status TEXT NOT NULL,
                    source_session_id TEXT,
                    source_turn INTEGER,
                    created_at TEXT NOT NULL,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    entities_json TEXT NOT NULL DEFAULT '[]',
                    relations_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS entities (
                    entity_name TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entity_mentions (
                    entity_name TEXT NOT NULL,
                    doc_key TEXT NOT NULL,
                    PRIMARY KEY (entity_name, doc_key)
                );

                CREATE TABLE IF NOT EXISTS relations (
                    relation_id TEXT PRIMARY KEY,
                    src_entity TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    dst_entity TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                ("schema_version", "2"),
            )

    def reset(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                DELETE FROM documents;
                DELETE FROM docs_fts;
                DELETE FROM candidates;
                DELETE FROM entities;
                DELETE FROM entity_mentions;
                DELETE FROM relations;
                """
            )

    def rebuild_documents(self, docs: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM documents")
            conn.execute("DELETE FROM docs_fts")
            conn.execute("DELETE FROM entity_mentions")
            conn.execute("DELETE FROM entities")
            conn.execute("DELETE FROM relations")
            for doc in docs:
                self.upsert_document(doc, conn=conn)

    def upsert_document(self, doc: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> None:
        owns_conn = conn is None
        conn = conn or self._connect()
        metadata = dict(doc.get("metadata") or {})
        conn.execute(
            """
            INSERT OR REPLACE INTO documents (
                doc_key, namespace, doc_type, title, path, content, metadata_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc["doc_key"],
                doc["namespace"],
                doc["doc_type"],
                doc.get("title") or doc["doc_key"],
                doc.get("path") or "",
                doc.get("content") or "",
                json_dumps(metadata),
                utc_now(),
            ),
        )
        conn.execute("DELETE FROM docs_fts WHERE doc_key = ?", (doc["doc_key"],))
        conn.execute(
            "INSERT INTO docs_fts(doc_key, namespace, title, content) VALUES (?, ?, ?, ?)",
            (
                doc["doc_key"],
                doc["namespace"],
                doc.get("title") or doc["doc_key"],
                doc.get("content") or "",
            ),
        )
        for entity in doc.get("entities", []):
            conn.execute(
                """
                INSERT OR REPLACE INTO entities(entity_name, entity_type, metadata_json, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (entity["name"], entity["type"], json_dumps(entity.get("metadata") or {}), utc_now()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO entity_mentions(entity_name, doc_key) VALUES(?, ?)",
                (entity["name"], doc["doc_key"]),
            )
        for relation in doc.get("relations", []):
            conn.execute(
                """
                INSERT OR REPLACE INTO relations(
                    relation_id, src_entity, predicate, dst_entity, metadata_json, created_at, active
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    relation.get("id") or make_id("rel"),
                    relation["src"],
                    relation["predicate"],
                    relation["dst"],
                    json_dumps(relation.get("metadata") or {}),
                    relation.get("created_at") or utc_now(),
                ),
            )
        if owns_conn:
            conn.commit()
            conn.close()

    def stage_candidate(self, candidate: CandidateMemory) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO candidates(
                    candidate_id, type, namespace, text, structured_json, confidence,
                    approval_status, source_session_id, source_turn, created_at, sensitive,
                    keywords_json, entities_json, relations_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.id,
                    candidate.type,
                    candidate.namespace,
                    candidate.text,
                    json_dumps(candidate.structured),
                    candidate.confidence,
                    candidate.approval_status,
                    candidate.source_session_id,
                    candidate.source_turn,
                    candidate.created_at or utc_now(),
                    1 if candidate.sensitive else 0,
                    json_dumps(candidate.keywords),
                    json_dumps(candidate.entity_refs),
                    json_dumps(candidate.relation_refs),
                ),
            )

    def remove_candidates(self, candidate_ids: set[str]) -> None:
        if not candidate_ids:
            return
        placeholders = ",".join("?" for _ in candidate_ids)
        with self._connect() as conn:
            conn.execute(
                f"DELETE FROM candidates WHERE candidate_id IN ({placeholders})",
                tuple(candidate_ids),
            )

    def list_candidates(self, *, source_session_id: str | None = None, approval_status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM candidates WHERE 1=1"
        params: list[Any] = []
        if source_session_id:
            query += " AND source_session_id = ?"
            params.append(source_session_id)
        if approval_status:
            query += " AND approval_status = ?"
            params.append(approval_status)
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_candidate_status(self, candidate_id: str, approval_status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE candidates SET approval_status = ? WHERE candidate_id = ?",
                (approval_status, candidate_id),
            )

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        namespaces: list[str] | None = None,
    ) -> list[MemorySearchHit]:
        tokens = [token for token in query.replace(":", " ").split() if token]
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{token}"' for token in tokens[:8])
        where = ""
        params: list[Any] = [fts_query]
        if namespaces:
            where = f" AND d.namespace IN ({','.join('?' for _ in namespaces)})"
            params.extend(namespaces)
        params.append(limit)

        sql = (
            """
            SELECT d.doc_key, d.namespace, d.title, d.path, d.content, d.metadata_json,
                   bm25(docs_fts) AS score
            FROM docs_fts
            JOIN documents d ON d.doc_key = docs_fts.doc_key
            WHERE docs_fts MATCH ?
            """
            + where
            + """
            ORDER BY score
            LIMIT ?
            """
        )
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            return self._fallback_like_search(query, limit=limit, namespaces=namespaces)

        hits = [self._row_to_hit(row) for row in rows]
        return self._expand_graph_hits(query, hits, limit=limit)

    def _fallback_like_search(
        self,
        query: str,
        *,
        limit: int,
        namespaces: list[str] | None,
    ) -> list[MemorySearchHit]:
        sql = "SELECT * FROM documents WHERE content LIKE ?"
        params: list[Any] = [f"%{query}%"]
        if namespaces:
            sql += f" AND namespace IN ({','.join('?' for _ in namespaces)})"
            params.extend(namespaces)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_hit(row, fallback_score=0.0) for row in rows]

    def _expand_graph_hits(
        self,
        query: str,
        hits: list[MemorySearchHit],
        *,
        limit: int,
    ) -> list[MemorySearchHit]:
        seen = {hit.doc_key for hit in hits}
        entities = self._entity_matches(query)
        if not entities or len(hits) >= limit:
            return hits
        with self._connect() as conn:
            related_rows = conn.execute(
                """
                SELECT d.doc_key, d.namespace, d.title, d.path, d.content, d.metadata_json
                FROM entity_mentions em
                JOIN documents d ON d.doc_key = em.doc_key
                WHERE em.entity_name IN (
                    SELECT DISTINCT dst_entity FROM relations WHERE src_entity IN ({placeholders})
                    UNION
                    SELECT DISTINCT src_entity FROM relations WHERE dst_entity IN ({placeholders})
                )
                LIMIT ?
                """.format(placeholders=",".join("?" for _ in entities)),
                tuple(entities + entities + [max(limit * 2, 10)]),
            ).fetchall()
        for row in related_rows:
            if row["doc_key"] in seen:
                continue
            hits.append(self._row_to_hit(row, fallback_score=5.0))
            seen.add(row["doc_key"])
            if len(hits) >= limit:
                break
        return hits[:limit]

    def _entity_matches(self, query: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_name FROM entities WHERE lower(entity_name) LIKE ? LIMIT 5",
                (f"%{query.lower()}%",),
            ).fetchall()
        return [row["entity_name"] for row in rows]

    def _row_to_hit(self, row: sqlite3.Row, fallback_score: float | None = None) -> MemorySearchHit:
        content = row["content"]
        snippet = content[:280].strip()
        metadata_raw = row["metadata_json"] if "metadata_json" in row.keys() else "{}"
        try:
            metadata = json.loads(metadata_raw)
        except Exception:
            metadata = {}
        score = fallback_score if fallback_score is not None else abs(float(row["score"]))
        return MemorySearchHit(
            doc_key=row["doc_key"],
            namespace=row["namespace"],
            title=row["title"],
            path=row["path"],
            snippet=snippet,
            score=score,
            metadata=metadata,
        )
