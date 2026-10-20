import asyncio
import io
import json
import logging
import os
import socket

import pytest

# Offline guarantee: set before anything imports LiteLLM, then block every outbound connection.
os.environ["PRQ_ENV_FILE"] = ""  # never read the developer's real .env (keys, LLM_PROVIDER) in tests
os.environ["LITELLM_MODE"] = "PRODUCTION"  # nor let LiteLLM load it
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_LOG"] = "ERROR"
NETWORK_ATTEMPTS: list[str] = []


def _blocked(*args, **kwargs):
    NETWORK_ATTEMPTS.append(repr(args[:2])[:120])
    raise ConnectionRefusedError("network access is blocked in tests")


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if any code tried to reach the network, even if it swallowed the error."""
    unexpected = [a for a in NETWORK_ATTEMPTS if a not in EXPECTED_BLOCKED]
    if unexpected:
        print(f"\nNETWORK ACCESS ATTEMPTED during tests: {unexpected}")
        session.exitstatus = 1


EXPECTED_BLOCKED: list[str] = []  # attempts a test deliberately provoked and asserted on


@pytest.fixture
def expect_blocked_network():
    """For tests that deliberately drive real LiteLLM code into the (blocked) network."""
    before = len(NETWORK_ATTEMPTS)
    yield lambda: NETWORK_ATTEMPTS[before:]  # attempts made by this test only
    EXPECTED_BLOCKED.extend(NETWORK_ATTEMPTS[before:])


from app.benchmark import CASES_DIR, load_case  # noqa: E402 - env and socket guard must be set first
from app.config import Settings  # noqa: E402 - env and socket guard must be set first
from app.llm import ModelConnector  # noqa: E402 - env and socket guard must be set first
from app.llm.mock import MockProvider  # noqa: E402 - env and socket guard must be set first
from app.observability import JsonFormatter, logger  # noqa: E402 - env and socket guard must be set first
from app.orchestrator import run_review  # noqa: E402 - env and socket guard must be set first


@pytest.fixture
def log_stream():
    """Captures the structured log output exactly as it would be emitted."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield stream
    logger.removeHandler(handler)


async def _no_sleep(_):
    return None


def mock_connector(script=None, **kw) -> ModelConnector:
    return ModelConnector(MockProvider(script), "mock-model", sleep=_no_sleep, **kw)


def benchmark_script() -> dict:
    return json.loads((CASES_DIR / "authorization-bypass" / "mock_responses.json").read_text())


def review(pr, script=None, settings=None, store=None, **kw):
    return asyncio.run(run_review(pr, mock_connector(script, **kw), settings or Settings(), store))


@pytest.fixture
def bypass_case():
    return load_case("authorization-bypass")
