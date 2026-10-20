"""SQLite review history. Stores findings (with their short evidence snippets) and execution metadata;
never diffs, prompts, raw model responses or credentials."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.models import ReviewResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_runs (
    run_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    repo TEXT, pr_number INTEGER, head_sha TEXT,
    status TEXT NOT NULL, recommendation TEXT NOT NULL, risk_level TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL, simulated INTEGER NOT NULL,
    finding_count INTEGER NOT NULL, raw_finding_count INTEGER NOT NULL,
    started_at TEXT NOT NULL, duration_ms INTEGER NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_pr ON review_runs (repo, pr_number, head_sha);
CREATE TABLE IF NOT EXISTS agent_outcomes (
    run_id TEXT NOT NULL REFERENCES review_runs(run_id) ON DELETE CASCADE,
    agent TEXT NOT NULL, status TEXT NOT NULL, simulated INTEGER NOT NULL,
    error_category TEXT, http_status INTEGER, attempts INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
    finding_count INTEGER NOT NULL, rejected_count INTEGER NOT NULL,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    PRIMARY KEY (run_id, agent)
);
CREATE TABLE IF NOT EXISTS findings (
    run_id TEXT NOT NULL REFERENCES review_runs(run_id) ON DELETE CASCADE,
    finding_id TEXT NOT NULL, agents TEXT NOT NULL, basis TEXT NOT NULL,
    severity TEXT NOT NULL, priority TEXT, category TEXT NOT NULL, validation TEXT NOT NULL,
    file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER, title TEXT NOT NULL,
    needs_human INTEGER NOT NULL, confidence REAL NOT NULL,
    PRIMARY KEY (run_id, finding_id)
);
CREATE TABLE IF NOT EXISTS publications (
    publish_key TEXT PRIMARY KEY,  -- repo#pr@head_sha
    run_id TEXT NOT NULL, comment_id INTEGER NOT NULL, action TEXT NOT NULL, published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluation_runs (
    evaluation_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL, mode TEXT NOT NULL, providers TEXT NOT NULL,
    run_ids TEXT NOT NULL,  -- comma-separated review_runs.run_id, one per case
    summary_json TEXT NOT NULL, rows_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, run_id TEXT
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        # One connection, used from the event loop thread only.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def save(self, result: ReviewResult) -> None:
        with self.db:
            self.db.execute("DELETE FROM review_runs WHERE run_id = ?", (result.run_id,))
            self.db.execute(
                "INSERT INTO review_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    result.run_id,
                    result.correlation_id,
                    result.repo,
                    result.pr_number,
                    result.head_sha,
                    result.status.value,
                    result.recommendation.value,
                    result.risk.level,
                    result.provider,
                    result.model,
                    int(result.simulated),
                    len(result.findings),
                    result.raw_finding_count,
                    result.started_at.isoformat(),
                    result.duration_ms,
                    result.model_dump_json(),
                ),
            )
            self.db.executemany(
                "INSERT INTO agent_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        result.run_id,
                        o.agent,
                        o.status,
                        int(o.simulated),
                        (o.error or {}).get("category"),
                        (o.error or {}).get("status"),
                        o.attempts,
                        o.duration_ms,
                        o.finding_count,
                        o.rejected_count,
                        o.input_tokens,
                        o.output_tokens,
                        o.cost_usd,
                    )
                    for o in result.agent_outcomes
                ],
            )
            self.db.executemany(
                "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        result.run_id,
                        f.id,
                        ",".join(f.agents),
                        f.basis.value,
                        f.severity.value,
                        f.priority.value if f.priority else None,
                        f.category.value,
                        f.validation.value,
                        f.file_path,
                        f.start_line,
                        f.end_line,
                        f.title,
                        int(f.needs_human),
                        f.confidence,
                    )
                    for f in result.findings
                ],
            )

    def get(self, run_id: str) -> ReviewResult | None:
        row = self.db.execute("SELECT result_json FROM review_runs WHERE run_id = ?", (run_id,)).fetchone()
        return ReviewResult.model_validate_json(row["result_json"]) if row else None

    def latest_for(self, repo: str, pr_number: int, head_sha: str) -> ReviewResult | None:
        row = self.db.execute(
            "SELECT result_json FROM review_runs WHERE repo=? AND pr_number=? AND head_sha=? "
            "AND status != 'cancelled' ORDER BY started_at DESC LIMIT 1",
            (repo, pr_number, head_sha),
        ).fetchone()
        return ReviewResult.model_validate_json(row["result_json"]) if row else None

    def recent(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT run_id, repo, pr_number, status, recommendation, provider, model, simulated, finding_count, "
            "started_at, duration_ms FROM review_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        runs = dict(self.db.execute("SELECT status, COUNT(*) FROM review_runs GROUP BY status").fetchall())
        errors = dict(
            self.db.execute(
                "SELECT error_category, COUNT(*) FROM agent_outcomes WHERE error_category IS NOT NULL "
                "GROUP BY error_category"
            ).fetchall()
        )
        return {"runs_by_status": runs, "agent_errors_by_category": errors}

    # ---- benchmark evaluations ----

    def save_evaluation(self, evaluation_id: str, started_at: datetime, summary: dict, rows: list[dict]) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO evaluation_runs VALUES (?,?,?,?,?,?,?)",
                (
                    evaluation_id,
                    started_at.isoformat(),
                    summary["mode"],
                    ",".join(summary["providers"]),
                    ",".join(r["run_id"] for r in rows),
                    json.dumps(summary),
                    json.dumps(rows),
                ),
            )

    def get_evaluation(self, evaluation_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM evaluation_runs WHERE evaluation_id = ?", (evaluation_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "summary": json.loads(row["summary_json"]), "rows": json.loads(row["rows_json"])}

    # ---- idempotency records ----

    def publication(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM publications WHERE publish_key = ?", (key,)).fetchone()

    def record_publication(self, key: str, run_id: str, comment_id: int, action: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO publications VALUES (?,?,?,?,?)",
                (key, run_id, comment_id, action, datetime.now(timezone.utc).isoformat()),
            )

    def claim_delivery(self, delivery_id: str) -> bool:
        """True the first time a webhook delivery is seen; False for redeliveries."""
        with closing(self.db.cursor()) as cur, self.db:
            cur.execute(
                "INSERT OR IGNORE INTO webhook_deliveries VALUES (?,?,NULL)",
                (delivery_id, datetime.now(timezone.utc).isoformat()),
            )
            return cur.rowcount == 1
