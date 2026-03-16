"""Background hydration for Gmail thread bodies."""

from __future__ import annotations

import base64
import html
import re
from dataclasses import replace
from datetime import datetime
from typing import Any

from app.gog_client import run_gog_json
from app.hydration_types import HydrationResult
from app.resource_store import ResourceHandle


class GmailHydrator:
    def supports(self, handle: ResourceHandle) -> bool:
        return handle.source == "gmail" and handle.kind == "thread"

    async def hydrate(self, handle: ResourceHandle) -> HydrationResult | None:
        payload = await run_gog_json("gmail", "thread", "get", handle.id)

        messages = _sorted_messages(payload.get("messages"))
        if not messages:
            return None

        rendered_messages = [_render_message(message) for message in messages]
        rendered_messages = [message for message in rendered_messages if message]
        if not rendered_messages:
            return None

        normalized_text = "\n\n".join(rendered_messages).strip()
        if not normalized_text:
            return None

        history_id = str(payload.get("historyId") or "").strip() or handle.version
        return HydrationResult(
            handle=replace(
                handle,
                version=history_id,
                metadata={
                    **handle.metadata,
                    "history_id": history_id,
                    "message_count": len(messages),
                },
            ),
            normalized_text=normalized_text,
            metadata={
                "history_id": history_id,
                "message_count": len(messages),
            },
        )


def _sorted_messages(value: Any) -> list[dict[str, Any]]:
    messages = [message for message in value or [] if isinstance(message, dict)]
    if not messages:
        return []

    def _sort_key(message: dict[str, Any]) -> tuple[int, str]:
        raw_date = message.get("internalDate")
        try:
            return int(str(raw_date)), str(message.get("id") or "")
        except (TypeError, ValueError):
            return 0, str(message.get("id") or "")

    return sorted(messages, key=_sort_key)


def _render_message(message: dict[str, Any]) -> str:
    headers = _header_map(message.get("payload", {}).get("headers"))
    lines = [f"From: {headers.get('from', '(unknown)')}"]

    if headers.get("to"):
        lines.append(f"To: {headers['to']}")
    if headers.get("date"):
        lines.append(f"Date: {headers['date']}")
    elif message.get("internalDate"):
        lines.append(f"Date: {_format_internal_date(message['internalDate'])}")
    if headers.get("subject"):
        lines.append(f"Subject: {headers['subject']}")

    body_text = _message_body_text(message)
    if body_text:
        lines.extend(["", body_text])

    snippet = str(message.get("snippet") or "").strip()
    if not body_text and snippet:
        lines.extend(["", snippet])

    return "\n".join(line for line in lines if line is not None).strip()


def _message_body_text(message: dict[str, Any]) -> str:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return ""

    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_body_parts(payload, plain_parts, html_parts)

    if plain_parts:
        return _normalize_body("\n\n".join(plain_parts))
    if html_parts:
        return _normalize_body(_html_to_text("\n\n".join(html_parts)))
    return ""


def _collect_body_parts(
    payload: dict[str, Any],
    plain_parts: list[str],
    html_parts: list[str],
) -> None:
    mime_type = str(payload.get("mimeType") or "")
    body = payload.get("body")
    if isinstance(body, dict) and body.get("data"):
        decoded = _decode_body_data(str(body["data"]))
        if decoded:
            if mime_type.startswith("text/plain"):
                plain_parts.append(decoded)
            elif mime_type.startswith("text/html"):
                html_parts.append(decoded)

    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            _collect_body_parts(part, plain_parts, html_parts)


def _decode_body_data(value: str) -> str:
    padded = value + ("=" * (-len(value) % 4))
    decoded = base64.urlsafe_b64decode(padded.encode())
    return decoded.decode("utf-8", errors="replace")


def _header_map(headers: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    if not isinstance(headers, list):
        return values

    for header in headers:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip().lower()
        value = str(header.get("value") or "").strip()
        if name and value and name not in values:
            values[name] = value
    return values


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</p>|</div>|</li>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _normalize_body(value: str) -> str:
    lines = [line.strip() for line in value.replace("\r\n", "\n").splitlines()]
    normalized: list[str] = []
    blank_run = 0

    for line in lines:
        if line:
            blank_run = 0
            normalized.append(line)
            continue

        blank_run += 1
        if blank_run == 1:
            normalized.append("")

    return "\n".join(normalized).strip()


def _format_internal_date(value: Any) -> str:
    try:
        parsed = datetime.fromtimestamp(int(str(value)) / 1000).astimezone()
    except (TypeError, ValueError, OSError):
        return str(value)
    return parsed.strftime("%Y-%m-%d %I:%M %p").replace(" 0", " ")
