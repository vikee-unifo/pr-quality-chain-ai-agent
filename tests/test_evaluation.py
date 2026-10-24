import asyncio
import json
import shutil

import pytest

from app.benchmark import CASES_DIR
from app.cli import main
from app.config import Settings
from app.evaluation import LiveConfigError, matches_seeded, run_benchmark, summarize
from app.models import Finding
from app.store import Store

ISSUE = json.loads((CASES_DIR / "authorization-bypass" / "expected.json").read_text())["seeded_issues"][0]


def finding(**kw):
    base = dict(
        id="f",
        agent="security",
        title="Ownership check removed: broken authorization",
        description="getOwnedAccount no longer checks ownership, so another user can read the account.",
        severity="high",
        category="security",
        file_path="src/accounts.ts",
        start_line=21,
        end_line=23,
        evidence="if (account.ownerId !== userId) {",
    )
    return Finding(**{**base, **kw})


# ---- B3: seeded-issue matching ----


@pytest.mark.parametrize(
    "kw",
    [
        {},
        dict(start_line=19, end_line=20),  # new-side hunk lines
        dict(title="IDOR in getOwnedAccount", description="The owner comparison was deleted; any user can transfer."),
        dict(start_line=None, end_line=None),  # no lines, but evidence quotes the removed check
    ],
)
def test_security_finding_on_removed_ownership_check_matches(kw):
    assert matches_seeded(finding(**kw), ISSUE)


@pytest.mark.parametrize(
    "kw",
    [
        # low-severity naming nit at the right line
        dict(
            title="Consider renaming variable",
            description="Style nit.",
            severity="low",
            category="maintainability",
            evidence="",
        ),
        # generic "map access" observation, even labelled security, no lines, no evidence
        dict(
            title="Repeated map access",
            description="Avoid repeated map access.",
            start_line=None,
            end_line=None,
            evidence="",
        ),
        # right words and lines, wrong category
        dict(category="logic"),
        # right words and category, unsupported location (createAccount, far from the check)
        dict(start_line=5, end_line=6),
        # only one concept group: ownership mentioned, no authorization consequence
        dict(title="Owner field unused", description="ownerId is no longer read here."),
        # different file
        dict(file_path="src/app.ts"),
        # no lines and evidence that does not quote the check
        dict(start_line=None, end_line=None, evidence="return account;"),
    ],
)
def test_generic_or_misplaced_findings_do_not_count_as_detection(kw):
    assert not matches_seeded(finding(**kw), ISSUE)


# ---- M1 / H4: metrics and persistence ----


def test_mock_benchmark_reports_funnel_and_denominators(tmp_path):
    store = Store(str(tmp_path / "eval.db"))
    rows, summary = asyncio.run(run_benchmark(False, Settings(), store=store, names=["authorization-bypass"]))
    [row] = rows
    assert summary["mode"].startswith("MOCK") and row["simulated"] and row["provider"] == "mock"
    assert (row["findings_generated"], row["schema_rejected"], row["after_aggregation"]) == (5, 1, 4)
    assert (row["accepted_by_validation"], row["withheld_by_validation"]) == (2, 2)
    assert (row["seeded_detected"], row["high_seeded_detected"], row["labeled_accepted"]) == (1, 1, 2)
    assert summary["seeded_detection"] == {"value": 1.0, "numerator": 1, "denominator": 1}
    assert summary["validation_acceptance"] == {"value": 0.5, "numerator": 2, "denominator": 4}
    assert "published_validity" not in summary  # circular metric removed
    # Unknown is not zero: the mock reports no usage or cost.
    assert row["cost_usd"] is None and summary["cost_usd"] is None and summary["agents_without_cost"] == 3
    assert {a["attempts"] for a in row["agents"]} == {1}


def test_evaluation_is_persisted_with_linked_review_runs(tmp_path):
    store = Store(str(tmp_path / "eval.db"))
    rows, summary = asyncio.run(run_benchmark(False, Settings(), store=store))
    saved = store.get_evaluation(summary["evaluation_id"])
    assert saved["mode"] == summary["mode"] and saved["providers"] == "mock/mock-model"
    assert saved["rows"][0]["agents"] == rows[0]["agents"] and saved["summary"]["cost_usd"] is None
    assert store.get(rows[0]["run_id"]) is not None  # the review run itself is stored too
    dump = "\n".join(store.db.iterdump())
    assert "@@ -18,8 +18,5 @@" not in dump and "untrusted_pr_content" not in dump and "You are the" not in dump


def test_unlabeled_findings_reduce_precision_and_no_seeded_issue_is_not_100_percent(tmp_path):
    case = tmp_path / "clean-refactor"
    shutil.copytree(CASES_DIR / "authorization-bypass", case)
    (case / "expected.json").write_text(json.dumps({"seeded_issues": [], "acceptable_findings": []}))
    rows, summary = asyncio.run(run_benchmark(False, Settings(), cases_dir=tmp_path))
    assert summary["precision_vs_labels"] == {"value": 0.0, "numerator": 0, "denominator": 2}
    assert rows[0]["unlabeled_accepted"]
    assert summary["high_severity_detection"]["value"] is None


def test_summary_flags_duplicates_and_mixed_modes():
    row = {
        k: 0
        for k in (
            "findings_generated",
            "schema_rejected",
            "after_aggregation",
            "withheld_by_validation",
            "seeded",
            "seeded_detected",
            "high_seeded",
            "high_seeded_detected",
            "severity_matches",
        )
    }
    live = {
        **row,
        "case": "c",
        "status": "complete",
        "simulated": False,
        "provider": "anthropic",
        "model": "m",
        "agents": [],
        "accepted_by_validation": 4,
        "labeled_accepted": 4,
        "duplicates": 2,
    }
    assert summarize([live])["duplicate_rate"]["value"] == 0.5 and summarize([live])["mode"] == "LIVE"
    assert summarize([live, {**live, "simulated": True}])["mode"].startswith("MIXED")


def test_run_benchmark_refuses_live_mode_with_mock_provider():
    with pytest.raises(LiveConfigError):
        asyncio.run(run_benchmark(True, Settings()))


# ---- CLI ----


def test_cli_review_of_benchmark_case_prints_simulated_report(capsys):
    assert main(["review", "--case", "authorization-bypass", "--publishable"]) == 0
    out = capsys.readouterr().out
    assert "Simulated run" in out and "Ownership check removed" in out and "src/transfers.ts" not in out


def test_cli_evaluate_writes_results_and_database(tmp_path, capsys):
    out_file, db = tmp_path / "results.md", tmp_path / "r.db"
    assert main(["evaluate", "--out", str(out_file), "--db", str(db)]) == 0
    text = out_file.read_text()
    assert "MOCK" in text and "Provider/model: mock/mock-model" in text and "unavailable" in text
    assert Store(str(db)).db.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 1


# ---- the three-case benchmark ----

EXPECTED_BY_CASE = {
    # case: (agents selected, seeded issues, seeded detected by the scripted mock, labelled accepted findings)
    "authorization-bypass": (3, 1, 1, 2),
    "atomicity-rollback-removed": (3, 1, 1, 2),
    "zero-amount-validation-test-removed": (2, 2, 2, 2),
}


def test_full_mock_benchmark_covers_all_three_target_agents(tmp_path):
    rows, summary = asyncio.run(run_benchmark(False, Settings(), store=Store(str(tmp_path / "all.db"))))
    by_case = {r["case"]: r for r in rows}
    assert set(by_case) == set(EXPECTED_BY_CASE)
    for case, (agents, seeded, detected, labeled) in EXPECTED_BY_CASE.items():
        row = by_case[case]
        assert (len(row["agents"]), row["seeded"], row["seeded_detected"], row["labeled_accepted"]) == (
            agents,
            seeded,
            detected,
            labeled,
        ), case
        assert row["status"] == "complete" and row["unlabeled_accepted"] == [] and row["duplicates"] == 0
    assert summary["evaluation_status"] == "complete" and summary["agent_calls"] == 8
    assert summary["seeded_detection"] == {"value": 1.0, "numerator": 4, "denominator": 4}


def _issue(case, issue_id):
    labels = json.loads((CASES_DIR / case / "expected.json").read_text())["seeded_issues"]
    return next(i for i in labels if i["id"] == issue_id)


@pytest.mark.parametrize(
    "case, issue_id, kw",
    [
        # right file, lines and category, but nothing about rollback / balances
        (
            "atomicity-rollback-removed",
            "ATOM-1",
            dict(
                file_path="src/transfers.ts",
                category="logic",
                start_line=56,
                end_line=58,
                title="Simplify control flow",
                description="This block could be extracted into a helper.",
            ),
        ),
        # right words, wrong category for the atomicity defect
        (
            "atomicity-rollback-removed",
            "ATOM-1",
            dict(
                file_path="src/transfers.ts",
                category="maintainability",
                start_line=56,
                end_line=58,
                title="Rollback removed",
                description="Balances are no longer restored after a debit.",
            ),
        ),
        # validation file but no mention of zero
        (
            "zero-amount-validation-test-removed",
            "VAL-1",
            dict(
                file_path="src/validation.ts",
                category="logic",
                start_line=11,
                end_line=11,
                title="Error message",
                description="The amount error message could be clearer.",
            ),
        ),
        # test file, mentions zero, but not that tests were removed
        (
            "zero-amount-validation-test-removed",
            "TEST-2",
            dict(
                file_path="test/api.test.ts",
                category="testing",
                start_line=132,
                end_line=134,
                title="Zero test naming",
                description="Rename the zero amount test case.",
            ),
        ),
    ],
)
def test_new_labels_reject_generic_findings(case, issue_id, kw):
    assert not matches_seeded(finding(**{"evidence": "", **kw}), _issue(case, issue_id))


def test_cli_rejects_unknown_case_before_writing(tmp_path, capsys):
    db = tmp_path / "x.db"
    assert main(["evaluate", "--case", "nope", "--db", str(db)]) == 2
    assert "unknown case" in capsys.readouterr().err and not db.exists()


def test_cli_runs_only_selected_cases(tmp_path, capsys):
    assert main(["evaluate", "--case", "zero-amount-validation-test-removed", "--db", str(tmp_path / "s.db")]) == 0
    out = capsys.readouterr().out
    assert "zero-amount-validation-test-removed" in out and "authorization-bypass" not in out
