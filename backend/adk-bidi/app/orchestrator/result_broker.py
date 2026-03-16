"""Async result broker for routing completed task results back into the spine."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from google.adk.plugins.base_plugin import BasePlugin

from app.orchestrator.contracts import TaskResult

log = logging.getLogger("athena.orchestrator.result_broker")

TaskResultHandler = Callable[[TaskResult], Awaitable[None]]


class ResultBroker:
    """Queues completed task results for orchestrator review."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TaskResult] = asyncio.Queue()
        self._drain_task: asyncio.Task | None = None
        self._handler: TaskResultHandler | None = None

    async def startup(self, handler: TaskResultHandler) -> None:
        self._handler = handler
        self._drain_task = asyncio.create_task(self._drain(), name="result-broker-drain")

    async def shutdown(self) -> None:
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

    async def publish(self, result: TaskResult) -> None:
        await self._queue.put(result)

    async def _drain(self) -> None:
        while True:
            try:
                result = await self._queue.get()
                try:
                    if self._handler is not None:
                        await self._handler(result)
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Unexpected error draining result broker queue")


class ResultBrokerPlugin(BasePlugin):
    """ADK plugin hook for future spoke-native result publishing."""

    def __init__(
        self,
        broker: ResultBroker,
        *,
        agent_name: str = "WorkspaceOrchestrator",
    ) -> None:
        super().__init__(name="athena_result_broker")
        self._broker = broker
        self._agent_name = agent_name

    async def after_agent_callback(self, *, agent, callback_context):
        del callback_context
        if getattr(agent, "name", "") != self._agent_name:
            return None

        response = getattr(agent, "response", None)
        if response is None:
            return None

        try:
            result = TaskResult.model_validate(response)
        except Exception:
            return None

        await self._broker.publish(result)
        return None
