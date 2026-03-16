"""Stable contracts for the conversation orchestrator runtime."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AthenaContractModel(BaseModel):
    """Base model for orchestrator contracts.

    Extra fields are rejected so contract drift is explicit.
    """

    model_config = ConfigDict(extra="forbid")


class TurnRecord(AthenaContractModel):
    role: Literal["user", "assistant", "system"]
    content: str


class InterruptContext(AthenaContractModel):
    interrupted: bool = False
    reason: str | None = None
    prior_turn_id: str | None = None


class Mode(str, Enum):
    respond_now = "respond_now"
    start_tasks = "start_tasks"
    respond_and_start_tasks = "respond_and_start_tasks"
    ask_clarify = "ask_clarify"
    await_confirmation = "await_confirmation"
    noop = "noop"


class Tone(str, Enum):
    bridge = "bridge"
    direct_answer = "direct_answer"
    clarification = "clarification"
    correction = "correction"
    completion = "completion"


class RunPolicy(str, Enum):
    blocking = "blocking"
    background = "background"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class SurfaceMode(str, Enum):
    immediate = "immediate"
    wait_for_silence = "wait_for_silence"
    next_turn = "next_turn"
    silent_state_only = "silent_state_only"


class ResponsePlan(AthenaContractModel):
    text: str
    tone: Tone
    interruptible: bool = True
    priority: int = 5
    channel: Literal["voice", "text", "both"] = "voice"


class TaskSpec(AthenaContractModel):
    task_id: str
    task_kind: str
    goal: str
    input_payload: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    run_policy: RunPolicy = RunPolicy.background
    dedupe_key: str | None = None
    confirmation_required: bool = False
    surface_on_completion: bool = True


class PendingConfirmation(AthenaContractModel):
    confirmation_id: str
    session_id: str
    source_task_id: str
    task_spec: TaskSpec
    action_preview: str
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    source_summary: str = ""
    created_at: datetime
    expires_at: datetime | None = None


class FollowUpTaskPlan(AthenaContractModel):
    user_request: str
    acknowledgment: str
    task_kind: str = "general"
    resource_hints: list[str] = Field(default_factory=list)


class TaskResult(AthenaContractModel):
    task_id: str
    session_id: str
    status: TaskStatus
    summary: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    resource_handles: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)
    error: str | None = None
    task_metadata: dict[str, Any] = Field(default_factory=dict)


class SurfacePlan(AthenaContractModel):
    surface_mode: SurfaceMode
    response_plan: ResponsePlan
    replace_prior_assumption: bool = False
    coalesce_with_task_ids: list[str] = Field(default_factory=list)


class OrchestratorDecision(AthenaContractModel):
    decision_id: str
    session_id: str
    source_event_id: str
    mode: Mode
    response_plan: ResponsePlan | None = None
    state_updates: dict[str, Any] = Field(default_factory=dict)
    task_specs: list[TaskSpec] = Field(default_factory=list)
    clarification_request: str | None = None
    surface_plan: SurfacePlan | None = None
    supersedes_decision_id: str | None = None


class TurnEnvelope(AthenaContractModel):
    session_id: str
    turn_id: str
    transcript: str
    timestamp: datetime
    recent_turns: list[TurnRecord] = Field(default_factory=list)
    active_task_ids: list[str] = Field(default_factory=list)
    screen_context_refs: list[str] = Field(default_factory=list)
    memory_context_ref: str | None = None
    interrupt_context: InterruptContext | None = None
    partial_segments: list[str] = Field(default_factory=list)
    raw_modality_summary: str | None = None
    user_speaking_duration_ms: int | None = None
    source: Literal["voice", "typed"] = "voice"
