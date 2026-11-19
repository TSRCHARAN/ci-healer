from ci_healer import logs, rules
from ci_healer.models import Category

from conftest import FLAKY_LOG, INFRA_LOG, RUFF_LOG, gh_log


def test_clean_line_strips_timestamp_and_ansi():
    assert logs.clean_line("2026-11-19T10:00:00.1234567Z \x1b[36;1mpytest -q\x1b[0m") == "pytest -q"


def test_extract_finds_failing_step_tests_and_files():
    ex = logs.extract(FLAKY_LOG, repo_name="demo")
    assert ex.failed_step == "pytest -q"
    assert ex.failing_tests == ["tests/test_api.py::test_fetch_with_timeout"]
    assert "tests/test_api.py" in ex.file_refs            # runner prefix stripped
    assert "deadline exceeded" in ex.text
    assert ex.signature


def test_signature_ignores_volatile_details_but_not_the_error():
    a = logs.failure_signature(["E TimeoutError: deadline exceeded after 2.013s on port 51234"])
    b = logs.failure_signature(["E TimeoutError: deadline exceeded after 1.870s on port 40021"])
    c = logs.failure_signature(["E KeyError: 'user_id'"])
    assert a == b != c


def test_file_refs_keep_dot_directories_and_skip_site_packages():
    ex = logs.extract(gh_log(
        "##[group]Run x", "##[endgroup]",
        "  File \"/opt/hostedtoolcache/Python/3.12/lib/python3.12/site-packages/foo.py\", line 3",
        "error in ./.github/workflows/ci.yml:12",
        "##[error]Process completed with exit code 1."))
    assert ".github/workflows/ci.yml" in ex.file_refs     # NOT "github/workflows/ci.yml"
    assert not any("site-packages" in f for f in ex.file_refs)


def test_real_world_layout_post_cleanup_action_markers_and_silent_failures():
    """Regression from dogfooding: post-job cleanup used to swallow the excerpt tail, composite
    action markers leaked in, 'Node.js' became a file, and an `exit 1` with no error-looking
    output got no signature."""
    raw = gh_log(
        "##[group]Run actions/checkout@v4", "##[endgroup]",
        "##[start-action display=Cache Flutter;id=__x.cache]",
        "##[end-action id=__x.cache;outcome=success;conclusion=success;duration_ms=409]",
        "##[group]Run size=$(stat -c%s build/app.apk)", "test \"$size\" -le 26214400", "##[endgroup]",
        "APK size: 54 MB",
        "##[error]Process completed with exit code 1.",
        "Post job cleanup.",
        "[command]/usr/bin/git config --local --unset-all http.https://github.com/.extraheader",
        "Stop and remove container: abc_postgres16alpine",
        "Cleaning up orphan processes",
        "##[warning]Node.js 20 is deprecated. The following actions target Node.js 20")
    ex = logs.extract(raw, "demo")
    assert ex.failed_step.startswith("size=")
    assert "APK size: 54 MB" in ex.text
    assert "Stop and remove container" not in ex.text and "start-action" not in ex.text
    assert "Node.js" not in ex.file_refs
    assert ex.signature                                    # fingerprinted from the step's output


def test_huge_logs_are_cut_visibly_and_keep_the_error():
    noise = [f"compiling unit {i}" for i in range(5000)]
    ex = logs.extract(gh_log("##[group]Run build", "##[endgroup]", *noise,
                             "fatal: something exploded", "##[error]Process completed with exit code 2."),
                      max_lines=100)
    assert ex.total_lines > 5000
    assert ex.kept_lines <= 101
    assert "lines omitted" in ex.text and "something exploded" in ex.text


def test_rules_catch_infra_and_lint_but_leave_test_failures_to_the_llm():
    infra = rules.classify_by_rules(logs.extract(INFRA_LOG))
    assert infra and infra[0].category == Category.INFRA and infra[1] == "runner-shutdown"

    lint = rules.classify_by_rules(logs.extract(RUFF_LOG))
    assert lint and lint[0].category == Category.LINT_FORMAT
    assert lint[0].trivial_fix_possible and "src/demo/util.py" in lint[0].implicated_files

    assert rules.classify_by_rules(logs.extract(FLAKY_LOG)) is None


def test_structural_rules():
    assert rules.classify_structural("startup_failure", "").category == Category.CONFIG
    assert rules.classify_structural("timed_out", "Run tests").category == Category.INFRA
    assert rules.classify_structural("failure", "Set up job").category == Category.INFRA
    assert rules.classify_structural("failure", "Run tests") is None
