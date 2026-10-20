import asyncio
import json

import httpx
import pytest

from app.llm import ErrorCategory, ModelConnector, ProviderError
from app.llm.litellm_provider import LiteLLMProvider, classify_exception
from app.llm.mock import MockProvider


def make(script, **kw):
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    provider = MockProvider(script)
    kw.setdefault("max_attempts", 3)
    connector = ModelConnector(provider, "mock-model", sleep=fake_sleep, **kw)
    return connector, provider, sleeps


def call(connector):
    return asyncio.run(connector.complete("sys", "user", metadata={"agent": "security", "run_id": "r1"}))


def test_success_is_marked_simulated():
    connector, provider, _ = make({"security": [{"findings": []}]})
    response = call(connector)
    assert json.loads(response.content) == {"findings": []}
    assert response.simulated and provider.calls == ["security"]


def test_transient_failure_is_retried_then_succeeds():
    connector, provider, sleeps = make({"security": [{"error": "server"}, {"error": "connection"}, "ok"]})
    assert call(connector).content == "ok"
    assert len(provider.calls) == 3 and len(sleeps) == 2


@pytest.mark.parametrize("scenario", ["auth", "permission", "quota", "invalid_request", "unsupported_model"])
def test_permanent_failures_are_not_retried(scenario):
    connector, provider, sleeps = make({"security": [{"error": scenario}]})
    with pytest.raises(ProviderError) as exc:
        call(connector)
    assert not exc.value.retryable and exc.value.attempts == 1
    assert len(provider.calls) == 1 and sleeps == []


def test_retries_stop_at_max_attempts():
    connector, provider, _ = make({"security": [{"error": "rate_limit"}]}, max_attempts=4)
    with pytest.raises(ProviderError) as exc:
        call(connector)
    assert exc.value.category is ErrorCategory.RATE_LIMIT
    assert exc.value.attempts == 4 and len(provider.calls) == 4


def test_retry_after_is_respected():
    connector, _, sleeps = make({"security": [{"error": "rate_limit", "retry_after": 7}, "ok"]})
    assert call(connector).content == "ok"
    assert sleeps == [7]


def test_retry_after_beyond_time_budget_gives_up():
    connector, provider, sleeps = make({"security": [{"error": "rate_limit", "retry_after": 500}]}, total_budget_s=60)
    with pytest.raises(ProviderError):
        call(connector)
    assert len(provider.calls) == 1 and sleeps == []


def test_backoff_is_exponential_with_bounded_jitter():
    connector, *_ = make({}, base_delay_s=1, max_delay_s=5)
    for attempt, cap in [(1, 1), (2, 2), (3, 4), (4, 5), (10, 5)]:
        assert all(0 <= connector.backoff(attempt, None) <= cap for _ in range(50))


def test_hanging_provider_times_out_and_is_retried():
    connector, provider, _ = make({"security": [{"hang": True}]}, timeout_s=0.02, max_attempts=2)
    with pytest.raises(ProviderError) as exc:
        call(connector)
    assert exc.value.category is ErrorCategory.TIMEOUT and len(provider.calls) == 2


def test_empty_response_is_an_error_and_not_retried():
    connector, provider, _ = make({"security": ["   "]})
    with pytest.raises(ProviderError) as exc:
        call(connector)
    assert exc.value.category is ErrorCategory.EMPTY_RESPONSE and len(provider.calls) == 1


def test_ambiguous_429_is_reported_as_ambiguous_and_retried():
    connector, provider, _ = make({"security": [{"error": "ambiguous_429"}, "ok"]})
    assert call(connector).content == "ok" and len(provider.calls) == 2


def test_logs_have_retry_metadata_and_no_prompt(log_stream):
    connector, *_ = make({"security": [{"error": "server"}, "ok"]})
    asyncio.run(connector.complete("SYSTEM-PROMPT", "DIFF-CONTENT", metadata={"agent": "security", "run_id": "r1"}))
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    assert [line["retry_decision"] for line in lines if line["outcome"] == "error"] == ["retry"]
    success = lines[-1]
    assert success["outcome"] == "success" and success["run_id"] == "r1" and success["attempt"] == 2
    assert success["usage_available"] is False
    assert "SYSTEM-PROMPT" not in log_stream.getvalue() and "DIFF-CONTENT" not in log_stream.getvalue()


# --- LiteLLM adapter: verified against LiteLLM's real exception classes, no network ---

litellm = pytest.importorskip("litellm")


def _response(status, headers=None):
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "https://provider.test"))


@pytest.mark.parametrize(
    "exc, category",
    [
        (lambda: litellm.AuthenticationError("bad key", "anthropic", "m"), ErrorCategory.AUTH),
        (
            lambda: litellm.RateLimitError("Rate limit reached: requests per minute", "openai", "m"),
            ErrorCategory.RATE_LIMIT,
        ),
        (
            lambda: litellm.RateLimitError("insufficient_quota: check your plan and billing", "openai", "m"),
            ErrorCategory.QUOTA_EXHAUSTED,
        ),
        (
            lambda: litellm.RateLimitError("Quota exceeded for requests per minute; check billing", "gemini", "m"),
            ErrorCategory.RATE_LIMIT_OR_QUOTA,
        ),
        (lambda: litellm.RateLimitError("Too Many Requests?", "x", "m"), ErrorCategory.RATE_LIMIT),
        (lambda: litellm.RateLimitError("429", "x", "m"), ErrorCategory.RATE_LIMIT_OR_QUOTA),
        (
            lambda: litellm.BadRequestError("Your credit balance is too low", "m", "anthropic"),
            ErrorCategory.QUOTA_EXHAUSTED,
        ),
        (lambda: litellm.BadRequestError("max_tokens invalid", "m", "anthropic"), ErrorCategory.INVALID_REQUEST),
        (lambda: litellm.NotFoundError("model x not found", "m", "openai"), ErrorCategory.UNSUPPORTED_MODEL),
        (lambda: litellm.Timeout("timed out", "m", "openai"), ErrorCategory.TIMEOUT),
        (lambda: litellm.APIConnectionError("conn reset", "openai", "m"), ErrorCategory.NETWORK),
        (lambda: litellm.ServiceUnavailableError("overloaded", "anthropic", "m"), ErrorCategory.SERVER),
        (lambda: litellm.InternalServerError("boom", "anthropic", "m"), ErrorCategory.SERVER),
        (lambda: ValueError("weird"), ErrorCategory.UNKNOWN),
    ],
)
def test_litellm_exceptions_are_classified(exc, category):
    error = classify_exception(exc())
    assert error.category is category


def test_litellm_retry_after_header_and_status_are_preserved():
    exc = litellm.RateLimitError("rate limit", "openai", "m", response=_response(429, {"retry-after": "12"}))
    error = classify_exception(exc)
    assert error.retry_after_s == 12 and error.status == 429 and error.provider == "openai"


def test_litellm_error_message_is_redacted():
    error = classify_exception(litellm.AuthenticationError("Incorrect API key sk-abcdef1234567890XYZ", "openai", "m"))
    assert "sk-abcdef" not in error.message


def test_litellm_adapter_disables_sdk_retries_and_normalizes(monkeypatch):
    seen = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        return litellm.ModelResponse(
            id="req-1",
            model="claude-x",
            choices=[{"message": {"role": "assistant", "content": "{}"}}],
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    connector = ModelConnector(LiteLLMProvider(api_key="sk-test-0000000000"), "anthropic/claude-x")
    response = call(connector)
    assert seen["num_retries"] == 0 and seen["max_retries"] == 0 and seen["api_key"] == "sk-test-0000000000"
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert response.provider == "anthropic" and response.request_id == "req-1" and not response.simulated
    assert response.usage.input_tokens == 10 and response.usage.output_tokens == 2


def test_litellm_adapter_failure_goes_through_retry_policy(monkeypatch):
    calls = []

    async def failing(**kwargs):
        calls.append(1)
        raise litellm.AuthenticationError("invalid x-api-key", "anthropic", "m")

    monkeypatch.setattr(litellm, "acompletion", failing)
    # A (fake) key is configured, so the pre-flight passes and the provider's own 401 is exercised.
    connector = ModelConnector(LiteLLMProvider(api_key="sk-test-0000000000"), "anthropic/claude-x", max_attempts=3)
    with pytest.raises(ProviderError) as exc:
        call(connector)
    assert exc.value.category is ErrorCategory.AUTH and calls == [1]
