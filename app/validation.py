"""Deterministic checks that a finding's file, lines and evidence exist in the PR context."""

from __future__ import annotations

from app.diff import ChangedFile, hunk_line_ranges, normalize_path
from app.models import Basis, Category, Finding, ToolResult, ValidationStatus

LINE_TOLERANCE = 3
MIN_EVIDENCE_CHARS = 10  # shorter snippets match almost anywhere and prove nothing


def _norm(line: str) -> str:
    if line[:1] in ("+", "-"):
        line = line[1:]
    return " ".join(line.split())


def _haystack(f: ChangedFile | None, content: str | None) -> str:
    lines = [line for h in f.hunks for line in h.lines] if f else []
    lines += content.splitlines() if content else []
    return "\n".join(_norm(line) for line in lines)


def validate(finding: Finding, files: dict[str, ChangedFile], contents: dict[str, str]) -> None:
    """Sets finding.validation and finding.validation_notes in place."""
    notes = finding.validation_notes
    path = normalize_path(finding.file_path)
    finding.file_path = path
    changed, content = files.get(path), contents.get(path)
    if changed is None and content is None:
        finding.validation = ValidationStatus.INVALID_LOCATION
        notes.append("file is not part of the PR diff or provided context")
        return

    if finding.start_line is not None:
        start, end = finding.start_line, finding.end_line or finding.start_line
        sides = {
            side
            for side, lo, hi in (hunk_line_ranges(changed) if changed else [])
            if start <= hi + LINE_TOLERANCE and end >= lo - LINE_TOLERANCE
        }
        if "new" in sides:
            notes.append("lines are within a changed hunk")
        elif "old" in sides:
            notes.append("lines refer to the pre-change version of a changed hunk")
        elif content is not None and end <= len(content.splitlines()):
            notes.append("lines exist in the file but are outside the changed lines")
        else:
            finding.validation = ValidationStatus.INVALID_LOCATION
            notes.append(f"lines {start}-{end} are not in the changed hunks or the provided file content")
            return
    else:
        notes.append("no line number given; file-level finding")

    if finding.basis is Basis.TOOL_CONFIRMED:
        finding.validation = ValidationStatus.VALID
        notes.append("reported by a deterministic tool")
        return
    evidence = [_norm(line) for line in finding.evidence.splitlines() if _norm(line)]
    haystack = _haystack(changed, content)
    if sum(map(len, evidence)) >= MIN_EVIDENCE_CHARS and all(line in haystack for line in evidence):
        finding.validation = ValidationStatus.VALID
        notes.append("evidence matches the source verbatim")
    else:
        finding.validation = ValidationStatus.UNVERIFIED_EVIDENCE
        notes.append("evidence was missing, too short, or not found verbatim in the diff or file")


_KIND_CATEGORY = {"security": Category.SECURITY, "test": Category.REGRESSION, "lint": Category.MAINTAINABILITY}


def tool_findings(checks: list[ToolResult]) -> list[Finding]:
    """Turns issues from failed CI/tool checks into findings confirmed by that tool."""
    findings = []
    for check in checks:
        if check.status != "failed":
            continue
        for i, issue in enumerate(check.issues, 1):
            findings.append(
                Finding(
                    id=f"tool-{check.name}-{i}",
                    agent=f"tool:{check.name}",
                    agents=[f"tool:{check.name}"],
                    basis=Basis.TOOL_CONFIRMED,
                    title=f"{check.name}: {issue.message[:150]}",
                    description=issue.message,
                    severity=issue.severity,
                    category=_KIND_CATEGORY.get(check.kind, Category.LOGIC),
                    file_path=issue.file_path,
                    start_line=issue.line,
                    end_line=issue.line,
                    evidence="",
                    confidence=1.0,
                )
            )
    return findings
