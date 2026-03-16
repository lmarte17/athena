"""WorkspaceCoordinatorAgent — headless ADK agent that routes workspace jobs to specialists.

The coordinator:
1. Receives a WorkspaceJobRequest
2. Uses LLM reasoning to decide which specialist(s) to invoke
3. Delegates via AgentTool
4. Collects results and normalizes into a WorkspaceJobResult

The coordinator runs in a fresh InMemorySession per job — not the live voice session.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from app.job_workspace import JobWorkspaceStore
from app.adk_agents.specialists.action import build_action_agent
from app.adk_agents.specialists.calendar import build_calendar_agent
from app.adk_agents.specialists.docs import build_docs_agent
from app.adk_agents.specialists.drive import build_drive_agent
from app.adk_agents.specialists.gmail import build_gmail_agent
from app.adk_agents.specialists.netbox import build_netbox_agent
from app.adk_agents.specialists.sheets import build_sheets_agent
from app.adk_agents.specialists.slides import build_slides_agent
from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
from app.resource_store import SessionResourceStore
from app.tracing import atrace_span, base_metadata, finish_span

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.retrieval import SemanticRetrieval

log = logging.getLogger("athena.adk_agents.coordinator")

_COORDINATOR_MODEL = os.getenv("ATHENA_COORDINATOR_MODEL", "gemini-3.1-pro-preview")
_APP_NAME = "athena_coordinator"
_USER_ID = "coordinator"

_COORDINATOR_INSTRUCTION = """\
You are Athena's workspace coordinator. You receive background job requests and
delegate them to specialist agents, decomposing complex requests into ordered steps.

## Available specialists

- `gmail_specialist`      — search, read, send, draft, archive, and organize Gmail
- `drive_specialist`      — search, list, copy, move, rename, share, and organize Drive files
- `docs_specialist`       — read, create, write, edit, and copy Google Docs
- `calendar_specialist`   — list, search, create, update, and delete Calendar events
- `sheets_specialist`     — create, read, and write Google Sheets spreadsheets
- `slides_specialist`     — create, inspect, and edit Google Slides presentations
- `retrieval_specialist`  — search and retrieve content from resources already in this session
- `action_specialist`     — execute confirmed write actions (send email, create doc, etc.)
- `netbox_specialist`     — query NetBox: devices, interfaces, IP addresses, prefixes, VLANs, racks

## Golden rule

**Do EXACTLY what was asked. Nothing more.**
Never call a specialist that wasn't needed to satisfy the request. Never add steps the user
didn't ask for. If the user says "find X in Drive," call drive_specialist only — do not also
check Gmail, create a doc, or add a calendar event unless explicitly requested.

## How to handle the request

**Single-service requests (most common):** Call exactly one specialist and return.
- "Check my email" → gmail_specialist only
- "Find the Q1 report" → drive_specialist only
- "What's on my calendar today" → calendar_specialist only
- "Look in my Drive for the proposal" → drive_specialist only

**Multi-service requests:** Only call multiple specialists when the request explicitly
requires data from more than one service. Identify dependencies — if a later step needs
data from an earlier one, call the earlier specialist first and pass its result forward.

Examples of genuine multi-step requests:
- "Summarize my unread emails AND create a doc with the action items"
  → 1. gmail_specialist (fetch emails) → 2. docs_specialist (create doc with gmail result)
- "Find the Q1 report in Drive AND add it to my calendar as a reminder"
  → 1. drive_specialist (get file link) → 2. calendar_specialist (create event with link)
- "Check if I'm free at 3pm AND send an invite to alice@example.com"
  → 1. calendar_specialist (check free/busy) → 2. action_specialist (create invite)
- "Turn this Google Doc into a presentation"
  → 1. docs_specialist (read/export the doc text) → 2. slides_specialist (build the deck from that text)

When a request transforms content from one Workspace artifact into another, call the reader
specialist first and pass the extracted content forward. Do not ask a writer specialist to
invent or infer missing source material.

## Output format

After calling all relevant specialists, return a single JSON object:
{
  "summary": "<concise voice-friendly summary, 1–3 sentences>",
  "artifacts": [<combined artifacts from all specialists, in order>],
  "follow_up_questions": [<combined follow-ups, deduplicated>],
  "resource_handles": [<combined resource handles>],
  "action_proposals": [],
  "error": null
}

Rules:
- Summary must be natural, voice-friendly, and answer the user's question directly.
- Combine artifacts from multiple specialists; preserve order (most relevant first).
- If a step fails, continue with remaining steps if possible; note the failure in summary.
- If ALL steps fail, set error and provide a short summary explaining why.
- If recent job workspace context is provided and the request is a correction or continuation,
  reuse the referenced resource IDs and scratchpad state before re-searching from scratch.
- Output ONLY the JSON — no extra commentary.
"""


class WorkspaceCoordinator:
    """Runs the workspace coordinator for a single job and returns a WorkspaceJobResult."""

    def __init__(
        self,
        resource_store: SessionResourceStore | None = None,
        semantic: "SemanticRetrieval | None" = None,
        workspace: JobWorkspaceStore | None = None,
    ) -> None:
        self._resource_store = resource_store
        self._semantic = semantic
        self._workspace = workspace
        self._session_service = InMemorySessionService()

    async def run(self, request: WorkspaceJobRequest) -> WorkspaceJobResult:
        """Execute a workspace job and return a structured result."""
        async with atrace_span(
            "athena.coordinator.run",
            inputs={"request": request.to_dict()},
            metadata=base_metadata(
                component="coordinator.run",
                athena_session_id=request.session_id,
                job_id=request.job_id,
                model=_COORDINATOR_MODEL,
            ),
            tags=["coordinator", "workspace"],
        ) as run:
            log.info(
                "[coordinator] starting job %s for session %s (hint=%s)",
                request.job_id[:8],
                request.session_id,
                request.job_type_hint,
            )
            if self._workspace is not None:
                self._workspace.start_job(request)

            coordinator_agent = self._build_coordinator_agent(
                request.session_id,
                request.job_id,
            )
            runner = Runner(
                agent=coordinator_agent,
                app_name=_APP_NAME,
                session_service=self._session_service,
            )

            session = await self._session_service.create_session(
                app_name=_APP_NAME,
                user_id=_USER_ID,
            )

            prompt = _build_coordinator_prompt(
                request,
                workspace_context=(
                    self._workspace.render_context(
                        request.session_id,
                        query=request.user_request,
                        limit=2,
                        max_chars=1400,
                    )
                    if self._workspace is not None
                    else ""
                ),
            )

            try:
                result_text = await _run_agent_to_text(
                    runner=runner,
                    session_id=session.id,
                    prompt=prompt,
                )
            except Exception as exc:
                log.exception("[coordinator] job %s failed", request.job_id[:8])
                finish_span(run, error=str(exc))
                return WorkspaceJobResult(
                    job_id=request.job_id,
                    session_id=request.session_id,
                    status="failed",
                    error=f"Coordinator error: {exc}",
                    completed_at=datetime.now(timezone.utc),
                )

            result = _parse_coordinator_result(result_text, request)
            finish_span(
                run,
                outputs={
                    "prompt": prompt,
                    "raw_result": result_text,
                    "result": result.to_dict(),
                    "adk_session_id": session.id,
                },
            )
            return result

    def _build_coordinator_agent(self, session_id: str, job_id: str) -> LlmAgent:
        """Build the coordinator LlmAgent with all specialist agents as tools."""
        from app.adk_agents.specialists.retrieval import build_retrieval_agent

        tools = [
            AgentTool(build_gmail_agent(self._workspace, session_id=session_id, job_id=job_id)),
            AgentTool(build_drive_agent(self._workspace, session_id=session_id, job_id=job_id)),
            AgentTool(build_docs_agent(self._workspace, session_id=session_id, job_id=job_id)),
            AgentTool(build_calendar_agent(self._workspace, session_id=session_id, job_id=job_id)),
            AgentTool(build_sheets_agent(self._workspace, session_id=session_id, job_id=job_id)),
            AgentTool(build_slides_agent(self._workspace, session_id=session_id, job_id=job_id)),
            AgentTool(build_retrieval_agent(
                self._resource_store,
                session_id,
                semantic=self._semantic,
                workspace_store=self._workspace,
                job_id=job_id,
            )),
            AgentTool(build_action_agent(self._workspace, session_id=session_id, job_id=job_id)),
            AgentTool(build_netbox_agent(self._workspace, session_id=session_id, job_id=job_id)),
        ]

        return LlmAgent(
            name="workspace_coordinator",
            model=_COORDINATOR_MODEL,
            instruction=_COORDINATOR_INSTRUCTION,
            tools=tools,
        )


def _build_coordinator_prompt(request: WorkspaceJobRequest, *, workspace_context: str = "") -> str:
    """Build the coordinator's input message from the job request."""
    parts: list[str] = [
        f"Job type hint: {request.job_type_hint}",
        f"User request: {request.user_request}",
    ]

    if request.resource_hints:
        parts.append(f"Resource hints: {', '.join(request.resource_hints)}")

    if request.conversation_window:
        recent = request.conversation_window[-4:]  # last 4 turns
        window_text = "\n".join(
            f"{turn.get('role', 'user').capitalize()}: {turn.get('content', '')}"
            for turn in recent
        )
        parts.append(f"Recent conversation:\n{window_text}")

    if workspace_context:
        parts.append(workspace_context)

    parts.append(
        "\nPlease complete this request using the appropriate specialist agent(s) "
        "and return the consolidated JSON result."
    )

    return "\n\n".join(parts)


async def _run_agent_to_text(
    runner: Runner,
    session_id: str,
    prompt: str,
) -> str:
    """Run a headless agent turn and collect the final text response."""
    message = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    final_text = ""
    async for event in runner.run_async(
        user_id=_USER_ID,
        session_id=session_id,
        new_message=message,
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts or []:
                if hasattr(part, "text") and part.text:
                    final_text += part.text

    return final_text.strip()


def _parse_coordinator_result(
    raw_text: str,
    request: WorkspaceJobRequest,
) -> WorkspaceJobResult:
    """Parse the coordinator's JSON output into a WorkspaceJobResult."""
    parsed: dict[str, Any] = {}

    # Try to extract JSON from the response (may be wrapped in markdown).
    json_match = re.search(r"\{[\s\S]*\}", raw_text)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
        except json.JSONDecodeError:
            log.warning(
                "[coordinator] job %s returned invalid JSON, using raw text as summary",
                request.job_id[:8],
            )
            parsed = {"summary": raw_text[:500] if raw_text else "Job completed."}

    if not parsed:
        parsed = {"summary": raw_text[:500] if raw_text else "Job completed."}

    return WorkspaceJobResult(
        job_id=request.job_id,
        session_id=request.session_id,
        status="completed" if not parsed.get("error") else "failed",
        completed_at=datetime.now(timezone.utc),
        summary=str(parsed.get("summary") or ""),
        artifacts=list(parsed.get("artifacts") or []),
        resource_handles=list(parsed.get("resource_handles") or []),
        follow_up_questions=list(parsed.get("follow_up_questions") or []),
        action_proposals=list(parsed.get("action_proposals") or []),
        error=parsed.get("error"),
    )


def build_workspace_coordinator(
    resource_store: SessionResourceStore | None = None,
    semantic: "SemanticRetrieval | None" = None,
    workspace: JobWorkspaceStore | None = None,
) -> WorkspaceCoordinator:
    """Factory for WorkspaceCoordinator."""
    return WorkspaceCoordinator(resource_store=resource_store, semantic=semantic, workspace=workspace)
