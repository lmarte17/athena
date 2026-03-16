"""Shared types for workspace hydration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.resource_store import ResourceHandle


@dataclass(frozen=True)
class HydrationResult:
    handle: ResourceHandle
    normalized_text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    relations: tuple[ResourceHandle, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata)))
        object.__setattr__(self, "relations", tuple(self.relations))


class ResourceHydrator(Protocol):
    def supports(self, handle: ResourceHandle) -> bool:
        ...

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        ...
