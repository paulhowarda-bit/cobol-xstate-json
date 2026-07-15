"""``python -m cobol_xstate`` - the interpreter-explicit entry point.

Equivalent to the ``cobol-xstate`` console script, but immune to PATH and Windows
file-association surprises (no shim, no ``.py`` on the command line), which makes it
the reliable way to invoke the tool from a script or CI job.
"""

from .cli import main

if __name__ == "__main__":
    main()
