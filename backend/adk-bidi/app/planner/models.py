"""Planner data models — ExecutionPlan, PlanStep, Skill."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class PlanStep:
    """A single unit of work in an execution plan."""

    step_id: str
    # One of: gmail, drive, docs, calendar, sheets, slides, retrieval, action
    specialist: str
    # Natural-language instruction sent directly to the specialist agent
    instruction: str
    # step_ids that must complete before this step can start
    depends_on: list[str] = field(default_factory=list)
    # Human-readable key used to reference this step's output in later steps
    output_key: str = ""

    def __post_init__(self) -> None:
        if not self.output_key:
            self.output_key = self.step_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "specialist": self.specialist,
            "instruction": self.instruction,
            "depends_on": list(self.depends_on),
            "output_key": self.output_key,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> PlanStep:
        return PlanStep(
            step_id=str(d["step_id"]),
            specialist=str(d["specialist"]),
            instruction=str(d["instruction"]),
            depends_on=list(d.get("depends_on") or []),
            output_key=str(d.get("output_key") or d["step_id"]),
        )


@dataclass
class ExecutionPlan:
    """A DAG of PlanSteps produced by the PlannerAgent."""

    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    # True when the job can be handled by a single coordinator call (no decomposition).
    is_trivial: bool = False
    # Skill ID if this plan was instantiated from the skill library.
    skill_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "job_id": self.job_id,
            "steps": [s.to_dict() for s in self.steps],
            "is_trivial": self.is_trivial,
            "skill_id": self.skill_id,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id=str(d.get("plan_id") or uuid.uuid4()),
            job_id=str(d.get("job_id") or ""),
            steps=[PlanStep.from_dict(s) for s in (d.get("steps") or [])],
            is_trivial=bool(d.get("is_trivial", False)),
            skill_id=d.get("skill_id"),
        )


@dataclass
class Skill:
    """A reusable plan template distilled from a successful multi-step job."""

    skill_id: str
    name: str
    description: str
    # Keywords / short phrases; any matching the user request triggers this skill.
    trigger_patterns: list[str]
    # The generalised plan structure (step shapes without job-specific IDs/content).
    plan_template: dict[str, Any]
    use_count: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_used_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "trigger_patterns": self.trigger_patterns,
            "plan_template": self.plan_template,
            "use_count": self.use_count,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat(),
        }
