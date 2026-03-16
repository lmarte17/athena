"""State helpers for a single WebSocket session."""

import asyncio
from collections.abc import Awaitable, Callable


class IdleReflectionTracker:
    """
    Tracks completed turns for one WS session and runs idle reflection callbacks.

    Behavior:
    - `record_turn()` buffers the turn and restarts the idle timer.
    - When the timer fires, buffered turns are drained and passed to callback.
    - `finalize()` cancels timer and returns any remaining buffered turns.
    """

    def __init__(
        self,
        session_id: str,
        idle_seconds: float,
        on_idle_reflection: Callable[[list[dict]], Awaitable[None]],
    ) -> None:
        self._session_id = session_id
        self._idle_seconds = idle_seconds
        self._on_idle_reflection = on_idle_reflection
        self._turns: list[dict] = []
        self._idle_task: asyncio.Task | None = None

    @property
    def idle_task(self) -> asyncio.Task | None:
        return self._idle_task

    def pending_turn_count(self) -> int:
        return len(self._turns)

    def record_turn(self, transcript_in: str, transcript_out: str) -> bool:
        if not transcript_in and not transcript_out:
            return False
        self._turns.append({
            "user": transcript_in,
            "athena": transcript_out,
        })
        self._schedule_idle_reflection()
        return True

    def cancel_idle_reflection(self) -> None:
        idle = self._idle_task
        if idle and not idle.done():
            idle.cancel()

    async def finalize(self) -> list[dict]:
        self.cancel_idle_reflection()
        return self._drain_turns()

    def _drain_turns(self) -> list[dict]:
        turns = self._turns.copy()
        self._turns.clear()
        return turns

    def _schedule_idle_reflection(self) -> None:
        self.cancel_idle_reflection()

        async def _idle_fire() -> None:
            try:
                await asyncio.sleep(self._idle_seconds)
            except asyncio.CancelledError:
                return
            if self._turns:
                turns = self._drain_turns()
                # Shield the reflection call so a WS-close cancel (from finalize())
                # doesn't abort it mid-flight after turns are already drained.
                await asyncio.shield(self._on_idle_reflection(turns))

        self._idle_task = asyncio.create_task(
            _idle_fire(),
            name=f"idle-reflect-{self._session_id[:8]}",
        )
