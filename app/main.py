"""FastAPI service: manual and webhook-triggered reviews, preview-first reporting, gated publishing.

Run: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app import report
from app.config import Settings
from app.github import GitHubClient, GitHubError, publish_key, verify_signature
from app.llm.connector import ModelConnector, build_connector
from app.models import PRInput, ReviewResult, ReviewStatus
from app.observability import configure_logging, log_event
from app.orchestrator import run_review
from app.store import Store

REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


class GitHubReviewRequest(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)


class PublishRequest(BaseModel):
    confirm: bool = False


def create_app(
    settings: Settings | None = None,
    connector: ModelConnector | None = None,
    store: Store | None = None,
    github: GitHubClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level, settings.secret_values())
    connector = connector or build_connector(settings)
    store = store or Store(settings.database_path)
    token = settings.github_token.get_secret_value() if settings.github_token else None
    github = github or GitHubClient(token, settings.github_api_url)
    publish_lock = asyncio.Lock()  # ponytail: one lock for all publishes; per-key locks if volume ever matters

    app = FastAPI(title="PR Quality Chain", version="0.1.0")

    def review_response(result: ReviewResult) -> dict:
        return {**result.model_dump(mode="json"), "report_preview": report.render(result)}

    async def review_github_pr(repo: str, number: int) -> ReviewResult:
        try:
            pr = await github.fetch_pr(repo, number, settings.max_files)
        except GitHubError as exc:
            raise HTTPException(502 if (exc.status or 500) >= 500 else 400, str(exc)) from None
        return await run_review(pr, connector, settings, store)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/status")
    def status() -> dict:
        return {
            "provider": connector.provider.name,
            "model": connector.model,
            "simulated": connector.simulated,
            "github_token_configured": token is not None,
            "webhook_configured": settings.github_webhook_secret is not None,
            "publish_enabled": settings.github_publish_enabled,
            **store.stats(),
            "recent_runs": store.recent(5),
        }

    @app.post("/reviews")
    async def create_review(pr: PRInput) -> dict:
        try:
            result = await run_review(pr, connector, settings, store)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return review_response(result)

    @app.post("/reviews/github")
    async def create_github_review(body: GitHubReviewRequest) -> dict:
        return review_response(await review_github_pr(body.repo, body.pr_number))

    def get_result(run_id: str) -> ReviewResult:
        result = store.get(run_id)
        if result is None:
            raise HTTPException(404, "review not found")
        return result

    @app.get("/reviews/{run_id}")
    def read_review(run_id: str) -> dict:
        return review_response(get_result(run_id))

    @app.get("/reviews/{run_id}/report", response_class=PlainTextResponse)
    def read_report(run_id: str, publishable: bool = False) -> str:
        """publishable=true shows exactly what /publish would post."""
        return report.render(get_result(run_id), publishable=publishable)

    @app.post("/reviews/{run_id}/publish")
    async def publish(run_id: str, body: PublishRequest) -> dict:
        if not settings.github_publish_enabled:
            raise HTTPException(403, "publishing is disabled; set GITHUB_PUBLISH_ENABLED=true to allow it")
        if not body.confirm:
            raise HTTPException(400, 'preview the report first, then publish with {"confirm": true}')
        result = get_result(run_id)
        if not (result.repo and result.pr_number and result.head_sha):
            raise HTTPException(409, "review is not linked to a GitHub pull request")
        if result.status not in (ReviewStatus.COMPLETE, ReviewStatus.PARTIAL):
            raise HTTPException(409, f"a {result.status.value} review cannot be published")
        key = publish_key(result.repo, result.pr_number, result.head_sha)
        async with publish_lock:
            existing = store.publication(key)
            if existing and existing["run_id"] == run_id:
                return {"action": "unchanged", "comment_id": existing["comment_id"], "publish_key": key}
            try:
                comment_id, action = await github.publish_comment(
                    result.repo,
                    result.pr_number,
                    key,
                    report.render(result, publishable=True),
                    existing["comment_id"] if existing else None,
                )
            except GitHubError as exc:
                raise HTTPException(502, str(exc)) from None
            store.record_publication(key, run_id, comment_id, action)
        log_event("report_published", run_id=run_id, publish_key=key, action=action, comment_id=comment_id)
        return {"action": action, "comment_id": comment_id, "publish_key": key}

    @app.post("/webhooks/github", status_code=202)
    async def webhook(
        request: Request,
        background: BackgroundTasks,
        x_github_event: str | None = Header(None),
        x_github_delivery: str | None = Header(None),
        x_hub_signature_256: str | None = Header(None),
    ) -> dict:
        if settings.github_webhook_secret is None:
            raise HTTPException(503, "webhook secret not configured")
        body = await request.body()
        if not verify_signature(settings.github_webhook_secret.get_secret_value(), body, x_hub_signature_256):
            raise HTTPException(401, "invalid signature")
        if x_github_event != "pull_request":
            return {"status": "ignored", "reason": f"event {x_github_event}"}
        payload = json.loads(body)
        if payload.get("action") not in REVIEW_ACTIONS:
            return {"status": "ignored", "reason": f"action {payload.get('action')}"}
        if not x_github_delivery or not store.claim_delivery(x_github_delivery):
            return {"status": "duplicate", "reason": "delivery already processed"}
        repo = payload["repository"]["full_name"]
        number = payload["pull_request"]["number"]
        sha = payload["pull_request"]["head"]["sha"]
        if store.latest_for(repo, number, sha):
            return {"status": "duplicate", "reason": "this head commit was already reviewed"}

        async def job() -> None:
            try:
                await review_github_pr(repo, number)
            except Exception as exc:  # noqa: BLE001 - background job: log, never crash the server
                log_event("webhook_review_failed", logging.ERROR, repo=repo, pr=number, error=type(exc).__name__)

        background.add_task(job)
        # Reports are never auto-published: a human previews and publishes via /reviews/{id}/publish.
        return {"status": "accepted", "repo": repo, "pr_number": number, "head_sha": sha}

    return app
