# ci-healer

A self-healing CI agent for GitHub Actions. When a workflow run fails it reads the logs,
classifies the failure (flaky test / infra / network / lint / real code break / …), reruns
transient failures within a budget, learns from the rerun's result, and opens a guarded fix PR
for trivial breaks. Everything it decides is recorded in SQLite; it starts in shadow (dry-run) mode.

- **Learn the space:** [docs/01-landscape.md](docs/01-landscape.md)
- **How the MVP works and why:** [docs/02-architecture.md](docs/02-architecture.md)

## Setup (Windows / PowerShell)

Always run commands from inside this folder.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
Copy-Item .env.example .env   # then fill in LLM_*, GITHUB_TOKEN, allowlist
```

## Choosing an LLM

Three variables: `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` (plus `LLM_BASE_URL` for custom
endpoints). Supported: `anthropic` (native SDK), and over the OpenAI-compatible protocol
`gemini`, `groq`, `openai`, `openrouter`, `mistral`, `deepseek`, `together`, `ollama`,
`openai_compatible`.

```powershell
.\.venv\Scripts\ci-healer.exe models                      # ids your key can use
.\.venv\Scripts\ci-healer.exe analyze owner/repo RUN_ID --provider groq --model <id>
```

`--provider/--model` override `.env` for one call, so you can run the same failure past two
providers and compare them in `ci-healer report` (each analysis records `provider/model`).

## Dogfooding, step by step

1. **Pick a repo** and put it in `CI_HEALER_ALLOWED_REPOS`; set your `LLM_*` variables. Create a fine-grained PAT for just
   that repo (Actions RW, Contents RW, Pull requests RW — *not* Workflows).
2. **Replay real failures, dry-run** (no webhook needed):
   ```powershell
   .\.venv\Scripts\ci-healer.exe failed owner/repo
   .\.venv\Scripts\ci-healer.exe analyze owner/repo <RUN_ID>
   .\.venv\Scripts\ci-healer.exe report
   ```
3. **Go live on webhooks** once the dry-run verdicts look right:
   ```powershell
   .\.venv\Scripts\ci-healer.exe serve
   gh extension install cli/gh-webhook
   gh webhook forward --repo=owner/repo --events=workflow_run --url=http://localhost:8000/webhook --secret=<GITHUB_WEBHOOK_SECRET>
   ```
   (Or use a smee.io channel.) Set `CI_HEALER_DRY_RUN=false` only when you're ready for it to act.
4. **Seed failures on a scratch branch** to exercise each path: an unused import (lint → fix PR),
   a test that fails randomly (flaky → rerun → learn), `exit 137` in a step (infra → rerun).

## Endpoints

| | |
|---|---|
| `POST /webhook` | GitHub webhook (`workflow_run`) |
| `GET /healthz` | mode + allowlist |
| `GET /analyses`, `GET /actions` | recent decisions (JSON) |
