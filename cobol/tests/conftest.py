"""Development convenience: find jcl-dependencies in its sibling checkout.

The JCL front-end lives in its own repository now. Installed (pip install
jcl-dependencies, or the [jcl] extra) it is simply importable and none of this runs.
From a bare dual-checkout - this repo and jcl-dependencies side by side, nothing
installed - the bridge tests (the auto-fork agreement, --bind-jcl, the dynamic-call
join) would silently skip, which on a developer machine is coverage lost for no reason.
So when the package is not importable but the sibling checkout is there, its src goes
on sys.path.

Deliberately NOT an install and NOT magic beyond this: if neither is present, the
bridge tests skip with the exact pip command, exactly as they do for any user without
the extra. Override the checkout location with JCL_DEPENDENCIES_REPO.
"""

import importlib.util
import os
import sys
from pathlib import Path

if importlib.util.find_spec("jcl_dependencies") is None:
    _sibling = Path(os.environ.get(
        "JCL_DEPENDENCIES_REPO",
        Path(__file__).resolve().parents[2].parent / "jcl-dependencies")) / "src"
    if (_sibling / "jcl_dependencies" / "__init__.py").is_file():
        sys.path.insert(0, str(_sibling))
