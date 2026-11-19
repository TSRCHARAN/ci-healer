"""The orchestrator: one `workflow_run` event in → recorded analyses and at most one action out.

    webhook ─► handle_workflow_run
                 ├─ not ours / not completed ──────────────► ignore
                 ├─ run on a ci-healer fix branch ─────────► record fix verification (never heal it)
                 ├─ success on attempt > 1 ───────────────► learn: earlier failures were transient
                 └─ failure ─► analyze each failed job
                                 log ─► excerpt ─► structural rules ─► signature rules ─► Claude
                                                                  └─► reconcile with history
                               ─► policy.decide ─► execute (rerun | fix PR | notify), dry-run aware

Every branch writes to SQLite, so `ci-healer report` can show exactly what happened and why.
"""

from __future__ import annotations

import dataclasses
import json
import logging

from . import logs, policy, rules, source
from .config import Settings
from .db import Database
from .fixer import (TEST_PATH, FixRejected, apply_edits, fence_safe, is_forbidden, normalise_path,
                    validate)
from .github import GitHubClient, GitHubError
from .llm import Analyzer, LLMAnalyzer, LLMError, unknown
from .providers import make_backend
from .models import Category, JobAnalysis, RunContext

log = logging.getLogger("ci_healer")

FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
MAX_JOBS_PER_RUN = 3          # bound cost on matrix builds where 20 jobs fail the same way
MAX_CANDIDATE_FILES = 6
MAX_FILE_BYTES = 100_000
CONFIG_EXTS = (".toml", ".cfg", ".ini", ".json", ".yml", ".yaml")


def resolve_repo_path(path: str, tree: list[str]) -> str | None:
    """Map a path as a tool printed it to a real repo path.

    Tools print paths relative to the job's working-directory, so in a monorepo with
    `defaults.run.working-directory: backend`, ruff says `app/x.py` for `backend/app/x.py`.
    Exact match wins; otherwise accept a suffix match only if it's unambiguous — guessing
    between `api/app/x.py` and `worker/app/x.py` is how you patch the wrong file.
    Without a tree (API failure) fall back to the path as printed.
    """
    if not tree or path in tree:
        return path
    matches = [p for p in tree if p.endswith("/" + path)]
    return matches[0] if len(matches) == 1 else None


class Healer:
    def __init__(self, settings: Settings, db: Database, gh: GitHubClient, analyzer: Analyzer):
        self.settings, self.db, self.gh, self.analyzer = settings, db, gh, analyzer

    @classmethod
    def from_settings(cls, s: Settings) -> "Healer":
        # Resolving the LLM config here makes a bad LLM_PROVIDER/MODEL/KEY fail at startup,
        # not on the first red build at 3 a.m.
        analyzer = LLMAnalyzer(make_backend(s.llm_config()))
        return cls(s, Database(s.db_path), GitHubClient(s.github_token), analyzer)

    # ===================================================================== entry point ====
    def handle_workflow_run(self, payload: dict) -> dict:
        if payload.get("action") != "completed":
            return {"status": "ignored", "reason": "run not completed"}
        ctx = RunContext.from_payload(payload)
        if not self.settings.repo_allowed(ctx.repo):
            return {"status": "ignored", "reason": f"{ctx.repo} not in CI_HEALER_ALLOWED_REPOS"}

        run_pk = self.db.upsert_run(ctx)
        if ctx.head_branch.startswith(self.settings.fix_branch_prefix):
            return self._record_fix_verification(ctx, run_pk)
        if ctx.conclusion == "success":
            return self._learn_from_rerun(ctx, run_pk) if ctx.run_attempt > 1 else {"status": "ok"}
        if ctx.conclusion not in FAILED_CONCLUSIONS:
            return {"status": "ignored", "reason": f"conclusion={ctx.conclusion}"}

        analyses = self.analyze(ctx, run_pk)
        prior = self.db.count_actions(ctx.repo, ctx.run_id, "rerun")
        decision = policy.decide(analyses, ctx, self.settings, prior)
        outcome = self.execute(decision, ctx, run_pk)
        return {
            "status": "analyzed", "run": ctx.html_url, "attempt": ctx.run_attempt,
            "jobs": [{"job": a.job_name, "step": a.failed_step,
                      "category": a.classification.category.value,
                      "confidence": a.classification.confidence, "source": a.source,
                      "llm": a.llm or None,
                      "summary": a.classification.summary} for a in analyses],
            "decision": decision.action, "reason": decision.reason, "outcome": outcome,
            "dry_run": self.settings.dry_run,
        }

    # ======================================================================== analysis ====
    def analyze(self, ctx: RunContext, run_pk: int) -> list[JobAnalysis]:
        if ctx.conclusion == "startup_failure":
            cls = rules.classify_structural(ctx.conclusion, "")
            a = JobAnalysis(0, "(workflow)", "", cls, "rule:structural", "", "")
            self.db.insert_analysis(run_pk, a)
            return [a]
        jobs = self.gh.list_jobs(ctx.repo, ctx.run_id, ctx.run_attempt)
        failed = [j for j in jobs if j.get("conclusion") in ("failure", "timed_out")]
        return [self._analyze_job(ctx, run_pk, j) for j in failed[:MAX_JOBS_PER_RUN]]

    def _analyze_job(self, ctx: RunContext, run_pk: int, job: dict) -> JobAnalysis:
        raw = self.gh.get_job_log(ctx.repo, job["id"])
        ex = logs.extract(raw, repo_name=ctx.repo.split("/")[-1])
        api_step = next((s["name"] for s in job.get("steps") or []
                         if s.get("conclusion") == "failure"), None)
        failed_step = api_step or ex.failed_step
        annotations = self.gh.get_annotations(ctx.repo, job["id"])
        history = self._history(ctx, ex)       # computed BEFORE we record this attempt
        usage: dict = {}

        structural = rules.classify_structural(job.get("conclusion", ""), failed_step)
        rule_hit = None if structural else rules.classify_by_rules(ex, annotations)
        if structural:
            cls, source = structural, "rule:structural"
        elif rule_hit:
            cls, source = rule_hit[0], f"rule:{rule_hit[1]}"
        else:
            try:
                cls, usage = self.analyzer.classify(ctx, job.get("name", ""), ex, annotations, history)
                source = "llm"
            except LLMError as e:
                log.warning("LLM classification failed: %s", e)
                cls, source = unknown(f"LLM unavailable: {e}"), "llm-error"

        cls, adjusted = policy.reconcile(cls, history)
        if adjusted:
            source += "+history"

        self.db.record_test_outcomes(ctx, ex.failing_tests, "failed")
        a = JobAnalysis(
            job_id=job["id"], job_name=job.get("name", ""), failed_step=failed_step,
            classification=cls, source=source, signature=ex.signature, excerpt=ex.text,
            file_refs=ex.file_refs, llm=usage.get("llm", ""), input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        self.db.insert_analysis(run_pk, a)
        return a

    def _earlier_attempt_failures(self, ctx: RunContext) -> list[str]:
        """Tests that failed on earlier attempts of THIS run (same commit).

        Local history first. If it's missing — a different server, or (seen in the live demo)
        parallel Action runs whose cached databases overwrote each other — ask GitHub: the
        previous attempt's jobs and logs are still there. The most important flakiness signal
        must not depend on a cache.
        """
        tests = self.db.failing_tests_of_earlier_attempts(ctx.repo, ctx.run_id, ctx.run_attempt)
        if tests or ctx.run_attempt <= 1:
            return tests
        prev = dataclasses.replace(ctx, run_attempt=ctx.run_attempt - 1)
        found: list[str] = []
        try:
            for job in self.gh.list_jobs(ctx.repo, ctx.run_id, prev.run_attempt):
                if job.get("conclusion") in ("failure", "timed_out"):
                    ex = logs.extract(self.gh.get_job_log(ctx.repo, job["id"]))
                    found += [t for t in ex.failing_tests if t not in found]
        except GitHubError as e:
            log.warning("could not read attempt %s from GitHub: %s", prev.run_attempt, e)
            return []
        self.db.record_test_outcomes(prev, found, "failed")   # backfill local history
        return found

    def _history(self, ctx: RunContext, ex: logs.LogExcerpt) -> dict:
        earlier = set(self._earlier_attempt_failures(ctx))
        return {
            "failing_tests": {t: self.db.test_history(ctx.repo, t) for t in ex.failing_tests[:10]},
            "reproduced_on_same_commit": sorted(set(ex.failing_tests) & earlier),
            "signature": self.db.signature_history(ctx.repo, ex.signature) if ex.signature else None,
        }

    # ======================================================================= execution ====
    def execute(self, d: policy.Decision, ctx: RunContext, run_pk: int) -> dict:
        dry = self.settings.dry_run
        if d.action == "rerun":
            if dry:
                self.db.insert_action(run_pk, "rerun", "planned", True, d.reason)
                return {"rerun": "planned (dry run)"}
            try:
                self.gh.rerun_failed_jobs(ctx.repo, ctx.run_id)
            except GitHubError as e:
                self.db.insert_action(run_pk, "rerun", "failed", False, d.reason, {"error": str(e)})
                return {"rerun": f"failed: {e}"}
            self.db.insert_action(run_pk, "rerun", "done", False, d.reason)
            return {"rerun": "triggered"}

        if d.action == "fix_pr" and d.job:
            return self._attempt_fix(d.job, ctx, run_pk)

        self._notify(ctx, run_pk, d.reason)
        return {"notify": d.reason}

    def _notify(self, ctx: RunContext, run_pk: int, reason: str) -> None:
        self.db.insert_action(run_pk, "notify", "done", self.settings.dry_run, reason)
        if self.settings.comment_on_prs and not self.settings.dry_run and ctx.pr_numbers:
            body = (f"**ci-healer** looked at [this failed run]({ctx.html_url}) "
                    f"(attempt {ctx.run_attempt}) and took no automatic action.\n\n> {reason}")
            try:
                self.gh.comment(ctx.repo, ctx.pr_numbers[0], body)
            except GitHubError as e:
                log.warning("comment failed: %s", e)

    # ------------------------------------------------------------------------------------
    def _attempt_fix(self, a: JobAnalysis, ctx: RunContext, run_pk: int) -> dict:
        def skip(why: str, detail: dict | None = None) -> dict:
            self.db.insert_action(run_pk, "fix_pr", "skipped", self.settings.dry_run, why, detail)
            return {"fix_pr": f"skipped: {why}"}

        # Don't fix a commit the branch has already moved past — the failure may be stale.
        branch_sha = self.gh.get_branch_sha(ctx.repo, ctx.head_branch)
        if branch_sha and branch_sha != ctx.head_sha:
            return skip("branch has new commits since the failing one")

        files = self._fetch_candidates(ctx, a)
        if not files:
            return skip("could not fetch any candidate source files")

        try:
            proposal, usage = self.analyzer.propose_fix(ctx, a.classification, a.excerpt, files)
        except LLMError as e:
            return skip(f"LLM error: {e}")
        if not proposal.can_fix:
            return skip(f"model declined: {proposal.explanation}")

        try:
            changed = apply_edits(files, proposal.edits)
            diff = validate(files, changed, self.settings)
            self._check_complete(a, files, changed)
        except FixRejected as e:
            return skip(f"guardrail: {e}", {"edits": [x.model_dump() for x in proposal.edits]})

        branch = f"{self.settings.fix_branch_prefix}{ctx.run_id}-{ctx.run_attempt}"
        detail = {"branch": branch, "title": proposal.pr_title, "diff": diff,
                  "files": sorted(changed), "usage": usage}
        if self.settings.dry_run:
            self.db.insert_action(run_pk, "fix_pr", "planned", True, proposal.explanation, detail)
            return {"fix_pr": "planned (dry run)", "branch": branch, "diff": diff}

        try:
            self.gh.create_branch(ctx.repo, branch, ctx.head_sha)
            self.gh.commit_files(ctx.repo, branch, ctx.head_sha, changed,
                                 f"{proposal.pr_title}\n\nAutomated fix proposed by ci-healer for {ctx.html_url}")
            pr = self.gh.create_pull(ctx.repo, head=branch, base=ctx.head_branch,
                                     title=f"[ci-healer] {proposal.pr_title}",
                                     body=self._pr_body(a, ctx, proposal.explanation, diff))
        except GitHubError as e:
            self.db.insert_action(run_pk, "fix_pr", "failed", False, str(e), detail)
            return {"fix_pr": f"failed: {e}"}
        detail["pr_url"] = pr.get("html_url")
        self.db.insert_action(run_pk, "fix_pr", "done", False, proposal.explanation, detail)
        return {"fix_pr": "opened", "url": pr.get("html_url")}

    @staticmethod
    def _check_complete(a: JobAnalysis, files: dict[str, str], changed: dict[str, str]) -> None:
        """Reject partial lint fixes. Found while dogfooding: a small model fixed 2 of 3 ruff
        errors and quietly skipped the hard one. Every guardrail passed, yet the PR would still
        be red. For lint failures, every *source* file the linter output names must be edited.
        (Config files that a linter merely mentions don't count.)"""
        if a.classification.category != Category.LINT_FORMAT:
            return
        flagged = {p for p in files if not p.endswith(CONFIG_EXTS)}
        missing = sorted(flagged - set(changed))
        if missing:
            raise FixRejected(f"partial fix — linter also flagged {', '.join(missing)}, which "
                              f"the proposal leaves untouched (CI would stay red)")

    def _fetch_candidates(self, ctx: RunContext, a: JobAnalysis) -> dict[str, str]:
        """Files to show the fixer, in priority order:
        1. what the classifier / the log named (lint errors point straight at the line);
        2. the failing tests' own files;
        3. the repo modules those test files import — where a logic bug actually lives.
        Everything is read-only, via the API, at the failing commit; nothing is executed."""
        tree = self.gh.list_tree(ctx.repo, ctx.head_sha)
        files: dict[str, str] = {}
        wanted = (a.classification.implicated_files + a.file_refs
                  + source.files_of_failing_tests(a.classification.failing_tests))
        self._fetch_into(files, wanted, ctx, tree)

        if self.settings.investigate_code_breaks and a.classification.category == Category.CODE_BREAK:
            imported: list[str] = []
            for path, text in list(files.items()):
                if TEST_PATH.search(path) and path.endswith(".py"):
                    imported += [p for p in source.imported_repo_files(text, path, tree)
                                 if p not in imported]
            self._fetch_into(files, imported, ctx, tree)
        return files

    def _fetch_into(self, files: dict[str, str], paths: list[str], ctx: RunContext,
                    tree: list[str]) -> None:
        for raw in paths:
            if len(files) >= MAX_CANDIDATE_FILES:
                return
            try:
                path = resolve_repo_path(normalise_path(raw), tree)
            except FixRejected:
                continue
            if not path or path in files or is_forbidden(path):
                continue
            content = self.gh.get_file(ctx.repo, path, ctx.head_sha)
            if content is not None and len(content) <= MAX_FILE_BYTES:
                files[path] = content

    def _pr_body(self, a: JobAnalysis, ctx: RunContext, explanation: str, diff: str) -> str:
        c = a.classification
        evidence = fence_safe("\n".join(c.evidence)) or "(none)"
        return f"""## 🤖 ci-healer proposed fix — human review required

**Failed run:** {ctx.html_url} (attempt {ctx.run_attempt}) · job `{a.job_name}` · step `{a.failed_step}`
**Diagnosis:** `{c.category.value}` · confidence {c.confidence:.2f} · via `{a.source}`

{fence_safe(c.summary)}

**Evidence from the log**
```
{evidence}
```

**What this changes**

{fence_safe(explanation)}

```diff
{fence_safe(diff)}
```

---
Guardrails passed: ≤{self.settings.max_fix_files} files, ≤{self.settings.max_fix_changed_lines} changed lines,
no workflow/lockfile/secret paths, no suppressions, no skipped tests or weakened assertions.
CI on this PR is the verification. This PR will never be merged automatically."""

    # ===================================================================== learning ======
    def _learn_from_rerun(self, ctx: RunContext, run_pk: int) -> dict:
        """Attempt N>1 of the same run passed → the tests that failed earlier on this very
        commit just passed. Record that; it's the ground truth flaky detection runs on."""
        earlier = self._earlier_attempt_failures(ctx)
        self.db.record_test_outcomes(ctx, earlier, "passed")
        self.db.insert_action(run_pk, "learned", "done", self.settings.dry_run,
                              "rerun of the same commit passed → earlier failure was transient",
                              {"tests_now_passing": earlier})
        return {"status": "learned", "tests_marked_flaky_evidence": earlier}

    def _record_fix_verification(self, ctx: RunContext, run_pk: int) -> dict:
        fix = self.db.find_fix_action(ctx.repo, ctx.head_branch)
        self.db.insert_action(run_pk, "fix_verified", "done", self.settings.dry_run,
                              f"CI on fix branch concluded: {ctx.conclusion}",
                              {"branch": ctx.head_branch, "conclusion": ctx.conclusion,
                               "fix_action_id": fix["id"] if fix else None})
        return {"status": "fix_verification", "branch": ctx.head_branch, "conclusion": ctx.conclusion}
