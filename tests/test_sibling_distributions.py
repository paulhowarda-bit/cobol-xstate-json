"""The sentinel for the mainframe-common dependencies.

When mainframe-artifacts and cobol-parser are reachable (installed, or via the sibling
checkout), this is a real assertion that they are. When they are not, conftest.py
ignores every other module in this suite - none of them can even import - and THIS one
remains, so the run ends as one clean skip naming the exact pip command instead of a
wall of collection errors.
"""

import importlib.util

import pytest

from _mainframe_common import ensure_on_path


def test_mainframe_common_distributions_are_reachable():
    reason = ensure_on_path()
    if reason is not None:
        pytest.skip(reason)
    for package in ("mainframe_artifacts", "cobol_parser"):
        assert importlib.util.find_spec(package) is not None, package
