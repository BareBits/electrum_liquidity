"""On-disk diagnostic log for the Inbound Liquidity plugin.

A *sink* — deliberately free of any Electrum imports so it is fully unit-
testable in isolation (mirroring the pure/glue split of `liquidity_manager.py`).
The plugin glue (`__init__.py`) feeds it the very same already-scrubbed entry
dicts it writes to the wallet-db decision log, so by construction this file can
never contain anything more sensitive than the GUI decision-log tab already
shows: ids are abbreviated, and no seeds/private keys/preimages are ever present.

On-disk format is **JSON-lines** (one JSON object per line) under a per-wallet
subfolder, one file per UTC day::

    <base_dir>/<wallet>/inbound_liquidity-YYYY-MM-DD.log

Rotation is implicit (the filename is the day); retention is enforced by pruning
day-files older than ``retention_days`` on every write. All I/O is wrapped so a
logging failure can never disrupt the plugin's automation, and writes stream a
single line at a time (no in-memory accumulation), so the logger's own footprint
stays flat regardless of how busy the wallet is.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from typing import Dict, List, Optional

# Files this logger owns, e.g. "inbound_liquidity-2026-07-02.log". The date is
# the rotation key; the strict pattern means pruning only ever touches our own
# files and never anything else a user might drop in the folder.
_FILE_PREFIX = "inbound_liquidity-"
_FILE_SUFFIX = ".log"
_FILE_RE = re.compile(r"^inbound_liquidity-(\d{4})-(\d{2})-(\d{2})\.log$")

# Wallet names become folder names; keep them to a filesystem-safe charset so a
# wallet called "../evil" or one with path separators can't escape base_dir.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

DEFAULT_RETENTION_DAYS = 30

# Most bytes one day-file may reach. Retention is otherwise purely by DAY, which
# bounds how many files exist but says nothing about how big one gets -- and the
# plugin writes a diagnostic line on every error path, including ones an outside
# party can provoke (a swap provider advertising malformed offers, a peer failing
# to connect). So a single day could grow without limit and fill the disk.
#
# Note what the module docstring's "footprint stays flat" claim does and does not
# cover: writes stream a line at a time, so the LOGGER's memory is flat. That was
# never a statement about the file. This is.
#
# 32 MiB is far more diagnostics than any real day produces, and the cap is per
# file, so retention_days of them is the real ceiling.
MAX_DAY_FILE_BYTES = 32 * 1024 * 1024


def _sanitize_name(name: str) -> str:
    """Reduce an arbitrary wallet name to a safe single path component."""
    cleaned = _SAFE_NAME_RE.sub("_", name or "").strip("._")
    return cleaned or "wallet"


def _day_from_ts(ts: float) -> datetime.date:
    """UTC calendar day for an epoch timestamp (the file's rotation key)."""
    return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).date()


def _iso_from_ts(ts: float) -> str:
    """Human-friendly UTC timestamp embedded in each JSON line for easy grepping."""
    return datetime.datetime.fromtimestamp(
        float(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DiagLog:
    """Append-only, self-rotating, per-wallet JSON-lines diagnostic logger."""

    def __init__(self, base_dir: str,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 max_day_file_bytes: int = MAX_DAY_FILE_BYTES) -> None:
        self.base_dir = base_dir
        self.retention_days = max(1, int(retention_days))
        self.max_day_file_bytes = max(1, int(max_day_file_bytes))
        # Day-files we have already announced as full, so the "stopping" line is
        # written once per file rather than on every subsequent write. Keyed by
        # path; a handful of entries at most (one per wallet per retained day).
        self._capped: set = set()

    # --- paths ------------------------------------------------------------
    def wallet_dir(self, wallet_name: str) -> str:
        return os.path.join(self.base_dir, _sanitize_name(wallet_name))

    def _file_for_day(self, wallet_name: str, day: datetime.date) -> str:
        fname = f"{_FILE_PREFIX}{day.isoformat()}{_FILE_SUFFIX}"
        return os.path.join(self.wallet_dir(wallet_name), fname)

    # --- writing ----------------------------------------------------------
    def write(self, wallet_name: str, entry: Dict) -> bool:
        """Append ``entry`` as one JSON line to today's per-wallet file.

        ``today`` is derived from ``entry['ts']`` (epoch seconds) so the routing
        is deterministic and testable. Returns True on success; never raises —
        a logging fault must not break automation.

        Returns False once the day-file has reached ``max_day_file_bytes``,
        having written one final line saying so. Stopping is the right failure
        here rather than rotating or truncating: a diagnostic log is only useful
        if what it contains is the ORIGINAL sequence of events, and the entries
        most worth keeping are the earliest ones — the beginning of whatever went
        wrong. Dropping the tail of a runaway day loses repetition; dropping the
        head would lose the diagnosis.
        """
        try:
            ts = float(entry.get("ts", 0.0))
            day = _day_from_ts(ts)
            folder = self.wallet_dir(wallet_name)
            os.makedirs(folder, exist_ok=True)
            path = self._file_for_day(wallet_name, day)
            # Prepend a readable UTC time without mutating the caller's dict.
            record = {"time": _iso_from_ts(ts), **entry}
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            encoded = (line + "\n").encode("utf-8")
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0          # not created yet
            if size + len(encoded) > self.max_day_file_bytes:
                if path not in self._capped:
                    self._capped.add(path)
                    self._write_capped_notice(path, ts)
                return False
            with open(path, "ab") as fh:
                fh.write(encoded)
            self._prune(wallet_name, day)
            return True
        except Exception:
            # Swallow every I/O / serialisation error: diagnostics are strictly
            # best-effort and must never propagate into the plugin.
            return False

    def _write_capped_notice(self, path: str, ts: float) -> None:
        """Record, once, that this day-file stopped accepting entries.

        Written as a normal JSON line so a reader parsing the file does not have
        to special-case it, and so the gap in the record is explicit — a log that
        silently stops looks identical to a plugin that silently stopped.
        """
        notice = {
            "time": _iso_from_ts(ts),
            "ts": ts,
            "category": "lifecycle",
            "kind": "diag_log",
            "reason": "diagnostic log reached its size limit for this day",
            "detail": (f"this file reached {self.max_day_file_bytes} bytes; "
                       f"further entries for this day are not recorded"),
        }
        try:
            with open(path, "ab") as fh:
                fh.write((json.dumps(notice, ensure_ascii=False,
                                     sort_keys=True) + "\n").encode("utf-8"))
        except OSError:
            pass

    # --- retention --------------------------------------------------------
    def _prune(self, wallet_name: str, today: datetime.date) -> None:
        """Delete this wallet's day-files older than the retention window."""
        cutoff = today - datetime.timedelta(days=self.retention_days - 1)
        folder = self.wallet_dir(wallet_name)
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            m = _FILE_RE.match(name)
            if not m:
                continue
            try:
                fday = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            if fday < cutoff:
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass

    # --- reading (used by tests / possible future tooling) ----------------
    def read_day(self, wallet_name: str, day: datetime.date) -> List[Dict]:
        """Parse back one day-file into a list of entry dicts (best-effort)."""
        path = self._file_for_day(wallet_name, day)
        out: List[Dict] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        out.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return out
        return out

    def list_day_files(self, wallet_name: str) -> List[str]:
        """Sorted day-file names currently retained for a wallet."""
        try:
            names = [n for n in os.listdir(self.wallet_dir(wallet_name))
                     if _FILE_RE.match(n)]
        except OSError:
            return []
        return sorted(names)
