"""The single entry point agents use for model calls: timeouts, bounded retries, and logging.

Retry ownership: this class is the only retry layer. Adapters must disable SDK retries.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

from app.config import Settings
from app.llm.base import ErrorCategory, ModelRequest, ModelResponse, Provider, ProviderError
from app.observability import log_event, safe_message


class ModelConnector:
    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        timeout_s: float = 60.0,
        max_output_tokens: int = 4000,
        max_attempts: int = 3,
        total_budget_s: float = 180.0,
        base_delay_s: float = 2.0,
        max_delay_s: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.provider = provider
        self.model = model
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self.max_attempts = max_attempts
        self.total_budget_s = total_budget_s
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self._sleep = sleep
        self._rng = rng or random.Random()

    @property
    def simulated(self) -> bool:
        return self.provider.simulated

    def backoff(self, attempt: int, retry_after_s: float | None) -> float:
        if retry_after_s is not None:
            return retry_after_s
        # Exponential backoff with full jitter.
        return self._rng.uniform(0, min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1)))

    async def complete(
        self, system: str, user: str, *, metadata: dict[str, str], json_schema: dict | None = None
    ) -> ModelResponse:
        request = ModelRequest(
            model=self.model,
            system=system,
            messages=[{"role": "user", "content": user}],
            json_schema=json_schema,
            timeout_s=self.timeout_s,
            max_output_tokens=self.max_output_tokens,
            metadata=metadata,
        )
        deadline = time.monotonic() + self.total_budget_s
        base_log = {"provider": self.provider.name, "model": self.model, "simulated": self.simulated, **metadata}

        for attempt in range(1, self.max_attempts + 1):
            remaining = deadline - time.monotonic()
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self.provider.complete(request), timeout=min(self.timeout_s, remaining)
                )
                if not response.content.strip():
                    raise ProviderError(
                        ErrorCategory.EMPTY_RESPONSE,
                        "Model returned empty content",
                        provider=self.provider.name,
                        retryable=False,
                    )
            except asyncio.TimeoutError:
                error = ProviderError(
                    ErrorCategory.TIMEOUT,
                    f"No response within {min(self.timeout_s, remaining):.1f}s",
                    provider=self.provider.name,
                )
            except ProviderError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - unexpected adapter bug; normalized, never leaked raw
                error = ProviderError(
                    ErrorCategory.UNKNOWN,
                    safe_message(f"{type(exc).__name__}: {exc}"),
                    provider=self.provider.name,
                    retryable=False,
                )
            else:
                usage = response.usage
                log_event(
                    "model_call",
                    **base_log,
                    attempt=attempt,
                    outcome="success",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    request_id=response.request_id,
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    cost_usd=usage.cost_usd if usage else None,
                    usage_available=bool(usage and usage.input_tokens is not None),
                )
                response.attempts = attempt
                return response

            error.attempts = attempt
            delay = self.backoff(attempt, error.retry_after_s)
            if not error.retryable:
                decision = "give_up_permanent"
            elif attempt >= self.max_attempts:
                decision = "give_up_max_attempts"
            elif time.monotonic() + delay >= deadline:
                decision = "give_up_time_budget"
            else:
                decision = "retry"
            log_event(
                "model_call",
                logging.WARNING,
                **base_log,
                attempt=attempt,
                outcome="error",
                duration_ms=int((time.monotonic() - started) * 1000),
                error_category=error.category.value,
                http_status=error.status,
                provider_code=error.code,
                error_message=error.message,
                retryable=error.retryable,
                retry_decision=decision,
                retry_delay_s=round(delay, 2) if decision == "retry" else None,
            )
            if decision != "retry":
                raise error
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover


def build_connector(settings: Settings) -> ModelConnector:
    if settings.llm_provider == "mock":
        from app.llm.mock import MockProvider

        provider: Provider = (
            MockProvider.from_file(settings.mock_script_path) if settings.mock_script_path else MockProvider()
        )
    elif settings.llm_provider == "litellm":
        from app.llm.litellm_provider import LiteLLMProvider

        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
        provider = LiteLLMProvider(api_key=key, api_base=settings.llm_base_url)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r}; use 'mock' or 'litellm'")
    return ModelConnector(
        provider,
        settings.llm_model,
        timeout_s=settings.llm_timeout_s,
        max_output_tokens=settings.llm_max_output_tokens,
        max_attempts=settings.llm_max_attempts,
        total_budget_s=settings.llm_total_budget_s,
    )
