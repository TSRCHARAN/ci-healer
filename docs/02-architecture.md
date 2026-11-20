# Phase 2 — MVP architecture, component by component

Read [01-landscape.md](01-landscape.md) first; every decision below points back to it.

## The shape: a workflow with two LLM judgment points, not an autonomous agent

```
GitHub ──workflow_run──► app.py ──202──► (background) pipeline.Healer.handle_workflow_run
                         verify HMAC                 │
                         dedupe delivery             ├─ ignore: not completed / repo not allowlisted
                                                     ├─ fix branch?  → record fix verification
                                                     ├─ success, attempt>1 → learn (flaky evidence)
                                                     └─ failure:
                                                          per failed job (max 3):
                                                            github.get_job_log
                                                            logs.extract        → excerpt, tests, files, signature
                                                            rules.structural    → free
                                                            rules.by_signature  → free
                                                            llm.classify        → only if rules didn't claim it
                                                            policy.reconcile    → history overrules a one-log guess
                                                          policy.decide         → rerun | fix_pr | notify
                                                          execute (dry-run aware)
                                                            fix_pr: fetch files → llm.propose_fix
                                                                    → fixer.apply_edits → fixer.validate → PR
```

Why not let Claude drive with tools ("read file", "rerun job", "open PR")? Because the input is
untrusted (§4.4 of the landscape doc) and the actions have side effects. With code owning the
control flow, the worst a prompt-injected log can do is produce a bad *proposal* — which then has
to get through `fixer.validate` and a human PR review. An agentic loop that explores the repo is a
sensible Phase 3 for the *fix* step only, inside a sandbox.

## Components

| File | Role | Key decisions |
|---|---|---|
| `config.py` | Settings from env | Safe defaults: `DRY_RUN=true`, empty allowlist = act on nothing. |
| `app.py` | Webhook receiver | HMAC over raw bytes, constant-time compare; 202 immediately, work in `BackgroundTasks`; idempotency on `X-GitHub-Delivery`. |
| `github.py` | REST client | httpx, 1:1 with documented endpoints; commits via Git Data API (one commit, no clone, never runs repo code). |
| `logs.py` | Log → excerpt | Strip timestamps/ANSI, segment on `##[group]Run`, locate failing step by its `##[error]`, window around error lines + tail, visible truncation markers, normalised failure signature. |
| `rules.py` | Free classifier | Structural signals (`startup_failure`, `timed_out`, "Set up job") and a short regex library. Infra/network verdicts skip the LLM entirely. |
| `llm.py` | What we ask the LLM | Provider-neutral prompts + Pydantic schemas. Effort `medium` to classify, `high` to write a fix. Logs wrapped in `<ci_log>` and declared untrusted in the system prompt. |
| `providers.py` | Whom we ask, and how | `LLM_PROVIDER/MODEL/KEY`. Two adapters: Anthropic native SDK (schema-enforced output, effort, refusal fallbacks) and one OpenAI-compatible HTTP adapter for everyone else. See below. |
| `policy.py` | Decide | Pure functions. Rerun only if *all* failed jobs are transient and the budget allows; fix only if every gate passes; otherwise notify. `reconcile` lets history overrule the LLM. |
| `fixer.py` | Enforce | Exact-match search/replace edits; forbidden paths; size caps; no suppressions; no skip/xfail; no touching assertions in tests. |
| `db.py` | Memory + audit | SQLite, five tables; every decision is recorded, including dry-run plans. |
| `pipeline.py` | Orchestrate | Ties it together; never heals its own fix branches; skips fixes when the branch has moved past the failing commit. |
| `cli.py` | Dogfooding | `analyze` replays any real run through the same pipeline; dry-run unless `--live`. |

## LLM providers: why two adapters cover ten providers

Almost every vendor now serves an OpenAI-compatible `/chat/completions` endpoint, so a
provider is just a row in `providers.PROVIDERS`: protocol, base URL, which env var holds its
key. Anthropic keeps a native adapter because its API gives guarantees the compat protocol
doesn't expose (schema-enforced output via `messages.parse`, `effort`, server-side refusal
fallbacks).

What actually differs between providers is **how strongly the JSON shape is guaranteed**, so
the compat adapter is built for the weakest case:

1. Ask with `response_format: json_schema` (refs inlined — several schema dialects reject `$ref`;
   `strict: true` only where the provider supports it).
2. If the provider rejects that (400/422), fall back to `json_object`, then to prompt-only, and
   remember the downgrade for the rest of the process.
3. The schema is also written into the system prompt, so steps 2–3 still know the shape.
4. Strip `<think>` blocks and ``` fences, extract the JSON object, validate with Pydantic.
5. On a validation error, send one repair turn ("that didn't match: …"); fail on the second miss.

There's no default model for non-Anthropic providers on purpose: model ids churn monthly, and a
stale default fails confusingly. `ci-healer models` lists what your key can use.

Each analysis row stores `llm = provider/model`, which is what lets you compare providers on
*your* failures later (e.g. run `analyze --provider gemini` and `--provider groq` on the same
run and look at `ci-healer report`).

## The data model (what "learning" means here)

- `runs` — one row per (run, attempt). Attempts are the experiment log.
- `analyses` — one per failed job: category, confidence, source (`rule:*`, `llm`, `+history`),
  evidence, signature, token usage.
- `actions` — everything the bot did or *would* have done: `rerun`, `fix_pr`, `notify`,
  `learned`, `fix_verified`.
- `test_outcomes` — per-test failed/passed per attempt. A test with both on one SHA has
  "flipped": that's the flakiness ground truth (`flip_shas`).
- `deliveries` — webhook GUIDs for idempotency.

The loop: a suspected flake is rerun → attempt 2's result is a label → next time the same test
fails on a different commit, `reconcile` uses that label even if Claude, seeing one log, says
"code break". Conversely, a "flaky" verdict for a test that already failed on an earlier attempt
of the same commit is downgraded to a real break and escalated, not retried forever.

## Trust model

1. **Allowlist** — only repos in `CI_HEALER_ALLOWED_REPOS`.
2. **Shadow mode** — `DRY_RUN=true` records plans (including the exact diff it would PR).
3. **Retry budget** — at most `CI_HEALER_MAX_RERUNS` extra attempts per run.
4. **Fix gates** — category ∈ {lint_format, code_break}, `trivial_fix_possible`, confidence ≥ 0.8,
   not a fork, branch not moved, then all of `fixer.validate`.
5. **PR, never push; never merge** — CI on the PR is the verifier; `fix_verified` records the result
   so you can compute fix precision over time.
6. **Least-privilege token** — a fine-grained PAT *without* the Workflows permission means GitHub
   itself refuses workflow edits, as a second wall behind `fixer.FORBIDDEN_PATHS`.

## Known limitations (deliberate, for the MVP)

- `BackgroundTasks` loses in-flight work on crash; no reconciliation poll yet.
- PAT auth; a real deployment would be a GitHub App (per-repo installation tokens, its own identity).
- Test identity is scraped from logs (pytest/go/jest/maven); JUnit XML artifacts would be better.
- Formatting fixes are written by Claude; for formatters, *running the formatter* in a sandbox is
  strictly better (deterministic). The guardrails make a wrong attempt cheap, not impossible.
- Reruns are immediate; infra failures would benefit from backoff.
- No quarantine: a confirmed flaky test is retried and recorded, not skipped.
- One fix PR per run (first eligible job).

## Dogfooding log

**2026-11-19 — a private monorepo (Flutter app + FastAPI backend), Gemini, dry run.** A real
failed run with two failed jobs.

| Job | Verdict | Source | Correct? |
|---|---|---|---|
| `app` — APK 54 MB vs 25 MB budget | `code_break` 0.95, no fix | Gemini Flash (1.4k in / 0.2k out tokens) | Yes — a real budget breach, not auto-fixable |
| `backend` — 3 ruff errors | `lint_format`, fix attempted → **model declined** | rule `ruff-check` (no LLM spend to classify) | Yes — C408 and RUF100 are trivial, but BLE001 (blind `except Exception`) has no safe mechanical fix without a suppression, which the rules forbid. A partial fix would leave CI red, so declining was right. |

Bugs that only real logs exposed (all fixed, with regression tests):
1. Post-job cleanup (git cleanup, service-container teardown) has no step marker, so it was
   glued onto the failing step and filled the excerpt's tail. → new segment at `Post job cleanup.`
2. GitHub's `##[start-action …]` / `##[end-action …]` markers leaked into excerpts. → skipped.
3. `Node.js` (from a deprecation warning) was treated as a file. → warnings and product names ignored.
4. A shell `test … ` that just exits 1 has no error-looking line → empty signature. → fingerprint the step's last output lines.
5. Monorepo `working-directory: backend` → ruff prints `app/x.py` for `backend/app/x.py`, so
   no file could be fetched. → resolve against the commit's tree, unambiguous suffix match only.
6. `gemini-pro-latest` returned 429 (no Pro quota on the key's tier). The pipeline degraded as
   designed: job marked `unknown / llm-error`, the rest of the run still processed.
7. **The important one.** `gemini-flash-latest` correctly declined the ruff fix (BLE001 has no
   safe mechanical fix). `gemini-3.5-flash-lite` instead proposed a *partial* fix — 2 of 3
   errors, BLE001 silently skipped, with self-talk in the explanation. It passed every
   guardrail, but CI would have stayed red. Even after the prompt said "all or none", the small
   model still proposed the partial fix — which is exactly why the fix was a **code** check:
   for lint failures every flagged source file must be edited, or the proposal is rejected.
   Safety cannot depend on which model someone configures.

**2026-11-19 — first live end-to-end fix**, same monorepo. Throwaway branches: a clean
base (existing ruff errors fixed by hand, slow app job disabled) and a broken branch with one
seeded unused import (ruff F401), with a PR between them to trigger CI.

1. CI failed on the ruff step.
2. `ci-healer analyze … --live`: the rule classified it (no LLM call); Gemini 3.5 Flash-Lite
   wrote a one-line fix; the monorepo path was resolved from `app/…` to `backend/app/…`; all
   guardrails passed; a fix PR opened against the broken branch.
3. CI on the fix PR passed — ruff, database migrations **and the full test suite** — and
   ci-healer recorded `fix_verified: success`. The throwaway PRs and branches were then removed.

Follow-up from reading the PR: the evidence block quoted only `Found 1 error.` (the line the
rule matched). Rule evidence now quotes the actual error lines.

**2026-11-19 — public demo, first run as a GitHub Action** ([ci-healer-demo](https://github.com/TSRCHARAN/ci-healer-demo)).
Four PRs that break CI on purpose, all handled concurrently by `TSRCHARAN/ci-healer@v1`.

- Lint (F401): rule → fix PR → CI on the fix PR passed → `fix_verified`. The full loop, with no human involved.
- Flaky timing test: Gemini said `flaky_test` (0.95) — but the branch *also* had an over-long
  line (a mistake in the demo itself). The bot's lint fix re-wrapped an `assert` line in a test
  file and the "no rewriting assertions" guardrail rejected it. Conservative but correct: a code
  check can't tell a harmless re-wrap from a changed expectation. With a non-retryable failure
  present, no rerun happened — rerunning can't fix a lint error.
- Real bug / missing secret: Gemini answered **503 "high demand"** and **timed out** while four
  requests landed at once. ci-healer degraded safely (`unknown`, no action), but that's a miss.
  → Patient retries (5 s / 15 s / 30 s, honouring `Retry-After`), a `TransientLLMError` type, and
  an optional `llm-fallback-model` used only for transient failures (never for a bad key).
  The PR comment for an LLM outage now says so, instead of "category 'unknown' is not auto-fixable".

**Round two (v1.0.1).** The fallback model rescued the 503s; the flaky test was rerun as
designed; the missing secret was diagnosed as `config`. New finding: the four healer runs ran
in parallel, each restored the same cached SQLite database and saved its own copy — the newest
won, so attempt 2 of the flaky run couldn't see attempt 1. → v1.0.2 reads the previous attempt's
failures straight from GitHub when local history is missing. Cross-commit flake history still
rides on the cache (best-effort); a shared store is the real fix for a multi-repo deployment.

**Round three (v1.0.3) — the off-by-one.** With the key and overload issues out of the way,
both Gemini models still diagnosed the median bug precisely and declined to fix it. Root cause
was ours, not theirs: the "is a trivial fix possible?" question was asked of the *classifier*,
which sees only the log — and a wrong return value never puts the broken line in the log. Lint
errors do (`file:line`), which is why those got fixed. Fix: for a confident `code_break`,
`source.py` statically resolves the failing test file's imports against the commit tree
(Python; nothing executed), those modules are fetched read-only, and the *fix* step — with the
code in front of it — decides. Same lite model, same failure, same prompt family: with
`shop/stats.py` shown it proposed exactly `s[mid - 1] + s[mid]`. Lesson for the taxonomy doc:
"trivial" is a property of the fix, and you can only judge it once you've seen the code.

**Round four (v1.0.4) — the killed job.** First re-run of #3 on v1.0.3 never reported: Gemini
served 503s and timeouts to both models, and the healer job hit the workflow's
`timeout-minutes: 10` mid-retry. A killed job writes no summary and no comment — from the
outside, ci-healer just went silent. The retry policy was bounded in *attempts* but not in
*time*: 4 attempts × 2-minute read timeouts × 2 models × 2 questions could exceed the job's
whole life. Fix: every LLM question gets a wall-clock budget (`LLM_TIME_BUDGET`, default
180 s) shared by primary and fallback — a retry that can't finish before the deadline isn't
started, each model still gets at least one try, and read timeouts shrink to fit. Rule of
thumb: 2 questions × budget + ~2 min install must fit inside `timeout-minutes`.

**Round five (v1.0.5) — the license.** No code change. Both repos were public without a
license file, which legally means "all rights reserved": people could read the Action but,
strictly, not use it. v1.0.5 adds the MIT license (`LICENSE`, plus `license = "MIT"` in
`pyproject.toml` so the package metadata carries it), and the moving `v1` tag was advanced so
`uses: TSRCHARAN/ci-healer@v1` resolves to licensed code. MIT because it is what users of a
GitHub Action expect: use, modify and redistribute freely, keep the copyright line, no
warranty. Lesson: a project is not "open source" until the license file exists, and the
major tag has to move for the change to reach anyone.

## Measuring it (next step after dogfooding)

`analyses` + `actions` already hold predictions and outcomes. The first metrics worth computing:
- rerun success rate by predicted category (is "flaky" really flaky?),
- fix PR CI pass rate and merge rate (`fix_verified`, then whether you merged it),
- LLM tokens per analysed failure, and the share of failures rules handled for free.
