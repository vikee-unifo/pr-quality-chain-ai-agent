import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import Store
from tests.conftest import benchmark_script, mock_connector
from tests.test_github import REPO, FakeGitHub

SECRET = "whsec-test-123456"


def make_client(script=None, fake=None, **settings):
    fake = fake or FakeGitHub()
    store = Store()
    app = create_app(Settings(github_webhook_secret=SECRET, **settings), mock_connector(script), store, fake.client())
    return TestClient(app), store, fake


def test_health_and_status():
    client, *_ = make_client()
    assert client.get("/health").json() == {"status": "ok"}
    status = client.get("/status").json()
    assert status["provider"] == "mock" and status["simulated"] is True and status["publish_enabled"] is False


def test_review_from_supplied_diff(bypass_case):
    client, store, _ = make_client(benchmark_script())
    response = client.post("/reviews", json=bypass_case[0].model_dump(mode="json"))
    body = response.json()
    assert response.status_code == 200 and body["status"] == "complete"
    assert body["recommendation"] == "request_changes" and "Simulated run" in body["report_preview"]
    assert client.get(f"/reviews/{body['run_id']}").json()["run_id"] == body["run_id"]
    report = client.get(f"/reviews/{body['run_id']}/report", params={"publishable": True}).text
    assert "src/transfers.ts" not in report
    assert client.get("/reviews/nope").status_code == 404


def test_invalid_input_is_rejected():
    client, *_ = make_client()
    assert client.post("/reviews", json={"diff": "not a diff"}).status_code == 400
    assert client.post("/reviews", json={}).status_code == 422
    assert client.post("/reviews/github", json={"repo": "bad repo", "pr_number": 1}).status_code == 422


def _github_review(client):
    return client.post("/reviews/github", json={"repo": REPO, "pr_number": 7}).json()


def test_publishing_is_disabled_by_default():
    client, _, fake = make_client(benchmark_script())
    run = _github_review(client)
    assert client.post(f"/reviews/{run['run_id']}/publish", json={"confirm": True}).status_code == 403
    assert fake.comments == []


def test_publishing_requires_confirmation_and_is_idempotent():
    client, _, fake = make_client(benchmark_script(), github_publish_enabled=True)
    run = _github_review(client)
    url = f"/reviews/{run['run_id']}/publish"
    assert client.post(url, json={}).status_code == 400
    first = client.post(url, json={"confirm": True}).json()
    again = client.post(url, json={"confirm": True}).json()
    assert first["action"] == "created" and again["action"] == "unchanged" and len(fake.comments) == 1
    # A newer review of the same head commit updates the same comment instead of adding another.
    second_run = _github_review(client)
    assert client.post(f"/reviews/{second_run['run_id']}/publish", json={"confirm": True}).json()["action"] == "updated"
    assert len(fake.comments) == 1


def test_failed_or_unlinked_reviews_cannot_be_published(bypass_case):
    client, *_ = make_client({"default": [{"error": "auth"}]}, github_publish_enabled=True)
    failed = _github_review(client)
    assert failed["status"] == "failed"
    assert client.post(f"/reviews/{failed['run_id']}/publish", json={"confirm": True}).status_code == 409
    local = client.post("/reviews", json=bypass_case[0].model_dump(mode="json")).json()
    assert client.post(f"/reviews/{local['run_id']}/publish", json={"confirm": True}).status_code == 409


def _signed(payload, event="pull_request", delivery="d-1", secret=SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "content": body,
        "headers": {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    }


PR_EVENT = {
    "action": "opened",
    "repository": {"full_name": REPO},
    "pull_request": {"number": 7, "head": {"sha": "abc123"}},
}


def test_webhook_reviews_once_and_never_publishes():
    client, store, fake = make_client(benchmark_script(), github_publish_enabled=True)
    response = client.post("/webhooks/github", **_signed(PR_EVENT))
    assert response.status_code == 202 and response.json()["status"] == "accepted"
    assert store.stats()["runs_by_status"] == {"complete": 1}
    assert client.post("/webhooks/github", **_signed(PR_EVENT)).json()["status"] == "duplicate"  # redelivery
    assert (
        client.post("/webhooks/github", **_signed(PR_EVENT, delivery="d-2")).json()["reason"]
        == "this head commit was already reviewed"
    )
    assert fake.comments == []


@pytest.mark.parametrize(
    "kwargs, code",
    [
        (dict(secret="wrong-secret"), 401),
    ],
)
def test_webhook_rejects_bad_signature(kwargs, code):
    client, *_ = make_client()
    assert client.post("/webhooks/github", **_signed(PR_EVENT, **kwargs)).status_code == code


def test_webhook_ignores_other_events_and_requires_secret():
    client, *_ = make_client()
    assert client.post("/webhooks/github", **_signed({}, event="push")).json()["status"] == "ignored"
    assert client.post("/webhooks/github", **_signed({**PR_EVENT, "action": "closed"})).json()["status"] == "ignored"
    unconfigured = TestClient(create_app(Settings(), mock_connector(), Store(), FakeGitHub().client()))
    assert unconfigured.post("/webhooks/github", **_signed(PR_EVENT)).status_code == 503
