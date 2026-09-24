"""Run the XState integration drivers in ONE Node process instead of one each.

Upstream ledger item 54: every driver used to be its own ``subprocess.run([NODE, driver])``,
and on a Windows machine with real-time scanning a bare Node start is ~1.2 s, so the
process starts alone cost ~37 s of a run. `run` and `check` keep the old contract - they
return a ``CompletedProcess`` with the driver's own returncode, stdout and stderr - so each
test still asserts, and fails, on exactly what it did.

Two things keep a case from seeing another's state:

* Node caches a module per URL, and a test may rewrite ``machine.mjs`` and ``drive.mjs``
  in place for a second case. So each case runs from a fresh copy of its directory's
  ``*.mjs`` files; its relative imports then resolve to that copy's own modules.
* A case that hangs is killed on its timeout and the session is restarted for the next
  one, so one bad machine cannot take the rest of the suite with it.
"""

import atexit
import itertools
import json
import queue
import shutil
import subprocess
import threading
from pathlib import Path

NODE = shutil.which("node")
SERVER = Path(__file__).with_name("node_session.mjs")

_proc = None
_lines = None
_case = itertools.count(1)


def _start():
    global _proc, _lines
    # SourceTextModule (the `check` op) is still behind this flag; --no-warnings keeps its
    # ExperimentalWarning out of the cases' stderr.
    _proc = subprocess.Popen(
        [NODE, "--experimental-vm-modules", "--no-warnings", str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8")
    _lines = queue.Queue()
    out = _proc.stdout

    def pump():
        for line in out:
            _lines.put(line)
        _lines.put(None)                        # the session ended

    threading.Thread(target=pump, daemon=True).start()


def _stop():
    global _proc
    if _proc is not None:
        _proc.kill()
        _proc.wait()
        _proc = None


atexit.register(_stop)


def _request(op, path, timeout):
    if _proc is None or _proc.poll() is not None:
        _start()
    args = [NODE, str(path)]
    _proc.stdin.write(json.dumps({"op": op, "path": str(path)}) + "\n")
    _proc.stdin.flush()
    try:
        line = _lines.get(timeout=timeout)
    except queue.Empty:
        _stop()                                 # a hung case: the next one gets a new session
        return subprocess.CompletedProcess(
            args, 1, "", f"node session: {path.name} timed out after {timeout}s\n")
    if line is None:
        err = _proc.stderr.read()
        _stop()
        return subprocess.CompletedProcess(args, 1, "", "node session died\n" + err)
    r = json.loads(line)
    return subprocess.CompletedProcess(args, r["code"], r["stdout"], r["stderr"])


def run(driver, timeout=30):
    """Run ``driver`` (a .mjs file) as ``node driver`` would, in the shared session."""
    driver = Path(driver)
    case = driver.parent / f"_case{next(_case)}"
    case.mkdir()
    for f in driver.parent.glob("*.mjs"):
        shutil.copyfile(f, case / f.name)
    return _request("run", case / driver.name, timeout)


def check(module, timeout=30):
    """Parse ``module`` without running it, as ``node --check module`` would."""
    return _request("check", Path(module), timeout)
