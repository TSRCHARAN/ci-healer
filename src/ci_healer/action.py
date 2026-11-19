"""GitHub Action entry point: `ci-healer action`.

When ci-healer runs as an Action, GitHub has already done the webhook part for us: a workflow
triggered `on: workflow_run` starts with the event payload written to $GITHUB_EVENT_PATH —
the *same* JSON the webhook receiver gets. So this module is tiny: read the event, run the
existing pipeline, and report back through the three channels an Action has:

- the job summary ($GITHUB_STEP_SUMMARY) — a markdown report on the run's page;
- step outputs ($GITHUB_OUTPUT) — `decision`, `category`, `pr-url` for later steps;
- workflow commands on stdout (`::notice::`, `::error::`) — annotations in the UI.

Design choice: the healer job only fails (red) on misconfiguration. Declining to act is a
normal, successful outcome and must not look like an error.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import Callable

from .config import Settings
from .pipeline import Healer
from .providers import ConfigError


def _append(path_var: str, text: str) -> None:
    path = os.environ.get(path_var)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def summary_markdown(result: dict, settings: Settings) -> str:
    mode = "🟡 dry run (records what it would do)" if settings.dry_run else "🟢 live"
    status = result.get("status")
    lines = ["## 🩺 ci-healer", ""]
    if status == "analyzed":
        lines += [f"**Run:** {result['run']} (attempt {result['attempt']}) · **Mode:** {mode}", "",
                  "| Job | Failed step | Category | Confidence | Decided by | Summary |",
                  "|---|---|---|---|---|---|"]
        for j in result["jobs"]:
            who = j["llm"] or j["source"]
            summ = (j["summary"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {j['job']} | {j['step']} | `{j['category']}` | {j['confidence']:.2f} "
                         f"| {who} | {summ} |")
        lines += ["", f"**Decision:** `{result['decision']}` — {result['reason']}", ""]
        out = result.get("outcome") or {}
        if out.get("url"):
            lines.append(f"**Fix PR:** {out['url']}")
        else:
            lines.append("**Outcome:** " + "; ".join(f"{k}: {v}" for k, v in out.items()
                                                       if k != "diff"))
        if out.get("diff"):
            lines += ["", "<details><summary>Proposed diff</summary>", "", "```diff",
                      out["diff"].replace("```", "ʼʼʼ"), "```", "</details>"]
    elif status == "learned":
        tests = result.get("tests_marked_flaky_evidence") or []
        lines.append("A rerun of the same commit **passed**, so the earlier failure was transient. "
                     + (f"Recorded flaky evidence for: {', '.join(f'`{t}`' for t in tests)}"
                        if tests else "No individual tests were identified."))
    elif status == "fix_verification":
        icon = "✅" if result.get("conclusion") == "success" else "❌"
        lines.append(f"{icon} CI on fix branch `{result['branch']}` concluded "
                     f"**{result.get('conclusion')}**.")
    else:
        lines.append(f"Nothing to do: {result.get('reason') or status}.")
    return "\n".join(lines) + "\n"


def run_action(healer_factory: Callable[[Settings], Healer] = Healer.from_settings) -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name != "workflow_run":
        print(f"::warning::ci-healer must be triggered by `on: workflow_run` (got '{event_name}').")
        return 0
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        payload = json.load(f)

    repo = payload["repository"]["full_name"]
    # Running inside the repo's own workflow IS the opt-in, so the allowlist is just this repo.
    settings = dataclasses.replace(Settings.from_env(), allowed_repos=frozenset({repo.lower()}))
    try:
        healer = healer_factory(settings)
    except ConfigError as e:
        print(f"::error title=ci-healer configuration::{e}")
        return 1

    result = healer.handle_workflow_run(payload)
    print(json.dumps(result, indent=2, default=str))
    _append("GITHUB_STEP_SUMMARY", summary_markdown(result, settings))

    outcome = result.get("outcome") or {}
    category = (result.get("jobs") or [{}])[0].get("category", "")
    _append("GITHUB_OUTPUT", f"decision={result.get('decision', result.get('status', ''))}\n"
                             f"category={category}\npr-url={outcome.get('url', '')}\n")
    if result.get("decision"):
        print(f"::notice title=ci-healer::{result['decision']} — {result.get('reason', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(run_action())
