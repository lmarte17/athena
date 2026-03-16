from __future__ import annotations

import json
import logging
from pathlib import Path

from app.memory_v2.models import Commitment
from app.memory_v2.vault import MemoryVault

log = logging.getLogger("athena.memory_v2.migration")


def migrate_legacy_memory(vault: MemoryVault) -> bool:
    migrated = False

    if not vault.profile_path.exists() and vault.legacy_profile_path.exists():
        vault.profile_path.write_text(vault.legacy_profile_path.read_text())
        migrated = True

    if not vault.commitments_path.exists() and vault.legacy_ongoing_path.exists():
        commitments = vault.parse_ongoing_markdown(vault.legacy_ongoing_path.read_text())
        vault.write_commitments(commitments)
        migrated = True

    if not any(vault.episodes_dir.rglob("*.md")) and vault.legacy_sessions_dir.exists():
        for path in sorted(vault.legacy_sessions_dir.glob("*.md")):
            created_at = path.name.removesuffix(".md")
            vault.write_session_summary(path.read_text(), created_at=f"{created_at[:10]}T00:00:00+00:00")
            migrated = True

    if not vault.audit_path.exists() and vault.legacy_log_path.exists():
        legacy_copy = vault.audit_dir / "legacy_memory.log"
        legacy_copy.write_text(vault.legacy_log_path.read_text())
        migrated = True

    if migrated:
        log.info("Legacy memory migrated into v2 vault at %s", vault.v2)
    return migrated
