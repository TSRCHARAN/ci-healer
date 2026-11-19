"""Apply and police Claude's proposed edits before anything touches GitHub.

The core idea: **the safety boundary is code, not a prompt.** The system prompt asks Claude to
behave; this module *enforces* it, so a confused model or a prompt-injected log still can't get
a forbidden change into a PR.

Gates, in order:
1. Edits may only target files we fetched and showed to the model.
2. Each old_string must match exactly once (no fuzzy patching, no silent misapplication).
3. Forbidden paths: workflows, lockfiles, env/secret files, CODEOWNERS, key material.
4. Size: at most N files and M changed lines.
5. No suppressions (noqa, type: ignore, eslint-disable, ...) — silencing a check isn't fixing it.
6. No test weakening: no skip/xfail/.only markers, and no removed/modified assertions in tests.
   ("Make CI green" is gameable — the easiest fix for a failing test is deleting it.)
"""

from __future__ import annotations

import difflib
import re

from .config import Settings
from .models import FileEdit

FORBIDDEN_PATHS = [re.compile(p) for p in (
    r"^\.github/",
    r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|"
    r"Pipfile\.lock|Cargo\.lock|go\.sum|composer\.lock|Gemfile\.lock)$",
    r"(^|/)\.env",
    r"(^|/)CODEOWNERS$",
    r"(?i)(secret|credential)",
    r"\.(pem|key|p12|pfx)$",
    r"(^|/)(Dockerfile|\.gitlab-ci\.yml|Jenkinsfile|\.circleci/)",
)]
SUPPRESSIONS = re.compile(
    r"#\s*noqa|type:\s*ignore|eslint-disable|@ts-ignore|@ts-nocheck|@ts-expect-error|"
    r"pylint:\s*disable|//\s*nolint|#\s*pragma:\s*no cover", re.IGNORECASE)
TEST_DISABLERS = re.compile(
    r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|@unittest\.skip|\.skip\(|\bxit\(|\bxdescribe\(|"
    r"\.only\(|@Disabled|@Ignore|t\.Skip\(")
ASSERTION = re.compile(r"\bassert|expect\(|\.to(Be|Equal|Match|Throw)|assertEqual|assert_|require\.")
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|_spec)\.\w+$|(^|/)test_[^/]+$")


class FixRejected(Exception):
    pass


def normalise_path(p: str) -> str:
    p = p.replace("\\", "/")
    p = re.sub(r"^(?:\./)+", "", p)        # strip "./" prefixes — but never a leading "."
    if p.startswith("/") or ".." in p.split("/"):
        raise FixRejected(f"unsafe path: {p!r}")
    return p


def is_forbidden(path: str) -> bool:
    return any(r.search(path) for r in FORBIDDEN_PATHS)


def apply_edits(files: dict[str, str], edits: list[FileEdit]) -> dict[str, str]:
    """Return {path: new_content} for files that actually changed."""
    if not edits:
        raise FixRejected("proposal contains no edits")
    updated = dict(files)
    for e in edits:
        path = normalise_path(e.path)
        if path not in files:
            raise FixRejected(f"edit targets a file that wasn't provided: {path}")
        if is_forbidden(path):
            raise FixRejected(f"edit targets a forbidden path: {path}")
        if not e.old_string:
            raise FixRejected(f"empty old_string for {path}")
        n = updated[path].count(e.old_string)
        if n != 1:
            raise FixRejected(f"old_string matches {n} times in {path} (must be exactly 1)")
        updated[path] = updated[path].replace(e.old_string, e.new_string, 1)
    return {p: c for p, c in updated.items() if c != files[p]}


def validate(original: dict[str, str], changed: dict[str, str], s: Settings) -> str:
    """Raise FixRejected on any guardrail violation; return the unified diff on success."""
    if not changed:
        raise FixRejected("edits produced no change")
    if len(changed) > s.max_fix_files:
        raise FixRejected(f"touches {len(changed)} files (max {s.max_fix_files})")

    diff_parts: list[str] = []
    changed_lines = 0
    for path, new in changed.items():
        if is_forbidden(path):
            raise FixRejected(f"forbidden path: {path}")
        diff = list(difflib.unified_diff(original[path].splitlines(), new.splitlines(),
                                         f"a/{path}", f"b/{path}", lineterm=""))
        added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
        removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
        changed_lines += len(added) + len(removed)

        if any(SUPPRESSIONS.search(l) for l in added):
            raise FixRejected(f"adds a lint/type suppression in {path}")
        if any(TEST_DISABLERS.search(l) for l in added):
            raise FixRejected(f"adds a skip/xfail/only marker in {path}")
        if TEST_PATH.search(path) and any(ASSERTION.search(l) for l in removed):
            raise FixRejected(f"removes or rewrites an assertion in test file {path}")
        diff_parts.append("\n".join(diff))

    if changed_lines > s.max_fix_changed_lines:
        raise FixRejected(f"changes {changed_lines} lines (max {s.max_fix_changed_lines})")
    return "\n".join(diff_parts)


def fence_safe(text: str) -> str:
    """Log text goes into PR bodies inside code fences; don't let it break out of them."""
    return text.replace("```", "ʼʼʼ")
