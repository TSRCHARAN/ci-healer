"""Find the source files a failing test depends on — without running anything.

Why this exists: a lint error says `shop/stats.py:3`, so the fixer knows where to look. A
failing *test* only says `tests/test_stats.py::test_median_even` — the bug is in whatever that
test imports, and the log never mentions it. Seen in the public demo: both Gemini models
diagnosed an off-by-one precisely but declined to fix it, because the broken line was not in
anything they'd been shown.

So, from the test file's text, we statically resolve its imports to repo files. Static means:
regex over `import` lines matched against the commit's file tree. No code is executed, and
anything not in the tree (stdlib, third-party packages) simply doesn't resolve.

Python only for now. Other languages fall back to what the log already names.
"""

from __future__ import annotations

import re

IMPORT_LINE = re.compile(
    r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+\(?([^\n)]+)|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))", re.M)


def files_of_failing_tests(failing_tests: list[str]) -> list[str]:
    """`tests/test_stats.py::test_median_even` -> `tests/test_stats.py` (pytest ids only)."""
    out: list[str] = []
    for t in failing_tests:
        path = t.split("::", 1)[0]
        if path.endswith(".py") and path not in out:
            out.append(path)
    return out


def python_imports(source: str, from_path: str) -> list[str]:
    """Modules a Python file imports, as dotted names. Relative imports are resolved against
    the file's own package so `from .util import x` in `pkg/tests/test_a.py` -> `pkg.tests.util`."""
    package = from_path.rsplit("/", 1)[0].replace("/", ".") if "/" in from_path else ""
    mods: list[str] = []
    for m in IMPORT_LINE.finditer(source):
        if m.group(1) is not None:                       # from X import a, b
            name = m.group(1)
            dots = len(name) - len(name.lstrip("."))
            if dots:
                base = package.split(".") if package else []
                base = base[: len(base) - (dots - 1)] if dots - 1 else base
                name = ".".join(base + ([name.lstrip(".")] if name.lstrip(".") else []))
            names = [n.strip().split(" as ")[0].strip() for n in m.group(2).split(",")]
            if m.group(1).lstrip(".") == "" and dots:    # `from . import util` -> pkg.util
                mods += [f"{name}.{n}" if name else n for n in names if n and n != "*"]
            elif name:
                mods.append(name)
        else:                                            # import a.b, c
            mods += [n.strip().split(" as ")[0].strip() for n in m.group(3).split(",")]
    return list(dict.fromkeys(mods))


def resolve_module(module: str, tree: list[str]) -> str | None:
    """Map `shop.stats` to `shop/stats.py` (or `shop/stats/__init__.py`) within the commit's
    tree. A repo-root match wins; otherwise accept a nested match (`backend/shop/stats.py`)
    only if it's unambiguous. Walks up the dotted name so `shop.stats.helpers` still finds
    `shop/stats.py` when `helpers` is a name inside it."""
    parts = module.split(".")
    tree_set = set(tree)
    while parts:
        rel = "/".join(parts)
        for candidate in (f"{rel}.py", f"{rel}/__init__.py"):
            if candidate in tree_set:
                return candidate
            nested = [p for p in tree if p.endswith("/" + candidate)]
            if len(nested) == 1:
                return nested[0]
        parts.pop()
    return None


def imported_repo_files(test_source: str, test_path: str, tree: list[str]) -> list[str]:
    """Repo files the test imports, source before tests, test file itself excluded."""
    found: list[str] = []
    for mod in python_imports(test_source, test_path):
        path = resolve_module(mod, tree)
        if path and path != test_path and path not in found:
            found.append(path)
    return found
