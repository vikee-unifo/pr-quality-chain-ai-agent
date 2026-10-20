"""Adapter for real providers through LiteLLM (optional dependency: pip install -e '.[llm]').

All provider-specific behaviour lives here. The application owns retries, so LiteLLM's own retries
are disabled (num_retries=0, max_retries=0) to stop the two layers multiplying attempts.
"""

from __future__ import annotations

import logging
import time
from email.utils import parsedate_to_datetime
from typing import Any

from app.llm.base import ErrorCategory, ModelRequest, ModelResponse, ProviderError, Usage
from app.observability import safe_message

_QUOTA_HINTS = ("insufficient_quota", "credit balance", "billing", "exceeded your current quota", "payment required")
_RATE_HINTS = ("rate limit", "rate_limit", "per minute", "requests per", "tokens per", "too many requests")
# Wording LiteLLM/SDKs use when no key is configured. OpenAI surfaces it as a 500 and Gemini as a
# connection error, so it must be checked before the status-based rules.
_MISSING_CREDENTIAL_HINTS = (
    "missing credentials",
    "missing anthropic api key",
    "missing gemini api key",
    "api key not set",
    "no api key",
    "api_key client option must be set",
)
# LiteLLM's own loggers bypass our redaction and log request parameters at DEBUG.
_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def _quiet_litellm_logging() -> None:
    for name in _LITELLM_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None) or {}
    try:
        value = headers.get("retry-after")
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def classify_exception(exc: Exception, provider: str = "litellm") -> ProviderError:
    """Map a LiteLLM (OpenAI-style) exception to a normalized ProviderError.

    Uses class names and attributes rather than isinstance so it works without importing litellm.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    code = str(code) if code else None
    text = str(getattr(exc, "message", None) or exc).lower()
    provider = getattr(exc, "llm_provider", None) or provider

    def err(category: ErrorCategory) -> ProviderError:
        return ProviderError(
            category,
            safe_message(getattr(exc, "message", None) or exc),
            provider=provider,
            status=status if isinstance(status, int) else None,
            code=code,
            retry_after_s=_retry_after(exc),
        )

    if any(h in text for h in _MISSING_CREDENTIAL_HINTS):
        return err(ErrorCategory.AUTH)
    quota_hint = (code or "") == "insufficient_quota" or any(h in text for h in _QUOTA_HINTS)
    rate_hint = any(h in text for h in _RATE_HINTS)
    if names & {"RateLimitError"} or status == 429:
        # Only call it exhausted credit when the provider says so and nothing suggests a per-minute limit.
        if quota_hint and not rate_hint:
            return err(ErrorCategory.QUOTA_EXHAUSTED)
        if rate_hint and not quota_hint:
            return err(ErrorCategory.RATE_LIMIT)
        return err(ErrorCategory.RATE_LIMIT_OR_QUOTA)
    if quota_hint or status == 402:  # e.g. Anthropic returns 400 "credit balance is too low"
        return err(ErrorCategory.QUOTA_EXHAUSTED)
    if names & {"AuthenticationError", "PermissionDeniedError"} or status in (401, 403):
        return err(ErrorCategory.AUTH)
    if names & {"Timeout", "APITimeoutError"} or status == 408:
        return err(ErrorCategory.TIMEOUT)
    if names & {"NotFoundError"} or status == 404 or "model not found" in text or "does not exist" in text:
        return err(ErrorCategory.UNSUPPORTED_MODEL)
    if names & {"APIConnectionError"}:
        return err(ErrorCategory.NETWORK)
    if names & {"ServiceUnavailableError", "InternalServerError"} or (isinstance(status, int) and status >= 500):
        return err(ErrorCategory.SERVER)
    if names & {"BadRequestError", "UnprocessableEntityError", "UnsupportedParamsError"} or status in (400, 422):
        return err(ErrorCategory.INVALID_REQUEST)
    return err(ErrorCategory.UNKNOWN)


class LiteLLMProvider:
    name = "litellm"
    simulated = False

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base

    def check_credentials(self, model: str) -> None:
        """Offline pre-flight: raise AUTH if LiteLLM reports required variables are missing. No network."""
        import litellm

        env = litellm.validate_environment(model=model, api_key=self.api_key, api_base=self.api_base)
        if not env.get("keys_in_environment") and env.get("missing_keys"):
            missing = ", ".join(env["missing_keys"])
            raise ProviderError(
                ErrorCategory.AUTH,
                f"Missing credentials for model {model}: set {missing} (or LLM_API_KEY)",
                provider=model.split("/", 1)[0],
                code="missing_credentials",
                retryable=False,
            )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError(
                ErrorCategory.INVALID_REQUEST,
                "litellm is not installed; run pip install -e '.[llm]'",
                provider=self.name,
                retryable=False,
            ) from exc
        _quiet_litellm_logging()
        self.check_credentials(request.model)

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "system", "content": request.system}] + [m.model_dump() for m in request.messages],
            "timeout": request.timeout_s,
            "max_tokens": request.max_output_tokens,
            "num_retries": 0,
            "max_retries": 0,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base

        started = time.monotonic()
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:  # noqa: BLE001 - every provider failure is normalized
            raise classify_exception(exc, provider=request.model.split("/", 1)[0]) from None

        usage = getattr(response, "usage", None)
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:  # noqa: BLE001 - cost is unknown for many models
            cost = None
        return ModelResponse(
            provider=request.model.split("/", 1)[0],
            model=getattr(response, "model", None) or request.model,
            content=response.choices[0].message.content or "",
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
                cost_usd=cost,
            ),
            request_id=getattr(response, "id", None),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
