from app.planner.execution_engine import _enrich_instruction
from app.planner.models import PlanStep


def test_enrich_instruction_includes_dependency_content_and_resource_handles():
    step = PlanStep(
        step_id="step_2",
        specialist="slides",
        instruction="Create a presentation from the source material.",
        depends_on=["step_1"],
    )
    prior = {
        "step_1": {
            "summary": "Read the strategy document.",
            "artifacts": [
                {
                    "type": "google_doc",
                    "id": "doc-123",
                    "title": "Q2 Strategy",
                    "content": "Priority one is expansion. Priority two is retention.",
                }
            ],
            "resource_handles": [
                {
                    "source": "docs",
                    "kind": "document",
                    "id": "doc-123",
                    "title": "Q2 Strategy",
                    "url": "https://docs.google.com/document/d/doc-123/edit",
                }
            ],
        }
    }

    enriched = _enrich_instruction(step, prior)

    assert "Use the dependency excerpts below as source material for this step." in enriched
    assert "Result from step_1: Read the strategy document." in enriched
    assert "Source excerpt from step_1 / Q2 Strategy:" in enriched
    assert "Priority one is expansion. Priority two is retention." in enriched
    assert (
        "Resource handle from step_1: docs/document Q2 Strategy [id=doc-123]"
        in enriched
    )
    assert enriched.endswith("Task: Create a presentation from the source material.")
