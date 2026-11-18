"""Spawn, track and *scope-kill* the rig's child processes.

Safety contract
---------------
The machine may be running unrelated ``bitcoind`` / ``electrum`` / ``Fulcrum``
processes (e.g. another rig or the user's real wallet). This module must
**never** kill those. Ownership is therefore never inferred from a process
*name*; it has to be proven, and there are three independent proofs:

* the marker environment variable (``paths.MARKER_ENV`` == ``paths.MARKER_VALUE``)
  stamped onto every child we start (see ``spawn``). Discovery walks
  ``/proc/<pid>/environ``, readable for our own uid. Because the marker value is
  the absolute run directory of this rig checkout, a match unambiguously means
  "ours" -- covering grandchildren and re-parented daemons a PID list would miss;
* the run-dir path in ``/proc/<pid>/cmdline``, which stays readable even for a
  non-dumpable process. This is what reaches the Electrum daemon, which marks
  itself non-dumpable because it holds wallet keys, so its ``environ`` is
  unreadable to us;
* for the ORCHESTRATOR itself, the pid recorded in ``state.json`` together with
  that process's start time, which makes the record immune to PID reuse (see
  ``_previous_orchestrator_pid``).

That third proof exists because the orchestrator cannot stamp its own
``/proc/environ``: a process's environ block is captured at ``exec`` time and
cannot be rewritten afterwards. The rig used to work around that by re-``execv``
-ing itself at startup, which is destructive when the orchestrator is a test
runner -- it restarts the whole pytest session, and discards any output pytest
had captured. Recording the pid instead costs one file write and is exact.

The nostr relay runs as a Docker container rather than a plain child; it is
tracked separately by its fixed, rig-scoped container name (see ``paths``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Optional

from . import paths


@dataclass
class ManagedProcess:
    """A single child process started by the rig."""

    name: str
    pid: int
    log_path: Path
    _log_handle: Optional[IO[bytes]] = field(default=None, repr=False)

    def is_alive(self) -> bool:
        return _pid_alive(self.pid)


class ProcessManager:
    """Starts children with the rig marker and tears them down by marker."""

    def __init__(self) -> None:
        self.children: list[ManagedProcess] = []

    # -- startup ------------------------------------------------------------
    def spawn(
        self,
        name: str,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[Path] = None,
    ) -> ManagedProcess:
        """Launch ``argv`` as a new session leader, stamped with the marker.

        ``start_new_session=True`` puts the child in its own process group so we
        can later signal the whole group (catching any helpers it forks).
        """
        full_env = dict(os.environ if env is None else env)
        full_env[paths.MARKER_ENV] = paths.MARKER_VALUE

        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = paths.LOG_DIR / f"{name}.log"
        log_handle = open(log_path, "ab", buffering=0)

        proc = subprocess.Popen(
            argv,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            start_new_session=True,
        )
        managed = ManagedProcess(name=name, pid=proc.pid, log_path=log_path, _log_handle=log_handle)
        self.children.append(managed)
        self._write_state()
        return managed

    # -- discovery ----------------------------------------------------------
    @staticmethod
    def find_rig_pids() -> list[int]:
        """All PIDs (excluding ourselves) belonging to this rig.

        Ownership is proven three complementary ways -- see the module docstring
        for why each is needed:

        * the ``ELECTRUM_LIQTEST_RIG`` env marker in ``/proc/<pid>/environ`` --
          catches any dumpable child;
        * the run-dir path appearing in ``/proc/<pid>/cmdline`` -- catches the
          services whose argv references ``.run/`` (bitcoind ``-datadir``,
          Fulcrum's conf, each Electrum ``--dir``, the docker ``-v`` mount).
          Essential because Electrum's daemon marks itself non-dumpable (it holds
          wallet keys), so its ``environ`` is unreadable to us;
        * a still-running orchestrator recorded in ``state.json`` -- which a scan
          of ``/proc`` cannot find, because a process cannot stamp the marker into
          its own already-exec'd environ.

        ``me`` is excluded, so the orchestrator never finds ITSELF through the
        state file it just wrote. That matters most under a test runner, where one
        pytest process is the orchestrator for every rig in the session: without
        the exclusion the first teardown would kill the test run.

        Note what is deliberately NOT a proof: a process merely having the run
        dir open, or being named like a rig service. A developer tailing
        ``.run/logs/bitcoind.log`` must not be swept up, and nor must their real
        wallet.
        """
        me = os.getpid()
        found: list[int] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == me:
                continue
            if _proc_marker(pid) == paths.MARKER_VALUE or _proc_cmdline_is_ours(pid):
                found.append(pid)
        previous = _previous_orchestrator_pid()
        if previous is not None and previous != me and previous not in found:
            found.append(previous)
        return found

    # -- teardown -----------------------------------------------------------
    def kill_previous(self) -> list[int]:
        """Kill leftovers from an earlier, ungracefully-stopped run.

        Matches purely on the marker, so a prior orchestrator process *and* all
        its children are reaped, while unrelated instances are left alone. Also
        force-removes the rig's nostr container (a Docker container is not a
        marked child, so it must be cleaned up by name).
        """
        remove_nostr_container()
        stale = self.find_rig_pids()
        if stale:
            _kill_pids(stale, grace=5.0)
        return stale

    def shutdown(self, *, grace: float = 8.0) -> None:
        """Terminate everything this run started, then marker-sweep stragglers.

        Our own children were started as session leaders, so we can safely
        SIGTERM their *process groups* (reaping any helpers they forked). The
        follow-up marker sweep kills by individual PID -- never by group --
        because a discovered process might share a group with something that is
        NOT ours (e.g. the shell that launched us).
        """
        remove_nostr_container()
        for child in self.children:
            _signal_own_group(child.pid, signal.SIGTERM)
        _wait_gone([c.pid for c in self.children], grace)

        remaining = self.find_rig_pids()
        if remaining:
            _kill_pids(remaining, grace=grace / 2)

        for child in self.children:
            if child._log_handle is not None:
                child._log_handle.close()

    # -- persistence --------------------------------------------------------
    def _write_state(self) -> None:
        """Persist who we are and what we started.

        ``orchestrator_starttime`` is what makes ``orchestrator_pid`` safe to act
        on later: a bare pid in a file is a liability, because by the time the
        next run reads it the kernel may have recycled it onto something
        unrelated. Pairing it with the process's start time identifies the
        process rather than the slot -- see ``_previous_orchestrator_pid``.
        """
        paths.RUN_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "orchestrator_pid": os.getpid(),
            "orchestrator_starttime": _proc_starttime(os.getpid()),
            "marker": paths.MARKER_VALUE,
            "children": [{"name": c.name, "pid": c.pid, "log": str(c.log_path)} for c in self.children],
        }
        paths.STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# Docker (nostr relay) helpers
# --------------------------------------------------------------------------
def remove_nostr_container() -> None:
    """Force-remove the rig's nostr container if it exists (idempotent).

    Scoped strictly to our fixed container name, so other Docker workloads are
    never affected.
    """
    subprocess.run(
        ["docker", "rm", "-f", paths.NOSTR_CONTAINER],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------
def _proc_marker(pid: int) -> Optional[str]:
    """Return the rig marker value from a process's environ, or None."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            raw = handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    needle = paths.MARKER_ENV.encode() + b"="
    for item in raw.split(b"\x00"):
        if item.startswith(needle):
            return item[len(needle):].decode(errors="replace")
    return None


def _proc_cmdline_is_ours(pid: int) -> bool:
    """True if ``pid``'s argv references this rig's run dir.

    Unlike ``environ``, ``/proc/<pid>/cmdline`` stays readable even for
    non-dumpable processes (e.g. the Electrum daemon), so this is what lets us
    discover and reap a leftover daemon from an ungraceful prior run. We exclude
    a bare shell/grep that merely *mentions* the path by also requiring a rig
    service binary token in the argv.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    cmdline = raw.replace(b"\x00", b" ")
    if paths.MARKER_VALUE.encode() not in cmdline:
        return False
    service_tokens = (b"bitcoind", b"Fulcrum", b"/electrum", b"electrum ",
                      b"docker", paths.NOSTR_CONTAINER.encode())
    return any(tok in cmdline for tok in service_tokens)


def _proc_starttime(pid: int) -> Optional[int]:
    """The process's start time in clock ticks since boot, or None.

    Field 22 of ``/proc/<pid>/stat``. Parsed from after the last ``)`` because
    field 2 is the executable name in parentheses and may itself contain spaces
    and parentheses -- splitting the whole line on whitespace is the classic way
    to misread this file.

    Paired with a pid, this identifies a PROCESS rather than a pid slot: the
    kernel can hand the same pid to something else, but not with the same start
    time.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    try:
        tail = raw[raw.rindex(b")") + 1:].split()
        return int(tail[19])        # field 22 overall, 20th after the comm field
    except (ValueError, IndexError):
        return None


def _previous_orchestrator_pid() -> Optional[int]:
    """The pid of an orchestrator from an earlier run that is STILL RUNNING, or
    None.

    Read out of ``state.json``, which the previous run wrote and which is still
    on disk at this point -- ``preflight`` sweeps before it wipes the run dir.

    Every condition here is a safety gate, because the outcome is a SIGKILL:

    * the file has to name this rig (``marker``), so a state file copied from
      another checkout is ignored;
    * the pid has to still be alive;
    * its start time has to match what was recorded. Without this the record
      would be a pid-reuse hazard -- the next run could read a months-old file
      and kill whatever innocent process now holds that number.

    Anything missing, malformed or mismatched yields None. Failing to find a
    previous orchestrator is cheap (its children are still discoverable by
    marker and cmdline); killing the wrong process is not.
    """
    try:
        state = json.loads(paths.STATE_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    if state.get("marker") != paths.MARKER_VALUE:
        return None
    pid, recorded = state.get("orchestrator_pid"), state.get("orchestrator_starttime")
    if not isinstance(pid, int) or not isinstance(recorded, int):
        # A state file from a build before the start time was recorded. Declining
        # is deliberate: a pid alone cannot be acted on safely.
        return None
    if pid <= 0 or not _pid_alive(pid):
        return None
    if _proc_starttime(pid) != recorded:
        return None      # the pid was recycled onto a different process
    return pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_pid(pid: int, sig: int) -> None:
    """Signal a single PID (used for discovered/leftover processes, where
    group-killing would be unsafe). Ownership is already proven by the marker.
    """
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _signal_own_group(pid: int, sig: int) -> None:
    """Signal the process group led by ``pid`` -- safe ONLY for children we
    started with ``start_new_session=True`` (they lead their own group)."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        _signal_pid(pid, sig)


def _wait_gone(pids: list[int], grace: float) -> list[int]:
    """Wait up to ``grace`` seconds for ``pids`` to exit; return survivors."""
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        alive = [p for p in pids if _pid_alive(p)]
        if not alive:
            return []
        time.sleep(0.15)
    return [p for p in pids if _pid_alive(p)]


def _kill_pids(pids: list[int], *, grace: float) -> None:
    """SIGTERM each PID individually, wait, then SIGKILL the survivors.

    Per-PID (not per-group) on purpose: these PIDs come from the marker sweep
    and may include a previous orchestrator whose group is shared with an
    unrelated shell. The marker already proves each PID is ours.
    """
    for pid in pids:
        _signal_pid(pid, signal.SIGTERM)
    survivors = _wait_gone(pids, grace)
    for pid in survivors:
        _signal_pid(pid, signal.SIGKILL)
