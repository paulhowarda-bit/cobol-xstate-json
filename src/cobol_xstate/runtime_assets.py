"""Access to the JavaScript runtime assets that ship inside the package.

``--target js`` emits a module that does ``import { ... } from './cobolRuntime.mjs'``,
so the runtime has to travel *with the package* and be written next to the emitted
module. Locating it by walking up from ``__file__`` only works in a source checkout —
in an installed wheel there is no repo root above the package — which silently produced
modules with a dangling import. These helpers read it as package data instead, so a
plain ``pip install`` behaves exactly like a checkout.

Assets:
  * ``cobolRuntime.mjs`` - the fixed-point decimal runtime the emitted module imports.
  * ``cobolDriver.mjs``  - the reference driver (PERFORM call-return + file I/O) used
    to drive a machine end-to-end for golden-master testing.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Tuple

from .errors import CobolXstateError

_PACKAGE = "cobol_xstate"
_DIR = "runtime"

#: The runtime asset filenames, in the order a consumer normally needs them.
RUNTIME_FILES: Tuple[str, ...] = ("cobolRuntime.mjs", "cobolDriver.mjs")


class RuntimeAssetMissing(CobolXstateError, RuntimeError):
    """A runtime asset is absent - the install is broken, and staying silent about it
    would emit a module with an unresolvable import. Also a ``RuntimeError`` for
    backward compatibility with callers that caught it as one."""


def _resource(name: str):
    return files(_PACKAGE).joinpath(_DIR, name)


def read_runtime_asset(name: str = "cobolRuntime.mjs") -> str:
    """Return the text of a packaged runtime asset.

    Raises ``RuntimeAssetMissing`` rather than returning empty/None: a caller that
    silently skipped this is exactly how the dangling-import bug shipped.
    """
    if name not in RUNTIME_FILES:
        raise ValueError(f"unknown runtime asset {name!r}; expected one of {RUNTIME_FILES}")
    try:
        return _resource(name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise RuntimeAssetMissing(
            f"{name} is missing from the cobol_xstate package. The emitted module "
            f"imports it, so the install is incomplete - reinstall the package "
            f"(pip install --force-reinstall cobol-xstate)."
        ) from exc


def runtime_asset_path(name: str = "cobolRuntime.mjs") -> Path:
    """Filesystem path to a packaged runtime asset.

    Only valid when the package is installed from a real directory (the normal case);
    prefer :func:`read_runtime_asset` when you just need the text, since it also works
    from a zipimport.
    """
    if name not in RUNTIME_FILES:
        raise ValueError(f"unknown runtime asset {name!r}; expected one of {RUNTIME_FILES}")
    return Path(str(_resource(name)))
