"""Memory management HTTP routes."""

from fastapi import APIRouter, Query

from app.dependencies import session_manager

router = APIRouter()


@router.get("/memory")
async def get_memory():
    """Return the current memory snapshot from Memory V2."""
    return session_manager.memory_service.snapshot().to_dict()


@router.get("/memory/search")
async def search_memory(q: str = Query(..., min_length=2)):
    hits = session_manager.memory_service.search_memory(q, limit=5)
    return {"query": q, "hits": [hit.to_dict() for hit in hits]}


@router.get("/memory/soul")
async def get_soul():
    return {"soul": session_manager.memory_service.snapshot().soul}


@router.post("/memory/clear")
async def clear_memory():
    """Clear user memory deterministically across the V2 vault and indexes."""
    archive_name = session_manager.memory_service.archive_and_reset()
    return {"archived": archive_name, "status": "reset"}


@router.delete("/memory/profile/{key}")
async def forget_profile_key(key: str):
    """Remove a specific key from semantic profile state."""
    existed = session_manager.memory_service.forget_key(key)
    return {"removed": key, "existed": existed}


@router.post("/memory/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: str):
    approved = session_manager.memory_service.approve_candidate(candidate_id)
    return {"candidate_id": candidate_id, "approved": approved}
