"""Pure helpers for WebSocket session logic."""

import json


class TurnAccumulator:
    """Collects transcript chunks for a single live turn.

    Input transcription design
    --------------------------
    Gemini Live sends input_transcription events *cumulatively within each
    speech segment* — e.g. "find" → "find the" → "find the JFK plan" — then
    marks the segment finished with `finished=True`.  When the user pauses
    and continues speaking, a fresh segment begins from scratch ("hardware" →
    "hardware requirements"), so the new partials share no prefix with the
    previous segment's final text.

    We therefore track:
    - `_completed_in`: list of finalized segment texts (one entry per finished
      segment).
    - `_current_in`: the latest cumulative partial for the in-progress segment.

    On every call we replace `_current_in` (always take the most recent
    cumulative text).  On `finished=True` we commit it to `_completed_in` and
    reset.  This eliminates the `startswith` fragility of the old approach and
    ensures intermediate partials never leak into the joined transcript.
    """

    def __init__(self) -> None:
        self._completed_in: list[str] = []
        self._current_in: str = ""
        self._turn_out: list[str] = []

    def add_input(self, text: str, finished: bool = False) -> None:
        if not text:
            return
        self._current_in = text  # replace with latest cumulative partial
        if finished:
            self._completed_in.append(text)
            self._current_in = ""

    def add_output(self, text: str) -> None:
        if text:
            self._turn_out.append(text)

    def interrupt(self) -> None:
        """Drop current in-flight partial turn."""
        self.clear()

    def finalize(self) -> tuple[str, str]:
        """Close current turn and return normalized transcript pair."""
        # Include any segment that ended without a finished=True event
        # (e.g. turn_complete arrives before the final transcription flag).
        segments = self._completed_in[:]
        if self._current_in:
            segments.append(self._current_in)
        transcript_in = " ".join(segments).strip()
        transcript_out = " ".join(self._turn_out).strip()
        self.clear()
        return transcript_in, transcript_out

    def clear(self) -> None:
        self._completed_in.clear()
        self._current_in = ""
        self._turn_out.clear()


def parse_client_text_message(raw_text: str) -> tuple[str | None, dict]:
    """
    Parse a JSON client text frame and return `(kind, message_dict)`.

    Raises:
        json.JSONDecodeError: when frame is not valid JSON.
    """
    msg = json.loads(raw_text)
    return msg.get("type"), msg


def has_turn_content(transcript_in: str, transcript_out: str) -> bool:
    """Return True when at least one side of the turn has non-empty content."""
    return bool(transcript_in or transcript_out)
