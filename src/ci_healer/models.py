"""Data shapes shared across the pipeline.

The Pydantic models double as the *structured-output schemas* we hand to Claude: the API
constrains the model's reply to exactly this JSON shape, and the SDK validates it back into
these classes. That's why there's no free-text parsing of LLM output anywhere in the codebase.

Note: we deliberately avoid numeric constraints (ge/le) in schemas sent to the API and clamp in
code instead, which keeps the schemas inside what structured outputs supports everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel


class Category(str, Enum):
    INFRA = "infra"              # runner died, disk full, OOM, job timeout
    NETWORK = "network"          # registry/DNS/connection resets — external, transient
    FLAKY_TEST = "flaky_test"    # test is non-deterministic w.r.t. the code
    LINT_FORMAT = "lint_format"  # linter / formatter / type-checker complaint
    CODE_BREAK = "code_break"    # deterministic failure caused by the code
    DEPENDENCY = "dependency"    # upstream package/lockfile drift
    CONFIG = "config"            # workflow YAML, secrets, permissions
    UNKNOWN = "unknown"


RETRYABLE = {Category.INFRA, Category.NETWORK, Category.FLAKY_TEST}
FIXABLE = {Category.LINT_FORMAT, Category.CODE_BREAK}


class Classification(BaseModel):
    """What Claude (or a rule) concluded about one failed job."""

    category: Category
    confidence: float
    summary: str
    evidence: list[str]           # verbatim log lines that support the verdict
    failing_tests: list[str]
    implicated_files: list[str]   # repo-relative paths that likely need changing
    trivial_fix_possible: bool    # small, local, obvious edit would fix it


class FileEdit(BaseModel):
    """An exact search/replace edit — the same shape as a code editor's 'replace' tool.

    Why not unified diffs? LLMs get line numbers and hunk headers wrong; an exact old_string
    either matches the file once or the edit is rejected. Cheap to validate, impossible to
    misapply silently.
    """

    path: str
    old_string: str
    new_string: str


class FixProposal(BaseModel):
    can_fix: bool
    explanation: str
    pr_title: str
    edits: list[FileEdit]


@dataclass
class RunContext:
    """The facts about a workflow run that every stage needs."""

    repo: str
    run_id: int
    run_attempt: int
    head_sha: str
    head_branch: str
    workflow_name: str
    event: str
    conclusion: str
    html_url: str
    is_fork: bool = False
    pr_numbers: list[int] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict) -> "RunContext":
        run = payload["workflow_run"]
        repo = payload["repository"]["full_name"]
        head_repo = (run.get("head_repository") or {}).get("full_name", repo)
        return cls(
            repo=repo,
            run_id=run["id"],
            run_attempt=run.get("run_attempt", 1),
            head_sha=run["head_sha"],
            head_branch=run.get("head_branch") or "",
            workflow_name=run.get("name") or "",
            event=run.get("event") or "",
            conclusion=run.get("conclusion") or "",
            html_url=run.get("html_url") or "",
            is_fork=head_repo.lower() != repo.lower(),
            pr_numbers=[p["number"] for p in run.get("pull_requests") or []],
        )


@dataclass
class JobAnalysis:
    job_id: int
    job_name: str
    failed_step: str
    classification: Classification
    source: str                   # "rule" | "llm" | "rule+history" | "llm+history"
    signature: str
    excerpt: str
    file_refs: list[str] = field(default_factory=list)
    llm: str = ""                 # "provider/model" that classified it; "" when a rule did
    input_tokens: int = 0
    output_tokens: int = 0
