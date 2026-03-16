"""Hybrid workspace backend provider.

Uses gog for Athena's existing Workspace coverage and slides-agent for
advanced Google Slides inspection/editing only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.tools.gog_backend import GogWorkspaceBackend
from app.tools.slides_agent_backend import SlidesAgentWorkspaceBackend


@dataclass(slots=True)
class HybridWorkspaceBackend:
    """Route existing Workspace calls to gog and advanced Slides calls to slides-agent."""

    gog: GogWorkspaceBackend
    slides_agent: SlidesAgentWorkspaceBackend

    def __getattr__(self, name: str) -> Any:
        if hasattr(self.gog, name):
            return getattr(self.gog, name)
        return getattr(self.slides_agent, name)


_default_backend: HybridWorkspaceBackend | None = None


def get_workspace_backend() -> HybridWorkspaceBackend:
    global _default_backend
    if _default_backend is None:
        _default_backend = HybridWorkspaceBackend(
            gog=GogWorkspaceBackend(),
            slides_agent=SlidesAgentWorkspaceBackend(),
        )
    return _default_backend
