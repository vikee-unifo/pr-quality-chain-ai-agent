"""Deterministic offline provider. Makes no network calls and returns only scripted content.

Script format: {"<agent name>" | "default": [step, ...]}. Steps are consumed in order per agent;
the last step repeats. A step is either a string (the response content) or an object:
  {"content": "..."}                    respond with this text
  {"findings": [...]}                   respond with this JSON
  {"error": "<scenario>", ...}          raise a simulated provider failure (see SCENARIOS)
  {"hang": true}                        never respond (exercises timeouts)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.llm.base import ErrorCategory, ModelRequest, ModelResponse, ProviderError

# scenario -> (category, HTTP status, provider error code, message)
SCENARIOS: dict[str, tuple[ErrorCategory, int | None, str | None, str]] = {
    "auth": (ErrorCategory.AUTH, 401, "invalid_api_key", "Simulated: invalid API key"),
    "permission": (ErrorCategory.AUTH, 403, "permission_denied", "Simulated: key lacks permission for model"),
    "rate_limit": (ErrorCategory.RATE_LIMIT, 429, "rate_limit_exceeded", "Simulated: requests per minute exceeded"),
    "quota": (ErrorCategory.QUOTA_EXHAUSTED, 429, "insufficient_quota", "Simulated: credit balance exhausted"),
    "ambiguous_429": (ErrorCategory.RATE_LIMIT_OR_QUOTA, 429, None, "Simulated: 429 without a structured reason"),
    "invalid_request": (ErrorCategory.INVALID_REQUEST, 400, "invalid_request_error", "Simulated: bad request"),
    "unsupported_model": (ErrorCategory.UNSUPPORTED_MODEL, 404, "model_not_found", "Simulated: model not found"),
    "timeout": (ErrorCategory.TIMEOUT, 408, None, "Simulated: provider request timed out"),
    "connection": (ErrorCategory.NETWORK, None, None, "Simulated: connection reset"),
    "server": (ErrorCategory.SERVER, 503, "overloaded", "Simulated: provider temporarily unavailable"),
}


class MockProvider:
    name = "mock"
    simulated = True

    def __init__(self, script: dict[str, list[Any]] | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items() if not k.startswith("_")}
        self.calls: list[str] = []  # agent names, in call order

    @classmethod
    def from_file(cls, path: str) -> MockProvider:
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def _next_step(self, agent: str) -> Any:
        steps = self.script.get(agent) or self.script.get("default")
        if not steps:
            return {"findings": []}
        return steps.pop(0) if len(steps) > 1 else steps[0]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        agent = request.metadata.get("agent", "default")
        self.calls.append(agent)
        step = self._next_step(agent)
        if isinstance(step, dict) and step.get("hang"):
            await asyncio.sleep(3600)
        if isinstance(step, dict) and "error" in step:
            category, status, code, message = SCENARIOS[step["error"]]
            raise ProviderError(
                category, message, provider=self.name, status=status, code=code, retry_after_s=step.get("retry_after")
            )
        if isinstance(step, str):
            content = step
        elif "content" in step:
            content = step["content"]
        else:
            content = json.dumps(step)
        return ModelResponse(
            provider=self.name,
            model=request.model,
            content=content,
            usage=None,  # nothing was measured; never report zero cost for a simulated call
            request_id=f"mock-{len(self.calls)}",
            simulated=True,
        )
