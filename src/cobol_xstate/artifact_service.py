"""The estate's artifact service - the single place this tool talks to mf-fetch.

Every member this tool needs (a copybook, a cataloged PROC, a control-card member, a
called program, a DDL) lives somewhere only the estate knows: a SYSLIB concatenation, a
source-control library, a network share. This tool has no business guessing at that, and
never does - it asks ``mf_fetch`` and reports what came back.

``cast_clients.mf_fetch.fetch_artifact(name, type=, copy=)`` is the default client, so a
normal run needs no wiring. It returns a dict this module keeps whole:

    {artifact_name, detected_type, found, copied_to, source_path,
     source_location, alternatives}

Three of those fields the tool used to throw away, and each one it threw away was an
answer to a question it then went on to guess at:

* ``detected_type`` - the service knows WHAT it found. Our own artifact-kind guess is an
  inference from how a name was used in one program; the service's is from what the
  member actually is. When they disagree, the service wins and the disagreement is
  recorded, because a name we thought was a program and the estate says is an assembler
  module is a finding, not a discrepancy to smooth over.
* ``alternatives`` - the same member name in three libraries is the SYSLIB-order
  ambiguity the artifact manifest otherwise only warns about. Recording which one was
  taken AND what else it could have been is the difference between a resolved dependency
  and a coin flip presented as a fact.
* ``source_location`` / ``source_path`` - a member's identity is the library it came
  from, never the local cache path it landed in.

Older/other clients are still accepted: a bare string, ``(text, source)``, or a dict
with any of the usual text/path keys. The service contract is the caller's, not ours.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# The estate client this tool is built against. Imported lazily (and only once) so the
# package still imports - and every non-fetching code path still runs - on a machine
# that has no estate client installed.
DEFAULT_FETCHER = "cast_clients.mf_fetch:fetch_artifact"

# Keys carrying member TEXT, and keys carrying a PATH, in the order clients use them.
_TEXT_KEYS = ("text", "content", "source", "data", "body")
_PATH_KEYS = ("copied_to", "path", "source_path", "file")
# Keys naming WHERE the member came from - its identity, preferred over a cache path.
_ORIGIN_KEYS = ("source_location", "source_path", "path", "copied_to", "file")


@dataclass
class Fetched:
    """One member, as the estate service returned it."""

    name: str
    text: str
    source: str                                  # where it came from (its identity)
    detected_type: Optional[str] = None          # what the SERVICE says this is
    requested_type: Optional[str] = None         # what WE asked for, if anything
    copied_to: Optional[str] = None              # local copy, when the client made one
    alternatives: List[str] = field(default_factory=list)

    @property
    def type_disagreement(self) -> Optional[str]:
        """Set when we asked for one kind and the service found another - a finding
        about the estate, so it is reported rather than quietly resolved either way."""
        if (self.requested_type and self.detected_type
                and self.requested_type.lower() != self.detected_type.lower()):
            return (f"requested as {self.requested_type}, but the service reports "
                    f"{self.detected_type}")
        return None

    def row(self) -> dict:
        """The reportable facts, omitting what the service did not tell us."""
        out = {"source": self.source, "bytes": len(self.text)}
        for key, val in (("detectedType", self.detected_type),
                         ("copiedTo", self.copied_to)):
            if val:
                out[key] = val
        if self.alternatives:
            # Not noise: the member resolved from ONE library and these are the others
            # that carry the same name. A reader deciding whether this run picked the
            # right one needs to see them.
            out["alternatives"] = list(self.alternatives)
        if self.type_disagreement:
            out["typeNote"] = self.type_disagreement
        return out


class ServiceUnavailable(Exception):
    """The estate client could not be imported or called at all - which is a different
    fact from a member being absent, and must never be reported as one."""


def load_fetcher(spec: Optional[str] = None) -> Tuple[Optional[Callable], Optional[str]]:
    """Import the estate client. ``spec`` is ``MODULE:FUNC`` (or ``MODULE.FUNC``);
    ``None`` means the default mf-fetch client.

    Returns ``(callable, None)`` or ``(None, why_not)``. A missing client is NOT an
    error here: the run continues against local search paths and says, in the report,
    that the estate was never reachable - so an empty result is never mistaken for an
    estate that has nothing."""
    import importlib

    target = spec or DEFAULT_FETCHER
    mod_name, sep, func_name = target.partition(":")
    if not sep:
        mod_name, _, func_name = target.rpartition(".")
    if not mod_name or not func_name:
        return None, (f"{target!r} is not MODULE:FUNC "
                      f"(e.g. {DEFAULT_FETCHER})")
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:
        why = f"{type(exc).__name__}: {exc}"
        if spec is None:
            return None, (f"the estate artifact service ({DEFAULT_FETCHER}) is not "
                          f"available here ({why}) - only members already on the "
                          f"copybook search path can be resolved")
        return None, f"could not import {mod_name} ({why})"
    fn = getattr(mod, func_name, None)
    if fn is None:
        return None, f"{mod_name} has no attribute {func_name}"
    if not callable(fn):
        return None, f"{target} is not callable"
    return fn, None


def _origin(d: dict, name: str) -> str:
    """The member's identity: the library/location it came from, NOT the local cache
    path it was copied to - two programs 'using DC01104' are the same dependency only
    if the same member resolved, so the origin is the evidence."""
    for key in _ORIGIN_KEYS:
        if d.get(key):
            return str(d[key])
    return f"<fetched {name}>"


def _text_from(d: dict) -> Optional[str]:
    for key in _TEXT_KEYS:
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            return val
    # No inline text: a fetch-to-disk client that only reports where it landed.
    for key in _PATH_KEYS:
        path = d.get(key)
        if isinstance(path, str) and os.path.isfile(path):
            with open(path, "r", errors="replace") as fh:
                return fh.read()
    return None


def coerce(got, name: str, requested_type: Optional[str] = None) -> Optional[Fetched]:
    """Whatever the client returned -> ``Fetched``, or ``None`` for 'not retrievable'.

    ``None`` is only ever 'the service was asked and had nothing'. It is never a guess
    and never a stand-in for an error - a client that raised is handled by the caller."""
    if got is None or got is False:
        return None
    if isinstance(got, str):
        return (Fetched(name=name, text=got, source=f"<fetched {name}>",
                        requested_type=requested_type) if got.strip() else None)
    if isinstance(got, (tuple, list)):
        if not got:
            return None
        text = str(got[0])
        source = str(got[1]) if len(got) > 1 and got[1] else f"<fetched {name}>"
        return (Fetched(name=name, text=text, source=source,
                        requested_type=requested_type) if text.strip() else None)
    if isinstance(got, dict):
        if got.get("found") is False:
            return None
        text = _text_from(got)
        if text is None or not text.strip():
            return None
        alts = got.get("alternatives") or []
        if isinstance(alts, (str, bytes)):
            alts = [str(alts)]
        return Fetched(
            name=str(got.get("artifact_name") or name),
            text=text,
            source=_origin(got, name),
            detected_type=(str(got["detected_type"])
                           if got.get("detected_type") else None),
            requested_type=requested_type,
            copied_to=(str(got["copied_to"]) if got.get("copied_to") else None),
            alternatives=[str(a) for a in alts],
        )
    return None


def call_service(fetcher: Callable, name: str, type_hint: Optional[str] = None,
                 copy_to: Optional[str] = None) -> Optional[Fetched]:
    """Ask the service for one member. Raises ``ServiceUnavailable`` if the call itself
    failed; returns ``None`` if the service answered and had nothing.

    Both keyword arguments are OPTIONAL parts of the contract: a client that does not
    accept ``type=`` or ``copy=`` (or names them differently) must not be broken by us,
    so each is dropped on ``TypeError`` and the call retried. The type hint is only ever
    a hint - a service that auto-detects is free to ignore it and tell us, via
    ``detected_type``, what the member really is."""
    attempts = []
    kwargs = {}
    if type_hint:
        kwargs["type"] = type_hint
    if copy_to:
        kwargs["copy"] = copy_to
    if kwargs:
        attempts.append(kwargs)
        if len(kwargs) > 1:                     # drop `copy` before dropping `type`
            attempts.append({k: v for k, v in kwargs.items() if k != "copy"})
    attempts.append({})

    last_type_error = None
    for kw in attempts:
        try:
            got = fetcher(name, **kw)
        except TypeError as exc:
            # Only a SIGNATURE mismatch justifies retrying with fewer arguments; a
            # TypeError raised from inside the client is a real failure and retrying
            # would hide it. We cannot tell them apart perfectly, so we retry and, if
            # every shape fails, report the original.
            last_type_error = exc
            continue
        except Exception as exc:
            raise ServiceUnavailable(f"{type(exc).__name__}: {exc}") from exc
        return coerce(got, name, requested_type=type_hint)
    raise ServiceUnavailable(
        f"{type(last_type_error).__name__}: {last_type_error}")


# Extension used when this tool saves a retrieved member locally, keyed by the estate's
# own type vocabulary. Both stages save through here so a member retrieved as a copybook
# in stage 1 and referenced as one in stage 2 lands under one name, not two.
EXT_FOR_TYPE = {
    "cobol": ".cbl", "copybook": ".cpy", "ddl": ".sql", "cntl": ".txt",
    "bms": ".bms", "csd": ".txt", "jcl": ".jcl", "proc": ".prc", "asm": ".asm",
}


def save_ext(type_name: Optional[str]) -> str:
    return EXT_FOR_TYPE.get((type_name or "").lower(), ".txt")


def save_member(dest: str, name: str, type_name: Optional[str], text: str) -> str:
    """Write a retrieved member into ``dest`` and return the path.

    Used when the client did not copy the member itself (or copied it somewhere other
    than where we were told to collect them), so that ``dest`` is always a complete,
    self-contained directory a later run can be pointed at with ``-I``."""
    os.makedirs(dest, exist_ok=True)
    safe = "".join(c if (c.isalnum() or c in "$#@._-") else "_" for c in name)
    path = os.path.join(dest, safe + save_ext(type_name))
    with open(path, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
    return path


def collect(fetched: "Fetched", dest: Optional[str]) -> "Fetched":
    """Make sure ``fetched`` has a local copy under ``dest``, whatever the client did.

    ``copy=`` is an optional part of the mf-fetch contract and its destination semantics
    are the client's, not ours - so rather than assume the copy landed where we asked,
    we check, and write the text ourselves if it did not. Self-correcting either way."""
    if not dest:
        return fetched
    copied = fetched.copied_to
    if copied and os.path.isfile(copied) and \
            os.path.abspath(copied).startswith(os.path.abspath(dest) + os.sep):
        return fetched                       # the client already put it where we asked
    fetched.copied_to = save_member(
        dest, fetched.name, fetched.detected_type or fetched.requested_type,
        fetched.text)
    return fetched


def normalize_fetched(got, name: str) -> Optional[Tuple[str, str]]:
    """Back-compatible ``(text, source)`` view of :func:`coerce`, for callers that only
    want the member text (the copybook resolver's contract)."""
    fetched = coerce(got, name)
    return (fetched.text, fetched.source) if fetched else None
