"""Classifies the change, selects agents, runs them with bounded parallelism, then validates,
aggregates and applies policy. Everything after the model calls is deterministic."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Protocol

from app import aggregate, policy
from app.agents import AGENTS, run_agent
from app.config import Settings
from app.diff import ChangedFile, build_context_text, parse_diff
from app.llm import ModelConnector
from app.models import AgentOutcome, Finding, PRInput, ReviewResult, ReviewStatus, RiskAssessment
from app.observability import log_event
from app.validation import tool_findings, validate

SECURITY_PATH = re.compile(
    r"auth|login|session|token|passw|secret|crypt|permission|acl|role|admin|account|payment|transfer|billing|"
    r"user|middleware|security|policy",
    re.IGNORECASE,
)
SECURITY_CODE = re.compile(
    r"owner|authori[sz]|permission|forbidden|isAdmin|role|password|token|secret|jwt|session|csrf|cors|"
    r"\bexec\(|\beval\(|subprocess|\bquery\(|\bsql\b|innerHTML|dangerouslySetInnerHTML|crypto|hash|verify",
    re.IGNORECASE,
)
CONFIG_PATH = re.compile(r"\.(json|ya?ml|toml|ini|cfg|conf)$|Dockerfile|(^|/)\.env|\.github/workflows/")
DEPENDENCY_PATH = re.compile(
    r"(^|/)(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|requirements[^/]*\.txt|pyproject\.toml|poetry\.lock|"
    r"go\.(mod|sum)|Cargo\.(toml|lock)|Gemfile(\.lock)?|pom\.xml|build\.gradle)$"
)


class ReviewStore(Protocol):
    def save(self, result: ReviewResult) -> None: ...


def _changed_lines(f: ChangedFile) -> list[str]:
    return [line[1:] for h in f.hunks for line in h.lines if line[:1] in "+-" and line[:1]]


def classify(files: list[ChangedFile]) -> RiskAssessment:
    types, reasons = set(), []
    for f in files:
        if f.is_test:
            types.add("tests")
        elif f.is_doc:
            types.add("docs")
        elif DEPENDENCY_PATH.search(f.path):
            types.add("dependencies")
        elif CONFIG_PATH.search(f.path):
            types.add("config")
        else:
            types.add("source")
        if not f.is_test and not f.is_doc:
            if SECURITY_PATH.search(f.path):
                types.add("security_sensitive")
                reasons.append(f"security-sensitive path: {f.path}")
            hits = sorted({m.group(0).lower() for line in _changed_lines(f) for m in SECURITY_CODE.finditer(line)})
            if hits:
                types.add("security_sensitive")
                reasons.append(f"security-relevant terms changed in {f.path}: {', '.join(hits[:5])}")

    if "security_sensitive" in types:
        level = "high"
    elif types & {"source", "dependencies", "config"}:
        level = "medium"
    else:
        level = "low"

    selected = []
    if "source" in types or level == "high":
        selected.append("code_quality")
    if types & {"security_sensitive", "dependencies", "config"}:
        selected.append("security")
    if types & {"source", "tests"}:
        selected.append("testing")
    if not selected:
        reasons.append("no reviewable code changes (documentation only or empty diff)")
    reasons.append(f"change types: {', '.join(sorted(types)) or 'none'}; risk {level}")
    return RiskAssessment(level=level, change_types=sorted(types), reasons=reasons, selected_agents=selected)


def observed_facts(pr: PRInput, files: list[ChangedFile]) -> list[str]:
    tests = [f.path for f in files if f.is_test]
    source = [f.path for f in files if not f.is_test and not f.is_doc]
    facts = [
        f"{len(files)} file(s) changed: +{sum(f.additions for f in files)} / -{sum(f.deletions for f in files)} lines",
        f"test files changed: {', '.join(tests) if tests else 'none'}",
    ]
    if source and not tests:
        facts.append(f"source changed without any test changes: {', '.join(source)}")
    removed_security = sorted(
        {
            f.path
            for f in files
            for h in f.hunks
            for line in h.lines
            if line.startswith("-") and SECURITY_CODE.search(line)
        }
    )
    if removed_security:
        facts.append(f"removed lines contain security-relevant terms in: {', '.join(removed_security)}")
    if pr.tool_results:
        facts += [f"CI/tool result supplied: {t.name} ({t.kind}) = {t.status}" for t in pr.tool_results]
    else:
        facts.append("no CI/tool results were supplied; PR Quality Chain did not run any tests or tools")
    return facts


async def run_review(
    pr: PRInput,
    connector: ModelConnector,
    settings: Settings,
    store: ReviewStore | None = None,
    run_id: str | None = None,
) -> ReviewResult:
    run_id = run_id or uuid.uuid4().hex[:12]
    correlation_id = uuid.uuid4().hex
    started_at, started = datetime.now(timezone.utc), time.monotonic()
    meta = {"run_id": run_id, "correlation_id": correlation_id}

    files = parse_diff(pr.diff)[: settings.max_files]
    risk = classify(files)
    facts = observed_facts(pr, files)
    log_event(
        "review_started",
        **meta,
        repo=pr.repo,
        pr=pr.pr_number,
        files=len(files),
        risk=risk.level,
        agents=risk.selected_agents,
    )

    semaphore = asyncio.Semaphore(settings.max_parallel_agents)

    async def bounded(name: str) -> tuple[AgentOutcome, list[Finding]]:
        spec = AGENTS[name]
        context, *_ = build_context_text(
            pr.title,
            pr.description,
            [f for f in files if spec.reviews(f)],
            pr.context_files,
            max_chars=settings.max_context_chars,
            max_file_chars=settings.max_file_patch_chars,
            facts=facts,
        )
        async with semaphore:
            try:
                return await asyncio.wait_for(run_agent(spec, context, connector, meta), settings.agent_timeout_s)
            except asyncio.TimeoutError:
                log_event(
                    "agent_finished", logging.WARNING, **meta, agent=name, outcome="failed", error_category="timeout"
                )
                return AgentOutcome(
                    agent=name,
                    status="failed",
                    simulated=connector.simulated,
                    error={"category": "timeout", "message": "agent time budget exceeded", "retryable": True},
                ), []

    def finish(status: ReviewStatus, outcomes: list[AgentOutcome], ai_findings: list[Finding]) -> ReviewResult:
        raw = ai_findings + tool_findings(pr.tool_results)
        file_map = {f.path: f for f in files}
        contents = {**pr.context_files, **pr.file_contents}
        for finding in raw:
            validate(finding, file_map, contents)
        findings = policy.apply(aggregate.aggregate(raw))
        failed = [o.agent for o in outcomes if o.status != "succeeded"]
        recommendation, reasons = policy.recommend(status, findings, pr.tool_results, failed)
        result = ReviewResult(
            run_id=run_id,
            correlation_id=correlation_id,
            repo=pr.repo,
            pr_number=pr.pr_number,
            head_sha=pr.head_sha,
            title=pr.title,
            status=status,
            recommendation=recommendation,
            recommendation_reasons=reasons,
            provider=connector.provider.name,
            model=connector.model,
            simulated=connector.simulated,
            risk=risk,
            observed_facts=facts,
            checks=pr.tool_results,
            agent_outcomes=outcomes,
            findings=findings,
            raw_finding_count=len(raw),
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if store:
            store.save(result)
        log_event(
            "review_finished",
            **meta,
            status=status.value,
            recommendation=recommendation.value,
            findings=len(findings),
            raw_findings=len(raw),
            failed_agents=failed or None,
            duration_ms=result.duration_ms,
        )
        return result

    try:
        results = await asyncio.gather(*(bounded(name) for name in risk.selected_agents))
    except asyncio.CancelledError:
        cancelled = [
            AgentOutcome(agent=n, status="cancelled", simulated=connector.simulated) for n in risk.selected_agents
        ]
        finish(ReviewStatus.CANCELLED, cancelled, [])
        raise

    outcomes = [outcome for outcome, _ in results]
    succeeded = [o for o in outcomes if o.status == "succeeded"]
    if len(succeeded) == len(outcomes):
        status = ReviewStatus.COMPLETE
    elif succeeded:
        status = ReviewStatus.PARTIAL
    else:
        status = ReviewStatus.FAILED
    return finish(status, outcomes, [f for _, found in results for f in found])
