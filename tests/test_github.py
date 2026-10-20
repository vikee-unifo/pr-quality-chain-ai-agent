import asyncio
import json

import httpx
import pytest

from app.github import MARKER_PREFIX, GitHubClient, GitHubError, publish_key, verify_signature

REPO = "acme/payments"
PATCH = "@@ -18,8 +18,5 @@\n   if (!account) {\n-  if (account.ownerId !== userId) {\n+  return account;"


class FakeGitHub:
    """Minimal in-memory GitHub REST API served through httpx.MockTransport."""

    def __init__(self, files=None, check_runs=None, login="prq-bot"):
        self.files = (
            files
            if files is not None
            else [
                {"filename": "src/accounts.ts", "status": "modified", "patch": PATCH},
                {"filename": "logo.png", "status": "added"},
                {
                    "filename": "src/new.ts",
                    "previous_filename": "src/old.ts",
                    "status": "renamed",
                    "patch": "@@ -1 +1 @@\n-a\n+b",
                },
            ]
        )
        self.check_runs = check_runs or []
        self.login = login
        self.comments: list[dict] = []
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path))
        if path == f"/repos/{REPO}/pulls/7":
            return httpx.Response(200, json={"title": "Remove check", "body": "desc", "head": {"sha": "abc123"}})
        if path == f"/repos/{REPO}/pulls/7/files":
            return httpx.Response(200, json=self.files)
        if path.startswith(f"/repos/{REPO}/contents/"):
            return httpx.Response(200, text="line1\nline2\n")
        if path == f"/repos/{REPO}/commits/abc123/check-runs":
            return httpx.Response(200, json={"check_runs": self.check_runs})
        if path == "/user":
            return httpx.Response(200, json={"login": self.login}) if self.login else httpx.Response(403)
        if path == f"/repos/{REPO}/issues/7/comments" and method == "GET":
            return httpx.Response(200, json=self.comments)
        if path == f"/repos/{REPO}/issues/7/comments" and method == "POST":
            comment = {
                "id": 100 + len(self.comments),
                "body": json.loads(request.content)["body"],
                "user": {"login": self.login},
            }
            self.comments.append(comment)
            return httpx.Response(201, json=comment)
        if path.startswith(f"/repos/{REPO}/issues/comments/") and method == "PATCH":
            cid = int(path.rsplit("/", 1)[1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = json.loads(request.content)["body"]
                    return httpx.Response(200, json=c)
            return httpx.Response(404)
        return httpx.Response(404)

    def client(self):
        return GitHubClient("ghp_" + "x" * 36, "https://api.github.test", transport=httpx.MockTransport(self.handler))


def test_fetch_pr_builds_reviewable_input():
    fake = FakeGitHub(
        check_runs=[
            {"name": "vitest", "status": "completed", "conclusion": "failure", "output": {"title": "2 failed"}},
            {"name": "lint", "status": "in_progress", "conclusion": None},
        ]
    )
    pr = asyncio.run(fake.client().fetch_pr(REPO, 7))
    assert (pr.title, pr.head_sha, pr.pr_number) == ("Remove check", "abc123", 7)
    assert "diff --git a/src/accounts.ts b/src/accounts.ts" in pr.diff
    assert "Binary files differ or patch too large" in pr.diff  # logo.png has no patch
    assert "rename from src/old.ts" in pr.diff
    assert set(pr.file_contents) == {"src/accounts.ts", "src/new.ts"}
    assert [(t.name, t.status) for t in pr.tool_results] == [("vitest", "failed"), ("lint", "pending")]


def test_fetch_errors_are_normalized():
    client = GitHubClient(None, "https://api.github.test", transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(GitHubError) as exc:
        asyncio.run(client.fetch_pr(REPO, 7))
    assert exc.value.status == 404


def test_publish_creates_then_updates_one_comment():
    fake = FakeGitHub()
    client, key = fake.client(), publish_key(REPO, 7, "abc123")
    cid, action = asyncio.run(client.publish_comment(REPO, 7, key, "v1", None))
    assert action == "created" and fake.comments[0]["body"].startswith(f"{MARKER_PREFIX}{key} -->")
    cid2, action2 = asyncio.run(client.publish_comment(REPO, 7, key, "v2", cid))
    assert (cid2, action2) == (cid, "updated") and len(fake.comments) == 1 and fake.comments[0]["body"].endswith("v2")


def test_publish_recovers_own_comment_when_local_record_lost():
    fake = FakeGitHub()
    client, key = fake.client(), publish_key(REPO, 7, "abc123")
    asyncio.run(client.publish_comment(REPO, 7, key, "v1", None))
    _, action = asyncio.run(client.publish_comment(REPO, 7, key, "v2", None))
    assert action == "updated" and len(fake.comments) == 1


def test_publish_ignores_marker_spoofed_by_another_user():
    fake = FakeGitHub()
    key = publish_key(REPO, 7, "abc123")
    fake.comments.append({"id": 1, "body": f"{MARKER_PREFIX}{key} -->\nspoof", "user": {"login": "mallory"}})
    _, action = asyncio.run(fake.client().publish_comment(REPO, 7, key, "real", None))
    assert action == "created" and fake.comments[0]["body"].endswith("spoof")


def test_publish_recreates_comment_deleted_by_a_human():
    fake = FakeGitHub()
    _, action = asyncio.run(fake.client().publish_comment(REPO, 7, "k", "v", 999))
    assert action == "created"


def test_client_never_calls_review_approval_or_merge_endpoints():
    fake = FakeGitHub()
    client = fake.client()
    asyncio.run(client.fetch_pr(REPO, 7))
    asyncio.run(client.publish_comment(REPO, 7, "k", "v", None))
    assert not any(p.endswith(("/reviews", "/merge")) for _, p in fake.requests)


def test_webhook_signature_verification():
    body = b'{"a":1}'
    import hashlib
    import hmac

    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret", body, good)
    assert not verify_signature("s3cret", body + b" ", good)
    assert not verify_signature("s3cret", body, None) and not verify_signature("s3cret", body, "sha1=abc")
