"""SkillLibrary — SQLite-backed store for reusable execution plan templates.

Workflow:
- After a successful multi-step job completes, `distill()` is called to
  optionally extract and store a reusable skill template.
- On each new job, `find_match()` checks the cache for a pattern hit before
  the planner calls the LLM.
- Matched skills skip the planning LLM call entirely (fast path).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from google.genai import types

from app.planner.models import Skill
from app.tracing import atrace_span, base_metadata, create_gemini_client, finish_span

if TYPE_CHECKING:
    from app.jobs.models import WorkspaceJobRequest, WorkspaceJobResult
    from app.planner.models import ExecutionPlan

log = logging.getLogger("athena.planner.skill_library")

_DISTILL_MODEL = os.getenv("ATHENA_DISTILL_MODEL", "gemini-3.1-flash-lite-preview")

_DISTILL_SYSTEM = """\
You are extracting a reusable skill from a successful multi-step workspace task.

Given:
- The user's original request
- The execution plan (list of steps with specialists and instructions)
- The result summary

Extract a generalised skill definition. Output ONLY JSON with this schema:

{
  "name": "<short snake_case skill name, e.g. 'search_and_summarise_emails'>",
  "description": "<one sentence describing what this skill does>",
  "trigger_patterns": ["<keyword or short phrase that should trigger this skill>", ...]
}

Rules:
- trigger_patterns: 3–8 short phrases (2–5 words each) that reliably indicate
  this exact type of multi-step task. Be specific enough to avoid false positives.
- name: snake_case, max 40 chars, describes the pattern not the specific content.
- description: generalise from the specific request (e.g. "search emails then create summary doc"
  not "find emails from Bob about Q1").
- Output ONLY the JSON — no markdown, no extra text.
"""


def _db_path() -> str:
    path = Path.home() / ".athena" / "skills.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


class SkillLibrary:
    """Persists and retrieves skill templates backed by SQLite."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _db_path()
        self._conn: aiosqlite.Connection | None = None
        # In-memory cache: loaded at startup, updated on write.
        self._cache: list[Skill] = []
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = create_gemini_client(
                "athena.skill_library",
                model=_DISTILL_MODEL,
                tags=["skill-library"],
            )
        return self._client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skills (
                skill_id        TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                description     TEXT,
                trigger_patterns TEXT,
                plan_template   TEXT,
                use_count       INTEGER DEFAULT 0,
                created_at      TEXT,
                last_used_at    TEXT
            )
            """
        )
        await self._conn.commit()
        await self._refresh_cache()
        log.info("[skill_library] loaded %d skill(s) from %s", len(self._cache), self._db_path)

    async def shutdown(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def find_match(self, user_request: str) -> Skill | None:
        """Return the first cached skill whose trigger patterns appear in the request."""
        request_lower = user_request.lower()
        best: Skill | None = None
        best_hits = 0

        for skill in self._cache:
            hits = sum(
                1 for p in skill.trigger_patterns if p.lower() in request_lower
            )
            if hits > best_hits:
                best_hits = hits
                best = skill

        if best and best_hits > 0:
            await self._record_hit(best.skill_id)
            return best
        return None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save_skill(
        self,
        name: str,
        description: str,
        plan: ExecutionPlan,
        trigger_patterns: list[str],
    ) -> Skill:
        """Persist a new skill. Updates the in-memory cache immediately."""
        skill = Skill(
            skill_id=str(uuid.uuid4()),
            name=name,
            description=description,
            trigger_patterns=trigger_patterns,
            plan_template=plan.to_dict(),
        )
        await self._persist(skill)
        self._cache.append(skill)
        log.info("[skill_library] saved new skill '%s' (id=%s)", name, skill.skill_id[:8])
        return skill

    # ------------------------------------------------------------------
    # Distillation (fire-and-forget after successful complex jobs)
    # ------------------------------------------------------------------

    async def distill(
        self,
        request: WorkspaceJobRequest,
        plan: ExecutionPlan,
        result: WorkspaceJobResult,
    ) -> None:
        """Extract and save a reusable skill if the completed job warrants it."""
        if plan.is_trivial or len(plan.steps) < 2:
            return
        if result.status != "completed" or result.error:
            return
        if plan.skill_id:
            # Already instantiated from a skill — no need to re-distill.
            return

        try:
            skill_info = await self._call_distill_llm(request, plan, result)
            if not skill_info:
                return

            name = skill_info.get("name", "")
            description = skill_info.get("description", "")
            patterns = list(skill_info.get("trigger_patterns") or [])

            if not name or not patterns:
                log.debug("[skill_library] distill returned incomplete data — skipping")
                return

            # Avoid duplicates: skip if a skill with the same name already exists.
            if any(s.name == name for s in self._cache):
                log.debug("[skill_library] skill '%s' already exists — skipping", name)
                return

            await self.save_skill(
                name=name,
                description=description,
                plan=plan,
                trigger_patterns=patterns,
            )
        except Exception:
            log.exception("[skill_library] distillation failed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_distill_llm(
        self,
        request: WorkspaceJobRequest,
        plan: ExecutionPlan,
        result: WorkspaceJobResult,
    ) -> dict[str, Any] | None:
        prompt = (
            f"User request: {request.user_request}\n\n"
            f"Execution plan steps:\n"
            + "\n".join(
                f"  {s.step_id} [{s.specialist}]: {s.instruction[:120]}"
                for s in plan.steps
            )
            + f"\n\nResult summary: {result.summary[:300]}"
        )

        async with atrace_span(
            "athena.skill_library.distill",
            inputs={"prompt": prompt, "system_instruction": _DISTILL_SYSTEM},
            metadata=base_metadata(
                component="skill_library.distill",
                athena_session_id=request.session_id,
                job_id=request.job_id,
                model=_DISTILL_MODEL,
            ),
            tags=["skill-library"],
        ) as run:
            response = await self._get_client().aio.models.generate_content(
                model=_DISTILL_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_DISTILL_SYSTEM,
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )

            raw = (response.text or "").strip()
            try:
                parsed = json.loads(raw)
                finish_span(run, outputs={"raw": raw, "parsed": parsed})
                return parsed
            except json.JSONDecodeError:
                log.debug("[skill_library] distill LLM returned invalid JSON: %r", raw[:200])
                finish_span(run, error="distill returned invalid JSON")
                return None

    async def _persist(self, skill: Skill) -> None:
        if self._conn is None:
            return
        now = datetime.utcnow().isoformat()
        await self._conn.execute(
            """
            INSERT INTO skills
                (skill_id, name, description, trigger_patterns, plan_template,
                 use_count, created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                skill.skill_id,
                skill.name,
                skill.description,
                json.dumps(skill.trigger_patterns),
                json.dumps(skill.plan_template),
                now,
                now,
            ),
        )
        await self._conn.commit()

    async def _record_hit(self, skill_id: str) -> None:
        if self._conn is None:
            return
        now = datetime.utcnow().isoformat()
        await self._conn.execute(
            "UPDATE skills SET use_count = use_count + 1, last_used_at = ? WHERE skill_id = ?",
            (now, skill_id),
        )
        await self._conn.commit()

    async def _refresh_cache(self) -> None:
        if self._conn is None:
            return
        async with self._conn.execute(
            "SELECT skill_id, name, description, trigger_patterns, plan_template, "
            "use_count, created_at, last_used_at FROM skills ORDER BY use_count DESC"
        ) as cursor:
            rows = await cursor.fetchall()

        self._cache = []
        for row in rows:
            try:
                skill = Skill(
                    skill_id=row[0],
                    name=row[1],
                    description=row[2] or "",
                    trigger_patterns=json.loads(row[3] or "[]"),
                    plan_template=json.loads(row[4] or "{}"),
                    use_count=int(row[5] or 0),
                    created_at=datetime.fromisoformat(row[6]) if row[6] else datetime.utcnow(),
                    last_used_at=datetime.fromisoformat(row[7]) if row[7] else datetime.utcnow(),
                )
                self._cache.append(skill)
            except Exception:
                log.debug("[skill_library] skipping malformed skill row: %r", row)
