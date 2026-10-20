import asyncio

from app.config import Settings
from app.diff import parse_diff
from app.llm.mock import MockProvider
from app.models import Recommendation, ReviewStatus
from app.orchestrator import classify, run_review
from tests.conftest import benchmark_script, mock_connector, review


def diff_for(*paths, line="+x = 1"):
    return "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-a\n{line}\n" for p in paths)


def test_risk_classification_selects_agents():
    assert classify(parse_diff(diff_for("README.md"))).selected_agents == []
    assert classify(parse_diff(diff_for("tests/test_a.py"))).selected_agents == ["testing"]
    plain = classify(parse_diff(diff_for("src/util.py")))
    assert plain.level == "medium" and plain.selected_agents == ["code_quality", "testing"]
    risky = classify(parse_diff(diff_for("src/auth/login.py")))
    assert risky.level == "high" and risky.selected_agents == ["code_quality", "security", "testing"]
    assert "security" in classify(parse_diff(diff_for("config/app.yaml"))).selected_agents
    by_content = classify(parse_diff(diff_for("src/util.py", line="+db.query(f'SELECT {x}')")))
    assert by_content.level == "high"


def test_benchmark_risk_is_high_and_runs_all_agents(bypass_case):
    risk = classify(parse_diff(bypass_case[0].diff))
    assert risk.level == "high" and set(risk.selected_agents) == {"code_quality", "security", "testing"}


def test_complete_review_of_benchmark_with_scripted_mock(bypass_case):
    result = review(bypass_case[0], benchmark_script())
    assert result.status is ReviewStatus.COMPLETE and result.simulated
    assert result.recommendation is Recommendation.REQUEST_CHANGES
    assert result.raw_finding_count == 5
    top = result.findings[0]
    assert top.category.value == "security" and top.priority.value == "P0" and top.validation.value == "valid"
    assert set(top.agents) == {"security", "code_quality"}  # duplicate merged, not dropped
    assert any("removed lines contain security-relevant terms" in fact for fact in result.observed_facts)
    assert any("did not run any tests" in fact for fact in result.observed_facts)


def test_partial_review_when_one_agent_fails(bypass_case):
    script = {**benchmark_script(), "code_quality": [{"error": "auth"}]}
    result = review(bypass_case[0], script)
    assert result.status is ReviewStatus.PARTIAL
    assert result.recommendation is Recommendation.ESCALATE
    failed = [o for o in result.agent_outcomes if o.status == "failed"]
    assert [o.agent for o in failed] == ["code_quality"] and failed[0].error["category"] == "auth"
    assert result.findings  # findings from the agents that did succeed are kept


def test_failed_review_when_every_agent_fails(bypass_case):
    result = review(bypass_case[0], {"default": [{"error": "server"}]}, max_attempts=2)
    assert result.status is ReviewStatus.FAILED and result.findings == []
    assert result.recommendation is Recommendation.ESCALATE
    assert all(o.attempts == 2 for o in result.agent_outcomes)


def test_agent_time_budget_is_enforced(bypass_case):
    script = {**benchmark_script(), "testing": [{"hang": True}]}
    result = review(bypass_case[0], script, settings=Settings(agent_timeout_s=0.1))
    testing = next(o for o in result.agent_outcomes if o.agent == "testing")
    assert result.status is ReviewStatus.PARTIAL and testing.error["category"] == "timeout"


def test_cancelled_review_is_recorded(bypass_case):
    saved = []

    class Store:
        def save(self, result):
            saved.append(result)

    async def scenario():
        task = asyncio.create_task(
            run_review(bypass_case[0], mock_connector({"default": [{"hang": True}]}), Settings(), Store())
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(scenario()) == "cancelled"
    assert saved[0].status is ReviewStatus.CANCELLED
    assert {o.status for o in saved[0].agent_outcomes} == {"cancelled"}


def test_parallelism_is_bounded(bypass_case):
    active, peak = 0, 0

    class Tracking(MockProvider):
        async def complete(self, request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return await super().complete(request)

    from app.llm import ModelConnector

    connector = ModelConnector(Tracking({"default": [{"findings": []}]}), "m")
    asyncio.run(run_review(bypass_case[0], connector, Settings(max_parallel_agents=1)))
    assert peak == 1
    peak = 0
    asyncio.run(run_review(bypass_case[0], connector, Settings(max_parallel_agents=3)))
    assert peak == 3


def test_docs_only_change_makes_no_model_calls():
    connector = mock_connector()
    result = asyncio.run(
        run_review(
            __import__("app.models", fromlist=["PRInput"]).PRInput(diff=diff_for("README.md")), connector, Settings()
        )
    )
    assert result.status is ReviewStatus.COMPLETE and connector.provider.calls == []
    assert result.recommendation is Recommendation.PROCEED_WITH_HUMAN_REVIEW
