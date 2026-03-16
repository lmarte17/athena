"""
IncrementalTapAgent — per-turn memory extraction.

Fires on every turn_complete event (via SessionManager broadcast queue).
Narrow job: given a single turn's transcript, extract tasks, commitments,
open loops, and quick facts, then append them to memory.log.

Does NOT update profile.yaml — that is ReflectionAgent's job.

Model: Gemini Flash now; swap to Gemma (local) later via MEMORY_TAP_MODEL env var.
"""

import json
import logging
import os

from google.genai import types

from app.memory_service import MemoryService
from app.tracing import atrace_span, base_metadata, create_gemini_client, finish_span

log = logging.getLogger("athena.tap")

_MEMORY_TAP_MODEL = os.getenv("MEMORY_TAP_MODEL", "gemini-2.5-flash")

_EXTRACT_PROMPT = """\
You are a memory extraction assistant for a personal AI.

Given a single conversation turn below, extract any notable items.
Look for:
- Tasks: things the user needs to do or explicitly committed to
- Facts: facts about the user, their work, tools, or situation
- Open loops: unresolved questions, pending decisions, follow-ups needed
- Commitments: explicit promises or plans ("I'll send that tomorrow", "we agreed to...")

Return a JSON array of extracted items. Each item:
  {"type": "task|fact|open_loop|commitment", "text": "...", "confidence": 0.0-1.0}

Rules:
- Only extract clearly stated or strongly implied items — do NOT hallucinate.
- Keep text entries concise (1 sentence max).
- Return [] if the turn has nothing notable (small talk, acknowledgments, etc.).
- Return ONLY the JSON array, no explanation.

--- TURN START ---
User: {user}

Athena: {athena}
--- TURN END ---
"""

class IncrementalTapAgent:
    def __init__(self, memory_service: MemoryService) -> None:
        self.memory = memory_service
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = create_gemini_client(
                "athena.memory.tap",
                model=_MEMORY_TAP_MODEL,
                tags=["memory", "tap"],
            )
        return self._client

    async def extract(self, transcript_in: str, transcript_out: str) -> list[dict]:
        """
        Extract notable items from a single turn.
        Returns a list of dicts ready to append to memory.log.
        Returns [] if nothing notable or if the LLM call fails.
        """
        if not transcript_in.strip() and not transcript_out.strip():
            return []

        prompt = _EXTRACT_PROMPT.format(
            user=transcript_in or "(no user speech)",
            athena=transcript_out or "(no Athena response)",
        )

        async with atrace_span(
            "athena.memory.tap",
            inputs={
                "transcript_in": transcript_in,
                "transcript_out": transcript_out,
            },
            metadata=base_metadata(
                component="memory.tap",
                model=_MEMORY_TAP_MODEL,
            ),
            tags=["memory", "tap"],
        ) as run:
            try:
                response = await self._get_client().aio.models.generate_content(
                    model=_MEMORY_TAP_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                raw = response.text or "[]"
                items = json.loads(raw)
                if not isinstance(items, list):
                    log.warning(f"Tap agent returned non-list: {raw[:200]}")
                    finish_span(run, outputs={"raw": raw[:200], "parsed": False})
                    return []

                for item in items:
                    item["source"] = "tap"

                finish_span(
                    run,
                    outputs={
                        "parsed": True,
                        "item_count": len(items),
                        "items": items,
                    },
                )
                return items

            except json.JSONDecodeError as e:
                log.warning(f"Tap agent JSON parse error: {e}")
                finish_span(run, error=str(e))
                return []
            except Exception as e:
                log.error(f"Tap agent error: {e}")
                finish_span(run, error=str(e))
                return []
