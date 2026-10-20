"""Structured JSON logging with secret redaction applied to every emitted line."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

logger = logging.getLogger("prq")

# Each pattern may capture a non-secret prefix in group "keep"; everything else matched is replaced.
_PATTERNS = [
    # Header / key-value credentials, including an optional auth scheme: "Authorization: Basic abc..."
    re.compile(
        r"(?i)(?P<keep>(?:x-goog-api-key|x-api-key|api[_-]?key|proxy-authorization|authorization|client[_-]?secret|"
        r"access[_-]?token|refresh[_-]?token|secret|password)[\"']?\s*[:=]\s*[\"']?)"
        r"(?:(?:bearer|basic|token)\s+)?[^\s\"',}]+"
    ),
    re.compile(r"(?i)(?P<keep>\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/\-]{8,}=*"),
    re.compile(r"(?i)(?P<keep>\btoken\s+)(?=[A-Za-z0-9._\-]*\d)[A-Za-z0-9._\-]{16,}"),
    # Provider messages that echo a (partially masked) key: "Incorrect API key provided: sk-proj-****abcd"
    re.compile(r"(?i)(?P<keep>api key provided:\s*)\S+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]*\*+[A-Za-z0-9_\-*]*"),  # masked keys
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),  # OpenAI / Anthropic style keys
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),  # Google API keys
    re.compile(r"\bAQ\.[0-9A-Za-z_\-]{20,}"),  # Google AI Studio keys in the newer "AQ." format
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key IDs
]
_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
_extra_secrets: set[str] = set()


def register_secrets(values: list[str]) -> None:
    _extra_secrets.update(v for v in values if v and len(v) >= 6)


def _env_secrets() -> list[str]:
    return [v for k, v in os.environ.items() if any(h in k.upper() for h in _SECRET_ENV_HINTS) and len(v) >= 6]


def redact(text: str) -> str:
    for secret in (*_extra_secrets, *_env_secrets()):
        text = text.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.groupdict().get("keep") or "") + "[REDACTED]", text)
    return text


def safe_message(text: object, limit: int = 300) -> str:
    """Redacted, single-line, truncated diagnostic text that is safe to log or store."""
    message = redact(" ".join(str(text).split()))
    return message if len(message) <= limit else message[:limit] + "..."


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return redact(json.dumps(payload, default=str))


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is at emit time (keeps working when stderr is redirected or captured)."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value):
        pass


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    register_secrets(secrets or [])
    if not any(isinstance(h, _StderrHandler) for h in logger.handlers):
        handler = _StderrHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False


def log_event(event: str, level: int = logging.INFO, **fields: object) -> None:
    logger.log(level, event, extra={"fields": {k: v for k, v in fields.items() if v is not None}})
