"""Deterministic first-pass classifier: structure + known signatures.

Why rules before the LLM?
- Free and instant. Most infra failures are a handful of well-known messages.
- High precision and *explainable*: the evidence is the exact line that matched.
- Infra/network verdicts lead to a retry, and a retry doesn't need a root-cause essay — so for
  these we skip the LLM call entirely.

Rules only *claim* a failure when they're confident. Anything else returns None and goes to
Claude. Keep this list short and boring; add a rule only after you've seen the message in the
wild (the `analyses` table tells you which signatures recur).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .logs import LogExcerpt
from .models import Category, Classification


@dataclass(frozen=True)
class Rule:
    name: str
    category: Category
    pattern: re.Pattern
    confidence: float


def _r(name: str, cat: Category, pattern: str, conf: float = 0.9) -> Rule:
    return Rule(name, cat, re.compile(pattern, re.IGNORECASE), conf)


RULES: list[Rule] = [
    # --- runner / infrastructure ---
    _r("runner-shutdown", Category.INFRA, r"runner has received a shutdown signal"),
    _r("runner-lost", Category.INFRA, r"lost communication with the server"),
    _r("hosted-runner-error", Category.INFRA, r"hosted runner encountered an error"),
    _r("disk-full", Category.INFRA, r"no space left on device"),
    _r("oom-kill", Category.INFRA, r"exit code 137|out of memory|Killed\s*$", 0.8),
    _r("job-timeout", Category.INFRA, r"has exceeded the maximum execution time", 0.85),
    # --- network / external services ---
    _r("dns", Category.NETWORK, r"could not resolve host|temporary failure in name resolution|getaddrinfo (?:EAI_AGAIN|ENOTFOUND)"),
    _r("conn-reset", Category.NETWORK, r"ECONNRESET|ETIMEDOUT|connection reset by peer"),
    _r("docker-ratelimit", Category.NETWORK, r"toomanyrequests|pull rate limit"),
    _r("registry-5xx", Category.NETWORK, r"(?:registry|npm|pypi|index).*\b(?:502|503|504)\b|\b(?:502 Bad Gateway|503 Service Unavailable)\b", 0.8),
    _r("pip-timeout", Category.NETWORK, r"ReadTimeoutError|Read timed out", 0.8),
    # --- lint / format (deterministic; the fixer handles these) ---
    _r("black-ruff-format", Category.LINT_FORMAT, r"would reformat|files? would be reformatted"),
    _r("ruff-check", Category.LINT_FORMAT, r"Found \d+ errors?\.|\[\*\] \d+ fixable", 0.85),
    _r("prettier", Category.LINT_FORMAT, r"Code style issues found|Run Prettier", 0.85),
    _r("eslint", Category.LINT_FORMAT, r"✖ \d+ problems? \(\d+ errors?", 0.85),
    # --- config ---
    _r("missing-secret", Category.CONFIG, r"Input required and not supplied|secret .* not (?:set|found)", 0.8),
    _r("token-permission", Category.CONFIG, r"Resource not accessible by integration", 0.85),
]


def classify_structural(conclusion: str, failed_step: str) -> Classification | None:
    """Signals that don't even need the log text."""
    if conclusion == "startup_failure":
        return _mk(Category.CONFIG, 0.95, "Workflow failed to start (invalid workflow file or config).",
                   [f"conclusion={conclusion}"])
    if conclusion == "timed_out":
        return _mk(Category.INFRA, 0.75, "Job hit its timeout.", [f"conclusion={conclusion}"])
    if failed_step == "Set up job":
        return _mk(Category.INFRA, 0.8, "Failure before any user step ran (runner setup).",
                   [f"failed_step={failed_step}"])
    return None


def classify_by_rules(excerpt: LogExcerpt, annotations: list[dict] | None = None) -> tuple[Classification, str] | None:
    """Return (classification, rule_name) for the first matching rule, else None."""
    haystack = excerpt.error_lines + excerpt.text.splitlines()[-80:]
    haystack += [a.get("message", "") for a in annotations or []]
    for rule in RULES:
        for line in haystack:
            if rule.pattern.search(line):
                fixable = rule.category == Category.LINT_FORMAT
                # Quote the actual errors, not just the line the rule matched ("Found 1 error."
                # tells a reviewer nothing; "F401 `json` imported but unused" does).
                evidence = [l.strip()[:300] for l in excerpt.error_lines
                            if "Process completed with exit code" not in l][:4]
                if line.strip()[:300] not in evidence:
                    evidence.append(line.strip()[:300])
                return _mk(rule.category, rule.confidence,
                           f"Matched known signature '{rule.name}'.", evidence,
                           failing_tests=excerpt.failing_tests,
                           implicated_files=excerpt.file_refs[:5] if fixable else [],
                           trivial=fixable), rule.name
    return None


def _mk(cat: Category, conf: float, summary: str, evidence: list[str], *,
        failing_tests: list[str] | None = None, implicated_files: list[str] | None = None,
        trivial: bool = False) -> Classification:
    return Classification(
        category=cat, confidence=conf, summary=summary, evidence=evidence,
        failing_tests=failing_tests or [], implicated_files=implicated_files or [],
        trivial_fix_possible=trivial,
    )
