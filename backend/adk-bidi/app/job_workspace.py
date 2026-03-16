"""Session-scoped scratchpad/workspace for complex background jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@dataclass
class WorkspaceEntry:
    kind: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "metadata": dict(self.metadata),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


@dataclass
class JobWorkspace:
    job_id: str
    session_id: str
    user_request: str
    status: str = "running"
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    result_summary: str = ""
    step_summaries: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    resource_handles: list[dict[str, Any]] = field(default_factory=list)
    entries: list[WorkspaceEntry] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = _now()

    def generated_resources(self) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for entry in self.entries:
            if entry.kind != "generated_resource":
                continue
            resource_id = str(entry.metadata.get("resource_id") or "").strip()
            source = str(entry.metadata.get("source") or "").strip()
            kind = str(entry.metadata.get("resource_kind") or "").strip()
            if not resource_id or not source or not kind:
                continue
            key = (source, kind, resource_id)
            if key in seen:
                continue
            seen.add(key)
            resources.append(
                {
                    "source": source,
                    "kind": kind,
                    "id": resource_id,
                    "title": str(entry.metadata.get("resource_title") or entry.title or ""),
                    "url": entry.metadata.get("url"),
                    "metadata": dict(entry.metadata.get("handle_metadata") or {}),
                    "dedupe_key": str(entry.metadata.get("dedupe_key") or ""),
                }
            )
        return resources

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "user_request": self.user_request,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "result_summary": self.result_summary,
            "step_summaries": [dict(item) for item in self.step_summaries],
            "artifacts": [dict(item) for item in self.artifacts],
            "resource_handles": [dict(item) for item in self.resource_handles],
            "generated_resources": self.generated_resources(),
            "entries": [entry.to_dict() for entry in self.entries],
        }


class JobWorkspaceStore:
    """In-memory mutable scratchpad for background jobs inside one live session."""

    def __init__(self) -> None:
        self._by_session: dict[str, dict[str, JobWorkspace]] = {}
        self._session_order: dict[str, list[str]] = {}
        self._active_job: dict[str, str] = {}

    def start_job(self, request: WorkspaceJobRequest) -> JobWorkspace:
        session_jobs = self._by_session.setdefault(request.session_id, {})
        workspace = session_jobs.get(request.job_id)
        if workspace is None:
            workspace = JobWorkspace(
                job_id=request.job_id,
                session_id=request.session_id,
                user_request=request.user_request,
            )
            session_jobs[request.job_id] = workspace
            self._session_order.setdefault(request.session_id, []).append(request.job_id)
        else:
            workspace.user_request = request.user_request or workspace.user_request
            workspace.status = "running"
            workspace.touch()
        self._active_job[request.session_id] = request.job_id
        return workspace

    def get_workspace(self, session_id: str, job_id: str | None = None) -> JobWorkspace | None:
        session_jobs = self._by_session.get(session_id, {})
        if job_id and job_id in session_jobs:
            return session_jobs[job_id]
        active = self._active_job.get(session_id)
        if active and active in session_jobs:
            return session_jobs[active]
        order = self._session_order.get(session_id, [])
        for candidate in reversed(order):
            workspace = session_jobs.get(candidate)
            if workspace is not None:
                return workspace
        return None

    def list_recent(self, session_id: str, limit: int = 3) -> list[JobWorkspace]:
        session_jobs = self._by_session.get(session_id, {})
        order = self._session_order.get(session_id, [])
        items: list[JobWorkspace] = []
        for job_id in reversed(order):
            workspace = session_jobs.get(job_id)
            if workspace is not None:
                items.append(workspace)
            if len(items) >= limit:
                break
        return items

    def save_entry(
        self,
        session_id: str,
        job_id: str,
        *,
        kind: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceEntry:
        workspace = self.get_workspace(session_id, job_id)
        if workspace is None:
            raise KeyError(f"Unknown workspace: session={session_id} job={job_id}")
        entry = WorkspaceEntry(
            kind=kind,
            title=title,
            content=content,
            metadata=dict(metadata or {}),
        )
        workspace.entries.append(entry)
        workspace.touch()
        return entry

    def lookup_generated_resource(
        self,
        session_id: str,
        job_id: str,
        *,
        dedupe_key: str,
    ) -> dict[str, Any] | None:
        workspace = self.get_workspace(session_id, job_id)
        if workspace is None or not dedupe_key:
            return None

        for entry in reversed(workspace.entries):
            if entry.kind != "generated_resource":
                continue
            if str(entry.metadata.get("dedupe_key") or "") != dedupe_key:
                continue

            resource_id = str(entry.metadata.get("resource_id") or "").strip()
            if not resource_id:
                continue
            return {
                "source": str(entry.metadata.get("source") or ""),
                "kind": str(entry.metadata.get("resource_kind") or ""),
                "id": resource_id,
                "title": str(entry.metadata.get("resource_title") or entry.title or ""),
                "url": entry.metadata.get("url"),
                "metadata": dict(entry.metadata.get("handle_metadata") or {}),
            }
        return None

    def remember_generated_resource(
        self,
        session_id: str,
        job_id: str,
        *,
        dedupe_key: str,
        source: str,
        kind: str,
        resource_id: str,
        title: str,
        url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace = self.get_workspace(session_id, job_id)
        if workspace is None:
            raise KeyError(f"Unknown workspace: session={session_id} job={job_id}")

        handle_metadata = dict(metadata or {})
        handle_metadata["dedupe_key"] = dedupe_key
        handle = {
            "source": source,
            "kind": kind,
            "id": resource_id,
            "title": title,
            "url": url,
            "metadata": handle_metadata,
        }
        self._merge_resource_handles(workspace, [handle])

        existing = None
        for entry in workspace.entries:
            if entry.kind == "generated_resource" and entry.metadata.get("dedupe_key") == dedupe_key:
                existing = entry
                break

        if existing is None:
            workspace.entries.append(
                WorkspaceEntry(
                    kind="generated_resource",
                    title=title or f"{source}:{kind}",
                    content=f"Created {source}/{kind} [id={resource_id}]",
                    metadata={
                        "dedupe_key": dedupe_key,
                        "source": source,
                        "resource_kind": kind,
                        "resource_id": resource_id,
                        "resource_title": title,
                        "url": url,
                        "handle_metadata": handle_metadata,
                    },
                )
            )
        else:
            existing.title = title or existing.title
            existing.content = f"Created {source}/{kind} [id={resource_id}]"
            existing.metadata = {
                "dedupe_key": dedupe_key,
                "source": source,
                "resource_kind": kind,
                "resource_id": resource_id,
                "resource_title": title,
                "url": url,
                "handle_metadata": handle_metadata,
            }
            existing.updated_at = _now()

        workspace.touch()
        return handle

    def record_step_result(
        self,
        request: WorkspaceJobRequest,
        *,
        step_id: str,
        specialist: str,
        instruction: str,
        output: dict[str, Any],
    ) -> None:
        workspace = self.start_job(request)
        summary = str(output.get("summary") or "")
        workspace.step_summaries.append(
            {
                "step_id": step_id,
                "specialist": specialist,
                "summary": summary,
                "instruction": instruction[:500],
                "recorded_at": _iso(_now()),
            }
        )
        if summary:
            workspace.entries.append(
                WorkspaceEntry(
                    kind="step_result",
                    title=f"{step_id}:{specialist}",
                    content=summary,
                    metadata={"step_id": step_id, "specialist": specialist},
                )
            )
        self._merge_artifacts(workspace, output.get("artifacts") or [])
        self._merge_resource_handles(workspace, output.get("resource_handles") or [])
        workspace.touch()

    def record_job_result(self, request: WorkspaceJobRequest, result: WorkspaceJobResult) -> None:
        workspace = self.start_job(request)
        workspace.status = result.status
        workspace.result_summary = result.summary or workspace.result_summary
        if result.summary:
            workspace.entries.append(
                WorkspaceEntry(
                    kind="result",
                    title="job_result",
                    content=result.summary,
                    metadata={"status": result.status},
                )
            )
        self._merge_artifacts(workspace, result.artifacts)
        self._merge_resource_handles(workspace, result.resource_handles)
        if result.error:
            workspace.entries.append(
                WorkspaceEntry(
                    kind="error",
                    title="job_error",
                    content=result.error,
                    metadata={"status": result.status},
                )
            )
        workspace.touch()
        self._active_job[request.session_id] = request.job_id

    def payload(self, session_id: str, job_id: str | None = None, *, recent_limit: int = 2) -> dict[str, Any]:
        current = self.get_workspace(session_id, job_id)
        recent = [workspace.to_dict() for workspace in self.list_recent(session_id, limit=recent_limit)]
        return {
            "current": current.to_dict() if current else None,
            "recent": recent,
        }

    def render_context(
        self,
        session_id: str,
        *,
        query: str = "",
        limit: int = 2,
        max_chars: int = 1800,
    ) -> str:
        workspaces = self._rank_recent(session_id, query=query, limit=limit)
        if not workspaces:
            return ""

        parts: list[str] = ["Recent job workspace you can reuse if relevant:"]
        for workspace in workspaces:
            parts.append(f"- Job {workspace.job_id[:8]}: {workspace.user_request}")
            if workspace.result_summary:
                parts.append(f"  Result: {workspace.result_summary}")
            for resource in workspace.generated_resources()[-3:]:
                source = resource.get("source", "resource")
                kind = resource.get("kind", "")
                title = resource.get("title", "")
                ident = resource.get("id", "")
                parts.append(f"  Generated: {source}/{kind} {title} [id={ident}]")
            handles = workspace.resource_handles[-3:]
            for handle in handles:
                source = handle.get("source", "resource")
                kind = handle.get("kind", "")
                title = handle.get("title", "")
                ident = handle.get("id", "")
                parts.append(f"  Resource: {source}/{kind} {title} [id={ident}]")
            for entry in workspace.entries[-5:]:
                preview = " ".join(entry.content.split())[:200]
                parts.append(f"  Scratchpad {entry.kind} {entry.title}: {preview}")

        rendered = "\n".join(parts)
        if len(rendered) > max_chars:
            return rendered[: max_chars - 3].rstrip() + "..."
        return rendered

    def clear_session(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)
        self._session_order.pop(session_id, None)
        self._active_job.pop(session_id, None)

    def _merge_artifacts(self, workspace: JobWorkspace, artifacts: list[dict[str, Any]]) -> None:
        existing_keys = {
            (artifact.get("type"), artifact.get("id"), artifact.get("title"))
            for artifact in workspace.artifacts
        }
        for artifact in artifacts:
            key = (artifact.get("type"), artifact.get("id"), artifact.get("title"))
            if key in existing_keys:
                continue
            workspace.artifacts.append(dict(artifact))
            existing_keys.add(key)
            content = str(artifact.get("content") or "").strip()
            if content:
                workspace.entries.append(
                    WorkspaceEntry(
                        kind="artifact",
                        title=str(artifact.get("title") or artifact.get("type") or "artifact"),
                        content=content[:1000],
                        metadata={"artifact_type": artifact.get("type"), "artifact_id": artifact.get("id")},
                    )
                )

    def _merge_resource_handles(self, workspace: JobWorkspace, handles: list[dict[str, Any]]) -> None:
        def _to_dict(h: Any) -> dict[str, Any]:
            if isinstance(h, str):
                return {"id": h}
            return dict(h)

        existing_keys = {
            (handle.get("source"), handle.get("kind"), handle.get("id"))
            for handle in (_to_dict(h) for h in workspace.resource_handles)
        }
        for handle in handles:
            handle = _to_dict(handle)
            key = (handle.get("source"), handle.get("kind"), handle.get("id"))
            if key in existing_keys:
                continue
            workspace.resource_handles.append(handle)
            existing_keys.add(key)

    def _rank_recent(self, session_id: str, *, query: str, limit: int) -> list[JobWorkspace]:
        candidates = self.list_recent(session_id, limit=max(limit * 2, 4))
        if not candidates:
            return []
        if not query.strip():
            return candidates[:limit]

        lowered = query.lower()

        def score(workspace: JobWorkspace) -> tuple[int, float]:
            score_value = 0
            haystacks = [
                workspace.user_request.lower(),
                workspace.result_summary.lower(),
                " ".join(str(item.get("title") or "").lower() for item in workspace.artifacts),
                " ".join(str(item.get("title") or "").lower() for item in workspace.resource_handles),
            ]
            for term in ["spreadsheet", "sheet", "table"]:
                if term in lowered and any(term in hay for hay in haystacks):
                    score_value += 3
            for term in ["document", "doc"]:
                if term in lowered and any(term in hay for hay in haystacks):
                    score_value += 3
            for term in ["missing", "add", "fix", "edit", "update", "revise", "complete"]:
                if term in lowered:
                    score_value += 2
            if workspace.resource_handles:
                score_value += 1
            return (score_value, workspace.updated_at.timestamp())

        ranked = sorted(candidates, key=score, reverse=True)
        return ranked[:limit]


def format_json_for_workspace(raw: str) -> str:
    parsed = json.loads(raw)
    return json.dumps(parsed, indent=2, sort_keys=True)
