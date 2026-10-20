"""GitHub REST adapter. Reads PR context; publishes one idempotent issue comment per head commit.

Least privilege (fine-grained token): Pull requests: read, Contents: read, Checks: read.
Publishing additionally needs Pull requests: write (or Issues: write) AND GITHUB_PUBLISH_ENABLED=true.
It only ever creates/updates an issue comment; it never submits an approving review or merges.
"""

from __future__ import annotations

import hashlib
import hmac

import httpx

from app.models import PRInput, ToolResult
from app.observability import log_event, safe_message

MARKER_PREFIX = "<!-- pr-quality-chain:publish-key="
MAX_FILE_BYTES = 200_000
MAX_CONTENT_FILES = 20


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def publish_key(repo: str, pr_number: int, head_sha: str) -> str:
    return f"{repo}#{pr_number}@{head_sha}"


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


_CHECK_STATUS = {
    "success": "passed",
    "failure": "failed",
    "timed_out": "failed",
    "cancelled": "error",
    "action_required": "error",
    "neutral": "passed",
    "skipped": "skipped",
    "stale": "error",
}


class GitHubClient:
    def __init__(
        self,
        token: str | None,
        api_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-quality-chain",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(base_url=api_url, headers=headers, timeout=30, transport=transport)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, **kw) -> httpx.Response:
        response = await self.http.get(path, **kw)
        if response.status_code >= 400:
            log_event("github_error", path=path, http_status=response.status_code)
            raise GitHubError(safe_message(f"GitHub GET {path} failed: {response.status_code}"), response.status_code)
        return response

    async def fetch_pr(self, repo: str, number: int, max_files: int = 50) -> PRInput:
        pr = (await self._get(f"/repos/{repo}/pulls/{number}")).json()
        head_sha = pr["head"]["sha"]
        files: list[dict] = []
        page = 1
        while len(files) < max_files:
            batch = (
                await self._get(f"/repos/{repo}/pulls/{number}/files", params={"per_page": 100, "page": page})
            ).json()
            files += batch
            if len(batch) < 100:
                break
            page += 1
        files = files[:max_files]

        diff_parts, contents = [], {}
        for f in files:
            old = f.get("previous_filename", f["filename"])
            header = f"diff --git a/{old} b/{f['filename']}\n"
            if f["status"] == "added":
                header += "new file mode 100644\n"
            elif f["status"] == "removed":
                header += "deleted file mode 100644\n"
            elif f["status"] == "renamed":
                header += f"rename from {old}\nrename to {f['filename']}\n"
            if "patch" not in f:  # GitHub omits patches for binary or very large files
                diff_parts.append(header + "Binary files differ or patch too large\n")
                continue
            a = "/dev/null" if f["status"] == "added" else f"a/{old}"
            b = "/dev/null" if f["status"] == "removed" else f"b/{f['filename']}"
            diff_parts.append(f"{header}--- {a}\n+++ {b}\n{f['patch']}\n")
            if f["status"] != "removed" and len(contents) < MAX_CONTENT_FILES:
                content = await self.file_content(repo, f["filename"], head_sha)
                if content is not None:
                    contents[f["filename"]] = content

        return PRInput(
            title=pr.get("title") or "",
            description=(pr.get("body") or "")[:20_000],
            diff="".join(diff_parts),
            repo=repo,
            pr_number=number,
            head_sha=head_sha,
            file_contents=contents,
            tool_results=await self.check_results(repo, head_sha),
        )

    async def file_content(self, repo: str, path: str, ref: str) -> str | None:
        try:
            response = await self._get(
                f"/repos/{repo}/contents/{path}",
                params={"ref": ref},
                headers={"Accept": "application/vnd.github.raw+json"},
            )
        except GitHubError:
            return None
        if len(response.content) > MAX_FILE_BYTES:
            return None
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            return None

    async def check_results(self, repo: str, sha: str) -> list[ToolResult]:
        """Existing CI results for the head commit. Empty if unavailable (e.g. no Checks permission)."""
        try:
            runs = (await self._get(f"/repos/{repo}/commits/{sha}/check-runs", params={"per_page": 100})).json()
        except GitHubError:
            return []
        results = []
        for run in runs.get("check_runs", []):
            status = (
                "pending" if run.get("status") != "completed" else _CHECK_STATUS.get(run.get("conclusion"), "error")
            )
            summary = ((run.get("output") or {}).get("title") or "")[:300]
            results.append(ToolResult(name=run.get("name", "check"), kind="ci", status=status, summary=summary))
        return results

    async def publish_comment(
        self, repo: str, number: int, key: str, body: str, known_comment_id: int | None
    ) -> tuple[int, str]:
        """Create or update the single report comment for `key`. Returns (comment_id, action)."""
        marked = f"{MARKER_PREFIX}{key} -->\n{body}"
        comment_id = known_comment_id or await self._find_marked_comment(repo, number, key)
        if comment_id:
            response = await self.http.patch(f"/repos/{repo}/issues/comments/{comment_id}", json={"body": marked})
            if response.status_code == 404:  # deleted by a human: create a fresh one
                comment_id = None
            elif response.status_code >= 400:
                raise GitHubError(f"GitHub comment update failed: {response.status_code}", response.status_code)
            else:
                return comment_id, "updated"
        response = await self.http.post(f"/repos/{repo}/issues/{number}/comments", json={"body": marked})
        if response.status_code >= 400:
            raise GitHubError(f"GitHub comment creation failed: {response.status_code}", response.status_code)
        return response.json()["id"], "created"

    async def _find_marked_comment(self, repo: str, number: int, key: str) -> int | None:
        """Recovers our comment if the local record was lost. Only trusts comments written by this token's
        own account, so nobody can make us overwrite their comment by pasting the marker."""
        try:
            login = (await self._get("/user")).json()["login"]
        except (GitHubError, KeyError):
            return None  # e.g. GitHub App installation tokens; rely on the local publication record
        marker = f"{MARKER_PREFIX}{key} -->"
        page = 1
        while page <= 10:
            comments = (
                await self._get(f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100, "page": page})
            ).json()
            for comment in comments:
                if (comment.get("user") or {}).get("login") == login and (comment.get("body") or "").startswith(marker):
                    return comment["id"]
            if len(comments) < 100:
                return None
            page += 1
        return None
