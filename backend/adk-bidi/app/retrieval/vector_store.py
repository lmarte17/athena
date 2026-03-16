"""SQLite-backed persistent vector index for workspace content.

Stores chunk embeddings as binary blobs and does in-memory cosine similarity
search via numpy. Fast enough for personal workspaces (< 50K chunks).

DB path: ~/.athena/workspace_index.db  (override via ATHENA_INDEX_DB)
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import numpy as np

log = logging.getLogger("athena.retrieval.vector_store")

_DEFAULT_DB = Path("~/.athena/workspace_index.db").expanduser()
_DB_PATH = Path(os.getenv("ATHENA_INDEX_DB", str(_DEFAULT_DB)))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title       TEXT,
    section     TEXT,
    chunk_text  TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    indexed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(source_type);
"""


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str          # "{source_id}:{index}"
    source_id: str
    source_type: str       # "drive" | "gmail" | "docs" | etc.
    title: str
    section: str
    chunk_text: str
    embedding: list[float]


@dataclass(frozen=True)
class ChunkResult:
    chunk_id: str
    source_id: str
    source_type: str
    title: str
    section: str
    chunk_text: str
    score: float           # cosine similarity [0, 1]


def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.frombuffer(blob, dtype=np.float32, count=n)


class VectorStore:
    """Persistent SQLite vector store with cosine similarity search."""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path

    async def startup(self) -> None:
        """Initialize the DB and run migrations."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        log.info("VectorStore initialized at %s", self._db_path)

    async def upsert_chunks(self, source_id: str, chunks: list[ChunkRecord]) -> None:
        """Replace all chunks for a given source_id, then insert the new ones."""
        if not chunks:
            await self.delete_source(source_id)
            return

        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                c.chunk_id,
                c.source_id,
                c.source_type,
                c.title,
                c.section,
                c.chunk_text,
                _vec_to_blob(c.embedding),
                now,
            )
            for c in chunks
        ]

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
            await db.executemany(
                """
                INSERT INTO chunks
                    (chunk_id, source_id, source_type, title, section, chunk_text, embedding, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await db.commit()

        log.debug("Upserted %d chunks for source_id=%s", len(chunks), source_id)

    async def delete_source(self, source_id: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
            await db.commit()

    async def search(
        self,
        query_vec: list[float],
        top_k: int = 5,
        source_type: str | None = None,
    ) -> list[ChunkResult]:
        """Cosine similarity search over all indexed chunks.

        Loads all embeddings into numpy for vectorized computation.
        Returns top_k results sorted by descending score.
        """
        sql = "SELECT chunk_id, source_id, source_type, title, section, chunk_text, embedding FROM chunks"
        params: tuple[Any, ...] = ()
        if source_type:
            sql += " WHERE source_type = ?"
            params = (source_type,)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return []

        # Build numpy matrix for batch cosine similarity
        q = np.array(query_vec, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm < 1e-9:
            return []

        embeddings = np.stack([_blob_to_vec(row["embedding"]) for row in rows])
        # Cosine similarity: dot(q, E.T) / (|q| * |E|)
        dots = embeddings @ q
        norms = np.linalg.norm(embeddings, axis=1)
        scores = np.where(norms > 1e-9, dots / (norms * q_norm), 0.0)

        top_indices = np.argpartition(scores, -min(top_k, len(scores)))[-top_k:]
        top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            row = rows[idx]
            results.append(
                ChunkResult(
                    chunk_id=row["chunk_id"],
                    source_id=row["source_id"],
                    source_type=row["source_type"],
                    title=row["title"] or "",
                    section=row["section"] or "",
                    chunk_text=row["chunk_text"],
                    score=float(scores[idx]),
                )
            )

        return results

    async def count(self) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM chunks") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
