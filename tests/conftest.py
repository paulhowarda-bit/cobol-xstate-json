"""Development convenience: find the sibling checkouts this suite builds on.

Two repositories feed this one now. mainframe-common carries the distributions
cobol-xstate depends on (mainframe-artifacts and cobol-parser); jcl-dependencies carries
the JCL front-end that --bind-jcl joins with. Installed (pip install), each is simply
importable and none of this runs. From a bare multi-checkout - the repos side by side,
nothing installed - the suite would fail (mainframe-artifacts / cobol-parser are hard dependencies) or the
bridge tests would silently skip (the JCL join), which on a developer machine is
coverage lost for no reason. So when a package is not importable but its sibling
checkout is there, its src goes on sys.path (see _mainframe_common.py for the
mainframe-artifacts / cobol-parser half; override the locations with MAINFRAME_COMMON_REPO /
JCL_DEPENDENCIES_REPO).

Deliberately NOT an install and NOT magic beyond this. If mainframe-common is neither
installed nor checked out, nothing here can even import - so every module except the
sentinel (test_sibling_distributions.py) is ignored, and the run ends as one clean
skip naming the exact pip command instead of a wall of collection errors. If the JCL
package is absent, only the bridge tests skip, exactly as they do for any user
without the extra.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from _mainframe_common import ensure_on_path

_HERE = Path(__file__).resolve().parent

if ensure_on_path() is not None:
    collect_ignore = sorted(
        p.name for p in _HERE.glob("test_*.py")
        if p.name != "test_sibling_distributions.py")

if importlib.util.find_spec("jcl_dependencies") is None:
    _sibling = Path(os.environ.get(
        "JCL_DEPENDENCIES_REPO",
        _HERE.parent.parent / "jcl-dependencies")) / "src"
    if (_sibling / "jcl_dependencies" / "__init__.py").is_file():
        sys.path.insert(0, str(_sibling))


@pytest.fixture(autouse=True)
def _pin_conventions_off(monkeypatch):
    """Determinism pin: default builds run conventions-less inside the suite.

    mfdep's naming conventions are ALWAYS-ON at build time (mfdep ships in the
    runtime environment; the import is deferred to first need and its absence is a
    loud failure, never a silent fallback) - but a test's expected output can no
    more depend on the day's mfdep.db contents than a golden can. So the suite pins
    build_machine's auto-load off, exactly as tools/byteproof*.py pin their builds
    with conventions=None. test_conventions.py exercises the real path by injecting
    its own Conventions explicitly, which bypasses this pin.
    """
    from cobol_xstate import statechart
    monkeypatch.setattr(statechart, "_load_conventions", lambda: None)
