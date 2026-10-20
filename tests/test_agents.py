import asyncio
import json

import pytest

from app.agents import AGENTS, run_agent
from tests.conftest import benchmark_script, mock_connector

GOOD = {
    "findings": [
        {
            "title": "Bug here",
            "description": "desc",
            "severity": "high",
            "category": "logic",
            "file_path": "a.py",
            "start_line": 1,
            "evidence": "x = 1",
            "confidence": 0.7,
        }
    ]
}


def run(agent, script, **kw):
    return asyncio.run(run_agent(AGENTS[agent], "ctx", mock_connector({agent: script}, **kw), {"run_id": "r"}))


@pytest.mark.parametrize("agent", ["code_quality", "security", "testing"])
def test_each_agent_returns_structured_findings(agent):
    outcome, findings = run(agent, [benchmark_script()[agent][0]])
    assert outcome.status == "succeeded" and outcome.simulated
    assert findings and all(f.agent == agent and f.id.startswith(agent) for f in findings)


def test_valid_finding_fields_are_preserved():
    outcome, [f] = run("security", [GOOD])
    assert (f.title, f.severity.value, f.start_line, f.confidence) == ("Bug here", "high", 1, 0.7)


def test_json_in_markdown_fence_is_accepted():
    outcome, findings = run("security", ["```json\n" + json.dumps(GOOD) + "\n```"])
    assert outcome.status == "succeeded" and len(findings) == 1


def test_empty_findings_list_is_success():
    outcome, findings = run("security", [{"findings": []}])
    assert outcome.status == "succeeded" and findings == []


@pytest.mark.parametrize(
    "content, category",
    [
        ("{not json", "malformed_response"),
        ('{"issues": []}', "schema_validation"),
        ('{"findings": "none"}', "schema_validation"),
        ("", "empty_response"),
    ],
)
def test_unusable_output_fails_the_agent(content, category):
    outcome, findings = run("security", [content])
    assert outcome.status == "failed" and outcome.error["category"] == category and findings == []


def test_schema_invalid_items_are_rejected_and_counted():
    bad = {"title": "x", "description": "short", "severity": "urgent", "category": "logic", "file_path": "a.py"}
    reversed_lines = {**GOOD["findings"][0], "start_line": 9, "end_line": 2}
    outcome, findings = run("security", [{"findings": [GOOD["findings"][0], bad, reversed_lines]}])
    assert outcome.status == "succeeded" and len(findings) == 1 and outcome.rejected_count == 2


def test_provider_failure_is_reported_not_hidden():
    outcome, findings = run("testing", [{"error": "quota"}])
    assert outcome.status == "failed" and findings == []
    assert outcome.error["category"] == "quota_exhausted" and outcome.error["status"] == 429


# ---- H2: recorded attempts match the connector's real attempts ----


@pytest.mark.parametrize(
    "script, status, attempts",
    [
        ([GOOD], "succeeded", 1),  # first-attempt success
        ([{"error": "rate_limit"}, GOOD], "succeeded", 2),  # rate limited, then succeeded
        ([{"error": "server"}], "failed", 3),  # retries exhausted (max_attempts=3)
        ([{"error": "auth"}], "failed", 1),  # permanent: not retried
        ([{"error": "timeout"}, "{not json"], "failed", 2),  # call succeeded on retry, output unusable
    ],
)
def test_outcome_records_actual_attempts(script, status, attempts):
    outcome, _ = run("security", script, max_attempts=3)
    assert (outcome.status, outcome.attempts) == (status, attempts)


# ---- M2: prose around the JSON object is tolerated; the schema stays strict ----


def test_json_surrounded_by_prose_is_parsed():
    content = "Here is my review:\n```json\n" + json.dumps(GOOD) + "\n```\nLet me know if you need more."
    outcome, findings = run("security", [content])
    assert outcome.status == "succeeded" and len(findings) == 1


@pytest.mark.parametrize(
    "content, category",
    [
        ('Sure! {"issues": []} done', "schema_validation"),
        ("I found no problems.", "malformed_response"),
        ("result: {broken", "malformed_response"),
    ],
)
def test_prose_does_not_relax_validation(content, category):
    outcome, _ = run("security", [content])
    assert outcome.status == "failed" and outcome.error["category"] == category
