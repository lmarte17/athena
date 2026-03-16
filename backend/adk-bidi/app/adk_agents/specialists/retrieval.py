"""RetrievalAgent — ADK specialist for selecting and summarizing session resources.

Handles follow-up queries against resources already stored in SessionResourceStore.
Uses semantic (embedding-based) chunk ranking for retrieval — tolerates transcription
errors and paraphrasing that would trip up keyword matching.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.job_workspace import JobWorkspaceStore
from app.resource_store import SessionResourceStore
from app.tools.job_workspace_tools import build_job_workspace_tools

if TYPE_CHECKING:
    from app.retrieval import SemanticRetrieval

log = logging.getLogger("athena.adk_agents.specialists.retrieval")

_MODEL = os.getenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview")

_RETRIEVAL_INSTRUCTION = """\
You are a retrieval specialist. Your job is to find relevant content from
resources already loaded in this session's workspace store.

Use `list_session_resources` to see what content is available.
Use `get_resource_content` to fetch the full text of a specific resource.
Use `select_relevant_chunks` to get the most relevant passages for the user's question.
Use `semantic_search_workspace` to search across ALL indexed workspace content
(not just current session) — useful when the user asks about something that may
have been loaded in a previous session.
Use `get_job_workspace_state` when the request sounds like a continuation or correction of
recent job work. Reuse resource IDs and scratchpad state before searching broadly again.

Return a structured result:
{
  "summary": "<1–2 sentence answer or summary>",
  "artifacts": [
    {
      "type": "retrieved_excerpt",
      "id": "<resource_id>",
      "title": "<resource title>",
      "content": "<relevant excerpt>"
    }
  ],
  "follow_up_questions": ["<related question>"],
  "resource_handles": []
}

If no relevant content is available, say so clearly in the summary.
"""


def _build_retrieval_tools(
    resource_store: SessionResourceStore,
    session_id: str,
    semantic: "SemanticRetrieval | None",
    workspace_store: JobWorkspaceStore | None = None,
    job_id: str = "",
):
    """Build retrieval tool functions closed over a specific session."""

    async def list_session_resources() -> dict[str, Any]:
        """List all resources available in the current session workspace store.

        Returns:
            Dict with 'resources' list — id, title, source, status for each.
        """
        snapshots = resource_store.list_snapshots(session_id)
        return {
            "resources": [
                {
                    "id": s.handle.id,
                    "title": s.handle.title,
                    "source": s.handle.source,
                    "kind": s.handle.kind,
                    "status": s.status,
                    "url": s.handle.url,
                }
                for s in snapshots
            ]
        }

    async def get_resource_content(resource_id: str) -> dict[str, Any]:
        """Get the full text content of a resource by ID.

        Args:
            resource_id: The resource ID from list_session_resources.

        Returns:
            Dict with 'content' (full text) or 'error'.
        """
        snapshots = resource_store.list_snapshots(session_id)
        for snap in snapshots:
            if snap.handle.id == resource_id:
                if snap.normalized_text:
                    return {"id": resource_id, "content": snap.normalized_text}
                return {"id": resource_id, "content": "", "status": snap.status}
        return {"id": resource_id, "error": "Resource not found"}

    async def select_relevant_chunks(
        resource_id: str,
        query: str,
        max_chunks: int = 5,
    ) -> dict[str, Any]:
        """Select the most relevant text chunks from a resource for a query.

        Uses semantic similarity (embedding-based) so it works even when the
        query uses different wording or contains transcription errors.

        Args:
            resource_id: The resource ID to search within.
            query: The user's question or search term (voice transcript OK).
            max_chunks: Maximum number of chunks to return (default 5).

        Returns:
            Dict with 'chunks' list of relevant passages.
        """
        snapshots = resource_store.list_snapshots(session_id)
        for snap in snapshots:
            if snap.handle.id != resource_id:
                continue
            if not snap.chunks:
                if snap.normalized_text:
                    return {"chunks": [snap.normalized_text[:2000]]}
                return {"chunks": [], "status": snap.status}

            if semantic is not None:
                ranked = await semantic.rerank_chunks(query, snap.chunks)
                return {"chunks": ranked[:max_chunks]}

            # Fallback: return first N chunks if semantic unavailable
            return {"chunks": snap.chunks[:max_chunks]}

        return {"chunks": [], "error": "Resource not found"}

    async def semantic_search_workspace(
        query: str,
        top_k: int = 5,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        """Search across ALL indexed workspace content using semantic similarity.

        Unlike select_relevant_chunks (which searches within a specific resource),
        this searches the persistent index that spans all resources ever loaded.
        Handles voice transcription errors and paraphrasing well.

        Args:
            query: Natural language query — voice transcript, question, or search phrase.
            top_k: Number of top results to return (default 5).
            source_type: Optional filter: "drive", "gmail", "docs", "calendar", etc.

        Returns:
            Dict with 'results' list, each containing title, section, excerpt, score.
            Returns 'index_empty' flag if no content has been indexed yet.
        """
        if semantic is None:
            return {"results": [], "error": "Semantic search not available"}

        results = await semantic.search_chunks(query, top_k=top_k, source_type=source_type)
        if not results:
            count = await semantic.store.count()
            return {
                "results": [],
                "index_empty": count == 0,
                "hint": "No indexed content found. Try loading the resource first.",
            }

        return {
            "results": [
                {
                    "source_id": r.source_id,
                    "source_type": r.source_type,
                    "title": r.title,
                    "section": r.section,
                    "excerpt": r.chunk_text[:500],
                    "score": round(r.score, 4),
                }
                for r in results
            ]
        }

    tools = [
        FunctionTool(list_session_resources),
        FunctionTool(get_resource_content),
        FunctionTool(select_relevant_chunks),
        FunctionTool(semantic_search_workspace),
    ]
    tools.extend(
        build_job_workspace_tools(
            workspace_store,
            session_id=session_id,
            job_id=job_id,
        )
    )
    return tools


def build_retrieval_agent(
    resource_store: SessionResourceStore,
    session_id: str,
    semantic: "SemanticRetrieval | None" = None,
    workspace_store: JobWorkspaceStore | None = None,
    *,
    job_id: str = "",
) -> LlmAgent:
    """Build the Retrieval specialist LlmAgent for a specific session."""
    tools = _build_retrieval_tools(
        resource_store,
        session_id,
        semantic,
        workspace_store=workspace_store,
        job_id=job_id,
    )

    return LlmAgent(
        name="retrieval_specialist",
        model=_MODEL,
        instruction=_RETRIEVAL_INSTRUCTION,
        tools=tools,
        output_key="retrieval_result",
    )
