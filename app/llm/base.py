"""Provider-neutral model contract. Agents depend only on these types, never on provider SDKs."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ErrorCategory(str, Enum):
    AUTH = "auth"  # invalid key or missing permission
    RATE_LIMIT = "rate_limit"  # request-rate / throughput limit, transient
    QUOTA_EXHAUSTED = "quota_exhausted"  # credits, billing or hard quota; needs a human
    RATE_LIMIT_OR_QUOTA = "rate_limit_or_quota"  # 429 whose cause the provider did not make clear
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_MODEL = "unsupported_model"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER = "server"  # temporary provider-side failure (5xx, overloaded)
    EMPTY_RESPONSE = "empty_response"
    MALFORMED_RESPONSE = "malformed_response"  # not parseable JSON
    SCHEMA_VALIDATION = "schema_validation"  # JSON but wrong shape
    UNKNOWN = "unknown"


RETRYABLE = {
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.RATE_LIMIT_OR_QUOTA,  # bounded by max attempts; a real quota problem fails again quickly
    ErrorCategory.TIMEOUT,
    ErrorCategory.NETWORK,
    ErrorCategory.SERVER,
}


class ProviderError(Exception):
    """A normalized provider failure. `message` must already be safe to log."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        provider: str,
        status: int | None = None,
        code: str | None = None,
        retry_after_s: float | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.message = message
        self.provider = provider
        self.status = status
        self.code = code
        self.retry_after_s = retry_after_s
        self.retryable = category in RETRYABLE if retryable is None else retryable
        self.attempts = 0

    def summary(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "provider": self.provider,
            "status": self.status,
            "code": self.code,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "message": self.message,
        }


class Message(BaseModel):
    role: str = "user"
    content: str


class ModelRequest(BaseModel):
    model: str
    system: str
    messages: list[Message]
    # JSON Schema the response must satisfy. Always enforced by the caller with Pydantic;
    # adapters may additionally pass it to providers that support structured output.
    json_schema: dict[str, Any] | None = None
    timeout_s: float = 60.0
    max_output_tokens: int = 4000
    # Correlation data for logs: run_id, correlation_id, agent. Never sent to the provider.
    metadata: dict[str, str] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class ModelResponse(BaseModel):
    provider: str
    model: str
    content: str
    usage: Usage | None = None
    request_id: str | None = None
    latency_ms: int = 0
    # Set by ModelConnector: how many attempts it took, including retries.
    attempts: int = 1
    # True for the mock provider: the content was scripted, not produced by a model.
    simulated: bool = False


class Provider(Protocol):
    name: str
    simulated: bool

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
