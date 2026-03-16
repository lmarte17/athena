from app.orchestrator.contracts import Mode, Tone
from app.orchestrator.clarification_loop import ClarificationLoop
from app.orchestrator.correction_loop import CorrectionLoop
from app.orchestrator.direct_response_spoke import DirectResponseSpoke, _fallback_response, _tone_for_mode


def test_direct_response_spoke_does_not_use_adk_output_schema():
    spoke = DirectResponseSpoke()

    assert spoke._agent.output_schema is None


def test_clarification_loop_does_not_use_adk_output_schema():
    loop = ClarificationLoop()

    assert loop._agent.sub_agents[0].output_schema is None


def test_correction_loop_does_not_use_adk_output_schema():
    loop = CorrectionLoop()

    assert loop._agent.sub_agents[0].output_schema is None


def test_direct_response_spoke_uses_bridge_tone_for_task_starts():
    assert _tone_for_mode(Mode.respond_and_start_tasks) == Tone.bridge
    assert _fallback_response(Mode.respond_and_start_tasks).tone == Tone.bridge
