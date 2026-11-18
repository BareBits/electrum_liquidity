"""The REAL usage-statistics receiver, running locally for the rig.

`scripts/analytics-endpoint/report.php` is what getbarebits.com serves. This
module stands that exact file up on `php -S` against a throwaway sqlite
database, so the usage-statistics e2e test exercises the deployed code rather
than a stub that merely agrees with it today.

Why bother, when tests/test_usage_stats_receiver.py already drives
`analytics_validate()` directly? Because that test checks the *validator*. It
cannot check the parts that only exist when something is actually serving: the
HTTP envelope, the size cap applied before the body is read, the rate limiter's
transaction, the one-row-per-install upsert, and -- most of the point -- that a
real Electrum daemon's real POST is accepted end to end. Those have failed in
ways a direct function call cannot see.

The files are COPIED into a temp directory rather than served in place, so the
sqlite database (and the WAL files beside it) never appear in the repo.

Requires the `php` binary with `pdo_sqlite`; callers should skip when
`available()` says no.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# scripts/analytics-endpoint/ lives two levels up from e2e/rig/.
ENDPOINT_SRC = Path(__file__).resolve().parent.parent.parent / "scripts" / "analytics-endpoint"
ENDPOINT_FILES = ("common.php", "report.php", "summary.php")
DB_NAME = "liquidity-reports.sqlite"


def _marker_env_name() -> str:
    """The rig's "this process is mine" environment variable. Imported lazily so
    this module stays usable (and testable) without the rig package on the
    path."""
    try:
        from . import paths
        return str(paths.MARKER_ENV)
    except Exception:
        return "ELECTRUM_LIQTEST_RIG"


def php_binary() -> Optional[str]:
    return shutil.which("php")


def available() -> Optional[str]:
    """None when the receiver can run here, else why it cannot.

    A string rather than a bool so the caller's skip message can say which of
    the two things is missing -- php itself, or the sqlite driver, which is a
    separate distro package (php8.3-sqlite3) and the one people actually lack.
    """
    php = php_binary()
    if php is None:
        return "the php binary is not installed"
    try:
        out = subprocess.run(
            [php, "-r", 'echo in_array("sqlite", PDO::getAvailableDrivers()) ? "y" : "n";'],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"php could not be run: {exc}"
    if out.stdout.strip() != "y":
        return ("php has no pdo_sqlite driver (install php-sqlite3, "
                "e.g. apt install php8.3-sqlite3)")
    if not ENDPOINT_SRC.is_dir():
        return f"the endpoint source is missing: {ENDPOINT_SRC}"
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class AnalyticsEndpoint:
    """A locally served copy of the production receiver.

    Use as a context manager, or call ``start()`` / ``stop()``. ``url`` is what
    the plugin's endpoint environment variable should be set to.
    """

    def __init__(self, workdir: Optional[Path] = None) -> None:
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        if workdir is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="liqanalytics_")
            self.dir = Path(self._tmp.name)
        else:
            self.dir = Path(workdir)
            self.dir.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self.proc: Optional[subprocess.Popen] = None
        self.log_path = self.dir / "php-server.log"
        self._log_handle = None

    # --- lifecycle --------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/report.php"

    @property
    def summary_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/summary.php"

    @property
    def db_path(self) -> Path:
        return self.dir / DB_NAME

    def start(self, timeout: float = 30.0) -> "AnalyticsEndpoint":
        reason = available()
        if reason is not None:
            raise RuntimeError(f"cannot start the analytics endpoint: {reason}")
        for name in ENDPOINT_FILES:
            shutil.copy2(ENDPOINT_SRC / name, self.dir / name)
        self._log_handle = open(self.log_path, "ab", buffering=0)
        # Spawned WITHOUT the rig marker in its environment, deliberately.
        #
        # The rig stamps every process it owns with `paths.MARKER_ENV`, and
        # `ProcessManager.kill_previous()` kills anything carrying it -- by
        # marker OR by cmdline, either is enough. A test module imports `run`
        # and calls `_ensure_marked()`, so the pytest process itself carries
        # the marker and any child inherits it. This server is test
        # infrastructure, not a rig service, so inheriting it meant the rig
        # shot the endpoint dead during its own preflight and every report
        # then failed with "connection refused" -- which looks exactly like a
        # plugin that is not reporting.
        env = dict(os.environ)
        env.pop(_marker_env_name(), None)
        self.proc = subprocess.Popen(
            [php_binary(), "-S", f"127.0.0.1:{self.port}", "-t", str(self.dir)],
            cwd=str(self.dir), env=env,
            stdout=self._log_handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
        self._wait_live(timeout)
        return self

    def _wait_live(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last = "no response"
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(
                    f"php -S exited with {self.proc.returncode}; see {self.log_path}")
            try:
                with urllib.request.urlopen(self.url, timeout=5) as response:
                    if json.loads(response.read().decode("utf-8")).get("ok") is True:
                        return
                    last = "liveness check did not answer ok"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = str(exc)
            time.sleep(0.2)
        raise TimeoutError(f"the analytics endpoint never came up: {last}")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
            self.proc = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def cleanup(self) -> None:
        self.stop()
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __enter__(self) -> "AnalyticsEndpoint":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # --- reading what landed ---------------------------------------------

    def reports(self) -> List[Dict[str, Any]]:
        """Every stored report, newest first, with its payload decoded.

        Read straight out of the receiver's own sqlite file: what the test
        asserts on is therefore what the endpoint actually persisted, not what
        it echoed back.
        """
        if not self.db_path.exists():
            return []
        # Read-only, and a copy of the path so a WAL checkpoint in the server
        # process cannot be disturbed by this connection.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY received_at DESC").fetchall()
        except sqlite3.OperationalError:
            return []          # the table does not exist yet: nothing was stored
        finally:
            conn.close()
        out: List[Dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            try:
                record["payload"] = json.loads(record["payload"])
            except (TypeError, ValueError):
                pass
            out.append(record)
        return out

    def report_count(self) -> int:
        return len(self.reports())

    def retained_ips(self) -> int:
        """How many client IPs are currently held for the rate limiter. The
        receiver keeps them for at most two hours and nothing else stores one,
        which is a claim worth being able to check."""
        if not self.db_path.exists():
            return 0
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM recent_requests").fetchone()[0])
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()

    def wait_for_report(self, timeout: float = 60.0,
                        count: int = 1) -> List[Dict[str, Any]]:
        """Wait until at least ``count`` reports have been stored."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reports = self.reports()
            if len(reports) >= count:
                return reports
            time.sleep(0.5)
        raise TimeoutError(
            f"only {self.report_count()} of {count} report(s) arrived within "
            f"{timeout:.0f}s; see {self.log_path}")

    def assert_nothing_arrived(self, settle: float = 5.0) -> None:
        """Give anything in flight time to land, then insist nothing did.

        The negative assertions in the e2e test are the important ones, and a
        negative that is checked too early proves nothing.
        """
        time.sleep(settle)
        reports = self.reports()
        assert not reports, (
            f"{len(reports)} report(s) reached the endpoint when none should "
            f"have: {[r.get('install_id') for r in reports]}")

    def post_raw(self, body: bytes,
                 content_type: str = "application/json") -> tuple:
        """POST a raw body; returns (status, decoded-or-raw). For the tests that
        drive the HTTP layer rather than the plugin."""
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return status, raw
