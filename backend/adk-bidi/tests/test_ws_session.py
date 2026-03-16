import asyncio

import pytest

from app.ws_session import IdleReflectionTracker


@pytest.mark.asyncio
async def test_idle_reflection_tracker_flushes_on_idle():
    callbacks: list[list[dict]] = []

    async def on_idle(turns: list[dict]) -> None:
        callbacks.append(turns)

    tracker = IdleReflectionTracker(
        session_id="session-1",
        idle_seconds=0.02,
        on_idle_reflection=on_idle,
    )

    assert tracker.record_turn("u1", "a1")
    await asyncio.sleep(0.06)

    assert len(callbacks) == 1
    assert callbacks[0] == [{"user": "u1", "athena": "a1"}]
    assert tracker.pending_turn_count() == 0


@pytest.mark.asyncio
async def test_idle_reflection_tracker_resets_timer_on_new_turn():
    callbacks: list[list[dict]] = []

    async def on_idle(turns: list[dict]) -> None:
        callbacks.append(turns)

    tracker = IdleReflectionTracker(
        session_id="session-2",
        idle_seconds=0.05,
        on_idle_reflection=on_idle,
    )

    tracker.record_turn("u1", "a1")
    await asyncio.sleep(0.03)
    tracker.record_turn("u2", "a2")
    await asyncio.sleep(0.07)

    assert len(callbacks) == 1
    assert callbacks[0] == [
        {"user": "u1", "athena": "a1"},
        {"user": "u2", "athena": "a2"},
    ]


@pytest.mark.asyncio
async def test_idle_reflection_tracker_finalize_returns_pending_and_cancels_idle():
    callbacks: list[list[dict]] = []

    async def on_idle(turns: list[dict]) -> None:
        callbacks.append(turns)

    tracker = IdleReflectionTracker(
        session_id="session-3",
        idle_seconds=0.2,
        on_idle_reflection=on_idle,
    )

    tracker.record_turn("u1", "a1")
    pending = await tracker.finalize()

    assert pending == [{"user": "u1", "athena": "a1"}]
    await asyncio.sleep(0.25)
    assert callbacks == []


@pytest.mark.asyncio
async def test_idle_reflection_tracker_ignores_empty_turn():
    callbacks: list[list[dict]] = []

    async def on_idle(turns: list[dict]) -> None:
        callbacks.append(turns)

    tracker = IdleReflectionTracker(
        session_id="session-4",
        idle_seconds=0.02,
        on_idle_reflection=on_idle,
    )

    assert not tracker.record_turn("", "")
    assert tracker.idle_task is None
    await asyncio.sleep(0.05)
    assert callbacks == []
