# Phase 1 — The self-healing CI landscape

*Written 2026-11-19. Vendor details checked against public pages that day; anything marked
"(unverified)" is from background knowledge and worth re-checking before you rely on it.*

The goal of this document is to give you a mental model sharp enough that every design decision
in Phase 2 feels obvious. It is ordered the way the problem flows: **where failures come from →
what kinds exist → who already attacks this → what is genuinely hard.**

---

## 1. How CI systems expose failures (GitHub Actions)

### 1.1 The object model

```
Repository
 └─ Workflow            (.github/workflows/ci.yml)
     └─ Workflow run    run_id, run_attempt, head_sha, head_branch, event, conclusion
         └─ Job         job_id (== check-run id), runner_name, conclusion
             └─ Step    number, name, conclusion
                 └─ log lines
```

Three facts about this model matter more than everything else:

1. **`run_attempt`.** When you click "Re-run failed jobs", GitHub does *not* create a new run. It
   creates attempt `N+1` of the **same `run_id` on the same `head_sha`**. That makes a rerun a
   controlled experiment: same code, same workflow, different moment. If attempt 1 failed and
   attempt 2 passed, the failure was **not caused by the code**. This is the single most reliable
   flakiness signal that exists, and it's free.
2. **A job *is* a check run.** Actions is built on the Checks API, so `job_id` works with
   `/check-runs/{id}/annotations`. Annotations are the structured version of `##[error]` lines —
   cheap to fetch, already de-noised.
3. **There is no native "test results" API.** Pass/fail per *test* exists only as text inside logs,
   unless the workflow uploads JUnit XML as an artifact or uses a reporter action. This is why
   every flaky-test product asks you to upload JUnit files: without them, test identity has to be
   scraped from log text.

### 1.2 Events (webhooks)

| Event | Fires | Use it for |
|---|---|---|
| `workflow_run` | `requested`, `in_progress`, `completed` — once per run | **Our trigger.** One event per failure unit; carries `conclusion`, `run_attempt`, `head_sha`, `pull_requests[]`, and `head_repository` (fork detection). |
| `workflow_job` | `queued`, `in_progress`, `completed`, `waiting` — once per job | Lower latency (react when the first job fails, not when the whole run ends), includes steps. ~N× the volume. |
| `check_suite` / `check_run` | Any CI that reports via Checks (Actions, CircleCI, Buildkite…) | CI-vendor-agnostic designs. |

Delivery mechanics you have to design around:

- Headers: `X-GitHub-Event` (which event), `X-GitHub-Delivery` (a GUID — **your idempotency key**),
  `X-Hub-Signature-256` (`sha256=` + HMAC-SHA256 of the *raw* body using your webhook secret).
- GitHub waits ~10 seconds for a 2xx. Reading logs and calling an LLM takes longer, so the
  receiver must **acknowledge immediately and process asynchronously**.
- Failed deliveries are **not retried automatically** (you can redeliver from the UI/API). A
  production system pairs webhooks with a periodic reconciliation poll (`GET /actions/runs?status=failure`).
- Locally your laptop has no public URL: use a relay (`smee.io`, or `gh webhook forward` from the
  `cli/gh-webhook` extension) or skip webhooks and poll.

`conclusion` values: `success`, `failure`, `cancelled`, `timed_out`, `startup_failure`,
`action_required`, `neutral`, `skipped`, `stale`. We care about `failure`, `timed_out` (often
infra) and `startup_failure` (the workflow YAML itself is invalid → config). `cancelled` is
usually a newer push superseding the run (`concurrency: cancel-in-progress`) — **not a failure**.

### 1.3 The REST endpoints that matter

| Endpoint | Why |
|---|---|
| `GET /repos/{o}/{r}/actions/runs/{run_id}` | Run metadata (also in the webhook). |
| `GET .../actions/runs/{run_id}/attempts/{n}/jobs` | Jobs + steps + per-step conclusion for a specific attempt → *which step failed*. |
| `GET .../actions/jobs/{job_id}/logs` | 302 redirect to a short-lived URL serving one job's **plain-text** log. |
| `GET .../actions/runs/{run_id}/logs` | Zip of all jobs, one file per step. Heavier; useful for exact step boundaries. |
| `GET .../check-runs/{job_id}/annotations` | Structured error/warning annotations. |
| `POST .../actions/runs/{run_id}/rerun-failed-jobs` | The "retry" primitive. Only reruns failed jobs → cheaper than a full rerun. |
| `POST .../actions/jobs/{job_id}/rerun` | Single-job retry; accepts `enable_debug_logging`. |
| Git Data + Pulls APIs | Create a branch, commit file changes, open a PR — all without cloning. |

Logs are retained 90 days by default (configurable on private repos).

### 1.4 What a raw job log looks like

```
2026-11-19T10:02:11.4410000Z ##[group]Run pytest -q
2026-11-19T10:02:11.4410000Z [36;1mpytest -q[0m
2026-11-19T10:02:11.4420000Z shell: /usr/bin/bash -e {0}
2026-11-19T10:02:11.4420000Z ##[endgroup]
2026-11-19T10:02:14.9010000Z FAILED tests/test_api.py::test_timeout_retry - AssertionError: ...
2026-11-19T10:02:14.9020000Z 1 failed, 41 passed in 3.41s
2026-11-19T10:02:14.9500000Z ##[error]Process completed with exit code 1.
```

- Every line is prefixed with an ISO-8601 timestamp (strip it before hashing or prompting).
- `##[group]Run <command>` / `##[endgroup]` mark step boundaries (imperfectly: composite actions
  and post-steps blur them — cross-check with the jobs API's `steps[]`).
- `##[error]` / `##[warning]` are workflow commands; they also become annotations.
- ANSI colour codes are everywhere; secrets appear masked as `***`.
- Logs can be many MB. The *useful* part is usually the last ~100 lines of the failing step plus
  the neighbourhood of each error line.

---

## 2. A taxonomy of CI failures

### 2.1 The two axes that actually decide the action

Taxonomies are only useful if they map to **actions**. Two questions do most of the work:

|  | **Caused by this change** | **Not caused by this change** |
|---|---|---|
| **Deterministic** (reproduces on rerun) | Real code break → fix it | Main is already broken / dependency drift → fix upstream, don't blame the PR |
| **Non-deterministic** | (rare: a change that *introduced* a race) | Flaky test or infra → retry, record, quarantine |

### 2.2 The working categories

| Category | Typical signals | Right response | Auto-fixable? |
|---|---|---|---|
| **Infra / runner** | `The runner has received a shutdown signal`, `lost communication with the server`, `No space left on device`, exit code 137 (OOM-kill), `timed_out` conclusion, failure in "Set up job" | Retry with backoff; alert if it repeats | Retry only |
| **Network / external dependency** | `ECONNRESET`, `Could not resolve host`, registry `5xx`/`429`, Docker Hub `toomanyrequests`, pip `ReadTimeoutError` | Retry with backoff | Retry only |
| **Flaky test** | Same SHA passes on rerun; test has intermittent history; timing/ordering words (`timeout`, `race`, `deadline`, `connection refused` to a local service) | Retry once, record, quarantine if recurrent, open an issue | Retry; the *real* fix is human |
| **Lint / format / type-check** | `ruff`, `eslint`, `prettier --check`, `black --check`, `mypy`, `tsc` | Fix PR | **Yes — the safest class** |
| **Real code break** | Compile/import/type error, deterministic assertion failure in code touched by the diff, reproduces on rerun | Notify; fix PR only when trivial | Sometimes (typos, missing imports) |
| **Dependency drift** | Unpinned dep released a breaking version; `npm ci` lockfile out of sync; an *old* SHA that used to pass now fails too | Pin / bump PR | Sometimes |
| **Config / workflow** | `startup_failure`, YAML errors, missing secret (fork PRs get no secrets!), `403` on `GITHUB_TOKEN` | Human | No — never let a bot edit workflows |
| **Cancelled** | `cancelled` conclusion | Ignore | — |

### 2.3 How to classify *reliably*

Reliability comes from layering signals by **cost and precision**, cheapest first:

1. **Structure** (free, near-perfect precision): the `conclusion`, *which step* failed (a failure in
   `actions/checkout` is never a code break), exit codes, annotations.
2. **Signature rules** (free, high precision, low recall): a regex library of known infra/lint
   messages. Explainable — you can show the exact line that matched.
3. **History** (cheap, the only ground truth for flakiness): has this test / this error signature
   failed and then passed on the same SHA before? How often does it fail on the default branch
   where the code didn't change?
4. **LLM** (costs money, handles the long tail): reads a *pre-extracted* excerpt, explains the
   root cause, names failing tests and implicated files. Crucially, an LLM looking at one log
   **cannot know** a failure is flaky — it can only notice hints. So an LLM "flaky" verdict is a
   *hypothesis*, and the rerun is the *experiment* that confirms or refutes it.
5. **Calibration loop**: record every prediction and what happened next (did the rerun pass? did the
   fix PR merge?). That turns the system's own actions into labelled data, and gives you a
   per-category precision number — which is what lets you grant it more autonomy later.

A useful trick: a **failure signature** — the error lines with numbers, hex ids, paths, durations
and timestamps normalised away, then hashed. Two failures with the same signature are "the same
failure", which lets you count recurrences across runs without understanding the text.

---

## 3. Who is doing this today

The market splits into three groups that rarely talk to each other.

### 3.1 Flaky-test analytics & quarantine

| Product | What it does well | Limits |
|---|---|---|
| **Trunk Flaky Tests** | Test-level history from JUnit uploads, flake detection, quarantine that keeps a flaky test from blocking merges. | Needs JUnit uploads; detects and quarantines, doesn't fix. |
| **BuildPulse** | Strong detection and impact metrics (disruption per test, builds lost, time lost); exposes the quarantine list via API. | Reported to leave the quarantine *workflow* to you. |
| **Mergify CI Insights** | Splits the problem into prevention / detection / quarantine; detection from default-branch history; pairs with its merge queue. | Value is highest if you also use Mergify's queue. |
| **Datadog Test Optimization** (formerly CI Visibility) | Early Flake Detection (runs *new* tests several times), automatic test retries, flaky test management. (unverified for 2026 specifics) | Requires Datadog instrumentation; priced as part of Datadog. |
| Others | Buildkite Test Engine, Gradle Develocity (test retry + flaky reports), CloudBees Smart Tests (ex-Launchable), Tuist (iOS). | Mostly ecosystem-bound. |

A recurring approach (Tuist's write-up describes it plainly): **flag a test as flaky when it passed
in one run and failed in another for the same commit SHA.** That's the `run_attempt` insight from §1.1.

### 3.2 Retry policy & merge queues

GitHub merge queue, Mergify, Trunk Merge Queue, Aviator; plus blanket-retry primitives (GitLab
`retry: when: runner_system_failure`, community "retry step" actions). These keep `main` green but
retry **blindly** — they don't know *why* something failed, so they mask real intermittent bugs.

### 3.3 AI fixers

| Product | Model of autonomy | Notes |
|---|---|---|
| **Nx Cloud Self-Healing CI** (since June 2025) | Agent reads failing task logs + the Nx project graph, **proposes** a fix; human approves; background verification run; opt-in auto-apply for selected tasks. | Excellent design for trust (propose → verify → apply), but requires an Nx workspace. |
| **GitHub "Fix with Copilot"** (May 2026; Pro/Pro+ in June; Mobile in July) | **Human-triggered** button on a failed run; cloud agent investigates, pushes a fix to the branch, tags you. | CI does not auto-run on the agent's commits until a human approves — a deliberate safety brake. A third-party analysis warns about approving agent edits to `.github/workflows/` unread. |
| **Gitar** | Autonomous fixes for CI failures and review comments. | Marketing material only; hard to verify specifics. |
| **Trunk AI DevOps Agent** (announced July 2025) | Background agent triggered by CI failures/webhooks; root-cause + fix. | Was waitlist at announcement; current availability unverified. |
| GitLab Duo Root Cause Analysis | Explains failed jobs. | Explain-only. (unverified for 2026 specifics) |

### 3.4 Where the gaps are

1. **Decision and action are siloed.** Flake tools have the history but don't act; AI fixers act but
   treat every red build as a code problem. Almost nobody uses *historical flakiness* to decide
   *whether* to spend an LLM call or open a PR. A unified **classify → policy → act → learn** loop
   is the opening.
2. **"Self-healing" is mostly button-pressing.** The big players gate on a human click. That's
   right for code changes, but retries of infra failures don't need a human — and nobody does
   *smart* retries well.
3. **Trust isn't measured publicly.** Few tools show "auto-fix precision by category" — the number
   you'd need to decide how much autonomy to grant.
4. **Ecosystem lock-in.** Nx needs Nx; Datadog needs Datadog; flake tools need JUnit uploads.
5. **Detection still misses a lot.** A 2026 IEEE TSE study, as cited by Harness, found 9.8–16.3% of
   failed pipeline runs involved flaky failures that existing detection mechanisms missed.
   (Secondhand citation — read the paper before quoting it.)

---

## 4. The genuinely hard parts

### 4.1 Flaky detection is statistical, not textual
- Base rates are low: a test that fails 1 in 200 runs needs hundreds of runs to estimate.
- Test identity is slippery: renames, parametrised ids (`test_x[case-3]`), order-dependent tests that
  only fail after some other test runs.
- "Flaky" vs "broken on main" look identical in a single log.
- **A flaky test can be a real bug** (a race condition in production code). Auto-retrying it forever
  hides the bug. Retry is a *diagnostic*, not a cure.

### 4.2 Retry economics
Retries burn CI minutes and wall-clock time, and blanket retries mask signal. Rules of thumb:
retry only failed jobs; cap retries per SHA (1–2); never retry deterministic failures (lint,
compile errors); back off for infra; and **record the outcome** so each retry produces a label.

### 4.3 Safe auto-fix boundaries
The objective "make CI green" is gameable: the easiest fix for a failing test is to delete it.
So the boundary has to be enforced by *code*, not by asking the model nicely:
- an **allowlist of categories** (lint/format, trivial import/typo breaks);
- **size limits** (files touched, lines changed);
- **forbidden paths**: `.github/workflows/` (supply-chain risk — GitHub even requires a separate
  `workflows` token scope), lockfiles, secrets/config, CODEOWNERS;
- **no test weakening**: no deleting assertions, no `skip`/`xfail` insertion;
- **PR, never push**: the bot proposes on a branch; CI on that PR is the verifier; never auto-merge.

### 4.4 Untrusted input → code with write access
CI logs contain attacker-controllable text: test names and output from fork PRs, output from
third-party dependencies. An LLM that reads logs and writes code into your repo is a textbook
prompt-injection target. Mitigations: give the model **no tools** (it returns structured data, our
code validates and applies it); skip fork PRs for auto-fix; least-privilege tokens; human review.

### 4.5 Trust is earned in stages
1. **Shadow mode** — classify and log what it *would* do (our `DRY_RUN=true` default).
2. **Act on reversible, low-risk things** — retries.
3. **Propose** code changes as PRs.
4. **Widen** the allowlist only where measured precision is high.

Every action should carry its evidence (the exact log lines), be attributable (branch prefix,
PR label), be rate-limited, and have a kill switch. One bad autonomous PR costs more trust than ten
good ones earn.

### 4.6 Smaller but real
- **Loops**: the bot must never "heal" its own fix PRs, and must dedupe webhook redeliveries.
- **Context**: a log alone is often not enough — the diff, whether `main` is also red, and history
  all change the answer.
- **Cost**: logs are huge; pre-extract excerpts and run free rules before paying for an LLM call.

---

## 5. What this means for our MVP (bridge to Phase 2)

| Insight | Design decision |
|---|---|
| Reruns are free experiments (§1.1) | Retry flaky/infra once, then learn from attempt N+1's result. |
| Webhooks must be acked in <10 s (§1.2) | FastAPI returns 202 immediately; work runs in a background task. |
| Delivery GUIDs exist (§1.2) | `deliveries` table for idempotency. |
| Cheap signals first (§2.3) | Rules → history → Claude, in that order. |
| An LLM can't *know* flakiness (§2.3) | Feed history into the prompt; treat "flaky" as a hypothesis the rerun tests. |
| "Make CI green" is gameable (§4.3) | Fix guardrails enforced in code: categories, size, forbidden paths, no test weakening, PR-only. |
| Logs are untrusted (§4.4) | Claude gets no tools; structured outputs only; skip forks for fixes. |
| Trust is staged (§4.5) | `DRY_RUN=true` by default; per-action audit log in SQLite. |

---

## Sources

- [Nx — Self-Healing CI docs](https://nx.dev/docs/features/ci-features/self-healing-ci)
- [Nx blog — Nx Self-Healing CI](https://nx.dev/blog/nx-self-healing-ci)
- [Nx blog — What's new in Nx Self-Healing CI](https://nx.dev/blog/whats-new-in-nx-self-healing-ci)
- [Trunk — AI DevOps Agent announcement](https://trunk.io/blog/engineers-shouldn-t-babysit-pipelines-ai-agents-should)
- [GitHub changelog — One-click fixes for failing Actions with Copilot cloud agent (2026-05-18)](https://github.blog/changelog/2026-05-18-one-click-fixes-for-failing-actions-with-copilot-cloud-agent/)
- [GitHub changelog — Fix with Copilot now in Pro, Pro+ and Max (2026-06-04)](https://github.blog/changelog/2026-06-04-fix-with-copilot-for-failing-actions-now-in-pro-pro-and-max/)
- [GitHub changelog — GitHub Mobile: fix failing Actions checks (2026-07-23)](https://github.blog/changelog/2026-07-23-github-mobile-fix-failing-actions-checks-with-copilot-cloud-agent)
- [agentpatterns.ai — One-click CI auto-fix (third-party analysis)](https://agentpatterns.ai/workflows/one-click-ci-auto-fix/)
- [Harness — Flaky test tooling landscape 2026](https://www.harness.io/blog/flaky-test-tooling-landscape-2026)
- [Mergify — How to get rid of flaky tests: 5 tools to know in 2026](https://articles.mergify.com/how-to-get-rid-of-flaky-tests-lethal-tools/)
- [Tuist — Flaky tests (2026-01-27)](https://tuist.dev/blog/2026/01/27/flaky-tests)
- [Gitar overview (hashnode)](https://hashnode.com/posts/ci-cd-security-automation/68f4e1000ad6ad5988689bff)
