"""evaluate --live failure reporting, driven through the real CLI with LiteLLM's call stubbed out (no network).

Distinguishes: missing credentials (exit 2, nothing written), every agent failing (exit 1, detection N/A),
some agents failing (exit 3, partial results kept), and a complete review that finds nothing (exit 0, 0/1).
"""

import json
import sqlite3

import pytest

from app.cli import main

litellm = pytest.importorskip("litellm")

SECURITY_FINDING = json.loads(
    (
        __import__("app.benchmark", fromlist=["CASES_DIR"]).CASES_DIR / "authorization-bypass" / "mock_responses.json"
    ).read_text()
)["security"][0]


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "LLM_API_KEY",
        "MOCK_SCRIPT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "litellm")
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-5-5")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "default.db"))
    return tmp_path


def stub_provider(monkeypatch, respond):
    """respond(agent_title) -> content string, or raises. Records which agents were called."""
    calls = []

    async def acompletion(**kwargs):
        title = kwargs["messages"][0]["content"].split(" of PR Quality Chain")[0].removeprefix("You are the ")
        calls.append(title)
        content = respond(title)
        return litellm.ModelResponse(
            id=f"req-{len(calls)}",
            model="claude-sonnet-5-5",
            choices=[{"message": {"role": "assistant", "content": content}}],
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return calls


def run(tmp_path, capsys):
    out, db = tmp_path / "live.md", tmp_path / "live.db"
    code = main(["evaluate", "--live", "--case", "authorization-bypass", "--out", str(out), "--db", str(db)])
    captured = capsys.readouterr()
    return code, captured, out, db


def stored_status(db):
    row = sqlite3.connect(db).execute("SELECT summary_json FROM evaluation_runs").fetchone()
    return json.loads(row[0])["evaluation_status"]


def test_missing_credentials_exit_2_before_anything_is_written(live_env, monkeypatch, capsys):
    calls = stub_provider(monkeypatch, lambda title: pytest.fail("provider must not be called"))
    code, captured, out, db = run(live_env, capsys)
    assert code == 2
    assert "ANTHROPIC_API_KEY" in captured.err and "anthropic/claude-sonnet-5-5" in captured.err
    assert captured.out == "" and calls == []
    assert not out.exists() and not db.exists() and not (live_env / "default.db").exists()


def test_all_agents_failing_is_not_measured(live_env, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-FAKE000000000")

    def fail(title):
        raise litellm.BadRequestError("Your credit balance is too low to access the Anthropic API.", "m", "anthropic")

    calls = stub_provider(monkeypatch, fail)
    code, captured, out, db = run(live_env, capsys)
    report = out.read_text()
    assert code == 1 and len(calls) == 3
    assert "LIVE: FAILED (not measured)" in report and "Detection metrics are N/A, not 0" in report
    assert "Seeded-issue detection (accepted findings only): N/A" in report
    assert "0/1" not in report and "0.0 (" not in report.split("```json")[0]
    assert "| N/A |" in report  # per-case detection column
    assert stored_status(db) == "failed"  # the failed attempt is still recorded
    assert "FAKE000000000" not in report and "FAKE000000000" not in captured.err


def test_partial_failure_keeps_results_but_is_marked_incomplete(live_env, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-FAKE000000000")

    def security_only(title):
        if title == "Security Review Agent":
            return json.dumps(SECURITY_FINDING)
        raise litellm.InternalServerError("overloaded", "anthropic", "m")

    stub_provider(monkeypatch, security_only)
    code, captured, out, db = run(live_env, capsys)
    report = out.read_text()
    assert code == 3
    assert "LIVE: PARTIAL (incomplete)" in report and "Do not report this as a complete evaluation" in report
    assert "2 agent call(s) failed" in report
    assert "Seeded-issue detection (accepted findings only): 1.0 (1/1)" in report  # available result preserved
    assert stored_status(db) == "partial"


def test_complete_review_with_no_detection_reports_zero(live_env, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-FAKE000000000")
    stub_provider(monkeypatch, lambda title: '{"findings": []}')
    code, captured, out, db = run(live_env, capsys)
    report = out.read_text()
    assert code == 0
    assert report.startswith("# Benchmark results: LIVE\n")
    assert "Seeded-issue detection (accepted findings only): 0.0 (0/1)" in report and "| 0/1 |" in report
    assert "N/A" in report  # precision has no findings to measure, which is genuinely N/A
    assert "FAILED" not in report and "PARTIAL" not in report
    assert stored_status(db) == "complete"


def test_mock_evaluation_is_unchanged(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert main(["evaluate", "--case", "authorization-bypass", "--db", str(tmp_path / "m.db")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Benchmark results: MOCK (scripted; not model performance)\n") and "1.0 (1/1)" in out
