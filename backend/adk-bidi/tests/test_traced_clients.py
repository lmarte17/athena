from app.memory_service import MemoryService
from app.planner import planner_agent
from app.planner.planner_agent import PlannerAgent
from app.planner.skill_library import SkillLibrary
from app.reflection_agent import ReflectionAgent
from app.retrieval import embedder
from app.tap_agent import IncrementalTapAgent


def test_planner_agent_uses_shared_traced_gemini_factory(monkeypatch):
    sentinel = object()
    calls: list[tuple[str, dict]] = []

    def fake_create_gemini_client(component: str, **kwargs):
        calls.append((component, kwargs))
        return sentinel

    monkeypatch.setattr(planner_agent, "create_gemini_client", fake_create_gemini_client)

    agent = PlannerAgent()

    assert agent._get_client() is sentinel
    assert agent._get_client() is sentinel
    assert calls == [
        (
            "athena.planner",
            {"model": planner_agent._PLANNER_MODEL, "tags": ["planner"]},
        )
    ]


def test_tap_agent_uses_shared_traced_gemini_factory(monkeypatch, tmp_path):
    from app import tap_agent

    sentinel = object()
    calls: list[tuple[str, dict]] = []

    def fake_create_gemini_client(component: str, **kwargs):
        calls.append((component, kwargs))
        return sentinel

    monkeypatch.setattr(tap_agent, "create_gemini_client", fake_create_gemini_client)

    agent = IncrementalTapAgent(MemoryService(base_dir=tmp_path))

    assert agent._get_client() is sentinel
    assert agent._get_client() is sentinel
    assert calls == [
        (
            "athena.memory.tap",
            {"model": tap_agent._MEMORY_TAP_MODEL, "tags": ["memory", "tap"]},
        )
    ]


def test_reflection_agent_uses_shared_traced_gemini_factory(monkeypatch, tmp_path):
    from app import reflection_agent

    sentinel = object()
    calls: list[tuple[str, dict]] = []

    def fake_create_gemini_client(component: str, **kwargs):
        calls.append((component, kwargs))
        return sentinel

    monkeypatch.setattr(reflection_agent, "create_gemini_client", fake_create_gemini_client)

    agent = ReflectionAgent(MemoryService(base_dir=tmp_path))

    assert agent._get_client() is sentinel
    assert agent._get_client() is sentinel
    assert calls == [
        (
            "athena.memory.reflection",
            {"model": reflection_agent._MEMORY_REFLECT_MODEL, "tags": ["memory", "reflection"]},
        )
    ]


def test_skill_library_uses_shared_traced_gemini_factory(monkeypatch, tmp_path):
    from app.planner import skill_library

    sentinel = object()
    calls: list[tuple[str, dict]] = []

    def fake_create_gemini_client(component: str, **kwargs):
        calls.append((component, kwargs))
        return sentinel

    monkeypatch.setattr(skill_library, "create_gemini_client", fake_create_gemini_client)

    library = SkillLibrary(db_path=str(tmp_path / "skills.db"))

    assert library._get_client() is sentinel
    assert library._get_client() is sentinel
    assert calls == [
        (
            "athena.skill_library",
            {"model": skill_library._DISTILL_MODEL, "tags": ["skill-library"]},
        )
    ]


def test_embedder_uses_shared_traced_gemini_factory(monkeypatch):
    sentinel = object()
    calls: list[tuple[str, dict]] = []

    def fake_create_gemini_client(component: str, **kwargs):
        calls.append((component, kwargs))
        return sentinel

    embedder._client.cache_clear()
    monkeypatch.setattr(embedder, "create_gemini_client", fake_create_gemini_client)

    assert embedder._client() is sentinel
    assert embedder._client() is sentinel
    assert calls == [
        (
            "athena.retrieval.embedder",
            {"model": embedder._EMBED_MODEL, "tags": ["retrieval", "embedding"]},
        )
    ]
    embedder._client.cache_clear()
