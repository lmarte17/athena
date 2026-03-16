from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def json_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=True)


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def approval_status_for(namespace: str, *, sensitive: bool = False) -> str:
    if namespace == "identity" or sensitive:
        return "pending"
    return "auto"


def build_audit_entry(
    action: str,
    namespace: str,
    payload: dict[str, Any],
    *,
    source_session_id: str | None = None,
) -> dict[str, Any]:
    entry = {
        "ts": utc_now(),
        "action": action,
        "namespace": namespace,
        "payload": payload,
    }
    if source_session_id:
        entry["source_session_id"] = source_session_id
    return entry
