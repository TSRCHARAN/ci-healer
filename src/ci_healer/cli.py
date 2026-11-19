"""Command-line entry points.

    ci-healer serve                       run the webhook receiver
    ci-healer failed OWNER/REPO           list recent failed runs (to pick one to analyse)
    ci-healer analyze OWNER/REPO RUN_ID   analyse one run now, without any webhook
    ci-healer report                      show recent analyses and actions from SQLite
    ci-healer models [--provider P]       list model ids your LLM key can use

Every command that talks to an LLM accepts --provider / --model, overriding LLM_PROVIDER /
LLM_MODEL for that one call — handy for running the same failure past two providers.

`analyze` is the dogfooding workhorse: it fetches a real run through the API and pushes it
through exactly the same pipeline the webhook uses. It is ALWAYS dry-run unless you pass
--live, regardless of .env — acting on a repo should be a deliberate choice each time.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from .config import Settings
from .providers import ConfigError


def _with_llm_overrides(s: Settings, args: argparse.Namespace) -> Settings:
    changes = {}
    if getattr(args, "provider", None):
        # switching provider: don't carry the other provider's model or key along
        changes.update(llm_provider=args.provider, llm_model="", llm_api_key="", llm_base_url="")
    if getattr(args, "model", None):
        changes["llm_model"] = args.model
    return dataclasses.replace(s, **changes)


def _settings_for_cli(repo: str | None, live: bool, args: argparse.Namespace) -> Settings:
    s = _with_llm_overrides(Settings.from_env(), args)
    extra = {repo.lower()} if repo else set()
    return dataclasses.replace(s, allowed_repos=s.allowed_repos | extra, dry_run=not live)


def _add_llm_flags(p: argparse.ArgumentParser) -> None:
    from .providers import PROVIDERS
    p.add_argument("--provider", choices=sorted(PROVIDERS), help="override LLM_PROVIDER")
    p.add_argument("--model", help="override LLM_MODEL")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ci-healer")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)

    sf = sub.add_parser("failed")
    sf.add_argument("repo")
    sf.add_argument("--limit", type=int, default=10)

    sa = sub.add_parser("analyze")
    sa.add_argument("repo")
    sa.add_argument("run_id", type=int)
    sa.add_argument("--attempt", type=int, help="analyse a specific attempt (default: latest)")
    sa.add_argument("--live", action="store_true", help="actually rerun / open PRs (default: dry run)")
    _add_llm_flags(sa)

    sr = sub.add_parser("report")
    sr.add_argument("--limit", type=int, default=20)

    sm = sub.add_parser("models")
    _add_llm_flags(sm)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        from .app import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0

    if args.cmd == "failed":
        from .github import GitHubClient
        gh = GitHubClient(Settings.from_env().github_token)
        for r in gh.list_failed_runs(args.repo, args.limit):
            print(f"{r['id']:>12}  attempt {r['run_attempt']}  {r['conclusion']:<10} "
                  f"{r['head_branch'][:30]:<30} {r['name'][:30]:<30} {r['created_at']}")
        return 0

    if args.cmd == "analyze":
        from .pipeline import Healer
        s = _settings_for_cli(args.repo, args.live, args)
        try:
            healer = Healer.from_settings(s)
        except ConfigError as e:
            print(f"LLM config error: {e}", file=sys.stderr)
            return 2
        print(f"using LLM: {healer.analyzer.backend.label}  dry_run={s.dry_run}", file=sys.stderr)
        run = (healer.gh.get_run_attempt(args.repo, args.run_id, args.attempt) if args.attempt
               else healer.gh.get_run(args.repo, args.run_id))
        payload = {"action": "completed", "workflow_run": run, "repository": run["repository"]}
        result = healer.handle_workflow_run(payload)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.cmd == "report":
        from .db import Database
        db = Database(Settings.from_env().db_path)
        print("== analyses ==")
        for a in db.recent_analyses(args.limit):
            print(f"{a['created_at']}  {a['repo']}#{a['run_id']}.{a['run_attempt']}  "
                  f"{a['category']:<12} {a['confidence'] or 0:.2f} {a['source']:<22} "
                  f"{(a['llm'] or '-'):<32} {a['job_name']}: {(a['summary'] or '')[:80]}")
        print("\n== actions ==")
        for x in db.recent_actions(args.limit):
            flag = " (dry)" if x["dry_run"] else ""
            print(f"{x['created_at']}  {x['repo']}#{x['run_id']}.{x['run_attempt']}  "
                  f"{x['kind']:<12} {x['status']}{flag:<7} {(x['reason'] or '')[:100]}")
        return 0

    if args.cmd == "models":
        from .providers import LLMError, make_backend
        s = _with_llm_overrides(Settings.from_env(), args)
        try:
            backend = make_backend(s.llm_config(need_model=False))
            ids = backend.list_models()
        except (ConfigError, LLMError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"# {s.llm_provider}: {len(ids)} models — set LLM_MODEL to one of these")
        print("\n".join(ids))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
