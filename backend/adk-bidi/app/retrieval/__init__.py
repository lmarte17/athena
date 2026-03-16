"""Semantic retrieval layer for Athena.

Provides embedding-based chunk search as a replacement for keyword overlap
scoring throughout the retrieval stack.

Usage:
    semantic = SemanticRetrieval()
    await semantic.startup()

    # Search across all indexed workspace content
    results = await semantic.search_chunks("hardware requirements for migration")

    # Rerank an in-memory list of chunk texts (no persistent store needed)
    ranked = await semantic.rerank_chunks("hardware bill of materials", chunks)
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from app.retrieval.embedder import embed_documents, embed_query
from app.retrieval.indexer import index_resource, index_session
from app.retrieval.vector_store import ChunkResult, VectorStore

log = logging.getLogger("athena.retrieval")


class SemanticRetrieval:
    """Facade that wires together the embedder, vector store, and indexer."""

    def __init__(self) -> None:
        self._store = VectorStore()

    async def startup(self) -> None:
        await self._store.startup()
        count = await self._store.count()
        log.info("SemanticRetrieval ready — %d chunks in index", count)

    @property
    def store(self) -> VectorStore:
        return self._store

    async def search_chunks(
        self,
        query: str,
        top_k: int = 5,
        source_type: str | None = None,
    ) -> list[ChunkResult]:
        """Semantic search over all indexed workspace chunks.

        Args:
            query: Natural language query (voice transcript, typed, etc.)
            top_k: Number of top results to return.
            source_type: Optional filter ("drive", "gmail", "docs", etc.)

        Returns:
            List of ChunkResult sorted by descending cosine similarity.
        """
        query_vec = await embed_query(query)
        return await self._store.search(query_vec, top_k=top_k, source_type=source_type)

    async def rerank_chunks(self, query: str, chunks: list[str]) -> list[str]:
        """Rerank an in-memory list of chunk texts by semantic similarity.

        Does NOT require chunks to be in the persistent store.
        Returns chunks sorted by descending cosine similarity to query.
        Falls back to the original order if embedding fails.
        """
        if not chunks:
            return []
        if len(chunks) == 1:
            return chunks

        try:
            query_vec, chunk_vecs = await asyncio.gather(
                embed_query(query),
                embed_documents(chunks),
            )
        except Exception:
            log.warning("rerank_chunks: embedding failed, keeping original order", exc_info=True)
            return chunks

        q = np.array(query_vec, dtype=np.float32)
        q_norm = float(np.linalg.norm(q))
        if q_norm < 1e-9 or not chunk_vecs:
            return chunks

        matrix = np.array(chunk_vecs, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        dots = matrix @ q
        scores = np.where(norms > 1e-9, dots / (norms * q_norm), 0.0)

        ranked_indices = np.argsort(scores)[::-1]
        return [chunks[i] for i in ranked_indices]

    async def index_resource_snapshot(self, snapshot) -> int:  # type: ignore[no-untyped-def]
        """Index a content-ready ResourceSnapshot. Returns chunk count."""
        return await index_resource(snapshot, self._store)
