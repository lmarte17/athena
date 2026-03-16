import json

import pytest

from app.memory_service import MemoryService
from app.reflection_agent import ReflectionAgent, _parse_json_object


class _StubResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubModels:
    def __init__(self, text: str) -> None:
        self._text = text

    async def generate_content(self, **kwargs):
        return _StubResponse(self._text)


class _StubAio:
    def __init__(self, text: str) -> None:
        self.models = _StubModels(text)


class _StubClient:
    def __init__(self, text: str) -> None:
        self.aio = _StubAio(text)


class _StubReflectionAgent(ReflectionAgent):
    def __init__(self, memory_service: MemoryService, model_output: str) -> None:
        super().__init__(memory_service)
        self._model_output = model_output

    def _get_client(self):
        return _StubClient(self._model_output)


def test_parse_json_object_recovers_common_malformed_forms():
    payload = (
        "```json\n"
        '{"summary_md":"x", "profile_updates": {}, "open_loops": [], "decisions": [],}\n'
        "```"
    )
    parsed = _parse_json_object(payload)
    assert parsed is not None
    assert parsed["summary_md"] == "x"
    assert parsed["open_loops"] == []


@pytest.mark.asyncio
async def test_reflection_run_writes_memory_artifacts(tmp_path):
    model_output = json.dumps({
        "summary_md": "# Session: test\n\nSummary text.",
        "profile_updates": {
            "name": {"value": "Alex", "confidence": 1.0},
            "current_project": {"value": "Athena", "confidence": 0.9},
        },
        "open_loops": ["Send roadmap update"],
        "decisions": ["Use option B"],
    })
    memory = MemoryService(base_dir=tmp_path)
    agent = _StubReflectionAgent(memory, model_output=model_output)

    await agent.run(
        session_id="session-1",
        transcript=[
            {"user": "My name is Alex", "athena": "Got it."},
            {"user": "Let's use option B", "athena": "Noted."},
        ],
    )

    sessions = list((tmp_path / "sessions").glob("*.md"))
    assert len(sessions) == 1
    assert "Summary text." in sessions[0].read_text()

    profile = memory.read_profile()
    assert profile["name"] == "Alex"
    assert profile["current_project"] == "Athena"

    ongoing = (tmp_path / "facts" / "ongoing.md").read_text()
    assert "Send roadmap update" in ongoing

    log_rows = [
        json.loads(line)
        for line in (tmp_path / "memory.log").read_text().splitlines()
        if line.strip()
    ]
    assert any(row["type"] == "open_loop" for row in log_rows)
    assert any(row["type"] == "decision" for row in log_rows)


@pytest.mark.asyncio
async def test_reflection_run_skips_when_output_unparseable(tmp_path):
    memory = MemoryService(base_dir=tmp_path)
    agent = _StubReflectionAgent(memory, model_output="this is not json")

    await agent.run(
        session_id="session-2",
        transcript=[{"user": "Hello", "athena": "Hi"}],
    )

    assert not list((tmp_path / "sessions").glob("*.md"))
    assert not (tmp_path / "user" / "profile.yaml").exists()
    assert not (tmp_path / "facts" / "ongoing.md").exists()
