"""System and debug HTTP routes."""

from fastapi import APIRouter

from app.dependencies import session_manager
from app.tracing import tracing_status

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/debug")
async def debug():
    """Diagnostic endpoint — confirms startup ran and shows tap queue state."""
    drain = session_manager._drain_task
    return {
        "startup_ran": drain is not None,
        "drain_task_running": drain is not None and not drain.done(),
        "tap_queue_size": session_manager._tap_queue.qsize(),
        "subscriber_count": len(session_manager._subscribers),
        "tracing": tracing_status().to_dict(),
    }
