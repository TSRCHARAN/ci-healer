"""Settings, loaded once from environment variables (and a local .env file).

Decision: plain env vars + a frozen dataclass instead of a settings framework. Every knob that
affects *what the bot is allowed to do* lives here, so the safety envelope is readable in one
screen. The defaults are the conservative ones: dry-run on, no repos allowed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Secrets are repr=False so printing/logging Settings never leaks them.
    github_token: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)
    db_path: str = "ci_healer.db"

    # --- LLM (see providers.py) -------------------------------------------------------------
    llm_provider: str = "anthropic"
    llm_model: str = ""                        # empty → provider default (anthropic only)
    llm_api_key: str = field(default="", repr=False)
    llm_base_url: str = ""
    llm_max_tokens: int = 8192
    llm_fallback_model: str = ""               # same provider, used when the primary is overloaded
    llm_time_budget: float = 180.0             # seconds per LLM question, retries + fallback included

    # --- safety envelope -------------------------------------------------------------------
    dry_run: bool = True                       # shadow mode: decide + record, never act
    allowed_repos: frozenset[str] = field(default_factory=frozenset)
    max_reruns_per_run: int = 1                # attempts we'll trigger beyond the first
    autofix_enabled: bool = True
    fix_branch_prefix: str = "ci-healer/fix-"
    max_fix_files: int = 3
    max_fix_changed_lines: int = 40
    min_fix_confidence: float = 0.8
    investigate_code_breaks: bool = True       # read the failing test's imports before deciding
    comment_on_prs: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        repos = os.getenv("CI_HEALER_ALLOWED_REPOS", "")
        return cls(
            github_token=os.getenv("GITHUB_TOKEN", ""),
            webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
            llm_provider=os.getenv("LLM_PROVIDER", "anthropic"),
            llm_model=os.getenv("LLM_MODEL", ""),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL", ""),
            llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
            llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", ""),
            llm_time_budget=float(os.getenv("LLM_TIME_BUDGET", "180")),
            db_path=os.getenv("CI_HEALER_DB", "ci_healer.db"),
            dry_run=_bool("CI_HEALER_DRY_RUN", True),
            allowed_repos=frozenset(r.strip().lower() for r in repos.split(",") if r.strip()),
            max_reruns_per_run=int(os.getenv("CI_HEALER_MAX_RERUNS", "1")),
            autofix_enabled=_bool("CI_HEALER_AUTOFIX", True),
            max_fix_files=int(os.getenv("CI_HEALER_MAX_FIX_FILES", "3")),
            max_fix_changed_lines=int(os.getenv("CI_HEALER_MAX_FIX_LINES", "40")),
            min_fix_confidence=float(os.getenv("CI_HEALER_MIN_FIX_CONFIDENCE", "0.8")),
            investigate_code_breaks=_bool("CI_HEALER_INVESTIGATE", True),
            comment_on_prs=_bool("CI_HEALER_COMMENT_ON_PRS", False),
        )

    def llm_config(self, need_model: bool = True):
        from .providers import resolve
        return resolve(self.llm_provider, self.llm_model, self.llm_api_key, self.llm_base_url,
                       self.llm_max_tokens, need_model=need_model,
                       fallback_model=self.llm_fallback_model, time_budget=self.llm_time_budget)

    def repo_allowed(self, full_name: str) -> bool:
        return full_name.lower() in self.allowed_repos
