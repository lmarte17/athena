"""Section-aware text chunking for workspace documents.

Tries to split on structural headings first (markdown, numbered sections,
Google Docs-style ALL-CAPS headers). Falls back to paragraph-based splitting.

Each chunk is returned as (section_title, chunk_text).
"""

from __future__ import annotations

import re
from typing import Iterator

# Max characters per chunk. ~2000 chars ≈ ~400 tokens.
_DEFAULT_MAX_CHARS = 2000
# Min characters to keep a chunk (ignore tiny fragments)
_MIN_CHUNK_CHARS = 50

# Patterns that indicate a section heading line
_HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,6}\s+.+?"               # Markdown: # Heading
    r"|(?:SECTION|PART|CHAPTER)\s+\d+[.:]\s*.+"  # SECTION 5: Hardware
    r"|\d+\.\d*\s+[A-Z][A-Za-z]+"  # 5.1 Hardware Requirements
    r"|\*\*[^*]{3,60}\*\*\s*$"   # **Bold header** (Google Docs export)
    r")",
    re.IGNORECASE,
)

# Matches markdown heading to extract the text
_MD_HEADING_TEXT_RE = re.compile(r"^#{1,6}\s+(.+)$")


def _heading_text(line: str) -> str:
    m = _MD_HEADING_TEXT_RE.match(line)
    if m:
        return m.group(1).strip()
    # Bold header: strip asterisks
    stripped = re.sub(r"^\*\*|\*\*$", "", line.strip())
    return stripped.strip() or line.strip()


def chunk_by_headings(
    text: str,
    max_chunk_chars: int = _DEFAULT_MAX_CHARS,
) -> list[tuple[str, str]]:
    """Split text into (section_title, chunk_text) pairs.

    Prefers splitting on heading lines. If no headings are found, falls back
    to paragraph-based splitting. Long sections are sub-chunked at paragraph
    boundaries to stay under max_chunk_chars.

    Returns a non-empty list — at minimum one chunk containing the whole text.
    """
    if not text or not text.strip():
        return []

    lines = text.splitlines()
    heading_indices = [i for i, line in enumerate(lines) if _HEADING_RE.match(line.strip())]

    if heading_indices:
        raw_sections = _split_on_headings(lines, heading_indices)
    else:
        raw_sections = _split_paragraphs(text)

    result: list[tuple[str, str]] = []
    for title, body in raw_sections:
        for sub in _sub_chunk(body, max_chunk_chars):
            if len(sub) >= _MIN_CHUNK_CHARS:
                result.append((title, sub))

    if not result and text.strip():
        # Last resort: return the whole text as one chunk
        result.append(("", text.strip()[:max_chunk_chars]))

    return result


def _split_on_headings(
    lines: list[str],
    heading_indices: list[int],
) -> Iterator[tuple[str, str]]:
    """Yield (heading_text, body) pairs by splitting on heading line indices."""
    # Content before first heading gets a blank title
    if heading_indices[0] > 0:
        pre = "\n".join(lines[: heading_indices[0]]).strip()
        if pre:
            yield ("", pre)

    for i, idx in enumerate(heading_indices):
        title = _heading_text(lines[idx])
        next_idx = heading_indices[i + 1] if i + 1 < len(heading_indices) else len(lines)
        body = "\n".join(lines[idx + 1 : next_idx]).strip()
        if body:
            yield (title, body)
        elif not body:
            # Heading with no body — include the heading text itself as content
            # so the section title is still searchable
            yield (title, title)


def _split_paragraphs(text: str) -> list[tuple[str, str]]:
    """Split on blank lines as paragraph boundaries."""
    paras = re.split(r"\n\s*\n", text.strip())
    return [("", p.strip()) for p in paras if p.strip()]


def _sub_chunk(text: str, max_chars: int) -> list[str]:
    """Split text into ≤max_chars pieces, preferring paragraph boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    paras = re.split(r"\n\s*\n", text)
    current = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current)
            current = para
        else:
            current = (current + "\n\n" + para).strip() if current else para

    if current:
        chunks.append(current)

    # If a single paragraph is still too long, hard-split it
    final: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final.append(chunk)
        else:
            for i in range(0, len(chunk), max_chars):
                part = chunk[i : i + max_chars].strip()
                if part:
                    final.append(part)

    return final or [text[:max_chars]]
