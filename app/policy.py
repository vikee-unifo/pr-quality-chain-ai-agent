"""Rule-based priority and recommendation policy. Pure functions, no model calls.

Priority rules (first match wins):
  P0  critical severity; or high-severity security finding with valid location/evidence;
      or high+ finding confirmed by a deterministic tool
  P1  any other high; or medium security
  P2  any other medium
  P3  low / info

A finding needs human attention when it is high+ severity, security-related, has a conflict between
agents, failed validation, or has confidence < 0.5.

Recommendation rules (first match wins). There is no "approve": a human always decides.
  escalate             review failed or was cancelled; or a valid critical finding exists
  escalate             review is partial (some agents failed), so coverage is incomplete
  request_changes      a CI/tool check failed; or a valid P0/P1 finding exists
  escalate             a high+ finding has an unresolved agent conflict
  proceed_with_human_review  otherwise
"""

from __future__ import annotations

from app.models import (
    Basis,
    Category,
    Finding,
    Priority,
    Recommendation,
    ReviewStatus,
    Severity,
    ToolResult,
    ValidationStatus,
)


def priority(f: Finding) -> Priority:
    valid = f.validation is ValidationStatus.VALID
    if f.severity is Severity.CRITICAL:
        return Priority.P0
    if f.severity is Severity.HIGH and ((f.category is Category.SECURITY and valid) or f.basis is Basis.TOOL_CONFIRMED):
        return Priority.P0
    if f.severity is Severity.HIGH or (f.severity is Severity.MEDIUM and f.category is Category.SECURITY):
        return Priority.P1
    if f.severity is Severity.MEDIUM:
        return Priority.P2
    return Priority.P3


def needs_human(f: Finding) -> bool:
    return (
        f.severity.rank >= Severity.HIGH.rank
        or f.category is Category.SECURITY
        or bool(f.conflicts)
        or f.validation is not ValidationStatus.VALID
        or f.confidence < 0.5
    )


def apply(findings: list[Finding]) -> list[Finding]:
    for f in findings:
        f.priority = priority(f)
        f.needs_human = needs_human(f)
    order = {p: i for i, p in enumerate(Priority)}
    return sorted(findings, key=lambda f: (order[f.priority], -f.severity.rank, f.file_path, f.start_line or 0))


def recommend(
    status: ReviewStatus, findings: list[Finding], checks: list[ToolResult], failed_agents: list[str]
) -> tuple[Recommendation, list[str]]:
    valid = [f for f in findings if f.validation is ValidationStatus.VALID]
    if status in (ReviewStatus.FAILED, ReviewStatus.CANCELLED):
        return Recommendation.ESCALATE, [f"AI review {status.value}; a full human review is required"]
    critical = [f for f in valid if f.severity is Severity.CRITICAL]
    if critical:
        return Recommendation.ESCALATE, [f"{len(critical)} validated critical finding(s)"]
    if status is ReviewStatus.PARTIAL:
        return Recommendation.ESCALATE, [f"review incomplete: agent(s) failed: {', '.join(failed_agents)}"]
    reasons = [f"CI/tool check failed: {c.name}" for c in checks if c.status == "failed"]
    serious = [f for f in valid if f.priority in (Priority.P0, Priority.P1)]
    if serious:
        reasons.append(f"{len(serious)} validated P0/P1 finding(s)")
    if reasons:
        return Recommendation.REQUEST_CHANGES, reasons
    if any(f.conflicts and f.severity.rank >= Severity.HIGH.rank for f in findings):
        return Recommendation.ESCALATE, ["agents disagree on a high-severity finding"]
    return Recommendation.PROCEED_WITH_HUMAN_REVIEW, ["no validated P0/P1 findings or failed checks"]
