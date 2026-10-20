"""Markdown report rendering. Model-written text is sanitized so it cannot spoof our markers or ping users."""

from __future__ import annotations

from app.models import Basis, Finding, Recommendation, ReviewResult, ReviewStatus, ValidationStatus

RECOMMENDATION_TEXT = {
    Recommendation.PROCEED_WITH_HUMAN_REVIEW: "Proceed with human review",
    Recommendation.REQUEST_CHANGES: "Request changes",
    Recommendation.ESCALATE: "Escalate for senior human review",
}


def clean(text: str, limit: int = 1500) -> str:
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;").replace("@", "@\u200b")
    return text if len(text) <= limit else text[:limit] + "…"


def _code(text: str) -> str:
    return "~~~\n" + clean(text, 1200).replace("~~~", "～～～") + "\n~~~"


def _location(f: Finding) -> str:
    if f.start_line is None:
        return f"`{f.file_path}`"
    lines = f"{f.start_line}" if f.end_line in (None, f.start_line) else f"{f.start_line}-{f.end_line}"
    return f"`{f.file_path}:{lines}`"


def _basis(f: Finding) -> str:
    return "Tool-confirmed" if f.basis is Basis.TOOL_CONFIRMED else "AI hypothesis"


def _finding(f: Finding) -> list[str]:
    out = [
        f"#### [{f.priority.value if f.priority else '?'}] {clean(f.title, 200)}",
        f"**{f.severity.value}** · {f.category.value} · {_location(f)} · {_basis(f)} · "
        f"confidence {f.confidence:.0%} · reported by {', '.join(f.agents)}",
        "",
        clean(f.description),
    ]
    if f.evidence:
        out += ["", "Evidence:", _code(f.evidence)]
    if f.impact:
        out += ["", f"**Impact:** {clean(f.impact)}"]
    if f.recommendation:
        out += ["", f"**Suggested fix:** {clean(f.recommendation)}"]
    out += ["", f"_Validation: {f.validation.value} ({'; '.join(f.validation_notes)})_"]
    for conflict in f.conflicts:
        out.append(f"_⚠️ {clean(conflict, 300)}_")
    return out + [""]


def render(result: ReviewResult, publishable: bool = False) -> str:
    """publishable=True renders exactly what may be posted: only findings that passed validation."""
    shown = [f for f in result.findings if f.validation is ValidationStatus.VALID] if publishable else result.findings
    withheld = len(result.findings) - len(shown)
    out = [f"## PR Quality Chain review: {result.status.value}", ""]
    if result.simulated:
        out += [
            "> **Simulated run.** Produced by the offline mock provider from scripted responses. "
            "This is not a real model inference.",
            "",
        ]
    if result.status is not ReviewStatus.COMPLETE:
        out += [
            f"> **Incomplete review ({result.status.value}).** Not all selected review agents finished. "
            "Do not treat the absence of findings as a clean result.",
            "",
        ]
    out += [
        f"**Recommendation: {RECOMMENDATION_TEXT[result.recommendation]}** "
        f"({'; '.join(result.recommendation_reasons)})",
        "",
        "_PR Quality Chain never approves or merges. A human reviewer makes the final decision._",
        "",
        f"**Risk:** {result.risk.level}. {'; '.join(result.risk.reasons)}",
        "",
        "**Agents:** "
        + (
            ", ".join(
                f"{o.agent} "
                + (
                    "✅"
                    if o.status == "succeeded"
                    else f"❌ {o.status}" + (f" ({o.error['category']}, {o.attempts} attempt(s))" if o.error else "")
                )
                for o in result.agent_outcomes
            )
            or "none selected"
        ),
        "",
        f"### Findings ({len(shown)})",
        "",
    ]
    if shown:
        out += ["| Priority | Severity | Finding | Location | Basis | Validation |", "|---|---|---|---|---|---|"]
        out += [
            f"| {f.priority.value if f.priority else '?'} | {f.severity.value} | {clean(f.title, 120)} | "
            f"{_location(f)} | {_basis(f)} | {f.validation.value} |"
            for f in shown
        ]
        out.append("")
        for f in shown:
            out += _finding(f)
    else:
        out += ["No findings.", ""]
    if withheld:
        out += [
            f"_{withheld} finding(s) withheld because they failed location or evidence validation. "
            "They are available in the review preview._",
            "",
        ]

    human = [f for f in shown if f.needs_human]
    if human:
        out += ["### Needs human judgment", ""]
        out += [f"- {clean(f.title, 150)} ({_location(f)})" for f in human]
        out.append("")

    out += ["### Validation checks", ""]
    if result.checks:
        out += [
            f"- {clean(c.name, 100)} ({c.kind}): **{c.status}**" + (f": {clean(c.summary, 300)}" if c.summary else "")
            for c in result.checks
        ]
    else:
        out.append("- No CI, lint or security-scanner results were supplied.")
    out += ["- PR Quality Chain does not execute PR code; it only uses results reported by CI.", ""]

    out += ["### Observed facts", ""] + [f"- {clean(fact, 300)}" for fact in result.observed_facts] + [""]
    out.append(f"<sub>run {result.run_id} · {result.provider}/{result.model} · {result.duration_ms} ms</sub>")
    return "\n".join(out)
