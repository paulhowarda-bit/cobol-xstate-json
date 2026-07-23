"""Optional per-stage wall-clock timing for one CLI run (diagnostic only).

Byte-neutral by design: when disabled, ``stage()`` is a no-op yield, ``start()``
returns ``None``, and nothing is emitted. When enabled, per-stage durations are
collected in call order and printed to stderr through the existing logger AFTER
the run completes - no output FILE is ever touched, so the tool's byte-stable
view contract is unaffected. The timing text itself is inherently
non-reproducible (real durations), which is fine: it only ever reaches stderr,
and only behind the explicit ``--timing`` flag.
"""

from __future__ import annotations

import contextlib
import time
from typing import List, Optional, Tuple


class StageTimer:
    """Accumulate labelled wall-clock spans and print them once at the end.

    Two ways to time a span, both no-ops when disabled:
      * ``with timer.stage("parse"): ...``           - a self-contained block
      * ``t0 = timer.start(); ...; timer.since("views", t0)`` - when wrapping the
        block in a ``with`` would force an awkward re-indent (e.g. a long
        if/elif/else).
    """

    def __init__(self, log, enabled: bool, source_name: str) -> None:
        self._log = log
        self._enabled = enabled
        self._src = source_name
        self._rows: List[Tuple[str, float]] = []

    def _record(self, name: str, seconds: float) -> None:
        self._rows.append((name, seconds * 1000.0))

    @contextlib.contextmanager
    def stage(self, name: str):
        if not self._enabled:
            yield
            return
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._record(name, time.perf_counter() - t0)

    def start(self) -> Optional[float]:
        """A perf_counter timestamp, or None when disabled (pairs with since())."""
        return time.perf_counter() if self._enabled else None

    def since(self, name: str, t0: Optional[float]) -> None:
        if self._enabled and t0 is not None:
            self._record(name, time.perf_counter() - t0)

    def report(self) -> None:
        if not self._enabled or not self._rows:
            return
        width = max(len(name) for name, _ in self._rows)
        measured = sum(ms for _, ms in self._rows)
        self._log.info(f"[{self._src}] timing (ms):")
        for name, ms in self._rows:
            self._log.info(f"  {name:<{width}}  {ms:9.1f}")
        # "measured", not "total": cheap I/O and JSON serialization between the
        # timed stages is deliberately unmeasured - the point is to locate the
        # dominant stage, not to reconcile to wall-clock.
        self._log.info(f"  {'measured':<{width}}  {measured:9.1f}")
