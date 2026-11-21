"""A thin, synchronous GitHub REST client — only the ~12 endpoints the healer needs.

Decisions:
- httpx directly rather than PyGithub: every call here maps 1:1 to a documented REST endpoint,
  so you can read this file next to the GitHub docs. (The Anthropic SDK uses its own HTTP stack
  internally; this client is independent of it.)
- Commits are made through the Git Data API (blob-less tree → commit → ref). That creates ONE
  commit with several files and never needs a local clone, so the server stays stateless and
  never executes repository code.
- Webhook signature verification lives here because it's GitHub protocol, not app logic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import httpx

API = "https://api.github.com"


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Check X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(secret, raw_body).

    Must be computed over the *raw bytes* (re-serialising parsed JSON changes whitespace),
    and compared in constant time so the check doesn't leak how many bytes matched.
    """
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


class GitHubError(RuntimeError):
    pass


PERMISSION_HINTS = (
    ("not permitted to create or approve pull requests",
     "tick *Allow GitHub Actions to create and approve pull requests* "
     "(repo Settings → Actions → General → Workflow permissions)"),
    ("Resource not accessible by integration",
     "the workflow's `permissions:` block must grant `actions: write`, `contents: write` "
     "and `pull-requests: write`, or pass a PAT via `github-token`"),
)


def explain(e: GitHubError) -> str:
    """GitHub's 403s are accurate but cryptic; the two a new user hits are translated into the
    setting to change. Anything else is returned as-is."""
    text = str(e)
    for needle, hint in PERMISSION_HINTS:
        if needle in text:
            return f"{text} — permission problem: {hint}."
    if "-> 403" in text:
        return (f"{text} — permission problem: check the workflow's `permissions:` block and the "
                "token passed as `github-token`.")
    return text


class GitHubClient:
    def __init__(self, token: str, base_url: str = API, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ci-healer",
            },
            timeout=30,
            transport=transport,
        )

    def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            raise GitHubError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    # --- reading runs --------------------------------------------------------------------
    def get_run(self, repo: str, run_id: int) -> dict:
        return self._req("GET", f"/repos/{repo}/actions/runs/{run_id}").json()

    def list_failed_runs(self, repo: str, limit: int = 10) -> list[dict]:
        r = self._req("GET", f"/repos/{repo}/actions/runs",
                      params={"status": "failure", "per_page": limit})
        return r.json()["workflow_runs"]

    def list_jobs(self, repo: str, run_id: int, attempt: int) -> list[dict]:
        r = self._req("GET", f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
                      params={"per_page": 100})
        return r.json()["jobs"]

    def get_job_log(self, repo: str, job_id: int) -> str:
        # The API answers 302 → a short-lived signed URL on another host. httpx drops the
        # Authorization header when a redirect changes origin, so our token isn't leaked.
        r = self._http.get(f"/repos/{repo}/actions/jobs/{job_id}/logs", follow_redirects=True)
        if r.status_code >= 400:
            raise GitHubError(f"job log {job_id} -> {r.status_code}")
        return r.text

    def get_annotations(self, repo: str, check_run_id: int) -> list[dict]:
        try:
            return self._req("GET", f"/repos/{repo}/check-runs/{check_run_id}/annotations").json()
        except GitHubError:
            return []  # annotations are a bonus signal; never fail the analysis over them

    # --- acting ----------------------------------------------------------------------------
    def rerun_failed_jobs(self, repo: str, run_id: int) -> None:
        self._req("POST", f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs")

    def list_tree(self, repo: str, sha: str) -> list[str]:
        """Every file path in the commit, in one call. Used to map paths printed by tools
        (relative to a job's working-directory) back to repo paths."""
        r = self._http.get(f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"})
        if r.status_code >= 400:
            return []
        return [t["path"] for t in r.json().get("tree", []) if t.get("type") == "blob"]

    def get_file(self, repo: str, path: str, ref: str) -> str | None:
        r = self._http.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise GitHubError(f"get_file {path} -> {r.status_code}")
        data = r.json()
        if isinstance(data, list) or data.get("type") != "file":
            return None
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    def get_run_attempt(self, repo: str, run_id: int, attempt: int) -> dict:
        return self._req("GET", f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}").json()

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        r = self._http.get(f"/repos/{repo}/branches/{branch}")
        return r.json()["commit"]["sha"] if r.status_code == 200 else None

    def create_branch(self, repo: str, branch: str, sha: str) -> None:
        self._req("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    def commit_files(self, repo: str, branch: str, parent_sha: str,
                     files: dict[str, str], message: str) -> str:
        """One commit containing all `files` (path → full new content) on top of parent_sha."""
        parent = self._req("GET", f"/repos/{repo}/git/commits/{parent_sha}").json()
        tree = self._req("POST", f"/repos/{repo}/git/trees", json={
            "base_tree": parent["tree"]["sha"],
            "tree": [{"path": p, "mode": "100644", "type": "blob", "content": c}
                     for p, c in files.items()],
        }).json()
        commit = self._req("POST", f"/repos/{repo}/git/commits", json={
            "message": message, "tree": tree["sha"], "parents": [parent_sha],
        }).json()
        self._req("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": commit["sha"]})
        return commit["sha"]

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        return self._req("POST", f"/repos/{repo}/pulls", json={
            "head": head, "base": base, "title": title, "body": body, "draft": False,
        }).json()

    def comment(self, repo: str, issue_number: int, body: str) -> None:
        self._req("POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body})
