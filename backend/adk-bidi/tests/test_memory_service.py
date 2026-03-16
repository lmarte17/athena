import json

from app.memory_service import MemoryService


def test_memory_service_roundtrip(tmp_path):
    memory = MemoryService(base_dir=tmp_path)

    memory.merge_profile({
        "name": {"value": "Alex", "confidence": 1.0},
        "timezone": "America/New_York",
    })
    profile = memory.read_profile()
    assert profile["name"] == "Alex"
    assert profile["timezone"] == "America/New_York"
    assert profile["_confidence"]["name"] == 1.0

    filename = memory.write_session_summary("# Session\n\nSummary body")
    assert filename.endswith(".md")
    sessions = memory.read_recent_sessions(3)
    assert len(sessions) == 1
    assert "Summary body" in sessions[0]

    memory.write_ongoing("# Ongoing\n\n- [ ] Send roadmap update\n")
    assert "Send roadmap update" in memory.read_ongoing()

    memory.append_log([
        {"type": "task", "text": "Ship refactor", "confidence": 0.9},
        {"type": "decision", "text": "Use sqlite memory store", "source": "reflection"},
    ])
    log_path = tmp_path / "memory.log"
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert rows[0]["type"] == "task"
    assert rows[1]["source"] == "reflection"
    assert "ts" in rows[0]


def test_archive_and_reset_preserves_empty_sessions_dir(tmp_path):
    memory = MemoryService(base_dir=tmp_path)

    memory.write_profile({"name": "Alex"})
    memory.write_ongoing("- [ ] Open loop\n")
    memory.write_session_summary("# Session\n\nOne")
    memory.append_log([{"type": "fact", "text": "Test fact"}])

    archive = memory.archive_and_reset()
    assert archive.startswith("sessions_archive_")

    sessions_dir = tmp_path / "sessions"
    assert sessions_dir.exists()
    assert not list(sessions_dir.glob("*.md"))

    assert not (tmp_path / "user" / "profile.yaml").exists()
    assert not (tmp_path / "facts" / "ongoing.md").exists()
    assert not (tmp_path / "memory.log").exists()


def test_memory_service_search_indexes_profile_commitments_and_sessions(tmp_path):
    memory = MemoryService(base_dir=tmp_path)

    memory.merge_profile({"name": {"value": "Alex", "confidence": 1.0}})
    memory.write_ongoing("# Ongoing\n\n- [ ] Prepare Athena launch brief\n")
    memory.write_session_summary("# Session\n\nDiscussed Athena launch brief.")

    hits = memory.search_memory("launch brief", limit=5)

    assert hits
    assert any("launch brief" in hit.snippet.lower() for hit in hits)


def test_staged_candidates_promote_into_commitments_on_reflection(tmp_path):
    memory = MemoryService(base_dir=tmp_path)

    memory.stage_candidates(
        [
            {
                "type": "commitment",
                "text": "Send the roadmap update",
                "confidence": 0.9,
            }
        ],
        source_session_id="session-123",
    )

    memory.consolidate_reflection(
        session_id="session-123",
        transcript=[{"user": "I'll send the roadmap update", "athena": "Noted."}],
        summary_md="# Session\n\nRoadmap follow-up.",
        profile_updates={},
        open_loops=[],
        decisions=[],
    )

    assert "Send the roadmap update" in memory.read_ongoing()
