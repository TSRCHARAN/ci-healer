"""Policy: turn per-job classifications into ONE action for the run.

This is where trust lives, so it's deliberately small, pure (no I/O) and fully unit-tested.
Two functions:

- `reconcile` — let *history* overrule a single-log guess. The LLM sees one log; the database
  has seen the same test pass and fail on one commit. When they disagree, history wins.
- `decide` — choose rerun / fix_pr / notify for the whole run.

Ordering of the decision:
1. If EVERY failed job looks transient (infra/network/flaky) → rerun, within budget.
   Rerunning is cheap, reversible, and turns the hypothesis "this is flaky" into a label.
2. Else if some job is a fix candidate passing every gate → open a fix PR.
3. Else → notify (record the diagnosis; optionally comment on the PR). Doing nothing is a
   perfectly good outcome — a wrong action is worse than no action.

Why "every job" for reruns? `rerun-failed-jobs` reruns all failed jobs; if one of them is a real
break the rerun burns CI minutes and still fails.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import FIXABLE, RETRYABLE, Category, Classification, JobAnalysis, RunContext


@dataclass
class Decision:
    action: str                 # rerun | fix_pr | notify
    reason: str
    job: JobAnalysis | None = None


def reconcile(cls: Classification, history: dict) -> tuple[Classification, bool]:
    tests: dict = history.get("failing_tests") or {}
    reproduced: list[str] = history.get("reproduced_on_same_commit") or []
    failing = list(tests)

    # A "flaky" verdict for tests that already failed on an earlier attempt of THIS commit:
    # the experiment says it reproduces. Treat as deterministic.
    if cls.category == Category.FLAKY_TEST and failing and set(failing) <= set(reproduced):
        return cls.model_copy(update={
            "category": Category.CODE_BREAK,
            "confidence": 0.7,
            "trivial_fix_possible": False,
            "summary": "Failed again on a rerun of the same commit, so not treated as flaky. "
                       + cls.summary,
        }), True

    # A "code break"/"unknown" verdict, but every failing test has flipped on a single commit
    # before and it hasn't reproduced on this one yet: history says flaky.
    if (cls.category in (Category.CODE_BREAK, Category.UNKNOWN) and failing
            and not reproduced and all(t.get("flip_shas", 0) >= 1 for t in tests.values())):
        return cls.model_copy(update={
            "category": Category.FLAKY_TEST,
            "confidence": max(cls.confidence, 0.75),
            "trivial_fix_possible": False,
            "summary": "History: these tests have both passed and failed on the same commit "
                       "before. " + cls.summary,
        }), True

    # Unknown failure whose exact signature has usually gone away on rerun → transient.
    sig = history.get("signature") or {}
    if (cls.category == Category.UNKNOWN and sig.get("later_passed", 0) >= 2
            and sig["later_passed"] / max(sig.get("seen", 1), 1) >= 0.5):
        cat = Category.FLAKY_TEST if failing else Category.INFRA
        return cls.model_copy(update={
            "category": cat, "confidence": 0.65,
            "summary": f"Signature seen {sig['seen']}x and passed on rerun {sig['later_passed']}x. "
                       + cls.summary,
        }), True

    return cls, False


def fix_eligibility(a: JobAnalysis, ctx: RunContext, s: Settings) -> tuple[bool, str]:
    c = a.classification
    checks = [
        (s.autofix_enabled, "autofix disabled"),
        (not ctx.is_fork, "run is from a fork (untrusted, and we can't push there)"),
        (bool(ctx.head_branch), "run has no branch (tag or detached)"),
        (not ctx.head_branch.startswith(s.fix_branch_prefix), "run is on our own fix branch"),
        (c.category in FIXABLE, f"category '{c.category.value}' is not auto-fixable"),
        (c.trivial_fix_possible, "fix judged non-trivial"),
        (c.confidence >= s.min_fix_confidence,
         f"confidence {c.confidence:.2f} < {s.min_fix_confidence}"),
        (bool(c.implicated_files or a.file_refs), "no candidate source files identified"),
    ]
    for ok, why in checks:
        if not ok:
            return False, why
    return True, "eligible"


def decide(analyses: list[JobAnalysis], ctx: RunContext, s: Settings, prior_reruns: int) -> Decision:
    if not analyses:
        return Decision("notify", "no failed jobs found to analyse")

    cats = [a.classification.category for a in analyses]
    if all(c in RETRYABLE for c in cats):
        used = max(ctx.run_attempt - 1, prior_reruns)
        if used >= s.max_reruns_per_run:
            return Decision("notify", f"looks transient ({', '.join(c.value for c in cats)}) but retry "
                                      f"budget is spent (attempt {ctx.run_attempt}); escalating to a human")
        return Decision("rerun", "all failed jobs look transient: "
                        + "; ".join(f"{a.job_name}={a.classification.category.value}" for a in analyses))

    reasons = []
    for a in analyses:
        ok, why = fix_eligibility(a, ctx, s)
        if ok:
            return Decision("fix_pr", f"{a.job_name}: {a.classification.category.value}, "
                                      f"confidence {a.classification.confidence:.2f}", job=a)
        reasons.append(f"{a.job_name}: {why}")
    return Decision("notify", "no safe automatic action — " + "; ".join(reasons))
