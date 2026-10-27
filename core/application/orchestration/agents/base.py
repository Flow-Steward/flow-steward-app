"""
BaseAgent — the standard interface every Flow Steward agent implements.

The orchestrator only knows about this interface. That's the entire contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.application.registry.skill_manifest import SkillManifest
    from core.application.registry.step_output_contract import StepOutputContract


@dataclass
class AgentInput:
    """Standard envelope the orchestrator passes to any agent."""

    task_context: dict  # Original task + KB context + task_envelope
    step_context: dict  # The active step and its execution identity
    agent_config: dict  # From the manifest's config section
    skill: str = ""  # Which skill to execute this run
    feedback: str | None = None  # From orchestrator on retry/rework
    state_ref: dict | None = None  # Full OrchestratorState reference (for complex agents)
    output_contract: StepOutputContract | None = (
        None  # Immutable host capture; never model-authored.
    )
    selected_skill: SkillManifest | None = None  # Authorized skill selected with that capture.


@dataclass
class AgentOutput:
    """Standard envelope every agent returns to the orchestrator."""

    status: str  # "pass" | "fail" | "escalate" | "blocked"
    artifacts: dict[str, Any]  # Named outputs matching manifest's outputs list
    summary: str  # Human-readable summary for orchestrator journal
    confidence: float = 1.0  # 0.0 - 1.0
    metadata: dict = field(default_factory=dict)  # Telemetry: tokens, duration, model, cost
    operation_retry_signal: dict[str, Any] | None = None  # Host-owned runtime channel.

    def is_success(self) -> bool:
        return self.status == "pass"

    def needs_retry(self) -> bool:
        return self.status in ("fail",)

    def needs_escalation(self) -> bool:
        return self.status in ("escalate", "blocked")


class BaseAgent(ABC):
    """Every agent implements this. That's the entire contract."""

    @abstractmethod
    def execute(self, input: AgentInput) -> AgentOutput:
        """Do the work. Return results."""
        ...

    def validate_input(self, input: AgentInput) -> list[str]:
        """Optional: check that required inputs are present.
        Returns list of missing/invalid fields. Empty = good to go."""
        return []

    def on_cancel(self) -> None:
        """Optional: cleanup if the orchestrator cancels mid-execution."""
        _ = self
