"""The GitHub Action entry point: event file in → pipeline → job summary + step outputs."""

import json

from ci_healer.action import run_action
from ci_healer.models import Category

from conftest import FLAKY_LOG, cls, payload


def make_job(job_id):
    return {"id": job_id, "name": "tests", "conclusion": "failure",
            "steps": [{"name": "Run pytest", "conclusion": "failure"}]}


def setup_env(monkeypatch, tmp_path, event: dict, event_name="workflow_run"):
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))


def test_action_runs_pipeline_and_reports(world, monkeypatch, tmp_path):
    healer, gh, an = world
    gh.jobs[(100, 1)] = [make_job(1)]
    gh.logs[1] = FLAKY_LOG
    an.classifications.append(cls(Category.FLAKY_TEST, 0.7, tests=["t::x"]))
    event = payload()
    event["repository"]["full_name"] = "someone/other-repo"   # not in .env allowlist — Action allows its own repo
    setup_env(monkeypatch, tmp_path, event)

    def factory(settings):
        assert settings.allowed_repos == {"someone/other-repo"}
        healer.settings = settings.__class__(**{**healer.settings.__dict__,
                                                "allowed_repos": settings.allowed_repos})
        return healer

    assert run_action(factory) == 0
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "## 🩺 ci-healer" in summary and "`flaky_test`" in summary and "`rerun`" in summary
    out = (tmp_path / "out.txt").read_text()
    assert "decision=rerun" in out and "category=flaky_test" in out
    assert gh.reruns == [100]


def test_action_ignores_wrong_trigger(monkeypatch, tmp_path):
    setup_env(monkeypatch, tmp_path, {}, event_name="push")
    assert run_action(lambda s: None) == 0
