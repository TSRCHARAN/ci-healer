"""End-to-end journeys through the real FastAPI app, real SQLite and the real pipeline, with
GitHub and Claude faked. Each test reads like a story of what the bot does over time."""

import dataclasses
import json

from fastapi.testclient import TestClient

from ci_healer.app import create_app
from ci_healer.models import Category, FileEdit, FixProposal

from conftest import (FLAKY_LOG, INFRA_LOG, REPO, RUFF_LOG, UTIL_PY, cls, payload, post,
                      remove_os_import)


def make_job(job_id, step="Run tests"):
    return {"id": job_id, "name": f"job-{job_id}", "conclusion": "failure",
            "steps": [{"name": "Set up job", "conclusion": "success"},
                      {"name": step, "conclusion": "failure"}]}


def actions(healer):
    return healer.db.recent_actions(100)


# ------------------------------------------------------------------ receiver basics ----
def test_receiver_auth_ping_and_idempotency(world):
    healer, gh, an = world
    client = TestClient(create_app(healer))

    bad = client.post("/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=nope"})
    assert bad.status_code == 401

    assert post(client, {"zen": "hi"}, "d0", event="ping").json() == {"pong": True}

    gh.jobs[(100, 1)] = [make_job(1, "Build")]
    gh.logs[1] = INFRA_LOG
    assert post(client, payload(), "d1").status_code == 202
    assert post(client, payload(), "d1").json() == {"status": "duplicate"}   # redelivery
    assert gh.reruns == [100]                                               # acted once


def test_repos_outside_the_allowlist_are_ignored(world):
    healer, gh, an = world
    p = payload()
    p["repository"]["full_name"] = "someone/else"
    assert healer.handle_workflow_run(p)["status"] == "ignored"


# ------------------------------------------------------------- the flaky-test story ----
def test_flaky_test_is_retried_learned_and_recognised_next_time(world):
    healer, gh, an = world
    test_id = "tests/test_api.py::test_fetch_with_timeout"

    # 1) attempt 1 fails; Claude suspects flakiness (timeout symptoms) → rerun
    gh.jobs[(100, 1)] = [make_job(1)]
    gh.logs[1] = FLAKY_LOG
    an.classifications.append(cls(Category.FLAKY_TEST, 0.7, tests=[test_id]))
    r = healer.handle_workflow_run(payload(run_id=100, attempt=1))
    assert r["decision"] == "rerun" and gh.reruns == [100]
    assert "deadline exceeded" in an.classify_calls[0]["excerpt"].text
    assert r["jobs"][0]["llm"] == "fake/model-1"                       # which model decided
    assert healer.db.recent_analyses(1)[0]["llm"] == "fake/model-1"

    # 2) attempt 2 of the same commit passes → recorded as a flip
    r = healer.handle_workflow_run(payload(run_id=100, attempt=2, conclusion="success"))
    assert r["tests_marked_flaky_evidence"] == [test_id]
    assert healer.db.test_history(REPO, test_id)["flip_shas"] == 1

    # 3) days later, a different commit hits the same test; Claude (seeing one log) calls it a
    #    code break — history overrules it and the bot retries instead of blaming the PR
    gh.jobs[(200, 1)] = [make_job(5)]
    gh.logs[5] = FLAKY_LOG
    an.classifications.append(cls(Category.CODE_BREAK, 0.6, tests=[test_id]))
    r = healer.handle_workflow_run(payload(run_id=200, attempt=1, sha="b" * 40))
    assert r["jobs"][0]["category"] == "flaky_test" and r["jobs"][0]["source"] == "llm+history"
    assert r["decision"] == "rerun" and gh.reruns == [100, 200]
    # the history Claude was shown included the earlier flip
    assert an.classify_calls[1]["history"]["failing_tests"][test_id]["flip_shas"] == 1


def test_failure_that_reproduces_on_rerun_is_escalated_not_retried_forever(world):
    healer, gh, an = world
    test_id = "tests/test_api.py::test_fetch_with_timeout"
    for attempt, job_id in ((1, 1), (2, 2)):
        gh.jobs[(100, attempt)] = [make_job(job_id)]
        gh.logs[job_id] = FLAKY_LOG
        an.classifications.append(cls(Category.FLAKY_TEST, 0.7, tests=[test_id]))
    healer.handle_workflow_run(payload(attempt=1))
    r = healer.handle_workflow_run(payload(attempt=2))
    assert r["jobs"][0]["category"] == "code_break"         # reproduced on same SHA
    assert r["decision"] == "notify" and gh.reruns == [100]


def test_rerun_learning_survives_lost_local_history(world, tmp_path):
    """Live-demo regression: parallel Action runs overwrote each other's cached DB, so attempt 2
    couldn't see attempt 1. The earlier attempt is now read back from GitHub instead."""
    from ci_healer.db import Database
    from ci_healer.pipeline import Healer
    healer, gh, an = world
    test_id = "tests/test_api.py::test_fetch_with_timeout"
    gh.jobs[(100, 1)] = [make_job(1)]
    gh.logs[1] = FLAKY_LOG
    an.classifications.append(cls(Category.FLAKY_TEST, 0.7, tests=[test_id]))
    healer.handle_workflow_run(payload(run_id=100, attempt=1))

    dbs = iter(range(10))

    def fresh():   # same GitHub, empty database — like a runner that got a stale cache
        return Healer(healer.settings, Database(str(tmp_path / f"fresh{next(dbs)}.db")), gh, an)

    r = fresh().handle_workflow_run(payload(run_id=100, attempt=2, conclusion="success"))
    assert r["tests_marked_flaky_evidence"] == [test_id]

    gh.jobs[(100, 2)] = [make_job(2)]
    gh.logs[2] = FLAKY_LOG
    an.classifications.append(cls(Category.FLAKY_TEST, 0.7, tests=[test_id]))
    r = fresh().handle_workflow_run(payload(run_id=100, attempt=2))
    assert r["jobs"][0]["category"] == "code_break"          # reproduced on the same commit


# ------------------------------------------------------------------- the fix story -----
def test_lint_failure_gets_a_guarded_fix_pr_and_its_ci_is_tracked(world):
    healer, gh, an = world
    gh.jobs[(300, 1)] = [make_job(9, "Lint")]
    gh.logs[9] = RUFF_LOG
    gh.files["src/demo/util.py"] = UTIL_PY
    gh.branch_shas["feature/x"] = "a" * 40
    an.proposals.append(remove_os_import())

    r = healer.handle_workflow_run(payload(run_id=300))
    assert r["jobs"][0]["source"] == "rule:ruff-check"
    assert an.classify_calls == []                          # rules handled it — no LLM spend
    assert r["decision"] == "fix_pr" and r["outcome"]["fix_pr"] == "opened"

    branch, files, _ = gh.commits[0]
    assert branch == "ci-healer/fix-300-1"
    assert files == {"src/demo/util.py": "import sys\n\n\ndef argv0() -> str:\n    return sys.argv[0]\n"}
    pr = gh.pulls[0]
    assert pr["base"] == "feature/x" and pr["title"].startswith("[ci-healer]")
    assert "lint_format" in pr["body"] and "-import os" in pr["body"]

    # CI on the fix branch fails → recorded as verification, and NOT healed recursively
    r = healer.handle_workflow_run(payload(run_id=301, branch="ci-healer/fix-300-1"))
    assert r["status"] == "fix_verification"
    kinds = [a["kind"] for a in actions(healer)]
    assert kinds[0] == "fix_verified" and "fix_pr" in kinds


def test_dry_run_records_the_plan_but_touches_nothing(world):
    healer, gh, an = world
    healer.settings = dataclasses.replace(healer.settings, dry_run=True)
    gh.jobs[(300, 1)] = [make_job(9, "Lint")]
    gh.logs[9] = RUFF_LOG
    gh.files["src/demo/util.py"] = UTIL_PY
    an.proposals.append(remove_os_import())

    r = healer.handle_workflow_run(payload(run_id=300))
    assert r["outcome"]["fix_pr"] == "planned (dry run)" and "-import os" in r["outcome"]["diff"]
    assert gh.pulls == [] and gh.branches == [] and gh.reruns == []
    planned = actions(healer)[0]
    assert planned["status"] == "planned" and planned["dry_run"] == 1
    assert json.loads(planned["detail"])["branch"] == "ci-healer/fix-300-1"


def test_a_cheating_fix_is_blocked_by_guardrails(world):
    healer, gh, an = world
    gh.jobs[(300, 1)] = [make_job(9, "Lint")]
    gh.logs[9] = RUFF_LOG
    gh.files["src/demo/util.py"] = UTIL_PY
    an.proposals.append(FixProposal(can_fix=True, explanation="silence it", pr_title="Silence",
                                    edits=[FileEdit(path="src/demo/util.py", old_string="import os\n",
                                                    new_string="import os  # noqa: F401\n")]))
    r = healer.handle_workflow_run(payload(run_id=300))
    assert "guardrail" in r["outcome"]["fix_pr"] and gh.pulls == []


def test_partial_lint_fix_is_rejected(world):
    """Regression from dogfooding: linter flagged two files; the model fixed one."""
    from conftest import gh_log
    healer, gh, an = world
    gh.jobs[(300, 1)] = [make_job(9, "Lint")]
    gh.logs[9] = gh_log("##[group]Run ruff check .", "##[endgroup]",
                        "src/demo/util.py:1:8: F401 [*] `os` imported but unused",
                        "src/demo/run.py:140:20: BLE001 Do not catch blind exception: `Exception`",
                        "Found 2 errors.", "##[error]Process completed with exit code 1.")
    gh.files["src/demo/util.py"] = UTIL_PY
    gh.files["src/demo/run.py"] = "try:\n    go()\nexcept Exception:\n    pass\n"
    an.proposals.append(remove_os_import())                     # only fixes util.py
    r = healer.handle_workflow_run(payload(run_id=300))
    assert "partial fix" in r["outcome"]["fix_pr"] and "src/demo/run.py" in r["outcome"]["fix_pr"]
    assert gh.pulls == []


def test_stale_commit_is_not_fixed(world):
    healer, gh, an = world
    gh.jobs[(300, 1)] = [make_job(9, "Lint")]
    gh.logs[9] = RUFF_LOG
    gh.branch_shas["feature/x"] = "c" * 40                    # branch moved on
    r = healer.handle_workflow_run(payload(run_id=300))
    assert "new commits" in r["outcome"]["fix_pr"] and an.fix_calls == []
