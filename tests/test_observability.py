import json

import pytest

from app.observability import configure_logging, log_event, redact, safe_message


def test_known_secret_formats_are_redacted():
    for secret in ["sk-ant-api03-abcdefghijklmnop", "ghp_" + "a" * 36, "github_pat_" + "b" * 30, "AIza" + "c" * 35]:
        assert secret not in redact(f"failed with {secret} here")
    assert "abc123def456" not in redact('{"Authorization": "Bearer abc123def456"}')
    assert "hunter2pass" not in redact("api_key=hunter2pass")


def test_registered_and_env_secrets_never_reach_logs(log_stream, monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "plain-secret-value-123")
    configure_logging("DEBUG", secrets=["configured-token-xyz"])
    log_event(
        "model_call",
        detail="key plain-secret-value-123 and configured-token-xyz",
        headers={"x-api-key": "plain-secret-value-123"},
    )
    output = log_stream.getvalue()
    assert "plain-secret-value-123" not in output and "configured-token-xyz" not in output
    assert json.loads(output.splitlines()[-1])["event"] == "model_call"


# ---- M4: masked keys, auth headers and provider error strings ----


@pytest.mark.parametrize(
    "text, secret",
    [
        (
            "Incorrect API key provided: sk-proj-********************abcd. You can find your API key at "
            "https://platform.openai.com/account/api-keys.",
            "abcd",
        ),
        ("key sk-ant-****wxyz was rejected", "wxyz"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ('{"authorization": "Bearer abc123def456ghi"}', "abc123def456ghi"),
        ("x-api-key: sk-ant-api03-abcdefghijk", "abcdefghijk"),
        ("x-goog-api-key=AIzaSyA1234567890abcdefghijklmnopqrs", "1234567890abcdef"),
        ("aws AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
        ("gemini key AQ.Ab8FAKEfakeFAKEfake0123456789xyz rejected", "Ab8FAKEfakeFAKEfake0123456789xyz"),
        ("token ghp_" + "a" * 36, "a" * 36),
    ],
)
def test_credentials_in_provider_messages_are_redacted(text, secret):
    assert secret not in redact(text)
    assert secret not in safe_message(text)


@pytest.mark.parametrize(
    "text",
    [
        "status 429: tokens per minute exceeded; max token count 4000",
        "invalid x-api-key",
        "model claude-sonnet-5-5 not found",
        "You can find your API key at https://platform.openai.com/account/api-keys.",
    ],
)
def test_useful_diagnostics_are_kept(text):
    assert redact(text) == text


# ---- M5: INFO logs carry safe operational metadata for successful calls ----


def test_cli_info_logs_report_model_calls_without_content(monkeypatch, capsys):
    from app.cli import main

    monkeypatch.setenv("LOG_LEVEL", "INFO")
    assert main(["review", "--case", "authorization-bypass"]) == 0
    err = capsys.readouterr().err
    calls = [json.loads(line) for line in err.splitlines() if '"model_call"' in line]
    finished = [json.loads(line) for line in err.splitlines() if '"agent_finished"' in line]
    assert len(calls) == 3 and all(c["outcome"] == "success" and c["attempt"] == 1 for c in calls)
    assert {c["provider"] for c in calls} == {"mock"} and all("duration_ms" in c for c in calls)
    assert all(f["attempts"] == 1 and f["usage_available"] is False for f in finished)
    assert "ownerId" not in err and "untrusted_pr_content" not in err and "You are the" not in err
