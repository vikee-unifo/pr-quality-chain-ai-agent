"""Benchmark harness: runs cases and scores validated findings against independently written labels.

Mock mode replays each case's scripted mock_responses.json. Its numbers only show that the pipeline
(validation, aggregation, policy, scoring) behaves as designed; they say nothing about model quality.
Live mode uses the configured real provider and is the only source of detection-performance claims.
Labels (expected.json) are read here only; they are never part of the review input sent to a model.

Seeded-issue matching (matches_seeded), all deterministic:
  1. same file, and the finding's category is one of the issue's `categories`;
  2. the finding's own words (title, description, impact, recommendation) contain at least one term from
     every group in `concept_terms` (e.g. an ownership term AND an authorization term);
  3. location: if the finding has lines, they must overlap one of `line_ranges` (old or new side of the
     diff) within `line_tolerance`. A finding without lines only counts if its validated evidence quotes
     the defect (contains one of `evidence_terms`). Mentioning a word never earns credit on its own.
Only findings that passed validation are scored; withheld findings are reported separately.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.benchmark import CASES_DIR, list_cases, load_case
from app.config import Settings
from app.llm import ModelConnector
from app.llm.mock import MockProvider
from app.models import Finding, ReviewResult, Severity, ValidationStatus
from app.orchestrator import ReviewStore, run_review


class LiveConfigError(ValueError):
    """Live mode was requested without a real provider configured."""


def require_live_provider(settings: Settings) -> None:
    if settings.llm_provider == "mock":
        raise LiveConfigError(
            "live mode requires a real provider: set LLM_PROVIDER=litellm and LLM_MODEL=<provider>/<model> "
            f"(current LLM_PROVIDER={settings.llm_provider!r}); refusing to fall back to the mock provider"
        )


def _words(f: Finding) -> str:
    return f"{f.title} {f.description} {f.impact} {f.recommendation}".lower()


def matches_seeded(f: Finding, issue: dict) -> bool:
    if f.file_path != issue["file_path"] or f.category.value not in issue["categories"]:
        return False
    text = _words(f)
    if not all(any(term.lower() in text for term in group) for group in issue["concept_terms"]):
        return False
    if f.start_line is None:
        return any(term in f.evidence for term in issue.get("evidence_terms", []))
    tolerance = issue.get("line_tolerance", 0)
    start, end = f.start_line, f.end_line or f.start_line
    return any(start <= hi + tolerance and end >= lo - tolerance for lo, hi in issue["line_ranges"].values())


def matches_acceptable(f: Finding, item: dict) -> bool:
    return f.file_path in item["file_paths"] and f.category.value == item["category"]


def _agent_rows(result: ReviewResult) -> list[dict]:
    return [
        {
            "agent": o.agent,
            "status": o.status,
            "error_category": (o.error or {}).get("category"),
            "http_status": (o.error or {}).get("status"),
            "attempts": o.attempts,
            "duration_ms": o.duration_ms,
            "input_tokens": o.input_tokens,  # None = provider did not report it
            "output_tokens": o.output_tokens,
            "cost_usd": o.cost_usd,  # None = unknown, never 0
            "schema_rejected": o.rejected_count,
        }
        for o in result.agent_outcomes
    ]


async def evaluate_case(
    name: str,
    connector: ModelConnector,
    settings: Settings,
    cases_dir: Path = CASES_DIR,
    store: ReviewStore | None = None,
) -> dict:
    pr, expected, _ = load_case(name, cases_dir)
    result = await run_review(pr, connector, settings, store)
    accepted = [f for f in result.findings if f.validation is ValidationStatus.VALID]
    seeded, acceptable = expected["seeded_issues"], expected.get("acceptable_findings", [])

    hits: dict[str, list[Finding]] = {item["id"]: [] for item in seeded + acceptable}
    unlabeled = []
    for f in accepted:
        label = next((i["id"] for i in seeded if matches_seeded(f, i)), None) or next(
            (i["id"] for i in acceptable if matches_acceptable(f, i)), None
        )
        if label:
            hits[label].append(f)
        else:
            unlabeled.append(f.title)

    high = [i for i in seeded if Severity(i["severity"]).rank >= Severity.HIGH.rank]
    detected = [i for i in seeded if hits[i["id"]]]
    agents = _agent_rows(result)
    return {
        "case": name,
        "run_id": result.run_id,
        "provider": result.provider,
        "model": result.model,
        "simulated": result.simulated,
        "status": result.status.value,
        "recommendation": result.recommendation.value,
        "review_ms": result.duration_ms,
        "agents": agents,
        "input_tokens": _sum_known(a["input_tokens"] for a in agents),
        "output_tokens": _sum_known(a["output_tokens"] for a in agents),
        "cost_usd": _sum_known(a["cost_usd"] for a in agents),
        # Finding funnel
        "findings_generated": result.raw_finding_count,
        "schema_rejected": sum(a["schema_rejected"] for a in agents),
        "after_aggregation": len(result.findings),
        "accepted_by_validation": len(accepted),
        "withheld_by_validation": len(result.findings) - len(accepted),
        # Scoring against labels (accepted findings only)
        "seeded": len(seeded),
        "seeded_detected": len(detected),
        "high_seeded": len(high),
        "high_seeded_detected": sum(bool(hits[i["id"]]) for i in high),
        "severity_matches": sum(any(f.severity.value == i["severity"] for f in hits[i["id"]]) for i in detected),
        "labeled_accepted": sum(len(v) for v in hits.values()),
        "unlabeled_accepted": unlabeled,  # need human adjudication; counted as false positives
        "duplicates": sum(max(0, len(v) - 1) for v in hits.values()),
    }


def _sum_known(values) -> float | None:
    """Sum of reported values; None if nothing was reported (unknown is not zero)."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _ratio(num: int, den: int) -> dict:
    return {"value": round(num / den, 3) if den else None, "numerator": num, "denominator": den}


TOTAL_KEYS = (
    "findings_generated",
    "schema_rejected",
    "after_aggregation",
    "accepted_by_validation",
    "withheld_by_validation",
    "seeded",
    "seeded_detected",
    "high_seeded",
    "high_seeded_detected",
    "severity_matches",
    "labeled_accepted",
    "duplicates",
)


MEASURED_STATUSES = ("complete", "partial")  # a failed or cancelled review produced nothing to score


def evaluation_status(rows: list[dict]) -> str:
    """complete: every review complete; partial: some agent or case failed; failed: nothing was measured."""
    if not any(r["status"] in MEASURED_STATUSES for r in rows):
        return "failed"
    return "complete" if all(r["status"] == "complete" for r in rows) else "partial"


def summarize(rows: list[dict]) -> dict:
    # Unmeasured cases are left out of every denominator: their detection is N/A, not 0.
    measured = [r for r in rows if r["status"] in MEASURED_STATUSES]
    t = {k: sum(r[k] for r in measured) for k in TOTAL_KEYS}
    simulated = {r["simulated"] for r in rows}
    agents = [a for r in rows for a in r["agents"]]
    return {
        "cases": len(rows),
        "mode": "MOCK (scripted; not model performance)"
        if simulated == {True}
        else "LIVE"
        if simulated == {False}
        else "MIXED (invalid: do not report)",
        "providers": sorted({f"{r['provider']}/{r['model']}" for r in rows}),
        "evaluation_status": evaluation_status(rows),
        "unmeasured_cases": [r["case"] for r in rows if r["status"] not in MEASURED_STATUSES],
        "seeded_detection": _ratio(t["seeded_detected"], t["seeded"]),
        "high_severity_detection": _ratio(t["high_seeded_detected"], t["high_seeded"]),
        "severity_accuracy": _ratio(t["severity_matches"], t["seeded_detected"]),
        "precision_vs_labels": _ratio(t["labeled_accepted"], t["accepted_by_validation"]),
        "duplicate_rate": _ratio(t["duplicates"], t["accepted_by_validation"]),
        "validation_acceptance": _ratio(t["accepted_by_validation"], t["after_aggregation"]),
        "agent_calls": len(agents),
        "agent_failures": sum(a["status"] != "succeeded" for a in agents),
        "input_tokens": _sum_known(a["input_tokens"] for a in agents),
        "output_tokens": _sum_known(a["output_tokens"] for a in agents),
        "cost_usd": _sum_known(a["cost_usd"] for a in agents),
        "agents_without_cost": sum(a["cost_usd"] is None for a in agents),
        "totals": t,
    }


async def run_benchmark(
    live: bool, settings: Settings, cases_dir: Path = CASES_DIR, store=None
) -> tuple[list[dict], dict]:
    from app.llm.connector import build_connector

    if live:
        require_live_provider(settings)
    rows = []
    started = datetime.now(timezone.utc)
    for name in list_cases(cases_dir):
        if live:
            connector = build_connector(settings)
        else:
            script = cases_dir / name / "mock_responses.json"
            connector = ModelConnector(MockProvider.from_file(str(script)), "mock-model")
        rows.append(await evaluate_case(name, connector, settings, cases_dir, store))
    summary = summarize(rows)
    if store is not None:
        summary["evaluation_id"] = uuid.uuid4().hex[:12]
        store.save_evaluation(summary["evaluation_id"], started, summary, rows)
    return rows, summary


def _fmt(ratio: dict) -> str:
    if ratio["value"] is None:
        return "N/A (nothing to measure)"
    return f"{ratio['value']} ({ratio['numerator']}/{ratio['denominator']})"


def _n(value) -> str:
    return "unavailable" if value is None else str(round(value, 6) if isinstance(value, float) else value)


def _detected(row: dict) -> str:
    return f"{row['seeded_detected']}/{row['seeded']}" if row["status"] in MEASURED_STATUSES else "N/A"


def render_markdown(rows: list[dict], summary: dict) -> str:
    status = summary["evaluation_status"]
    heading = {"complete": "", "partial": ": PARTIAL (incomplete)", "failed": ": FAILED (not measured)"}[status]
    out = [f"# Benchmark results: {summary['mode']}{heading}", ""]
    if status == "failed":
        out += [
            "> **Not measured.** No usable review result was produced (every selected agent failed; see the "
            "agent table). Detection metrics are N/A, not 0.",
            "",
        ]
    elif status == "partial":
        out += [
            f"> **Incomplete evaluation.** {summary['agent_failures']} agent call(s) failed"
            + (f"; not measured: {', '.join(summary['unmeasured_cases'])}" if summary["unmeasured_cases"] else "")
            + ". Metrics cover only the results that were produced. Do not report this as a complete evaluation.",
            "",
        ]
    if summary["mode"].startswith("MOCK"):
        out += ["> Scripted mock responses. These numbers test the pipeline, not a model's detection ability.", ""]
    out += [f"Provider/model: {', '.join(summary['providers'])}"]
    if summary.get("evaluation_id"):
        out += [f"Evaluation ID: {summary['evaluation_id']} (stored in SQLite with per-case review runs)"]
    out += [
        "",
        "| Case | Run | Status | Recommendation | Seeded detected | Generated | Schema-rejected | After dedupe | "
        "Accepted | Withheld | Labeled | Duplicates | Time (ms) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    out += [
        f"| {r['case']} | {r['run_id']} | {r['status']} | {r['recommendation']} | "
        f"{_detected(r)} | {r['findings_generated']} | {r['schema_rejected']} | "
        f"{r['after_aggregation']} | {r['accepted_by_validation']} | {r['withheld_by_validation']} | "
        f"{r['labeled_accepted']} | {r['duplicates']} | {r['review_ms']} |"
        for r in rows
    ]
    out += [
        "",
        "| Case | Agent | Status | Error | Attempts | ms | Input tok | Output tok | Cost USD |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    out += [
        f"| {r['case']} | {a['agent']} | {a['status']} | {a['error_category'] or '-'} | {a['attempts']} | "
        f"{a['duration_ms']} | {_n(a['input_tokens'])} | {_n(a['output_tokens'])} | {_n(a['cost_usd'])} |"
        for r in rows
        for a in r["agents"]
    ]
    out += [
        "",
        f"- Cases: {summary['cases']}",
        f"- Seeded-issue detection (accepted findings only): {_fmt(summary['seeded_detection'])}",
        f"- High-severity seeded detection: {_fmt(summary['high_severity_detection'])} (target ≥ 0.8)",
        f"- Severity accuracy of detected issues: {_fmt(summary['severity_accuracy'])}",
        f"- Precision vs labels: {_fmt(summary['precision_vs_labels'])} (target ≥ 0.7; denominator = accepted "
        "findings; unlabeled ones count as false positives until a human adjudicates them)",
        f"- Duplicate rate: {_fmt(summary['duplicate_rate'])} (target < 0.1)",
        f"- Validation acceptance: {_fmt(summary['validation_acceptance'])} of aggregated findings passed "
        "location/evidence checks; the rest were withheld",
        "- Published findings are all validated because invalid ones are filtered out before publishing; that is "
        "a design guarantee, not a measured result.",
        f"- Agent calls: {summary['agent_calls']}, failed: {summary['agent_failures']}; tokens in/out: "
        f"{_n(summary['input_tokens'])}/{_n(summary['output_tokens'])}; cost USD: {_n(summary['cost_usd'])} "
        f"({summary['agents_without_cost']} agent call(s) without reported cost)",
    ]
    return "\n".join(out)
