from __future__ import annotations

import pytest

from app.orchestrator.contracts import TaskResult, TaskStatus
from app.orchestrator.result_broker import ResultBroker


@pytest.mark.asyncio
async def test_result_broker_drains_published_task_results():
    broker = ResultBroker()
    received = []

    async def handler(result: TaskResult) -> None:
        received.append(result.task_id)

    await broker.startup(handler)
    try:
        await broker.publish(
            TaskResult(
                task_id="task-1",
                session_id="session-1",
                status=TaskStatus.completed,
                summary="Done",
            )
        )
        for _ in range(20):
            if received:
                break
            await __import__("asyncio").sleep(0.01)
    finally:
        await broker.shutdown()

    assert received == ["task-1"]
