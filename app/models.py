"""Stable data contracts: PR input, findings, agent outcomes, and review results."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return ["info", "low", "medium", "high", "critical"].index(self.value)


class Category(str, Enum):
    SECURITY = "security"
    LOGIC = "logic"
    ERROR_HANDLING = "error_handling"
    REGRESSION = "regression"
    TESTING = "testing"
    MAINTAINABILITY = "maintainability"
    PERFORMANCE = "performance"


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Basis(str, Enum):
    """How we know: an AI hypothesis, or a result reported by a deterministic tool (tests, linter, scanner)."""

    AI_HYPOTHESIS = "ai_hypothesis"
    TOOL_CONFIRMED = "tool_confirmed"


class ValidationStatus(str, Enum):
    VALID = "valid"  # location exists in the PR context and the evidence matches the source
    UNVERIFIED_EVIDENCE = "unverified_evidence"  # location ok, evidence not found verbatim
    INVALID_LOCATION = "invalid_location"  # file or line not present in the available context
    PENDING = "pending"


class Recommendation(str, Enum):
    """Deliberately has no 'approve' value: final decisions belong to a human."""

    PROCEED_WITH_HUMAN_REVIEW = "proceed_with_human_review"
    REQUEST_CHANGES = "request_changes"
    ESCALATE = "escalate"


class ReviewStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"  # at least one selected agent failed
    FAILED = "failed"  # every selected agent failed
    CANCELLED = "cancelled"


# ---- Input ----


class ToolResult(BaseModel):
    """A test, lint or security-scanner result supplied by CI. PR Quality Chain never runs PR code itself."""

    name: str
    kind: str = Field("ci", description="test | lint | security | ci")
    status: str = Field(description="passed | failed | error | skipped | unavailable")
    summary: str = ""
    issues: list[ToolIssue] = Field(default_factory=list)


class ToolIssue(BaseModel):
    file_path: str
    line: int | None = Field(None, ge=1)
    message: str = Field(max_length=500)
    severity: Severity = Severity.MEDIUM


ToolResult.model_rebuild()


class PRInput(BaseModel):
    title: str = Field("", max_length=500)
    description: str = Field("", max_length=20_000)
    diff: str = Field(max_length=5_000_000)
    repo: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    # Full post-change content of changed files (optional), used for validation and extra context.
    file_contents: dict[str, str] = Field(default_factory=dict)
    # Unchanged but related files, e.g. existing tests for a changed module.
    context_files: dict[str, str] = Field(default_factory=dict)
    tool_results: list[ToolResult] = Field(default_factory=list)


# ---- Findings ----


class AgentFinding(BaseModel):
    """What an agent (model) must return for each finding."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=3000)
    severity: Severity
    category: Category
    file_path: str = Field(min_length=1, max_length=500)
    start_line: int | None = Field(None, ge=1)
    end_line: int | None = Field(None, ge=1)
    evidence: str = Field("", max_length=2000, description="Code copied verbatim from the diff or file")
    impact: str = Field("", max_length=1500)
    recommendation: str = Field("", max_length=1500)
    confidence: float = Field(0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _line_order(self) -> AgentFinding:
        if self.end_line is not None and self.start_line is None:
            self.start_line = self.end_line
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class AgentOutput(BaseModel):
    findings: list[dict] = Field(max_length=25)


class Finding(AgentFinding):
    id: str
    agent: str
    basis: Basis = Basis.AI_HYPOTHESIS
    validation: ValidationStatus = ValidationStatus.PENDING
    validation_notes: list[str] = Field(default_factory=list)
    priority: Priority | None = None
    needs_human: bool = False
    agents: list[str] = Field(default_factory=list, description="All agents/tools that reported this issue")
    merged_ids: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


# ---- Run results ----


class AgentOutcome(BaseModel):
    agent: str
    status: str  # succeeded | failed | cancelled
    simulated: bool = False
    finding_count: int = 0
    rejected_count: int = 0  # findings dropped for failing the schema
    error: dict | None = None
    attempts: int = 0
    duration_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class RiskAssessment(BaseModel):
    level: str  # low | medium | high
    change_types: list[str]
    reasons: list[str]
    selected_agents: list[str]


class ReviewResult(BaseModel):
    run_id: str
    correlation_id: str
    repo: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    title: str = ""
    status: ReviewStatus
    recommendation: Recommendation
    recommendation_reasons: list[str]
    provider: str
    model: str
    simulated: bool
    risk: RiskAssessment
    observed_facts: list[str]
    checks: list[ToolResult]
    agent_outcomes: list[AgentOutcome]
    findings: list[Finding]
    raw_finding_count: int
    started_at: datetime
    duration_ms: int
