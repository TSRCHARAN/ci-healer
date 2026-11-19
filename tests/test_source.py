"""Static import resolution: from a failing test file to the repo modules it exercises."""

from ci_healer.source import (files_of_failing_tests, imported_repo_files, python_imports,
                              resolve_module)

TREE = ["shop/__init__.py", "shop/stats.py", "shop/cache.py", "tests/test_stats.py",
        "backend/app/db.py", "backend/app/llm/gemini.py", "api/app/x.py", "worker/app/x.py",
        "pkg/__init__.py", "pkg/tests/test_a.py", "pkg/util.py"]


def test_files_of_failing_tests_from_pytest_ids():
    assert files_of_failing_tests(["tests/test_stats.py::test_median_even", "tests/test_stats.py::test_mean",
                            "TestFoo.testBar"]) == ["tests/test_stats.py"]


def test_python_imports_absolute_and_relative():
    src = ("import os, json\nimport pytest as pt\nfrom shop.stats import (mean,\n    median)\n"
           "from . import util, helpers as h\nfrom ..util import x\n")
    assert python_imports(src, "pkg/tests/test_a.py") == [
        "os", "json", "pytest", "shop.stats", "pkg.tests.util", "pkg.tests.helpers", "pkg.util"]


def test_resolve_module_root_nested_and_ambiguous():
    assert resolve_module("shop.stats", TREE) == "shop/stats.py"
    assert resolve_module("shop", TREE) == "shop/__init__.py"
    assert resolve_module("shop.stats.median", TREE) == "shop/stats.py"   # walks up
    assert resolve_module("app.db", TREE) == "backend/app/db.py"          # unique nested
    assert resolve_module("app.x", TREE) is None                          # two candidates
    assert resolve_module("pytest", TREE) is None                         # not in repo


def test_imported_repo_files_skips_third_party_and_self():
    src = "import pytest\n\nfrom shop.stats import mean, median\nfrom shop.cache import warm_cache\n"
    assert imported_repo_files(src, "tests/test_stats.py", TREE) == ["shop/stats.py", "shop/cache.py"]
