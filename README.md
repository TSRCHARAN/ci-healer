# ci-healer 🩺

**Self-healing CI for GitHub Actions.** When a workflow run fails, ci-healer reads the logs,
works out *what kind* of failure it is, and does the right thing:

| Failure | What ci-healer does |
|---|---|
| Infra / network blip (runner died, registry 503, DNS…) | Re-runs the failed jobs once |
| Flaky test | Re-runs once, and **remembers** — if the rerun of the same commit passes, that test is recorded as flaky and recognised next time |
| Lint / format / trivial break | Opens a small **fix PR** — only if it passes every guardrail. For a failing test it first reads (never runs) the modules the test imports, so an off-by-one in `shop/stats.py` is found from `tests/test_stats.py` |
| Real bug, config problem, anything risky | Explains the diagnosis (job summary / PR comment) and leaves it to you |

Classification is cheap-first: known error signatures are matched by rules (free), and only the
rest goes to an LLM — Anthropic, Gemini, Groq, OpenAI, OpenRouter, Mistral, DeepSeek, Together,
Ollama, or any OpenAI-compatible endpoint.

**See it live:** [TSRCHARAN/ci-healer-demo](https://github.com/TSRCHARAN/ci-healer-demo) has
branches that break on purpose and the PRs ci-healer opened for them.

## Use it as a GitHub Action

1. **Add your LLM key** as a repository secret named `LLM_API_KEY`
   (Settings → Secrets and variables → Actions).
2. **Let Actions open PRs:** Settings → Actions → General → Workflow permissions →
   tick *Allow GitHub Actions to create and approve pull requests*.
3. **Add the workflow** — copy [examples/ci-healer.yml](examples/ci-healer.yml) to
   `.github/workflows/ci-healer.yml` and set `workflows: ["<your CI workflow name>"]`.

It starts in **dry-run**: every failed run gets a ci-healer report in its job summary, and
nothing is changed. Set `dry-run: "false"` when the verdicts look right.

### Inputs

| Input | Default | |
|---|---|---|
| `llm-provider` | `anthropic` | see list above |
| `llm-model` | *(anthropic: `claude-opus-5-5`)* | required for other providers |
| `llm-api-key` | — | pass from a secret |
| `llm-fallback-model` | — | second model (same provider) used when the main one is overloaded |
| `github-token` | `github.token` | see note below |
| `dry-run` | `true` | `false` to actually rerun / open PRs |
| `comment-on-prs` | `false` | post the diagnosis on the PR when no action is taken |
| `max-reruns` | `1` | extra attempts per run |
| `autofix` | `true` | allow fix PRs at all |
| `investigate` | `true` | for failing tests, read the imported source (Python) before judging the fix |

Outputs: `decision`, `category`, `pr-url`.

**Token note:** GitHub deliberately doesn't trigger workflows from PRs created with the default
`GITHUB_TOKEN`, so fix PRs opened that way won't get CI. To have CI verify fix PRs, store a
fine-grained PAT (Contents, Pull requests, Actions: read & write — *not* Workflows) as
`CI_HEALER_TOKEN`; the example workflow uses it when present.

## Safety model

- **Guardrails are code, not prompts.** Fix PRs are rejected if they touch workflows, lockfiles
  or secrets; add `noqa` / `eslint-disable`-style suppressions; skip tests or edit assertions;
  exceed 3 files / 40 lines; or fix only *some* of the reported lint errors.
- **The LLM has no tools.** It returns structured data; ci-healer validates and applies it.
  Logs are treated as untrusted input (prompt-injection aware).
- **Never on forks, never on its own fix branches, never auto-merged.** CI on the fix PR is the
  verifier.
- **One retry budget per run**; a "flaky" test that fails again on the same commit is escalated,
  not retried forever.

## How it works / why

- [docs/01-landscape.md](docs/01-landscape.md) — how CI exposes failures, a failure taxonomy,
  who else does this (Trunk, BuildPulse, Mergify, Nx, Copilot…) and the hard parts.
- [docs/02-architecture.md](docs/02-architecture.md) — every component and decision, plus a
  dogfooding log of what real logs broke.

## Run it locally / as a server

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
Copy-Item .env.example .env        # LLM_PROVIDER / LLM_MODEL / LLM_API_KEY, GITHUB_TOKEN, allowlist
```

```powershell
.\.venv\Scripts\ci-healer.exe models                          # model ids your key can use
.\.venv\Scripts\ci-healer.exe failed owner/repo               # recent failed runs
.\.venv\Scripts\ci-healer.exe analyze owner/repo RUN_ID       # dry run; add --live to act
.\.venv\Scripts\ci-healer.exe report                          # what it decided and why
.\.venv\Scripts\ci-healer.exe serve                           # webhook server (needs .[server])
```

`analyze` accepts `--provider` / `--model` to compare LLMs on the same failure.
