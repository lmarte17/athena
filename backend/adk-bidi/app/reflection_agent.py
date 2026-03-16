"""
ReflectionAgent — post-session memory synthesis.

Fires once when a session ends (via SessionManager.on_session_end).
Reads the full session transcript, calls Gemini Flash to:
  1. Generate a markdown session summary (episodic memory)
  2. Extract profile facts (semantic memory)
  3. Identify open loops and decisions

Writes:
  - ~/.athena/sessions/YYYY-MM-DD-HHMMSS.md
  - Merges into ~/.athena/user/profile.yaml

Does NOT block the WebSocket teardown — runs as asyncio background task.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

from google.genai import types

from app.memory_service import MemoryService
from app.tracing import atrace_span, base_metadata, create_gemini_client, finish_span

log = logging.getLogger("athena.reflection")

_MEMORY_REFLECT_MODEL = os.getenv("MEMORY_REFLECT_MODEL", "gemini-2.5-flash")

_REFLECT_PROMPT = """\
You are processing a completed conversation session for a personal AI coworker named Athena.

Given the full session transcript below, produce a structured reflection.

Return exactly one valid JSON object with these keys and no extras:

{
  "summary_md": "<markdown string>",
  "profile_updates": {
    "<key>": {"value": "<fact>", "confidence": <number 0.0 to 1.0>}
  },
  "open_loops": ["<string>", "..."],
  "decisions": ["<string>", "..."]
}

Summary markdown format:
```
# Session: {now}

**Topic**: [1-line topic description]

## What happened
[2-4 bullet points summarizing the conversation]

## Decisions made
[Bullet list, or "None" if none]

## Open loops
[Bullet list, or "None" if none]

## User signals
[1-2 observations about mood, working style, or preferences noticed this session]
```

Rules:
- Be factual and specific. Do NOT hallucinate details not present in the transcript.
- For profile_updates: only include facts clearly stated or strongly implied about the USER
  (preferences, projects, personal details). Never write facts about Athena's internal behavior.
- Confidence 1.0 = explicitly stated ("my name is Alex"), 0.7 = implied, 0.5 = uncertain.
- `profile_updates` can be an empty object.
- `open_loops` and `decisions` can be empty arrays.
- Topic must describe what the USER wanted — their goal or question. Do NOT describe what
  Athena did internally (e.g. "Athena checked the calendar", "background job ran"). Write the
  user's intent: "User wanted to review today's schedule", not "Checking calendar events".
- open_loops must be real unresolved user needs, not Athena's pending internal actions.
  Never write things like "Athena will check X" or "calendar results pending" as open loops.
- Output must be strict JSON: no markdown fences, no comments, no trailing commas.
- Return ONLY the JSON object, no explanation.

--- SESSION TRANSCRIPT ---
{transcript}
--- END TRANSCRIPT ---
"""


def _normalize_profile_updates(raw: object) -> dict:
    """Defensively normalize profile_updates into expected mergeable shape."""
    if not isinstance(raw, dict):
        return {}

    normalized: dict = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue

        if isinstance(value, dict) and "value" in value:
            conf_raw = value.get("confidence", 0.7)
            try:
                confidence = float(conf_raw)
            except (TypeError, ValueError):
                confidence = 0.7
            confidence = max(0.0, min(1.0, confidence))
            normalized[key] = {
                "value": value["value"],
                "confidence": confidence,
            }
        else:
            # Accept bare values as a fallback.
            normalized[key] = {
                "value": value,
                "confidence": 0.7,
            }
    return normalized


def _normalize_string_list(raw: object) -> list[str]:
    """Normalize reflection list fields (open_loops / decisions) to clean strings."""
    if not isinstance(raw, list):
        return []
    items: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def _parse_json_object(raw: str) -> dict | None:
    """Parse reflection JSON with light recovery for common model formatting issues."""
    text = raw.strip()
    if not text:
        return {}

    candidates: list[str] = []
    candidates.append(text)

    # Strip markdown code fences if present.
    no_fence = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    no_fence = re.sub(r"\s*```$", "", no_fence).strip()
    if no_fence and no_fence != text:
        candidates.append(no_fence)

    # Extract object slice if response contains extra prose.
    start = no_fence.find("{")
    end = no_fence.rfind("}")
    if start != -1 and end != -1 and end > start:
        extracted = no_fence[start:end + 1]
        if extracted != no_fence:
            candidates.append(extracted)

    # Deduplicate while preserving order.
    seen: set[str] = set()
    uniq_candidates: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            uniq_candidates.append(candidate)
            seen.add(candidate)

    for candidate in uniq_candidates:
        # 1) direct parse
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # 2) strip JS-style comments and trailing commas, then parse
        cleaned = re.sub(r"(?m)//.*$", "", candidate)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    return None

def _format_transcript(turns: list[dict]) -> str:
    """Format a list of {"user": ..., "athena": ...} turns as readable text."""
    lines = []
    for i, turn in enumerate(turns, 1):
        user_text = turn.get("user", "").strip()
        athena_text = turn.get("athena", "").strip()
        if user_text:
            lines.append(f"[Turn {i}] User: {user_text}")
        if athena_text:
            lines.append(f"[Turn {i}] Athena: {athena_text}")
    return "\n".join(lines)


class ReflectionAgent:
    def __init__(self, memory_service: MemoryService) -> None:
        self.memory = memory_service
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = create_gemini_client(
                "athena.memory.reflection",
                model=_MEMORY_REFLECT_MODEL,
                tags=["memory", "reflection"],
            )
        return self._client

    async def run(self, session_id: str, transcript: list[dict]) -> None:
        """
        Run post-session reflection. Fires as an asyncio background task.

        Args:
            session_id: ADK session ID (for logging).
            transcript:  List of {"user": str, "athena": str} dicts, one per turn.
        """
        if not transcript:
            log.info(f"Reflection skipped for {session_id}: empty transcript")
            return

        transcript_text = _format_transcript(transcript)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        prompt = _REFLECT_PROMPT.format(transcript=transcript_text, now=now)

        log.info(f"Reflection starting for session {session_id} ({len(transcript)} turns)")

        async with atrace_span(
            "athena.memory.reflection",
            inputs={
                "session_id": session_id,
                "transcript_turns": len(transcript),
                "transcript": transcript_text,
            },
            metadata=base_metadata(
                component="memory.reflection",
                athena_session_id=session_id,
                model=_MEMORY_REFLECT_MODEL,
            ),
            tags=["memory", "reflection"],
        ) as run:
            try:
                response = await self._get_client().aio.models.generate_content(
                    model=_MEMORY_REFLECT_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )
                raw = response.text or "{}"
                parsed = _parse_json_object(raw)
                if parsed is None:
                    message = (
                        f"unable to parse model output (first 300 chars): {raw[:300]!r}"
                    )
                    log.error(
                        f"Reflection JSON parse error for {session_id}: {message}"
                    )
                    finish_span(run, error=message)
                    return
                result = parsed
            except Exception as e:
                log.error(f"Reflection LLM error for {session_id}: {e}")
                finish_span(run, error=str(e))
                return

            profile_updates = _normalize_profile_updates(result.get("profile_updates", {}))
            open_loops = _normalize_string_list(result.get("open_loops", []))
            decisions = _normalize_string_list(result.get("decisions", []))

            summary_md = result.get("summary_md", "").strip()
            if not summary_md:
                log.warning(f"Reflection produced no summary for {session_id}")

            self.memory.consolidate_reflection(
                session_id=session_id,
                transcript=transcript,
                summary_md=summary_md,
                profile_updates=profile_updates,
                open_loops=open_loops,
                decisions=decisions,
            )

            finish_span(
                run,
                outputs={
                    "summary_md": summary_md,
                    "profile_update_count": len(profile_updates),
                    "open_loop_count": len(open_loops),
                    "decision_count": len(decisions),
                },
            )

        log.info(f"Reflection complete for session {session_id}")
