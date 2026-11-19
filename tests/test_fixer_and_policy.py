import pytest

from ci_healer.fixer import FixRejected, apply_edits, validate
from ci_healer.models import Category, FileEdit, JobAnalysis, RunContext
from ci_healer.policy import decide, reconcile

from conftest import UTIL_PY, cls

FILES = {"src/demo/util.py": UTIL_PY,
         "tests/test_util.py": "def test_argv0():\n    assert argv0()\n    assert len(argv0()) > 0\n",
         ".github/workflows/ci.yml": "on: push\n"}


def run(files_edits, settings):
    changed = apply_edits(FILES, files_edits)
    return validate(FILES, changed, settings)


# ---------------------------------------------------------------- fixer guardrails ----
def test_valid_minimal_edit_produces_diff(settings):
    diff = run([FileEdit(path="./src/demo/util.py", old_string="import os\n", new_string="")], settings)
    assert "-import os" in diff


@pytest.mark.parametrize("edit, why", [
    (FileEdit(path="src/demo/util.py", old_string="import", new_string="from x import"), "matches 2 times"),
    (FileEdit(path="src/other.py", old_string="a", new_string="b"), "wasn't provided"),
    (FileEdit(path="./.github/workflows/ci.yml", old_string="on: push", new_string="on: [push]"), "forbidden"),
    (FileEdit(path="../etc/passwd", old_string="x", new_string="y"), "unsafe path"),
    (FileEdit(path="src/demo/util.py", old_string="import os\n", new_string="import os  # noqa: F401\n"), "suppression"),
    (FileEdit(path="tests/test_util.py", old_string="def test_argv0",
              new_string="@pytest.mark.skip\ndef test_argv0"), "skip/xfail"),
    (FileEdit(path="tests/test_util.py", old_string="    assert len(argv0()) > 0\n",
              new_string="    assert len(argv0()) >= 0\n"), "assertion"),
])
def test_guardrails_reject_unsafe_edits(edit, why, settings):
    with pytest.raises(FixRejected, match=why):
        run([edit], settings)


def test_size_limit(settings):
    big = "\n".join(f"x{i} = {i}" for i in range(60)) + "\n"
    with pytest.raises(FixRejected, match="changes .* lines"):
        run([FileEdit(path="src/demo/util.py", old_string="import os\n", new_string=big)], settings)


def test_monorepo_paths_resolve_only_when_unambiguous():
    from ci_healer.pipeline import resolve_repo_path
    tree = ["backend/app/llm/gemini.py", "README.md", "api/app/x.py", "worker/app/x.py"]
    assert resolve_repo_path("README.md", tree) == "README.md"
    assert resolve_repo_path("app/llm/gemini.py", tree) == "backend/app/llm/gemini.py"
    assert resolve_repo_path("app/x.py", tree) is None                 # two candidates: refuse
    assert resolve_repo_path("nope.py", tree) is None
    assert resolve_repo_path("a.py", []) == "a.py"                      # no tree: as printed


# ---------------------------------------------------------------------- policy -------
def ctx(attempt=1, fork=False, branch="feature/x"):
    return RunContext(repo="octo/demo", run_id=1, run_attempt=attempt, head_sha="a" * 40,
                      head_branch=branch, workflow_name="CI", event="pull_request",
                      conclusion="failure", html_url="", is_fork=fork)


def job(c, refs=None):
    return JobAnalysis(job_id=1, job_name="test", failed_step="pytest", classification=c,
                       source="llm", signature="s", excerpt="", file_refs=refs or [])


def test_transient_failures_are_retried_once(settings):
    jobs = [job(cls(Category.INFRA)), job(cls(Category.FLAKY_TEST))]
    assert decide(jobs, ctx(attempt=1), settings, prior_reruns=0).action == "rerun"
    second = decide(jobs, ctx(attempt=2), settings, prior_reruns=1)
    assert second.action == "notify" and "budget" in second.reason


def test_a_real_break_blocks_the_rerun(settings):
    jobs = [job(cls(Category.INFRA)), job(cls(Category.CODE_BREAK, trivial=False))]
    assert decide(jobs, ctx(), settings, 0).action == "notify"


def test_fix_requires_every_gate(settings):
    lint = job(cls(Category.LINT_FORMAT, trivial=True, files=["src/demo/util.py"]))
    assert decide([lint], ctx(), settings, 0).action == "fix_pr"

    forked = decide([lint], ctx(fork=True), settings, 0)
    assert forked.action == "notify" and "fork" in forked.reason

    unsure = job(cls(Category.CODE_BREAK, confidence=0.5, trivial=True, files=["a.py"]))
    assert "confidence" in decide([unsure], ctx(), settings, 0).reason

    own = decide([lint], ctx(branch="ci-healer/fix-1-1"), settings, 0)
    assert own.action == "notify" and "own fix branch" in own.reason


def test_history_overrules_a_single_log_guess():
    t = "tests/test_api.py::test_fetch"
    # Claude says flaky, but it already failed on an earlier attempt of the same commit
    c, changed = reconcile(cls(Category.FLAKY_TEST, tests=[t]),
                           {"failing_tests": {t: {"flip_shas": 0}}, "reproduced_on_same_commit": [t]})
    assert changed and c.category == Category.CODE_BREAK

    # Claude says code break, but this test has passed+failed on one commit before
    c, changed = reconcile(cls(Category.CODE_BREAK, tests=[t]),
                           {"failing_tests": {t: {"flip_shas": 2}}, "reproduced_on_same_commit": []})
    assert changed and c.category == Category.FLAKY_TEST and not c.trivial_fix_possible

    # no history → untouched
    c, changed = reconcile(cls(Category.CODE_BREAK), {"failing_tests": {}})
    assert not changed and c.category == Category.CODE_BREAK
