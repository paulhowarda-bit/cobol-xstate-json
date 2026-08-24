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
