"""Async wrapper around the Gemini embedding API.

Uses gemini-embedding-001 with separate task types for queries vs. documents
so that the embedding space is optimized for retrieval.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from google.genai import types as genai_types

from app.tracing import atrace_span, base_metadata, create_gemini_client, finish_span

log = logging.getLogger("athena.retrieval.embedder")

_EMBED_MODEL = os.getenv("ATHENA_EMBED_MODEL", "gemini-embedding-001")
_EMBED_DIM = int(os.getenv("ATHENA_EMBED_DIM", "768"))

# Max texts per batch (API limit)
_BATCH_SIZE = 100


@lru_cache(maxsize=1)
def _client():
    return create_gemini_client(
        "athena.retrieval.embedder",
        model=_EMBED_MODEL,
        tags=["retrieval", "embedding"],
    )


async def embed_query(text: str) -> list[float]:
    """Embed a user query using RETRIEVAL_QUERY task type.

    Returns a 768-dim float vector (or whatever ATHENA_EMBED_DIM is set to).
    Returns a zero vector on error so callers degrade gracefully.
    """
    text = text.strip()
    if not text:
        return [0.0] * _EMBED_DIM
    async with atrace_span(
        "athena.retrieval.embed_query",
        inputs={"text": text},
        metadata=base_metadata(
            component="retrieval.embed_query",
            model=_EMBED_MODEL,
            embedding_dim=_EMBED_DIM,
        ),
        tags=["retrieval", "embedding"],
    ) as run:
        try:
            result = await _client().aio.models.embed_content(
                model=_EMBED_MODEL,
                contents=text,
                config=genai_types.EmbedContentConfig(
                    task_type="RETRIEVAL_QUERY",
                    output_dimensionality=_EMBED_DIM,
                ),
            )
            emb = result.embeddings[0].values
            values = list(emb)
            finish_span(
                run,
                outputs={
                    "embedding_dim": len(values),
                    "zero_fallback": False,
                },
            )
            return values
        except Exception as exc:
            log.warning("embed_query failed for text=%r", text[:80], exc_info=True)
            finish_span(run, error=str(exc))
            return [0.0] * _EMBED_DIM


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed a list of document chunks using RETRIEVAL_DOCUMENT task type.

    Batches requests to stay within API limits.
    Returns a list of 768-dim float vectors, one per input text.
    Failed embeddings are returned as zero vectors.
    """
    if not texts:
        return []

    async with atrace_span(
        "athena.retrieval.embed_documents",
        inputs={
            "text_count": len(texts),
            "batch_size": _BATCH_SIZE,
        },
        metadata=base_metadata(
            component="retrieval.embed_documents",
            model=_EMBED_MODEL,
            embedding_dim=_EMBED_DIM,
        ),
        tags=["retrieval", "embedding"],
    ) as run:
        results: list[list[float]] = []
        failed_batches = 0
        last_error: str | None = None
        for batch_start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[batch_start : batch_start + _BATCH_SIZE]
            try:
                result = await _client().aio.models.embed_content(
                    model=_EMBED_MODEL,
                    contents=batch,
                    config=genai_types.EmbedContentConfig(
                        task_type="RETRIEVAL_DOCUMENT",
                        output_dimensionality=_EMBED_DIM,
                    ),
                )
                for emb in result.embeddings:
                    results.append(list(emb.values))
            except Exception as exc:
                failed_batches += 1
                last_error = str(exc)
                log.warning(
                    "embed_documents batch failed (start=%d, size=%d)",
                    batch_start,
                    len(batch),
                    exc_info=True,
                )
                results.extend([[0.0] * _EMBED_DIM] * len(batch))

        finish_span(
            run,
            outputs={
                "result_count": len(results),
                "failed_batches": failed_batches,
                "zero_fallback": failed_batches > 0,
                "last_error": last_error,
            },
        )
        return results
