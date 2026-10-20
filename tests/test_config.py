"""Local .env loading: values reach Settings and LiteLLM, the real environment wins, and the test suite never
reads the developer's own .env."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import DEFAULT_ENV_FILE, Settings, load_env_file
from app.observability import log_event

ROOT = Path(__file__).resolve().parent.parent
GEMINI_ENV = """
# Gemini through LiteLLM
LLM_PROVIDER=litellm
export LLM_MODEL=gemini/gemini-3.8-flash   # inline comment
GEMINI_API_KEY="test-gemini-key-0123456789"
LLM_MAX_OUTPUT_TOKENS='8000'

not a setting
=missing-name
EMPTY_VALUE=
"""


@pytest.fixture
def env(monkeypatch):
    """An isolated os.environ without any provider settings, restored after the test."""
    isolated = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "GEMINI_", "PRQ_"))}
    monkeypatch.setattr(os, "environ", isolated)
    return isolated


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(GEMINI_ENV)
    return path


def test_gemini_settings_are_loaded_from_env_file(env, env_file):
    loaded = load_env_file(env_file)
    assert loaded == ["LLM_PROVIDER", "LLM_MODEL", "GEMINI_API_KEY", "LLM_MAX_OUTPUT_TOKENS"]
    settings = Settings.from_env()
    assert (settings.llm_provider, settings.llm_model, settings.llm_max_output_tokens) == (
        "litellm",
        "gemini/gemini-3.8-flash",
        8000,
    )
    assert env["GEMINI_API_KEY"] == "test-gemini-key-0123456789"  # quotes stripped; visible to LiteLLM
    assert "EMPTY_VALUE" not in env


def test_real_environment_variables_take_precedence(env, env_file):
    env["LLM_MODEL"] = "gemini/gemini-3.5-flash-lite"
    env["LLM_PROVIDER"] = ""  # empty counts as unset, as everywhere else in the settings
    load_env_file(env_file)
    settings = Settings.from_env()
    assert settings.llm_model == "gemini/gemini-3.5-flash-lite"
    assert settings.llm_provider == "litellm"


def test_missing_file_keeps_the_mock_default(env, tmp_path):
    assert load_env_file(tmp_path / "absent.env") == []
    assert Settings.from_env().llm_provider == "mock"


def test_prq_env_file_selects_or_disables_the_file(env, env_file):
    env["PRQ_ENV_FILE"] = ""
    assert load_env_file() == []
    env["PRQ_ENV_FILE"] = str(env_file)
    assert "GEMINI_API_KEY" in load_env_file()


def test_test_suite_never_reads_the_developer_env_file():
    assert os.environ.get("PRQ_ENV_FILE") == "" and load_env_file() == []
    assert DEFAULT_ENV_FILE == ROOT / ".env"


def test_loaded_key_is_redacted_from_logs(env, env_file, log_stream):
    load_env_file(env_file)
    log_event("model_call", detail="calling gemini with test-gemini-key-0123456789")
    line = json.loads(log_stream.getvalue().splitlines()[-1])
    assert "test-gemini-key-0123456789" not in log_stream.getvalue() and "[REDACTED]" in line["detail"]


def test_env_file_is_loaded_when_the_application_starts(env_file):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "GEMINI_"))}
    env.update(PRQ_ENV_FILE=str(env_file), PYTHONPATH=str(ROOT))
    code = (
        "import app, os; from app.config import Settings; s = Settings.from_env(); print(s.llm_provider, s.llm_model)"
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert result.stdout.split() == ["litellm", "gemini/gemini-3.8-flash"], result.stderr[-500:]


def test_env_file_is_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored and "!.env.example" in ignored
