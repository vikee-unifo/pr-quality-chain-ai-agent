import sqlite3

from app.models import ReviewStatus
from app.store import Store
from tests.conftest import benchmark_script, review


def test_review_is_persisted_and_reloaded(tmp_path, bypass_case):
    path = str(tmp_path / "reviews.db")
    result = review(bypass_case[0], benchmark_script(), store=Store(path))

    reopened = Store(path)  # schema creation is idempotent
    loaded = reopened.get(result.run_id)
    assert loaded == result
    assert reopened.latest_for(result.repo, result.pr_number, result.head_sha) is None  # no pr_number in fixture
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM agent_outcomes WHERE run_id=?", (result.run_id,)).fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM findings WHERE run_id=?", (result.run_id,)).fetchone()[0] == len(
        result.findings
    )


def test_no_diff_or_prompt_is_stored(bypass_case):
    store = Store()
    result = review(bypass_case[0], benchmark_script(), store=store)
    dump = "\n".join(store.db.iterdump())
    assert "@@ -18,8 +18,5 @@" not in dump  # the diff
    assert "untrusted_pr_content" not in dump and "You are the" not in dump  # the prompt
    assert result.run_id in dump


def test_failed_runs_and_error_stats_are_recorded(bypass_case):
    store = Store()
    review(bypass_case[0], {"default": [{"error": "auth"}]}, store=store)
    assert store.stats() == {"runs_by_status": {"failed": 1}, "agent_errors_by_category": {"auth": 3}}
    assert store.recent()[0]["status"] == ReviewStatus.FAILED.value


def test_webhook_delivery_claim_is_idempotent():
    store = Store()
    assert store.claim_delivery("d1") and not store.claim_delivery("d1") and store.claim_delivery("d2")
