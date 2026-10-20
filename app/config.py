"""Settings loaded from environment variables, optionally seeded from a local .env file. See .env.example."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, SecretStr

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_env_file(path: Path | str | None = None) -> list[str]:
    """Copy KEY=VALUE lines from .env into os.environ and return the names loaded (never the values).

    Variables already set to a non-empty value in the real environment win over the file.
    PRQ_ENV_FILE selects another file; PRQ_ENV_FILE="" disables loading (the test suite does this).
    """
    if path is None:
        configured = os.environ.get("PRQ_ENV_FILE")
        if configured == "":
            return []
        path = configured or DEFAULT_ENV_FILE
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ENV_LINE.match(line.strip())
        if not match:
            continue  # blank lines, comments, anything that is not KEY=VALUE
        name, value = match.groups()
        if value[:1] in ("'", '"') and value.endswith(value[0]) and len(value) >= 2:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()  # inline comment after an unquoted value
        if value and not os.environ.get(name):
            os.environ[name] = value
            loaded.append(name)
    return loaded


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


class Settings(BaseModel):
    # Model provider. "mock" makes no network calls; "litellm" routes to a real provider.
    llm_provider: str = "mock"
    # For litellm: "<provider>/<model>", e.g. "anthropic/claude-sonnet-5-5", "openai/gpt-5", "gemini/gemini-2.5-pro".
    llm_model: str = "mock-model"
    # Optional explicit key/endpoint. If unset, litellm reads the provider's own variable (ANTHROPIC_API_KEY, ...).
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_timeout_s: float = 60.0
    llm_max_output_tokens: int = 4000
    llm_max_attempts: int = 3
    llm_total_budget_s: float = 180.0
    # Path to a JSON script for the mock provider (see benchmark/cases/*/mock_responses.json).
    mock_script_path: str | None = None

    max_parallel_agents: int = 3
    agent_timeout_s: float = 240.0
    max_context_chars: int = 60_000
    max_file_patch_chars: int = 15_000
    max_files: int = 50

    github_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_webhook_secret: SecretStr | None = None
    # Write operations stay off unless explicitly enabled.
    github_publish_enabled: bool = False

    database_path: str = "pr_quality_chain.db"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        fields = {
            "llm_provider": _env("LLM_PROVIDER"),
            "llm_model": _env("LLM_MODEL"),
            "llm_api_key": _env("LLM_API_KEY"),
            "llm_base_url": _env("LLM_BASE_URL"),
            "llm_timeout_s": _env("LLM_TIMEOUT_S"),
            "llm_max_output_tokens": _env("LLM_MAX_OUTPUT_TOKENS"),
            "llm_max_attempts": _env("LLM_MAX_ATTEMPTS"),
            "llm_total_budget_s": _env("LLM_TOTAL_BUDGET_S"),
            "mock_script_path": _env("MOCK_SCRIPT_PATH"),
            "max_parallel_agents": _env("MAX_PARALLEL_AGENTS"),
            "agent_timeout_s": _env("AGENT_TIMEOUT_S"),
            "max_context_chars": _env("MAX_CONTEXT_CHARS"),
            "github_token": _env("GITHUB_TOKEN"),
            "github_api_url": _env("GITHUB_API_URL"),
            "github_webhook_secret": _env("GITHUB_WEBHOOK_SECRET"),
            "github_publish_enabled": _env("GITHUB_PUBLISH_ENABLED"),
            "database_path": _env("DATABASE_PATH"),
            "log_level": _env("LOG_LEVEL"),
        }
        return cls(**{k: v for k, v in fields.items() if v is not None})

    def secret_values(self) -> list[str]:
        return [s.get_secret_value() for s in (self.llm_api_key, self.github_token, self.github_webhook_secret) if s]
