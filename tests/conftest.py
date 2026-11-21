"""Test doubles and realistic fixture logs.

The pipeline depends on two outside worlds — GitHub and Claude — through narrow interfaces, so
tests swap in fakes and exercise every real code path (parsing, rules, history, policy,
guardrails, persistence) without network calls or API spend.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field

import pytest
from ci_healer.github import GitHubError

from ci_healer.config import Settings
from ci_healer.db import Database
from ci_healer.models import Category, Classification, FileEdit, FixProposal
from ci_healer.pipeline import Healer

REPO = "octo/demo"
SECRET = "test-secret"
TS = "2026-11-19T10:00:0{}.1234567Z "


def gh_log(*lines: str) -> str:
    """Render lines the way GitHub serves them: timestamp prefix on every line."""
    return "\n".join(TS.format(i % 10) + l for i, l in enumerate(lines))


SETUP = ["##[group]Operating System", "Ubuntu 24.04", "##[endgroup]",
         "##[group]Run actions/checkout@v4", "with:", "  repository: octo/demo", "##[endgroup]",
         "Syncing repository: octo/demo"]

FLAKY_LOG = gh_log(*SETUP,
    "##[group]Run pytest -q", "\x1b[36;1mpytest -q\x1b[0m", "##[endgroup]",
    "........................................F.",
    "=================================== FAILURES ===================================",
    "___________________________ test_fetch_with_timeout ____________________________",
    "    def test_fetch_with_timeout():",
    ">       assert client.fetch('/slow', timeout=2) == 'ok'",
    "E       TimeoutError: deadline exceeded after 2.013s",
    "/home/runner/work/demo/demo/tests/test_api.py:41: TimeoutError",
    "=========================== short test summary info ============================",
    "FAILED tests/test_api.py::test_fetch_with_timeout - TimeoutError: deadline exceeded after 2.013s",
    "1 failed, 41 passed in 3.41s",
    "##[error]Process completed with exit code 1.")

RUFF_LOG = gh_log(*SETUP,
    "##[group]Run ruff check .", "ruff check .", "##[endgroup]",
    "src/demo/util.py:1:8: F401 [*] `os` imported but unused",
    "Found 1 error.",
    "[*] 1 fixable with the `--fix` option.",
    "##[error]Process completed with exit code 1.")

INFRA_LOG = gh_log(*SETUP,
    "##[group]Run make build", "make build", "##[endgroup]",
    "compiling module 14/90",
    "##[error]The runner has received a shutdown signal. This can happen when the runner service is stopped.",
    "##[error]The operation was canceled.")

UTIL_PY = "import os\nimport sys\n\n\ndef argv0() -> str:\n    return sys.argv[0]\n"


@dataclass
class FakeGitHub:
    jobs: dict = field(default_factory=dict)        # (run_id, attempt) -> [job]
    logs: dict = field(default_factory=dict)        # job_id -> text
    files: dict = field(default_factory=dict)       # path -> content
    branch_shas: dict = field(default_factory=dict)
    reruns: list = field(default_factory=list)
    branches: list = field(default_factory=list)
    commits: list = field(default_factory=list)
    pulls: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    refuse: dict = field(default_factory=dict)      # method name -> GitHubError to raise

    def _maybe_refuse(self, name):
        if name in self.refuse:
            raise self.refuse[name]

    def list_jobs(self, repo, run_id, attempt):
        return self.jobs.get((run_id, attempt), [])

    def get_job_log(self, repo, job_id):
        return self.logs[job_id]

    def get_annotations(self, repo, job_id):
        return []

    def rerun_failed_jobs(self, repo, run_id):
        self._maybe_refuse("rerun_failed_jobs")
        self.reruns.append(run_id)

    def get_branch_sha(self, repo, branch):
        return self.branch_shas.get(branch)

    def list_tree(self, repo, sha):
        return list(self.files)

    def get_file(self, repo, path, ref):
        return self.files.get(path)

    def create_branch(self, repo, branch, sha):
        self._maybe_refuse("create_branch")
        self.branches.append((branch, sha))

    def commit_files(self, repo, branch, parent_sha, files, message):
        self.commits.append((branch, files, message))
        return "c0ffee"

    def create_pull(self, repo, head, base, title, body):
        self._maybe_refuse("create_pull")
        self.pulls.append({"head": head, "base": base, "title": title, "body": body})
        return {"html_url": f"https://github.com/{repo}/pull/{len(self.pulls)}"}

    def comment(self, repo, number, body):
        self.comments.append((number, body))


@dataclass
class FakeAnalyzer:
    """Scripted Claude: returns queued classifications / proposals and records the calls."""

    classifications: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    classify_calls: list = field(default_factory=list)
    fix_calls: list = field(default_factory=list)

    def classify(self, ctx, job_name, excerpt, annotations, history):
        self.classify_calls.append({"job": job_name, "excerpt": excerpt, "history": history})
        return self.classifications.pop(0), {"llm": "fake/model-1", "input_tokens": 1000,
                                             "output_tokens": 200}

    def propose_fix(self, ctx, classification, excerpt_text, files):
        self.fix_calls.append({"files": files})
        return self.proposals.pop(0), {"input_tokens": 2000, "output_tokens": 300}


def cls(category: Category, confidence=0.9, trivial=False, files=None, tests=None) -> Classification:
    return Classification(category=category, confidence=confidence, summary=f"{category.value} test",
                          evidence=["E   something"], failing_tests=tests or [],
                          implicated_files=files or [], trivial_fix_possible=trivial)


def remove_os_import() -> FixProposal:
    return FixProposal(can_fix=True, explanation="Remove the unused `os` import flagged by ruff F401.",
                       pr_title="Remove unused os import",
                       edits=[FileEdit(path="src/demo/util.py", old_string="import os\nimport sys\n",
                                       new_string="import sys\n")])


def payload(run_id=100, attempt=1, conclusion="failure", sha="a" * 40, branch="feature/x",
            fork=False, prs=(7,)) -> dict:
    return {
        "action": "completed",
        "repository": {"full_name": REPO},
        "workflow_run": {
            "id": run_id, "run_attempt": attempt, "head_sha": sha, "head_branch": branch,
            "name": "CI", "event": "pull_request", "conclusion": conclusion,
            "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "head_repository": {"full_name": "evil/demo" if fork else REPO},
            "pull_requests": [{"number": n} for n in prs],
        },
    }


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def post(client, data: dict, delivery: str, event: str = "workflow_run"):
    body = json.dumps(data).encode()
    return client.post("/webhook", content=body, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": sign(body), "Content-Type": "application/json"})


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(github_token="x", webhook_secret=SECRET, db_path=str(tmp_path / "t.db"),
                    dry_run=False, allowed_repos=frozenset({REPO}))


@pytest.fixture
def world(settings):
    gh, an = FakeGitHub(), FakeAnalyzer()
    healer = Healer(settings, Database(settings.db_path), gh, an)
    return healer, gh, an
