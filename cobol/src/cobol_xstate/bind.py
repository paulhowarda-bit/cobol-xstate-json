"""``--bind-jcl``: the one place the COBOL and JCL front-ends meet.

The COBOL says *what a program does*; it does not say *what dataset it does it to*. That
binding is in the JCL, so closing it means having both halves in hand. This module is the
only thing in either front-end that knows the other exists, and it reaches for it
LAZILY - so ``import cobol_xstate`` never touches the JCL package, and a COBOL install
without it is a complete, working install that simply cannot do this one join.

The join itself lives in the JCL package (``cobol_xstate_jcl.views.bind_cobol_artifacts``)
even though it serves a COBOL feature, because it consumes a plain manifest **dict** plus
parsed ``Job`` objects and imports nothing COBOL. Moving it here would drag ``Job`` into
this package and make the dependency real in both directions.

**Why the version check.** A skewed pair of packages does not fail visibly on its own: an
unbound manifest looks FINE, because its file rows say exactly what an unbound run's rows
say - "bind the JCL to resolve the DSN". So the contract version is asserted at import
time, where it can still be reported, rather than discovered by reading a report that
quietly did less than it appeared to.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from cobol_xstate_core.prefetch import PrefetchResult

from .errors import CobolXstateError

#: The distribution that provides the JCL half.
JCL_DIST = "cobol-xstate-jcl"

#: The COBOL-artifact-manifest contract this package binds against. Must match
#: ``cobol_xstate_jcl.BIND_API_VERSION``.
BIND_API_VERSION = 1


class JclSupportMissing(CobolXstateError):
    """--bind-jcl was asked for, but the JCL package is not installed or is skewed."""


def available() -> bool:
    """True if the JCL half can be used right now."""
    try:
        _jcl()
    except JclSupportMissing:
        return False
    return True


def _jcl():
    """Import the JCL package, checking that it speaks the contract we bind against."""
    try:
        import cobol_xstate_jcl
    except ImportError as exc:
        raise JclSupportMissing(
            f"--bind-jcl needs the {JCL_DIST} package, which is not installed "
            f"({exc}). Install it with:  pip install cobol-xstate[jcl]"
        ) from exc
    theirs = getattr(cobol_xstate_jcl, "BIND_API_VERSION", None)
    if theirs != BIND_API_VERSION:
        raise JclSupportMissing(
            f"{JCL_DIST} speaks bind-contract version {theirs!r}, but this build binds "
            f"against version {BIND_API_VERSION}. Upgrade whichever is older - a skewed "
            f"pair would produce a manifest that looks bound and is not.")
    return cobol_xstate_jcl


def jcl_api():
    """The JCL package's library facade, or :class:`JclSupportMissing`.

    Used by the COBOL CLI's JCL auto-fork (``cobol-xstate job.jcl``), which is kept for
    one release so existing scripts do not break, and delegates rather than reimplementing.
    """
    _jcl()                       # the install + contract check, with its own messages
    from cobol_xstate_jcl import api
    return api


def bind_jobs(sources: Sequence[Tuple[str, str]], *, fetcher: Optional[Any],
              paths: Sequence[str], dest: Optional[str],
              result: PrefetchResult, unavailable: Optional[str] = None,
              jobs: int = 1, seen: Optional[Any] = None) -> List[Any]:
    """Prefetch and parse each JCL, into the SHARED prefetch result.

    Sharing the result is not an optimization. A ddname the program opens is very often
    contributed by a cataloged PROC rather than by the JCL file itself, so an unresolved
    PROC here does not merely lose steps - it loses the ddname->DSN binding that is the
    entire reason for passing the JCL.
    """
    jcl = _jcl()
    out: List[Any] = []
    for name, text in sources:
        jcl.prefetch_jcl(text, fetcher, paths=list(paths), dest=dest,
                         source_name=name, unavailable=unavailable, result=result,
                         jobs=jobs, seen=seen)
        out.append(jcl.parse_jcl(text, resolver=result.resolver(), source_name=name))
    return out


def bind_manifest(manifest: dict, jcl_jobs: Sequence[Any]) -> dict:
    """Join a COBOL artifact manifest's file ddnames to the datasets the JCL binds."""
    return _jcl().bind_cobol_artifacts(manifest, list(jcl_jobs))
