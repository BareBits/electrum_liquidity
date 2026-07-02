"""Unit tests for the pure on-disk diagnostic logger (`diag_log.DiagLog`).

No Electrum imports: the module is deliberately standalone. `conftest.py` puts
the package dir on sys.path so `diag_log` imports directly.
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Dict

import pytest

from diag_log import (  # type: ignore  (package dir on sys.path via conftest)  # noqa: E402
    DiagLog,
    _sanitize_name,
    _day_from_ts,
)


def _ts(y: int, m: int, d: int, hh: int = 12) -> float:
    """Epoch seconds for a UTC calendar day (stable across the test host's tz)."""
    return datetime.datetime(y, m, d, hh, tzinfo=datetime.timezone.utc).timestamp()


def _entry(ts: float, **over) -> Dict:
    base = {
        "ts": ts, "category": "action", "kind": "swap", "amount_sat": 25_000,
        "source": "117x1x0", "dest": "on-chain", "reason": "channel over trigger",
        "detail": None, "state": {},
    }
    base.update(over)
    return base


# --- routing & format -----------------------------------------------------
def test_write_creates_dated_file_in_wallet_subfolder(tmp_path) -> None:
    log = DiagLog(str(tmp_path))
    ts = _ts(2026, 7, 2)
    assert log.write("mywallet", _entry(ts)) is True
    expected = tmp_path / "mywallet" / "inbound_liquidity-2026-07-02.log"
    assert expected.exists()
    lines = expected.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["reason"] == "channel over trigger"
    assert rec["time"] == "2026-07-02T12:00:00Z"   # readable ts injected
    assert rec["ts"] == ts                          # original fields preserved


def test_entries_route_to_per_day_files(tmp_path) -> None:
    log = DiagLog(str(tmp_path), retention_days=999)
    log.write("w", _entry(_ts(2026, 7, 1), reason="day1"))
    log.write("w", _entry(_ts(2026, 7, 2), reason="day2a"))
    log.write("w", _entry(_ts(2026, 7, 2), reason="day2b"))
    assert log.list_day_files("w") == [
        "inbound_liquidity-2026-07-01.log",
        "inbound_liquidity-2026-07-02.log",
    ]
    day2 = log.read_day("w", datetime.date(2026, 7, 2))
    assert [e["reason"] for e in day2] == ["day2a", "day2b"]  # append order


def test_write_does_not_mutate_callers_entry(tmp_path) -> None:
    log = DiagLog(str(tmp_path))
    e = _entry(_ts(2026, 7, 2))
    log.write("w", e)
    assert "time" not in e   # the readable ts is added to a copy, not the caller's dict


# --- name safety ----------------------------------------------------------
def test_wallet_name_is_sanitised_and_cannot_escape_base(tmp_path) -> None:
    log = DiagLog(str(tmp_path))
    log.write("../../etc/evil", _entry(_ts(2026, 7, 2)))
    # Nothing written outside base_dir; the traversal collapsed to a safe name.
    produced = list(tmp_path.rglob("inbound_liquidity-*.log"))
    assert len(produced) == 1
    assert tmp_path in produced[0].parents
    assert ".." not in str(produced[0].relative_to(tmp_path))


def test_sanitize_name_edges() -> None:
    assert _sanitize_name("a/b\\c") == "a_b_c"
    assert _sanitize_name("") == "wallet"
    assert _sanitize_name("...") == "wallet"
    assert _sanitize_name("good.name-1") == "good.name-1"


# --- retention pruning ----------------------------------------------------
def test_prune_drops_files_older_than_retention_on_write(tmp_path) -> None:
    log = DiagLog(str(tmp_path), retention_days=30)
    # Seed an old day and a recent day directly.
    log.write("w", _entry(_ts(2026, 6, 1), reason="old"))
    assert (tmp_path / "w" / "inbound_liquidity-2026-06-01.log").exists()
    # A write 30+ days later prunes the old file (window is [day-29 .. day]).
    log.write("w", _entry(_ts(2026, 7, 2), reason="new"))
    assert not (tmp_path / "w" / "inbound_liquidity-2026-06-01.log").exists()
    assert (tmp_path / "w" / "inbound_liquidity-2026-07-02.log").exists()


def test_retention_window_boundary_is_inclusive(tmp_path) -> None:
    # retention_days=30 keeps today and the 29 preceding days.
    log = DiagLog(str(tmp_path), retention_days=30)
    log.write("w", _entry(_ts(2026, 6, 3), reason="edge_kept"))     # 29 days before 7-2
    log.write("w", _entry(_ts(2026, 6, 2), reason="edge_dropped"))  # 30 days before -> out
    log.write("w", _entry(_ts(2026, 7, 2), reason="today"))
    files = log.list_day_files("w")
    assert "inbound_liquidity-2026-06-03.log" in files
    assert "inbound_liquidity-2026-06-02.log" not in files


def test_prune_ignores_foreign_files(tmp_path) -> None:
    log = DiagLog(str(tmp_path), retention_days=1)
    folder = tmp_path / "w"
    folder.mkdir()
    (folder / "notes.txt").write_text("keep me", encoding="utf-8")
    (folder / "inbound_liquidity-2020-01-01.log").write_text("old\n", encoding="utf-8")
    log.write("w", _entry(_ts(2026, 7, 2)))
    assert (folder / "notes.txt").exists()                              # untouched
    assert not (folder / "inbound_liquidity-2020-01-01.log").exists()   # pruned


# --- robustness -----------------------------------------------------------
def test_write_never_raises_on_bad_base_dir(tmp_path) -> None:
    # base_dir sits *under* a regular file -> makedirs must fail; write returns False.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    log = DiagLog(str(blocker / "logs"))
    assert log.write("w", _entry(_ts(2026, 7, 2))) is False


def test_read_day_missing_file_is_empty(tmp_path) -> None:
    log = DiagLog(str(tmp_path))
    assert log.read_day("nope", datetime.date(2026, 7, 2)) == []


def test_day_from_ts_is_utc() -> None:
    assert _day_from_ts(_ts(2026, 7, 2, hh=23)) == datetime.date(2026, 7, 2)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
