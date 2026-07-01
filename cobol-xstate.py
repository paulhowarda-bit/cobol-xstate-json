#!/usr/bin/env python3
"""Zero-setup launcher: run the tool straight from a source checkout.

    python cobol-xstate.py <file.cbl> [options]

No install and no PYTHONPATH needed - this puts ``src/`` on the path and calls the
CLI, so a fresh ``git clone`` (or ``git pull``) of the public repo always runs the
current code. (Equivalent to ``pip install -e .`` then ``cobol-xstate ...``.)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from cobol_xstate.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
