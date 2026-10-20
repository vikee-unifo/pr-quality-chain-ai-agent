"""Deterministic deduplication. Merges never drop information: the strongest finding leads,
the others are recorded in merged_ids/agents, and disagreements are recorded in conflicts."""

from __future__ import annotations

from difflib import SequenceMatcher

from app.models import Basis, Finding, ValidationStatus

LINE_TOLERANCE = 3


def _lines(f: Finding) -> tuple[int, int] | None:
    return (f.start_line, f.end_line or f.start_line) if f.start_line else None


def same_issue(a: Finding, b: Finding) -> bool:
    if a.file_path != b.file_path:
        return False
    la, lb = _lines(a), _lines(b)
    if la and lb and (la[0] > lb[1] + LINE_TOLERANCE or lb[0] > la[1] + LINE_TOLERANCE):
        return False
    if (la is None) != (lb is None):
        return False
    ea, eb = " ".join(a.evidence.split()), " ".join(b.evidence.split())
    evidence_overlap = bool(ea and eb and (ea in eb or eb in ea))
    title_similarity = SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio()
    return a.category == b.category or evidence_overlap or title_similarity >= 0.6


def _strength(f: Finding) -> tuple:
    return (
        f.validation is ValidationStatus.VALID,
        f.basis is Basis.TOOL_CONFIRMED,
        f.severity.rank,
        f.confidence,
        len(f.evidence),
    )


def merge(group: list[Finding]) -> Finding:
    group = sorted(group, key=_strength, reverse=True)
    lead = group[0].model_copy(deep=True)
    if len(group) == 1:
        return lead
    others = group[1:]
    lead.merged_ids = [f.id for f in others]
    lead.agents = sorted({a for f in group for a in f.agents})
    highest = max(group, key=lambda f: f.severity.rank)
    if highest.severity.rank > lead.severity.rank:
        lead.severity = highest.severity  # never let a merge lower the severity
    if any(f.basis is Basis.TOOL_CONFIRMED for f in group):
        lead.basis = Basis.TOOL_CONFIRMED
    lead.confidence = max(f.confidence for f in group)
    severities = {f.agent: f.severity.value for f in group}
    if max(f.severity.rank for f in group) - min(f.severity.rank for f in group) >= 2:
        lead.conflicts.append(
            "Severity disagreement: "
            + ", ".join(f"{a}={s}" for a, s in severities.items())
            + f"; kept {lead.severity.value}"
        )
    categories = {f.category.value for f in group}
    if len(categories) > 1:
        lead.conflicts.append(f"Categorized differently by agents: {', '.join(sorted(categories))}")
    lead.validation_notes.append(f"merged {len(others)} duplicate(s) from {', '.join(f.agent for f in others)}")
    return lead


def aggregate(findings: list[Finding]) -> list[Finding]:
    groups: list[list[Finding]] = []
    for finding in findings:
        for group in groups:
            if any(same_issue(finding, member) for member in group):
                group.append(finding)
                break
        else:
            groups.append([finding])
    return [merge(group) for group in groups]
