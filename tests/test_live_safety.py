"""Guards that must hold before any live model call: no silent mock fallback, no answer leakage,
clear credential failures, and no network or prompt exposure from LiteLLM itself."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents import AGENTS, SYSTEM_TEMPLATE
from app.cli import main
from app.config import Settings
from app.llm import ErrorCategory, ModelConnector, ProviderError
from app.llm.mock import MockProvider
from app.orchestrator import run_review

ROOT = Path(__file__).resolve().parent.parent
PROVIDER_ENV = ("LLM_PROVIDER", "LLM_MODEL", "MOCK_SCRIPT_PATH", "LLM_API_KEY")


# ---- B1: explicit live mode never uses the mock ----


@pytest.fixture
def mock_calls(monkeypatch):
    calls = []

    async def spy(self, request):
        calls.append(request.metadata.get("agent"))
        raise AssertionError("mock provider must not be used in live mode")

    monkeypatch.setattr(MockProvider, "complete", spy)
    return calls


@pytest.mark.parametrize(
    "command",
    [
        ["review", "--case", "authorization-bypass", "--live"],
        ["evaluate", "--live"],
    ],
)
@pytest.mark.parametrize("provider", [None, "mock", "not-a-provider"])
def test_live_mode_refuses_without_real_provider(command, provider, monkeypatch, tmp_path, capsys, mock_calls):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    if provider:
        monkeypatch.setenv("LLM_PROVIDER", provider)
    db = tmp_path / "should-not-exist.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    out_file = tmp_path / "results.md"

    code = main(command + (["--out", str(out_file)] if command[0] == "evaluate" else []))

    captured = capsys.readouterr()
    assert code == 2
    assert "LLM_PROVIDER" in captured.err and "error:" in captured.err
    assert captured.out == ""  # no report, no fabricated results
    assert mock_calls == []
    assert not db.exists() and not out_file.exists()


def test_offline_mode_still_uses_the_scripted_mock(monkeypatch, capsys):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    assert main(["review", "--case", "authorization-bypass"]) == 0
    assert "Simulated run" in capsys.readouterr().out


# ---- B2: nothing in the model input reveals the benchmark answer ----


class Recorder(MockProvider):
    def __init__(self):
        super().__init__({"default": [{"findings": []}]})
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return await super().complete(request)


def test_context_sent_to_every_agent_contains_no_answer_or_labels():
    from app.benchmark import load_case

    pr, expected, case_dir = load_case("authorization-bypass")
    recorder = Recorder()
    asyncio.run(run_review(pr, ModelConnector(recorder, "m"), Settings()))
    assert sorted(r.metadata["agent"] for r in recorder.requests) == sorted(AGENTS)

    labels = expected["seeded_issues"] + expected["acceptable_findings"]
    forbidden = [
        "bypass",
        "seeded",
        "defect",
        "expected",
        "introduce",
        "AUTHZ-1",
        "TEST-1",
        *(item["description"] for item in labels),
    ]
    scripted = json.loads((case_dir / "mock_responses.json").read_text())
    forbidden += [
        f["title"] for steps in scripted.values() if isinstance(steps, list) for step in steps for f in step["findings"]
    ]
    for request in recorder.requests:
        user = request.messages[0].content
        spec = AGENTS[request.metadata["agent"]]
        assert request.system == SYSTEM_TEMPLATE.format(title=spec.title, focus=spec.focus)  # no case data
        for term in forbidden:
            assert term.lower() not in user.lower(), (request.metadata["agent"], term)
        # The evidence a reviewer needs is still there.
        assert "PR title: Simplify account lookup" in user
        assert "if (account.ownerId !== userId) {" in user and "getOwnedAccount" in user
        assert "forbids reading another user's account" in user  # legitimate regression tests


# ---- H1: missing credentials fail fast, offline, without retries or secrets ----

litellm = pytest.importorskip("litellm")
KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY")


@pytest.mark.parametrize(
    "model, variable",
    [
        ("anthropic/claude-sonnet-5-5", "ANTHROPIC_API_KEY"),
        ("openai/gpt-5", "OPENAI_API_KEY"),
        ("gemini/gemini-2.5-pro", "GEMINI_API_KEY"),
    ],
)
def test_missing_credentials_are_auth_errors_before_any_call(model, variable, monkeypatch):
    from app.llm.litellm_provider import LiteLLMProvider

    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UNRELATED_SERVICE_TOKEN", "unrelated-secret-value-123")
    calls = []

    async def acompletion(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    connector = ModelConnector(LiteLLMProvider(), model, max_attempts=3)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(connector.complete("s", "u", metadata={"agent": "security"}))
    error = exc.value
    assert error.category is ErrorCategory.AUTH and error.code == "missing_credentials"
    assert not error.retryable and error.attempts == 1 and calls == []
    assert model in error.message and variable in error.message
    assert "unrelated-secret-value-123" not in error.message


@pytest.mark.parametrize(
    "exc",
    [
        lambda: litellm.InternalServerError(
            "OpenAIException - Missing credentials. Please pass an `api_key`, or set the OPENAI_API_KEY", "openai", "m"
        ),
        lambda: litellm.APIConnectionError(
            "Missing Gemini API key. Set the GEMINI_API_KEY or GOOGLE_API_KEY environment variable", "gemini", "m"
        ),
        lambda: litellm.AuthenticationError("Missing Anthropic API Key - A call is being made", "anthropic", "m"),
    ],
)
def test_provider_missing_key_errors_are_classified_as_auth(exc):
    from app.llm.litellm_provider import classify_exception

    error = classify_exception(exc())
    assert error.category is ErrorCategory.AUTH and not error.retryable


# ---- H5 / M3: LiteLLM import and logging ----


def _clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("LITELLM_")}
    env["PYTHONPATH"] = str(ROOT)
    return env


PROBE = """
import os, socket
attempts = []
def blocked(*a, **k):
    attempts.append(a[:1]); raise OSError("blocked")
socket.socket.connect = blocked; socket.getaddrinfo = blocked; socket.create_connection = blocked
{imports}
import litellm
print(len(attempts), os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"), os.environ.get("LITELLM_LOG"),
      os.environ.get("LITELLM_MODE"))
"""


def test_importing_the_app_prevents_litellm_network_access_on_import():
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(imports="import app")],
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.stdout.split() == ["0", "True", "ERROR", "PRODUCTION"], result.stderr[-500:]


def test_empty_setting_is_treated_as_unset():
    env = {**_clean_env(), "LITELLM_LOCAL_MODEL_COST_MAP": "", "LITELLM_LOG": ""}
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(imports="import app")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.stdout.split() == ["0", "True", "ERROR", "PRODUCTION"], result.stderr[-500:]


def test_without_the_app_defaults_litellm_would_reach_the_network():
    """Control: proves the probe above can detect the import-time fetch."""
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(imports="")],
        # PRODUCTION keeps LiteLLM from loading a developer's .env, which could set the cost-map flag.
        env={**_clean_env(), "LITELLM_MODE": "PRODUCTION"},
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert int(result.stdout.split()[0]) > 0, result.stderr[-500:]


def test_litellm_does_not_log_prompts_or_keys_even_with_debug_root_logging(monkeypatch, capfd, expect_blocked_network):
    """Runs the real LiteLLM call path (network is blocked) with the root logger at DEBUG."""
    from app.llm.litellm_provider import LiteLLMProvider

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    root = logging.getLogger()
    handler, old_level = Capture(level=logging.DEBUG), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        connector = ModelConnector(
            LiteLLMProvider(api_key="sk-ant-test-SECRETKEY123456"),
            "anthropic/claude-sonnet-5-5",
            max_attempts=1,
            timeout_s=10,
        )
        with pytest.raises(ProviderError):
            asyncio.run(connector.complete("SYSTEM-PROMPT-MARKER", "SOURCE-DIFF-MARKER", metadata={}))
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    output = "\n".join(records) + "".join(capfd.readouterr())
    assert expect_blocked_network()  # the real request path was exercised and stopped at the socket
    for secret in ("SYSTEM-PROMPT-MARKER", "SOURCE-DIFF-MARKER", "SECRETKEY123456"):
        assert secret not in output


DOTENV_PROBE = """
import os
{imports}
import litellm
print(os.environ.get("PRQ_DOTENV_SENTINEL", "absent"))
"""


@pytest.mark.parametrize("imports, expected", [("import app", "absent"), ("", "loaded-by-litellm")])
def test_litellm_does_not_load_dotenv_on_its_own_once_the_app_is_imported(imports, expected, tmp_path):
    """The app is the only .env loader. Control row: bare LiteLLM would load a .env from the working directory."""
    (tmp_path / ".env").write_text("PRQ_DOTENV_SENTINEL=loaded-by-litellm\nLITELLM_LOCAL_MODEL_COST_MAP=True\n")
    env = {**_clean_env(), "PRQ_ENV_FILE": "", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    result = subprocess.run(
        [sys.executable, "-c", DOTENV_PROBE.format(imports=imports)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert result.stdout.split()[-1:] == [expected], result.stderr[-500:]
