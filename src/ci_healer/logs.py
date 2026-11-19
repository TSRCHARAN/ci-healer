"""Turn a multi-megabyte GitHub Actions job log into a small, high-signal excerpt.

Why this matters: the useful part of a failed log is usually <5% of it — the tail of the
failing step plus the neighbourhood of each error line. Sending everything to an LLM is slower,
more expensive, and *worse* (the signal gets diluted by setup noise). So we:

1. clean   — strip timestamps and ANSI colour codes;
2. segment — split into steps on `##[group]Run ...` markers;
3. locate  — the failing step is the last segment containing an `##[error]` line;
4. window  — keep lines around error-ish lines + the step's tail, merge overlaps, cap the size;
5. derive  — failing test ids, file references, and a normalised failure *signature*.

Truncation is never silent: omitted spans are replaced with a visible marker.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
ANSI = re.compile(r"(?:\x1b)?\[[0-9;]*m")
STEP_START = re.compile(r"^##\[group\]Run (.+)$")

ERRORISH = re.compile(
    r"##\[error\]|\berror\b|\bERROR\b|\bFAILED\b|\bFAIL\b|Traceback|Exception|panic:|fatal:|"
    r"\bfailed\b|AssertionError|✕|●|would reformat|E\d{3}\b|F\d{3}\b",
    re.IGNORECASE,
)

# Test-id extractors for the common runners. Each yields a stable id we can track over time.
TEST_PATTERNS = [
    re.compile(r"^FAILED ([\w./\\-]+::[^\s]+)"),                 # pytest summary line
    re.compile(r"^--- FAIL: (\S+)"),                              # go test
    re.compile(r"^\s*✕ (.+?)(?: \(\d+ ?m?s\))?$"),               # jest/vitest
    re.compile(r"^\[ERROR\]\s+(\S+\(\S+\))\s"),                    # maven surefire
]

FILE_REF = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|rb|php|cs|"
    r"toml|cfg|ini|json|ya?ml))(?::(\d+))?"
)
RUNNER_WORKSPACE = re.compile(r"^.*?/_?work/([^/]+)/\1/")
# "Node.js 20 is deprecated", "Vue.js" ... — product names, not files.
PRODUCT_NAME = re.compile(r"^[A-Z][A-Za-z]*\.js$")
# Post-step section: git cleanup, service-container teardown, orphan-process reaping. It comes
# AFTER the failing step's ##[error] with no "##[group]Run" marker, so we start a new segment.
POST_STEP = re.compile(r"^Post job cleanup\.$|^Stop and remove container:|^Cleaning up orphan processes")
# GitHub's composite-action bookkeeping (2026 log format) — structure, not content.
ACTION_MARKER = re.compile(r"^##\[(?:start|end)-action ")
NOISE_PATHS = ("site-packages/", "node_modules/", "/opt/hostedtoolcache", "/usr/lib/",
               "dist-packages/", "/_work/_actions/", "<frozen")


@dataclass
class LogExcerpt:
    text: str
    failed_step: str
    error_lines: list[str]
    failing_tests: list[str]
    file_refs: list[str]
    total_lines: int
    kept_lines: int
    signature: str = ""
    step_titles: list[str] = field(default_factory=list)


def clean_line(line: str) -> str:
    return ANSI.sub("", TIMESTAMP.sub("", line.rstrip("\r\n")))


def split_steps(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Segment on `##[group]Run X`. Anything before the first marker is 'Set up job'.

    Imperfect by nature (composite actions, post-steps), which is why we locate the failing
    step by its `##[error]` line rather than by matching step names from the jobs API.
    """
    steps: list[tuple[str, list[str]]] = [("Set up job", [])]
    for line in lines:
        if ACTION_MARKER.match(line):
            continue
        m = STEP_START.match(line)
        if m:
            steps.append((m.group(1).strip(), []))
        elif POST_STEP.match(line) and steps[-1][0] != "Post job cleanup":
            steps.append(("Post job cleanup", []))
        steps[-1][1].append(line)
    return [s for s in steps if s[1]]


def _failing_segment(steps: list[tuple[str, list[str]]]) -> tuple[str, list[str]]:
    for title, body in reversed(steps):
        if any(l.startswith("##[error]") for l in body):
            return title, body
    return steps[-1] if steps else ("", [])


def _windows(body: list[str], before: int, after: int, tail: int) -> list[tuple[int, int]]:
    spans = [(max(0, i - before), min(len(body), i + after + 1))
             for i, l in enumerate(body) if ERRORISH.search(l)]
    spans.append((max(0, len(body) - tail), len(body)))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def normalise(line: str) -> str:
    """Erase the parts of an error line that vary between occurrences of the *same* failure."""
    s = line.lower()
    s = re.sub(r"0x[0-9a-f]+", "<hex>", s)
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", s)
    s = re.sub(r"\b[0-9a-f]{12,40}\b", "<sha>", s)
    s = re.sub(r"(?:/[\w.@-]+)+/([\w.-]+)", r"\1", s)        # keep basename only
    s = re.sub(r"\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds|m)\b", "<dur>", s)
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"\s+", " ", s).strip()


def failure_signature(error_lines: list[str]) -> str:
    """Hash of the first few distinct normalised error lines. Same failure → same signature,
    even across runs with different timings, ports, temp paths or commit SHAs."""
    seen: list[str] = []
    for l in error_lines:
        n = normalise(l)
        if n and n not in seen and "process completed with exit code" not in n:
            seen.append(n)
        if len(seen) == 5:
            break
    return hashlib.sha1("\n".join(seen).encode()).hexdigest()[:12] if seen else ""


def parse_failing_tests(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        for pat in TEST_PATTERNS:
            m = pat.match(line)
            if m and m.group(1) not in out:
                out.append(m.group(1).strip())
    return out


def extract_file_refs(lines: list[str], repo_name: str = "") -> list[str]:
    refs: list[str] = []
    for line in lines:
        if any(n in line for n in NOISE_PATHS) or line.startswith("##[warning]"):
            continue
        for m in FILE_REF.finditer(line):
            path = m.group(1).replace("\\", "/")
            # Runner checkouts live at /home/runner/work/<repo>/<repo>/... (self-hosted:
            # .../_work/<repo>/<repo>/...). Strip only that exact shape — a plain "/<repo>/"
            # match would mangle a source dir that happens to share the repo's name.
            path = RUNNER_WORKSPACE.sub("", path)
            path = re.sub(r"^(?:\./)+", "", path)  # NOT lstrip("./"): that would eat ".github"
            if (path and path not in refs and not path.startswith(("http", "www."))
                    and not PRODUCT_NAME.match(path)):
                refs.append(path)
    return refs[:20]


def extract(raw_log: str, repo_name: str = "", max_lines: int = 250,
            before: int = 25, after: int = 8, tail: int = 60) -> LogExcerpt:
    lines = [clean_line(l) for l in raw_log.splitlines()]
    steps = split_steps(lines)
    title, body = _failing_segment(steps)

    kept: list[str] = []
    for s, e in _windows(body, before, after, tail):
        if kept or s > 0:
            kept.append(f"... [{s} lines omitted] ..." if not kept else "... [lines omitted] ...")
        kept.extend(body[s:e])
    if len(kept) > max_lines:  # keep the end: that's where the final error is
        dropped = len(kept) - max_lines
        kept = [f"... [{dropped} more lines omitted] ..."] + kept[-max_lines:]

    error_lines = [l for l in body if l.startswith("##[error]") or ERRORISH.search(l)]
    signature = failure_signature(error_lines)
    if not signature:
        # Nothing "error-looking" besides the exit code (e.g. a shell `test` that just exits 1):
        # fingerprint the last few output lines of the step instead.
        tail_out = [l for l in body if l.strip() and not l.startswith("##[")][-5:]
        signature = failure_signature(tail_out)
    return LogExcerpt(
        text="\n".join(kept),
        failed_step=title,
        error_lines=error_lines[:50],
        failing_tests=parse_failing_tests(body),
        file_refs=extract_file_refs(body, repo_name),
        total_lines=len(lines),
        kept_lines=len(kept),
        signature=signature,
        step_titles=[t for t, _ in steps],
    )
