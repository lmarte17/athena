"""Indexes workspace resources into the vector store when content becomes ready.

Called as a fire-and-forget background task from resource_store callbacks.
"""

from __future__ import annotations

import logging

from app.resource_store import ResourceSnapshot, SessionResourceStore
from app.retrieval.chunker import chunk_by_headings
from app.retrieval.embedder import embed_documents
from app.retrieval.vector_store import ChunkRecord, VectorStore

log = logging.getLogger("athena.retrieval.indexer")


async def index_resource(
    snapshot: ResourceSnapshot,
    store: VectorStore,
) -> int:
    """Chunk, embed, and persist a content-ready ResourceSnapshot.

    Returns the number of chunks indexed. Returns 0 if the snapshot has no
    usable text or if embedding fails gracefully.
    """
    text = snapshot.normalized_text
    if not text or not text.strip():
        return 0

    source_id = snapshot.handle.id
    title = snapshot.handle.title or ""
    source_type = snapshot.handle.source

    # Chunk by structural headings
    pairs = chunk_by_headings(text)
    if not pairs:
        return 0

    chunk_texts = [chunk_text for (_section, chunk_text) in pairs]
    embeddings = await embed_documents(chunk_texts)

    if len(embeddings) != len(pairs):
        log.warning(
            "Embedding count mismatch for source_id=%s: expected %d, got %d",
            source_id,
            len(pairs),
            len(embeddings),
        )
        return 0

    records = [
        ChunkRecord(
            chunk_id=f"{source_id}:{i}",
            source_id=source_id,
            source_type=source_type,
            title=title,
            section=section,
            chunk_text=chunk_text,
            embedding=emb,
        )
        for i, ((section, chunk_text), emb) in enumerate(zip(pairs, embeddings))
    ]

    await store.upsert_chunks(source_id, records)
    log.info(
        "Indexed %d chunks for %s (%s / %s)",
        len(records),
        title,
        source_type,
        source_id,
    )
    return len(records)


async def index_session(
    session_id: str,
    resource_store: SessionResourceStore,
    store: VectorStore,
) -> int:
    """Index all content-ready resources in a session. Returns total chunks indexed."""
    snapshots = resource_store.list_snapshots(session_id)
    total = 0
    for snap in snapshots:
        if snap.status == "content_ready" and snap.normalized_text:
            total += await index_resource(snap, store)
    return total
