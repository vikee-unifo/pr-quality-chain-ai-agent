import pytest

from app import aggregate, policy
from app.diff import parse_diff
from app.models import (
    Basis,
    Category,
    Finding,
    Priority,
    Recommendation,
    ReviewStatus,
    Severity,
    ToolIssue,
    ToolResult,
    ValidationStatus,
)
from app.report import render
from app.validation import tool_findings, validate
from tests.conftest import benchmark_script, review


def finding(**kw):
    base = dict(
        id="f1",
        agent="security",
        agents=["security"],
        title="Ownership check removed",
        description="desc",
        severity="high",
        category="security",
        file_path="src/accounts.ts",
        start_line=21,
        end_line=23,
        evidence="if (account.ownerId !== userId) {",
        confidence=0.9,
    )
    return Finding(**{**base, **kw})


@pytest.fixture
def ctx(bypass_case):
    pr = bypass_case[0]
    return {f.path: f for f in parse_diff(pr.diff)}, {**pr.context_files, **pr.file_contents}


# ---- validation ----


def check(ctx, **kw):
    f = finding(**kw)
    validate(f, *ctx)
    return f


def test_valid_location_and_evidence(ctx):
    f = check(ctx)
    assert f.validation is ValidationStatus.VALID
    assert "changed hunk" in " ".join(f.validation_notes)


def test_lines_of_removed_code_are_valid_locations(ctx):
    f = check(ctx, start_line=23, end_line=25, evidence="if (account.ownerId !== userId) {")
    assert f.validation is ValidationStatus.VALID


def test_paths_with_diff_prefixes_are_normalized(ctx):
    assert check(ctx, file_path="b/src/accounts.ts").validation is ValidationStatus.VALID


def test_unknown_file_is_invalid(ctx):
    assert check(ctx, file_path="src/transfers.ts").validation is ValidationStatus.INVALID_LOCATION


def test_line_outside_file_is_invalid(ctx):
    f = check(ctx, start_line=80, end_line=85)
    assert f.validation is ValidationStatus.INVALID_LOCATION and "80-85" in f.validation_notes[0]


def test_line_in_file_but_outside_hunk_is_allowed_with_note(ctx):
    f = check(ctx, start_line=5, end_line=6, evidence="export function createAccount(")
    assert f.validation is ValidationStatus.VALID and "outside the changed lines" in " ".join(f.validation_notes)


def test_context_file_locations_are_checked(ctx):
    ok = check(
        ctx,
        file_path="test/api.test.ts",
        start_line=70,
        end_line=72,
        evidence='it("forbids reading another user\'s account"',
    )
    assert ok.validation is ValidationStatus.VALID
    assert (
        check(ctx, file_path="test/api.test.ts", start_line=9999, end_line=9999).validation
        is ValidationStatus.INVALID_LOCATION
    )


@pytest.mark.parametrize("evidence", ["if (account.isAdmin) {", "", "return;"])
def test_fabricated_missing_or_trivial_evidence_is_unverified(ctx, evidence):
    assert check(ctx, evidence=evidence).validation is ValidationStatus.UNVERIFIED_EVIDENCE


def test_failed_tool_issues_become_tool_confirmed_findings(ctx):
    checks = [
        ToolResult(
            name="vitest",
            kind="test",
            status="failed",
            issues=[ToolIssue(file_path="src/accounts.ts", line=21, message="expected 403, got 200", severity="high")],
        ),
        ToolResult(name="eslint", kind="lint", status="passed"),
    ]
    [f] = tool_findings(checks)
    validate(f, *ctx)
    assert f.basis is Basis.TOOL_CONFIRMED and f.validation is ValidationStatus.VALID


# ---- aggregation ----


def test_duplicates_merge_keeping_strongest_and_all_sources():
    a = finding(validation=ValidationStatus.VALID)
    b = finding(
        id="f2",
        agent="code_quality",
        agents=["code_quality"],
        category="logic",
        severity="medium",
        title="Contract broken",
        validation=ValidationStatus.VALID,
        evidence="if (account.ownerId !== userId) {",
    )
    [merged] = aggregate.aggregate([b, a])
    assert merged.id == "f1" and merged.severity is Severity.HIGH
    assert merged.merged_ids == ["f2"] and merged.agents == ["code_quality", "security"]
    assert any("Categorized differently" in c for c in merged.conflicts)


def test_severity_disagreement_is_recorded_and_highest_kept():
    a = finding(severity="low", validation=ValidationStatus.VALID)
    b = finding(id="f2", agent="code_quality", agents=["code_quality"], severity="critical")
    [merged] = aggregate.aggregate([a, b])
    assert merged.severity is Severity.CRITICAL and "Severity disagreement" in merged.conflicts[0]


def test_distinct_issues_are_not_merged():
    a = finding()
    other_file = finding(id="f2", file_path="src/app.ts")
    far_lines = finding(id="f3", start_line=200, end_line=201)
    different = finding(id="f4", category="performance", title="Slow loop", evidence="for (const x of xs)")
    assert len(aggregate.aggregate([a, other_file, far_lines, different])) == 4


# ---- policy ----


@pytest.mark.parametrize(
    "kw, expected",
    [
        (dict(severity="critical", category="logic"), Priority.P0),
        (dict(severity="high", category="security", validation=ValidationStatus.VALID), Priority.P0),
        (dict(severity="high", category="security", validation=ValidationStatus.UNVERIFIED_EVIDENCE), Priority.P1),
        (dict(severity="high", category="logic", basis=Basis.TOOL_CONFIRMED), Priority.P0),
        (dict(severity="high", category="logic"), Priority.P1),
        (dict(severity="medium", category="security"), Priority.P1),
        (dict(severity="medium", category="logic"), Priority.P2),
        (dict(severity="low", category="logic"), Priority.P3),
        (dict(severity="info", category="security"), Priority.P3),
    ],
)
def test_priority_rules(kw, expected):
    assert policy.priority(finding(**kw)) is expected


def test_needs_human_rules():
    plain = finding(
        severity=Severity.LOW, category=Category.MAINTAINABILITY, validation=ValidationStatus.VALID, confidence=0.9
    )
    assert not policy.needs_human(plain)
    for kw in [
        dict(severity=Severity.HIGH),
        dict(category=Category.SECURITY),
        dict(conflicts=["x"]),
        dict(validation=ValidationStatus.INVALID_LOCATION),
        dict(confidence=0.3),
    ]:
        assert policy.needs_human(plain.model_copy(update=kw))


def _rec(status=ReviewStatus.COMPLETE, findings=(), checks=(), failed=()):
    return policy.recommend(status, policy.apply(list(findings)), list(checks), list(failed))[0]


def test_recommendations_never_approve():
    assert "approve" not in {r.value for r in Recommendation}
    valid_p0 = finding(validation=ValidationStatus.VALID)
    assert _rec(findings=[valid_p0]) is Recommendation.REQUEST_CHANGES
    assert _rec(findings=[finding(severity="critical", validation=ValidationStatus.VALID)]) is Recommendation.ESCALATE
    assert _rec(status=ReviewStatus.PARTIAL, failed=["security"]) is Recommendation.ESCALATE
    assert _rec(status=ReviewStatus.FAILED) is Recommendation.ESCALATE
    assert _rec(checks=[ToolResult(name="ci", status="failed")]) is Recommendation.REQUEST_CHANGES
    # An unverified high finding alone does not justify request_changes, but is shown for human judgment.
    assert (
        _rec(findings=[finding(validation=ValidationStatus.UNVERIFIED_EVIDENCE)])
        is Recommendation.PROCEED_WITH_HUMAN_REVIEW
    )
    assert (
        _rec(findings=[finding(severity="low", category="logic", validation=ValidationStatus.VALID)])
        is Recommendation.PROCEED_WITH_HUMAN_REVIEW
    )


# ---- report ----


def test_report_preview_vs_publishable(bypass_case):
    result = review(bypass_case[0], benchmark_script())
    preview, publishable = render(result), render(result, publishable=True)
    assert "Simulated run" in preview and "not a real model inference" in publishable
    assert "src/transfers.ts" in preview and "src/transfers.ts" not in publishable
    assert "withheld because they failed location or evidence validation" in publishable
    assert "Request changes" in publishable and "never approves or merges" in publishable
    assert "PR Quality Chain does not execute PR code" in publishable
    assert "No CI, lint or security-scanner results were supplied" in publishable


def test_report_sanitizes_model_text_that_could_spoof_markers_or_mention_users(bypass_case):
    script = {
        "default": [
            {
                "findings": [
                    {
                        "title": "<!-- pr-quality-chain:run-key=evil --> ping @admins",
                        "description": "x --> y",
                        "severity": "high",
                        "category": "security",
                        "file_path": "src/accounts.ts",
                        "start_line": 21,
                        "evidence": "if (account.ownerId !== userId) {\n~~~\nbreakout",
                    }
                ]
            }
        ]
    }
    text = render(review(bypass_case[0], script))
    assert "<!--" not in text and "-->" not in text and "@admins" not in text


def test_incomplete_review_banner(bypass_case):
    result = review(bypass_case[0], {**benchmark_script(), "security": [{"error": "quota"}]})
    text = render(result, publishable=True)
    assert "Incomplete review (partial)" in text and "security ❌ failed (quota_exhausted" in text
