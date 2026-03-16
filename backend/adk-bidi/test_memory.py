"""
Smoke test for Phase 1b memory pipeline.

Run from backend/adk-bidi/:
    export SSL_CERT_FILE=$(uv run python -m certifi)
    uv run python test_memory.py

Tests:
1. MemoryService — read/write profile, session summary, memory log
2. IncrementalTapAgent — extract from a fake turn (uses gemini-2.5-flash)
3. ReflectionAgent — synthesize from a fake session transcript (uses gemini-2.5-flash)
"""

import asyncio
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "app" / ".env")

from app.memory_service import MemoryService
from app.tap_agent import IncrementalTapAgent
from app.reflection_agent import ReflectionAgent


# Use a temp dir so we don't pollute ~/.athena during testing
TEST_DIR = Path("/tmp/athena-test")

FAKE_TURN = {
    "user": "I need to finish the Q1 roadmap doc by Thursday. "
            "I prefer working in Notion, and my name is Alex.",
    "athena": "Got it — roadmap doc due Thursday. I'll remind you tomorrow. "
              "I see you have Notion open, want me to help structure it?",
}

FAKE_TRANSCRIPT = [
    FAKE_TURN,
    {
        "user": "Yeah let's do that. Also I decided we're going with Option B for the architecture.",
        "athena": "Option B it is. I'll note that decision. "
                  "Should I capture the reasons too, or just the choice?",
    },
    {
        "user": "Just the choice for now, I'm in a rush.",
        "athena": "Noted. Decision logged: Option B selected for architecture.",
    },
]


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


async def test_memory_service():
    section("1. MemoryService")
    ms = MemoryService(base_dir=TEST_DIR)

    # Profile round-trip
    ms.write_profile({"name": "TestUser", "role": "Engineer", "_confidence": {"name": 1.0}})
    profile = ms.read_profile()
    assert profile.get("name") == "TestUser", f"Profile read failed: {profile}"
    print("  ✓ write_profile / read_profile")

    # merge_profile — should update role, add timezone
    ms.merge_profile({
        "role": {"value": "Lead Engineer", "confidence": 0.9},
        "timezone": {"value": "America/New_York", "confidence": 0.7},
    })
    profile = ms.read_profile()
    assert profile.get("role") == "Lead Engineer"
    assert profile.get("timezone") == "America/New_York"
    print("  ✓ merge_profile")

    # session summary
    filename = ms.write_session_summary("# Session: 2026-03-03\n\nTest summary.")
    recent = ms.read_recent_sessions(n=1)
    assert len(recent) == 1 and "Test summary" in recent[0]
    print(f"  ✓ write_session_summary → {filename}")

    # memory log
    ms.append_log([{"type": "task", "text": "Finish roadmap", "confidence": 0.9}])
    log_path = TEST_DIR / "memory.log"
    lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    assert lines[0]["type"] == "task"
    print(f"  ✓ append_log ({len(lines)} entries)")

    print("\n  MemoryService: all checks passed")


async def test_tap_agent():
    section("2. IncrementalTapAgent (live LLM call)")
    ms = MemoryService(base_dir=TEST_DIR)
    tap = IncrementalTapAgent(ms)

    print(f"  Sending turn to tap agent...")
    print(f"  User: {FAKE_TURN['user'][:60]}...")
    print(f"  Athena: {FAKE_TURN['athena'][:60]}...")

    entries = await tap.extract(FAKE_TURN["user"], FAKE_TURN["athena"])
    print(f"\n  Extracted {len(entries)} items:")
    for e in entries:
        print(f"    [{e.get('type', '?')}] {e.get('text', '?')} (conf={e.get('confidence', '?')})")

    if entries:
        ms.append_log(entries)
        log_path = TEST_DIR / "memory.log"
        count = len(log_path.read_text().strip().splitlines())
        print(f"\n  ✓ memory.log now has {count} entries")
    else:
        print("\n  ⚠ No entries extracted (LLM returned empty) — check if this is expected")


async def test_reflection_agent():
    section("3. ReflectionAgent (live LLM call)")
    ms = MemoryService(base_dir=TEST_DIR)
    reflect = ReflectionAgent(ms)

    print(f"  Running reflection on {len(FAKE_TRANSCRIPT)}-turn transcript...")

    await reflect.run(session_id="test-session-001", transcript=FAKE_TRANSCRIPT)

    # Check session summary was written
    sessions_dir = TEST_DIR / "sessions"
    summaries = list(sessions_dir.glob("*.md"))
    if summaries:
        latest = sorted(summaries)[-1]
        print(f"\n  Session summary written: {latest.name}")
        print(f"  Preview:\n{latest.read_text()[:400]}")
    else:
        print("\n  ⚠ No session summary written")

    # Check profile was updated
    profile = ms.read_profile()
    print(f"\n  Profile after reflection:")
    for k, v in profile.items():
        if not k.startswith("_"):
            print(f"    {k}: {v}")

    # Check memory log
    log_path = TEST_DIR / "memory.log"
    if log_path.exists():
        entries = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        reflect_entries = [e for e in entries if e.get("source") == "reflection"]
        print(f"\n  Reflection added {len(reflect_entries)} log entries:")
        for e in reflect_entries:
            print(f"    [{e.get('type')}] {e.get('text', '')[:80]}")


async def main():
    print("\nAthena Phase 1b — Memory Pipeline Smoke Test")
    print(f"Test dir: {TEST_DIR}")

    # Clean test dir
    import shutil
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)

    try:
        await test_memory_service()
        await test_tap_agent()
        await test_reflection_agent()

        print(f"\n{'='*60}")
        print("  All tests complete")
        print(f"  Test artifacts in: {TEST_DIR}")
        print(f"{'='*60}\n")

    except AssertionError as e:
        print(f"\n  ASSERTION FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n  ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)


asyncio.run(main())
